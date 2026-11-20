"""Users module — user management for HuntTech Telegram bots.

Provides data structures and managers for:
- User records and permissions
- Access control (allow/deny per bot)
- Per-user settings (common + individual)
- Admin notification and invitation flow
- Permission-based command filtering
- aiogram middleware for access control
- Standard Telegram UI handlers
"""

from __future__ import annotations

from hunttech_bot_common.users.base import UserRecord, user_from_telegram
from hunttech_bot_common.users.access import AccessManager
from hunttech_bot_common.users.settings import UserSettingsManager
from hunttech_bot_common.users.middleware import (
    AccessControlMiddleware,
    CallbackAccessMiddleware,
)
from hunttech_bot_common.users.telegram import (
    sync_user_menu,
    start_access_gate,
    request_access_handler,
    admin_approval_callback,
    user_list_handler,
    user_delete_callback,
    access_callback_handler,
    get_standard_user_commands,
    get_standard_admin_commands,
    get_standard_groups,
    ACCESS_DENIED_TEXT,
    ACCESS_REQUEST_SENT_TEXT,
    ACCESS_GRANTED_TEXT,
    INVITATION_TEXT,
    ACCESS_REVOKED_TEXT,
)
from hunttech_bot_common.users.ptb import (
    PTBUserHandlers,
    get_standard_commands,
    get_admin_commands,
    get_bot_access_path,
    get_shared_access_path,
)

__all__ = [
    "UserRecord",
    "user_from_telegram",
    "AccessManager",
    "UserSettingsManager",
    "AccessControlMiddleware",
    "CallbackAccessMiddleware",
    "sync_user_menu",
    "start_access_gate",
    "request_access_handler",
    "admin_approval_callback",
    "user_list_handler",
    "user_delete_callback",
    "access_callback_handler",
    "get_standard_user_commands",
    "get_standard_admin_commands",
    "get_standard_groups",
    "ACCESS_DENIED_TEXT",
    "ACCESS_REQUEST_SENT_TEXT",
    "ACCESS_GRANTED_TEXT",
    "INVITATION_TEXT",
    "ACCESS_REVOKED_TEXT",
    "PTBUserHandlers",
    "get_standard_commands",
    "get_admin_commands",
    "get_bot_access_path",
    "get_shared_access_path",
]
