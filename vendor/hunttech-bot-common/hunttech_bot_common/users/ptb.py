"""Standard python-telegram-bot handlers for user management.

Provides ready-to-use handler factories for PTB-based bots:

- ``PTBUserHandlers`` — class with all standard user management handlers
- Can be registered onto any PTB ``Application``
- Uses ``AccessManager`` from ``hunttech_bot_common.users.access``

Usage::

    from hunttech_bot_common.users.ptb import PTBUserHandlers

    am = AccessManager(data_path="data/access.json", master_admin_id=12345)
    uh = PTBUserHandlers(access_manager=am, bot_name="My Bot")

    # Register all handlers
    uh.register(app, exclude=["start"])

    # Or get individual handlers:
    app.add_handler(CommandHandler("start", uh.start_handler))
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable

from hunttech_bot_common.users.access import AccessManager
from hunttech_bot_common.telegram import CommandDef, CommandGroup

logger = logging.getLogger(__name__)


def get_shared_access_path() -> Path:
    """[DEPRECATED] Get the shared access users database path for all HuntTech bots.

    Use :func:`get_bot_access_path` instead — each bot should have its own file.

    All bots share the same file at ~/.hermes/hunttech_bots/access_users.json.
    """
    import warnings
    warnings.warn(
        "get_shared_access_path is deprecated, use get_bot_access_path(bot_name)",
        DeprecationWarning,
        stacklevel=2,
    )
    try:
        from hermes_constants import get_hermes_home
        return get_hermes_home() / "hunttech_bots" / "access_users.json"
    except ImportError:
        return Path.home() / ".hermes" / "hunttech_bots" / "access_users.json"


def get_bot_access_path(bot_name: str) -> Path:
    """Get the per-bot access users database path.

    Each bot has its own file at ``~/.hermes/hunttech_bots/access_{bot_name}.json``.
    This ensures that access is granted per-bot, not globally.

    Args:
        bot_name: Unique bot identifier (e.g. ``hunttechprotocols``).
    """
    bot_name = bot_name.strip().replace("/", "_").replace("\\", "_")
    try:
        from hermes_constants import get_hermes_home
        base = get_hermes_home() / "hunttech_bots"
    except ImportError:
        base = Path.home() / ".hermes" / "hunttech_bots"
    base.mkdir(parents=True, exist_ok=True)
    return base / f"access_{bot_name}.json"


class PTBUserHandlers:
    """Standard user management handlers for python-telegram-bot.

    Provides ready-to-use handler callbacks for:
    - /start — access gate
    - /request_access — request access to the bot
    - /user list — admin: list allowed users
    - /user add <id> — admin: add user
    - /user remove <id> — admin: remove user
    - /user ban <id> — admin: ban user
    - /user unban <id> — admin: unban user
    """

    @classmethod
    def from_bot_db(
        cls,
        bot_name: str,
        master_admin_id: int,
        **kwargs: Any,
    ) -> "PTBUserHandlers":
        """Create PTBUserHandlers with a per-bot access database.

        Each bot gets its own access file, so access must be granted
        per-bot — not shared across all HuntTech bots.

        Args:
            bot_name: Unique bot identifier (e.g. ``hunttechprotocols``).
            master_admin_id: Telegram user ID of the master admin.
        """
        am = AccessManager(
            data_path=get_bot_access_path(bot_name),
            master_admin_id=master_admin_id,
            bot_name=bot_name,
        )
        return cls(access_manager=am, bot_name=bot_name, **kwargs)

    @classmethod
    def from_shared_db(
        cls,
        master_admin_id: int,
        bot_name: str = "Bot",
        **kwargs: Any,
    ) -> "PTBUserHandlers":
        """Create PTBUserHandlers with the shared user database."""
        am = AccessManager(
            data_path=get_shared_access_path(),
            master_admin_id=master_admin_id,
            bot_name=bot_name,
        )
        return cls(access_manager=am, bot_name=bot_name, **kwargs)

    def __init__(
        self,
        access_manager: AccessManager,
        bot_name: str = "Bot",
        welcome_admin_text: str | None = None,
        welcome_user_text: str | None = None,
        access_denied_text: str | None = None,
    ) -> None:
        self._am = access_manager
        self._bot_name = bot_name
        self._welcome_admin = welcome_admin_text or (
            "👑 Добро пожаловать, администратор!\n\n"
            "Вы имеете полный доступ к боту.\n"
            "/help — список команд\n"
            "/user list — управление пользователями"
        )
        self._welcome_user = welcome_user_text or (
            "👋 С возвращением!\n"
            "/help — список команд"
        )
        self._access_denied_text = access_denied_text or (
            "🚫 Доступ запрещён.\n"
            "Отправьте /request_access чтобы запросить доступ у администратора."
        )

    # ── Properties ────────────────────────────────────────────

    @property
    def access_manager(self) -> AccessManager:
        return self._am

    # ── Handlers ──────────────────────────────────────────────

    async def is_allowed(self, update: Any) -> bool:
        """Check if the user from this update is allowed. Use as middleware/guard."""
        user = getattr(update, "effective_user", None)
        user_id = None if user is None else getattr(user, "id", None)
        if user_id is None:
            return False
        return self._am.is_allowed(user_id)

    async def is_admin(self, update: Any) -> bool:
        """Check if the user from this update is an admin."""
        user = getattr(update, "effective_user", None)
        user_id = None if user is None else getattr(user, "id", None)
        if user_id is None:
            return False
        return self._am.is_admin(user_id)

    async def start_handler(self, update: Any, context: Any) -> None:
        """/start — access gate with welcome message."""
        from telegram import Update
        from telegram.ext import ContextTypes

        message = update.effective_message
        if not message:
            return

        user = update.effective_user
        user_id = user.id
        username = user.username or ""
        first_name = user.first_name or ""
        last_name = user.last_name or ""

        if self._am.is_admin(user_id):
            await message.reply_text(self._welcome_admin)
            return

        if self._am.is_allowed(user_id):
            await message.reply_text(self._welcome_user)
            return

        # Unknown user — request access
        result = self._am.request_access(
            user_id=user_id,
            username=username,
            first_name=first_name,
            last_name=last_name,
        )

        if result.get("is_already_allowed"):
            await message.reply_text("✅ Доступ уже есть. /help")
            return

        if not result.get("is_new"):
            await message.reply_text("⏳ Ваш запрос на рассмотрении. Ожидайте.")
            return

        # Notify admin
        display_name = f"{first_name} {last_name}".strip() or username or f"User#{user_id}"
        admin_text = (
            f"🔔 Новый запрос доступа к боту *{self._bot_name}*\n\n"
            f"Пользователь: {display_name}\n"
            f"ID: `{user_id}`\n"
            f"Username: @{username}\n\n"
            f"Разрешить: /user add {user_id}"
        )
        try:
            await context.bot.send_message(chat_id=self._am.master_admin_id, text=admin_text)
        except Exception as exc:
            logger.warning("Failed to notify admin: %s", exc)

        await message.reply_text("✅ Запрос отправлен администратору. Ожидайте.")

    async def request_access_handler(self, update: Any, context: Any) -> None:
        """/request_access — alias to start access request."""
        await self.start_handler(update, context)

    async def user_command_handler(self, update: Any, context: Any) -> None:
        """/user [list|add|remove|ban|unban] — user management (admin only)."""
        from telegram import Update
        from telegram.ext import ContextTypes

        message = update.effective_message
        if not message:
            return

        user_id = update.effective_user.id
        args = message.text.strip().split()
        cmd = args[1] if len(args) > 1 else "help"
        logger.info("USER_MGMT: admin_id=%d action=%s target=%s", user_id, cmd, args[2] if len(args) > 2 else "—")

        if not self._am.is_admin(user_id):
            await message.reply_text("Только администратор может управлять пользователями.")
            return

        if cmd in ("list", "список"):
            users = self._am.get_allowed_users()
            pending = self._am.get_pending_requests()
            lines = []
            if pending:
                lines.append("⏳ Ожидают подтверждения:")
                for req in pending:
                    if req.get("status") == "pending":
                        name = req.get("full_name") or req.get("username") or f"User#{req['user_id']}"
                        lines.append(f"  • {name} (id={req['user_id']})")
                lines.append("")
            if users:
                lines.append("✅ Разрешённые пользователи:")
                for u in users:
                    name = u.get("full_name") or u.get("username") or f"User#{u['user_id']}"
                    banned = " 🚫(забанен)" if u.get("is_banned") else ""
                    lines.append(f"  • {name} (id={u['user_id']}){banned}")
            else:
                lines.append("Нет разрешённых пользователей.")
            lines.append("")
            lines.append(f"👑 Администратор: id={self._am.master_admin_id}")
            await message.reply_text("\n".join(lines))
            return

        # All following commands need a target ID
        if len(args) < 3:
            await message.reply_text("Укажите Telegram ID: /user <cmd> <id>")
            return

        try:
            target_id = int(args[2])
        except ValueError:
            await message.reply_text("Укажите числовой Telegram ID.")
            return

        if cmd in ("add", "добавить"):
            self._am.add_user(user_id=target_id, added_by=user_id)
            await message.reply_text(f"✅ Пользователь {target_id} добавлен.")
            return

        if cmd in ("remove", "delete", "удалить"):
            if target_id == self._am.master_admin_id:
                await message.reply_text("Нельзя удалить главного администратора.")
                return
            if self._am.remove_user(target_id):
                await message.reply_text(f"✅ Пользователь {target_id} удалён.")
            else:
                await message.reply_text(f"Пользователь {target_id} не найден.")
            return

        if cmd in ("ban", "заблокировать"):
            if target_id == self._am.master_admin_id:
                await message.reply_text("Нельзя заблокировать главного администратора.")
                return
            if self._am.ban_user(target_id):
                await message.reply_text(f"🚫 Пользователь {target_id} заблокирован.")
            else:
                await message.reply_text(f"Пользователь {target_id} не найден.")
            return

        if cmd in ("unban", "разблокировать"):
            if self._am.unban_user(target_id):
                await message.reply_text(f"✅ Пользователь {target_id} разблокирован.")
            else:
                await message.reply_text(f"Пользователь {target_id} не найден.")
            return

        # Default: show help
        await self._show_user_help(message)

    async def _show_user_help(self, message: Any) -> None:
        lines = [
            "Управление пользователями:",
            "",
            "/user list — список пользователей",
            "/user add <id> — добавить пользователя",
            "/user remove <id> — удалить пользователя",
            "/user ban <id> — заблокировать",
            "/user unban <id> — разблокировать",
            "/request_access — запросить доступ",
        ]
        await message.reply_text("\n".join(lines))

    # ── Registration helper ───────────────────────────────────

    def register(
        self,
        app: Any,
        *,
        exclude: set[str] | None = None,
    ) -> None:
        """Register all standard handlers onto a PTB Application.

        Args:
            app: PTB ``Application`` instance.
            exclude: Set of handler names to skip (e.g. ``{"start"}``).
        """
        from telegram.ext import CommandHandler

        exclude = exclude or set()

        if "start" not in exclude:
            app.add_handler(CommandHandler("start", self.start_handler))
        if "request_access" not in exclude:
            app.add_handler(CommandHandler("request_access", self.request_access_handler))
        if "user" not in exclude:
            app.add_handler(CommandHandler("user", self.user_command_handler))

    # ── Standard command definitions ──────────────────────────

    def get_commands(self) -> list[CommandDef]:
        """Get standard command definitions for help rendering."""
        return get_standard_commands()

    def get_admin_commands(self) -> list[CommandDef]:
        """Get admin-only command definitions."""
        return get_admin_commands()


def get_standard_commands() -> list[CommandDef]:
    """Standard user-facing commands available to all allowed users."""
    return [
        CommandDef(command="start", title="Запустить бота", description="Начать работу с ботом"),
        CommandDef(command="request_access", title="Запросить доступ", description="Отправить запрос администратору"),
        CommandDef(command="help", title="Справка", description="Показать список команд"),
    ]


def get_admin_commands() -> list[CommandDef]:
    """Admin-only command definitions."""
    return [
        CommandDef(
            command="user",
            title="Управление пользователями",
            description="user list|add|remove|ban|unban",
            admin=True,
        ),
    ]


__all__ = [
    "PTBUserHandlers",
    "get_standard_commands",
    "get_admin_commands",
]
