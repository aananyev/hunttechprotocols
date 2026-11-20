"""Security module — URL validation, IP checking, input sanitization."""

from __future__ import annotations

import ipaddress
import re
import socket
from urllib.parse import urlparse

from hunttech_bot_common.exceptions import FileValidationError


def _resolve_host(host: str) -> str | None:
    """Resolve a hostname to an IP address string, or None on failure."""
    try:
        return socket.gethostbyname(host)
    except (socket.gaierror, OSError):
        return None


def is_private_ip(host: str) -> bool:
    """Check if a hostname or IP address resolves to a private/reserved IP.

    Args:
        host: Hostname or IP address string.

    Returns:
        True if the host is private, loopback, or link-local.
    """
    # Check if it's already an IP
    try:
        addr = ipaddress.ip_address(host)
        return addr.is_private or addr.is_loopback or addr.is_link_local
    except ValueError:
        pass

    # Resolve hostname
    ip_str = _resolve_host(host)
    if ip_str is None:
        return False  # Can't resolve, assume public

    try:
        addr = ipaddress.ip_address(ip_str)
        return addr.is_private or addr.is_loopback or addr.is_link_local
    except ValueError:
        return False


def validate_url(url: str, allow_private: bool = False) -> str:
    """Validate and sanitize a URL.

    Checks:
    - URL has a valid scheme (http or https)
    - Host is not a private IP (unless allow_private=True)
    - No credentials in the URL (for security)
    - URL is not excessively long

    Args:
        url: The URL string to validate.
        allow_private: If True, allow private IPs.

    Returns:
        The sanitized URL string.

    Raises:
        FileValidationError: If the URL is invalid or unsafe.
    """
    if not url or len(url) > 8192:
        raise FileValidationError("URL is empty or too long")

    parsed = urlparse(url)

    # Validate scheme
    if parsed.scheme not in ("http", "https"):
        raise FileValidationError(
            f"URL scheme '{parsed.scheme}' is not allowed. Only http and https are permitted."
        )

    # Check for embedded credentials
    if parsed.username or parsed.password:
        raise FileValidationError(
            "URL must not contain embedded credentials"
        )

    # Validate host
    host = parsed.hostname
    if not host:
        raise FileValidationError("URL has no hostname")

    # Check for private IPs
    if not allow_private and is_private_ip(host):
        raise FileValidationError(
            f"URL resolves to a private IP address: {host}"
        )

    # Reconstruct the URL without fragments (for security)
    sanitized = f"{parsed.scheme}://{parsed.hostname}"
    if parsed.port is not None:
        sanitized += f":{parsed.port}"
    sanitized += parsed.path or "/"
    if parsed.query:
        sanitized += f"?{parsed.query}"

    return sanitized


def mask_secret(value: str, visible_chars: int = 4) -> str:
    """Mask a sensitive value for display.

    Shows the first ``visible_chars`` and last ``visible_chars`` characters,
    with '...' in between for longer values.

    Args:
        value: The secret string to mask.
        visible_chars: Number of characters to show at each end.

    Returns:
        The masked string.
    """
    if not value:
        return ""
    if len(value) <= visible_chars * 2 + 3:
        return value[:visible_chars] + "***"
    return value[:visible_chars] + "..." + value[-visible_chars:]


_DANGEROUS_PATTERNS = re.compile(
    r"<script[^>]*>.*?</script>|"
    r"javascript\s*:|"
    r"on\w+\s*=|"
    r"vbscript\s*:|"
    r"data\s*:\s*text/html|"
    r"<[^>]*\s+on\w+\s*=|"
    r"rm\s+-rf\s+/|"
    r"\|.*?\bsh\b|"
    r";.*?\bbash\b",
    re.IGNORECASE | re.DOTALL,
)


def sanitize_text_input(text: str) -> str:
    """Strip dangerous content from user text input.

    Removes or neutralises script tags, event handlers, and command injection
    patterns.

    Args:
        text: The raw user input.

    Returns:
        The sanitized text.
    """
    if not text:
        return text

    # Remove dangerous patterns entirely
    sanitized = _DANGEROUS_PATTERNS.sub("", text)

    # Remove control characters (except newlines and tabs)
    sanitized = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", sanitized)

    # Limit length
    sanitized = sanitized[:10000]

    return sanitized.strip()


__all__ = [
    "validate_url",
    "is_private_ip",
    "mask_secret",
    "sanitize_text_input",
]
