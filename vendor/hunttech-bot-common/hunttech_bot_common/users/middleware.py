"""AccessControlMiddleware — aiogram middleware for user access control.

Blocks unauthorized users from using any bot command except /start.
Must be registered as the first middleware on the Dispatcher.

Usage::

    from hunttech_bot_common.users.middleware import AccessControlMiddleware

    dp.message.middleware.register(AccessControlMiddleware(
        get_access_manager=lambda: app.access_manager,
    ))
"""

from __future__ import annotations

import logging
from typing import Any, Callable

logger = logging.getLogger(__name__)


class AccessControlMiddleware:
    """aiogram middleware that blocks unauthorized users.

    Only /start is allowed through for unauthorized users (to show
    the "access denied" message). All other commands are silently blocked
    with a "🚫 Доступ запрещён" message.
    Non-command messages are passed through (they may be part of FSM).
    """

    def __init__(
        self,
        get_access_manager: Callable[[], Any | None],
        block_message: str | None = None,
    ) -> None:
        """Initialize middleware.

        Args:
            get_access_manager: Callable returning an AccessManager instance
                               (or None). Called per-request so the app
                               singleton can be set up after middleware registration.
            block_message: Custom message for blocked users.
                           If None, a default Russian message is used.
        """
        self._get_am = get_access_manager
        self._block_message = block_message or (
            "🚫 *Доступ запрещён*\n\n"
            "У вас нет доступа к этому боту.\n"
            "Отправьте `/start` чтобы запросить доступ у администратора."
        )

    async def __call__(
        self,
        handler: Callable,
        event: Any,
        data: dict[str, Any],
    ) -> Any:
        """Middleware call for incoming messages."""
        am = self._get_am()

        # If no AccessManager configured, allow everything
        if am is None:
            return await handler(event, data)

        user_id = event.from_user.id if event.from_user else None
        if user_id is None:
            return await handler(event, data)

        # Admin is always allowed
        if am.is_admin(user_id):
            return await handler(event, data)

        # Allowed user
        if am.is_allowed(user_id):
            return await handler(event, data)

        # Non-command messages from unauthorized users — block
        text = getattr(event, "text", None) or ""
        is_command = text.startswith("/")

        if not is_command:
            # Non-command from unauthorized user — pass through
            # (could be FSM input from an authorized flow)
            return await handler(event, data)

        command = text.split()[0].lower()

        # Allow /start for access gate
        if command == "/start":
            return await handler(event, data)

        # Block all other commands
        logger.info(
            "Blocked command '%s' from unauthorized user %s",
            command,
            user_id,
        )
        try:
            await event.answer(self._block_message)
        except Exception:
            pass
        return  # Don't call handler — command is silently blocked


class CallbackAccessMiddleware:
    """aiogram middleware that blocks callbacks from unauthorized users.

    Usage::

        dp.callback_query.middleware.register(CallbackAccessMiddleware(
            get_access_manager=lambda: app.access_manager,
        ))
    """

    def __init__(
        self,
        get_access_manager: Callable[[], Any | None],
    ) -> None:
        """Initialize middleware.

        Args:
            get_access_manager: Callable returning an AccessManager instance.
        """
        self._get_am = get_access_manager

    async def __call__(
        self,
        handler: Callable,
        event: Any,
        data: dict[str, Any],
    ) -> Any:
        """Middleware call for incoming callbacks."""
        am = self._get_am()

        if am is None:
            return await handler(event, data)

        user_id = event.from_user.id if event.from_user else None
        if user_id is None:
            return await handler(event, data)

        # Admin or allowed user
        if am.is_admin(user_id) or am.is_allowed(user_id):
            return await handler(event, data)

        # Access-request flow callbacks (access:*) MUST pass through from
        # unauthorized users — otherwise the «📨 Запросить доступ» button
        # after /start is dead and the admin never gets notified
        # (real bug: @hunttech_short_vacancy_bot, 2026-08-07).
        callback_data = getattr(event, "data", "") or ""
        if callback_data.startswith("access:"):
            return await handler(event, data)

        # Block callback from unauthorized user
        logger.info(
            "Blocked callback '%s' from unauthorized user %s",
            getattr(event, "data", ""),
            user_id,
        )
        try:
            await event.answer(
                "🚫 Доступ запрещён. Обратитесь к администратору.",
                show_alert=True,
            )
        except Exception:
            pass
        return


__all__ = [
    "AccessControlMiddleware",
    "CallbackAccessMiddleware",
]
