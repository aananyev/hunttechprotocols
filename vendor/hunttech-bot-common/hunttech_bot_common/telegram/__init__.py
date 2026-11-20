"""Telegram module — bot utilities for commands, escaping, callbacks, and permissions."""

from __future__ import annotations

import html as stdlib_html
import re
from dataclasses import dataclass, field
from typing import Iterable, Protocol, Sequence


# --- Command definitions ---

@dataclass
class CommandDef:
    """Definition of a bot command."""

    command: str
    title: str = ""
    description: str = ""
    emoji: str = ""
    permissions: set[str] = field(default_factory=set)
    admin: bool = False
    hidden: bool = False
    group: str = ""
    aliases: list[str] = field(default_factory=list)
    subcommands: list[str] = field(default_factory=list)
    syntax: str = ""
    show_in_help: bool = True
    show_in_menu: bool = True
    details: str = ""
    handler_name: str = ""
    order: int = 0
    public: bool = True


@dataclass
class CommandGroup:
    """Grouping for bot commands in help text."""

    key: str
    title: str
    emoji: str = ""
    description: str = ""
    order: int = 0


# --- Permission checker protocol ---

class PermissionChecker(Protocol):
    """Protocol for checking user permissions."""

    async def __call__(self, user_id: int, permission: str) -> bool:
        ...


# --- Escaping ---

_MD_SPECIAL_CHARS = re.compile(r"([_*\[\]()~`>#+\-=|{}.!\\])")


def escape_md_simple(text: str) -> str:
    """Escape MarkdownV2 special characters in text.

    Escapes: _ * [ ] ( ) ~ ` > # + - = | { } . ! \\
    """
    return _MD_SPECIAL_CHARS.sub(r"\\\1", text)


def escape_html(text: str) -> str:
    """Escape HTML special characters for Telegram HTML parse mode.
    Принимает любой тип: str(), int, float — всё оборачивается в str().
    """
    return stdlib_html.escape(str(text), quote=True)


# --- Message splitting ---

def split_long_message(text: str, max_len: int = 3800) -> list[str]:
    """Split a long message into chunks by paragraphs.

    Each chunk will be at most ``max_len`` characters.
    Preserves paragraph breaks where possible.
    """
    if len(text) <= max_len:
        return [text]

    paragraphs = text.split("\n\n")
    chunks: list[str] = []
    current = ""

    for para in paragraphs:
        # Check if adding this paragraph would exceed the limit
        if current and len(current) + 2 + len(para) > max_len:
            chunks.append(current)
            current = ""

        # Handle a paragraph that itself exceeds the limit
        if len(para) > max_len:
            # Flush current buffer first
            if current:
                chunks.append(current)
                current = ""
            lines = para.split("\n")
            for line in lines:
                if current and len(current) + 1 + len(line) > max_len:
                    chunks.append(current)
                    current = line
                elif len(line) > max_len:
                    if current:
                        chunks.append(current)
                        current = ""
                    # Split by characters as last resort
                    for i in range(0, len(line), max_len):
                        chunks.append(line[i : i + max_len])
                else:
                    current = f"{current}\n{line}" if current else line
        else:
            current = f"{current}\n\n{para}" if current else para

    if current:
        chunks.append(current)

    return chunks


# --- Callback data helpers ---

_CALLBACK_SEPARATOR = ":"


def make_callback_data(*parts: str) -> str:
    """Join callback data parts with the separator."""
    return _CALLBACK_SEPARATOR.join(parts)


def parse_callback_data(data: str) -> list[str]:
    """Split callback data by the separator."""
    return data.split(_CALLBACK_SEPARATOR)


# --- Help text rendering ---

def render_help_text(
    commands: Sequence[CommandDef],
    groups: Sequence[CommandGroup],
    user_permissions: set[str] | None = None,
    admin_ids: set[int] | None = None,
    user_id: int | None = None,
) -> str:
    """Render help text from command definitions.

    Organises commands by group, respecting permissions and admin visibility.

    Args:
        commands: List of command definitions.
        groups: List of command groups.
        user_permissions: Set of permission strings the user has.
        admin_ids: Set of admin user IDs.
        user_id: Current user's ID for admin check.

    Returns:
        Formatted help text string.
    """
    if user_permissions is None:
        user_permissions = set()
    if admin_ids is None:
        admin_ids = set()

    is_admin = user_id is not None and user_id in admin_ids

    # Build group lookup
    group_map = {g.key: g for g in groups}

    # Filter visible commands
    visible_commands: list[CommandDef] = []
    for cmd in commands:
        if cmd.hidden and not is_admin:
            continue
        if cmd.admin and not is_admin:
            continue
        if cmd.permissions and not cmd.permissions.intersection(user_permissions):
            continue
        visible_commands.append(cmd)

    if not visible_commands:
        return "No commands available."

    # Group commands
    grouped: dict[str, list[CommandDef]] = {}
    for cmd in visible_commands:
        grouped.setdefault(cmd.group, []).append(cmd)

    lines: list[str] = ["*Available commands:*\n"]

    for group_key in sorted(grouped.keys()):
        grp = group_map.get(group_key)
        if grp and grp.emoji:
            lines.append(f"\n{grp.emoji} *{grp.title}*")
        elif grp:
            lines.append(f"\n*{grp.title}*")

        for cmd in sorted(grouped[group_key], key=lambda c: c.command):
            emoji = f"{cmd.emoji} " if cmd.emoji else ""
            if cmd.title:
                lines.append(
                    f"  {emoji}/`{cmd.command}` — {cmd.title}"
                )
            else:
                lines.append(f"  {emoji}/`{cmd.command}`")
            if cmd.description:
                lines.append(f"      {cmd.description}")

    return "\n".join(lines)


def send_with_hidden_keyboard(
    bot,
    chat_id: int,
    text: str,
    reply_markup=None,
    parse_mode=None,
    **kwargs
):
    """Отправляет сообщение, предварительно скрывая постоянную нижнюю клавиатуру.

    Это предотвращает перекрытие inline-кнопок постоянной ReplyKeyboard.

    Args:
        bot: Экземпляр bot с методом send_message (например, aiogram.Bot)
        chat_id: ID чата
        text: Текст сообщения
        reply_markup: Клавиатура (обычно InlineKeyboardMarkup)
        parse_mode: Режим парсинга (например, ParseMode.MARKDOWN из aiogram)
        **kwargs: Дополнительные аргументы для send_message

    Returns:
        Результат отправки сообщения
    """
    from aiogram.types import ReplyKeyboardRemove
    import asyncio

    async def _send():
        # Сначала скрываем постоянную клавиатуру
        await bot.send_message(chat_id, " ", reply_markup=ReplyKeyboardRemove())
        # Затем отправляем основное сообщение
        return await bot.send_message(
            chat_id=chat_id,
            text=text,
            reply_markup=reply_markup,
            parse_mode=parse_mode,
            **kwargs
        )

    return asyncio.create_task(_send())


__all__ = [
    "CommandDef",
    "CommandGroup",
    "PermissionChecker",
    "escape_md_simple",
    "escape_html",
    "split_long_message",
    "make_callback_data",
    "parse_callback_data",
    "render_help_text",
]
