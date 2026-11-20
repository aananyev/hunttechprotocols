"""Database migrations — SQL file-based versioned migrations.

Each migration is a numbered SQL file::

    migrations/
    ├── 001_create_users.sql
    ├── 002_create_settings.sql
    └── 003_add_permissions.sql

The migrator tracks applied migrations in a ``_migrations`` table.

Usage::

    migrator = DatabaseMigrator(pool, migrations_dir="migrations/")
    await migrator.run()  # Applies all pending migrations
    status = await migrator.status()  # Check which are applied
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any

from hunttech_bot_common.database.pool import DatabasePool
from hunttech_bot_common.exceptions import DatabaseError

logger = logging.getLogger(__name__)

# Default migration tracking table
MIGRATIONS_TABLE = "_migrations"


class DatabaseMigrator:
    """SQL file-based migration runner.

    Applies SQL migration files in order.
    Each file is applied in a separate transaction.
    Tracks applied migrations in a ``_migrations`` table.
    """

    def __init__(
        self,
        pool: DatabasePool,
        migrations_dir: str | Path = "migrations/",
        table_name: str = MIGRATIONS_TABLE,
    ) -> None:
        """Initialize the migrator.

        Args:
            pool: DatabasePool instance.
            migrations_dir: Path to directory containing SQL migration files.
            table_name: Name of the migration tracking table.
        """
        self._pool = pool
        self._migrations_dir = Path(migrations_dir)
        self._table_name = table_name

    @property
    def migrations_dir(self) -> Path:
        """Get the migrations directory path."""
        return self._migrations_dir

    async def ensure_table(self) -> None:
        """Create the migration tracking table if it doesn't exist."""
        await self._pool.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {self._table_name} (
                version INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                checksum TEXT NOT NULL DEFAULT '',
                duration_ms INTEGER NOT NULL DEFAULT 0
            )
            """
        )

    def _discover_migrations(self) -> list[dict[str, Any]]:
        """Discover migration files in the migrations directory.

        Files are expected to be named like ``001_description.sql``.

        Returns:
            Sorted list of migration info dicts with keys:
                version, name, path, content.
        """
        if not self._migrations_dir.exists():
            logger.warning(
                "Migrations directory not found: %s", self._migrations_dir
            )
            return []

        files = sorted(self._migrations_dir.glob("*.sql"))
        migrations: list[dict[str, Any]] = []

        for fpath in files:
            match = re.match(r"^(\d+)[_-]", fpath.stem)
            if not match:
                logger.warning(
                    "Skipping file with invalid migration name: %s "
                    "(expected format: 001_description.sql)",
                    fpath.name,
                )
                continue

            version = int(match.group(1))
            content = fpath.read_text(encoding="utf-8")

            migrations.append({
                "version": version,
                "name": fpath.name,
                "path": fpath,
                "content": content,
            })

        return migrations

    async def get_applied_versions(self) -> set[int]:
        """Get set of already applied migration versions."""
        try:
            rows = await self._pool.fetch(
                f"SELECT version FROM {self._table_name} ORDER BY version"
            )
            return {row["version"] for row in rows}
        except Exception:
            # Table may not exist yet
            return set()

    async def get_pending_migrations(self) -> list[dict[str, Any]]:
        """Get list of migrations that haven't been applied yet."""
        await self.ensure_table()
        all_migrations = self._discover_migrations()
        applied = await self.get_applied_versions()

        return [
            m for m in all_migrations if m["version"] not in applied
        ]

    async def run(
        self,
        up_to_version: int | None = None,
        fake: bool = False,
    ) -> list[dict[str, Any]]:
        """Apply all pending migrations.

        Args:
            up_to_version: Only apply migrations up to this version (inclusive).
            fake: Mark migrations as applied without running them.

        Returns:
            List of applied migration info dicts.

        Raises:
            DatabaseError: If a migration fails.
        """
        pending = await self.get_pending_migrations()

        if up_to_version is not None:
            pending = [m for m in pending if m["version"] <= up_to_version]

        if not pending:
            logger.info("No pending migrations to apply")
            return []

        import time

        applied: list[dict[str, Any]] = []

        for migration in pending:
            version = migration["version"]
            name = migration["name"]

            if fake:
                logger.info("Fake-applied migration: %s", name)
                await self._mark_applied(migration, checksum="fake", duration_ms=0)
                applied.append(migration)
                continue

            logger.info("Applying migration: %s ...", name)

            try:
                start = time.monotonic()

                # Execute migration in a transaction
                async with self._pool.pool.acquire() as conn:  # type: ignore
                    async with conn.transaction():
                        await conn.execute(migration["content"])

                duration_ms = int((time.monotonic() - start) * 1000)

                # Record the migration
                import hashlib

                checksum = hashlib.md5(
                    migration["content"].encode("utf-8")
                ).hexdigest()

                await self._mark_applied(migration, checksum, duration_ms)

                applied.append(migration)
                logger.info(
                    "Migration %s applied in %dms", name, duration_ms
                )

            except Exception as e:
                raise DatabaseError(
                    f"Migration {name} failed: {e}"
                ) from e

        return applied

    async def _mark_applied(
        self,
        migration: dict[str, Any],
        checksum: str = "",
        duration_ms: int = 0,
    ) -> None:
        """Mark a migration as applied in the tracking table."""
        await self._pool.execute(
            f"""
            INSERT INTO {self._table_name} (version, name, checksum, duration_ms)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (version) DO UPDATE
            SET name = EXCLUDED.name,
                checksum = EXCLUDED.checksum,
                duration_ms = EXCLUDED.duration_ms,
                applied_at = NOW()
            """,
            migration["version"],
            migration["name"],
            checksum,
            duration_ms,
        )

    async def rollback_one(self, version: int) -> bool:
        """Rollback a specific migration (if rollback SQL exists).

        Looks for a file named ``{version:03d}_rollback_*.sql``.

        Args:
            version: Migration version to rollback.

        Returns:
            True if rolled back, False if no rollback file.
        """
        # Search for rollback file
        rollback_files = sorted(self._migrations_dir.glob("*rollback*"))
        target = None
        for rf in rollback_files:
            match = re.match(r"^(\d+)_rollback_", rf.stem)
            if match and int(match.group(1)) == version:
                target = rf
                break

        if target is None:
            logger.warning(
                "No rollback file found for migration %d", version
            )
            return False

        content = target.read_text(encoding="utf-8")

        try:
            await self._pool.execute(content)
            await self._pool.execute(
                f"DELETE FROM {self._table_name} WHERE version = $1",
                version,
            )
            logger.info("Rolled back migration %d", version)
            return True
        except Exception as e:
            raise DatabaseError(
                f"Failed to rollback migration {version}: {e}"
            ) from e

    async def status(self) -> list[dict[str, Any]]:
        """Get full migration status (all discovered + applied info).

        Returns:
            List of dicts with keys: version, name, applied, applied_at, checksum.
        """
        all_migrations = self._discover_migrations()
        if not all_migrations:
            return []

        try:
            applied_rows = await self._pool.fetch(
                f"SELECT version, name, applied_at, checksum "
                f"FROM {self._table_name} ORDER BY version"
            )
            applied_map = {r["version"]: r for r in applied_rows}
        except Exception:
            applied_map = {}

        result: list[dict[str, Any]] = []
        for m in all_migrations:
            applied = m["version"] in applied_map
            info: dict[str, Any] = {
                "version": m["version"],
                "name": m["name"],
                "applied": applied,
            }
            if applied:
                record = applied_map[m["version"]]
                info["applied_at"] = str(record["applied_at"])
                info["checksum"] = record["checksum"]
            else:
                info["applied_at"] = None
                info["checksum"] = None
            result.append(info)

        return result

    async def reset(self, confirm: bool = False) -> bool:
        """Drop all applied migrations and re-run from scratch.

        WARNING: This drops and recreates the migrations table.
        It does NOT drop application tables.

        Args:
            confirm: Must be True to proceed.

        Returns:
            True if reset was performed.
        """
        if not confirm:
            logger.warning("Reset requires confirm=True")
            return False

        await self._pool.execute(f"DROP TABLE IF EXISTS {self._table_name}")
        await self.run()
        logger.info("Migrations reset complete")
        return True


__all__ = [
    "DatabaseMigrator",
]
