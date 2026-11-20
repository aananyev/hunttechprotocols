"""
Email module — email configuration, connection testing, and validation.

Provides reusable email config management for all HuntTech bots:
- Load/save/clear config from JSON with .env fallback
- SMTP + IMAP connection testing
- Input validation (email, hostname, port, password)
"""

from __future__ import annotations

from hunttech_bot_common.email.config import (
    ConnectionTestResult,
    clear_email_config,
    default_email_config,
    format_email_config,
    load_email_config,
    save_email_config,
    test_email_connections,
    test_imap_connection,
    test_smtp_connection,
    validate_email,
    validate_hostname,
    validate_port,
    validate_password,
)

__all__ = [
    "ConnectionTestResult",
    "clear_email_config",
    "default_email_config",
    "format_email_config",
    "load_email_config",
    "save_email_config",
    "test_email_connections",
    "test_imap_connection",
    "test_smtp_connection",
    "validate_email",
    "validate_hostname",
    "validate_port",
    "validate_password",
]
