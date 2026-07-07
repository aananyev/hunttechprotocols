#!/usr/bin/env python3
"""
Telegram Bot: достаёт НЕПРОЧИТАННЫЕ письма с темой "Конспект встречи" из IMAP почты,
извлекает txt-содержимое и выводит нумерованный список в обратном хронологическом порядке.
После просмотра помечает письма как прочитанные.
"""

import asyncio
import imaplib
import email
import logging
import httpx
import json
from email.header import decode_header
from email.utils import parsedate_to_datetime
from datetime import datetime
from pathlib import Path
from typing import Optional

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("bot")

from aiogram import Bot, Dispatcher
from aiogram.filters import Command, CommandObject
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from aiogram.enums import ParseMode

import os
from dotenv import load_dotenv

# ── Конфигурация ──────────────────────────────────────────────

load_dotenv()
TG_TOKEN = os.getenv("TG_TOKEN", "") or exit("❌ TG_TOKEN не задан! Положи токен в .env")

SUBJECT_FILTER = "Конспект встречи"
MAX_MSG_LEN = 3800  # Telegram лимит ~4096, оставляем запас под Markdown
USERS_FILE = Path(__file__).parent / "users.json"


# ── Хранилище пользовательских настроек почты ─────────────────

def _load_users() -> dict:
    """Загружает настройки пользователей: {user_id: {email, server, port, password}}"""
    if not USERS_FILE.exists():
        return {}
    try:
        with open(USERS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except (json.JSONDecodeError, IOError):
        pass
    return {}


def _save_users(users: dict):
    """Сохраняет настройки пользователей."""
    with open(USERS_FILE, "w", encoding="utf-8") as f:
        json.dump(users, f, ensure_ascii=False, indent=2)


def get_user_config(user_id: int) -> dict | None:
    """Возвращает настройки пользователя или None."""
    users = _load_users()
    return users.get(str(user_id))


def save_user_config(user_id: int, email: str, server: str, login: str, password: str):
    """Сохраняет настройки пользователя (IMAP + AI)."""
    users = _load_users()
    key = str(user_id)
    if key not in users:
        users[key] = {}
    users[key].update({
        "email": email,
        "server": server,
        "port": 993,
        "login": login,
        "password": password,
    })
    _save_users(users)


def save_ai_config(user_id: int, endpoint: str, api_key: str, model: str):
    """Сохраняет AI-настройки пользователя."""
    users = _load_users()
    key = str(user_id)
    if key not in users:
        users[key] = {}
    users[key]["ai"] = {
        "endpoint": endpoint,
        "api_key": api_key,
        "model": model,
    }
    _save_users(users)


def get_ai_config(user_id: int) -> dict | None:
    """Возвращает AI-настройки пользователя или None."""
    config = get_user_config(user_id)
    if config and "ai" in config:
        return config["ai"]
    return None


# ── Хелперы ───────────────────────────────────────────────────

def decode_mime_header(header_value: str) -> str:
    """Декодирует MIME-заголовок (QP, Base64 и т.д.) в читаемую строку."""
    if header_value is None:
        return ""
    parts = decode_header(header_value)
    result: list[str] = []
    for part, charset in parts:
        if isinstance(part, bytes):
            try:
                result.append(part.decode(charset or "utf-8", errors="replace"))
            except (LookupError, UnicodeDecodeError):
                result.append(part.decode("utf-8", errors="replace"))
        else:
            result.append(str(part))
    return "".join(result)


def get_email_date(msg) -> Optional[datetime]:
    """Извлекает дату письма из Date-заголовка."""
    date_str = msg.get("Date")
    if not date_str:
        return None
    try:
        return parsedate_to_datetime(date_str)
    except (ValueError, TypeError):
        return None


def extract_txt_attachments(msg) -> list[str]:
    """
    Рекурсивно обходит части письма. Возвращает содержимое:
      - text/plain вложений (файлы .txt)
      - text/plain тела письма (если нет отдельных вложений)
    """
    texts: list[str] = []

    def _walk(part):
        content_type = part.get_content_type()
        content_disposition = str(part.get("Content-Disposition", "")).lower()

        if content_type == "text/plain":
            try:
                payload = part.get_payload(decode=True)
                if payload:
                    charset = part.get_content_charset() or "utf-8"
                    texts.append(payload.decode(charset, errors="replace"))
            except Exception:
                pass

        if part.is_multipart():
            for sub in part.get_payload():
                if isinstance(sub, email.message.Message):
                    _walk(sub)

    _walk(msg)
    return texts


# ── Общие IMAP-хелперы ────────────────────────────────────────

def _connect_imap(config: dict) -> imaplib.IMAP4_SSL:
    """Подключается к IMAP по настройкам пользователя и возвращает объект с открытой INBOX."""
    imap_login = config.get("login", config["email"])
    logger.info("Подключение к IMAP %s (login: %s)...", config["server"], imap_login)
    server = imaplib.IMAP4_SSL(config["server"], config.get("port", 993))
    server.login(imap_login, config["password"])
    server.select("INBOX")
    logger.info("Успешно подключились к IMAP")
    return server


def fetch_notes(user_id: int) -> tuple[str, list]:
    """Ищет НЕПРОЧИТАННЫЕ письма с темой, начинающейся с "Конспект встречи".
    Возвращает (текст_для_вывода, список_совпадений)."""
    config = get_user_config(user_id)
    if not config:
        return ("❌ Почта не настроена. Используйте /setup для настройки.", [])
    server = _connect_imap(config)
    try:
        typ, data = server.search(None, "UNSEEN")
        unseen_ids = data[0].split() if data[0] else []
        logger.info("Непрочитанных писем всего: %d", len(unseen_ids))
        if not unseen_ids:
            return ("📭 Нет непрочитанных писем.", [])
        matched = _filter_and_extract(server, unseen_ids)
        if not matched:
            return ("📭 Нет непрочитанных писем с темой «Конспект встречи».", [])
        return (_format_list(matched, "📋 **Новые конспекты встреч**"), matched)
    finally:
        server.close()
        server.logout()


def fetch_notes_last_week(user_id: int) -> tuple[str, list]:
    """Ищет ВСЕ письма с темой, начинающейся с "Конспект встречи", за последние 7 дней.
    Возвращает (текст_для_вывода, список_совпадений)."""
    config = get_user_config(user_id)
    if not config:
        return ("❌ Почта не настроена. Используйте /setup для настройки.", [])
    server = _connect_imap(config)
    try:
        typ, data = server.search(None, "ALL")
        all_ids = data[0].split() if data[0] else []
        logger.info("Всего писем в ящике: %d", len(all_ids))
        if not all_ids:
            return ("📭 В почтовом ящике нет писем.", [])
        now = datetime.now()
        week_ago = now.timestamp() - 7 * 24 * 3600
        matched = _filter_and_extract(server, all_ids)
        matched = [(dt, r, t) for dt, r, t in matched if dt.timestamp() >= week_ago]
        if not matched:
            return ("📭 За последнюю неделю нет конспектов встреч.", [])
        return (_format_list(matched, "📋 **Конспекты встреч за неделю**"), matched)
    finally:
        server.close()
        server.logout()


def _filter_and_extract(server, msg_ids: list[bytes]) -> list[tuple]:
    """Принимает список ID писем, возвращает совпадающие по теме.
       Каждый элемент: (datetime, display_text, txt_content).
       Использует BODY.PEEK[] — НЕ устанавливает флаг \\Seen."""
    matched: list[tuple] = []

    for msg_id in msg_ids:
        # BODY.PEEK[] читает письмо НЕ устанавливая флаг прочтения
        typ, msg_data = server.fetch(msg_id, "(BODY.PEEK[])")
        if typ != "OK":
            continue

        raw_email = msg_data[0][1]
        msg = email.message_from_bytes(raw_email)

        subject = decode_mime_header(msg.get("Subject", ""))
        if not subject.lower().startswith(SUBJECT_FILTER.lower()):
            continue

        clean_subject = subject
        for ch in ("«", "»", '"', "'"):
            clean_subject = clean_subject.replace(ch, "")
        clean_subject = clean_subject.strip()

        remainder = subject[len(SUBJECT_FILTER):].strip()
        for ch in ("«", "»", '"', "'"):
            remainder = remainder.replace(ch, "")
        remainder = remainder.strip()

        if remainder.lower().startswith("от "):
            display = clean_subject
        else:
            display = remainder

        dt = get_email_date(msg) or datetime.now()

        # Извлекаем txt-содержимое (вложение или тело письма)
        txts = extract_txt_attachments(msg)
        txt_content = "\n\n---\n\n".join(txts) if txts else ""

        matched.append((dt, display, txt_content))

        # Гарантия: явно снимаем флаг \\Seen, если сервер его вдруг поставил
        try:
            server.store(msg_id, "-FLAGS", "(\\Seen)")
        except Exception:
            pass

    matched.sort(key=lambda x: x[0], reverse=True)
    return matched


def _format_list(matched: list[tuple[datetime, str]], title: str) -> str:
    """Форматирует список в текст для Telegram."""
    lines: list[str] = []
    lines.append(f"{title} — всего {len(matched)}")
    lines.append("")
    for idx, (_, display) in enumerate(matched, 1):
        lines.append(f"{idx}. {display}")
    return "\n".join(lines)


# ── Telegram Bot ───────────────────────────────────────────────

import json
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from aiogram import Router
from aiogram.types import ReplyKeyboardRemove

bot = Bot(token=TG_TOKEN)
dp = Dispatcher()


# ── Хранилище промптов ──────────────────────────────────────

PROMPTS_FILE = Path(__file__).parent / "prompts.json"


def _load_prompts() -> dict[str, str]:
    """Загружает промпты из JSON-файла."""
    if not PROMPTS_FILE.exists():
        return {}
    try:
        with open(PROMPTS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except (json.JSONDecodeError, IOError):
        pass
    return {}


def _save_prompts(prompts: dict[str, str]):
    """Сохраняет промпты в JSON-файл."""
    with open(PROMPTS_FILE, "w", encoding="utf-8") as f:
        json.dump(prompts, f, ensure_ascii=False, indent=2)


def _format_prompt_list() -> str:
    """Возвращает список промптов с командами управления."""
    prompts = _load_prompts()
    lines = ["📜 **Мои промпты**"]
    lines.append("")
    if not prompts:
        lines.append("_Промптов пока нет._")
    else:
        for idx, topic in enumerate(sorted(prompts.keys()), 1):
            lines.append(f"{idx}. {topic}")
    lines.append("")
    lines.append("── Управление ──")
    lines.append("/add_prompt    — добавить новый промпт")
    lines.append("/edit_prompt   — редактировать промпт")
    lines.append("/text_prompt   — показать текст промпта")
    lines.append("/delete_prompt — удалить промпт")
    return "\n".join(lines)


def _prompt_keyboard() -> InlineKeyboardMarkup:
    """Инлайн-клавиатура с кнопками команд группы промптов."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="➕ Добавить", callback_data="prompt:add"),
            InlineKeyboardButton(text="✏️ Редактировать", callback_data="prompt:edit"),
        ],
        [
            InlineKeyboardButton(text="📄 Текст", callback_data="prompt:text"),
            InlineKeyboardButton(text="🗑 Удалить", callback_data="prompt:delete"),
        ],
    ])


def _first_prompt_keyboard() -> InlineKeyboardMarkup:
    """Кнопки Да/Нет для добавления первого промпта."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="➕ Да, добавить", callback_data="first_prompt:yes"),
            InlineKeyboardButton(text="❌ Нет", callback_data="first_prompt:no"),
        ],
    ])


# ── Callback-хендлер для кнопок промптов ─────────────────────

@dp.callback_query(lambda c: c.data and c.data.startswith("prompt:"))
async def prompt_buttons_callback(callback: CallbackQuery, state: FSMContext):
    """Обрабатывает нажатия кнопок промптов."""
    action = callback.data.split(":", 1)[1]
    await callback.answer()  # убираем "часики" на кнопке
    message = callback.message

    if action == "add":
        await message.answer("📝 Введите **тему** нового промпта:", parse_mode=ParseMode.MARKDOWN)
        await state.set_state(AddPromptState.topic)

    elif action == "edit":
        prompts = _load_prompts()
        if not prompts:
            await message.answer(
                "📭 Промптов нет. Добавить первый?", 
                reply_markup=_first_prompt_keyboard(),
            )
            return
        topics = "\n".join(f"• {t}" for t in sorted(prompts.keys()))
        await message.answer(
            f"📝 **Редактирование промпта**\n\n"
            f"Доступные промпты:\n{topics}\n\n"
            "Введите **тему** или **номер** промпта для редактирования:",
            parse_mode=ParseMode.MARKDOWN,
        )
        await state.set_state(EditPromptState.topic)

    elif action == "text":
        prompts = _load_prompts()
        if not prompts:
            await message.answer(
                "📭 Промптов нет. Добавить первый?", 
                reply_markup=_first_prompt_keyboard(),
            )
            return
        topics = "\n".join(f"• {t}" for t in sorted(prompts.keys()))
        await message.answer(
            f"📜 **Доступные промпты:**\n{topics}\n\n"
            "Введите **тему** или **номер** промпта:",
            parse_mode=ParseMode.MARKDOWN,
        )
        await state.set_state(GetPromptState.topic)

    elif action == "delete":
        prompts = _load_prompts()
        if not prompts:
            await message.answer(
                "📭 Промптов нет. Добавить первый?", 
                reply_markup=_first_prompt_keyboard(),
            )
            return
        topics = "\n".join(f"• {t}" for t in sorted(prompts.keys()))
        await message.answer(
            f"🗑 **Удаление промпта**\n\n"
            f"Доступные промпты:\n{topics}\n\n"
            "Введите **тему** или **номер** промпта для удаления:",
            parse_mode=ParseMode.MARKDOWN,
        )
        await state.set_state(DeletePromptState.topic)


# ── Callback-хендлер для кнопок Да/Нет (первый промпт) ────────

@dp.callback_query(lambda c: c.data and c.data.startswith("first_prompt:"))
async def first_prompt_callback(callback: CallbackQuery, state: FSMContext):
    """Обрабатывает нажатия кнопок "Да, добавить" / "Нет" при пустом списке промптов."""
    action = callback.data.split(":", 1)[1]
    await callback.answer()
    message = callback.message

    # Удаляем сообщение с кнопками, чтобы нельзя было нажать повторно
    try:
        await message.delete()
    except Exception:
        pass

    if action == "yes":
        await message.answer("📝 Введите **тему** нового промпта:", parse_mode=ParseMode.MARKDOWN)
        await state.set_state(AddPromptState.topic)
    else:
        await message.answer("❌ Хорошо. Если захотите — напишите `/add_prompt`")
        await state.clear()


# ── FSM: Состояния диалогов ─────────────────────────────────

class AddPromptState(StatesGroup):
    topic = State()
    text = State()


class GetPromptState(StatesGroup):
    topic = State()


class DeletePromptState(StatesGroup):
    topic = State()


class AskAddFirstPrompt(StatesGroup):
    """Спрашивает, хочет ли пользователь добавить первый промпт."""
    waiting = State()


class EditPromptState(StatesGroup):
    topic = State()
    text = State()


class SetupState(StatesGroup):
    email = State()
    server = State()
    login = State()
    password = State()


class AiSetupState(StatesGroup):
    provider = State()
    api_key = State()
    model = State()


AI_PROVIDERS = {
    "openrouter": {
        "label": "OpenRouter",
        "endpoint": "https://openrouter.ai/api/v1",
        "hint_model": "deepseek/deepseek-v4-flash",
    },
    "hermes": {
        "label": "Hermes / Nous",
        "endpoint": "https://openrouter.ai/api/v1",
        "hint_model": "nousresearch/hermes-4",
    },
    "openai": {
        "label": "OpenAI",
        "endpoint": "https://api.openai.com/v1",
        "hint_model": "gpt-4o",
    },
}

AI_PROVIDER_EMOJI = {
    "openrouter": "🟣",
    "hermes": "🟢",
    "openai": "🔵",
}


def _ai_provider_keyboard() -> InlineKeyboardMarkup:
    """Клавиатура выбора AI-провайдера."""
    kb = []
    for key, info in AI_PROVIDERS.items():
        emoji = AI_PROVIDER_EMOJI.get(key, "⚙️")
        kb.append([
            InlineKeyboardButton(
                text=f"{emoji} {info['label']}",
                callback_data=f"ai_provider:{key}"
            )
        ])
    kb.append([
        InlineKeyboardButton(text="⚙️ Другое (свой вариант)", callback_data="ai_provider:custom")
    ])
    return InlineKeyboardMarkup(inline_keyboard=kb)


# ── Клавиатура Саммари ────────────────────────────────────────

def _get_item_button(idx: int, display: str) -> InlineKeyboardMarkup | None:
    """Возвращает inline-клавиатуру с одной кнопкой для пункта списка."""
    prompts = _load_prompts()
    if not prompts:
        return None

    matched_prompt = None
    for topic in prompts:
        if display.lower().startswith(topic.lower()):
            matched_prompt = topic
            break

    if matched_prompt:
        return InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(
                text=f"🟢 Саммари #{idx}",
                callback_data=f"summary:{idx}:{matched_prompt}"
            )
        ]])
    else:
        return InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(
                text=f"🟡 Выбрать промпт #{idx}",
                callback_data=f"choose_prompt:{idx}"
            )
        ]])


# ── Callback-хендлер для кнопки Саммари ─────────────────────

@dp.callback_query(lambda c: c.data and c.data.startswith("summary:"))
async def summary_callback(callback: CallbackQuery, state: FSMContext):
    """Обрабатывает нажатие кнопки Саммари.
    Формат callback_data: summary:IDX:PROMPT_TOPIC"""
    parts = callback.data.split(":", 2)
    if len(parts) < 3:
        await callback.answer("❌ Ошибка данных", show_alert=True)
        return
    _, idx_str, prompt_topic = parts
    idx = int(idx_str) - 1  # 0-based
    await callback.answer()

    user_id = callback.from_user.id

    # Загружаем из кеша
    items = _load_notes_cache(user_id)
    if idx < 0 or idx >= len(items):
        await callback.message.answer("❌ Конспект устарел. Запросите /list заново.")
        return

    _dt, display, txt_content = items[idx]

    # Загружаем промпт
    prompts = _load_prompts()
    prompt_text = prompts.get(prompt_topic, "")
    if not prompt_text:
        await callback.message.answer(f"❌ Промпт «{prompt_topic}» не найден.")
        return

    if not txt_content:
        await callback.message.answer("❌ В письме не найден текст конспекта (txt-вложение).")
        return

    # Показываем статус
    status_msg = await callback.message.answer(
        f"⏳ Обрабатываю «{display}» через нейросеть...",
        parse_mode=ParseMode.MARKDOWN,
    )

    # Вызываем AI
    system_prompt = prompt_text
    user_text = f"Конспект встречи: «{display}»\n\n{txt_content}"
    result = await call_ai(user_id, system_prompt, user_text)

    # Удаляем статус
    try:
        await status_msg.delete()
    except Exception:
        pass

    # Выводим результат
    header = f"🧠 **Саммари: {display}**\n\n---\n\n"
    full_text = header + result

    if len(full_text) <= 4000:
        await callback.message.answer(full_text, parse_mode=ParseMode.MARKDOWN)
    else:
        # Разбиваем на части
        await callback.message.answer(header, parse_mode=ParseMode.MARKDOWN)
        for i in range(0, len(result), 3500):
            await callback.message.answer(result[i:i + 3500])


@dp.callback_query(lambda c: c.data and c.data.startswith("choose_prompt:"))
async def choose_prompt_callback(callback: CallbackQuery, state: FSMContext):
    """Обрабатывает нажатие кнопки Выбрать промпт."""
    parts = callback.data.split(":", 1)
    if len(parts) < 2:
        await callback.answer("❌ Ошибка данных", show_alert=True)
        return
    idx = int(parts[1]) - 1
    await callback.answer()

    user_id = callback.from_user.id
    items = _load_notes_cache(user_id)
    if idx < 0 or idx >= len(items):
        await callback.message.answer("❌ Конспект устарел. Запросите /list заново.")
        return

    _dt, display, _txt = items[idx]
    await callback.message.answer(
        f"📝 Для конспекта «{display}» не найден подходящий промпт.\n\n"
        f"Создайте промпт с названием, которое совпадает с началом строки:\n"
        f"📌 `/add_prompt` → тема: `{display.split()[0] if display.split() else display}` → текст промпта\n\n"
        f"Или используйте `/prompt` для управления промптами.",
        parse_mode=ParseMode.MARKDOWN,
    )


# ── Команда /prompt ─────────────────────────────────────────

@dp.message(Command("prompt", "промпт", "промпты"))
async def cmd_list_prompts(message: Message, state: FSMContext, command: CommandObject):
    """Выводит список всех промптов и команды управления.
       Поддерживает подкоманды: /prompt add, edit, text, delete"""

    # Проверяем подкоманды
    if command.args:
        sub = command.args.strip().lower()
        if sub == "add":
            await message.answer(
                "📝 Введите **тему** нового промпта:",
                parse_mode=ParseMode.MARKDOWN,
            )
            await state.set_state(AddPromptState.topic)
            return
        elif sub == "edit":
            prompts = _load_prompts()
            if not prompts:
                await message.answer(
                    "📭 Промптов пока нет. Добавить первый?",
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=_first_prompt_keyboard(),
                )
                await state.set_state(AskAddFirstPrompt.waiting)
                return
            sorted_topics = sorted(prompts.keys())
            topics = "\n".join(f"• {t}" for t in sorted_topics)
            await message.answer(
                f"📝 **Редактирование промпта**\n\n"
                f"Доступные промпты:\n{topics}\n\n"
                "Введите **тему** или **номер** промпта для редактирования:",
                parse_mode=ParseMode.MARKDOWN,
            )
            await state.set_state(EditPromptState.topic)
            return
        elif sub == "text":
            prompts = _load_prompts()
            if not prompts:
                await message.answer(
                    "📭 Промптов пока нет. Добавить первый?",
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=_first_prompt_keyboard(),
                )
                await state.set_state(AskAddFirstPrompt.waiting)
                return
            sorted_topics = sorted(prompts.keys())
            topics = "\n".join(f"• {t}" for t in sorted_topics)
            await message.answer(
                f"📜 **Доступные промпты:**\n{topics}\n\n"
                "Введите **тему** или **номер** промпта:",
                parse_mode=ParseMode.MARKDOWN,
            )
            await state.set_state(GetPromptState.topic)
            return
        elif sub == "delete":
            prompts = _load_prompts()
            if not prompts:
                await message.answer(
                    "📭 Промптов пока нет. Добавить первый?",
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=_first_prompt_keyboard(),
                )
                await state.set_state(AskAddFirstPrompt.waiting)
                return
            sorted_topics = sorted(prompts.keys())
            topics = "\n".join(f"• {t}" for t in sorted_topics)
            await message.answer(
                f"🗑 **Удаление промпта**\n\n"
                f"Доступные промпты:\n{topics}\n\n"
                "Введите **тему** или **номер** промпта для удаления:",
                parse_mode=ParseMode.MARKDOWN,
            )
            await state.set_state(DeletePromptState.topic)
            return

    prompts = _load_prompts()
    if not prompts:
        await message.answer(
            "📭 Промптов пока нет. Добавить первый?",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=_first_prompt_keyboard(),
        )
        await state.set_state(AskAddFirstPrompt.waiting)
        return
    text = _format_prompt_list()
    await message.answer(text, parse_mode=ParseMode.MARKDOWN, reply_markup=_prompt_keyboard())


# ── Команда /add_prompt ──────────────────────────────────────

@dp.message(AskAddFirstPrompt.waiting)
async def ask_add_first_prompt(message: Message, state: FSMContext):
    """Обрабатывает ответ Да/Нет на предложение добавить первый промпт."""
    answer = message.text.strip().lower()
    if answer in ("да", "yes", "lf", "д", "y"):
        await state.set_state(AddPromptState.topic)
        await message.answer(
            "📝 Введите **тему** нового промпта:",
            parse_mode=ParseMode.MARKDOWN,
        )
    else:
        await message.answer("❌ Хорошо. Если захотите — напишите `/add_prompt`")
        await state.clear()


@dp.message(Command("add_prompt", "prompt_add"))
async def cmd_add_prompt_start(message: Message, state: FSMContext):
    """Начинает диалог добавления промпта: запрашивает тему."""
    await message.answer(
        "📝 Введите **тему** нового промпта:",
        parse_mode=ParseMode.MARKDOWN,
    )
    await state.set_state(AddPromptState.topic)


@dp.message(AddPromptState.topic)
async def add_prompt_topic(message: Message, state: FSMContext):
    """Сохраняет тему и запрашивает текст промпта."""
    topic = message.text.strip()
    if not topic:
        await message.answer("⚠️ Тема не может быть пустой. Введите тему:")
        return

    # Проверяем, нет ли уже такой темы
    prompts = _load_prompts()
    if topic in prompts:
        await message.answer(
            f"⚠️ Промпт с темой «{topic}» уже существует!\n"
            f"Текущий текст:\n`{prompts[topic][:200]}`\n\n"
            "Введите **другую** тему:",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    await state.update_data(topic=topic)
    await message.answer(
        f"✅ Тема «{topic}» принята.\n\n"
        "Теперь введите **текст** промпта:",
        parse_mode=ParseMode.MARKDOWN,
    )
    await state.set_state(AddPromptState.text)


@dp.message(AddPromptState.text)
async def add_prompt_text(message: Message, state: FSMContext):
    """Сохраняет текст промпта и завершает диалог."""
    text = message.text.strip()
    if not text:
        await message.answer("⚠️ Текст промпта не может быть пустым. Введите текст:")
        return

    data = await state.get_data()
    topic = data["topic"]

    prompts = _load_prompts()
    prompts[topic] = text
    _save_prompts(prompts)

    await message.answer(
        f"🧠 **Промпт «{topic}» добавлен в память.**\n\n"
        f"📄 Длина: {len(text)} символов",
        parse_mode=ParseMode.MARKDOWN,
    )
    await state.clear()

    # Автоматически показываем обновлённый список
    text = _format_prompt_list()
    await message.answer(text, parse_mode=ParseMode.MARKDOWN, reply_markup=_prompt_keyboard())


# ── Команда /text_prompt ─────────────────────────────────────

@dp.message(Command("text_prompt", "prompt_text"))
async def cmd_text_prompt_start(message: Message, state: FSMContext):
    """
    /text_prompt <номер> — сразу показывает текст промпта по номеру
    /text_prompt — диалог: спрашивает тему
    """
    prompts = _load_prompts()
    if not prompts:
        await message.answer(
            "📭 Промптов пока нет. Добавить первый?",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=_first_prompt_keyboard(),
        )
        await state.set_state(AskAddFirstPrompt.waiting)
        return

    sorted_topics = sorted(prompts.keys())

    # Пробуем распарсить номер из команды
    parts = message.text.strip().split(maxsplit=1)
    if len(parts) == 2:
        arg = parts[1].strip()
        try:
            idx = int(arg) - 1  # конвертируем в 0-based
            if 0 <= idx < len(sorted_topics):
                topic = sorted_topics[idx]
                text = prompts[topic]
                full = f"📌 **{topic}**\n\n{text}"
                if len(full) <= MAX_MSG_LEN:
                    await message.answer(full, parse_mode=ParseMode.MARKDOWN)
                else:
                    await message.answer(
                        f"📌 **{topic}**\n\n{text[:MAX_MSG_LEN - 50]}\n\n"
                        f"_…текст слишком длинный, сохранён в боте_",
                        parse_mode=ParseMode.MARKDOWN,
                    )
                return
            else:
                await message.answer(
                    f"⚠️ Неверный номер. Введите число от 1 до {len(sorted_topics)}."
                )
                return
        except ValueError:
            # Аргумент — не число, возможно тема
            if arg in prompts:
                text = prompts[arg]
                full = f"📌 **{arg}**\n\n{text}"
                if len(full) <= MAX_MSG_LEN:
                    await message.answer(full, parse_mode=ParseMode.MARKDOWN)
                else:
                    await message.answer(
                        f"📌 **{arg}**\n\n{text[:MAX_MSG_LEN - 50]}\n\n"
                        f"_…текст слишком длинный, сохранён в боте_",
                        parse_mode=ParseMode.MARKDOWN,
                    )
                return

    # Без аргументов — запускаем FSM диалог
    topics = "\n".join(f"• {t}" for t in sorted_topics)
    await message.answer(
        f"📜 **Доступные промпты:**\n{topics}\n\n"
        "Введите **тему** или **номер** промпта:",
        parse_mode=ParseMode.MARKDOWN,
    )
    await state.set_state(GetPromptState.topic)


@dp.message(GetPromptState.topic)
async def text_prompt_show(message: Message, state: FSMContext):
    """Показывает текст промпта по теме или номеру."""
    arg = message.text.strip()
    prompts = _load_prompts()
    if not prompts:
        await message.answer("📭 Промпты закончились. Сначала добавьте через /add_prompt")
        await state.clear()
        return

    sorted_topics = sorted(prompts.keys())
    topic: str | None = None

    # Пробуем номер
    try:
        idx = int(arg) - 1
        if 0 <= idx < len(sorted_topics):
            topic = sorted_topics[idx]
    except ValueError:
        pass

    # Пробуем тему
    if topic is None and arg in prompts:
        topic = arg

    if topic is None:
        topics = "\n".join(f"• {t}" for t in sorted_topics)
        await message.answer(
            f"⚠️ Не найдено. Доступные промпты:\n{topics}\n\n"
            "Введите **тему** или **номер**:",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    text = prompts[topic]
    full = f"📌 **{topic}**\n\n{text}"
    if len(full) <= MAX_MSG_LEN:
        await message.answer(full, parse_mode=ParseMode.MARKDOWN)
    else:
        await message.answer(
            f"📌 **{topic}**\n\n{text[:MAX_MSG_LEN - 50]}\n\n"
            f"_…текст слишком длинный, сохранён в боте_",
            parse_mode=ParseMode.MARKDOWN,
        )

    await state.clear()


# ── Команда /delete_prompt ───────────────────────────────────

@dp.message(Command("delete_prompt", "prompt_delete"))
async def cmd_delete_prompt_start(message: Message, state: FSMContext):
    """
    /delete_prompt <номер> — сразу удаляет промпт по номеру
    /delete_prompt — диалог: спрашивает тему
    """
    prompts = _load_prompts()
    if not prompts:
        await message.answer(
            "📭 Промптов пока нет. Добавить первый?",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=_first_prompt_keyboard(),
        )
        await state.set_state(AskAddFirstPrompt.waiting)
        return

    sorted_topics = sorted(prompts.keys())

    # Пробуем распарсить номер из аргумента
    parts = message.text.strip().split(maxsplit=1)
    if len(parts) == 2:
        arg = parts[1].strip()
        try:
            idx = int(arg) - 1
            if 0 <= idx < len(sorted_topics):
                topic = sorted_topics[idx]
                del prompts[topic]
                _save_prompts(prompts)
                await message.answer(
                    f"🗑 **Промпт «{topic}» удалён.**",
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=_prompt_keyboard(),
                )
                return
            else:
                await message.answer(
                    f"⚠️ Неверный номер. Введите от 1 до {len(sorted_topics)}."
                )
                return
        except ValueError:
            # Аргумент — не число, возможно тема
            if arg in prompts:
                del prompts[arg]
                _save_prompts(prompts)
                await message.answer(
                    f"🗑 **Промпт «{arg}» удалён.**",
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=_prompt_keyboard(),
                )
                return

    # Без аргументов — FSM диалог
    topics = "\n".join(f"• {t}" for t in sorted_topics)
    await message.answer(
        f"🗑 **Удаление промпта**\n\n"
        f"Доступные промпты:\n{topics}\n\n"
        "Введите **тему** или **номер** промпта для удаления:",
        parse_mode=ParseMode.MARKDOWN,
    )
    await state.set_state(DeletePromptState.topic)


@dp.message(DeletePromptState.topic)
async def delete_prompt_confirm(message: Message, state: FSMContext):
    """Удаляет промпт по теме или номеру."""
    arg = message.text.strip()
    prompts = _load_prompts()
    if not prompts:
        await message.answer("📭 Промпты закончились.")
        await state.clear()
        return

    sorted_topics = sorted(prompts.keys())
    topic: str | None = None

    # Пробуем номер
    try:
        idx = int(arg) - 1
        if 0 <= idx < len(sorted_topics):
            topic = sorted_topics[idx]
    except ValueError:
        pass

    # Пробуем тему
    if topic is None and arg in prompts:
        topic = arg

    if topic is None:
        topics = "\n".join(f"• {t}" for t in sorted_topics)
        await message.answer(
            f"⚠️ Не найдено. Доступные промпты:\n{topics}\n\n"
            "Введите **тему** или **номер**:",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    del prompts[topic]
    _save_prompts(prompts)

    await message.answer(
        f"🗑 **Промпт «{topic}» удалён.**",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=_prompt_keyboard(),
    )
    await state.clear()

    # Автоматически показываем обновлённый список
    text = _format_prompt_list()
    await message.answer(text, parse_mode=ParseMode.MARKDOWN, reply_markup=_prompt_keyboard())


# ── Команда /edit_prompt ─────────────────────────────────────

@dp.message(Command("edit_prompt", "prompt_edit"))
async def cmd_edit_prompt_start(message: Message, state: FSMContext):
    """
    /edit_prompt <номер> — сразу запрашивает новый текст для промпта по номеру
    /edit_prompt — диалог: спрашивает тему
    """
    prompts = _load_prompts()
    if not prompts:
        await message.answer(
            "📭 Промптов пока нет. Добавить первый?",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=_first_prompt_keyboard(),
        )
        await state.set_state(AskAddFirstPrompt.waiting)
        return

    sorted_topics = sorted(prompts.keys())

    # Пробуем распарсить номер из аргумента
    parts = message.text.strip().split(maxsplit=1)
    if len(parts) == 2:
        arg = parts[1].strip()
        try:
            idx = int(arg) - 1
            if 0 <= idx < len(sorted_topics):
                topic = sorted_topics[idx]
                await state.update_data(topic=topic)
                await message.answer(
                    f"📝 Редактирование промпта **«{topic}»**\n\n"
                    f"Текущий текст:\n`{prompts[topic][:200]}`\n\n"
                    "Введите **новый текст** промпта:",
                    parse_mode=ParseMode.MARKDOWN,
                )
                await state.set_state(EditPromptState.text)
                return
            else:
                await message.answer(
                    f"⚠️ Неверный номер. Введите от 1 до {len(sorted_topics)}."
                )
                return
        except ValueError:
            if arg in prompts:
                await state.update_data(topic=arg)
                await message.answer(
                    f"📝 Редактирование промпта **«{arg}»**\n\n"
                    f"Текущий текст:\n`{prompts[arg][:200]}`\n\n"
                    "Введите **новый текст** промпта:",
                    parse_mode=ParseMode.MARKDOWN,
                )
                await state.set_state(EditPromptState.text)
                return

    # Без аргументов — спрашиваем тему
    topics = "\n".join(f"• {t}" for t in sorted_topics)
    await message.answer(
        f"📝 **Редактирование промпта**\n\n"
        f"Доступные промпты:\n{topics}\n\n"
        "Введите **тему** или **номер** промпта для редактирования:",
        parse_mode=ParseMode.MARKDOWN,
    )
    await state.set_state(EditPromptState.topic)


@dp.message(EditPromptState.topic)
async def edit_prompt_topic(message: Message, state: FSMContext):
    """Принимает тему/номер промпта и запрашивает новый текст."""
    arg = message.text.strip()
    prompts = _load_prompts()
    if not prompts:
        await message.answer("📭 Промпты закончились.")
        await state.clear()
        return

    sorted_topics = sorted(prompts.keys())
    topic: str | None = None

    # Пробуем номер
    try:
        idx = int(arg) - 1
        if 0 <= idx < len(sorted_topics):
            topic = sorted_topics[idx]
    except ValueError:
        pass

    # Пробуем тему
    if topic is None and arg in prompts:
        topic = arg

    if topic is None:
        topics = "\n".join(f"• {t}" for t in sorted_topics)
        await message.answer(
            f"⚠️ Не найдено. Доступные промпты:\n{topics}\n\n"
            "Введите **тему** или **номер**:",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    await state.update_data(topic=topic)
    await message.answer(
        f"📝 Редактирование промпта **«{topic}»**\n\n"
        f"Текущий текст:\n`{prompts[topic][:200]}`\n\n"
        "Введите **новый текст** промпта:",
        parse_mode=ParseMode.MARKDOWN,
    )
    await state.set_state(EditPromptState.text)


@dp.message(EditPromptState.text)
async def edit_prompt_text(message: Message, state: FSMContext):
    """Сохраняет новый текст промпта."""
    new_text = message.text.strip()
    if not new_text:
        await message.answer("⚠️ Текст не может быть пустым. Введите текст:")
        return

    data = await state.get_data()
    topic = data["topic"]

    prompts = _load_prompts()
    prompts[topic] = new_text
    _save_prompts(prompts)

    await message.answer(
        f"✅ **Промпт обновлён!**\n\n"
        f"📌 Тема: `{topic}`\n"
        f"📄 Длина: {len(new_text)} символов",
        parse_mode=ParseMode.MARKDOWN,
    )
    await state.clear()

    # Автоматически показываем обновлённый список
    text = _format_prompt_list()
    await message.answer(text, parse_mode=ParseMode.MARKDOWN, reply_markup=_prompt_keyboard())


# ── Команда /init — сброс и повторная настройка ────────────────

async def _start_init(message: Message, state: FSMContext):
    """Очищает настройки пользователя и запускает 4-шаговую настройку заново."""
    user_id = message.from_user.id

    # Очищаем старые настройки
    users = _load_users()
    if str(user_id) in users:
        del users[str(user_id)]
        _save_users(users)
        logger.info("Настройки пользователя %s сброшены", user_id)

    await message.answer(
        "🔄 **Настройки сброшены.**\n\n"
        "📧 Шаг 1/4. Введите адрес электронной почты:",
        parse_mode=ParseMode.MARKDOWN,
    )
    await state.set_state(SetupState.email)


def _help_text() -> str:
    """Возвращает полный текст справки."""
    return (
        "📚 **Полная справка по командам**\n\n"
        "🔧 **── Настройка ──**\n\n"
        "`/setup`\n"
        "  Настройка IMAP-подключения к почте.\n"
        "  4 шага: Email → IMAP-сервер → Логин → Пароль.\n"
        "  После ввода проверяет подключение и сохраняет.\n\n"
        "`/init` или `/setup init`\n"
        "  **Сбросить** все настройки почты и настроить\n"
        "  заново. Полностью очищает email, IMAP, логин\n"
        "  и пароль, после чего запускает 4 шага настройки.\n\n"
        "  При первом запуске бота команда `/start`\n"
        "  автоматически запускает `/init`.\n\n"
        "`/setup_ai` или `/setup_llm`\n"
        "  Настройка подключения к нейросети.\n"
        "  Выбор провайдера (OpenRouter, Hermes/Nous,\n"
        "  OpenAI, свой вариант) → API Key → Модель.\n"
        "  Нужно для функции «Саммари».\n\n"
        "📬 **── Конспекты встреч ──**\n\n"
        "`/list` или `/конспекты`\n"
        "  Показать только **непрочитанные** конспекты.\n"
        "  Флаг UNSEEN НЕ снимается — письма остаются\n"
        "  непрочитанными в почте.\n\n"
        "`/list_all` или `/все_конспекты`\n"
        "  Показать **все** конспекты за последние 7 дней.\n\n"
        "🤖 **── Промпты (для нейросети) ──**\n\n"
        "`/prompt` или `/промпты`\n"
        "  Список всех сохранённых промптов\n"
        "  с инлайн-кнопками управления.\n"
        "  Подкоманды: `/prompt add`, `edit`, `text`, `delete`\n\n"
        "`/add_prompt` или `/prompt_add` или `/prompt add`\n"
        "  Добавить новый промпт.\n"
        "  Бот спросит тему и текст.\n\n"
        "`/edit_prompt` или `/prompt_edit` или `/prompt edit`\n"
        "  Редактировать промпт.\n"
        "  Варианты:\n"
        "  • `/edit_prompt 1` — по номеру\n"
        "  • `/edit_prompt` — диалог\n\n"
        "`/text_prompt` или `/prompt_text` или `/prompt text`\n"
        "  Показать текст промпта.\n"
        "  Варианты:\n"
        "  • `/text_prompt 1` — по номеру\n"
        "  • `/text_prompt` — диалог\n\n"
        "`/delete_prompt` или `/prompt_delete` или `/prompt delete`\n"
        "  Удалить промпт.\n"
        "  Варианты:\n"
        "  • `/delete_prompt 1` — по номеру\n"
        "  • `/delete_prompt` — диалог\n\n"
        "ℹ️ **── Прочее ──**\n\n"
        "`/help` или `/htlp` или `/помощь`\n"
        "  Эта справка.\n\n"
        "`/start`\n"
        "  Краткое приветствие."
    )


@dp.message(Command("init"))
async def cmd_init(message: Message, state: FSMContext):
    """Сбрасывает все настройки почты и запускает настройку заново."""
    await _start_init(message, state)


@dp.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    user_id = message.from_user.id
    config = get_user_config(user_id)
    if not config:
        await message.answer(
            "👋 Привет! Я бот для конспектов встреч.\n\n"
            "Похоже, почта ещё не настроена. "
            "Давайте настроим подключение.",
            parse_mode=ParseMode.MARKDOWN,
        )
        await _start_init(message, state)
        return
    await message.answer(
        "👋 Привет! Я бот для конспектов встреч и промптов.\n\n"
        "Напиши `/help` — покажу все команды.",
        parse_mode=ParseMode.MARKDOWN,
    )


@dp.message(Command("help", "htlp", "помощь", "команды"))
async def cmd_help(message: Message):
    """Показывает полную справку по всем командам бота."""
    await message.answer(_help_text(), parse_mode=ParseMode.MARKDOWN)


@dp.message(Command("get_notes", "list", "конспекты", "конспект"))
async def cmd_get_notes(message: Message):
    user = message.from_user
    logger.info("UNSEEN запрос от @%s", user.username or user.id)

    # Проверяем настройки пользователя
    if not get_user_config(user.id):
        await message.answer(
            "❌ Почта ещё не настроена.\n\n"
            "Используйте `/setup` чтобы указать:\n"
            "1️⃣ Адрес электронной почты\n"
            "2️⃣ IMAP-сервер\n"
            "3️⃣ Логин\n"
            "4️⃣ Пароль приложения",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    sent = await message.answer("🔍 Ищу новые конспекты...")

    try:
        _, items = fetch_notes(user.id)
    except imaplib.IMAP4.error as e:
        await sent.edit_text(f"❌ Ошибка IMAP: `{e}`")
        return
    except Exception as e:
        await sent.edit_text(f"❌ Ошибка: `{e}`")
        import traceback
        traceback.print_exc()
        return

    if not items:
        await sent.delete()
        return

    await sent.delete()

    # Сохраняем в кеш для кнопки Саммари
    _save_notes_cache(user.id, items)

    total = len(items)
    await message.answer(f"📋 **Новые конспекты встреч** — всего {total}", parse_mode=ParseMode.MARKDOWN)

    for idx, (dt, display, _txt) in enumerate(items, 1):
        date_str = dt.strftime("%d.%m.%Y %H:%M")
        text = f"**{idx}.** {display}\n📅 {date_str}"
        button = _get_item_button(idx, display)
        await message.answer(text, parse_mode=ParseMode.MARKDOWN, reply_markup=button)


@dp.message(Command("list_all", "все_конспекты"))
async def cmd_list_all(message: Message):
    user = message.from_user
    logger.info("ALL WEEK запрос от @%s", user.username or user.id)

    if not get_user_config(user.id):
        await message.answer(
            "❌ Почта ещё не настроена.\n\n"
            "Используйте `/setup` чтобы указать:\n"
            "1️⃣ Адрес электронной почты\n"
            "2️⃣ IMAP-сервер\n"
            "3️⃣ Логин\n"
            "4️⃣ Пароль приложения",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    sent = await message.answer("🔍 Загружаю конспекты за неделю...")

    try:
        _, items = fetch_notes_last_week(user.id)
    except imaplib.IMAP4.error as e:
        await sent.edit_text(f"❌ Ошибка IMAP: `{e}`")
        return
    except Exception as e:
        await sent.edit_text(f"❌ Ошибка: `{e}`")
        import traceback
        traceback.print_exc()
        return

    if not items:
        await sent.delete()
        return

    await sent.delete()

    _save_notes_cache(user.id, items)

    total = len(items)
    await message.answer(f"📋 **Конспекты встреч за неделю** — всего {total}", parse_mode=ParseMode.MARKDOWN)

    for idx, (dt, display, _txt) in enumerate(items, 1):
        date_str = dt.strftime("%d.%m.%Y %H:%M")
        text = f"**{idx}.** {display}\n📅 {date_str}"
        button = _get_item_button(idx, display)
        await message.answer(text, parse_mode=ParseMode.MARKDOWN, reply_markup=button)


# ── Команда /setup ────────────────────────────────────────────

@dp.message(Command("setup"))
async def cmd_setup_start(message: Message, state: FSMContext, command: CommandObject):
    """Начинает настройку почты. /setup init — сброс и настройка заново."""

    # Проверяем аргумент /setup init
    if command.args and command.args.strip().lower() == "init":
        await _start_init(message, state)
        return

    config = get_user_config(message.from_user.id)
    current = config["email"] if config else "не задан"
    await message.answer(
        "📧 **Настройка подключения к почте**\n\n"
        f"**Email** ({current}):\n"
        "Введите адрес электронной почты:",
        parse_mode=ParseMode.MARKDOWN,
    )
    await state.set_state(SetupState.email)


@dp.message(SetupState.email)
async def setup_email(message: Message, state: FSMContext):
    """Сохраняет email (или оставляет старый при пустом вводе) и запрашивает IMAP-сервер."""
    email = message.text.strip()
    if not email:
        # Пустой ввод — оставляем старое значение
        config = get_user_config(message.from_user.id)
        if config and config.get("email"):
            email = config["email"]
        else:
            await message.answer("⚠️ Email не может быть пустым. Введите адрес электронной почты:")
            return
    elif "@" not in email:
        await message.answer("⚠️ Введите корректный email (например: ivan@example.ru):")
        return
    await state.update_data(email=email)

    config = get_user_config(message.from_user.id)
    current = config["server"] if config else "не задан"
    await message.answer(
        f"✅ Email: `{email}`\n\n"
        f"**IMAP-сервер** ({current}):\n"
        "Введите адрес IMAP-сервера\n"
        "(например: `imap.yandex.ru`, `imap.mail.ru`):\n\n"
        "💡 _Оставьте пустым, чтобы оставить текущее значение_",
        parse_mode=ParseMode.MARKDOWN,
    )
    await state.set_state(SetupState.server)


@dp.message(SetupState.server)
async def setup_server(message: Message, state: FSMContext):
    """Сохраняет сервер (или оставляет старый при пустом вводе) и запрашивает логин."""
    server = message.text.strip()
    if not server or "." not in server:
        config = get_user_config(message.from_user.id)
        if config and config.get("server"):
            server = config["server"]
        else:
            await message.answer("⚠️ Введите корректный IMAP-сервер (например: imap.yandex.ru):\n\n💡 _Оставьте пустым, чтобы оставить текущее значение_")
            return
    await state.update_data(server=server)

    config = get_user_config(message.from_user.id)
    current = config["login"] if config else "не задан"
    await message.answer(
        f"✅ Сервер: `{server}`\n\n"
        f"**Логин** ({current}):\n"
        "Введите логин для подключения к IMAP\n"
        "(обычно совпадает с email):\n\n"
        "💡 _Оставьте пустым, чтобы оставить текущее значение_",
        parse_mode=ParseMode.MARKDOWN,
    )
    await state.set_state(SetupState.login)


@dp.message(SetupState.login)
async def setup_login(message: Message, state: FSMContext):
    """Сохраняет логин (или оставляет старый при пустом вводе) и запрашивает пароль."""
    login = message.text.strip()
    if not login:
        config = get_user_config(message.from_user.id)
        if config and config.get("login"):
            login = config["login"]
        else:
            await message.answer("⚠️ Логин не может быть пустым. Введите логин:\n\n💡 _Оставьте пустым, чтобы оставить текущее значение_")
            return
    await state.update_data(login=login)

    config = get_user_config(message.from_user.id)
    current = "••••••••" if config and config.get("password") else "не задан"
    await message.answer(
        f"✅ Логин: `{login}`\n\n"
        f"**Пароль** ({current}):\n"
        "Введите пароль приложения для IMAP\n"
        "(для Яндекса — создайте пароль приложения в настройках почты):\n\n"
        "💡 _Оставьте пустым, чтобы оставить текущее значение_",
        parse_mode=ParseMode.MARKDOWN,
    )
    await state.set_state(SetupState.password)


@dp.message(SetupState.password)
async def setup_password(message: Message, state: FSMContext):
    """Сохраняет пароль (или оставляет старый при пустом вводе), проверяет подключение и завершает."""
    password = message.text.strip()
    if not password:
        config = get_user_config(message.from_user.id)
        if config and config.get("password"):
            password = config["password"]
        else:
            await message.answer("⚠️ Пароль не может быть пустым. Введите пароль:\n\n💡 _Оставьте пустым, чтобы оставить текущее значение_")
            return

    data = await state.get_data()
    email = data.get("email", "")
    server = data.get("server", "")
    login = data.get("login", "")
    user_id = message.from_user.id

    # Проверяем подключение
    status = await message.answer("🔄 Проверяю подключение...")
    try:
        test_server = imaplib.IMAP4_SSL(server, 993)
        test_server.login(login, password)
        test_server.select("INBOX")
        test_server.close()
        test_server.logout()
    except imaplib.IMAP4.error as e:
        await status.edit_text(
            f"❌ Ошибка подключения: `{e}`\n\n"
            "Попробуйте ещё раз:\n"
            "• Убедитесь, что IMAP включён в настройках почты\n"
            "• Проверьте логин и пароль\n\n"
            "Начните заново: `/setup`",
            parse_mode=ParseMode.MARKDOWN,
        )
        await state.clear()
        return
    except Exception as e:
        await status.edit_text(
            f"❌ Ошибка: `{e}`\n\nНачните заново: `/setup`",
            parse_mode=ParseMode.MARKDOWN,
        )
        await state.clear()
        return

    # Сохраняем настройки
    save_user_config(user_id, email, server, login, password)
    await state.clear()

    await status.edit_text(
        f"✅ **Настройка завершена!**\n\n"
        f"📧 Email: `{email}`\n"
        f"🖥  IMAP: `{server}:993`\n"
        f"🔑 Логин: `{login}`\n\n"
        "Теперь можно использовать команды:",
        parse_mode=ParseMode.MARKDOWN,
    )

    # Автоматически показываем справку
    await message.answer(_help_text(), parse_mode=ParseMode.MARKDOWN)

    # Спрашиваем, хочет ли пользователь настроить AI
    await message.answer(
        "🤖 Хотите настроить подключение к нейросети?\n"
        "Это нужно для функции «Саммари».",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="🤖 Да, настроить AI", callback_data="ai_after_setup:yes"),
                InlineKeyboardButton(text="🚫 Нет", callback_data="ai_after_setup:no"),
            ]
        ]),
    )


# ── Кеш конспектов (для кнопки Саммари) ───────────────────

NOTES_CACHE_FILE = Path(__file__).parent / "notes_cache.json"


def _save_notes_cache(user_id: int, items: list):
    """Сохраняет список конспектов с txt-содержимым в кеш."""
    cache = {}
    if NOTES_CACHE_FILE.exists():
        try:
            cache = json.loads(NOTES_CACHE_FILE.read_text(encoding="utf-8"))
        except Exception:
            cache = {}
    # Сериализуем datetime -> str
    serialized = []
    for dt, display, txt in items:
        serialized.append({
            "dt": dt.isoformat() if dt else "",
            "display": display,
            "txt": txt,
        })
    cache[str(user_id)] = serialized
    NOTES_CACHE_FILE.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")


def _load_notes_cache(user_id: int) -> list:
    """Загружает последний кеш конспектов пользователя."""
    if not NOTES_CACHE_FILE.exists():
        return []
    try:
        cache = json.loads(NOTES_CACHE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return []
    serialized = cache.get(str(user_id), [])
    items = []
    for item in serialized:
        dt = datetime.fromisoformat(item["dt"]) if item.get("dt") else datetime.now()
        items.append((dt, item["display"], item["txt"]))
    return items


# ── Универсальная функция вызова нейросети ──────────────────

async def call_ai(user_id: int, system_prompt: str, user_text: str) -> str:
    """Вызывает нейросеть через OpenAI-совместимый API.
    Настройки (endpoint, api_key, model) берутся из users.json для user_id.
    Возвращает ответ ассистента или текст ошибки."""
    ai_config = get_ai_config(user_id)
    if not ai_config:
        return "❌ AI не настроен. Используйте `/setup_ai`"

    endpoint = ai_config.get("endpoint", "").rstrip("/")
    api_key = ai_config.get("api_key", "")
    model = ai_config.get("model", "gpt-4o")

    if not endpoint or not api_key:
        return "❌ AI настроен не полностью. Проверьте endpoint и API key через `/setup_ai`"

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    # Для OpenRouter добавляем заголовок с именем приложения
    if "openrouter" in endpoint.lower():
        headers["HTTP-Referer"] = "https://t.me/hunttech_protocols_bot"
        headers["X-Title"] = "HunttechProtocolsBot"

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_text},
        ],
    }

    try:
        async with httpx.AsyncClient(timeout=120) as client:
            response = await client.post(
                f"{endpoint}/chat/completions",
                headers=headers,
                json=payload,
            )
            if response.status_code != 200:
                return f"❌ Ошибка API ({response.status_code}): {response.text[:500]}"
            result = response.json()
            return result["choices"][0]["message"]["content"]
    except httpx.TimeoutException:
        return "❌ Таймаут: нейросеть не ответила за 120 секунд"
    except Exception as e:
        return f"❌ Ошибка: {e}"


# ── Команда /setup_ai — настройка нейросети ────────────────

@dp.callback_query(lambda c: c.data and c.data.startswith("ai_after_setup:"))
async def ai_after_setup_callback(callback: CallbackQuery, state: FSMContext):
    """Обрабатывает ответ на вопрос «настроить AI?» после IMAP setup."""
    action = callback.data.split(":", 1)[1]
    await callback.answer()
    try:
        await callback.message.delete()
    except Exception:
        pass

    if action == "yes":
        await callback.message.answer(
            "🤖 **Настройка нейросети**\n\n"
            "Выберите провайдера:",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=_ai_provider_keyboard(),
        )
        await state.set_state(AiSetupState.provider)
    else:
        await callback.message.answer("🚫 Хорошо. Если захотите — `/setup_ai`")

@dp.message(Command("setup_ai", "setup_llm"))
async def cmd_setup_ai(message: Message, state: FSMContext):
    """Начинает настройку AI: выбор провайдера."""
    await message.answer(
        "🤖 **Настройка нейросети**\n\n"
        "Выберите провайдера:",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=_ai_provider_keyboard(),
    )
    await state.set_state(AiSetupState.provider)


@dp.callback_query(lambda c: c.data and c.data.startswith("ai_provider:"))
async def ai_provider_callback(callback: CallbackQuery, state: FSMContext):
    """Обрабатывает выбор провайдера AI."""
    provider_key = callback.data.split(":", 1)[1]
    await callback.answer()
    await callback.message.delete()

    if provider_key == "custom":
        await state.update_data(ai_endpoint="", ai_provider_label="Свой вариант")
        await callback.message.answer(
            "🔗 Введите **API Endpoint URL**:\n\n"
            "Например: `https://api.openai.com/v1`\n"
            "или `https://openrouter.ai/api/v1`",
            parse_mode=ParseMode.MARKDOWN,
        )
        await state.set_state(AiSetupState.api_key)
        return

    provider = AI_PROVIDERS.get(provider_key)
    if not provider:
        await callback.message.answer("❌ Неизвестный провайдер.")
        await state.clear()
        return

    await state.update_data(
        ai_endpoint=provider["endpoint"],
        ai_provider_label=provider["label"],
    )

    await callback.message.answer(
        f"{AI_PROVIDER_EMOJI.get(provider_key, '')} **{provider['label']}**\n"
        f"🔗 Endpoint: `{provider['endpoint']}`\n\n"
        "🔑 Введите **API Key**:\n"
        "(ключ от провайдера, для OpenRouter — ваш ключ OpenRouter):",
        parse_mode=ParseMode.MARKDOWN,
    )
    await state.set_state(AiSetupState.api_key)


@dp.message(AiSetupState.api_key)
async def ai_setup_apikey(message: Message, state: FSMContext):
    """Сохраняет API key и запрашивает модель."""
    api_key = message.text.strip()
    if not api_key:
        await message.answer("⚠️ API Key не может быть пустым. Введите ключ:")
        return

    data = await state.get_data()
    endpoint = data.get("ai_endpoint", "")

    # Если endpoint не задан (custom), спрашиваем его
    if not endpoint:
        await state.update_data(ai_api_key=api_key)
        await message.answer(
            "🔗 Введите **API Endpoint URL**:\n\n"
            "Например: `https://api.openai.com/v1`",
            parse_mode=ParseMode.MARKDOWN,
        )
        # Перенаправляем на тот же state, но с флагом что api_key уже есть
        await state.set_state(AiSetupState.model)
        # Сохраняем флаг что нужно спросить endpoint
        await state.update_data(_need_endpoint=True)
        return

    await state.update_data(ai_api_key=api_key)
    await state.update_data(_need_endpoint=False)

    # Показываем подсказку по модели
    provider_label = data.get("ai_provider_label", "")
    hint = data.get("_hint_model", "gpt-4o")
    await message.answer(
        f"📝 Введите **название модели**:\n\n"
        f"Например: `{hint}`\n"
        f"• Для OpenRouter: `deepseek/deepseek-v4-flash`\n"
        f"• Для OpenAI: `gpt-4o`, `gpt-4o-mini`\n"
        f"• Любая другая модель провайдера",
        parse_mode=ParseMode.MARKDOWN,
    )
    await state.set_state(AiSetupState.model)


@dp.message(AiSetupState.model)
async def ai_setup_model(message: Message, state: FSMContext):
    """Сохраняет модель, завершает настройку AI."""
    model = message.text.strip()
    if not model:
        await message.answer("⚠️ Название модели не может быть пустым. Введите модель:")
        return

    data = await state.get_data()
    api_key = data.get("ai_api_key", "")
    endpoint = data.get("ai_endpoint", "")

    # Если endpoint ещё не задан (custom путь)
    need_endpoint = data.get("_need_endpoint", False)
    if need_endpoint:
        # Текущее сообщение — это endpoint, а не модель
        endpoint = model
        model = ""
        await state.update_data(ai_endpoint=endpoint, _need_endpoint=False)
        await message.answer(
            "📝 Введите **название модели**:\n\n"
            "Например: `gpt-4o`, `deepseek-chat`, `claude-sonnet-4`",
            parse_mode=ParseMode.MARKDOWN,
        )
        await state.set_state(AiSetupState.model)
        return

    if not endpoint:
        # Крайний случай — не должно произойти
        await message.answer("❌ Ошибка: не указан endpoint. Начните заново: `/setup_ai`")
        await state.clear()
        return

    user_id = message.from_user.id
    save_ai_config(user_id, endpoint, api_key, model)
    await state.clear()

    provider_label = data.get("ai_provider_label", "Пользовательский")
    await message.answer(
        f"✅ **AI-настройки сохранены!**\n\n"
        f"🧩 Провайдер: `{provider_label}`\n"
        f"🔗 Endpoint: `{endpoint}`\n"
        f"📝 Модель: `{model}`\n\n"
        "Теперь кнопка «Саммари» будет работать!",
        parse_mode=ParseMode.MARKDOWN,
    )


# ── Неизвестные команды ─────────────────────────────────────

@dp.message()
async def unknown_command(message: Message):
    """Ловит любые сообщения, начинающиеся с /, которые не обработали другие хендлеры."""
    if message.text and message.text.startswith("/") and len(message.text) > 1:
        logger.info("Неизвестная команда: %s", message.text.split()[0])
        await message.answer(
            "Неизвестная команда. Введите /help для подсказки."
        )


# ── Запуск ─────────────────────────────────────────────────────

async def main():
    logger.info("🤖 Бот конспектов встреч запускается...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())