"""
DB setup helpers — validation, connection testing, and config formatting.

Provides reusable DB config functions for all HuntTech bots:
- Test PostgreSQL connection with given parameters
- Validate port number
- Build database URL from components
- Format config for display (password masked)

Usage::

    from hunttech_bot_common.services.db_setup import (
        test_db_connection, format_db_config, make_db_url, validate_port,
    )

    ok, msg = await test_db_connection(host, port, name, user, password)
    url = make_db_url(host, port, name, user, password)
    display = format_db_config(host, port, name, user)
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

# ── Validation ──────────────────────────────────────────────────


def validate_port(value: str | int) -> str | None:
    """Validate a port number. Returns error message or None."""
    try:
        port = int(value)
        if port < 1 or port > 65535:
            return "Порт должен быть от 1 до 65535"
    except (ValueError, TypeError):
        return "Порт должен быть числом"
    return None


# ── URL building ────────────────────────────────────────────────


def make_db_url(
    host: str,
    port: int,
    name: str,
    user: str,
    password: str,
) -> str:
    """Build a PostgreSQL connection URL from components."""
    import urllib.parse
    escaped_user = urllib.parse.quote(user, safe="")
    escaped_password = urllib.parse.quote(password, safe="")
    return f"postgresql://{escaped_user}:{escaped_password}@{host}:{port}/{name}"


# ── Connection testing ──────────────────────────────────────────


@dataclass
class DbTestResult:
    """Result of a database connection test."""

    success: bool
    message: str


async def test_db_connection(
    host: str,
    port: int,
    name: str,
    user: str,
    password: str,
    timeout: int = 10,
) -> DbTestResult:
    """Test PostgreSQL connection with the given parameters.

    Args:
        host: Database hostname.
        port: Database port.
        name: Database name.
        user: Database user.
        password: Database password.
        timeout: Connection timeout in seconds.

    Returns:
        DbTestResult with success status and message.
    """
    from hunttech_bot_common.database.pool import PoolConfig, DatabasePool

    url = make_db_url(host, port, name, user, password)
    config = PoolConfig.from_url(
        f"{url}?connect_timeout={timeout}&pool_min=1&pool_max=2"
    )

    pool = DatabasePool(config)
    try:
        await pool.connect()
        await pool.close()
        return DbTestResult(
            success=True,
            message=f"✅ *PostgreSQL подключён!*\n`{host}:{port}/{name}`",
        )
    except Exception as e:
        return DbTestResult(
            success=False,
            message=f"❌ *Ошибка подключения:* `{e}`",
        )


# ── Formatting ──────────────────────────────────────────────────


def format_db_config(
    host: str | None = None,
    port: int | None = None,
    name: str | None = None,
    user: str | None = None,
    masked_password: str = "****",
) -> str:
    """Format DB config for display. Password is masked.

    Args:
        host: Database hostname.
        port: Database port.
        name: Database name.
        user: Database user.
        masked_password: Text to show instead of real password.

    Returns:
        Markdown-formatted string.
    """
    if not host:
        return "❌ *База данных не настроена.*\nИспользуйте `/setup db` для настройки."

    lines = [
        "🗄️ *Текущая конфигурация БД:*\n",
        f"• *Хост:* `{host}`",
        f"• *Порт:* `{port or 5432}`",
        f"• *База:* `{name or '—'}`",
        f"• *Пользователь:* `{user or '—'}`",
        f"• *Пароль:* `{masked_password}`",
    ]
    return "\n".join(lines)


# ── Config persistence ──────────────────────────────────────────
# Re-export DbConfigService for convenience

from hunttech_bot_common.services.db_config_service import DbConfigService  # noqa: E402, F811


__all__ = [
    "DbConfigService",
    "DbTestResult",
    "format_db_config",
    "make_db_url",
    "test_db_connection",
    "validate_port",
]
