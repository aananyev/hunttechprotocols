"""UserSettingsManager — manages common and per-user settings.

Supports two types of settings:
1. **Common settings** — defined by the developer, copied to each user on first access.
   The user cannot change common settings via /setup.
2. **Individual settings** — the user can manage these via /setup.
   The developer defines which keys are individual.

Usage::

    settings_mgr = UserSettingsManager(
        common_settings={
            "ai_endpoint": "https://api.deepseek.com/v1",
            "ai_model": "deepseek-chat",
            "max_tokens": 4096,
        },
        individual_keys={"theme", "language", "notifications"},
    )

    # On user first access, copy common settings:
    settings_mgr.apply_common_to_user(user_id, access_manager)

    # /setup handler: get individual settings
    user_settings = settings_mgr.get_individual(user_id, access_manager)

    # /setup handler: update individual settings
    settings_mgr.update_individual(user_id, {"theme": "dark"}, access_manager)

    # /setup show: get all settings (common + individual merged)
    all_settings = settings_mgr.get_all(user_id, access_manager)
"""

from __future__ import annotations

from typing import Any


class UserSettingsManager:
    """Manages common (developer-defined) and individual (user-controlled) settings.

    Common settings are copied to each user on first access.
    Individual settings are managed by the user via /setup.
    """

    def __init__(
        self,
        common_settings: dict[str, Any] | None = None,
        individual_keys: set[str] | None = None,
    ) -> None:
        """Initialize the settings manager.

        Args:
            common_settings: Developer-defined default settings.
                             Copied to each user on first access.
            individual_keys: Set of setting keys the user can manage via /setup.
        """
        self._common = dict(common_settings or {})
        self._individual_keys = set(individual_keys or set())

    @property
    def common(self) -> dict[str, Any]:
        """Get read-only view of common settings."""
        return dict(self._common)

    @common.setter
    def common(self, value: dict[str, Any]) -> None:
        """Set common settings."""
        self._common = dict(value)

    @property
    def individual_keys(self) -> set[str]:
        """Get the set of keys that are user-manageable."""
        return set(self._individual_keys)

    @individual_keys.setter
    def individual_keys(self, value: set[str]) -> None:
        """Set which keys are user-manageable."""
        self._individual_keys = set(value)

    def apply_common_to_user(self, user_id: int, access_manager: Any) -> bool:
        """Apply common settings to a user on first access.

        Only sets keys that are not already present in the user's settings.
        Common settings are stored in user.settings (not individually editable).

        Args:
            user_id: Telegram user ID.
            access_manager: AccessManager instance.

        Returns:
            True if settings were applied.
        """
        user_settings = access_manager.get_user_settings(user_id)
        changed = False
        for key, value in self._common.items():
            if key not in user_settings:
                user_settings[key] = value
                changed = True
        if changed:
            access_manager.update_user_settings(user_id, user_settings)
        return changed

    def get_individual(self, user_id: int, access_manager: Any) -> dict[str, Any]:
        """Get only the individual (user-manageable) settings.

        Args:
            user_id: Telegram user ID.
            access_manager: AccessManager instance.

        Returns:
            Dict of individual setting key -> value.
        """
        all_settings = access_manager.get_user_settings(user_id)
        return {
            k: v
            for k, v in all_settings.items()
            if k in self._individual_keys
        }

    def get_common_for_user(self, user_id: int, access_manager: Any) -> dict[str, Any]:
        """Get only the common (developer-defined) settings visible to user.

        Common settings are shown but not editable via /setup.

        Args:
            user_id: Telegram user ID.
            access_manager: AccessManager instance.

        Returns:
            Dict of common setting key -> value.
        """
        all_settings = access_manager.get_user_settings(user_id)
        return {
            k: v
            for k, v in all_settings.items()
            if k in self._common and k not in self._individual_keys
        }

    def get_all(self, user_id: int, access_manager: Any) -> dict[str, Any]:
        """Get all settings for a user (common + individual merged).

        Args:
            user_id: Telegram user ID.
            access_manager: AccessManager instance.

        Returns:
            Dict of all setting key -> value.
        """
        return dict(access_manager.get_user_settings(user_id))

    def update_individual(
        self,
        user_id: int,
        settings: dict[str, Any],
        access_manager: Any,
    ) -> dict[str, Any]:
        """Update individual user-manageable settings.

        Only keys in ``individual_keys`` are accepted.
        Other keys in ``settings`` are silently ignored.

        Args:
            user_id: Telegram user ID.
            settings: Dict of setting key -> value to update.
            access_manager: AccessManager instance.

        Returns:
            Dict of actually updated key -> value.
        """
        filtered = {
            k: v
            for k, v in settings.items()
            if k in self._individual_keys
        }
        if filtered:
            access_manager.update_user_settings(user_id, filtered)
        return filtered

    def reset_individual(self, user_id: int, access_manager: Any) -> bool:
        """Reset individual settings for a user (remove all individual keys).

        Args:
            user_id: Telegram user ID.
            access_manager: AccessManager instance.

        Returns:
            True if reset was performed.
        """
        return access_manager.reset_user_settings(user_id, self._individual_keys)

    def is_individual_key(self, key: str) -> bool:
        """Check if a setting key is user-manageable."""
        return key in self._individual_keys

    def get_common_keys(self) -> set[str]:
        """Get the set of common setting keys."""
        return set(self._common.keys())

    def get_settings_help_text(self, user_id: int, access_manager: Any) -> str:
        """Generate help text showing common and individual settings.

        Args:
            user_id: Telegram user ID.
            access_manager: AccessManager instance.

        Returns:
            Formatted help text.
        """
        all_settings = self.get_all(user_id, access_manager)
        common = self.get_common_for_user(user_id, access_manager)
        individual = self.get_individual(user_id, access_manager)

        lines: list[str] = ["*Settings:*\n"]

        if common:
            lines.append("*Common settings (read-only):*")
            for key, value in common.items():
                masked = self._mask_value(key, value)
                lines.append(f"  • `{key}`: `{masked}`")
            lines.append("")

        if individual:
            lines.append("*Individual settings (editable via /setup):*")
            for key, value in individual.items():
                masked = self._mask_value(key, value)
                lines.append(f"  • `{key}`: `{masked}`")
            lines.append("")

        if not all_settings:
            lines.append("No settings configured yet.")

        return "\n".join(lines)

    @staticmethod
    def _mask_value(key: str, value: Any) -> str:
        """Mask sensitive values in display."""
        sensitive_keys = {"api_key", "password", "token", "secret"}
        if any(s in key.lower() for s in sensitive_keys):
            if isinstance(value, str) and len(value) > 8:
                return value[:4] + "****"
            if value:
                return "****"
        return str(value) if value is not None else "(not set)"


__all__ = [
    "UserSettingsManager",
]
