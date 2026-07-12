"""
Menu adapter for HuntTechProtocols.

Implements Telegram BotCommand menu using the hunttech-bot-common library.
The bot itself has HELP_COMMANDS/HELP_GROUPS — this adapter maps them
to BotCommand format and handles per-user scope synchronization.
"""
import hashlib
import json
import logging
from typing import Optional

from hunttech_bot_common.telegram import build_menu_commands, menu_commands_hash

logger = logging.getLogger("bot")

ADMIN_USER_ID = 272980897

# ── Public menu commands ──────────────────────────────────────────────────
# Commands that appear in the Telegram BotCommand sidebar menu.
# Subset of HELP_COMMANDS — only main commands, no subcommands with spaces.
# Order matters: commands appear in this order in the menu.

PUBLIC_MENU_COMMANDS = [
    ("start", "👋 Начать работу с ботом"),
    ("list", "📬 Непрочитанные конспекты встреч"),
    ("list_all", "📋 Все конспекты за неделю"),
    ("list_new", "🆕 Новые конспекты (не показанные ранее)"),
    ("prompt", "🤖 Управление промптами"),
    ("setup", "🔧 Настроить почту, AI и Wiki"),
    ("setup_ai", "🧠 Настроить нейросеть"),
    ("wiki_test", "📚 Проверить Яндекс Вики"),
    ("help", "❓ Справка по командам"),
]

# Admin-only menu commands (shown only to admin)
ADMIN_MENU_COMMANDS = []

# Menu hash cache: {user_id: hash}
_menu_hashes: dict[str, str] = {}


def _build_menu(is_admin: bool = False) -> list[tuple[str, str]]:
    """Build menu commands list filtered by admin status.

    Returns list of (command, description) tuples for BotCommand API.
    """
    cmds = list(PUBLIC_MENU_COMMANDS)
    if is_admin:
        cmds.extend(ADMIN_MENU_COMMANDS)
    return cmds


def _hash(cmds: list[tuple[str, str]]) -> str:
    """Calculate stable hash of a menu command list."""
    raw = json.dumps(cmds, sort_keys=True, ensure_ascii=False)
    return hashlib.md5(raw.encode()).hexdigest()[:12]


def is_admin(user_id: int) -> bool:
    """Check if user is the bot admin."""
    return user_id == ADMIN_USER_ID


async def _sync_menu(bot, user_id: int) -> bool:
    """Synchronize command menu for a specific user.

    Uses BotCommandScopeChat for per-user targeting.
    Skips if menu hash hasn't changed (no redundant API calls).

    Args:
        bot: aiogram Bot instance.
        user_id: Telegram user ID.

    Returns:
        True if menu was synced, False on error.
    """
    is_admin_user = is_admin(user_id)
    cmds = _build_menu(is_admin=is_admin_user)
    h = _hash(cmds)
    cache_key = str(user_id)

    # Skip if menu hasn't changed
    if _menu_hashes.get(cache_key) == h:
        return True

    from aiogram.types import BotCommand, BotCommandScopeChat

    bot_commands = [BotCommand(command=cmd, description=desc) for cmd, desc in cmds]
    scope = BotCommandScopeChat(chat_id=user_id)

    try:
        await bot.set_my_commands(commands=bot_commands, scope=scope)
        _menu_hashes[cache_key] = h
        logger.info("✅ Menu synced for user %s (%d commands)", user_id, len(bot_commands))
        return True
    except Exception as e:
        logger.error("Failed to sync menu for user %s: %s", user_id, e)
        return False


async def _sync_default_menu(bot):
    """Set default menu for all users (fallback when no per-user scope)."""
    cmds = _build_menu(is_admin=False)
    from aiogram.types import BotCommand
    bot_commands = [BotCommand(command=cmd, description=desc) for cmd, desc in cmds]
    try:
        await bot.set_my_commands(commands=bot_commands, scope=None)
        logger.info("✅ Default menu synced: %d commands", len(bot_commands))
    except Exception as e:
        logger.error("Failed to sync default menu: %s", e)


async def _sync_admin_menu(bot):
    """Set admin-specific menu for admin user."""
    if not ADMIN_USER_ID:
        return
    cmds = _build_menu(is_admin=True)
    from aiogram.types import BotCommand, BotCommandScopeChat
    bot_commands = [BotCommand(command=cmd, description=desc) for cmd, desc in cmds]
    scope = BotCommandScopeChat(chat_id=ADMIN_USER_ID)
    try:
        await bot.set_my_commands(commands=bot_commands, scope=scope)
        _menu_hashes[str(ADMIN_USER_ID)] = _hash(cmds)
        logger.info("✅ Admin menu synced: %d commands", len(bot_commands))
    except Exception as e:
        logger.error("Failed to sync admin menu: %s", e)


def invalidate_menu(user_id: int):
    """Invalidate menu hash so next call triggers re-sync."""
    _menu_hashes.pop(str(user_id), None)
