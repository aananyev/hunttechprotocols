"""DbConfigService — persistent DB configuration storage.

Stores database connection settings in a JSON file.
Only the master admin can read/write these settings.
Regular users have no access to this data.

Usage::

    service = DbConfigService(data_path="data/db_config.json")
    config = service.load()
    if config:
        pool = DatabasePool(PoolConfig.from_url(config["url"]))
        await pool.connect()

    # Admin sets config
    service.save({
        "url": "postgresql://user:***@host/db",
        "pool_min": 2,
        "pool_max": 10,
        "sslmode": "require",
        "connect_timeout": 10,
    })
    service.delete()  # Clear config
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Default config keys
CONFIG_KEYS = {"url", "pool_min", "pool_max", "sslmode", "connect_timeout"}


class DbConfigService:
    """Persistent DB configuration stored in a JSON file.

    Thread-safe via atomic write (write to temp, then replace).
    """

    def __init__(self, data_path: str | Path = "data/db_config.json") -> None:
        """Initialize the service.

        Args:
            data_path: Path to the JSON config file.
        """
        self._data_path = Path(data_path)

    def load(self) -> dict[str, Any] | None:
        """Load DB configuration from file.

        Returns:
            Dict with keys: url, pool_min, pool_max, sslmode, connect_timeout.
            None if file doesn't exist or is invalid.
        """
        if not self._data_path.exists():
            return None

        try:
            raw = self._data_path.read_text(encoding="utf-8")
            data = json.loads(raw)
            # Validate required keys
            if "url" not in data:
                logger.warning("DB config missing 'url' key")
                return None
            return data
        except (json.JSONDecodeError, OSError) as e:
            logger.error("Failed to load DB config: %s", e)
            return None

    def save(self, config: dict[str, Any]) -> bool:
        """Save DB configuration to file.

        Args:
            config: Dict with at minimum a "url" key.
                   Optional: pool_min, pool_max, sslmode, connect_timeout.

        Returns:
            True if saved successfully.
        """
        if "url" not in config or not config["url"]:
            logger.error("Cannot save DB config: 'url' is required")
            return False

        # Normalise config
        normalized: dict[str, Any] = {
            "url": config["url"],
            "pool_min": int(config.get("pool_min", 2)),
            "pool_max": int(config.get("pool_max", 10)),
            "sslmode": config.get("sslmode", "prefer"),
            "connect_timeout": int(config.get("connect_timeout", 10)),
        }

        self._data_path.parent.mkdir(parents=True, exist_ok=True)

        # Atomic write
        tmp_path = self._data_path.with_suffix(".tmp")
        try:
            tmp_path.write_text(
                json.dumps(normalized, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            tmp_path.replace(self._data_path)
            logger.info("DB config saved to %s", self._data_path)
            return True
        except OSError as e:
            logger.error("Failed to save DB config: %s", e)
            return False

    def delete(self) -> bool:
        """Delete the DB config file.

        Returns:
            True if deleted or file doesn't exist.
        """
        if not self._data_path.exists():
            return True
        try:
            self._data_path.unlink()
            logger.info("DB config deleted")
            return True
        except OSError as e:
            logger.error("Failed to delete DB config: %s", e)
            return False

    def exists(self) -> bool:
        """Check if DB config file exists and has valid data."""
        return self.load() is not None

    def get_database_url(self) -> str | None:
        """Get just the database URL, if configured."""
        config = self.load()
        if config:
            return config.get("url")
        return None

    def to_pool_config(self) -> Any:
        """Convert stored config to a PoolConfig object.

        Returns:
            PoolConfig instance, or None if no config.

        Usage::

            config = service.to_pool_config()
            if config:
                pool = DatabasePool(config)
                await pool.connect()
        """
        from hunttech_bot_common.database.pool import PoolConfig

        data = self.load()
        if not data:
            return None
        return PoolConfig.from_url(
            f"{data['url']}"
            f"?sslmode={data.get('sslmode', 'prefer')}"
            f"&connect_timeout={data.get('connect_timeout', 10)}"
            f"&pool_min={data.get('pool_min', 2)}"
            f"&pool_max={data.get('pool_max', 10)}"
        )

    def format_config_display(self, config: dict[str, Any] | None = None) -> str:
        """Format config for display in Telegram (secrets masked).

        Args:
            config: Config dict. If None, loads from file.

        Returns:
            Formatted string with masked secrets.
        """
        if config is None:
            config = self.load()

        if not config:
            return "❌ *База данных не настроена.*\nИспользуйте `/setup db` для настройки."

        url = config.get("url", "")
        # Mask password in URL
        masked_url = self._mask_db_url(url)

        lines = [
            "🗄️ *Текущая конфигурация БД:*\n",
            f"• `URL`: `{masked_url}`",
            f"• `Min pool`: `{config.get('pool_min', 2)}`",
            f"• `Max pool`: `{config.get('pool_max', 10)}`",
            f"• `SSL mode`: `{config.get('sslmode', 'prefer')}`",
            f"• `Timeout`: `{config.get('connect_timeout', 10)}с`",
        ]
        return "\n".join(lines)

    @staticmethod
    def _mask_db_url(url: str) -> str:
        """Mask password in a database URL for safe display."""
        import re
        masked = re.sub(
            r"(postgresql://[^:]+:)([^@]+)(@.*)",
            lambda m: m.group(1) + "***" + m.group(3),
            url,
        )
        return masked


__all__ = [
    "DbConfigService",
]
