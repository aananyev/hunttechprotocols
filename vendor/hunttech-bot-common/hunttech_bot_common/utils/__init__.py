"""Utils module — general utilities for async retry, chunking, and formatting."""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any, Callable, Iterable, List, TypeVar

T = TypeVar("T")


async def async_retry(
    fn: Callable[..., Any],
    max_attempts: int = 3,
    delay: float = 2.0,
    backoff: float = 2.0,
    *args: Any,
    **kwargs: Any,
) -> Any:
    """Execute an async function with retry logic and exponential backoff.

    Args:
        fn: The async function to call.
        max_attempts: Maximum number of attempts (default: 3).
        delay: Initial delay between retries in seconds (default: 2).
        backoff: Backoff multiplier (default: 2).
        *args: Positional arguments to pass to ``fn``.
        **kwargs: Keyword arguments to pass to ``fn``.

    Returns:
        The result of the successful function call.

    Raises:
        The last exception raised by ``fn`` if all attempts fail.
    """
    last_exception: Exception | None = None
    current_delay = delay

    for attempt in range(1, max_attempts + 1):
        try:
            if asyncio.iscoroutinefunction(fn):
                result = await fn(*args, **kwargs)
            else:
                result = fn(*args, **kwargs)
            return result
        except Exception as exc:
            last_exception = exc
            if attempt < max_attempts:
                await asyncio.sleep(current_delay)
                current_delay *= backoff
            else:
                raise

    # Should not reach here, but type safety
    if last_exception:
        raise last_exception
    raise RuntimeError("Unexpected: retry loop finished without exception")


def chunk_list(items: Iterable[T], chunk_size: int) -> List[List[T]]:
    """Split an iterable into chunks of the specified size.

    Args:
        items: The items to chunk.
        chunk_size: Maximum size of each chunk.

    Returns:
        A list of chunks, each being a list of up to ``chunk_size`` items.
    """
    items_list = list(items)
    return [
        items_list[i : i + chunk_size]
        for i in range(0, len(items_list), chunk_size)
    ]


def format_datetime(dt: datetime, format_str: str = "%Y-%m-%d %H:%M:%S") -> str:
    """Format a datetime object as a string.

    Args:
        dt: The datetime object to format.
        format_str: The strftime format string (default: '%Y-%m-%d %H:%M:%S').

    Returns:
        The formatted datetime string.
    """
    return dt.strftime(format_str)


def format_file_size(size_bytes: int) -> str:
    """Format a file size in bytes to a human-readable string.

    Args:
        size_bytes: Size in bytes.

    Returns:
        Human-readable size string (e.g., '1.5 MB', '234 KB').
    """
    if size_bytes < 1024:
        return f"{size_bytes} B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    elif size_bytes < 1024 * 1024 * 1024:
        return f"{size_bytes / (1024 * 1024):.1f} MB"
    else:
        return f"{size_bytes / (1024 * 1024 * 1024):.1f} GB"


__all__ = [
    "async_retry",
    "chunk_list",
    "format_datetime",
    "format_file_size",
]
