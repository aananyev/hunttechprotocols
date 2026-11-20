"""
Email configuration module for HuntTech Telegram bots.

Provides:
- Load/save/clear email config from JSON file with .env fallback
- Default email config from environment variables
- SMTP + IMAP connection testing
- Formatted config display

Usage::

    from hunttech_bot_common.email import (
        load_email_config, save_email_config, clear_email_config,
        format_email_config, default_email_config, test_email_connections,
    )

    # Load config (falls back to env vars)
    cfg = load_email_config()

    # Test connections
    results = await test_email_connections(cfg)
    for r in results:
        print(r.status, r.message)
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ── Data structures ─────────────────────────────────────────────

DEFAULT_EMAIL_CONFIG_FILE = "email_config.json"


@dataclass
class ConnectionTestResult:
    """Result of a single connection test (SMTP or IMAP)."""

    service: str  # "SMTP" or "IMAP"
    success: bool
    message: str = ""
    host: str = ""
    port: int = 0

    @property
    def emoji(self) -> str:
        return "✅" if self.success else "❌"

    @property
    def short(self) -> str:
        return f"{self.emoji} {self.service}: {self.message}"


# ── Config helpers ──────────────────────────────────────────────


def default_email_config() -> dict[str, Any]:
    """Return email config from environment variables (defaults)."""
    return {
        "sender": os.getenv("MAIL_SENDER", "alan@hunttech.ru"),
        "smtp_host": os.getenv("MAIL_SMTP_HOST", "smtp.yandex.ru"),
        "smtp_port": int(os.getenv("MAIL_SMTP_PORT", "465")),
        "imap_host": os.getenv("MAIL_IMAP_HOST", "imap.yandex.ru"),
        "imap_port": int(os.getenv("MAIL_IMAP_PORT", "993")),
        "password": os.getenv("HUNTTECH_DOCS_YANDEX_MAIL_PASSWORD", ""),
    }


def load_email_config(config_path: str | Path | None = None) -> dict[str, Any]:
    """Load email config from JSON file, fall back to env defaults.

    Args:
        config_path: Path to config JSON file. If None, uses
                     ``{CWD}/email_config.json``.

    Returns:
        Dict with keys: sender, smtp_host, smtp_port, imap_host, imap_port, password.
    """
    if config_path is None:
        config_path = Path.cwd() / DEFAULT_EMAIL_CONFIG_FILE
    path = Path(config_path)

    if path.exists():
        try:
            return json.loads(path.read_text("utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Failed to read email config %s: %s", path, exc)

    return default_email_config()


def save_email_config(
    cfg: dict[str, Any],
    config_path: str | Path | None = None,
) -> None:
    """Save email config to JSON file.

    Args:
        cfg: Dict with email settings.
        config_path: Path to config JSON file.
    """
    if config_path is None:
        config_path = Path.cwd() / DEFAULT_EMAIL_CONFIG_FILE
    path = Path(config_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), "utf-8")


def clear_email_config(config_path: str | Path | None = None) -> None:
    """Delete the local email config file — next load will use env defaults.

    Args:
        config_path: Path to config JSON file.
    """
    if config_path is None:
        config_path = Path.cwd() / DEFAULT_EMAIL_CONFIG_FILE
    path = Path(config_path)
    if path.exists():
        path.unlink()
        logger.info("Email config cleared: %s", path)


def format_email_config(cfg: dict[str, Any]) -> str:
    """Format email config for display (passwords masked).

    Args:
        cfg: Email config dict.

    Returns:
        Markdown-formatted string.
    """
    pw = cfg.get("password", "")
    masked = pw[:4] + "****" if len(pw) > 8 else ("****" if pw else "(не задан)")
    name = cfg.get("sender", "")
    return (
        f"📧 **Текущая конфигурация email:**\n\n"
        f"  • **Отправитель:** `{name}`\n"
        f"  • **SMTP-сервер:** `{cfg.get('smtp_host')}:{cfg.get('smtp_port')}`\n"
        f"  • **IMAP-сервер:** `{cfg.get('imap_host')}:{cfg.get('imap_port')}`\n"
        f"  • **Пароль:** `{masked}`\n"
    )


# ── Connection testing ──────────────────────────────────────────


async def test_smtp_connection(
    sender: str,
    password: str,
    host: str = "smtp.yandex.ru",
    port: int = 465,
    timeout: int = 15,
) -> ConnectionTestResult:
    """Test SMTP connection by sending a test email to self.

    Args:
        sender: Email address (also used as recipient).
        password: SMTP password / app password.
        host: SMTP server hostname.
        port: SMTP server port.
        timeout: Connection timeout in seconds.

    Returns:
        ConnectionTestResult with success status and message.
    """
    if not sender or not password:
        return ConnectionTestResult(
            service="SMTP", success=False,
            message="пропущен (нет данных)",
            host=host, port=port,
        )

    import smtplib
    from email.message import EmailMessage
    from datetime import datetime

    try:
        msg = EmailMessage()
        msg["From"] = sender
        msg["To"] = sender
        msg["Subject"] = "[TEST] HuntTech Bot — проверка связи"
        msg.set_content(
            f"Тестовое письмо от бота.\n"
            f"Отправлено: {datetime.now().isoformat()}\n"
            f"SMTP: {host}:{port}\n"
        )
        with smtplib.SMTP_SSL(host, port, timeout=timeout) as smtp:
            smtp.login(sender, password)
            smtp.send_message(msg)

        return ConnectionTestResult(
            service="SMTP", success=True,
            message="письмо отправлено",
            host=host, port=port,
        )
    except smtplib.SMTPAuthenticationError:
        return ConnectionTestResult(
            service="SMTP", success=False,
            message="ошибка авторизации",
            host=host, port=port,
        )
    except smtplib.SMTPException as exc:
        return ConnectionTestResult(
            service="SMTP", success=False,
            message=f"ошибка SMTP: {exc}",
            host=host, port=port,
        )
    except OSError as exc:
        return ConnectionTestResult(
            service="SMTP", success=False,
            message=f"сетевая ошибка: {exc}",
            host=host, port=port,
        )


async def test_imap_connection(
    login: str,
    password: str,
    host: str = "imap.yandex.ru",
    port: int = 993,
    timeout: int = 15,
) -> ConnectionTestResult:
    """Test IMAP connection by logging in and out.

    Args:
        login: Email address / IMAP login.
        password: IMAP password / app password.
        host: IMAP server hostname.
        port: IMAP server port.
        timeout: Connection timeout in seconds.

    Returns:
        ConnectionTestResult with success status and message.
    """
    if not login or not password:
        return ConnectionTestResult(
            service="IMAP", success=False,
            message="пропущен (нет данных)",
            host=host, port=port,
        )

    import imaplib

    try:
        with imaplib.IMAP4_SSL(host, port, timeout=timeout) as imap:
            imap.login(login, password)
            imap.logout()

        return ConnectionTestResult(
            service="IMAP", success=True,
            message="подключение установлено",
            host=host, port=port,
        )
    except imaplib.IMAP4.error as exc:
        msg = str(exc)
        if "authentication" in msg.lower() or "auth" in msg.lower():
            return ConnectionTestResult(
                service="IMAP", success=False,
                message="ошибка авторизации",
                host=host, port=port,
            )
        return ConnectionTestResult(
            service="IMAP", success=False,
            message=f"ошибка IMAP: {msg[:80]}",
            host=host, port=port,
        )
    except OSError as exc:
        return ConnectionTestResult(
            service="IMAP", success=False,
            message=f"сетевая ошибка: {exc}",
            host=host, port=port,
        )


async def test_email_connections(
    cfg: dict[str, Any],
    timeout: int = 15,
) -> list[ConnectionTestResult]:
    """Test both SMTP and IMAP connections with the given config.

    Args:
        cfg: Email config dict with keys: sender, password,
             smtp_host, smtp_port, imap_host, imap_port.
        timeout: Connection timeout in seconds.

    Returns:
        List of two ConnectionTestResult (SMTP, IMAP).
    """
    sender = cfg.get("sender", "")
    password = cfg.get("password", "")
    smtp_host = cfg.get("smtp_host", "smtp.yandex.ru")
    smtp_port = cfg.get("smtp_port", 465)
    imap_host = cfg.get("imap_host", "imap.yandex.ru")
    imap_port = cfg.get("imap_port", 993)

    smtp_result = await test_smtp_connection(
        sender, password, smtp_host, smtp_port, timeout,
    )
    imap_result = await test_imap_connection(
        sender, password, imap_host, imap_port, timeout,
    )
    return [smtp_result, imap_result]


# ── Validation ──────────────────────────────────────────────────


def validate_email(value: str) -> str | None:
    """Validate an email address. Returns error message or None."""
    if "@" not in value:
        return "Email должен содержать @"
    domain = value.split("@")[-1]
    if "." not in domain:
        return "Email должен содержать домен (user@domain.ru)"
    return None


def validate_hostname(value: str) -> str | None:
    """Validate a hostname. Returns error message or None."""
    if "." not in value or len(value) < 4:
        return "Хост должен содержать домен (например, imap.example.ru)"
    return None


def validate_port(value: str | int) -> str | None:
    """Validate a port number. Returns error message or None."""
    try:
        port = int(value)
        if port < 1 or port > 65535:
            return "Порт должен быть от 1 до 65535"
    except (ValueError, TypeError):
        return "Порт должен быть числом"
    return None


def validate_password(value: str) -> str | None:
    """Validate a password. Returns error message or None."""
    if len(value) < 4:
        return "Пароль должен быть минимум 4 символа"
    return None


__all__ = [
    "ConnectionTestResult",
    "default_email_config",
    "load_email_config",
    "save_email_config",
    "clear_email_config",
    "format_email_config",
    "test_smtp_connection",
    "test_imap_connection",
    "test_email_connections",
    "validate_email",
    "validate_hostname",
    "validate_port",
    "validate_password",
]
