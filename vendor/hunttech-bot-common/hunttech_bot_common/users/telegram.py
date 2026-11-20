"""Standard Telegram UI components for user management.

Provides ready-to-use handler generators for common user management flows:

- ``/start`` — access gate with request access flow
- ``/request_access`` — request access to the bot
- ``/user add|delete|list`` — admin user management
- ``admin:allow`` / ``admin:deny`` — callback handler for admin approval

Usage::

    from hunttech_bot_common.users.telegram import (
        start_access_gate,
        request_access_handler,
        admin_user_handlers,
        admin_approval_callback,
        sync_user_menu,
    )

    # In your bot's handler registration:
    dp.message.register(start_access_gate, Command("start"))
    dp.message.register(request_access_handler, Command("request_access"))
    dp.callback_query.register(admin_approval_callback, F.data.startswith("admin:"))
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from aiogram.enums import ParseMode

from hunttech_bot_common.telegram import (
    CommandDef,
    CommandGroup,
    escape_html,
    escape_md_simple,
    render_help_text,
)

logger = logging.getLogger(__name__)

# ── Constants ───────────────────────────────────────────────────

ACCESS_DENIED_TEXT = (
    "🚫 *Доступ запрещён*\n\n"
    "У вас нет доступа к этому боту.\n"
    "Отправьте `/request_access` чтобы запросить доступ у администратора."
)

ACCESS_REQUEST_SENT_TEXT = (
    "✅ *Запрос отправлен!*\n\n"
    "Администратор получил уведомление о вашем запросе.\n"
    "Пожалуйста, ожидайте — как только доступ будет предоставлен, "
    "вы получите уведомление."
)

ACCESS_GRANTED_TEXT = (
    "🎉 *Доступ предоставлен!*\n\n"
    "Теперь вы можете пользоваться ботом.\n"
    "Отправьте `/help` чтобы увидеть список доступных команд."
)

ACCESS_ALREADY_GRANTED_TEXT = (
    "✅ *У вас уже есть доступ к этому боту.*\n\n"
    "Отправьте `/help` чтобы увидеть список доступных команд."
)

PENDING_REQUEST_TEXT = (
    "⏳ *Запрос уже отправлен.*\n\n"
    "Ваш запрос на доступ к боту всё ещё находится на рассмотрении.\n"
    "Пожалуйста, ожидайте."
)

DENIED_REQUEST_TEXT = (
    "❌ *Запрос на доступ отклонён.*\n\n"
    "Администратор отклонил ваш запрос.\n"
    "Если это ошибка — обратитесь к администратору напрямую."
)

INVITATION_TEXT = (
    "📨 *Вас пригласили в бота!*\n\n"
    "Администратор предоставил вам доступ к боту.\n"
    "Отправьте `/start` чтобы начать использование."
)

ACCESS_REVOKED_TEXT = (
    "🚫 *Доступ к боту отозван.*\n\n"
    "Администратор отключил вас от бота.\n"
    "Если это ошибка — обратитесь к администратору."
)


# ── Menu sync ──────────────────────────────────────────────────

async def sync_user_menu(
    bot: Any,
    user_id: int,
    admin_ids: set[int],
    commands: list[CommandDef] | None = None,
) -> None:
    """Synchronise the Telegram bot command menu for a specific user.

    Only shows commands the user is allowed to use.

    Args:
        bot: aiogram Bot instance.
        user_id: Telegram user ID.
        admin_ids: Set of admin user IDs.
        commands: Full list of CommandDef. If None, skips menu sync.
    """
    if commands is None:
        return

    from aiogram.types import BotCommand, BotCommandScopeChat

    is_admin = user_id in admin_ids

    menu_commands: list[BotCommand] = []
    for cmd in commands:
        if cmd.hidden and not is_admin:
            continue
        if cmd.admin and not is_admin:
            continue
        if not cmd.show_in_menu:
            continue
        menu_commands.append(
            BotCommand(command=cmd.command, description=cmd.title or cmd.command)
        )

    try:
        await bot.set_my_commands(
            commands=menu_commands,
            scope=BotCommandScopeChat(chat_id=user_id),
        )
    except Exception as e:
        logger.warning("Failed to sync menu for user %s: %s", user_id, e)


# ── Start handler ──────────────────────────────────────────────

async def start_access_gate(
    event: Any,
    user_id: int,
    access_manager: Any,
    bot: Any,
    commands: list[CommandDef] | None = None,
    welcome_text: str | None = None,
    parse_mode: str | None = ParseMode.MARKDOWN,
) -> Any:
    """Handle /start command with access gate.

    Args:
        event: aiogram Message or CallbackQuery.
        user_id: Telegram user ID.
        access_manager: AccessManager instance.
        bot: aiogram Bot instance.
        commands: Full command list for menu sync.
        welcome_text: Custom welcome text for allowed users.
        parse_mode: Parse mode for welcome_text (default Markdown,
            pass None for plain text — единый формат HuntTech).

    Returns:
        "allowed", "denied", "pending", or "requested" based on user status.
        "requested" — unknown user: an access request was auto-created and
        the admin notified (with «✅ Разрешить / ❌ Запретить» buttons).
    """
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    # Sync menu first (before any message)
    if commands:
        await sync_user_menu(bot, user_id, access_manager.get_admin_ids(), commands)

    # Check if admin
    if access_manager.is_admin(user_id):
        text = welcome_text or (
            "👑 *Добро пожаловать, администратор!*\n\n"
            "Вы имеете полный доступ к боту.\n"
            f"Используйте `/help` для списка команд.\n"
            f"Управление пользователями: `/user list`"
        )
        await event.answer(text, parse_mode=parse_mode)
        return "allowed"

    # Check if already allowed
    if access_manager.is_allowed(user_id):
        text = welcome_text or (
            "👋 *С возвращением!*\n\n"
            "Ваш доступ активен.\n"
            "Используйте `/help` для списка команд."
        )
        await event.answer(text, parse_mode=parse_mode)
        return "allowed"

    # Check pending request
    status = access_manager.get_request_status(user_id)
    if status == "pending":
        # Show request pending message with button to re-request
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="⏳ Проверить статус",
                        callback_data="access:check_status",
                    ),
                ],
            ]
        )
        await event.answer(PENDING_REQUEST_TEXT, reply_markup=kb, parse_mode=ParseMode.MARKDOWN)
        return "pending"

    if status == "denied":
        # Show denied message with button to re-request
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="📨 Запросить доступ",
                        callback_data="access:request",
                    ),
                ],
            ]
        )
        await event.answer(DENIED_REQUEST_TEXT, reply_markup=kb, parse_mode=ParseMode.MARKDOWN)
        return "denied"

    # Not in any list — automatically create an access request and notify
    # the admin (owner's algorithm: user presses /start → admin gets
    # «✅ Разрешить / ❌ Запретить» buttons). request_access_handler answers
    # the user with «✅ Запрос отправлен!» and notifies the admin.
    from hunttech_bot_common.users.telegram import request_access_handler

    await request_access_handler(
        event,
        user_id,
        access_manager,
        bot,
        bot_name=access_manager.bot_name,
    )
    return "requested"


# ── Request access handler ─────────────────────────────────────

async def request_access_handler(
    event: Any,
    user_id: int,
    access_manager: Any,
    bot: Any,
    bot_name: str = "Bot",
    admin_notification_text: str | None = None,
) -> str:
    """Handle access request from a user.

    Creates a pending request and notifies the admin.

    Args:
        event: aiogram Message or CallbackQuery.
        user_id: Telegram user ID.
        access_manager: AccessManager instance.
        bot: aiogram Bot instance.
        bot_name: Bot name for admin notification.
        admin_notification_text: Custom admin notification text.

    Returns:
        "already_allowed", "already_pending", "denied_before", or "new_request".
    """
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    user = event.from_user
    result = access_manager.request_access(
        user_id=user_id,
        username=user.username,
        first_name=user.first_name,
        last_name=user.last_name,
    )

    if result["is_already_allowed"]:
        await event.answer(ACCESS_ALREADY_GRANTED_TEXT, parse_mode=ParseMode.MARKDOWN)
        return "already_allowed"

    if not result["is_new"]:
        # Already pending
        await event.answer(PENDING_REQUEST_TEXT, parse_mode=ParseMode.MARKDOWN)
        return "already_pending"

    # New request — notify admin.
    # Текст — HTML (не Markdown): username/имя пользователя содержат
    # спецсимволы (`_`, `*`, …), которые ломают Markdown при глобальном
    # parse_mode бота (реальный баг: @hunttech_short_vacancy_bot,
    # «can't parse entities» на @pavel_korab, 2026-08-07).
    user_info = result["user_info"]
    display_name = user_info.get("full_name") or user_info.get("username") or f"User#{user_id}"
    mention = f'<a href="tg://user?id={user_id}">{escape_html(display_name)}</a>'

    notif_text = admin_notification_text or (
        f"🔔 <b>Новый запрос доступа</b> к боту <i>{escape_html(bot_name)}</i>\n\n"
        f"👤 Пользователь: {mention}\n"
        f"🆔 ID: <code>{user_id}</code>\n"
        f"📝 Username: @{escape_html(user.username or '—')}\n"
        f"🌐 Язык: {escape_html(user.language_code or '—')}\n\n"
        "Что хотите сделать?"
    )

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Разрешить",
                    callback_data=f"admin:allow:{user_id}",
                ),
                InlineKeyboardButton(
                    text="❌ Запретить",
                    callback_data=f"admin:deny:{user_id}",
                ),
            ],
        ]
    )

    try:
        await bot.send_message(
            chat_id=access_manager.master_admin_id,
            text=notif_text,
            reply_markup=kb,
            parse_mode=ParseMode.HTML,
        )
    except Exception as e:
        logger.error("Failed to notify admin about access request: %s", e)

    await event.answer(ACCESS_REQUEST_SENT_TEXT, parse_mode=ParseMode.MARKDOWN)
    return "new_request"


# ── Admin approval callback ────────────────────────────────────

async def admin_approval_callback(
    callback: Any,
    access_manager: Any,
    bot: Any,
    bot_name: str = "Bot",
    welcome_text: str | None = None,
    welcome_parse_mode: str | None = None,
) -> None:
    """Handle admin approval/denial callback.

    Callback data format: ``admin:allow:USER_ID`` or ``admin:deny:USER_ID``

    Args:
        callback: aiogram CallbackQuery.
        access_manager: AccessManager instance.
        bot: aiogram Bot instance.
        bot_name: Bot name for user notification.
        welcome_text: Optional bot welcome text sent to the user right after
            the grant (contains /help hint). Pass None to keep the default
            invitation message only.
        welcome_parse_mode: Parse mode for welcome_text (e.g. None for plain
            text — стандарт HuntTech 08.2026). Ignored if welcome_text is None.
    """
    from aiogram.enums import ParseMode

    await callback.answer()

    admin_id = callback.from_user.id

    # Check that the clicker is actually admin
    if not access_manager.is_admin(admin_id):
        await callback.message.edit_text(
            "❌ *Недостаточно прав.*\n"
            "Только администратор может управлять доступом.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    parts = callback.data.split(":")
    if len(parts) < 3:
        await callback.message.edit_text("❌ *Некорректные данные.*", parse_mode=ParseMode.MARKDOWN)
        return

    action = parts[1]
    target_user_id = int(parts[2])

    if action == "allow":
        # Approve the request
        user_info = access_manager.get_user(target_user_id)

        if user_info:
            # User already existed (e.g., re-added)
            await callback.message.edit_text(
                f"✅ *Пользователь уже имеет доступ.*\n"
                f"🆔 `{target_user_id}`",
                parse_mode=ParseMode.MARKDOWN,
            )
            return

        # Approve and add user
        req = None
        for r in access_manager.get_pending_requests():
            if r["user_id"] == target_user_id:
                req = r
                break

        access_manager.approve_request(target_user_id, approved_by=admin_id)

        display_name = (req or {}).get("full_name") or f"User#{target_user_id}"
        mention = f'<a href="tg://user?id={target_user_id}">{escape_html(display_name)}</a>'

        await callback.message.edit_text(
            f"✅ *Доступ предоставлен!*\n"
            f"Пользователь {mention}\n"
            f"🆔 `{target_user_id}`\n\n"
            f"Пользователь получит уведомление.",
            parse_mode=ParseMode.HTML,
        )

        # Notify the user
        try:
            await bot.send_message(
                chat_id=target_user_id,
                text=INVITATION_TEXT,
                parse_mode=ParseMode.MARKDOWN,
            )
        except Exception:
            logger.warning(
                "Could not notify user %s about access grant "
                "(user may not have started the bot)",
                target_user_id,
            )

        # Welcome + /help hint right after the grant (стандарт HuntTech:
        # доступ предоставлен → пользователь сразу видит приветствие).
        if welcome_text:
            try:
                await bot.send_message(
                    chat_id=target_user_id,
                    text=welcome_text,
                    parse_mode=welcome_parse_mode,
                )
            except Exception:
                logger.warning(
                    "Could not send welcome to user %s after access grant",
                    target_user_id,
                )

    elif action == "deny":
        access_manager.deny_request(target_user_id)
        display_name = f"User#{target_user_id}"

        # Try to get name from pending
        for r in access_manager.get_pending_requests():
            if r["user_id"] == target_user_id:
                display_name = r.get("full_name") or f"User#{target_user_id}"
                break

        await callback.message.edit_text(
            f"❌ *Доступ отклонён* для `{target_user_id}` "
            f"({display_name}).",
            parse_mode=ParseMode.MARKDOWN,
        )

        # Notify the user about the denial so they know the request
        # was rejected (and can re-request via /start afterwards).
        try:
            await bot.send_message(
                chat_id=target_user_id,
                text=DENIED_REQUEST_TEXT,
                parse_mode=ParseMode.MARKDOWN,
            )
        except Exception:
            logger.warning(
                "Could not notify user %s about access denial "
                "(user may not have started the bot)",
                target_user_id,
            )


# ── Pending-запросы при старте бота ──────────────────────────────


async def notify_admin_of_pending_requests(
    bot: Any,
    access_manager: Any,
    bot_name: str = "Bot",
) -> int:
    """Отправить администратору уведомления о неподтверждённых запросах доступа.

    Вызывается при старте бота (после приветствия и changelog): если часть
    уведомлений потерялась (ошибка отправки, падение при обработке /start),
    админ всё равно узнает о висящих запросах и одобрит/отклонит кнопками
    «✅ Разрешить» / «❌ Запретить» (admin:allow / admin:deny).

    Возвращает количество отправленных уведомлений (0 — нет pending).
    """
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    admin_id = access_manager.master_admin_id
    sent = 0
    for req in access_manager.get_pending_requests():
        if req.get("status") != "pending":
            continue
        uid = req["user_id"]
        display_name = req.get("full_name") or req.get("username") or f"User#{uid}"
        mention = f'<a href="tg://user?id={uid}">{escape_html(display_name)}</a>'
        username = req.get("username")
        text = (
            f"🔔 <b>Запрос доступа</b> к боту <i>{escape_html(bot_name)}</i> "
            f"— ожидает рассмотрения\n\n"
            f"👤 Пользователь: {mention}\n"
            f"🆔 ID: <code>{uid}</code>\n"
            f"📝 Username: @{escape_html(username or '—')}\n\n"
            "Что хотите сделать?"
        )
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="✅ Разрешить",
                        callback_data=f"admin:allow:{uid}",
                    ),
                    InlineKeyboardButton(
                        text="❌ Запретить",
                        callback_data=f"admin:deny:{uid}",
                    ),
                ],
            ]
        )
        try:
            await bot.send_message(
                chat_id=admin_id,
                text=text,
                reply_markup=kb,
                parse_mode=ParseMode.HTML,
            )
            sent += 1
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "Failed to notify admin %s about pending request %s: %s",
                admin_id, uid, e,
            )
    return sent


# ── User list with delete buttons ──────────────────────────────

async def user_list_handler(
    event: Any,
    access_manager: Any,
    bot: Any,
) -> None:
    """Show list of allowed users with optional delete buttons.

    Args:
        event: aiogram Message.
        access_manager: AccessManager instance.
        bot: aiogram Bot instance.
    """
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    users = access_manager.get_allowed_users()
    pending = access_manager.get_pending_requests()

    lines: list[str] = []

    if pending:
        lines.append("⏳ *Ожидают подтверждения:*")
        for req in pending:
            if req.get("status") == "pending":
                name = req.get("full_name") or req.get("username") or f"User#{req['user_id']}"
                lines.append(f"  • `{req['user_id']}` — {escape_md_simple(name)}")
        lines.append("")
        lines.append("ℹ️ Используйте кнопки в уведомлении для одобрения.")
        lines.append("")

    if users:
        lines.append("👥 *Разрешённые пользователи:*")
        for u in users:
            name = u.get("full_name") or u.get("username") or f"User#{u['user_id']}"
            banned = " 🚫" if u.get("is_banned") else ""
            lines.append(f"  • `{u['user_id']}` — {escape_md_simple(name)}{banned}")

        lines.append("")
        lines.append(f"👑 *Администраторы:* `{access_manager.get_admin_ids()}`")
    else:
        lines.append("📭 *Нет разрешённых пользователей.*")
        lines.append("")
        lines.append(f"👑 *Администраторы:* `{access_manager.get_admin_ids()}`")

    text = "\n".join(lines)

    # Build delete buttons for active users
    kb_buttons = []
    for u in users:
        if u.get("is_banned"):
            continue
        name = u.get("full_name") or u.get("username") or f"User#{u['user_id']}"
        short_name = name[:30]
        kb_buttons.append([
            InlineKeyboardButton(
                text=f"❌ {short_name}",
                callback_data=f"userlist:del:{u['user_id']}",
            ),
        ])

    kb = InlineKeyboardMarkup(inline_keyboard=kb_buttons) if kb_buttons else None
    await event.answer(text, reply_markup=kb, parse_mode=ParseMode.MARKDOWN)


async def user_delete_callback(
    callback: Any,
    access_manager: Any,
    bot: Any,
) -> None:
    """Handle userlist:del callback — delete user and notify.

    Args:
        callback: aiogram CallbackQuery.
        access_manager: AccessManager instance.
        bot: aiogram Bot instance.
    """
    await callback.answer()

    admin_id = callback.from_user.id

    if not access_manager.is_admin(admin_id):
        await callback.answer(
            "❌ Только администратор может управлять доступом.",
            show_alert=True,
        )
        return

    parts = callback.data.split(":")
    if len(parts) < 3:
        return

    target_user_id = int(parts[2])

    if access_manager.remove_user(target_user_id):
        # Notify removed user
        try:
            await bot.send_message(
                chat_id=target_user_id,
                text=ACCESS_REVOKED_TEXT,
                parse_mode=ParseMode.MARKDOWN,
            )
        except Exception:
            pass

        await callback.message.edit_text(
            f"✅ *Пользователь `{target_user_id}` удалён.*\n"
            f"Уведомление отправлено.\n\n"
            f"ℹ️ Обновите список: `/user list`",
            parse_mode=ParseMode.MARKDOWN,
        )
    else:
        await callback.message.edit_text(
            f"❌ *Пользователь `{target_user_id}` не найден.*",
            parse_mode=ParseMode.MARKDOWN,
        )


# ── Access request from callback ───────────────────────────────

async def access_callback_handler(
    callback: Any,
    access_manager: Any,
    bot: Any,
    bot_name: str = "Bot",
) -> None:
    """Handle access:* callbacks from start access gate buttons.

    Supports:
    - ``access:request`` — user requests access
    - ``access:check_status`` — check request status

    Args:
        callback: aiogram CallbackQuery.
        access_manager: AccessManager instance.
        bot: aiogram Bot instance.
        bot_name: Bot name for admin notification.
    """
    await callback.answer()

    user_id = callback.from_user.id
    action = callback.data.split(":")[1]

    if action == "request":
        await request_access_handler(
            callback,
            user_id,
            access_manager,
            bot,
            bot_name=bot_name,
        )
    elif action == "check_status":
        status = access_manager.get_request_status(user_id)
        if status == "pending":
            await callback.message.edit_text(PENDING_REQUEST_TEXT, parse_mode=ParseMode.MARKDOWN)
        elif status == "denied":
            await callback.message.edit_text(DENIED_REQUEST_TEXT, parse_mode=ParseMode.MARKDOWN)
        else:
            # Already allowed or no request
            if access_manager.is_allowed(user_id):
                await callback.message.edit_text(
                    "✅ *У вас уже есть доступ к боту.*\n\n"
                    "Используйте `/start` чтобы начать.",
                    parse_mode=ParseMode.MARKDOWN,
                )
            else:
                await callback.message.edit_text(
                    "❌ *Запрос не найден.*\n\n"
                    "Отправьте `/request_access` чтобы запросить доступ.",
                    parse_mode=ParseMode.MARKDOWN,
                )


# ── Standard command definitions ───────────────────────────────

def get_standard_user_commands() -> list[CommandDef]:
    """Get standard user management command definitions.

    Returns a list of CommandDef that can be added to the bot's command registry.
    """
    return [
        CommandDef(
            command="start",
            title="Начать работу",
            description="Проверить доступ и начать работу с ботом",
            emoji="🚀",
            permissions=set(),
            admin=False,
            hidden=False,
            group="main",
        ),
        CommandDef(
            command="help",
            title="Помощь",
            description="Показать список команд",
            emoji="ℹ️",
            permissions=set(),
            admin=False,
            hidden=False,
            group="main",
        ),
        CommandDef(
            command="request_access",
            title="Запросить доступ",
            description="Отправить запрос на доступ к боту",
            emoji="📨",
            permissions=set(),
            admin=False,
            hidden=False,
            group="main",
        ),
        CommandDef(
            command="setup",
            title="Настройки",
            description="Управление личными настройками",
            emoji="⚙️",
            permissions={"setup"},
            admin=False,
            hidden=False,
            group="settings",
        ),
    ]


def get_standard_admin_commands() -> list[CommandDef]:
    """Get standard admin-only command definitions."""
    return [
        CommandDef(
            command="user",
            title="Управление пользователями",
            description="add/delete/list — управление доступом",
            emoji="👥",
            permissions=set(),
            admin=True,
            hidden=False,
            group="admin",
        ),
    ]


def get_standard_groups() -> list[CommandGroup]:
    """Get standard command groups for help rendering."""
    return [
        CommandGroup(key="main", title="Основное", emoji="🚀"),
        CommandGroup(key="settings", title="Настройки", emoji="⚙️"),
        CommandGroup(key="admin", title="Администрирование", emoji="👑"),
    ]


__all__ = [
    "sync_user_menu",
    "start_access_gate",
    "request_access_handler",
    "admin_approval_callback",
    "user_list_handler",
    "user_delete_callback",
    "access_callback_handler",
    "notify_admin_of_pending_requests",
    "get_standard_user_commands",
    "get_standard_admin_commands",
    "get_standard_groups",
    "ACCESS_DENIED_TEXT",
    "ACCESS_REQUEST_SENT_TEXT",
    "ACCESS_GRANTED_TEXT",
    "INVITATION_TEXT",
    "ACCESS_REVOKED_TEXT",
]
