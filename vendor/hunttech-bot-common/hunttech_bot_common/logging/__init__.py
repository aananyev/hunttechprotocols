"""Logging module — structured logging with secrets masking."""

from __future__ import annotations

import json
import logging
import re
import sys
from typing import Any


def mask_secret(value: str, visible_chars: int = 4) -> str:
    """Mask a secret value, showing only the first and last few characters.

    Args:
        value: The secret string to mask.
        visible_chars: Number of characters to show at start and end.

    Returns:
        Masked string like 'sk-...abcd'.
    """
    if not value:
        return ""
    if len(value) <= visible_chars * 2 + 3:
        return value[:visible_chars] + "***"
    return value[:visible_chars] + "..." + value[-visible_chars:]


class SecretsMaskingFilter(logging.Filter):
    """Logging filter that masks sensitive information in log records.

    Masks:
    - API keys (patterns like 'sk-...', 'api-...')
    - Bearer tokens
    - Password fields in JSON/dict representations
    - Bot tokens
    """

    # Patterns to mask in string messages
    _SECRET_PATTERNS: list[tuple[re.Pattern, str]] = [
        (re.compile(r'(sk-[a-zA-Z0-9]{20,})'), r'sk-...\1'[-16:].replace('\\1', '***') if False else 'sk-...***'),
        (re.compile(r'(api[-_][a-zA-Z0-9]{16,})'), r'api-...***'),
        (re.compile(r'(bot[\d]+:[\w-]{20,})', re.IGNORECASE), r'bot...***'),
        (re.compile(r'(Bearer\s+)[a-zA-Z0-9\-_.]{16,}', re.IGNORECASE), r'\1***'),
        (re.compile(r'(password["\']?\s*[:=]\s*["\']?)[^"\'&\s,}]+'), r'\1***'),
        (re.compile(r'(token["\']?\s*[:=]\s*["\']?)[^"\'&\s,}]+'), r'\1***'),
        (re.compile(r'(secret["\']?\s*[:=]\s*["\']?)[^"\'&\s,}]+'), r'\1***'),
        (re.compile(r'(api_key["\']?\s*[:=]\s*["\']?)[^"\'&\s,}]+', re.IGNORECASE), r'\1***'),
    ]

    def filter(self, record: logging.LogRecord) -> bool:
        """Apply masking to the log record's message and args."""
        if hasattr(record, "msg") and isinstance(record.msg, str):
            record.msg = self._mask_text(record.msg)
        if hasattr(record, "args") and record.args:
            masked_args: tuple[Any, ...] = tuple(
                self._mask_text(str(a)) if isinstance(a, str) else a
                for a in record.args
            )
            record.args = masked_args
        return True

    def _mask_text(self, text: str) -> str:
        """Apply all secret patterns to mask text."""
        result = text
        for pattern, _replacement in self._SECRET_PATTERNS:
            result = pattern.sub("***", result)
        return result


def setup_logging(
    level: int | str = logging.INFO,
    json_format: bool = False,
) -> None:
    """Configure logging with optional JSON output and secrets masking.

    Args:
        level: Logging level (default: INFO).
        json_format: If True, output logs as JSON lines instead of plain text.
    """
    if isinstance(level, str):
        level = getattr(logging, level.upper(), logging.INFO)

    root_logger = logging.getLogger()
    root_logger.setLevel(level)

    # Защита от захвата чужого процесса (например, Hermes CLI/gateway, где
    # плагин HuntTech загружается в тот же процесс): если root-логгер уже
    # настроен Hermes'ом (файловые обработчики ~/.hermes/logs/*.log), не
    # сносим их и не вешаем консольный вывод на root — иначе логи агента
    # (run_agent, conversation_loop и т.п.) начнут печататься в терминал.
    has_hermes_file_handlers = any(
        isinstance(h, logging.FileHandler)
        and "hermes" in str(getattr(h, "baseFilename", "")).lower()
        for h in root_logger.handlers
    )
    if has_hermes_file_handlers:
        return

    # Remove existing handlers
    root_logger.handlers.clear()

    handler = logging.StreamHandler(sys.stdout)
    handler.setLevel(level)

    if json_format:
        formatter = _JsonFormatter()
    else:
        formatter = logging.Formatter(
            "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )

    handler.setFormatter(formatter)
    handler.addFilter(SecretsMaskingFilter())
    root_logger.addHandler(handler)


class _JsonFormatter(logging.Formatter):
    """JSON log formatter that outputs structured log records."""

    def format(self, record: logging.LogRecord) -> str:
        log_entry: dict[str, Any] = {
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if hasattr(record, "exc_info") and record.exc_info:
            log_entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(log_entry)


__all__ = [
    "setup_logging",
    "SecretsMaskingFilter",
    "mask_secret",
]
