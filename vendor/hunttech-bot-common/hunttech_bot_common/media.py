"""Медиа: логотип HuntTech для приветствий (стандарт HuntTech).

При /start и при старте бота логотип компании отправляется ПЕРВЫМ
сообщением — над приветствием (эталон — @hunttech_short_vacancy_bot,
одобрено владельцем).

Файл логотипа лежит в ``hunttech_bot_common/assets/hunttech_logo.png``.
``send_logo`` не роняет поток приветствия: при отсутствии файла или
ошибке отправки возвращает False.

Поддерживает и aiogram (``FSInputFile``), и python-telegram-bot
(``telegram.InputFile``) — фреймворк определяется доступным импортом.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

LOGO_PATH = Path(__file__).resolve().parent / "assets" / "hunttech_logo.png"


def _is_ptb_bot(bot: Any) -> bool:
    """PTB-бот (python-telegram-bot)? Фреймворк определяем ПО ТИПУ объекта.

    Импорт-проба не годится: в venv Hermes установлены и aiogram, и PTB —
    по доступности aiogram всегда «побеждает», и PTB-боту уходит
    aiogram-объект FSInputFile → `FSInputFile.read() missing 'bot'`
    (реальный баг docs-бота, 2026-08-06).
    """
    try:
        from telegram import Bot as PTBBot
    except ImportError:
        return False
    return isinstance(bot, PTBBot)


def _photo_for_bot(bot: Any) -> Any:
    """Фото-объект логотипа под фреймворк бота (InputFile/FSInputFile).

    Питфолл PTB v22: InputFile НЕ принимает pathlib.Path (только
    IO[bytes]/bytes/str-as-содержимое) — `'PosixPath' object has no
    attribute 'read'`. Читаем файл в bytes с именем.
    """
    if _is_ptb_bot(bot):
        from telegram import InputFile  # python-telegram-bot

        return InputFile(LOGO_PATH.read_bytes(), filename=LOGO_PATH.name)
    from aiogram.types import FSInputFile

    return FSInputFile(str(LOGO_PATH))


async def send_logo(bot: Any, chat_id: int) -> bool:
    """Отправить фото-логотип HuntTech над приветствием.

    Args:
        bot: aiogram Bot или PTB Application.bot.
        chat_id: получатель.

    Returns:
        True при успехе; False при отсутствии файла или ошибке
        (не роняет поток приветствия).
    """
    try:
        if not LOGO_PATH.exists():
            logger.warning("Логотип не найден: %s", LOGO_PATH)
            return False
        await bot.send_photo(chat_id=chat_id, photo=_photo_for_bot(bot))
        return True
    except Exception as e:  # noqa: BLE001
        logger.warning("Не удалось отправить логотип %s: %s", chat_id, e)
        return False
