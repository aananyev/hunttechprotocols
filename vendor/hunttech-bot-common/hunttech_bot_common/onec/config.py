"""1C integration configuration for HuntTech bots.

Provides settings management for connecting to 1C via OData HTTP services.
Supports loading from environment variables and .env files.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from hunttech_bot_common.exceptions import ConfigurationError


def _get_env(key: str, default: str | None = None) -> str:
    """Get an environment variable or raise ConfigurationError."""
    value = os.environ.get(key)
    if value is not None:
        return value
    if default is not None:
        return default
    raise ConfigurationError(f"Required environment variable '{key}' is not set")


def _get_env_int(key: str, default: int | None = None) -> int:
    """Get an integer environment variable."""
    value = _get_env(key)
    try:
        return int(value)
    except (ValueError, TypeError) as exc:
        raise ConfigurationError(
            f"Environment variable '{key}' must be an integer, got '{value}'"
        ) from exc


def _get_env_bool(key: str, default: bool = False) -> bool:
    """Get a boolean environment variable."""
    value = os.environ.get(key)
    if value is None:
        return default
    return value.lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class OneCSettings:
    """1C OData service connection settings."""

    base_url: str
    username: str
    password: str
    timeout: int = 30
    verify_ssl: bool = True
    max_retries: int = 3
    retry_backoff: float = 1.0

    @classmethod
    def from_env(cls, prefix: str = "ONEC_") -> OneCSettings | None:
        """Create from environment variables with optional prefix.

        Required variables:
            {prefix}BASE_URL - 1C OData service base URL
            {prefix}USERNAME - 1C service username
            {prefix}PASSWORD - 1C service password

        Optional variables:
            {prefix}TIMEOUT - Request timeout in seconds (default: 30)
            {prefix}VERIFY_SSL - Verify SSL certificates (default: true)
            {prefix}MAX_RETRIES - Max retry attempts (default: 3)
            {prefix}RETRY_BACKOFF - Retry backoff factor (default: 1.0)

        Returns:
            OneCSettings instance or None if BASE_URL is not set.
        """
        base_url = os.environ.get(f"{prefix}BASE_URL")
        if not base_url:
            return None

        username = _get_env(f"{prefix}USERNAME")
        password = _get_env(f"{prefix}PASSWORD")
        timeout = int(os.environ.get(f"{prefix}TIMEOUT", "30"))
        verify_ssl = _get_env_bool(f"{prefix}VERIFY_SSL", True)
        max_retries = int(os.environ.get(f"{prefix}MAX_RETRIES", "3"))
        retry_backoff = float(os.environ.get(f"{prefix}RETRY_BACKOFF", "1.0"))

        return cls(
            base_url=base_url.rstrip("/"),
            username=username,
            password=password,
            timeout=timeout,
            verify_ssl=verify_ssl,
            max_retries=max_retries,
            retry_backoff=retry_backoff,
        )

    def __repr__(self) -> str:
        masked_url = self.base_url
        if "@" in masked_url:
            parts = masked_url.split("@")
            if "//" in parts[0]:
                proto, creds = parts[0].split("//", 1)
                if ":" in creds:
                    user, _ = creds.split(":", 1)
                    masked_url = f"{proto}//{user}:***@{parts[1]}"
        return (
            f"OneCSettings(base_url={masked_url!r}, "
            f"username={self.username!r}, "
            f"timeout={self.timeout}, "
            f"verify_ssl={self.verify_ssl})"
        )


@dataclass(frozen=True)
class CatalogSyncSettings:
    """Settings for catalog synchronization operations."""

    batch_size: int = 100
    full_sync_interval_hours: int = 24
    incremental_sync_interval_minutes: int = 15
    conflict_strategy: str = "server_wins"  # "server_wins", "client_wins", "merge"
    enabled_catalogs: list[str] = field(default_factory=list)  # Empty = all

    @classmethod
    def from_env(cls, prefix: str = "ONEC_SYNC_") -> CatalogSyncSettings:
        """Create from environment variables with optional prefix.

        Optional variables:
            {prefix}BATCH_SIZE - Items per batch (default: 100)
            {prefix}FULL_SYNC_INTERVAL_HOURS - Full sync interval (default: 24)
            {prefix}INCREMENTAL_SYNC_INTERVAL_MINUTES - Incremental sync interval (default: 15)
            {prefix}CONFLICT_STRATEGY - Conflict resolution (default: server_wins)
            {prefix}ENABLED_CATALOGS - Comma-separated list of catalog names (default: all)
        """
        batch_size = int(os.environ.get(f"{prefix}BATCH_SIZE", "100"))
        full_sync_interval_hours = int(os.environ.get(f"{prefix}FULL_SYNC_INTERVAL_HOURS", "24"))
        incremental_sync_interval_minutes = int(
            os.environ.get(f"{prefix}INCREMENTAL_SYNC_INTERVAL_MINUTES", "15")
        )
        conflict_strategy = os.environ.get(f"{prefix}CONFLICT_STRATEGY", "server_wins")
        enabled_catalogs_raw = os.environ.get(f"{prefix}ENABLED_CATALOGS", "")
        enabled_catalogs = [
            c.strip() for c in enabled_catalogs_raw.split(",") if c.strip()
        ]

        return cls(
            batch_size=batch_size,
            full_sync_interval_hours=full_sync_interval_hours,
            incremental_sync_interval_minutes=incremental_sync_interval_minutes,
            conflict_strategy=conflict_strategy,
            enabled_catalogs=enabled_catalogs,
        )


@dataclass(frozen=True)
class DocumentBuilderSettings:
    """Settings for 1C document building operations."""

    default_currency: str = "RUB"
    default_vat_rate: float = 0.20
    auto_post_documents: bool = False
    require_counterparty_inn: bool = True
    default_warehouse_ref: str | None = None
    default_organization_ref: str | None = None

    @classmethod
    def from_env(cls, prefix: str = "ONEC_DOC_") -> DocumentBuilderSettings:
        """Create from environment variables with optional prefix."""
        default_currency = os.environ.get(f"{prefix}DEFAULT_CURRENCY", "RUB")
        default_vat_rate = float(os.environ.get(f"{prefix}DEFAULT_VAT_RATE", "0.20"))
        auto_post_documents = _get_env_bool(f"{prefix}AUTO_POST", False)
        require_counterparty_inn = _get_env_bool(f"{prefix}REQUIRE_INN", True)
        default_warehouse_ref = os.environ.get(f"{prefix}DEFAULT_WAREHOUSE_REF")
        default_organization_ref = os.environ.get(f"{prefix}DEFAULT_ORG_REF")

        return cls(
            default_currency=default_currency,
            default_vat_rate=default_vat_rate,
            auto_post_documents=auto_post_documents,
            require_counterparty_inn=require_counterparty_inn,
            default_warehouse_ref=default_warehouse_ref,
            default_organization_ref=default_organization_ref,
        )


@dataclass(frozen=True)
class OneCConfig:
    """Complete 1C integration configuration."""

    service: OneCSettings | None = None
    sync: CatalogSyncSettings = field(default_factory=CatalogSyncSettings)
    documents: DocumentBuilderSettings = field(default_factory=DocumentBuilderSettings)

    @classmethod
    def from_env(cls, env_file: str | Path | None = None) -> OneCConfig:
        """Load complete configuration from environment variables.

        Args:
            env_file: Optional path to a .env file to load.

        Returns:
            OneCConfig instance with all settings populated.
        """
        if env_file:
            load_dotenv(env_file)
        else:
            load_dotenv()

        service = OneCSettings.from_env()
        sync = CatalogSyncSettings.from_env()
        documents = DocumentBuilderSettings.from_env()

        return cls(service=service, sync=sync, documents=documents)

    def is_configured(self) -> bool:
        """Check if 1C service is configured."""
        return self.service is not None

    def __repr__(self) -> str:
        return (
            f"OneCConfig(service={self.service!r}, "
            f"sync={self.sync!r}, documents={self.documents!r})"
        )


__all__ = [
    "OneCSettings",
    "CatalogSyncSettings",
    "DocumentBuilderSettings",
    "OneCConfig",
]