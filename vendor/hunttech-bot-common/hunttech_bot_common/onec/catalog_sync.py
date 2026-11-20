"""Catalog synchronization for 1C integration.

Provides incremental and full synchronization of 1C catalogs
with local storage, conflict resolution, and progress tracking.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from hunttech_bot_common.onec.client import OneCClient
from hunttech_bot_common.onec.config import CatalogSyncSettings, OneCConfig
from hunttech_bot_common.onec.mapper import (
    CATALOG_MAPPERS,
    map_from_onec,
    map_to_onec,
)
from hunttech_bot_common.onec.models import (
    OneCRef,
    ODataResponse,
    SyncResult,
    SyncState,
)

logger = logging.getLogger(__name__)


@dataclass
class CatalogSyncEngine:
    """Engine for synchronizing 1C catalogs with local storage.

    Features:
    - Incremental sync using modification timestamps
    - Full sync with configurable intervals
    - Conflict resolution strategies
    - Persistent sync state
    - Progress callbacks
    """

    client: OneCClient
    settings: CatalogSyncSettings
    state_dir: Path
    progress_callback: Any | None = None  # Callable[[SyncResult], None]

    def __post_init__(self) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self._states: dict[str, SyncState] = {}
        self._load_all_states()

    def _load_all_states(self) -> None:
        """Load sync states for all catalogs."""
        for state_file in self.state_dir.glob("*.json"):
            try:
                catalog_name = state_file.stem
                data = json.loads(state_file.read_text(encoding="utf-8"))
                self._states[catalog_name] = SyncState(
                    catalog_name=catalog_name,
                    last_sync_timestamp=datetime.fromisoformat(data["last_sync_timestamp"])
                    if data.get("last_sync_timestamp")
                    else None,
                    last_sync_token=data.get("last_sync_token"),
                    last_full_sync=datetime.fromisoformat(data["last_full_sync"])
                    if data.get("last_full_sync")
                    else None,
                    etag=data.get("etag"),
                )
            except Exception as e:
                logger.warning("Failed to load sync state for %s: %s", state_file, e)

    def _save_state(self, state: SyncState) -> None:
        """Persist sync state to disk."""
        state_file = self.state_dir / f"{state.catalog_name}.json"
        data = {
            "catalog_name": state.catalog_name,
            "last_sync_timestamp": state.last_sync_timestamp.isoformat()
            if state.last_sync_timestamp
            else None,
            "last_sync_token": state.last_sync_token,
            "last_full_sync": state.last_full_sync.isoformat()
            if state.last_full_sync
            else None,
            "etag": state.etag,
        }
        state_file.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        self._states[state.catalog_name] = state

    def get_state(self, catalog_name: str) -> SyncState:
        """Get or create sync state for a catalog."""
        if catalog_name not in self._states:
            self._states[catalog_name] = SyncState(catalog_name=catalog_name)
        return self._states[catalog_name]

    def needs_full_sync(self, catalog_name: str) -> bool:
        """Check if catalog needs full synchronization."""
        state = self.get_state(catalog_name)
        if state.last_full_sync is None:
            return True
        elapsed = datetime.now() - state.last_full_sync
        return elapsed >= timedelta(hours=self.settings.full_sync_interval_hours)

    def needs_incremental_sync(self, catalog_name: str) -> bool:
        """Check if catalog needs incremental synchronization."""
        state = self.get_state(catalog_name)
        if state.last_sync_timestamp is None:
            return True
        elapsed = datetime.now() - state.last_sync_timestamp
        return elapsed >= timedelta(minutes=self.settings.incremental_sync_interval_minutes)

    def should_sync_catalog(self, catalog_name: str) -> bool:
        """Check if catalog should be synchronized based on settings."""
        if self.settings.enabled_catalogs:
            return catalog_name in self.settings.enabled_catalogs
        return True

    async def sync_catalog(
        self,
        catalog_name: str,
        *,
        force_full: bool = False,
        batch_size: int | None = None,
    ) -> SyncResult:
        """Synchronize a single catalog.

        Args:
            catalog_name: 1C catalog name (e.g., "Counterparties", "Nomenclature")
            force_full: Force full sync even if incremental is due
            batch_size: Override default batch size

        Returns:
            SyncResult with statistics.
        """
        if not self.should_sync_catalog(catalog_name):
            logger.info("Catalog %s is disabled, skipping", catalog_name)
            return SyncResult(catalog_name=catalog_name, errors=["Catalog disabled in settings"])

        start_time = time.perf_counter()
        batch_size = batch_size or self.settings.batch_size
        state = self.get_state(catalog_name)

        # Determine sync mode
        do_full_sync = force_full or self.needs_full_sync(catalog_name)
        if not do_full_sync and not self.needs_incremental_sync(catalog_name):
            logger.info("Catalog %s is up to date, skipping", catalog_name)
            return SyncResult(catalog_name=catalog_name, errors=["Up to date"])

        logger.info("Starting %s sync for catalog %s", "full" if do_full_sync else "incremental", catalog_name)

        result = SyncResult(catalog_name=catalog_name)

        try:
            if do_full_sync:
                result = await self._full_sync(catalog_name, batch_size, result)
            else:
                result = await self._incremental_sync(catalog_name, batch_size, result)

            # Update state
            state.last_sync_timestamp = datetime.now()
            if do_full_sync:
                state.last_full_sync = datetime.now()
            self._save_state(state)

        except Exception as e:
            logger.exception("Sync failed for catalog %s", catalog_name)
            result.errors.append(f"Sync failed: {e}")

        result.duration_seconds = time.perf_counter() - start_time

        if self.progress_callback:
            try:
                self.progress_callback(result)
            except Exception as e:
                logger.warning("Progress callback failed: %s", e)

        logger.info(
            "Sync completed for %s: %d created, %d updated, %d deleted, %d errors in %.2fs",
            catalog_name,
            result.created,
            result.updated,
            result.deleted,
            len(result.errors),
            result.duration_seconds,
        )

        return result

    async def _full_sync(
        self,
        catalog_name: str,
        batch_size: int,
        result: SyncResult,
    ) -> SyncResult:
        """Perform full catalog synchronization."""
        # Fetch all items
        async for item in self.client.iter_catalog(catalog_name, batch_size=batch_size):
            result.total_fetched += 1
            try:
                mapped = map_from_onec(catalog_name, item)
                await self._upsert_local(catalog_name, mapped, result)
            except Exception as e:
                result.errors.append(f"Item {item.get('Ref_Key', 'unknown')}: {e}")

        # Mark locally stored items not in 1C as deleted (optional - depends on strategy)
        # For now we don't auto-delete, just mark deletion_mark

        return result

    async def _incremental_sync(
        self,
        catalog_name: str,
        batch_size: int,
        result: SyncResult,
    ) -> SyncResult:
        """Perform incremental catalog synchronization."""
        state = self.get_state(catalog_name)

        # Build filter for modified since last sync
        filter_parts = []
        if state.last_sync_timestamp:
            ts = state.last_sync_timestamp.isoformat()
            filter_parts.append(f"ModificationDate ge datetime'{ts}'")

        # Also include items marked for deletion
        filter_parts.append("DeletionMark eq true")

        filter_str = " or ".join(f"({p})" for p in filter_parts) if filter_parts else None

        async for item in self.client.iter_catalog(
            catalog_name,
            filter_=filter_str,
            batch_size=batch_size,
        ):
            result.total_fetched += 1
            try:
                mapped = map_from_onec(catalog_name, item)
                if item.get("DeletionMark"):
                    await self._mark_deleted_local(catalog_name, mapped, result)
                else:
                    await self._upsert_local(catalog_name, mapped, result)
            except Exception as e:
                result.errors.append(f"Item {item.get('Ref_Key', 'unknown')}: {e}")

        return result

    async def _upsert_local(
        self,
        catalog_name: str,
        item: dict[str, Any],
        result: SyncResult,
    ) -> None:
        """Insert or update local item. Override in subclass."""
        # This is a placeholder - actual implementation depends on storage backend
        # Subclasses should override this to persist to their database
        key = item.get("key") or item.get("Ref_Key")
        if not key:
            result.errors.append("Item missing key")
            return

        # Check if exists locally (placeholder)
        exists = await self._local_exists(catalog_name, key)
        if exists:
            result.updated += 1
        else:
            result.created += 1

    async def _mark_deleted_local(
        self,
        catalog_name: str,
        item: dict[str, Any],
        result: SyncResult,
    ) -> None:
        """Mark local item as deleted. Override in subclass."""
        key = item.get("key") or item.get("Ref_Key")
        if not key:
            return
        # Placeholder - actual implementation depends on storage
        result.deleted += 1

    async def _local_exists(self, catalog_name: str, key: str) -> bool:
        """Check if item exists locally. Override in subclass."""
        return False

    async def sync_all(
        self,
        *,
        force_full: bool = False,
        catalogs: list[str] | None = None,
    ) -> list[SyncResult]:
        """Synchronize all configured catalogs."""
        target_catalogs = catalogs or list(CATALOG_MAPPERS.keys())
        results = []

        for catalog_name in target_catalogs:
            if not self.should_sync_catalog(catalog_name):
                continue
            result = await self.sync_catalog(catalog_name, force_full=force_full)
            results.append(result)

        return results

    async def push_local_changes(
        self,
        catalog_name: str,
        local_items: list[dict[str, Any]],
        *,
        conflict_strategy: str | None = None,
    ) -> SyncResult:
        """Push local changes to 1C.

        Args:
            catalog_name: 1C catalog name
            local_items: List of local items to push
            conflict_strategy: Override default conflict strategy

        Returns:
            SyncResult with push statistics.
        """
        strategy = conflict_strategy or self.settings.conflict_strategy
        result = SyncResult(catalog_name=catalog_name)

        for item in local_items:
            try:
                key = item.get("key") or item.get("Ref_Key")
                onec_data = map_to_onec(catalog_name, item)

                if key:
                    # Check if exists in 1C
                    existing = await self.client.get_catalog_by_key(catalog_name, key)
                    if existing:
                        # Handle conflict
                        if strategy == "server_wins":
                            # Skip, keep server version
                            result.skipped += 1
                            continue
                        elif strategy == "client_wins":
                            # Force update
                            await self.client.update_catalog_entry(
                                catalog_name, key, onec_data, etag=existing.get("@odata.etag")
                            )
                            result.updated += 1
                        elif strategy == "merge":
                            # Merge logic would go here
                            merged = self._merge_items(existing, onec_data)
                            await self.client.update_catalog_entry(
                                catalog_name, key, merged, etag=existing.get("@odata.etag")
                            )
                            result.updated += 1
                    else:
                        # Create new
                        await self.client.create_catalog_entry(catalog_name, onec_data)
                        result.created += 1
                else:
                    # No key, create new
                    await self.client.create_catalog_entry(catalog_name, onec_data)
                    result.created += 1

            except Exception as e:
                result.errors.append(f"Push failed for item {item.get('key', 'unknown')}: {e}")

        return result

    def _merge_items(self, server_item: dict[str, Any], client_item: dict[str, Any]) -> dict[str, Any]:
        """Merge server and client items (simple strategy: newer wins)."""
        # This is a placeholder for more sophisticated merge logic
        server_modified = server_item.get("ModificationDate") or server_item.get("DataVersion")
        client_modified = client_item.get("ModificationDate") or client_item.get("DataVersion")

        if client_modified and server_modified:
            if client_modified > server_modified:
                return client_item
        return server_item


class LocalStorageSyncEngine(CatalogSyncEngine):
    """Sync engine with JSON file-based local storage.

    Suitable for development and small deployments.
    """

    def __init__(
        self,
        client: OneCClient,
        settings: CatalogSyncSettings,
        state_dir: Path,
        storage_dir: Path,
        progress_callback: Any | None = None,
    ):
        super().__init__(client, settings, state_dir, progress_callback)
        self.storage_dir = storage_dir
        self.storage_dir.mkdir(parents=True, exist_ok=True)

    def _get_catalog_file(self, catalog_name: str) -> Path:
        return self.storage_dir / f"{catalog_name}.json"

    def _load_catalog(self, catalog_name: str) -> dict[str, dict[str, Any]]:
        """Load catalog from local JSON file."""
        file_path = self._get_catalog_file(catalog_name)
        if not file_path.exists():
            return {}
        try:
            data = json.loads(file_path.read_text(encoding="utf-8"))
            return {item["key"]: item for item in data if "key" in item}
        except Exception as e:
            logger.warning("Failed to load catalog %s: %s", catalog_name, e)
            return {}

    def _save_catalog(self, catalog_name: str, items: dict[str, dict[str, Any]]) -> None:
        """Save catalog to local JSON file."""
        file_path = self._get_catalog_file(catalog_name)
        data = list(items.values())
        file_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    async def _local_exists(self, catalog_name: str, key: str) -> bool:
        catalog = self._load_catalog(catalog_name)
        return key in catalog

    async def _upsert_local(
        self,
        catalog_name: str,
        item: dict[str, Any],
        result: SyncResult,
    ) -> None:
        catalog = self._load_catalog(catalog_name)
        key = item.get("key") or item.get("Ref_Key")
        if not key:
            result.errors.append("Item missing key")
            return

        item["key"] = key
        item["_synced_at"] = datetime.now().isoformat()

        if key in catalog:
            result.updated += 1
        else:
            result.created += 1

        catalog[key] = item
        self._save_catalog(catalog_name, catalog)

    async def _mark_deleted_local(
        self,
        catalog_name: str,
        item: dict[str, Any],
        result: SyncResult,
    ) -> None:
        catalog = self._load_catalog(catalog_name)
        key = item.get("key") or item.get("Ref_Key")
        if not key or key not in catalog:
            return

        catalog[key]["deletion_mark"] = True
        catalog[key]["_deleted_at"] = datetime.now().isoformat()
        self._save_catalog(catalog_name, catalog)
        result.deleted += 1

    async def get_local_item(self, catalog_name: str, key: str) -> dict[str, Any] | None:
        """Get a single item from local storage."""
        catalog = self._load_catalog(catalog_name)
        return catalog.get(key)

    async def get_all_local(self, catalog_name: str) -> list[dict[str, Any]]:
        """Get all items from local storage."""
        catalog = self._load_catalog(catalog_name)
        return list(catalog.values())

    async def get_local_changes(
        self,
        catalog_name: str,
        since: datetime | None = None,
    ) -> list[dict[str, Any]]:
        """Get local items modified since timestamp."""
        catalog = self._load_catalog(catalog_name)
        if since is None:
            return list(catalog.values())

        changes = []
        for item in catalog.values():
            synced_at = item.get("_synced_at")
            if synced_at:
                try:
                    item_time = datetime.fromisoformat(synced_at)
                    if item_time > since:
                        changes.append(item)
                except Exception:
                    pass
        return changes


async def create_sync_engine(
    config: OneCConfig,
    state_dir: Path,
    storage_dir: Path | None = None,
    progress_callback: Any | None = None,
) -> CatalogSyncEngine | LocalStorageSyncEngine:
    """Factory function to create appropriate sync engine."""
    if not config.service:
        raise ValueError("1C service not configured")

    client = OneCClient(config.service)

    if storage_dir:
        return LocalStorageSyncEngine(
            client=client,
            settings=config.sync,
            state_dir=state_dir,
            storage_dir=storage_dir,
            progress_callback=progress_callback,
        )
    return CatalogSyncEngine(
        client=client,
        settings=config.sync,
        state_dir=state_dir,
        progress_callback=progress_callback,
    )


__all__ = [
    "CatalogSyncEngine",
    "LocalStorageSyncEngine",
    "create_sync_engine",
]