"""Files module — file handling, validation, and temporary directory utilities."""

from __future__ import annotations

import os
import re
import tempfile
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Generator, Iterator

from hunttech_bot_common.exceptions import FileValidationError


@contextmanager
def temp_directory(suffix: str | None = None) -> Iterator[Path]:
    """Context manager that creates a temporary directory and cleans up on exit.

    Args:
        suffix: Optional suffix for the temp directory name.

    Yields:
        Path to the temporary directory.
    """
    dir_path = Path(tempfile.mkdtemp(suffix=suffix or ""))
    try:
        yield dir_path
    finally:
        # Clean up recursively
        import shutil
        shutil.rmtree(dir_path, ignore_errors=True)


def safe_join(base: str | Path, *paths: str) -> Path:
    """Join path components safely, preventing path traversal.

    Args:
        base: Base directory path.
        *paths: Path components to join.

    Returns:
        Resolved Path.

    Raises:
        FileValidationError: If the resulting path would escape the base directory.
    """
    base_path = Path(base).resolve()
    joined = base_path.joinpath(*paths).resolve()

    if not str(joined).startswith(str(base_path)):
        raise FileValidationError(
            f"Path traversal detected: '{joined}' is outside base '{base_path}'"
        )

    return joined


def validate_extension(filename: str, allowed_extensions: set[str]) -> None:
    """Validate that a filename has an allowed extension.

    Args:
        filename: The filename to check.
        allowed_extensions: Set of allowed extensions (e.g., {'.txt', '.pdf'}).

    Raises:
        FileValidationError: If the extension is not allowed.
    """
    ext = Path(filename).suffix.lower()
    if not ext:
        raise FileValidationError(f"File '{filename}' has no extension")
    if ext not in allowed_extensions:
        raise FileValidationError(
            f"Extension '{ext}' is not allowed. Allowed: {', '.join(sorted(allowed_extensions))}"
        )


def validate_file_size(file_size: int, max_bytes: int) -> None:
    """Validate that a file size does not exceed the maximum.

    Args:
        file_size: File size in bytes.
        max_bytes: Maximum allowed size in bytes.

    Raises:
        FileValidationError: If the file is too large.
    """
    if file_size > max_bytes:
        from hunttech_bot_common.utils import format_file_size
        raise FileValidationError(
            f"File size {format_file_size(file_size)} exceeds maximum of "
            f"{format_file_size(max_bytes)}"
        )


def sanitize_filename(filename: str) -> str:
    """Sanitize a filename by removing or replacing unsafe characters.

    Keeps alphanumeric, dash, underscore, dot, and space characters.
    Limits length to 255 characters.
    """
    # Remove null bytes
    filename = filename.replace("\x00", "")
    # Replace path separators
    filename = filename.replace("/", "_").replace("\\", "_")
    # Remove characters that are not safe
    filename = re.sub(r'[^\w.\- ]', "", filename)
    # Collapse multiple spaces/dots/dashes
    filename = re.sub(r'[ ]+', " ", filename).strip()
    filename = re.sub(r'\.{2,}', ".", filename)
    filename = re.sub(r'\-{2,}', "-", filename)
    # Strip leading dots (from path traversal like "..")
    filename = filename.lstrip(".")
    # Limit length
    if len(filename) > 255:
        name, ext = os.path.splitext(filename)
        name = name[: 255 - len(ext) - 1]
        filename = f"{name}{ext}"
    # Ensure not empty
    if not filename:
        filename = f"untitled_{uuid.uuid4().hex[:8]}"
    return filename


__all__ = [
    "temp_directory",
    "safe_join",
    "validate_extension",
    "validate_file_size",
    "sanitize_filename",
]
