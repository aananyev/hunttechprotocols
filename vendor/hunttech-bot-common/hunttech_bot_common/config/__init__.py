"""Configuration module — environment-based settings management."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any

from dotenv import load_dotenv

from hunttech_bot_common.exceptions import ConfigurationError


def _mask_value(value: str | None, visible: int = 4) -> str:
    """Mask a secret value for display."""
    if value is None:
        return "<not set>"
    if len(value) <= visible + 2:
        return value[:1] + "***"
    return value[:visible] + "..." + value[-visible:] if len(value) > visible * 2 else value[:visible] + "***"


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


@dataclass
class TelegramSettings:
    """Telegram bot configuration."""

    bot_token: str
    admin_ids: list[int] = field(default_factory=list)

    @classmethod
    def from_env(cls) -> TelegramSettings:
        """Create from environment variables."""
        token = _get_env("BOT_TOKEN")
        admin_raw = os.environ.get("ADMIN_IDS", "")
        admin_ids: list[int] = []
        if admin_raw:
            for part in admin_raw.split(","):
                part = part.strip()
                if part:
                    try:
                        admin_ids.append(int(part))
                    except ValueError:
                        pass
        return cls(bot_token=token, admin_ids=admin_ids)

    def __repr__(self) -> str:
        return (
            f"TelegramSettings(bot_token={_mask_value(self.bot_token)}, "
            f"admin_ids={self.admin_ids})"
        )


@dataclass
class AISettings:
    """AI provider configuration."""

    endpoint: str
    api_key: str
    model: str
    provider: str = "openai"
    default_timeout: int = 120

    @classmethod
    def from_env(cls) -> AISettings:
        """Create from environment variables."""
        endpoint = _get_env("AI_ENDPOINT", "https://api.openai.com/v1/chat/completions")
        api_key = _get_env("AI_API_KEY", "")
        model = _get_env("AI_MODEL", "gpt-4o-mini")
        provider = os.environ.get("AI_PROVIDER", "openai")
        timeout = int(os.environ.get("AI_TIMEOUT", "120"))
        return cls(
            endpoint=endpoint,
            api_key=api_key,
            model=model,
            provider=provider,
            default_timeout=timeout,
        )

    def __repr__(self) -> str:
        return (
            f"AISettings(endpoint={self.endpoint!r}, "
            f"api_key={_mask_value(self.api_key)}, "
            f"model={self.model!r}, "
            f"provider={self.provider!r})"
        )


@dataclass
class DatabaseSettings:
    """Database configuration."""

    url: str
    pool_min: int = 2
    pool_max: int = 10

    @classmethod
    def from_env(cls) -> DatabaseSettings | None:
        """Create from environment variables. Returns None if DATABASE_URL is not set."""
        url = os.environ.get("DATABASE_URL")
        if not url:
            return None
        pool_min = int(os.environ.get("DATABASE_POOL_MIN", "2"))
        pool_max = int(os.environ.get("DATABASE_POOL_MAX", "10"))
        return cls(url=url, pool_min=pool_min, pool_max=pool_max)

    def __repr__(self) -> str:
        masked = _mask_value(self.url)
        return (
            f"DatabaseSettings(url={masked}, "
            f"pool_min={self.pool_min}, pool_max={self.pool_max})"
        )


@dataclass
class AppSettings:
    """Top-level application settings."""

    telegram: TelegramSettings
    ai: AISettings
    database: DatabaseSettings | None = None
    log_level: str = "INFO"
    log_json_format: bool = False

    @classmethod
    def from_env(cls, env_file: str | None = None) -> AppSettings:
        """Load settings from environment variables.

        Args:
            env_file: Optional path to a .env file to load.

        Returns:
            An AppSettings instance.
        """
        if env_file:
            load_dotenv(env_file)
        else:
            load_dotenv()

        telegram = TelegramSettings.from_env()
        ai = AISettings.from_env()
        database = DatabaseSettings.from_env()
        log_level = os.environ.get("LOG_LEVEL", "INFO")
        log_json = os.environ.get("LOG_JSON_FORMAT", "false").lower() in (
            "1", "true", "yes", "on"
        )

        return cls(
            telegram=telegram,
            ai=ai,
            database=database,
            log_level=log_level,
            log_json_format=log_json,
        )

    def __repr__(self) -> str:
        return (
            f"AppSettings(\n"
            f"  telegram={self.telegram!r},\n"
            f"  ai={self.ai!r},\n"
            f"  database={self.database!r},\n"
            f"  log_level={self.log_level!r},\n"
            f"  log_json_format={self.log_json_format!r}\n"
            f")"
        )


__all__ = [
    "AppSettings",
    "TelegramSettings",
    "AISettings",
    "DatabaseSettings",
]
