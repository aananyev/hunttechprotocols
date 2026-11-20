"""Base data structures for the users module."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass
class UserRecord:
    """A user record stored in the access manager's JSON database.

    Represents a known user of a specific bot instance.
    Each bot has its own table of users.
    """

    user_id: int
    username: str | None = None
    full_name: str | None = None
    first_name: str = ""
    last_name: str | None = None
    added_by: int | None = None
    created_at: str = ""  # ISO format
    permissions: list[str] = field(default_factory=list)
    settings: dict[str, Any] = field(default_factory=dict)
    is_banned: bool = False
    banned_at: str | None = None
    language_code: str | None = None

    @property
    def display_name(self) -> str:
        """Best available display name."""
        if self.full_name:
            return self.full_name
        if self.username:
            return f"@{self.username}"
        if self.first_name:
            return f"{self.first_name} {self.last_name or ''}".strip()
        return f"User#{self.user_id}"

    @property
    def mention_html(self) -> str:
        """HTML mention for Telegram messages."""
        name = self.display_name
        return f'<a href="tg://user?id={self.user_id}">{name}</a>'

    def has_permission(self, permission: str) -> bool:
        """Check if user has a specific permission string."""
        return permission in self.permissions


def user_from_telegram(
    user_id: int,
    username: str | None = None,
    first_name: str = "",
    last_name: str | None = None,
    language_code: str | None = None,
) -> UserRecord:
    """Create a UserRecord from Telegram user data.

    Args:
        user_id: Telegram user ID.
        username: Telegram username (without @).
        first_name: User's first name.
        last_name: User's last name.
        language_code: User's language code.

    Returns:
        A UserRecord instance with current timestamp.
    """
    full_name = f"{first_name} {last_name}".strip() if last_name else first_name
    return UserRecord(
        user_id=user_id,
        username=username,
        full_name=full_name or None,
        first_name=first_name,
        last_name=last_name,
        created_at=datetime.now().isoformat(),
        language_code=language_code,
    )


__all__ = [
    "UserRecord",
    "user_from_telegram",
]
