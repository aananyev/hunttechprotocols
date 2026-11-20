"""AccessManager — JSON-backed user access control for Telegram bots.

Provides:
- Per-bot user database (JSON file)
- Allow/deny/list users
- Permission-based command filtering
- Pending access requests
- Admin management
"""

from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from hunttech_bot_common.exceptions import PermissionDeniedError
from hunttech_bot_common.users.base import UserRecord, user_from_telegram
from hunttech_bot_common.telegram import CommandDef

logger = logging.getLogger(__name__)


class AccessManager:
    """JSON-backed user access control.

    Manages which users can access a bot instance and which commands
    each user can use. Each bot has its own AccessManager with its own
    data file.

    Usage::

        am = AccessManager(
            data_path="data/access.json",
            master_admin_id=272980897,
            bot_name="My Bot",
        )

        # Check access
        if am.is_allowed(user_id):
            ...

        # Add user
        am.add_user(user_id=12345, username="ivanov")

        # Filter commands
        allowed_commands = am.filter_commands(commands, user_id)

        # Get allowed users list
        users = am.get_allowed_users()
    """

    def __init__(
        self,
        data_path: str | Path,
        master_admin_id: int,
        bot_name: str = "Bot",
        auto_save: bool = True,
    ) -> None:
        """Initialize AccessManager.

        Args:
            data_path: Path to JSON file for user data.
            master_admin_id: Telegram user ID of the master admin.
            bot_name: Human-readable bot name for notifications.
            auto_save: Whether to save after every mutation.
        """
        self.data_path = Path(data_path)
        self.master_admin_id = master_admin_id
        self.bot_name = bot_name
        self.auto_save = auto_save

        # In-memory state
        self._users: dict[int, dict[str, Any]] = {}  # user_id -> raw dict
        self._command_permissions: dict[str, set[str]] = {}  # command -> required permissions
        self._pending_requests: dict[int, dict[str, Any]] = {}  # user_id -> request info
        self._lock = threading.RLock()

        # Load existing data
        self.reload()

    # ── Persistence ─────────────────────────────────────────────

    def reload(self) -> None:
        """Reload data from the JSON file."""
        with self._lock:
            self._users.clear()
            self._pending_requests.clear()
            self._command_permissions.clear()

            if not self.data_path.exists():
                logger.info("Access data file not found: %s — starting fresh", self.data_path)
                return

            try:
                raw = self.data_path.read_text(encoding="utf-8")
                data = json.loads(raw)
                self._users = {
                    int(uid): rec
                    for uid, rec in data.get("users", {}).items()
                }
                self._pending_requests = {
                    int(uid): req
                    for uid, req in data.get("pending_requests", {}).items()
                }
                cp = data.get("command_permissions", {})
                self._command_permissions = {
                    cmd: set(perms)
                    for cmd, perms in cp.items()
                }
                logger.info("Access data loaded: %d users, %d pending", len(self._users), len(self._pending_requests))
            except (json.JSONDecodeError, OSError) as e:
                logger.error("Failed to load access data: %s", e)

    def save(self) -> None:
        """Save current state to JSON file."""
        with self._lock:
            data_path = self.data_path
            data_path.parent.mkdir(parents=True, exist_ok=True)

            data = {
                "users": {
                    str(uid): rec
                    for uid, rec in self._users.items()
                },
                "pending_requests": {
                    str(uid): req
                    for uid, req in self._pending_requests.items()
                },
                "command_permissions": {
                    cmd: list(perms)
                    for cmd, perms in self._command_permissions.items()
                },
                "meta": {
                    "master_admin_id": self.master_admin_id,
                    "bot_name": self.bot_name,
                    "updated_at": datetime.now().isoformat(),
                },
            }

            # Atomic write via temp file
            tmp_path = data_path.with_suffix(".tmp")
            tmp_path.write_text(
                json.dumps(data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            tmp_path.replace(data_path)

    # ── User management ─────────────────────────────────────────

    def add_user(
        self,
        user_id: int,
        username: str | None = None,
        full_name: str | None = None,
        added_by: int | None = None,
    ) -> bool:
        """Add a user to the allowed list.

        Returns True if user was newly added, False if already existed.
        """
        with self._lock:
            if user_id in self._users:
                # Update existing
                existing = self._users[user_id]
                if username:
                    existing["username"] = username
                if full_name:
                    existing["full_name"] = full_name
                existing["updated_at"] = datetime.now().isoformat()
                if self.auto_save:
                    self.save()
                return False

            rec = {
                "user_id": user_id,
                "username": username,
                "full_name": full_name,
                "added_by": added_by,
                "created_at": datetime.now().isoformat(),
                "permissions": [],
                "settings": {},
                "is_banned": False,
                "banned_at": None,
            }
            self._users[user_id] = rec

            # Remove from pending if was there
            self._pending_requests.pop(user_id, None)

            if self.auto_save:
                self.save()
            return True

    def remove_user(self, user_id: int) -> bool:
        """Remove a user from the allowed list.

        Returns True if user was removed, False if not found.
        """
        with self._lock:
            if user_id not in self._users:
                return False
            del self._users[user_id]
            if self.auto_save:
                self.save()
            return True

    def get_allowed_users(self) -> list[dict[str, Any]]:
        """Get list of all allowed user records."""
        with self._lock:
            return list(self._users.values())

    def get_user(self, user_id: int) -> dict[str, Any] | None:
        """Get a specific user record, or None if not found."""
        with self._lock:
            return self._users.get(user_id)

    def is_allowed(self, user_id: int) -> bool:
        """Check if a user is allowed to use the bot."""
        if user_id == self.master_admin_id:
            return True
        with self._lock:
            user = self._users.get(user_id)
            if user is None:
                return False
            if user.get("is_banned"):
                return False
            return True

    def is_admin(self, user_id: int) -> bool:
        """Check if a user is an admin (master admin only)."""
        return user_id == self.master_admin_id

    def get_admin_ids(self) -> set[int]:
        """Get set of admin user IDs."""
        return {self.master_admin_id}

    def get_user_count(self) -> int:
        """Get total number of allowed users."""
        with self._lock:
            return len([u for u in self._users.values() if not u.get("is_banned")])

    # ── Ban / Unban ─────────────────────────────────────────────

    def ban_user(self, user_id: int) -> bool:
        """Ban a user. Returns True if banned, False if not found."""
        with self._lock:
            user = self._users.get(user_id)
            if user is None:
                return False
            user["is_banned"] = True
            user["banned_at"] = datetime.now().isoformat()
            if self.auto_save:
                self.save()
            return True

    def unban_user(self, user_id: int) -> bool:
        """Unban a user. Returns True if unbanned, False if not found."""
        with self._lock:
            user = self._users.get(user_id)
            if user is None:
                return False
            user["is_banned"] = False
            user["banned_at"] = None
            if self.auto_save:
                self.save()
            return True

    # ── Permission management ───────────────────────────────────

    def set_command_permissions(self, cmd_perms: dict[str, set[str]]) -> None:
        """Set required permissions per command.

        Args:
            cmd_perms: Mapping of command name -> set of required permission strings.
                       Empty set means available to all allowed users.
                       Example: {"start": set(), "admin": {"admin"}, "setup": {"setup"}}
        """
        with self._lock:
            self._command_permissions = {
                cmd: set(perms) if isinstance(perms, (set, list)) else set()
                for cmd, perms in cmd_perms.items()
            }
            if self.auto_save:
                self.save()

    def get_command_permissions(self) -> dict[str, set[str]]:
        """Get current command permissions mapping."""
        with self._lock:
            return dict(self._command_permissions)

    def add_permission(self, user_id: int, permission: str) -> bool:
        """Add a permission string to a user.

        Returns True if added, False if user not found.
        """
        with self._lock:
            user = self._users.get(user_id)
            if user is None:
                return False
            perms = set(user.get("permissions", []))
            if permission not in perms:
                perms.add(permission)
                user["permissions"] = list(perms)
                if self.auto_save:
                    self.save()
            return True

    def remove_permission(self, user_id: int, permission: str) -> bool:
        """Remove a permission string from a user.

        Returns True if removed, False if user or permission not found.
        """
        with self._lock:
            user = self._users.get(user_id)
            if user is None:
                return False
            perms = set(user.get("permissions", []))
            if permission in perms:
                perms.discard(permission)
                user["permissions"] = list(perms)
                if self.auto_save:
                    self.save()
                return True
            return False

    def has_permission(self, user_id: int, permission: str) -> bool:
        """Check if a user has a specific permission.

        Master admin always has all permissions.
        """
        if user_id == self.master_admin_id:
            return True
        with self._lock:
            user = self._users.get(user_id)
            if user is None:
                return False
            return permission in set(user.get("permissions", []))

    def get_user_permissions(self, user_id: int) -> set[str]:
        """Get all permission strings for a user."""
        if user_id == self.master_admin_id:
            return {"admin", "setup", "user_manage"}
        with self._lock:
            user = self._users.get(user_id)
            if user is None:
                return set()
            return set(user.get("permissions", []))

    def set_user_permissions(self, user_id: int, permissions: set[str]) -> bool:
        """Set the full permission set for a user.

        Returns True if set, False if user not found.
        """
        with self._lock:
            user = self._users.get(user_id)
            if user is None:
                return False
            user["permissions"] = list(permissions)
            if self.auto_save:
                self.save()
            return True

    # ── Command filtering ───────────────────────────────────────

    def user_can_use_command(self, user_id: int, command: str) -> bool:
        """Check if a user can use a specific command.

        Master admin can use any command.
        If command has no required permissions, all allowed users can use it.
        """
        if user_id == self.master_admin_id:
            return True

        if not self.is_allowed(user_id):
            return False

        required = self._command_permissions.get(command, set())
        if not required:
            return True  # No permissions required

        user_perms = self.get_user_permissions(user_id)
        return bool(required.intersection(user_perms))

    def filter_commands(
        self,
        commands: list[CommandDef],
        user_id: int,
    ) -> list[CommandDef]:
        """Filter commands based on user's access and permissions.

        Args:
            commands: All registered command definitions.
            user_id: Telegram user ID.

        Returns:
            List of commands the user can see and use.
        """
        if not self.is_allowed(user_id) and user_id != self.master_admin_id:
            return []

        is_admin = self.is_admin(user_id)
        user_perms = self.get_user_permissions(user_id)

        result: list[CommandDef] = []
        for cmd in commands:
            # Hidden commands: only for admin
            if cmd.hidden and not is_admin:
                continue

            # Admin-only commands
            if cmd.admin and not is_admin:
                continue

            # Permission check
            if cmd.permissions:
                if not cmd.permissions.intersection(user_perms):
                    continue

            # Command-level permission from command_permissions map
            if not self.user_can_use_command(user_id, cmd.command):
                continue

            result.append(cmd)

        return result

    # ── Pending access requests ─────────────────────────────────

    def request_access(
        self,
        user_id: int,
        username: str | None = None,
        first_name: str = "",
        last_name: str | None = None,
    ) -> dict[str, Any]:
        """Submit an access request from a user.

        Returns a dict with:
            - "is_new": True if this is a new request
            - "is_already_allowed": True if user is already in the allowed list
            - "user_info": user info dict
        """
        with self._lock:
            # Check if already allowed
            if user_id in self._users:
                user = self._users[user_id]
                return {
                    "is_new": False,
                    "is_already_allowed": True,
                    "user_info": user,
                }

            # Check if already pending
            if user_id in self._pending_requests:
                req = self._pending_requests[user_id]
                # Denied request: allow re-request — create a NEW pending
                # request so the admin gets a fresh notification
                # (otherwise the user sees «Запрос уже отправлен» forever).
                if req.get("status") == "denied":
                    del self._pending_requests[user_id]
                else:
                    return {
                        "is_new": False,
                        "is_already_allowed": False,
                        "user_info": req,
                    }

            # New request
            req = {
                "user_id": user_id,
                "username": username,
                "first_name": first_name,
                "last_name": last_name,
                "full_name": f"{first_name} {last_name}".strip() if last_name else first_name,
                "requested_at": datetime.now().isoformat(),
                "status": "pending",
            }
            self._pending_requests[user_id] = req
            if self.auto_save:
                self.save()
            return {
                "is_new": True,
                "is_already_allowed": False,
                "user_info": req,
            }

    def get_pending_requests(self) -> list[dict[str, Any]]:
        """Get list of all pending access requests."""
        with self._lock:
            return list(self._pending_requests.values())

    def approve_request(self, user_id: int, approved_by: int | None = None) -> bool:
        """Approve a pending access request.

        Returns True if approved, False if request not found.
        """
        with self._lock:
            req = self._pending_requests.get(user_id)
            if req is None:
                return False

            self.add_user(
                user_id=user_id,
                username=req.get("username"),
                full_name=req.get("full_name"),
                added_by=approved_by,
            )
            # add_user already saves with auto_save
            return True

    def deny_request(self, user_id: int) -> bool:
        """Deny a pending access request.

        Returns True if denied, False if request not found.
        """
        with self._lock:
            if user_id not in self._pending_requests:
                return False
            req = self._pending_requests[user_id]
            req["status"] = "denied"
            if self.auto_save:
                self.save()
            return True

    def clear_denied_request(self, user_id: int) -> bool:
        """Remove a denied request from pending (user can re-request)."""
        with self._lock:
            req = self._pending_requests.get(user_id)
            if req is None:
                return False
            if req.get("status") == "denied":
                del self._pending_requests[user_id]
                if self.auto_save:
                    self.save()
                return True
            return False

    def get_request_status(self, user_id: int) -> str | None:
        """Get the status of a user's access request.

        Returns: "pending", "denied", or None if no request.
        """
        with self._lock:
            req = self._pending_requests.get(user_id)
            if req is None:
                return None
            return req.get("status", "pending")

    # ── User settings (from settings manager interface) ─────────

    def get_user_settings(self, user_id: int) -> dict[str, Any]:
        """Get a user's personal settings dict."""
        with self._lock:
            user = self._users.get(user_id)
            if user is None:
                return {}
            return dict(user.get("settings", {}))

    def update_user_settings(self, user_id: int, settings: dict[str, Any]) -> bool:
        """Update a user's personal settings.

        Returns True if updated, False if user not found.
        """
        with self._lock:
            user = self._users.get(user_id)
            if user is None:
                return False
            current = dict(user.get("settings", {}))
            current.update(settings)
            user["settings"] = current
            if self.auto_save:
                self.save()
            return True

    def reset_user_settings(self, user_id: int, settings_keys: set[str] | None = None) -> bool:
        """Reset a user's personal settings.

        Args:
            user_id: User ID.
            settings_keys: If set, only reset these keys. If None, reset all.

        Returns True if reset, False if user not found.
        """
        with self._lock:
            user = self._users.get(user_id)
            if user is None:
                return False
            if settings_keys is not None:
                current = dict(user.get("settings", {}))
                for key in settings_keys:
                    current.pop(key, None)
                user["settings"] = current
            else:
                user["settings"] = {}
            if self.auto_save:
                self.save()
            return True

    # ── Utility methods ─────────────────────────────────────────

    def get_mention_html(self, user_id: int) -> str:
        """Get HTML mention for a user, or fallback text."""
        user = self.get_user(user_id)
        if user:
            name = user.get("full_name") or user.get("username") or f"User#{user_id}"
        else:
            name = f"User#{user_id}"
        return f'<a href="tg://user?id={user_id}">{name}</a>'

    def is_banned(self, user_id: int) -> bool:
        """Check if a user is banned."""
        with self._lock:
            user = self._users.get(user_id)
            if user is None:
                return False
            return user.get("is_banned", False)


__all__ = [
    "AccessManager",
]
