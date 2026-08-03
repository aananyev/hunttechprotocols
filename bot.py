#!/usr/bin/env python3
"""
🤖 HuntTech Protocols Bot
=================================
Бизнес-назначение: 
Автоматизация рутины рекрутингового агентства — достаём из почты 
«Конспекты встреч» (ежедневные совещания Совета директоров IT-компании),
извлекаем текстовые отчёты и даём одним нажатием кнопки сгенерировать
структурированное саммари по заданному шаблону (промпту) через нейросеть.

Основан на aiogram 3.x.
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
import io
import tempfile
import zipfile
import xml.etree.ElementTree as ET

# ── Access control (из hunttech-bot-common) ───────────────
from hunttech_bot_common.users import AccessManager
from hunttech_bot_common.users.ptb import get_bot_access_path
from hunttech_bot_common.telegram import escape_md_simple
from hunttech_bot_common.email import (
    test_email_connections, format_email_config,
    validate_email, validate_hostname, validate_password,
)

try:
    import db  # Модуль PostgreSQL (включается при наличии DB_HOST в .env)
except ImportError:
    import types
    db = types.ModuleType('db')
    db.DB_ENABLED = False
    db.DB_POOL = None
    db.ADMIN_USER_ID = 0
    db.apply_config = lambda *a, **kw: (False, "db module not available")
    db.init_db_pool = lambda *a, **kw: None
    db.close_db_pool = lambda *a, **kw: None
    db.ensure_tables = lambda *a, **kw: None
    db.save_meeting = lambda *a, **kw: None
    db.get_meeting_by_msg_id = lambda *a, **kw: None
    db.save_summary = lambda *a, **kw: None
    db.get_recent_meetings = lambda *a, **kw: []
    db.get_summaries_for_meeting = lambda *a, **kw: []
    db.get_stats = lambda *a, **kw: {}
    logging.getLogger("bot").info("📦 Модуль PostgreSQL не подключён — работаю без БД")


# ── Логирование ──────────────────────────────────────────────────
# Логи нужны, чтобы отслеживать работу бота в фоне — кто и когда
# запрашивал конспекты, были ли ошибки IMAP/AI.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("bot")

from aiogram import Bot, Dispatcher
from aiogram.filters import Command, CommandObject
from aiogram.types import BotCommand, Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from aiogram.enums import ParseMode

import os
from dotenv import load_dotenv

# ── Конфигурация ──────────────────────────────────────────────

load_dotenv()
# TG_TOKEN — ключ от @BotFather, даёт боту доступ к Telegram API.
# Хранится в .env (не в git!), чтобы не светить секрет в репозитории.
TG_TOKEN = os.getenv("TG_TOKEN", "") or exit("❌ TG_TOKEN не задан! Положи токен в .env")

# MASTER_ADMIN_ID — Telegram user ID владельца бота (главный администратор).
# Администратор имеет полный доступ и может управлять пользователями.
# Если не задан — используется db.ADMIN_USER_ID (если db доступен).
MASTER_ADMIN_ID = int(os.getenv("MASTER_ADMIN_ID", "0")) or 0

# SUBJECT_FILTER — бизнес-правило: мы ищем только письма с темой
# "Конспект встречи", которые секретарь Совета директоров отправляет
# после каждого ежедневного совещания.
SUBJECT_FILTER = "Конспект встречи"

# MAX_MSG_LEN — Telegram не принимает сообщения длиннее ~4096 символов.
# Оставляем запас 300 символов под Markdown-разметку, чтобы биться
# в лимит на длинных ответах нейросети.
MAX_MSG_LEN = 3800

# USERS_FILE — хранилище учётных данных пользователей (email, пароль,
# AI-ключи). Каждый пользователь бота подключает свою почту.
# Файл исключён из git — в нём пароли приложений от IMAP и API-ключи.
USERS_FILE = Path(__file__).parent / "users.json"

# NEW_COMMS_FILE stores conspect IDs already shown via /list new.
NEW_COMMS_FILE = Path(__file__).parent / "new_comms.json"


# ═══════════════════════════════════════════════════════════════════
# БЛОК ХРАНЕНИЯ ДАННЫХ
# ═══════════════════════════════════════════════════════════════════
# Бизнес-требование: бот многопользовательский. Каждый рекрутер
# агентства может подключить свой почтовый ящик и свои AI-ключи.
# Данные хранятся в JSON-файлах (не БД, потому что пользователей 
# пока единицы, и администрировать проще через файлы).


# ── Хранилище пользовательских настроек почты ─────────────────

# ---- Storage for /list new already-shown IDs ---------------

def _load_new_comms() -> dict:
    """Load already-shown conspect IDs: {user_id: [msg_id1, ...]}"""
    if not NEW_COMMS_FILE.exists():
        return {}
    try:
        with open(NEW_COMMS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except (json.JSONDecodeError, IOError):
        pass
    return {}


def _save_new_comms(data: dict):
    """Save shown conspect IDs."""
    with open(NEW_COMMS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _mark_new_comms_shown(user_id: int, msg_ids: list[str]):
    """Mark conspect IDs as already shown via /list new."""
    data = _load_new_comms()
    key = str(user_id)
    if key not in data:
        data[key] = []
    existing = set(data[key])
    for mid in msg_ids:
        if mid not in existing:
            data[key].append(mid)
            existing.add(mid)
    _save_new_comms(data)


def _get_new_comms_for_user(user_id: int) -> set:
    """Return set of already-shown conspect IDs for a user."""
    data = _load_new_comms()
    return set(data.get(str(user_id), []))


# ── Трекер отправленных cron-уведомлений ──────────────────
# Отдельный файл, не влияющий на /list new.

NOTIFIED_FILE = Path(__file__).parent / "notified_comms.json"


def _load_notified() -> dict:
    """Load notified conspect IDs: {user_id: [uid, ...]}"""
    if not NOTIFIED_FILE.exists():
        return {}
    try:
        with open(NOTIFIED_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except (json.JSONDecodeError, IOError):
        pass
    return {}


def _save_notified(data: dict):
    with open(NOTIFIED_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _get_notified_comms_for_user(user_id: int) -> set:
    """Return set of conspect IDs already notified via cron."""
    data = _load_notified()
    return set(data.get(str(user_id), []))


def _mark_notified(user_id: int, msg_ids: list[str]):
    """Mark conspect IDs as already notified (cron), without affecting /list new."""
    data = _load_notified()
    key = str(user_id)
    if key not in data:
        data[key] = []
    existing = set(data[key])
    for mid in msg_ids:
        if mid not in existing:
            data[key].append(mid)
            existing.add(mid)
    _save_notified(data)


def _load_users() -> dict:
    """Загружает учётные записи пользователей: {user_id: {email, server, port, password, ai?}}"""
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
    """Сохраняет учётные записи пользователей."""
    with open(USERS_FILE, "w", encoding="utf-8") as f:
        json.dump(users, f, ensure_ascii=False, indent=2)


def get_user_config(user_id: int) -> dict | None:
    """Возвращает настройки IMAP/AI конкретного пользователя.
       Бизнес-правило: без настроек нельзя /list и /list_all."""
    users = _load_users()
    return users.get(str(user_id))


def save_user_config(user_id: int, email: str, server: str, login: str, password: str):
    """Сохраняет IMAP-настройки пользователя (email, IMAP-сервер, логин, пароль).
       Вызывается после успешной проверки подключения в /setup."""
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
    """Сохраняет AI-настройки пользователя (endpoint, api_key, модель).
       Вызывается после /setup_ai. API-ключ хранится рядом с IMAP-паролем."""
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
    """Возвращает AI-настройки пользователя или None.
       Без AI-конфига кнопка «Саммари» не работает."""
    config = get_user_config(user_id)
    if config and "ai" in config:
        return config["ai"]
    return None


# ═══════════════════════════════════════════════════════════════════
# ФУНКЦИИ ДЛЯ YANDEX WIKI
# ═══════════════════════════════════════════════════════════════════
# Яндекс Вики — корпоративный вики-сервис из состава Яндекс 360 для бизнеса.
# Бизнес-правило: после того как нейросеть сгенерировала саммари совещания,
# его можно опубликовать как страницу в Яндекс Вики. Тогда все члены Совета
# директоров видят утверждённые протоколы в едином корпоративном хранилище,
# а не только в Telegram-чате.
#
# Аутентификация: IAM-токен Яндекc Облака через JWT (авторизованный ключ сервисного аккаунта).
# Токен передаётся в заголовке Authorization: Bearer ***
# Авторизованный ключ: Yandex Cloud Console → Сервисные аккаунты → Ключи → Авторизованный ключ
# Роль: wiki.editor или wiki.admin
# API endpoint: https://api.wiki.yandex.net/v1/


def save_wiki_config(user_id: int, authorized_key: str, org_id: str = "", mode: str = "", folder: str = ""):
    """Сохраняет настройки Яндекс Вики: авторизованный ключ сервисного аккаунта и ID организации.
       Бизнес-правило: authorized_key — это JSON с полями id, service_account_id, private_key.
       IAM-токен получается свежим через JWT при каждом запросе к Wiki API.
       org_id сохраняется только если передан непустой; если не передан — сохраняется старый.
       mode: 'auto' (автопубликация), 'button' (по кнопке), 'off' (выкл) — по умолчанию 'off'.
       folder: slug раздела Wiki, куда публиковать страницы (например, 'hr_meetings')."""
    users = _load_users()
    key = str(user_id)
    if key not in users:
        users[key] = {}
    old_wiki = users[key].get("wiki", {})
    existing_org_id = old_wiki.get("org_id", "")
    users[key]["wiki"] = {
        "authorized_key": authorized_key,
        "org_id": org_id or existing_org_id,
        "mode": mode or old_wiki.get("mode", "off"),
        "folder": folder or old_wiki.get("folder", ""),
    }
    # Очищаем старые поля, если были
    users[key]["wiki"].pop("api_key", None)
    users[key]["wiki"].pop("client_id", None)
    users[key]["wiki"].pop("client_secret", None)
    users[key]["wiki"].pop("oauth_token", None)
    _save_users(users)


def get_wiki_config(user_id: int) -> dict | None:
    """Возвращает настройки Яндекс Вики или None.
       Без настроек wiki команды /wiki_test, /setup wiki test и публикация не работают."""
    config = get_user_config(user_id)
    if config and "wiki" in config:
        return config["wiki"]
    return None


def get_wiki_mode(user_id: int) -> str:
    """Возвращает режим публикации в Wiki: 'auto', 'button' или 'off' (по умолчанию)."""
    wiki_config = get_wiki_config(user_id)
    if wiki_config:
        return wiki_config.get("mode", "off")
    return "off"


# ── Хранилище настроек PostgreSQL ──────────────────────────


def save_db_config(user_id: int, host: str, port: int, name: str, user: str, password: str):
    """Сохраняет настройки подключения к PostgreSQL в users.json.
       Только пользователь-администратор может настраивать БД."""
    users = _load_users()
    key = str(user_id)
    if key not in users:
        users[key] = {}
    users[key]["db"] = {
        "host": host,
        "port": port,
        "name": name,
        "user": user,
        "password": password,
    }
    _save_users(users)


def get_db_config(user_id: int) -> dict | None:
    """Возвращает настройки PostgreSQL или None."""
    config = get_user_config(user_id)
    if config and "db" in config:
        return config["db"]
    return None


async def _get_wiki_token(wiki_config: dict) -> str | None:
    """Получает токен для Wiki API из конфига.
       Если есть authorized_key — создаёт JWT и получает IAM-токен.
       Если есть client_id/client_secret (старый формат) — получает OAuth-токен (fallback).
       Возвращает токен (str) или None."""
    auth_key = wiki_config.get("authorized_key")
    if auth_key:
        # Пробуем распарсить как JSON и создать JWT
        import json
        try:
            key_json = json.loads(auth_key) if isinstance(auth_key, str) else auth_key
            jwt_token = _create_jwt_from_authorized_key(key_json)
            if jwt_token:
                return await _get_yandex_iam_token_from_jwt(jwt_token)
            else:
                logger.error("Не удалось создать JWT из authorized_key")
                return None
        except json.JSONDecodeError as e:
            logger.error(f"authorized_key не является валидным JSON: {e}")
            return None

    # Старый формат (API-ключ — не работает с IAM REST API, но пробуем для совместимости)
    api_key = wiki_config.get("api_key")
    if api_key:
        logger.warning("API-ключ не поддерживается напрямую, нужен авторизованный ключ")
        return None

    # Старый формат (OAuth через ClientID/ClientSecret) — fallback
    client_id = wiki_config.get("client_id")
    client_secret = wiki_config.get("client_secret")
    if client_id and client_secret:
        logger.warning(
            "Используется устаревший OAuth-формат для wiki. "
            "Рекомендуется перенастроить через /setup wiki"
        )
        return await _get_yandex_oauth_token(client_id, client_secret)

    return None


# ═══════════════════════════════════════════════════════════════════
# БЛОК ОБРАБОТКИ ПОЧТЫ (IMAP)
# ═══════════════════════════════════════════════════════════════════
# Бизнес-процесс: каждое утро после совещания Совета директоров 
# секретарь высылает текстовую расшифровку встречи в виде .txt-файла.
# Бот забирает эти письма, не помечая их прочитанными (UNSEEN),
# чтобы пользователь мог перепроверить в веб-интерфейсе почты.


# ── Хелперы ───────────────────────────────────────────────────

def decode_mime_header(header_value: str) -> str:
    """
    Декодирует MIME-заголовки (QP, Base64).
    Бизнес-правило: темы писем могут содержать кириллицу, закодированную 
    в =?UTF-8?B?...?=, поэтому нужна правильная раскодировка.
    """
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
    """Извлекает дату письма из Date-заголовка.
       Бизнес-правило: дата важна для сортировки — показываем 
       сначала самые свежие конспекты."""
    date_str = msg.get("Date")
    if not date_str:
        return None
    try:
        return parsedate_to_datetime(date_str)
    except (ValueError, TypeError):
        return None


def extract_txt_attachments(msg) -> list[str]:
    """
    Рекурсивно обходит все части письма и собирает текстовое содержимое.
    Бизнес-правило: конспект встречи приходит как .txt-вложение.
    Если вложения нет, но есть text/plain в теле — тоже берём.
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

def _email_config_error(config: dict | None) -> str | None:
    """Проверить полноту email-конфига. Возвращает None если всё ок,
    иначе сообщение об ошибке (неполная настройка)."""
    if not config:
        return "❌ Почта не настроена. Используйте /setup для настройки."
    missing = [k for k in ("email", "server", "login", "password") if not config.get(k)]
    if missing:
        return ("❌ Настройка почты неполная (не хватает: "
                + ", ".join(missing)
                + "). Используйте /setup для настройки.")
    return None


def _connect_imap(config: dict) -> imaplib.IMAP4_SSL:
    """
    Подключается к IMAP-серверу по настройкам пользователя.
    Бизнес-правило: логин для IMAP часто совпадает с email, 
    но бывает отличается (например, логин — часть email до @).
    """
    imap_login = config.get("login") or config.get("email", "")
    logger.info("Подключение к IMAP %s (login: %s)...", config.get("server"), imap_login)
    server = imaplib.IMAP4_SSL(config["server"], config.get("port", 993))
    server.login(imap_login, config["password"])
    server.select("INBOX")
    logger.info("Успешно подключились к IMAP")
    return server


def _filter_and_extract(server, msg_ids: list[bytes]) -> list[tuple]:
    """
    Фильтрует письма по теме SUBJECT_FILTER и извлекает txt-содержимое.
    
    Бизнес-правило — КРИТИЧЕСКИ ВАЖНОЕ:
    Письма НЕ ДОЛЖНЫ помечаться как прочитанные. Используем BODY.PEEK[]
    вместо RFC822, плюс явно снимаем флаг \\Seen — двойная защита.
    
    Возвращает список кортежей:
        (datetime, display, txt_content, email_msg_id, email_from)
    """
    matched: list[tuple] = []

    for msg_id in msg_ids:
        # BODY.PEEK[] — единственный правильный способ читать письмо
        # не снимая флаг UNSEEN.
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

        txts = extract_txt_attachments(msg)
        txt_content = "\n\n---\n\n".join(txts) if txts else ""

        email_msg_id = msg.get("Message-ID", "") or ""
        email_from = decode_mime_header(msg.get("From", ""))
        imap_msg_id = msg_id.decode() if isinstance(msg_id, bytes) else str(msg_id)

        matched.append((dt, display, txt_content, email_msg_id, email_from, imap_msg_id))

        try:
            server.store(msg_id, "-FLAGS", "(\\Seen)")
        except Exception:
            pass

    # Сортируем: самые свежие сверху — рекрутеру важно видеть
    # последний созвон первым.
    matched.sort(key=lambda x: x[0], reverse=True)
    return matched


def _format_list(matched: list, title: str) -> str:
    """Форматирует список конспектов в текст для отправки в Telegram.
       Каждый элемент: (datetime, display_text, txt_content, ...)."""
    lines: list[str] = []
    lines.append(f"{title} — всего {len(matched)}")
    lines.append("")
    for idx, item in enumerate(matched, 1):
        lines.append(f"{idx}. {item[1]}")
    return "\n".join(lines)


# ── Функции выборки писем ─────────────────────────────────────

def fetch_notes(user_id: int) -> tuple[str, list]:
    """
    Ищет НЕПРОЧИТАННЫЕ письма с темой "Конспект встречи".
    
    Бизнес-процесс: рекрутер приходит утром, нажимает /list,
    видит только то, что пришло после его последнего захода 
    (UNSEEN). Письма остаются непрочитанными — можно вернуться
    и перепроверить в веб-почте.
    """
    config = get_user_config(user_id)
    err = _email_config_error(config)
    if err:
        return (err, [])
    assert config is not None  # _email_config_error вернул бы ошибку при None
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
        # Сохраняем конспекты в PostgreSQL (асинхронно, fire-and-forget)
        try:
            loop = asyncio.get_event_loop()
            for item in matched:
                dt, disp, txt, email_msg_id, frm, imap_msg_id = item[0], item[1], item[2], item[3], item[4], item[5]
                if email_msg_id:
                    loop.create_task(
                        db.save_meeting(email_msg_id, user_id, frm, f"{SUBJECT_FILTER}: {disp}", dt, txt, imap_msg_id)
                    )
        except RuntimeError:
            pass
        return (_format_list(matched, "📋 **Новые конспекты встреч**"), matched)
    finally:
        server.close()
        server.logout()


def fetch_new_notes(user_id: int) -> tuple[str, list]:
    """
    Return conspects not yet shown via /list new.
    IDs are saved in new_comms.json after display.
    """
    config = get_user_config(user_id)
    err = _email_config_error(config)
    if err:
        return (err, [])
    assert config is not None
    server = _connect_imap(config)
    try:
        typ, data = server.search(None, "UNSEEN")
        all_ids = data[0].split() if data[0] else []
        if not all_ids:
            return ("No unread emails.", [])
        matched = _filter_and_extract(server, all_ids)
        if not matched:
            return ("No new meeting notes.", [])
        seen = _get_new_comms_for_user(user_id)
        new_items = []
        for item in matched:
            dt, display, txt, email_msg_id, email_from, imap_msg_id = item[0], item[1], item[2], item[3], item[4], item[5]
            uid = f"{dt.timestamp()}:{display}"
            if uid not in seen:
                new_items.append(item)
        if not new_items:
            return ("No new conspects since last check.", [])
        # Сохраняем в PostgreSQL (асинхронно, fire-and-forget)
        try:
            loop = asyncio.get_event_loop()
            for item in new_items:
                dt, disp, txt, email_msg_id, frm, imap_msg_id = item[0], item[1], item[2], item[3], item[4], item[5]
                if email_msg_id:
                    loop.create_task(
                        db.save_meeting(email_msg_id, user_id, frm, f"{SUBJECT_FILTER}: {disp}", dt, txt, imap_msg_id)
                    )
        except RuntimeError:
            pass
        return (_format_list(new_items, "New conspects (first time)"), new_items)
    finally:
        server.close()
        server.logout()


def fetch_notes_last_week(user_id: int) -> tuple[str, list]:
    """
    Ищет ВСЕ конспекты за последние 7 дней (не только непрочитанные).
    
    Бизнес-назначение: если рекрутер хочет пересмотреть, что было
    на неделе — неважно, читал он это или нет.
    """
    config = get_user_config(user_id)
    err = _email_config_error(config)
    if err:
        return (err, [])
    assert config is not None
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
        matched = [item for item in matched if item[0].timestamp() >= week_ago]
        if not matched:
            return ("📭 За последнюю неделю нет конспектов встреч.", [])
        return (_format_list(matched, "📋 **Конспекты встреч за неделю**"), matched)
    finally:
        server.close()
        server.logout()


# ═══════════════════════════════════════════════════════════════════
# ФУНКЦИИ ДЛЯ YANDEX WIKI API
# ═══════════════════════════════════════════════════════════════════
# Бизнес-логика: Яндекс Вики — это корпоративная база знаний.
# Мы используем её как хранилище утверждённых протоколов совещаний.
# Сгенерированное нейросетью саммари можно опубликовать как страницу,
# чтобы все члены команды имели к нему доступ.
#
# API: https://api.wiki.yandex.net/v1/
# Аутентификация: IAM-токен Яндекс Облака или OAuth Яндекс ID.
# Токен передаётся в заголовке Authorization: Bearer <token>.


WIKI_API_BASE = "https://api.wiki.yandex.net/v1"


async def _get_yandex_oauth_token(client_id: str, client_secret: str) -> str | None:
    """
    Получает OAuth-токен Яндекс ID через Client Credentials flow.
    
    POST https://oauth.yandex.ru/token
    grant_type=client_credentials
    
    Пробует комбинации: body params / Basic Auth, со scope / без scope.
    
    Возвращает access_token или None при ошибке.
    """
    scopes_to_try = [None, "wiki:read_write", "cloud_api", "wiki_api"]
    
    for use_basic in [False, True]:
        for scope in scopes_to_try:
            method_desc = "Basic Auth" if use_basic else "body params"
            scope_desc = f"scope={scope}" if scope else "без scope"
            label = f"{method_desc}/{scope_desc}"

            try:
                kwargs = {
                    "data": {"grant_type": "client_credentials"},
                    "headers": {"Content-Type": "application/x-www-form-urlencoded"},
                }

                if scope:
                    kwargs["data"]["scope"] = scope

                if use_basic:
                    import base64
                    auth_str = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
                    kwargs["headers"]["Authorization"] = f"Basic {auth_str}"
                else:
                    kwargs["data"]["client_id"] = client_id
                    kwargs["data"]["client_secret"] = client_secret

                async with httpx.AsyncClient(timeout=15) as client:
                    resp = await client.post("https://oauth.yandex.ru/token", **kwargs)

                if resp.status_code == 200:
                    data = resp.json()
                    token = data.get("access_token")
                    if token:
                        logger.info(f"✓ OAuth-токен получен ({label})")
                        return token
                    else:
                        logger.warning(
                            f"Статус 200, но access_token отсутствует "
                            f"({label}): {resp.text[:300]}"
                        )
                        return None

                # Логируем детали ошибки
                logger.warning(
                    f"✗ OAuth ({label}) — HTTP {resp.status_code}: "
                    f"{resp.text[:300]}"
                )

            except Exception as e:
                logger.error(
                    f"✗ OAuth ({label}) — "
                    f"исключение: {type(e).__name__}: {e}"
                )

    logger.error("Все комбинации аутентификации не сработали.")
    return None


def _create_jwt_from_authorized_key(key_json: dict) -> str | None:
    """
    Создаёт JWT для аутентификации сервисного аккаунта Яндекc Облака
    из авторизованного ключа (Authorized Key).

    Алгоритм: PS256 (RSASSA-PSS с SHA-256)
    JWT payload: {"iss": service_account_id, "aud": "...", "iat": ..., "exp": ...}
    """
    try:
        import jwt as pyjwt
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import rsa, padding
        from cryptography.hazmat.primitives import hashes
        import time

        service_account_id = key_json.get("service_account_id")
        key_id = key_json.get("id")
        private_key_pem = key_json.get("private_key")

        if not all([service_account_id, key_id, private_key_pem]):
            logger.error("JWT: отсутствуют поля в ключе (service_account_id, id, private_key)")
            return None

        # Загружаем приватный ключ
        private_key = serialization.load_pem_private_key(
            private_key_pem.encode(),
            password=None,
        )

        # Создаём JWT
        now = int(time.time())
        payload = {
            "iss": service_account_id,
            "aud": "https://iam.api.cloud.yandex.net/iam/v1/tokens",
            "iat": now,
            "exp": now + 3600,  # 1 час
        }

        headers = {
            "alg": "PS256",
            "kid": key_id,
            "typ": "JWT",
        }

        token = pyjwt.encode(payload, private_key, algorithm="PS256", headers=headers)
        logger.info(f"✓ JWT создан для сервисного аккаунта {service_account_id[:10]}...")
        return token

    except Exception as e:
        logger.error(f"✗ Ошибка создания JWT: {type(e).__name__}: {e}")
        return None


async def _get_yandex_iam_token_from_jwt(jwt_token: str) -> str | None:
    """
    Обменивает JWT на IAM-токен Яндекc Облака.

    POST https://iam.api.cloud.yandex.net/iam/v1/tokens
    {"jwt": "..."}
    """
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                "https://iam.api.cloud.yandex.net/iam/v1/tokens",
                json={"jwt": jwt_token},
            )

        debug_msg = f"IAM из JWT: HTTP {resp.status_code} — {resp.text[:500]}"
        logger.warning(debug_msg)
        with open("/tmp/iam_debug.log", "a") as f:
            f.write(f"{debug_msg}\n")

        if resp.status_code == 200:
            data = resp.json()
            token = data.get("iamToken")
            if token:
                logger.info("✓ IAM-токен получен через JWT")
                return token
            else:
                logger.warning(f"Статус 200, но iamToken отсутствует: {resp.text[:300]}")
                return None

        return None

    except Exception as e:
        err_msg = f"✗ IAM из JWT: исключение {type(e).__name__}: {e}"
        logger.error(err_msg)
        with open("/tmp/iam_debug.log", "a") as f:
            f.write(f"{err_msg}\n")
        return None


async def _test_wiki_connection(iam_token: str, org_id: str = "") -> str:
    """
    Проверяет подключение к Яндекс Вики API.
    
    Бизнес-правило: перед публикацией страницы нужно убедиться,
    что API-доступ работает. Тест получает информацию о текущем
    пользователе и список последних страниц.
    
    Возвращает отформатированный отчёт с результатами проверки.
    Если проверка не удалась — возвращает строку с ❌.
    """
    headers = {
        "Authorization": f"Bearer {iam_token}",
        "Content-Type": "application/json",
    }
    # Если указана организация — добавляем заголовок
    if org_id:
        headers["X-Org-ID"] = org_id

    report_parts = []
    all_ok = True

    # ── Тест 1: получение информации о пользователе ────────────────
    # Проверяем, что токен валиден и API отвечает.
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(f"{WIKI_API_BASE}/users/me", headers=headers)
            if resp.status_code == 200:
                user_data = resp.json()
                login = user_data.get("login", "неизвестно")
                email = user_data.get("email", "не указан")
                report_parts.append(
                    f"✅ **Пользователь:** `{login}` ({email})"
                )
            elif resp.status_code == 401:
                report_parts.append("❌ **Ошибка авторизации (401):** IAM-токен недействителен или истёк.")
                all_ok = False
            else:
                report_parts.append(f"❌ **Ошибка API ({resp.status_code}):** {escape_md_simple(resp.text[:200])}")
                all_ok = False
    except httpx.TimeoutException:
        report_parts.append("❌ **Таймаут:** Яндекс Вики не ответил за 15 секунд.")
        all_ok = False
    except Exception as e:
        report_parts.append(f"❌ **Ошибка подключения:** {escape_md_simple(e)}")
        all_ok = False

    # ── Тест 2: список страниц (проверяем доступ на чтение) ────────
    # Пробуем получить список кластеров или страниц, чтобы убедиться,
    # что у пользователя есть права на чтение wiki.
    if all_ok:
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(
                    f"{WIKI_API_BASE}/pages",
                    headers=headers,
                    params={"pageSize": 5},
                )
                if resp.status_code == 200:
                    data = resp.json()
                    pages = data.get("pages", [])
                    if pages:
                        report_parts.append(
                            f"✅ **Доступ к страницам:** получено {len(pages)} страниц"
                        )
                        # Показываем примеры страниц
                        for p in pages[:3]:
                            title = p.get("title", "без названия")
                            slug = p.get("slug", "?")
                            report_parts.append(f"   📄 `{title}` (/{slug})")
                    else:
                        report_parts.append("✅ **Доступ к страницам:** есть, но страниц пока нет.")
                elif resp.status_code == 403:
                    report_parts.append("⚠️ **Нет прав на чтение страниц.** Проверьте настройки доступа в Яндекс Вики.")
                else:
                    report_parts.append(f"⚠️ **Не удалось получить страницы:** HTTP {resp.status_code}")
        except Exception as e:
            report_parts.append(f"⚠️ **Ошибка при получении страниц:** {escape_md_simple(e)}")

    # ── Тест 3: информация о кластере (организации) ────────────────
    # Узнаём, к какому кластеру/организации привязан токен.
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(f"{WIKI_API_BASE}/clusters", headers=headers)
            if resp.status_code == 200:
                clusters = resp.json()
                if isinstance(clusters, list) and clusters:
                    for c in clusters:
                        report_parts.append(f"🏢 **Кластер:** `{c.get('id', '?')}` — {c.get('title', '')}")
                elif isinstance(clusters, dict):
                    report_parts.append(f"🏢 **Кластер:** `{clusters.get('id', '?')}`")
            # Не все аккаунты имеют доступ к кластерам — это нормально
    except Exception:
        pass

    # Формируем итоговый отчёт
    if all_ok:
        title = "✅ **Подключение к Яндекс Вики работает!**\n\n"
    else:
        title = "❌ **Подключение к Яндекс Вики НЕ работает.**\n\n"

    return title + "\n".join(report_parts)


async def publish_to_wiki(title: str, content: str, wiki_config: dict) -> tuple[bool, str]:
    """
    Публикует страницу в Яндекс Вики.
    
    Бизнес-правило: после генерации AI-саммари конспекта встречи,
    страница автоматически (или по кнопке) публикуется в корпоративной
    Яндекс Вики, чтобы все члены Совета директоров видели протокол.
    
    API: POST {WIKI_API_BASE}/pages — создаёт новую страницу.
    Если страница с таким title уже существует — создаст дубликат
    (Wiki позволяет страницы с одинаковыми названиями в разных разделах).
    
    Args:
        title: название страницы (например, «Совет директоров 2026-07-10»)
        content: markdown-содержимое страницы
        wiki_config: словарь с authorized_key и org_id
    
    Returns:
        (success: bool, message: str) — результат и ссылка или ошибка
    """
    token = await _get_wiki_token(wiki_config)
    if not token:
        return False, "❌ Не удалось получить IAM-токен для Яндекс Вики."

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    org_id = wiki_config.get("org_id", "")
    if org_id:
        headers["X-Org-ID"] = org_id

    payload = {
        "title": title,
        "content": content,
    }
    # Если указана папка (slug родительского раздела) — добавляем parent
    folder = wiki_config.get("folder", "")
    if folder:
        payload["parent"] = folder

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{WIKI_API_BASE}/pages",
                headers=headers,
                json=payload,
            )
            if resp.status_code in (200, 201):
                data = resp.json()
                page_slug = data.get("slug", "?")
                page_url = f"https://wiki.yandex.ru/{page_slug}"
                if org_id:
                    page_url += f"?orgId={org_id}"
                return True, f"✅ Страница опубликована: {page_url}"
            elif resp.status_code == 401:
                return False, "❌ Ошибка авторизации (401): IAM-токен недействителен."
            elif resp.status_code == 403:
                return False, (
                    "❌ Нет прав на создание страниц (403).\n"
                    "Проверьте, что сервисный аккаунт имеет роль `wiki.editor`."
                )
            else:
                return False, f"❌ Ошибка Wiki API ({resp.status_code}): {resp.text[:300]}"
    except httpx.TimeoutException:
        return False, "❌ Таймаут: Яндекс Вики не ответил за 30 секунд."
    except Exception as e:
        return False, f"❌ Ошибка подключения к Wiki: {e}"


# ═══════════════════════════════════════════════════════════════════
# ФУНКЦИЯ: ПОМЕТИТЬ ПИСЬМО ПРОЧИТАННЫМ В IMAP
# ═══════════════════════════════════════════════════════════════════


def _set_email_read(user_id: int, imap_msg_id: str) -> bool:
    """Помечает письмо по IMAP msg_id как прочитанное (флаг \\Seen).
       После успешной цепочки AI→Wiki→БД — письмо уходит из /list."""
    config = get_user_config(user_id)
    if _email_config_error(config):
        return False
    assert config is not None
    try:
        server = _connect_imap(config)
        try:
            server.store(imap_msg_id.encode(), "+FLAGS", "(\\Seen)")
            logger.info("📩 Письмо %s помечено прочитанным (%s)", imap_msg_id, user_id)
            return True
        finally:
            server.close()
            server.logout()
    except Exception as e:
        logger.error("❌ Пометка письма %s: %s", imap_msg_id, e)
        return False


import json
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from aiogram import Router
from aiogram.types import ReplyKeyboardRemove

bot = Bot(token=TG_TOKEN)
dp = Dispatcher()

# ── Access Manager ─────────────────────────────────────────
# AccessManager управляет тем, какие пользователи Telegram имеют
# доступ к этому боту. Каждый бот имеет свой файл доступа.
_master_admin_id = MASTER_ADMIN_ID or getattr(db, "ADMIN_USER_ID", 0)

# ── Нижнее меню (стандарт HuntTech) ─────────────────────────
# Кнопки ReplyKeyboard ЭКВИВАЛЕНТНЫ командам: нажатие кнопки
# маршрутизируется в тот же хендлер, что и текстовая команда.
SIDE_MENU_BUTTONS = {
    "notes": "📬 Конспекты",
    "prompt": "🤖 Промпты",
    "setup": "🔧 Настройки",
    "help": "❓ Справка",
}
SIDE_MENU_ALIASES: dict[str, str] = {}
for _cmd, _text in SIDE_MENU_BUTTONS.items():
    SIDE_MENU_ALIASES[_text.lower()] = _cmd
    SIDE_MENU_ALIASES[_cmd] = _cmd


def _main_menu_keyboard() -> "ReplyKeyboardMarkup":
    """Нижняя ReplyKeyboard-клавиатура с основными кнопками бота."""
    from aiogram.types import KeyboardButton, ReplyKeyboardMarkup
    btns = [KeyboardButton(text=t) for t in SIDE_MENU_BUTTONS.values()]
    return ReplyKeyboardMarkup(
        keyboard=[
            btns[0:2],   # notes, prompt
            btns[2:4],   # setup, help
        ],
        resize_keyboard=True,
        one_time_keyboard=False,
        input_field_placeholder="Выберите действие или введите команду...",
    )
if not _master_admin_id:
    logger.warning(
        "⚠️ MASTER_ADMIN_ID не задан! "
        "Бот будет работать без контроля доступа. "
        "Укажите MASTER_ADMIN_ID в .env или db.ADMIN_USER_ID."
    )
access_manager = AccessManager(
    data_path=get_bot_access_path("hunttechprotocols"),
    master_admin_id=_master_admin_id,
    bot_name="HuntTech Protocols",
)


# ═══════════════════════════════════════════════════════════════════
# БЛОК ПРОМПТОВ (Шаблоны для нейросети)
# ═══════════════════════════════════════════════════════════════════
# Бизнес-правило: у агентства несколько типов встреч. Каждый тип —
# свой промпт (шаблон саммари). Например, "ежедневный Совет Директоров"
# формирует строгий отчёт по разделам: операционные вопросы, кадры, 
# коммерция, приоритеты. Промпт сопоставляется с конспектом по первому
# слову названия.

PROMPTS_FILE = Path(__file__).parent / "prompts.json"


def _load_prompts() -> dict[str, str]:
    """Загружает промпты пользователя: {тема: текст шаблона}."""
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
    """Сохраняет промпты. Каждый промпт — это system_prompt для нейросети."""
    with open(PROMPTS_FILE, "w", encoding="utf-8") as f:
        json.dump(prompts, f, ensure_ascii=False, indent=2)


def _format_prompt_list() -> str:
    """
    Форматирует список промптов с командами управления для отправки в Telegram.
    Бизнес-правило: показываем пользователю не просто список, а сразу
    кнопки действий — чтобы не пришлось запоминать команды.
    """
    prompts = _load_prompts()
    lines = ["📜 **Мои промпты**"]
    lines.append("")
    if not prompts:
        lines.append("_Промптов пока нет._")
    else:
        for idx, topic in enumerate(sorted(prompts.keys()), 1):
            lines.append(f"{idx}. {escape_md_simple(topic)}")
    lines.append("")
    lines.append("── Управление ──")
    lines.append("/add_prompt    — добавить новый промпт")
    lines.append("/edit_prompt   — редактировать промпт")
    lines.append("/text_prompt   — показать текст промпта")
    lines.append("/delete_prompt — удалить промпт")
    return "\n".join(lines)


def _prompt_keyboard() -> InlineKeyboardMarkup:
    """
    Инлайн-клавиатура для управления промптами.
    Бизнес-правило: кнопки удобнее, чем запоминать команды.
    Пользователь видит список и тут же может нажать «Добавить» или «Удалить».
    """
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
    """
    Кнопки Да/Нет для онбординга: когда промптов ещё нет,
    предлагаем пользователю создать первый.
    Бизнес-правило: пустой список — не тупик, а точка старта.
    """
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="➕ Да, добавить", callback_data="first_prompt:yes"),
            InlineKeyboardButton(text="❌ Нет", callback_data="first_prompt:no"),
        ],
    ])


# ── Callback-хендлер для кнопок промптов ─────────────────────

@dp.callback_query(lambda c: c.data and c.data.startswith("prompt:"))
async def prompt_buttons_callback(callback: CallbackQuery, state: FSMContext):
    """
    Обрабатывает нажатия инлайн-кнопок управления промптами.
    Каждая кнопка запускает соответствующий FSM-диалог.
    """
    action = callback.data.split(":", 1)[1]
    await callback.answer()  # убираем "часики" на кнопке
    message = callback.message

    if action == "add":
        # Начинаем диалог добавления: шаг 1 — тема
        await message.answer("📝 Введите **тему** нового промпта:", parse_mode=ParseMode.MARKDOWN)
        await state.set_state(AddPromptState.topic)

    elif action == "edit":
        # Показываем список доступных промптов и предлагаем выбрать
        prompts = _load_prompts()
        if not prompts:
            await message.answer(
                "📭 Промптов нет. Добавить первый?", 
                reply_markup=_first_prompt_keyboard(),
            )
            return
        topics = "\n".join(f"• {escape_md_simple(t)}" for t in sorted(prompts.keys()))
        await message.answer(
            f"📝 **Редактирование промпта**\n\n"
            f"Доступные промпты:\n{topics}\n\n"
            "Введите **тему** или **номер** промпта для редактирования:",
            parse_mode=ParseMode.MARKDOWN,
        )
        await state.set_state(EditPromptState.topic)

    elif action == "text":
        # Показываем список и предлагаем выбрать промпт для просмотра
        prompts = _load_prompts()
        if not prompts:
            await message.answer(
                "📭 Промптов нет. Добавить первый?", 
                reply_markup=_first_prompt_keyboard(),
            )
            return
        topics = "\n".join(f"• {escape_md_simple(t)}" for t in sorted(prompts.keys()))
        await message.answer(
            f"📜 **Доступные промпты:**\n{topics}\n\n"
            "Введите **тему** или **номер** промпта:",
            parse_mode=ParseMode.MARKDOWN,
        )
        await state.set_state(GetPromptState.topic)

    elif action == "delete":
        # Показываем список и предлагаем выбрать промпт для удаления
        prompts = _load_prompts()
        if not prompts:
            await message.answer(
                "📭 Промптов нет. Добавить первый?", 
                reply_markup=_first_prompt_keyboard(),
            )
            return
        topics = "\n".join(f"• {escape_md_simple(t)}" for t in sorted(prompts.keys()))
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
    """
    Обрабатывает ответ "Да, добавить" / "Нет" при пустом списке промптов.
    Бизнес-правило: пользователь не должен вводить текст "да" — 
    достаточно нажать кнопку.
    """
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
        await message.answer("❌ Хорошо. Если захотите — напишите `/add_prompt`",
                             parse_mode=ParseMode.MARKDOWN)
        await state.clear()


# ═══════════════════════════════════════════════════════════════════
# БЛОК FSM (Finite State Machine — Диалоговые состояния)
# ═══════════════════════════════════════════════════════════════════
# aiogram использует FSM для многошаговых форм. Когда пользователь
# вводит команду /add_prompt, бот переходит в состояние AddPromptState.topic,
# ждёт тему, потом запрашивает текст и т.д.

class AddPromptState(StatesGroup):
    """Добавление промпта: шаг 1 = тема/файл, шаг 2 = текст, + ожидание темы из файла"""
    topic = State()
    text = State()
    waiting_topic_from_file = State()


class GetPromptState(StatesGroup):
    """Просмотр промпта: шаг 1 = выбор темы/номера"""
    topic = State()


class DeletePromptState(StatesGroup):
    """Удаление промпта: шаг 1 = выбор темы/номера"""
    topic = State()


class AskAddFirstPrompt(StatesGroup):
    """Онбординг: спрашиваем пользователя, хочет ли он создать первый промпт"""
    waiting = State()


class EditPromptState(StatesGroup):
    """Редактирование промпта: шаг 1 = тема, шаг 2 = новый текст"""
    topic = State()
    text = State()


class SetupState(StatesGroup):
    """Настройка IMAP: 4 шага — email → сервер → логин → пароль"""
    email = State()
    server = State()
    login = State()
    password = State()


class AiSetupState(StatesGroup):
    """Настройка AI-провайдера: выбор провайдера → API key → модель"""
    provider = State()
    api_key = State()
    model = State()


class WikiSetupState(StatesGroup):
    """Настройка Яндекс Вики: API-ключ сервисного аккаунта Яндекc Облака.
       Бизнес-правило: API-ключ создаётся в Yandex Cloud Console для сервисного аккаунта
       с ролью wiki.editor. После ввода ключа бот получает IAM-токен и проверяет Wiki API."""
    api_key = State()


class DbSetupState(StatesGroup):
    """Настройка PostgreSQL: 5 шагов — хост, порт, имя БД, пользователь, пароль.
       Доступна только администратору (AlekseyAnanyev)."""
    host = State()
    port = State()
    name = State()
    user = State()
    password = State()


class SetupSingleField(StatesGroup):
    """Одношаговая настройка одного поля почты (/setup email|imap|login|password)."""
    value = State()


# ═══════════════════════════════════════════════════════════════════
# СТАТУСЫ НАСТРОЕК (🔴/🟡/🟢) — МЕНЮ /setup
# ═══════════════════════════════════════════════════════════════════
# /setup, /setup email, /setup db, /setup ai показывают список кнопок
# с параметрами. Каждый параметр отмечен флагом:
#   🔴 — данные не введены
#   🟡 — данные введены, но не проверены
#   🟢 — данные введены и проверены (успешный тест подключения)
# Флаг «проверено» хранится в users.json:
#   email: users[key]["email_checked"] = True
#   db:    users[key]["db"]["checked"]     = True
#   ai:    users[key]["ai"]["checked"]     = True

SETUP_SECTIONS = {
    "email": {
        "title": "📧 Почта (IMAP/SMTP)",
        "fields": [
            ("email", "📧 Email"),
            ("server", "🔌 IMAP-сервер"),
            ("login", "👤 Логин"),
            ("password", "🔑 Пароль"),
        ],
        "check_key": "email_checked",
    },
    "db": {
        "title": "🗄️ PostgreSQL",
        "fields": [
            ("host", "🖥️ Хост"),
            ("port", "🔢 Порт"),
            ("name", "🗄️ Имя БД"),
            ("user", "👤 Пользователь"),
            ("password", "🔑 Пароль"),
        ],
        "check_key": "checked",
    },
    "ai": {
        "title": "🤖 Нейросеть",
        "fields": [
            ("endpoint", "🔗 Endpoint"),
            ("api_key", "🔑 API Key"),
            ("model", "📝 Модель"),
        ],
        "check_key": "checked",
    },
}

SETUP_FIELD_LABELS = {
    "email": "📧 Email",
    "server": "🔌 IMAP-сервер",
    "login": "👤 Логин",
    "password": "🔑 Пароль",
    "host": "🖥️ Хост",
    "port": "🔢 Порт",
    "name": "🗄️ Имя БД",
    "user": "👤 Пользователь",
    "endpoint": "🔗 Endpoint",
    "api_key": "🔑 API Key",
    "model": "📝 Модель",
}


# ── Автоопределение серверов почты по домену email ─────────────
# Таблица известных провайдеров: (imap, pop3, smtp)
KNOWN_MAIL_PROVIDERS = {
    "yandex.ru": ("imap.yandex.ru", "pop.yandex.ru", "smtp.yandex.ru"),
    "ya.ru": ("imap.yandex.ru", "pop.yandex.ru", "smtp.yandex.ru"),
    "mail.ru": ("imap.mail.ru", "pop.mail.ru", "smtp.mail.ru"),
    "bk.ru": ("imap.mail.ru", "pop.mail.ru", "smtp.mail.ru"),
    "list.ru": ("imap.mail.ru", "pop.mail.ru", "smtp.mail.ru"),
    "inbox.ru": ("imap.mail.ru", "pop.mail.ru", "smtp.mail.ru"),
    "gmail.com": ("imap.gmail.com", "pop.gmail.com", "smtp.gmail.com"),
    "googlemail.com": ("imap.gmail.com", "pop.gmail.com", "smtp.gmail.com"),
    "outlook.com": ("outlook.office365.com", "outlook.office365.com", "smtp.office365.com"),
    "hotmail.com": ("outlook.office365.com", "outlook.office365.com", "smtp.office365.com"),
    "live.com": ("outlook.office365.com", "outlook.office365.com", "smtp.office365.com"),
    "icloud.com": ("imap.mail.me.com", "pop.mail.me.com", "smtp.mail.me.com"),
    "me.com": ("imap.mail.me.com", "pop.mail.me.com", "smtp.mail.me.com"),
    "rambler.ru": ("imap.rambler.ru", "pop.rambler.ru", "smtp.rambler.ru"),
    "lenta.ru": ("imap.rambler.ru", "pop.rambler.ru", "smtp.rambler.ru"),
    "myrambler.ru": ("imap.rambler.ru", "pop.rambler.ru", "smtp.rambler.ru"),
    "qq.com": ("imap.qq.com", "pop.qq.com", "smtp.qq.com"),
    "163.com": ("imap.163.com", "pop.163.com", "smtp.163.com"),
}


def _detect_mail_servers(email: str) -> dict | None:
    """Пытается определить (imap, pop3, smtp) хосты по домену email.
    Возвращает {'imap': ..., 'pop3': ..., 'smtp': ...} или None, если
    ни один сервер определить не удалось.
    Порядок: 1) таблица провайдеров, 2) MX-запись домена (dig),
    3) стандартные префиксы imap./pop./smtp. + DNS.
    """
    if not email or "@" not in email:
        return None
    domain = email.rsplit("@", 1)[1].strip().lower()
    if not domain:
        return None

    # 1) Известный провайдер
    hit = KNOWN_MAIL_PROVIDERS.get(domain)
    if hit:
        return {"imap": hit[0], "pop3": hit[1], "smtp": hit[2]}

    # 2) MX-запись домена → провайдер (dig, с таймаутом; нет dig — пропускаем)
    mx = _mx_provider_servers(domain)
    if mx:
        return {"imap": mx[0], "pop3": mx[1], "smtp": mx[2]}

    # 3) Стандартные префиксы imap./pop./smtp. — проверяем DNS
    import socket
    found = {}
    for proto, prefix in (("imap", "imap"), ("pop3", "pop"), ("smtp", "smtp")):
        host = f"{prefix}.{domain}"
        try:
            socket.getaddrinfo(host, None, socket.AF_INET, socket.SOCK_STREAM)
            found[proto] = host
        except OSError:
            pass
    return found or None


def _mx_provider_servers(domain: str) -> tuple | None:
    """Определяет провайдера почты по MX-записи домена (subprocess dig).
    Возвращает (imap, pop3, smtp) серверы провайдера или None.
    Работает для корпоративных доменов на чужих почтовых системах
    (например, mail@corp.ru на Яндексе → MX mx.yandex.net → yandex)."""
    import shutil
    import subprocess
    if not shutil.which("dig"):
        return None
    try:
        out = subprocess.run(
            ["dig", "+short", "MX", domain],
            capture_output=True, text=True, timeout=5,
        ).stdout
    except Exception:
        return None
    mx_host = ""
    for line in out.splitlines():
        line = line.strip().rstrip(".")
        if not line or line.startswith(";"):
            continue
        parts = line.split()
        if parts:
            mx_host = parts[-1].lower()
            break
    if not mx_host:
        return None
    if "yandex" in mx_host:
        return KNOWN_MAIL_PROVIDERS["yandex.ru"]
    if "mail.ru" in mx_host:
        return KNOWN_MAIL_PROVIDERS["mail.ru"]
    if "google" in mx_host or "googlemail" in mx_host:
        return KNOWN_MAIL_PROVIDERS["gmail.com"]
    if "outlook" in mx_host or "office365" in mx_host or "microsoft" in mx_host:
        return KNOWN_MAIL_PROVIDERS["outlook.com"]
    if "icloud" in mx_host or "apple" in mx_host:
        return KNOWN_MAIL_PROVIDERS["icloud.com"]
    if "rambler" in mx_host:
        return KNOWN_MAIL_PROVIDERS["rambler.ru"]
    if "qq.com" in mx_host:
        return KNOWN_MAIL_PROVIDERS["qq.com"]
    return None


def _auto_fill_mail_servers(user_id: int, email: str) -> dict | None:
    """Определяет серверы по email и сохраняет их в users.json.
    Заполняет только пустые поля (не перетирает введённые вручную).
    Возвращает определённый dict или None."""
    detected = _detect_mail_servers(email)
    if not detected:
        return None
    users = _load_users()
    key = str(user_id)
    if key not in users:
        users[key] = {}
    changed = False
    if not users[key].get("server") and detected.get("imap"):
        users[key]["server"] = detected["imap"]
        changed = True
    if not users[key].get("smtp_host") and detected.get("smtp"):
        users[key]["smtp_host"] = detected["smtp"]
        changed = True
    if not users[key].get("pop3_host") and detected.get("pop3"):
        users[key]["pop3_host"] = detected["pop3"]
        changed = True
    # Логин для IMAP обычно совпадает с email — подставляем автоматически,
    # если ещё не введён вручную
    if not users[key].get("login") and email:
        users[key]["login"] = email
        changed = True
    if changed:
        _save_users(users)
    return detected


def _smtp_host_for(config: dict, server: str = "") -> str:
    """SMTP-хост для проверки подключения: приоритет — сохранённый
    smtp_host (автоопределённый или введённый), затем вывод из imap-хоста,
    затем дефолт smtp.yandex.ru."""
    if config and config.get("smtp_host"):
        return config["smtp_host"]
    srv = server or (config or {}).get("server", "")
    if "imap" in srv:
        return srv.replace("imap", "smtp")
    return "smtp.yandex.ru"


def _section_config(user_id: int, section: str) -> tuple:
    """Возвращает (config, users, key) для секции: email — корень users[key], db/ai — вложенный dict."""
    users = _load_users()
    key = str(user_id)
    u = users.get(key, {})
    if section == "email":
        return u, users, key
    return u.get(section, {}), users, key


def _field_status(user_id: int, section: str, field: str) -> str:
    """Флаг параметра: 🔴 не введено · 🟡 введено, не проверено · 🟢 введено и проверено.
    Для поля email зелёный флаг ставится сразу при вводе (валидность адреса
    уже подтверждена validate_email), проверка SMTP/IMAP — отдельный шаг
    (email_checked влияет на общий флаг секции)."""
    cfg, users, key = _section_config(user_id, section)
    if not cfg.get(field):
        return "🔴"
    if section == "email" and field == "email":
        return "🟢"
    check_key = SETUP_SECTIONS[section]["check_key"]
    checked = cfg.get(check_key) if section == "email" else cfg.get(check_key)
    return "🟢" if checked else "🟡"


def _section_overall(user_id: int, section: str) -> str:
    """Общий флаг секции: 🔴 есть незаполненные · 🟢 всё заполнено и проверено · 🟡 заполнено, не проверено."""
    cfg, users, key = _section_config(user_id, section)
    fields = SETUP_SECTIONS[section]["fields"]
    if not all(cfg.get(f) for f, _ in fields):
        return "🔴"
    check_key = SETUP_SECTIONS[section]["check_key"]
    return "🟢" if cfg.get(check_key) else "🟡"


def _setup_section_text(user_id: int, section: str) -> str:
    """Текст меню секции /setup."""
    spec = SETUP_SECTIONS[section]
    lines = [f"⚙️ **{spec['title']}**\n", "Выберите параметр для настройки:", ""]
    for field, label in spec["fields"]:
        flag = _field_status(user_id, section, field)
        lines.append(f"{flag} {label}")
    lines += [
        "",
        "🔴 — не введено",
        "🟡 — введено, не проверено",
        "🟢 — введено и проверено",
    ]
    return "\n".join(lines)


def _setup_section_keyboard(user_id: int, section: str) -> InlineKeyboardMarkup:
    """Кнопки-параметры секции с флагами + полная настройка/проверка/назад."""
    spec = SETUP_SECTIONS[section]
    rows = []
    for field, label in spec["fields"]:
        flag = _field_status(user_id, section, field)
        rows.append([InlineKeyboardButton(
            text=f"{flag} {label}",
            callback_data=f"setup_param:{section}:{field}",
        )])
    rows.append([InlineKeyboardButton(
        text="▶️ Полная настройка",
        callback_data=f"setup_full:{section}",
    )])
    rows.append([
        InlineKeyboardButton(text="🧪 Проверить", callback_data=f"setup_test:{section}"),
        InlineKeyboardButton(text="⬅️ Назад", callback_data="setup_menu:root"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _setup_root_text(user_id: int) -> str:
    """Текст главного меню /setup."""
    return (
        "⚙️ **Настройки бота**\n\n"
        "Выберите раздел:\n\n"
        f"{_section_overall(user_id, 'email')} 📧 Почта (IMAP/SMTP)\n"
        f"{_section_overall(user_id, 'db')} 🗄️ PostgreSQL\n"
        f"{_section_overall(user_id, 'ai')} 🤖 Нейросеть\n\n"
        "🔴 — не введено\n"
        "🟡 — введено, не проверено\n"
        "🟢 — введено и проверено"
    )


def _setup_root_keyboard(user_id: int) -> InlineKeyboardMarkup:
    """Главное меню /setup: разделы с общими флагами."""
    sections = [
        ("email", "📧 Почта"),
        ("db", "🗄️ PostgreSQL"),
        ("ai", "🤖 Нейросеть"),
    ]
    rows = []
    for key, label in sections:
        flag = _section_overall(user_id, key)
        rows.append([InlineKeyboardButton(
            text=f"{flag} {label}",
            callback_data=f"setup_menu:{key}",
        )])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ═══════════════════════════════════════════════════════════════════
# БЛОК AI-ПРОВАЙДЕРОВ
# ═══════════════════════════════════════════════════════════════════
# Бизнес-требование: пользователь может выбрать любого провайдера
# с OpenAI-совместимым API. Предустановлены OpenRouter, Hermes/Nous, 
# OpenAI — плюс возможность указать свой endpoint.

AI_PROVIDERS = {
    "openai": {
        "label": "OpenAI",
        "endpoint": "https://api.openai.com/v1",
        "hint_model": "gpt-4o",
    },
    "openrouter": {
        "label": "OpenRouter",
        "endpoint": "https://openrouter.ai/api/v1",
        "hint_model": "deepseek/deepseek-v4-flash",
    },
    "deepseek": {
        "label": "DeepSeek 🇨🇳",
        "endpoint": "https://api.deepseek.com/v1",
        "hint_model": "deepseek-v4-flash",
    },
    "qwen": {
        "label": "Qwen (Alibaba) 🇨🇳",
        "endpoint": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "hint_model": "qwen-max",
    },
    "gemini": {
        "label": "Google Gemini",
        "endpoint": "https://generativelanguage.googleapis.com/v1beta/openai/",
        "hint_model": "gemini-2.0-flash-001",
    },
    "zhipu": {
        "label": "Zhipu AI / GLM 🇨🇳",
        "endpoint": "https://open.bigmodel.cn/api/paas/v4",
        "hint_model": "glm-4-flash",
    },
    "moonshot": {
        "label": "Moonshot / Kimi 🇨🇳",
        "endpoint": "https://api.moonshot.cn/v1",
        "hint_model": "moonshot-v1-8k",
    },
    "nebius": {
        "label": "Nebius AI Studio",
        "endpoint": "https://api.studio.nebius.ai/v1/",
        "hint_model": "meta-llama/llama-4-maverick",
    },
    "together": {
        "label": "Together AI",
        "endpoint": "https://api.together.xyz/v1",
        "hint_model": "meta-llama/Llama-4-Maverick-17B-128E-Instruct",
    },
    "siliconflow": {
        "label": "SiliconFlow 🇨🇳",
        "endpoint": "https://api.siliconflow.cn/v1",
        "hint_model": "Qwen/Qwen2.5-72B-Instruct",
    },
    "gigachat": {
        "label": "GigaChat (Сбер) 🇷🇺",
        "endpoint": "https://gigachat.devices.sberbank.ru/api/v1/",
        "hint_model": "GigaChat:30+",
    },
    "yandexgpt": {
        "label": "YandexGPT 🇷🇺",
        "endpoint": "https://llm.api.cloud.yandex.net/beta/openai/v1/",
        "hint_model": "yandexgpt-lite",
    },
}

AI_PROVIDER_EMOJI = {
    "openai": "🔵",
    "openrouter": "🟣",
    "deepseek": "🐋",
    "qwen": "🔶",
    "gemini": "✨",
    "zhipu": "💬",
    "moonshot": "🌙",
    "nebius": "🔥",
    "together": "🤝",
    "siliconflow": "💎",
    "gigachat": "🟢",
    "yandexgpt": "🔴",
}

# ── Популярные модели для каждого провайдера ──────────────

AI_MODELS_PER_PROVIDER = {
    "openai": ["gpt-4o", "gpt-4o-mini", "gpt-4-turbo", "gpt-4", "gpt-3.5-turbo"],
    "openrouter": [
        "deepseek/deepseek-v4-flash", "deepseek/deepseek-chat",
        "anthropic/claude-sonnet-4", "anthropic/claude-3.5-haiku",
        "google/gemini-2.0-flash-001",
        "meta-llama/llama-4-maverick", "qwen/qwen-max",
    ],
    "deepseek": ["deepseek-v4-flash", "deepseek-chat", "deepseek-reasoner", "deepseek-v3"],
    "qwen": ["qwen-max", "qwen-plus", "qwen-turbo", "qwen2.5-72b-instruct"],
    "gemini": ["gemini-2.0-flash-001", "gemini-2.0-pro", "gemini-1.5-pro", "gemini-1.5-flash"],
    "zhipu": ["glm-4-flash", "glm-4-plus", "glm-4-air", "glm-4-0520"],
    "moonshot": ["moonshot-v1-8k", "moonshot-v1-32k", "moonshot-v1-128k"],
    "nebius": [
        "meta-llama/llama-4-maverick", "mistralai/mistral-large",
        "deepseek/deepseek-chat", "Qwen/Qwen2.5-72B-Instruct",
    ],
    "together": [
        "meta-llama/Llama-4-Maverick-17B-128E-Instruct",
        "mistralai/Mixtral-8x22B-Instruct-v0.1",
        "Qwen/Qwen2.5-72B-Instruct",
    ],
    "siliconflow": [
        "Qwen/Qwen2.5-72B-Instruct", "deepseek-ai/DeepSeek-V3",
        "meta-llama/Meta-Llama-3.3-70B-Instruct",
        "Pro/Llama-4-Maverick-17B-128E",
    ],
    "gigachat": ["GigaChat:30+", "GigaChat-Pro", "GigaChat-Plus"],
    "yandexgpt": ["yandexgpt-lite", "yandexgpt", "yandexgpt-pro"],
}


def _ai_provider_keyboard() -> InlineKeyboardMarkup:
    """Клавиатура выбора AI-провайдера при настройке /setup_ai."""
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


# ═══════════════════════════════════════════════════════════════════
# БЛОК КНОПОК САММАРИ
# ═══════════════════════════════════════════════════════════════════
# Бизнес-логика: когда бот показывает список конспектов, под каждым
# письмом находится кнопка. Если название конспекта начинается со слова,
# совпадающего с темой промпта — кнопка зелёная "Саммари". 
# Если промпт не найден — жёлтая "Выбрать промпт" с предложением создать.


def _get_item_button(idx: int, display: str) -> InlineKeyboardMarkup | None:
    """
    Создаёт кнопку под конспектом: 🟢 Саммари (если есть подходящий промпт)
    или 🟡 Выбрать промпт (если нет).
    
    Бизнес-правило сопоставления: название конспекта должно начинаться
    с темы промпта (без учёта регистра). Например, промпт "План развития"
    подойдёт к конспекту "План развития на Q2".
    """
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
    """
    Когда пользователь нажимает 🟢 Саммари #N:
    - Берём txt-содержимое конспекта из кеша
    - Берём текст промпта (шаблон саммари)
    - Отправляем в нейросеть через call_ai()
    - Показываем результат
    
    Формат callback_data: summary:IDX:PROMPT_TOPIC
    """
    parts = callback.data.split(":", 2)
    if len(parts) < 3:
        await callback.answer("❌ Ошибка данных", show_alert=True)
        return
    _, idx_str, prompt_topic = parts
    idx = int(idx_str) - 1  # 0-based
    await callback.answer()

    user_id = callback.from_user.id

    # Загружаем из кеша — конспекты с txt-содержимым
    items = _load_notes_cache(user_id)
    if idx < 0 or idx >= len(items):
        await callback.message.answer("❌ Конспект устарел. Запросите /list заново.")
        return

    item = items[idx]
    _dt = item[0]
    display = item[1]
    txt_content = item[2]
    imap_id = item[5] if len(item) >= 6 else ""

    # Загружаем промпт
    prompts = _load_prompts()
    prompt_text = prompts.get(prompt_topic, "")
    if not prompt_text:
        await callback.message.answer(f"❌ Промпт «{escape_md_simple(prompt_topic)}» не найден.")
        return

    if not txt_content:
        await callback.message.answer("❌ В письме не найден текст конспекта (txt-вложение).")
        return

    # Показываем статус — нейросеть может думать до минуты
    status_msg = await callback.message.answer(
        f"⏳ Обрабатываю «{escape_md_simple(display)}» через нейросеть...",
        parse_mode=ParseMode.MARKDOWN,
    )

    # Вызываем AI: system_prompt = текст промпта, user_text = конспект
    system_prompt = prompt_text
    user_text = f"Конспект встречи: «{display}»\n\n{txt_content}"
    result = await call_ai(user_id, system_prompt, user_text)

    # ── Сохраняем в PostgreSQL ───────────────────────────────
    if db.DB_POOL and not result.startswith("❌"):
        ai_config = get_ai_config(user_id)
        ai_model = (ai_config or {}).get("model", "unknown")
        wiki = get_wiki_config(user_id)
        wiki_published = bool(wiki and wiki.get("authorized_key") and get_wiki_mode(user_id) == "auto")
        try:
            meeting_id = await db.get_meeting_by_msg_id(prompt_topic)
            # fallback — сохраняем с фейковым msg_id
            if not meeting_id:
                meeting_id = await db.save_meeting(
                    f"manual:{prompt_topic}:{datetime.now().isoformat()}",
                    user_id, "", f"{SUBJECT_FILTER}: {display}",
                    datetime.now(), txt_content,
                )
            if meeting_id:
                await db.save_summary(
                    meeting_id=meeting_id,
                    user_id=user_id,
                    prompt_topic=prompt_topic,
                    ai_model=ai_model,
                    summary_text=result,
                    wiki_published=wiki_published,
                    wiki_url="",
                )
        except Exception as e:
            logger.error("❌ Ошибка сохранения саммари в БД: %s", e)

    # Удаляем статус
    try:
        await status_msg.delete()
    except Exception:
        pass

    # Выводим результат с заголовком
    header = f"🧠 **Саммари: {escape_md_simple(display)}**\n\n---\n\n"
    full_text = header + result

    # Telegram не принимает >4000 символов — режем
    if len(full_text) <= 4000:
        await callback.message.answer(full_text, parse_mode=ParseMode.MARKDOWN)
    else:
        # Разбиваем на части: заголовок отдельно, текст кусками
        await callback.message.answer(header, parse_mode=ParseMode.MARKDOWN)
        for i in range(0, len(result), 3500):
            await callback.message.answer(result[i:i + 3500])

    # ── Авто-публикация в Wiki или кнопка ─────────────────────
    wiki_config = get_wiki_config(user_id)
    if wiki_config and wiki_config.get("authorized_key"):
        wiki_mode = get_wiki_mode(user_id)
        if wiki_mode == "auto":
            # Автоматическая публикация в Wiki
            page_title = f"{prompt_topic} {datetime.now().strftime('%Y-%m-%d')}"
            success, msg = await publish_to_wiki(page_title, result, wiki_config)
            await callback.message.answer(msg, parse_mode=ParseMode.MARKDOWN)
        elif wiki_mode == "button":
            # Кнопка «Опубликовать в Wiki»
            kb = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(
                    text="📤 Опубликовать в Wiki",
                    callback_data=f"publish_wiki:{idx_str}:{prompt_topic}"
                )
            ]])
            await callback.message.answer(
                "📚 Хотите опубликовать это саммари в Яндекс Вики?",
                reply_markup=kb,
            )

    # ── Помечаем письмо прочитанным ───────────────────────────
    # Если AI успешно сгенерировал саммари — письмо уходит из /list
    if not result.startswith("❌") and imap_id:
        _set_email_read(user_id, imap_id)


@dp.callback_query(lambda c: c.data and c.data.startswith("choose_prompt:"))
async def choose_prompt_callback(callback: CallbackQuery, state: FSMContext):
    """
    Когда пользователь нажимает 🟡 Выбрать промпт #N — предлагаем
    создать подходящий промпт для этого типа конспекта.
    
    Бизнес-правило: подсказываем первое слово из названия конспекта
    как тему нового промпта.
    """
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
        f"📝 Для конспекта «{escape_md_simple(display)}» не найден подходящий промпт.\n\n"
        f"Создайте промпт с названием, которое совпадает с началом строки:\n"
        f"📌 `/add_prompt` → тема: `{display.split()[0] if display.split() else display}` → текст промпта\n\n"
        f"Или используйте `/prompt` для управления промптами.",
        parse_mode=ParseMode.MARKDOWN,
    )


# ── Callback-хендлер для кнопки «📤 Опубликовать в Wiki» ────

@dp.callback_query(lambda c: c.data and c.data.startswith("publish_wiki:"))
async def publish_wiki_callback(callback: CallbackQuery, state: FSMContext):
    """
    Когда пользователь нажимает «📤 Опубликовать в Wiki»:
    - Достаём саммари из кеша (повторно генерируем через AI)
    - Публикуем в Яндекс Вики
    - Показываем результат
    
    Формат callback_data: publish_wiki:IDX:PROMPT_TOPIC
    """
    parts = callback.data.split(":", 2)
    if len(parts) < 3:
        await callback.answer("❌ Ошибка данных", show_alert=True)
        return
    _, idx_str, prompt_topic = parts
    idx = int(idx_str) - 1
    await callback.answer()

    user_id = callback.from_user.id

    # Проверяем настройки Wiki
    wiki_config = get_wiki_config(user_id)
    if not wiki_config or not wiki_config.get("authorized_key"):
        await callback.message.answer(
            "❌ **Яндекс Вики не настроен.**\n"
            "Настройте через `/setup wiki`.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    # Загружаем конспект и промпт из кеша
    items = _load_notes_cache(user_id)
    if idx < 0 or idx >= len(items):
        await callback.message.answer("❌ Конспект устарел. Запросите /list заново.")
        return

    _dt, display, txt_content = items[idx]
    imap_id = items[idx][5] if len(items[idx]) >= 6 else ""
    prompts = _load_prompts()
    prompt_text = prompts.get(prompt_topic, "")
    if not prompt_text or not txt_content:
        await callback.message.answer("❌ Данные конспекта или промпта не найдены.")
        return

    # Повторно генерируем саммари (или можно было кешировать, но проще перегенерировать)
    status_msg = await callback.message.answer(
        f"⏳ Генерирую саммари для «{escape_md_simple(display)}»...",
        parse_mode=ParseMode.MARKDOWN,
    )

    system_prompt = prompt_text
    user_text = f"Конспект встречи: «{display}»\n\n{txt_content}"
    result = await call_ai(user_id, system_prompt, user_text)

    # ── Сохраняем в PostgreSQL ───────────────────────────────
    if db.DB_POOL and not result.startswith("❌"):
        ai_config = get_ai_config(user_id)
        ai_model = (ai_config or {}).get("model", "unknown")
        try:
            meeting_id = await db.get_meeting_by_msg_id(prompt_topic)
            if not meeting_id:
                meeting_id = await db.save_meeting(
                    f"manual:{prompt_topic}:{datetime.now().isoformat()}",
                    user_id, "", f"{SUBJECT_FILTER}: {display}",
                    datetime.now(), txt_content,
                )
            if meeting_id:
                wiki_url = f"https://wiki.yandex.ru/?orgId={wiki_config.get('org_id', '')}" if wiki_config.get("org_id") else ""
                await db.save_summary(
                    meeting_id=meeting_id,
                    user_id=user_id,
                    prompt_topic=prompt_topic,
                    ai_model=ai_model,
                    summary_text=result,
                    wiki_published=True,
                    wiki_url=wiki_url,
                )
        except Exception as e:
            logger.error("❌ Ошибка сохранения саммари (wiki) в БД: %s", e)

    try:
        await status_msg.delete()
    except Exception:
        pass

    if result.startswith("❌"):
        await callback.message.answer(result)
        return

    # Публикуем в Wiki
    page_title = f"{prompt_topic} {datetime.now().strftime('%Y-%m-%d')}"
    success, msg = await publish_to_wiki(page_title, result, wiki_config)
    await callback.message.answer(msg, parse_mode=ParseMode.MARKDOWN)

    # Помечаем письмо прочитанным — саммари опубликовано в Wiki
    if imap_id:
        _set_email_read(user_id, imap_id)


# ═══════════════════════════════════════════════════════════════════
# КОМАНДЫ TELEGRAM — ПРОМПТЫ
# ═══════════════════════════════════════════════════════════════════


# ── Вспомогательные функции для загрузки файлов ──────────

async def _extract_text_from_file(message: Message) -> str | None:
    """Downloads and extracts text from an attached file.
       Supported: txt, docx, pdf, rtf, doc, pages."""
    if not message.document:
        return None

    file_name = message.document.file_name or "file"
    ext = Path(file_name).suffix.lower()

    try:
        tg_file = await bot.get_file(message.document.file_id)
        temp_dir = tempfile.mkdtemp()
        local_path = Path(temp_dir) / file_name
        await bot.download_file(tg_file.file_path, destination=str(local_path))
    except Exception as e:
        logger.error(f"Error downloading file: {e}")
        return None

    try:
        if ext == ".txt":
            text = local_path.read_text(encoding="utf-8", errors="replace")

        elif ext == ".docx":
            from docx import Document
            doc = Document(str(local_path))
            text = "\n".join(p.text for p in doc.paragraphs)

        elif ext == ".pdf":
            from pypdf import PdfReader
            reader = PdfReader(str(local_path))
            text = "\n".join(page.extract_text() or "" for page in reader.pages)

        elif ext == ".rtf":
            from striprtf.striprtf import rtf_to_text
            raw = local_path.read_text(encoding="utf-8", errors="replace")
            text = rtf_to_text(raw)

        elif ext == ".doc":
            import olefile
            try:
                ole = olefile.OleFileIO(str(local_path))
                if ole.exists('WordDocument'):
                    data = ole.openstream('WordDocument').read()
                    text = data.decode("utf-16-le", errors="replace")
                    text = "".join(c for c in text if c.isprintable() or c in "\n\r\t")
                else:
                    text = ""
                ole.close()
            except Exception:
                text = ""
            if not text.strip():
                text = local_path.read_bytes().decode("utf-8", errors="replace")
                text = "".join(c for c in text if c.isprintable() or c in "\n\r\t")

        elif ext == ".pages":
            with zipfile.ZipFile(str(local_path), "r") as zf:
                xml_candidates = [n for n in zf.namelist() if n.endswith(".xml") and "index" in n.lower()]
                xml_candidates += [n for n in zf.namelist() if n.endswith(".xml")]
                text = ""
                for xml_name in xml_candidates:
                    try:
                        xml_content = zf.read(xml_name)
                        root = ET.fromstring(xml_content)
                        texts = []
                        for elem in root.iter():
                            if elem.text and elem.text.strip():
                                texts.append(elem.text.strip())
                        if texts:
                            text = "\n".join(texts)
                            break
                    except Exception:
                        continue

        else:
            return None

        return text.strip()

    except Exception as e:
        logger.error(f"Error extracting from {file_name}: {e}")
        return None
    finally:
        import shutil
        shutil.rmtree(temp_dir, ignore_errors=True)


def _detect_title_from_text(text: str) -> str | None:
    """Detect prompt theme from first non-empty line."""
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    if not lines:
        return None
    first = lines[0]
    if len(first) <= 100 and not first.startswith(("-", "*", "1.", "2.", "3.", "4.", "5.")):
        return first
    return None


@dp.message(Command("prompt", "промпт", "промпты"))
async def cmd_list_prompts(message: Message, state: FSMContext, command: CommandObject):
    """Выводит список всех промптов с кнопками управления.
       Поддерживает подкоманды: /prompt add, edit, text, delete.
       Бизнес-правило: разные способы ввода — /prompt add (для тех,
       кто знает), /add_prompt (прямая команда), кнопки (для всех)."""

    # Проверяем подкоманды — пользователь может написать /prompt add
    if command.args:
        sub = command.args.strip().lower()
        if sub == "add":
            # Check if theme is in the command: /prompt add "Theme"
            parts = command.args.strip().split(maxsplit=1)
            if len(parts) > 1:
                topic = parts[1].strip().strip(chr(34)).strip("'").strip()
                if topic:
                    prompts = _load_prompts()
                    if topic in prompts:
                        await message.answer(
                            f"Prompt with topic \"{escape_md_simple(topic)}\" already exists! "
                            f"Current text:\n`{prompts[topic][:200]}`\n\n"
                            "Enter a **different** topic:",
                            parse_mode=ParseMode.MARKDOWN,
                        )
                        await state.set_state(AddPromptState.topic)
                        return
                    await state.update_data(topic=topic)
                    await message.answer(
                        f"Topic \"{escape_md_simple(topic)}\" accepted.\n\n"
                        "Now enter the **text** of the prompt:\n"
                        "_(or attach a file: txt, docx, pdf, rtf, doc, pages)_",
                        parse_mode=ParseMode.MARKDOWN,
                    )
                    await state.set_state(AddPromptState.text)
                    return

            await message.answer(
                "Enter the **topic** of the new prompt:\n"
                "_(or attach a file - topic will be detected from first line)_",
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
            topics = "\n".join(f"• {escape_md_simple(t)}" for t in sorted_topics)
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
            topics = "\n".join(f"• {escape_md_simple(t)}" for t in sorted_topics)
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
            topics = "\n".join(f"• {escape_md_simple(t)}" for t in sorted_topics)
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
    """Обрабатывает текстовый ответ Да/Нет на предложение добавить первый промпт.
       Используется, если пользователь не нажал инлайн-кнопку, а напечатал текст."""
    answer = message.text.strip().lower()
    if answer in ("да", "yes", "lf", "д", "y"):
        await state.set_state(AddPromptState.topic)
        await message.answer(
            "📝 Введите **тему** нового промпта:",
            parse_mode=ParseMode.MARKDOWN,
        )
    else:
        await message.answer("❌ Хорошо. Если захотите — напишите `/add_prompt`",
                             parse_mode=ParseMode.MARKDOWN)
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
    """
    Сохраняет тему промпта и запрашивает текст (шаблон саммари).
    Бизнес-правило: темы промптов должны быть уникальны — это ключ
    для сопоставления с конспектами.
    Поддерживает загрузку файла для автоматического определения темы.
    """
    # Если пользователь прислал файл — извлекаем текст и определяем тему
    if message.document:
        file_text = await _extract_text_from_file(message)
        if file_text:
            detected = _detect_title_from_text(file_text)
            if detected:
                topic = detected
                prompts = _load_prompts()
                if topic in prompts:
                    await message.answer(
                        f"⚠️ Промпт с темой «{escape_md_simple(topic)}» уже существует!\n"
                        f"Текущий текст:\n`{prompts[topic][:200]}`\n\n"
                        "Введите **другую** тему или пришлите другой файл:",
                        parse_mode=ParseMode.MARKDOWN,
                    )
                    return
                await state.update_data(topic=topic, file_text=file_text)
                await message.answer(
                    f"✅ Из файла определена тема: **«{escape_md_simple(topic)}»**\\n\\n"
                    "Теперь введите **текст** промпта или пришлите другой файл:\n"
                    "_(если оставить пустым — будет использован текст из файла)_",
                    parse_mode=ParseMode.MARKDOWN,
                )
                await state.set_state(AddPromptState.text)
                return
            else:
                # Не удалось определить тему — сохраняем текст, спрашиваем тему
                preview = file_text[:100].replace("\n", " ")
                await state.update_data(file_text=file_text)
                await message.answer(
                    f"📄 Текст из файла (первые 100 символов):\n`{escape_md_simple(preview)}...`\n\n"
                    "Не удалось автоматически определить тему.\n"
                    "Введите **тему** этого промпта вручную:",
                    parse_mode=ParseMode.MARKDOWN,
                )
                await state.set_state(AddPromptState.waiting_topic_from_file)
                return
        await message.answer("⚠️ Не удалось извлечь текст из файла. Попробуйте другой формат или введите текст:")
        return

    topic = message.text.strip()
    if not topic:
        await message.answer("⚠️ Тема не может быть пустой. Введите тему или пришлите файл:")
        return

    # Проверяем уникальность темы
    prompts = _load_prompts()
    if topic in prompts:
        await message.answer(
            f"⚠️ Промпт с темой «{escape_md_simple(topic)}» уже существует!\n"
            f"Текущий текст:\n`{prompts[topic][:200]}`\n\n"
            "Введите **другую** тему:",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    await state.update_data(topic=topic)
    await message.answer(
        f"✅ Тема «{escape_md_simple(topic)}» принята.\n\n"
        "Теперь введите **текст** промпта:\n"
        "_(или пришлите файл: txt, docx, pdf, rtf, doc, pages)_",
        parse_mode=ParseMode.MARKDOWN,
    )
    await state.set_state(AddPromptState.text)
@dp.message(AddPromptState.text)
async def add_prompt_text(message: Message, state: FSMContext):
    """
    Сохраняет текст промпта.
    Бизнес-правило: после сохранения показываем обновлённый список,
    чтобы пользователь видел результат.
    Поддерживает загрузку файла как текста промпта.
    """
    data = await state.get_data()
    topic = data.get("topic")

    # Если пользователь прислал файл — извлекаем текст
    if message.document:
        file_text = await _extract_text_from_file(message)
        if file_text:
            text = file_text
        else:
            await message.answer("⚠️ Не удалось извлечь текст из файла. Попробуйте другой формат или введите текст:")
            return
    else:
        text = message.text.strip()
        if not text:
            # Если есть file_text из предыдущего шага — используем его
            if data.get("file_text"):
                text = data["file_text"]
            else:
                await message.answer("⚠️ Текст промпта не может быть пустым. Введите текст или пришлите файл:")
                return

    prompts = _load_prompts()
    prompts[topic] = text
    _save_prompts(prompts)

    await message.answer(
        f"🧠 **Промпт «{escape_md_simple(topic)}» добавлен в память.**\n\n"
        f"📄 Длина: {len(text)} символов",
        parse_mode=ParseMode.MARKDOWN,
    )
    await state.clear()

    # Автоматически показываем обновлённый список
    list_text = _format_prompt_list()
    await message.answer(list_text, parse_mode=ParseMode.MARKDOWN, reply_markup=_prompt_keyboard())

# ── Обработчик для ручного ввода темы после загрузки файла ──

@dp.message(AddPromptState.waiting_topic_from_file)
async def add_prompt_topic_from_file(message: Message, state: FSMContext):
    """
    Пользователь загрузил файл, но тема не определилась автоматически.
    Спрашиваем тему вручную, затем переходим к вводу текста.
    """
    topic = message.text.strip()
    if not topic:
        await message.answer("⚠️ Тема не может быть пустой. Введите тему:")
        return

    data = await state.get_data()
    file_text = data.get("file_text", "")

    prompts = _load_prompts()
    if topic in prompts:
        await message.answer(
            f"⚠️ Промпт с темой «{escape_md_simple(topic)}» уже существует!\n"
            f"Текущий текст:\n`{prompts[topic][:200]}`\n\n"
            "Введите **другую** тему:",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    await state.update_data(topic=topic)
    if file_text:
        # Текст из файла уже есть — сохраняем сразу
        prompts[topic] = file_text
        _save_prompts(prompts)
        await message.answer(
            f"🧠 **Промпт «{escape_md_simple(topic)}» добавлен в память.**\n\n"
            f"📄 Длина: {len(file_text)} символов (из файла)",
            parse_mode=ParseMode.MARKDOWN,
        )
        await state.clear()
        list_text = _format_prompt_list()
        await message.answer(list_text, parse_mode=ParseMode.MARKDOWN, reply_markup=_prompt_keyboard())
    else:
        await message.answer(
            f"✅ Тема «{escape_md_simple(topic)}» принята.\n\n"
            "Теперь введите **текст** промпта или пришлите файл:",
            parse_mode=ParseMode.MARKDOWN,
        )
        await state.set_state(AddPromptState.text)

# ── Команда /text_prompt ─────────────────────────────────────

@dp.message(Command("text_prompt", "prompt_text"))
async def cmd_text_prompt_start(message: Message, state: FSMContext):
    """
    /text_prompt <номер> — сразу показывает текст промпта (без диалога)
    /text_prompt — диалог: спрашивает тему
    
    Бизнес-правило: power user может написать /text_prompt 3 и сразу
    получить текст. Новичок вводит /text_prompt и выбирает из списка.
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

    # Пробуем распарсить номер/тему из аргумента команды
    parts = message.text.strip().split(maxsplit=1)
    if len(parts) == 2:
        arg = parts[1].strip()
        try:
            # Аргумент — число: ищем по индексу (1-based)
            idx = int(arg) - 1
            if 0 <= idx < len(sorted_topics):
                topic = sorted_topics[idx]
                text = prompts[topic]
                full = f"📌 **{escape_md_simple(topic)}**\n\n{escape_md_simple(text)}"
                if len(full) <= MAX_MSG_LEN:
                    await message.answer(full, parse_mode=ParseMode.MARKDOWN)
                else:
                    await message.answer(
                        f"📌 **{escape_md_simple(topic)}**\n\n{escape_md_simple(text[:MAX_MSG_LEN - 50])}\n\n"
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
            # Аргумент — не число, возможно это тема промпта
            if arg in prompts:
                text = prompts[arg]
                full = f"📌 **{escape_md_simple(arg)}**\n\n{escape_md_simple(text)}"
                if len(full) <= MAX_MSG_LEN:
                    await message.answer(full, parse_mode=ParseMode.MARKDOWN)
                else:
                    await message.answer(
                        f"📌 **{escape_md_simple(arg)}**\n\n{escape_md_simple(text[:MAX_MSG_LEN - 50])}\n\n"
                        f"_…текст слишком длинный, сохранён в боте_",
                        parse_mode=ParseMode.MARKDOWN,
                    )
                return

    # Без аргументов — запускаем FSM диалог выбора
    topics = "\n".join(f"• {escape_md_simple(t)}" for t in sorted_topics)
    await message.answer(
        f"📜 **Доступные промпты:**\n{topics}\n\n"
        "Введите **тему** или **номер** промпта:",
        parse_mode=ParseMode.MARKDOWN,
    )
    await state.set_state(GetPromptState.topic)


@dp.message(GetPromptState.topic)
async def text_prompt_show(message: Message, state: FSMContext):
    """
    Показывает текст промпта по теме или номеру (FSM-диалог).
    Принимает как точное название темы, так и порядковый номер.
    """
    arg = message.text.strip()
    prompts = _load_prompts()
    if not prompts:
        await message.answer("📭 Промпты закончились. Сначала добавьте через /add_prompt")
        await state.clear()
        return

    sorted_topics = sorted(prompts.keys())
    topic: str | None = None

    # Сначала пробуем интерпретировать как номер
    try:
        idx = int(arg) - 1
        if 0 <= idx < len(sorted_topics):
            topic = sorted_topics[idx]
    except ValueError:
        pass

    # Если не номер — ищем по точному совпадению темы
    if topic is None and arg in prompts:
        topic = arg

    if topic is None:
        # Ничего не нашли — показываем список и просим повторить
        topics = "\n".join(f"• {escape_md_simple(t)}" for t in sorted_topics)
        await message.answer(
            f"⚠️ Не найдено. Доступные промпты:\n{topics}\n\n"
            "Введите **тему** или **номер**:",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    text = prompts[topic]
    full = f"📌 **{escape_md_simple(topic)}**\n\n{escape_md_simple(text)}"
    if len(full) <= MAX_MSG_LEN:
        await message.answer(full, parse_mode=ParseMode.MARKDOWN)
    else:
        await message.answer(
            f"📌 **{escape_md_simple(topic)}**\n\n{escape_md_simple(text[:MAX_MSG_LEN - 50])}\n\n"
            f"_…текст слишком длинный, сохранён в боте_",
            parse_mode=ParseMode.MARKDOWN,
        )

    await state.clear()


# ── Команда /delete_prompt ───────────────────────────────────

@dp.message(Command("delete_prompt", "prompt_delete"))
async def cmd_delete_prompt_start(message: Message, state: FSMContext):
    """
    /delete_prompt <номер> — удаляет промпт без диалога
    /delete_prompt — диалог: спрашивает тему
    
    Бизнес-правило: после удаления показываем кнопки управления,
    чтобы пользователь мог сразу добавить новый промпт.
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

    # Пробуем распарсить номер/тему из аргумента
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
                    f"🗑 **Промпт «{escape_md_simple(topic)}» удалён.**",
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
                    f"🗑 **Промпт «{escape_md_simple(arg)}» удалён.**",
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=_prompt_keyboard(),
                )
                return

    # Без аргументов — FSM диалог
    topics = "\n".join(f"• {escape_md_simple(t)}" for t in sorted_topics)
    await message.answer(
        f"🗑 **Удаление промпта**\n\n"
        f"Доступные промпты:\n{topics}\n\n"
        "Введите **тему** или **номер** промпта для удаления:",
        parse_mode=ParseMode.MARKDOWN,
    )
    await state.set_state(DeletePromptState.topic)


@dp.message(DeletePromptState.topic)
async def delete_prompt_confirm(message: Message, state: FSMContext):
    """Удаляет промпт по теме или номеру (FSM-диалог)."""
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
        topics = "\n".join(f"• {escape_md_simple(t)}" for t in sorted_topics)
        await message.answer(
            f"⚠️ Не найдено. Доступные промпты:\n{topics}\n\n"
            "Введите **тему** или **номер**:",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    del prompts[topic]
    _save_prompts(prompts)

    await message.answer(
        f"🗑 **Промпт «{escape_md_simple(topic)}» удалён.**",
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
    /edit_prompt <номер> — сразу запрашивает новый текст для промпта
    /edit_prompt — диалог: выбирает тему, потом запрашивает текст
    
    Бизнес-правило: перед вводом нового текста показываем старый (до 200 символов),
    чтобы пользователь помнил, что он редактирует.
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

    # Пробуем распарсить номер/тему из аргумента
    parts = message.text.strip().split(maxsplit=1)
    if len(parts) == 2:
        arg = parts[1].strip()
        try:
            idx = int(arg) - 1
            if 0 <= idx < len(sorted_topics):
                topic = sorted_topics[idx]
                await state.update_data(topic=topic)
                await message.answer(
                    f"📝 Редактирование промпта **«{escape_md_simple(topic)}»**\n\n"
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
                    f"📝 Редактирование промпта **«{escape_md_simple(arg)}»**\n\n"
                    f"Текущий текст:\n`{prompts[arg][:200]}`\n\n"
                    "Введите **новый текст** промпта:",
                    parse_mode=ParseMode.MARKDOWN,
                )
                await state.set_state(EditPromptState.text)
                return

    # Без аргументов — спрашиваем тему
    topics = "\n".join(f"• {escape_md_simple(t)}" for t in sorted_topics)
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
        topics = "\n".join(f"• {escape_md_simple(t)}" for t in sorted_topics)
        await message.answer(
            f"⚠️ Не найдено. Доступные промпты:\n{topics}\n\n"
            "Введите **тему** или **номер**:",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    await state.update_data(topic=topic)
    await message.answer(
        f"📝 Редактирование промпта **«{escape_md_simple(topic)}»**\n\n"
        f"Текущий текст:\n`{prompts[topic][:200]}`\n\n"
        "Введите **новый текст** промпта:",
        parse_mode=ParseMode.MARKDOWN,
    )
    await state.set_state(EditPromptState.text)


@dp.message(EditPromptState.text)
async def edit_prompt_text(message: Message, state: FSMContext):
    """Сохраняет новый текст промпта и показывает обновлённый список."""
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


# ═══════════════════════════════════════════════════════════════════
# КОМАНДЫ TELEGRAM — НАСТРОЙКА И ОСНОВНЫЕ
# ═══════════════════════════════════════════════════════════════════


# ── Команда /init — сброс и повторная настройка ────────────────

async def _start_init(message: Message, state: FSMContext):
    """
    Очищает настройки пользователя и запускает 4-шаговую настройку заново.
    Бизнес-правило: если сменился пароль от почты или нужно переподключиться —
    /init полностью очищает старые данные и начинает с нуля.
    """
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



# ═══════════════════════════════════════════════════════════════════
# HELP REGISTRY — единый реестр групп, команд и эмодзи
# ═══════════════════════════════════════════════════════════════════

HELP_GROUPS = {
    "setup": {"emoji": "🔧", "title": "Настройка", "description": "Настройка почты, AI-провайдера, Яндекс Вики и PostgreSQL"},
    "notes": {"emoji": "📬", "title": "Конспекты встреч", "description": "Просмотр конспектов встреч из почты"},
    "prompt": {"emoji": "🤖", "title": "Промпты", "description": "Управление шаблонами промптов для нейросети"},
    "wiki": {"emoji": "📚", "title": "Яндекс Вики", "description": "Публикация саммари в Яндекс Вики"},
    "other": {"emoji": "ℹ️", "title": "Прочее", "description": "Справка и приветствие"},
}

HELP_COMMANDS = {
    "start": {
        "emoji": "👋", "group": "setup", "title": "Приветствие",
        "short": "Показать приветственное сообщение",
        "syntax": "/start", "aliases": [], "admin": False, "public": True,
        "details": "При первом запуске запускает онбординг (/init).\nЕсли уже настроен — показывает приветствие.",
    },
    "init": {
        "emoji": "🔄", "group": "setup", "title": "Сброс настроек",
        "short": "Сбросить настройки почты и настроить заново",
        "syntax": "/init", "aliases": [], "admin": False, "public": True,
        "details": "Полностью очищает текущие настройки почты.\n4 шага: Email → IMAP-сервер → Логин → Пароль.",
    },
    "setup": {
        "emoji": "🔧", "group": "setup", "title": "Настройка",
        "short": "Запустить мастер настройки почты (4 шага)",
        "syntax": "/setup [подкоманда]", "aliases": [], "admin": False, "public": True,
        "details": "Без аргументов — 4 шага: Email, IMAP-сервер, логин, пароль.\nПодкоманды описаны в /help setup.",
    },
    "setup_email": {
        "emoji": "📧", "group": "setup", "title": "Изменить email",
        "short": "Изменить только email почтового ящика",
        "syntax": "/setup email", "aliases": [], "admin": False, "public": True,
    },
    "setup_imap": {
        "emoji": "🔌", "group": "setup", "title": "Изменить IMAP-сервер",
        "short": "Изменить только IMAP-сервер",
        "syntax": "/setup imap", "aliases": [], "admin": False, "public": True,
    },
    "setup_login": {
        "emoji": "👤", "group": "setup", "title": "Изменить логин",
        "short": "Изменить только логин почты",
        "syntax": "/setup login", "aliases": [], "admin": False, "public": True,
    },
    "setup_password": {
        "emoji": "🔑", "group": "setup", "title": "Изменить пароль",
        "short": "Изменить только пароль приложения",
        "syntax": "/setup password", "aliases": [], "admin": False, "public": True,
    },
    "setup_show": {
        "emoji": "📊", "group": "setup", "title": "Показать настройки",
        "short": "Показать текущие настройки по разделам",
        "syntax": "/setup show [all|account|ai|wiki]", "aliases": [], "admin": False, "public": True,
    },
    "setup_ai": {
        "emoji": "🧠", "group": "setup", "title": "Настроить AI",
        "short": "Настроить AI-провайдера для саммари",
        "syntax": "/setup ai", "aliases": ["/setup_ai", "/setup_llm"],
        "admin": False, "public": True,
        "details": "Выбор провайдера → API-ключ → модель.\nПосле сохранения — автоматическая проверка.",
    },
    "setup_ai_test": {
        "emoji": "🔌", "group": "setup", "title": "Проверить AI",
        "short": "Проверить подключение к AI",
        "syntax": "/setup ai test", "aliases": [], "admin": False, "public": True,
    },
    "setup_wiki": {
        "emoji": "📚", "group": "wiki", "title": "Настроить Wiki",
        "short": "Настроить подключение к Яндекс Вики",
        "syntax": "/setup wiki", "aliases": ["/setup_wiki"],
        "admin": False, "public": True,
        "details": "Потребуется JSON авторизованного ключа сервисного аккаунта.",
    },
    "setup_wiki_org": {
        "emoji": "🏢", "group": "wiki", "title": "ID организации",
        "short": "Указать ID организации Яндекс 360",
        "syntax": "/setup wiki org <ID>", "aliases": [], "admin": False, "public": True,
    },
    "setup_wiki_folder": {
        "emoji": "📁", "group": "wiki", "title": "Папка Wiki",
        "short": "Указать slug папки для публикации",
        "syntax": "/setup wiki folder <slug>", "aliases": [], "admin": False, "public": True,
    },
    "setup_wiki_mode": {
        "emoji": "⚙️", "group": "wiki", "title": "Режим публикации",
        "short": "Режим публикации: auto/button/off",
        "syntax": "/setup wiki mode auto|button|off", "aliases": [], "admin": False, "public": True,
    },
    "setup_wiki_test": {
        "emoji": "🔍", "group": "wiki", "title": "Проверить Wiki",
        "short": "Проверить подключение к Яндекс Вики",
        "syntax": "/setup wiki test", "aliases": ["/setup_wiki_test"],
        "admin": False, "public": True,
    },
    "wiki_test": {
        "emoji": "🔍", "group": "wiki", "title": "Статус Wiki",
        "short": "Проверить подключение к Wiki",
        "syntax": "/wiki test", "aliases": ["/wiki_stat", "/wikistat"],
        "admin": False, "public": True,
    },
    "setup_db": {
        "emoji": "🗄️", "group": "setup", "title": "Настроить БД",
        "short": "Настроить PostgreSQL (только для администратора)",
        "syntax": "/setup db", "aliases": [], "admin": True, "public": False,
    },
    "setup_db_test": {
        "emoji": "🔌", "group": "setup", "title": "Проверить БД",
        "short": "Проверить подключение к PostgreSQL",
        "syntax": "/setup db test", "aliases": [], "admin": True, "public": False,
    },
    "list": {
        "emoji": "📬", "group": "notes", "title": "Непрочитанные",
        "short": "Непрочитанные конспекты (UNSEEN не снимается)",
        "syntax": "/list", "aliases": ["/get_notes", "/конспекты", "/конспект"],
        "admin": False, "public": True,
    },
    "list_all": {
        "emoji": "📋", "group": "notes", "title": "Все конспекты",
        "short": "Все конспекты за последние 7 дней",
        "syntax": "/list all", "aliases": ["/list_all", "/все_конспекты"],
        "admin": False, "public": True,
    },
    "list_new": {
        "emoji": "🆕", "group": "notes", "title": "Новые конспекты",
        "short": "Новые конспекты (не показанные ранее)",
        "syntax": "/list new", "aliases": ["/list_new", "/novye_konspekty"],
        "admin": False, "public": True,
        "details": "ID конспектов сохраняются — повторно не выводятся.",
    },
    "prompt": {
        "emoji": "🤖", "group": "prompt", "title": "Управление промптами",
        "short": "Список промптов с кнопками управления",
        "syntax": "/prompt", "aliases": ["/промпт", "/промпты"],
        "admin": False, "public": True,
    },
    "add_prompt": {
        "emoji": "➕", "group": "prompt", "title": "Добавить промпт",
        "short": "Добавить новый промпт",
        "syntax": "/add_prompt", "aliases": ["/prompt_add"],
        "admin": False, "public": True,
    },
    "edit_prompt": {
        "emoji": "✏️", "group": "prompt", "title": "Редактировать промпт",
        "short": "Редактировать существующий промпт",
        "syntax": "/edit_prompt <номер>", "aliases": ["/prompt_edit"],
        "admin": False, "public": True,
    },
    "text_prompt": {
        "emoji": "📖", "group": "prompt", "title": "Текст промпта",
        "short": "Показать полный текст промпта",
        "syntax": "/text_prompt <номер>", "aliases": ["/prompt_text"],
        "admin": False, "public": True,
    },
    "delete_prompt": {
        "emoji": "🗑", "group": "prompt", "title": "Удалить промпт",
        "short": "Удалить промпт",
        "syntax": "/delete_prompt <номер>", "aliases": ["/prompt_delete"],
        "admin": False, "public": True,
    },
    "help": {
        "emoji": "❓", "group": "other", "title": "Справка",
        "short": "Показать эту справку",
        "syntax": "/help [раздел]", "aliases": ["/помощь", "/команды"],
        "admin": False, "public": True,
    },
    "user": {
        "emoji": "👥", "group": "setup", "title": "Управление пользователями",
        "short": "Управление пользователями (только админ)",
        "syntax": "/user [list|add <id>|remove <id>|ban <id>|unban <id>]",
        "aliases": [], "admin": True, "public": False,
        "details": "Только для администратора.\n"
            "Подкоманды:\n"
            "• `list` — список пользователей (AccessManager)\n"
            "• `add <id>` — добавить пользователя по ID\n"
            "• `remove <id>` — удалить пользователя\n"
            "• `ban <id>` — заблокировать пользователя\n"
            "• `unban <id>` — разблокировать пользователя",
    },
    "request_access": {
        "emoji": "🔑", "group": "setup", "title": "Запросить доступ",
        "short": "Запросить доступ к боту",
        "syntax": "/request_access", "aliases": [], "admin": False, "public": True,
    },
}


def get_command_meta(name: str) -> dict | None:
    clean = name.lstrip("/").lower().replace(" ", "_")
    if clean in HELP_COMMANDS:
        return HELP_COMMANDS[clean]
    for cmd, meta in HELP_COMMANDS.items():
        aliases_clean = [a.lstrip("/").lower() for a in meta.get("aliases", [])]
        if clean in aliases_clean:
            return meta
    return None


def get_command_emoji(name: str) -> str:
    meta = get_command_meta(name)
    return meta["emoji"] if meta else ""


def get_group_emoji(group: str) -> str:
    g = HELP_GROUPS.get(group)
    return g["emoji"] if g else ""


def render_help_overview() -> str:
    lines = ["📚 **Справка по командам**\n"]
    for gkey, ginfo in HELP_GROUPS.items():
        lines.append(f"{ginfo['emoji']} **{ginfo['title']}**")
        lines.append(ginfo["description"])
        lines.append(f"Подробнее: `/help {gkey}`\n")
    lines.append("Используйте `/help <раздел>` для подробной справки.")
    return "\n".join(lines)


def render_help_group(group: str) -> str | None:
    ginfo = HELP_GROUPS.get(group)
    if not ginfo:
        return None
    lines = [f"{ginfo['emoji']} **{ginfo['title']}**\n"]
    for cmd_key, cmd_meta in HELP_COMMANDS.items():
        if cmd_meta.get("group") == group and cmd_meta.get("public", True):
            emoji = cmd_meta["emoji"]
            syntax = cmd_meta["syntax"]
            short = cmd_meta.get("short", "")
            aliases = cmd_meta.get("aliases", [])
            lines.append(f"{emoji} `{syntax}`")
            lines.append(f"  {short}")
            if aliases:
                lines.append(f"  Алиасы: {', '.join(f'`{a}`' for a in aliases)}")
            admin_tag = " 🔐 админ" if cmd_meta.get("admin") else ""
            if admin_tag:
                lines[-1] += admin_tag
            lines.append("")
    if group == "setup":
        lines.append("**Подробнее о подкомандах /setup:**\n")
        subs = [
            ("email", "📧", "Изменить только email"),
            ("imap", "🔌", "Изменить только IMAP-сервер"),
            ("login", "👤", "Изменить только логин"),
            ("password", "🔑", "Изменить только пароль"),
            ("show", "📊", "Показать настройки"),
            ("ai", "🧠", "Настроить AI-провайдера"),
            ("ai test", "🔌", "Проверить подключение к AI"),
            ("wiki", "📚", "Настроить Яндекс Вики"),
            ("db", "🗄️", "Настроить PostgreSQL (админ)"),
        ]
        for sub, sub_e, desc in subs:
            lines.append(f"  {sub_e} `{sub}` — {desc}")
    return "\n".join(lines)


# ---- Help sections -------------------------------------------------

HELP_SETUP = (
    "🔧 **Настройка**\n\n"
    "`/start`\n"
    "  Приветствие. При первом запуске запускает `/init`.\n\n"
    "`/init` или `/setup init`\n"
    "  Сбросить все настройки почты и настроить заново.\n"
    "  4 шага: Email, IMAP-сервер, логин, пароль.\n\n"
    "`/setup`\n"
    "  Настройка IMAP-подключения к почте:\n"
    "  4 шага: Email, IMAP-сервер, логин, пароль.\n"
    "  Пустой Enter в шаге сохраняет текущее значение.\n\n"
    "`/setup email`\n"
    "  Изменить только email.\n"
    "`/setup imap`\n"
    "  Изменить только IMAP-сервер.\n"
    "`/setup login` или `/setup user`\n"
    "  Изменить только логин.\n"
    "`/setup password` или `/setup pass`\n"
    "  Изменить только пароль.\n\n"
    "`/setup show all`\n"
    "  Показать все настройки текущего пользователя.\n"
    "`/setup show account`\n"
    "  Показать настройки доступа к почте.\n"
    "`/setup show ai`\n"
    "  Показать настройки нейросети.\n"
    "`/setup show wiki`\n"
    "  Показать настройки Яндекс Вики.\n\n"
    "`/setup ai`\n"
    "  Настройка нейросети для «Саммари». Популярные модели\n"
    "  показываются при выборе провайдера.\n"
    "`/setup ai test`\n"
    "  Проверить подключение к нейросети. Отправляет тестовый\n"
    "  запрос и показывает результат.\n"
    "`/setup wiki`\n"
    "  Настройка подключения к Яндекс Вики.\n"
    "  Нужно для публикации саммари в вики.\n"
    "`/setup db`\n"
    "  Настройка PostgreSQL (только для администратора).\n"
    "  5 шагов: хост, порт, БД, пользователь, пароль.\n"
    "`/setup db test`\n"
    "  Проверить подключение к PostgreSQL.\n"
)

HELP_LIST = (
    "📬 **Конспекты встреч**\n\n"
    "`/list`\n"
    "  Непрочитанные конспекты. Флаг UNSEEN НЕ снимается.\n"
    "`/list all`\n"
    "  Все конспекты за последние 7 дней.\n"
    "`/list new`\n"
    "  Новые конспекты (не показанные ранее через эту команду).\n"
    "  ID сохраняются -- повторно не выводятся.\n"
)

HELP_PROMPT = (
    "🤖 **Промпты (для нейросети)**\n\n"
    "`/prompt`\n"
    "  Список промптов с кнопками управления.\n"
    "  Подкоманды:\n"
    "  * `/prompt add` -- добавить\n"
    "  * `/prompt edit <номер>` -- редактировать\n"
    "  * `/prompt text <номер>` -- текст\n"
    "  * `/prompt delete <номер>` -- удалить\n"
)

HELP_WIKI = (
    "📚 **Яндекс Вики**\n\n"
    "`/setup wiki`\n"
    "  Настройка подключения к Яндекс Вики (IAM через JWT).\n"
    "  Потребуется JSON авторизованного ключа сервисного аккаунта.\n"
    "`/setup wiki test`\n"
    "  Проверка подключения к Яндекс Вики.\n"
    "`/setup wiki org <ID>`\n"
    "  Указать ID организации Яндекс 360 для бизнеса.\n"
    "`/setup wiki folder <slug>`\n"
    "  Указать slug папки (раздела) Wiki для публикации.\n"
    "  Например: `/setup wiki folder hr_meetings`\n"
    "`/setup wiki mode auto|button|off`\n"
    "  Режим публикации:\n"
    "  • `auto` — сразу после AI саммари → в Wiki\n"
    "  • `button` — кнопка «📤 В Wiki» под саммари\n"
    "  • `off` — публикация отключена (по умолчанию)\n"
    "`/wiki test` или `/wiki stat`\n"
    "  Проверка подключения. Показывает информацию\n"
    "  о пользователе и доступных страницах.\n"
)

HELP_OTHER = (
    "ℹ️ **Прочее**\n\n"
    "`/help` или `/помощь` или `/команды`\n"
    "  Эта справка.\n\n"
    "`/help <раздел>`\n"
    "  Разделы: `setup`, `list`/`notes`, `prompt`, `wiki`.\n\n"
    "`/start`\n"
    "  Краткое приветствие.\n"
)

HELP_ALL_SECTIONS = {
    "setup": ("🔧 Настройка", HELP_SETUP),
    "list": ("📬 Конспекты", HELP_LIST),
    "notes": ("📬 Конспекты", HELP_LIST),
    "prompt": ("🤖 Промпты", HELP_PROMPT),
    "wiki": ("📚 Яндекс Вики", HELP_WIKI),
}


def _help_text(section=None):
    """Return help text. section=None|"all"=full, or a section name."""
    intro = "📚 **Справка по командам**\n\n"
    full = (
        f"{HELP_SETUP}\n\n"
        f"{HELP_LIST}\n\n"
        f"{HELP_PROMPT}\n\n"
        f"{HELP_WIKI}\n\n"
        f"{HELP_OTHER}"
    )
    if not section or section == "all":
        return intro + full
    entry = HELP_ALL_SECTIONS.get(section.lower())
    if entry:
        return intro + entry[1]
    known = "`, `/help ".join(k for k in sorted(HELP_ALL_SECTIONS.keys()) if k != "notes")
    available = " ".join(f"{g['emoji']} {gk}" for gk, g in HELP_GROUPS.items())
    return f"❓ Раздел справки «{escape_md_simple(section)}» не найден.\n\nДоступные разделы: {available}"
@dp.message(Command("init"))
async def cmd_init(message: Message, state: FSMContext):
    """Сбрасывает все настройки почты и запускает настройку заново."""
    await _start_init(message, state)


@dp.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    """
    Команда /start: проверяет доступ, если пользователь новый — запускает онбординг.
    Если уже настроен — показывает приветствие.
    """
    user_id = message.from_user.id
    user = message.from_user

    # ── Access gate ──────────────────────────────────────────
    # Если master_admin_id не задан — пропускаем всех (fallback)
    if _master_admin_id:
        if access_manager.is_admin(user_id):
            # Master admin — полный доступ
            pass
        elif access_manager.is_allowed(user_id):
            # Разрешённый пользователь — полный доступ
            pass
        else:
            # Неизвестный пользователь — запрос доступа
            result = access_manager.request_access(
                user_id=user_id,
                username=user.username or "",
                first_name=user.first_name or "",
                last_name=user.last_name or "",
            )

            if result.get("is_already_allowed"):
                # Уже есть доступ (возник гонка)
                pass
            elif not result.get("is_new"):
                await message.answer(
                    "⏳ Ваш запрос на рассмотрении. Ожидайте, пожалуйста."
                )
                return
            else:
                # Новый запрос — уведомляем администратора
                display_name = (
                    f"{user.first_name or ''} {user.last_name or ''}"
                ).strip() or user.username or f"User#{user_id}"
                try:
                    await bot.send_message(
                        chat_id=_master_admin_id,
                        text=(
                            f"🔔 Новый запрос доступа к боту *HuntTech Protocols*\n\n"
                            f"Пользователь: {escape_md_simple(display_name)}\n"
                            f"ID: `{user_id}`\n"
                            f"Username: @{escape_md_simple(user.username or '-')}\n\n"
                            f"Разрешить: /user add {user_id}"
                        ),
                        parse_mode=ParseMode.MARKDOWN,
                    )
                except Exception as exc:
                    logger.warning("Не удалось уведомить администратора: %s", exc)

                await message.answer(
                    "🚫 Доступ запрещён.\n"
                    "Ваш запрос отправлен администратору. Ожидайте.\n\n"
                    "Если вы уже подавали запрос — /request_access"
                )
                return

    # ── Существующая логика /start ─────────────────────────
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


# ── Команда /request_access — запрос доступа к боту ───────

@dp.message(Command("request_access"))
async def cmd_request_access(message: Message):
    """Если пользователь не имеет доступа — отправляет запрос администратору."""
    user_id = message.from_user.id
    user = message.from_user

    if not _master_admin_id:
        await message.answer("✅ Доступ открыт (контроль доступа не настроен).")
        return

    if access_manager.is_allowed(user_id) or access_manager.is_admin(user_id):
        await message.answer("✅ У вас уже есть доступ к боту.")
        return

    result = access_manager.request_access(
        user_id=user_id,
        username=user.username or "",
        first_name=user.first_name or "",
        last_name=user.last_name or "",
    )

    if result.get("is_already_allowed"):
        await message.answer("✅ У вас уже есть доступ к боту.")
        return

    if result.get("is_new"):
        display_name = (
            f"{user.first_name or ''} {user.last_name or ''}"
        ).strip() or user.username or f"User#{user_id}"
        try:
            await bot.send_message(
                chat_id=_master_admin_id,
                text=(
                    f"🔔 Новый запрос доступа к боту *HuntTech Protocols*\n\n"
                    f"Пользователь: {escape_md_simple(display_name)}\n"
                    f"ID: `{user_id}`\n"
                    f"Username: @{escape_md_simple(user.username or '-')}\n\n"
                    f"Разрешить: /user add {user_id}"
                ),
                parse_mode=ParseMode.MARKDOWN,
            )
        except Exception as exc:
            logger.warning("Не удалось уведомить администратора: %s", exc)
        await message.answer(
            "✅ Запрос отправлен администратору. Ожидайте."
        )
    else:
        await message.answer(
            "⏳ Ваш запрос уже на рассмотрении. Ожидайте."
        )


# ── Команда /user — управление пользователями (только админ) ──

@dp.message(Command("user"))
async def cmd_user(message: Message):
    """/user [list|add|remove|ban|unban] — управление пользователями (только админ)."""
    user_id = message.from_user.id
    args = message.text.strip().split()
    subcmd = args[1] if len(args) > 1 else "help"

    if not _master_admin_id:
        await message.answer("❌ Контроль доступа не настроен.")
        return

    if not access_manager.is_admin(user_id):
        await message.answer("❌ Только администратор может управлять пользователями.")
        return

    if subcmd in ("list", "список"):
        users = access_manager.get_allowed_users()
        pending = access_manager.get_pending_requests()
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
        lines.append(f"👑 Администратор: id={access_manager.master_admin_id}")
        await message.answer("\n".join(lines))
        return

    if len(args) < 3:
        await message.answer("Укажите Telegram ID: /user <cmd> <id>")
        return

    try:
        target_id = int(args[2])
    except ValueError:
        await message.answer("Укажите числовой Telegram ID.")
        return

    if subcmd in ("add", "добавить"):
        access_manager.add_user(user_id=target_id, added_by=user_id)
        await message.answer(f"✅ Пользователь {target_id} добавлен.")
        # Уведомляем пользователя, что доступ открыт
        try:
            await bot.send_message(
                chat_id=target_id,
                text=(
                    "🎉 Вам открыт доступ к боту *HuntTech Protocols*!\n\n"
                    "Напишите /start чтобы начать."
                ),
                parse_mode=ParseMode.MARKDOWN,
            )
        except Exception as exc:
            logger.warning("Не удалось уведомить пользователя %s: %s", target_id, exc)
        return

    if subcmd in ("remove", "delete", "удалить"):
        if target_id == access_manager.master_admin_id:
            await message.answer("Нельзя удалить главного администратора.")
            return
        if access_manager.remove_user(target_id):
            await message.answer(f"✅ Пользователь {target_id} удалён.")
        else:
            await message.answer(f"Пользователь {target_id} не найден.")
        return

    if subcmd in ("ban", "заблокировать"):
        if target_id == access_manager.master_admin_id:
            await message.answer("Нельзя заблокировать главного администратора.")
            return
        if access_manager.ban_user(target_id):
            await message.answer(f"🚫 Пользователь {target_id} заблокирован.")
        else:
            await message.answer(f"Пользователь {target_id} не найден.")
        return

    if subcmd in ("unban", "разблокировать"):
        if access_manager.unban_user(target_id):
            await message.answer(f"✅ Пользователь {target_id} разблокирован.")
        else:
            await message.answer(f"Пользователь {target_id} не найден.")
        return

    # Справка
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
    await message.answer("\n".join(lines))


@dp.message(Command("help", "помощь", "команды"))
async def cmd_help(message: Message, command: CommandObject = None):
    """
    /help -- краткая справка по группам.
    /help <раздел> -- подробная справка по разделу.
    /help all -- полная справка (все разделы).
    """
    section = command.args.strip().lower() if command and command.args else None
    if section:
        group_help = render_help_group(section)
        if group_help:
            await message.answer(group_help, parse_mode=ParseMode.MARKDOWN)
            return
        await message.answer(_help_text(section), parse_mode=ParseMode.MARKDOWN)
        return
    await message.answer(render_help_overview(), parse_mode=ParseMode.MARKDOWN)


# ── Команда /list (только непрочитанные) ─────────────────────

@dp.message(Command("get_notes", "list", "конспекты", "конспект"))
async def cmd_get_notes(message: Message):
    """
    /list — показывает НЕПРОЧИТАННЫЕ конспекты встреч.
    
    Бизнес-процесс: рекрутер нажимает /list, видит только письма,
    которые пришли после его последнего визита. Письма НЕ помечаются
    прочитанными — можно перепроверить в веб-почте.
    """
    # Redirect /list new and /list all
    if message.text and len(message.text.split()) > 1:
        parts = message.text.strip().split(maxsplit=1)
        if len(parts) == 2:
            arg = parts[1].strip().lower()
            if arg in ("new", "novye"):
                return await cmd_list_new(message)
            elif arg == "all":
                return await cmd_list_all(message)

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
        header, items = fetch_notes(user.id)
    except imaplib.IMAP4.error as e:
        await sent.edit_text(f"❌ Ошибка IMAP: `{e}`")
        return
    except Exception as e:
        await sent.edit_text(f"❌ Ошибка: `{e}`")
        import traceback
        traceback.print_exc()
        return

    if not items:
        await sent.edit_text(header or "📭 Нет непрочитанных писем.")
        return

    await sent.delete()

    # Сохраняем txt-содержимое в кеш — нужно для кнопки Саммари
    _save_notes_cache(user.id, items)

    total = len(items)
    await message.answer(f"📋 **Новые конспекты встреч** — всего {total}", parse_mode=ParseMode.MARKDOWN)

    # Каждый конспект — отдельное сообщение с собственной кнопкой
    for idx, item in enumerate(items, 1):
        dt, display = item[0], item[1]
        date_str = dt.strftime("%d.%m.%Y %H:%M")
        text = f"**{idx}.** {escape_md_simple(display)}\n📅 {date_str}"
        button = _get_item_button(idx, display)
        await message.answer(text, parse_mode=ParseMode.MARKDOWN, reply_markup=button)


# ── Команда /list_all (все за неделю) ────────────────────────

@dp.message(Command("list_all", "все_конспекты"))
# ---- Command /list new (only not-yet-shown conspects) ------

@dp.message(Command("list_new", "novye_konspekty"))
async def cmd_list_new(message: Message):
    """Show only conspects not yet displayed via /list new."""
    user = message.from_user
    logger.info("NEW NOTES request from @%s", user.username or user.id)

    if not get_user_config(user.id):
        await message.answer(
            "Mail not configured yet. Use /setup.",
        )
        return

    sent = await message.answer("Searching for new conspects...")

    try:
        header, items = fetch_new_notes(user.id)
    except imaplib.IMAP4.error as e:
        await sent.edit_text(f"IMAP error: {e}")
        return
    except Exception as e:
        await sent.edit_text(f"Error: {e}")
        import traceback
        traceback.print_exc()
        return

    if not items:
        await sent.edit_text(header or "No new conspects.")
        return

    await sent.delete()

    msg_ids = [f"{item[0].timestamp()}:{item[1]}" for item in items]
    _mark_new_comms_shown(user.id, msg_ids)

    _save_notes_cache(user.id, items)

    total = len(items)
    await message.answer(f"New conspects: {total} total", parse_mode=ParseMode.MARKDOWN)

    for idx, item in enumerate(items, 1):
        dt, display = item[0], item[1]
        date_str = dt.strftime("%d.%m.%Y %H:%M")
        text = f"**{idx}.** {escape_md_simple(display)}\n{date_str}"
        button = _get_item_button(idx, display)
        await message.answer(text, parse_mode=ParseMode.MARKDOWN, reply_markup=button)


async def cmd_list_all(message: Message):
    """
    /list_all — показывает ВСЕ конспекты за последние 7 дней.
    В отличие от /list — не фильтрует по UNSEEN.
    """
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
        header, items = fetch_notes_last_week(user.id)
    except imaplib.IMAP4.error as e:
        await sent.edit_text(f"❌ Ошибка IMAP: `{e}`")
        return
    except Exception as e:
        await sent.edit_text(f"❌ Ошибка: `{e}`")
        import traceback
        traceback.print_exc()
        return

    if not items:
        await sent.edit_text(header or "📭 Нет конспектов за неделю.")
        return

    await sent.delete()

    _save_notes_cache(user.id, items)

    total = len(items)
    await message.answer(f"📋 **Конспекты встреч за неделю** — всего {total}", parse_mode=ParseMode.MARKDOWN)

    for idx, item in enumerate(items, 1):
        dt, display = item[0], item[1]
        date_str = dt.strftime("%d.%m.%Y %H:%M")
        text = f"**{idx}.** {escape_md_simple(display)}\n📅 {date_str}"
        button = _get_item_button(idx, display)
        await message.answer(text, parse_mode=ParseMode.MARKDOWN, reply_markup=button)


# ═══════════════════════════════════════════════════════════════════
# КОМАНДА /setup — НАСТРОЙКА IMAP
# ═══════════════════════════════════════════════════════════════════
# Бизнес-процесс: пользователь вводит 4 параметра для подключения к почте.
# После каждого шага показываем текущее значение (если это перенастройка).
# Пустой Enter сохраняет старое значение — удобно, когда меняется только
# пароль, а email и сервер те же.

# ── Функция показа настроек /setup show ────────────────────

async def _cmd_setup_show(message: Message, arg: str):
    """Показывает настройки текущего пользователя.
       /setup show all     — все настройки
       /setup show account — почта и IMAP
       /setup show ai      — AI-настройки
       /setup show wiki    — Яндекс Вики
    """
    user_id = message.from_user.id
    config = get_user_config(user_id)
    parts = arg.split(maxsplit=1)

    if len(parts) < 2:
        await message.answer(
            "ℹ️ **Показ настроек**\n\n"
            "Использование:\n"
            "• `/setup show all` — все настройки\n"
            "• `/setup show account` — доступ к почте\n"
            "• `/setup show ai` — нейросеть\n"
            "• `/setup show wiki` — Яндекс Вики",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    section = parts[1]

    if section == "all":
        lines = ["📋 **Все настройки пользователя**\n"]

        # Account
        lines.append("**📧 Доступ к почте:**")
        if config:
            lines.append(f"  • Email: `{config.get('email', 'не задан')}`")
            lines.append(f"  • IMAP-сервер: `{config.get('server', 'не задан')}`")
            lines.append(f"  • Порт: `{config.get('port', 993)}`")
            lines.append(f"  • Логин: `{config.get('login', 'не задан')}`")
            lines.append(f"  • Пароль: {'✅ задан' if config.get('password') else '❌ не задан'}")
        else:
            lines.append("  ❌ **Не настроено.** Используйте `/setup`")
        lines.append("")

        # AI
        lines.append("**🤖 Нейросеть (AI):**")
        ai = get_ai_config(user_id)
        if ai:
            lines.append(f"  • Endpoint: `{ai.get('endpoint', 'не задан')}`")
            lines.append(f"  • API ключ: {'✅ задан' if ai.get('api_key') else '❌ не задан'}")
            lines.append(f"  • Модель: `{ai.get('model', 'не задана')}`")
        else:
            lines.append("  ❌ **Не настроено.** Используйте `/setup ai`")
        lines.append("")

        # Wiki
        lines.append("**📚 Яндекс Вики:**")
        wiki = get_wiki_config(user_id)
        if wiki:
            has_key = bool(wiki.get("authorized_key"))
            has_api = bool(wiki.get("api_key"))
            has_old_oauth = bool(wiki.get("client_id") and wiki.get("client_secret"))
            lines.append(f"  • Авторизованный ключ: {'✅ задан' if has_key else '❌ не задан'}")
            if has_api:
                lines.append("  • ⚠️ API-ключ не поддерживается — ")
                lines.append("    перенастройте через `/setup wiki`")
            if has_old_oauth:
                lines.append("  • ⚠️ Используется **устаревший OAuth-формат** — ")
                lines.append("    перенастройте через `/setup wiki`")
            lines.append(f"  • ID организации: `{wiki.get('org_id', 'не указан') or 'не указан'}`")
            folder = wiki.get("folder", "")
            lines.append(f"  • Папка: `{folder}`" if folder else "  • Папка: не указана")
            mode = wiki.get("mode", "off")
            mode_labels = {"auto": "🚀 Авто", "button": "📤 По кнопке", "off": "⏸️ Выкл"}
            lines.append(f"  • Режим публикации: {mode_labels.get(mode, mode)}")
        else:
            lines.append("  ❌ **Не настроено.** Используйте `/setup wiki`")

        # DB
        lines.append("")
        lines.append("**🗄️ PostgreSQL:**")
        db_config = get_db_config(user_id)
        if db_config:
            lines.append(f"  • Хост: `{db_config.get('host', '?')}`")
            lines.append(f"  • Порт: `{db_config.get('port', 5432)}`")
            lines.append(f"  • БД: `{db_config.get('name', '?')}`")
            lines.append(f"  • Пользователь: `{db_config.get('user', '?')}`")
            lines.append(f"  • Пароль: {'✅ задан' if db_config.get('password') else '❌ не задан'}")
            if db.DB_POOL:
                lines.append("  • Статус: ✅ **Подключено**")
            else:
                lines.append("  • Статус: ⏸️ **Не подключено** (перезапустите бот)")
        else:
            lines.append("  ❌ **Не настроено.** Используйте `/setup db`")

        await message.answer("\n".join(lines), parse_mode=ParseMode.MARKDOWN)

    elif section == "account":
        if not config:
            await message.answer(
                "❌ **Доступ к почте не настроен.**\n\n"
                "Используйте `/setup` для настройки.",
                parse_mode=ParseMode.MARKDOWN,
            )
            return
        await message.answer(
            "📧 **Доступ к почте**\n\n"
            f"• Адрес: `{config.get('email', 'не задан')}`\n"
            f"• IMAP-сервер: `{config.get('server', 'не задан')}`\n"
            f"• Порт: `{config.get('port', 993)}`\n"
            f"• Логин: `{config.get('login', 'не задан')}`\n"
            f"• Пароль: {'✅ задан' if config.get('password') else '❌ не задан'}",
            parse_mode=ParseMode.MARKDOWN,
        )

    elif section == "ai":
        ai = get_ai_config(user_id)
        if not ai:
            await message.answer(
                "❌ **Нейросеть не настроена.**\n\n"
                "Используйте `/setup ai` для настройки.",
                parse_mode=ParseMode.MARKDOWN,
            )
            return
        await message.answer(
            "🤖 **Настройки нейросети (AI)**\n\n"
            f"• Endpoint: `{ai.get('endpoint', 'не задан')}`\n"
            f"• API ключ: {'✅ задан' if ai.get('api_key') else '❌ не задан'}\n"
            f"• Модель: `{ai.get('model', 'не задана')}`",
            parse_mode=ParseMode.MARKDOWN,
        )

    elif section == "wiki":
        wiki = get_wiki_config(user_id)
        if not wiki:
            await message.answer(
                "❌ **Яндекс Вики не настроена.**\n\n"
                "Используйте `/setup wiki` для настройки.",
                parse_mode=ParseMode.MARKDOWN,
            )
            return
        has_key = bool(wiki.get("authorized_key"))
        has_old = bool(wiki.get("client_id") and wiki.get("client_secret"))
        mode = wiki.get("mode", "off")
        mode_labels = {"auto": "🚀 Авто (сразу в Wiki)", "button": "📤 По кнопке", "off": "⏸️ Выключено"}
        lines = [
            "📚 **Настройки Яндекс Вики**\n",
            "• JWT-ключ: {'✅ задан' if has_key else '❌ не задан'}",
        ]
        if has_old:
            lines.append("• ⚠️ Старый OAuth-формат — перенастройте через `/setup wiki`")
        folder = wiki.get("folder", "")
        lines.append(f"• Папка: `{folder}`" if folder else "• Папка: не указана")
        lines.extend([
            f"• ID организации: `{wiki.get('org_id', 'не указан') or 'не указан'}`",
            f"• Режим публикации: {mode_labels.get(mode, mode)}",
            "",
            "Для проверки используйте `/setup wiki test` или `/wiki test`.",
        ])
        await message.answer("\n".join(lines), parse_mode=ParseMode.MARKDOWN)

    elif section == "db":
        db_config = get_db_config(user_id)
        if not db_config:
            await message.answer(
                "🗄️ **PostgreSQL не настроен.**\n\n"
                "Используйте `/setup db` для настройки.",
                parse_mode=ParseMode.MARKDOWN,
            )
            return
        status = "✅ **Подключено**" if db.DB_POOL else "⏸️ **Не подключено**"
        await message.answer(
            "🗄️ **Настройки PostgreSQL**\n\n"
            f"• Хост: `{db_config.get('host', '?')}`\n"
            f"• Порт: `{db_config.get('port', 5432)}`\n"
            f"• БД: `{db_config.get('name', '?')}`\n"
            f"• Пользователь: `{db_config.get('user', '?')}`\n"
            f"• Пароль: {'✅ задан' if db_config.get('password') else '❌ не задан'}\n"
            f"• Статус: {status}\n\n"
            "Для проверки: `/setup db test`",
            parse_mode=ParseMode.MARKDOWN,
        )

    else:
        await message.answer(
            f"❌ Неизвестная секция: `{section}`.\n\n"
            "Доступно: `all`, `account`, `ai`, `wiki`, `db`.",
            parse_mode=ParseMode.MARKDOWN,
        )



# ── Команда /setup ai test ─────────────────────────────────

async def _cmd_setup_ai_test(message: Message):
    """Тестирует текущее AI-подключение."""
    user_id = message.from_user.id
    ai_config = get_ai_config(user_id)

    if not ai_config:
        await message.answer(
            "❌ **AI не настроен.**\n\n"
            "Используйте `/setup ai` чтобы настроить нейросеть.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    endpoint = ai_config.get("endpoint", "")
    api_key = ai_config.get("api_key", "")
    model = ai_config.get("model", "")

    await message.answer(
        f"⏳ Тестирую подключение к **{escape_md_simple(model)}**...\n"
        f"🔗 `{endpoint}`",
        parse_mode=ParseMode.MARKDOWN,
    )

    result = await _test_ai_connection(endpoint, api_key, model)
    await message.answer(
        f"🧪 **Результат теста AI**\n\n"
        f"🔗 Endpoint: `{endpoint}`\n"
        f"📝 Модель: `{model}`\n\n"
        f"{escape_md_simple(result)}",
        parse_mode=ParseMode.MARKDOWN,
    )

@dp.message(Command("setup"))
async def cmd_setup_start(message: Message, state: FSMContext, command: CommandObject):
    """Начинает настройку. /setup init — сброс и настройка заново.
       /setup, /setup email|db|ai — меню с кнопками-параметрами и флагами 🔴/🟡/🟢."""

    # Проверяем аргумент
    if command.args:
        arg = command.args.strip().lower()

        if arg == "init":
            await _start_init(message, state)
            return

        if arg.startswith("show"):
            await _cmd_setup_show(message, arg)
            return

        if arg == "ai":
            # Меню нейросети с флагами параметров
            await message.answer(
                _setup_section_text(message.from_user.id, "ai"),
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=_setup_section_keyboard(message.from_user.id, "ai"),
            )
            return

        if arg == "ai test":
            # Тестирование AI-подключения
            await _cmd_setup_ai_test(message)
            return

        if arg == "wiki test":
            # Тестирование подключения к Яндекс Вики
            await cmd_setup_wiki_test(message)
            return

        if arg == "wiki":
            # Перенаправляем на настройку Яндекс Вики
            await cmd_setup_wiki(message, state)
            return

        if arg.startswith("wiki org "):
            # Установка ID организации для Яндекс Вики
            org_id = arg[len("wiki org "):].strip()
            if not org_id:
                await message.answer(
                    "⚠️ Укажите ID организации.\n"
                    "Пример: `/setup wiki org bpf1234567890abcdef`",
                    parse_mode=ParseMode.MARKDOWN,
                )
                return
            user_id = message.from_user.id
            wiki_config = get_wiki_config(user_id)
            if not wiki_config or not wiki_config.get("authorized_key"):
                await message.answer(
                    "❌ **Сначала настройте Яндекс Вики через `/setup wiki`.**",
                    parse_mode=ParseMode.MARKDOWN,
                )
                return
            # Обновляем org_id
            wiki_config["org_id"] = org_id
            users = _load_users()
            key = str(user_id)
            if key in users:
                users[key]["wiki"] = wiki_config
                _save_users(users)
            await message.answer(
                f"✅ **ID организации сохранён:** `{org_id}`\n\n"
                "Проверьте подключение: `/wiki test`",
                parse_mode=ParseMode.MARKDOWN,
            )
            return

        if arg.startswith("wiki folder"):
            # Slug папки (раздела) в Яндекс Вики для публикации
            folder = arg[len("wiki folder"):].strip()
            if not folder:
                await message.answer(
                    "⚠️ Укажите slug папки.\n"
                    "Пример: `/setup wiki folder hr_meetings`\n\n"
                    "Slug — это идентификатор раздела в URL Яндекс Вики.\n"
                    "Например, для страницы `https://wiki.yandex.ru/hr_meetings/`\n"
                    "slug будет `hr_meetings`.",
                    parse_mode=ParseMode.MARKDOWN,
                )
                return
            user_id = message.from_user.id
            wiki_config = get_wiki_config(user_id)
            if not wiki_config or not wiki_config.get("authorized_key"):
                await message.answer(
                    "❌ **Сначала настройте Яндекс Вики через `/setup wiki`.**",
                    parse_mode=ParseMode.MARKDOWN,
                )
                return
            wiki_config["folder"] = folder
            users = _load_users()
            key = str(user_id)
            if key in users:
                users[key]["wiki"] = wiki_config
                _save_users(users)
            await message.answer(
                f"✅ **Папка Wiki сохранена:** `{folder}`\n\n"
                "Новые саммари будут публиковаться в этом разделе.\n"
                "Проверьте: `/setup show wiki`",
                parse_mode=ParseMode.MARKDOWN,
            )
            return

        if arg.startswith("wiki mode"):
            # Смена режима публикации в Wiki
            mode = arg[len("wiki mode"):].strip()
            if mode not in ("auto", "button", "off"):
                await message.answer(
                    "⚠️ **Неверный режим.**\n"
                    "Используйте: `/setup wiki mode auto|button|off`\n\n"
                    "• `auto` — сразу после AI саммари публикуется в Wiki\n"
                    "• `button` — под саммари кнопка «📤 В Wiki»\n"
                    "• `off` — публикация в Wiki отключена",
                    parse_mode=ParseMode.MARKDOWN,
                )
                return
            user_id = message.from_user.id
            wiki_config = get_wiki_config(user_id)
            if not wiki_config or not wiki_config.get("authorized_key"):
                await message.answer(
                    "❌ **Сначала настройте Яндекс Вики через `/setup wiki`.**",
                    parse_mode=ParseMode.MARKDOWN,
                )
                return
            # Обновляем mode
            wiki_config["mode"] = mode
            users = _load_users()
            key = str(user_id)
            if key in users:
                users[key]["wiki"] = wiki_config
                _save_users(users)
            mode_labels = {"auto": "🚀 Авто (сразу в Wiki)", "button": "📤 По кнопке", "off": "⏸️ Выключено"}
            await message.answer(
                f"✅ **Режим публикации в Wiki:** {escape_md_simple(mode_labels.get(mode, mode))}\n\n"
                f"Текущий режим: `/setup show wiki`",
                parse_mode=ParseMode.MARKDOWN,
            )
            return

        if arg == "db":
            # Настройка PostgreSQL (только для администратора)
            user_id = message.from_user.id
            if user_id != db.ADMIN_USER_ID:
                await message.answer("❌ Команда только для администратора.")
                return
            await message.answer(
                _setup_section_text(user_id, "db"),
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=_setup_section_keyboard(user_id, "db"),
            )
            return

        if arg == "db test":
            # Тест подключения к PostgreSQL
            user_id = message.from_user.id
            if user_id != db.ADMIN_USER_ID:
                await message.answer("❌ Команда только для администратора.")
                return
            if not db.DB_POOL:
                await message.answer(
                    "❌ **PostgreSQL не подключён.**\n"
                    "Настройте через `/setup db`.",
                    parse_mode=ParseMode.MARKDOWN,
                )
                return
            try:
                async with db.DB_POOL.acquire() as conn:
                    ver = await conn.fetchval("SELECT version()")
                    uptime = await conn.fetchval("SELECT pg_postmaster_start_time()")
                await message.answer(
                    "🗄️ **PostgreSQL: тест подключения**\n\n"
                    f"✅ **Подключение работает**\n"
                    f"📊 **Версия:** `{ver}`\n"
                    f"🕒 **Запущен с:** `{uptime}`\n"
                    f"🔗 `hr.hunttech.ru:5432/hunttech_protocols`",
                    parse_mode=ParseMode.MARKDOWN,
                )
            except Exception as e:
                await message.answer(f"❌ **Ошибка:** {escape_md_simple(e)}", parse_mode=ParseMode.MARKDOWN)
            return

        if arg == "email test":
            # Тест SMTP и IMAP
            user_id = message.from_user.id
            config = get_user_config(user_id)
            if not config:
                await message.answer("❌ **Почта не настроена.** Сначала выполните `/setup` или `/init`.",
                                     parse_mode=ParseMode.MARKDOWN)
                return
            cfg = {
                "sender": config.get("login") or config.get("email"),
                "password": config.get("password"),
                "smtp_host": _smtp_host_for(config),
                "smtp_port": 465,
                "imap_host": config.get("server", "imap.yandex.ru"),
                "imap_port": 993,
            }
            progress = await message.answer("🔄 Проверяю SMTP и IMAP...")
            results = await test_email_connections(cfg, timeout=15)
            await progress.edit_text(
                "📧 **Результаты проверки:**\n\n" + "\n".join(r.short for r in results),
                parse_mode=ParseMode.MARKDOWN,
            )
            return

        if arg == "email show":
            config = get_user_config(message.from_user.id)
            if not config:
                await message.answer("❌ **Почта не настроена.**", parse_mode=ParseMode.MARKDOWN)
                return
            cfg = {
                "sender": config.get("login") or config.get("email", ""),
                "password": config.get("password", ""),
                "smtp_host": _smtp_host_for(config),
                "smtp_port": 465,
                "imap_host": config.get("server", "imap.yandex.ru"),
                "imap_port": 993,
            }
            await message.answer(format_email_config(cfg), parse_mode=ParseMode.MARKDOWN)
            return

        if arg == "db stat":
            # Статистика данных в БД (только для администратора)
            user_id = message.from_user.id
            if user_id != db.ADMIN_USER_ID:
                await message.answer("❌ Команда только для администратора.")
                return
            if not db.DB_POOL:
                await message.answer(
                    "❌ **PostgreSQL не подключён.**\n"
                    "Настройте через `/setup db`.",
                    parse_mode=ParseMode.MARKDOWN,
                )
                return
            try:
                async with db.DB_POOL.acquire() as conn:
                    cnt_m = await conn.fetchval("SELECT COUNT(*) FROM meetings")
                    cnt_s = await conn.fetchval("SELECT COUNT(*) FROM summary_log")
                    cnt_m_today = await conn.fetchval(
                        "SELECT COUNT(*) FROM meetings WHERE DATE(received_at) = CURRENT_DATE"
                    )
                    cnt_s_today = await conn.fetchval(
                        "SELECT COUNT(*) FROM summary_log WHERE DATE(created_at) = CURRENT_DATE"
                    )
                    last_m = await conn.fetch(
                        "SELECT id, subject, received_at, summary_created_at "
                        "FROM meetings ORDER BY received_at DESC LIMIT 5"
                    )
                    by_day = await conn.fetch(
                        "SELECT DATE(received_at) AS day, COUNT(*) AS cnt "
                        "FROM meetings GROUP BY DATE(received_at) ORDER BY day DESC LIMIT 7"
                    )
                    users_who_generated = await conn.fetch(
                        "SELECT DISTINCT user_id FROM summary_log ORDER BY user_id"
                    )
                lines = [
                    "🗄️ **PostgreSQL: статистика данных**\n",
                    f"📝 **Всего встреч:** {cnt_m}",
                    f"🧠 **Всего саммари:** {cnt_s}",
                    f"📅 **За сегодня:** {cnt_m_today} встреч, {cnt_s_today} саммари",
                ]
                if by_day:
                    lines.append("")
                    lines.append("**📆 По дням (последние 7):**")
                    for r in by_day:
                        lines.append(f"  • {r['day']}: {r['cnt']} встреч")
                if last_m:
                    lines.append("")
                    lines.append("**🆕 Последние встречи:**")
                    for r in last_m:
                        sid = r['summary_created_at']
                        status = "✅ саммари" if sid else "⏳ без саммари"
                        lines.append(f"  • #{r['id']} `{r['subject'][:40]}` — {status}")
                if users_who_generated:
                    lines.append("")
                    lines.append(f"👤 **Генерировали саммари:** {len(users_who_generated)} пользователей")
                await message.answer("\n".join(lines), parse_mode=ParseMode.MARKDOWN)
            except Exception as e:
                await message.answer(f"❌ **Ошибка:** {escape_md_simple(e)}", parse_mode=ParseMode.MARKDOWN)
            return

        if arg == "email":
            # Меню почты с флагами параметров
            await message.answer(
                _setup_section_text(message.from_user.id, "email"),
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=_setup_section_keyboard(message.from_user.id, "email"),
            )
            return

        if arg in ("imap", "login", "user", "password", "pass"):
            # Одношаговая настройка отдельного поля почты
            # Без FSM-диалога — только запрос значения и сохранение
            field = "imap" if arg == "imap" else \
                    "login" if arg in ("login", "user") else \
                    "password"
            await state.update_data(field=field)
            config = get_user_config(message.from_user.id)
            current = ""
            if config and config.get(field):
                current = f"\n\nТекущее значение: `{config[field][:20]}...`" if field == "password" else f"\n\nТекущее значение: `{config[field]}`"
            await message.answer(
                f"{_single_field_prompt(field)}{current}\n\n"
                "или `/skip` — оставить текущее значение:",
                parse_mode=ParseMode.MARKDOWN,
            )
            await state.set_state(SetupSingleField.value)
            return

    # /setup без аргументов — главное меню с разделами и флагами
    await message.answer(
        _setup_root_text(message.from_user.id),
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=_setup_root_keyboard(message.from_user.id),
    )


def _setup_skip_done_keyboard() -> "ReplyKeyboardMarkup":
    """Нижнее меню при вводе серверов/пароля в /setup email:
    «Оставить прежнее» — подтвердить текущее/авто-значение (как /skip);
    «Готово» — завершить настройку и сохранить (выйти из FSM).
    «Изменить» — просто ввести новое значение с клавиатуры."""
    from aiogram.types import KeyboardButton, ReplyKeyboardMarkup

    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Оставить прежнее"), KeyboardButton(text="Готово")],
        ],
        resize_keyboard=True,
    )


async def _finish_email_setup_early(message: Message, state: FSMContext) -> None:
    """Досрочное завершение /setup email (кнопка «Готово»): сохраняет
    введённые в state значения, недостающие поля берёт из текущего конфига.
    Проверка IMAP/SMTP не выполняется (не все поля введены) — статус остаётся 🟡."""
    from aiogram.types import ReplyKeyboardRemove

    data = await state.get_data()
    user_id = message.from_user.id
    config = get_user_config(user_id) or {}
    email = data.get("email") or config.get("email", "")
    server = data.get("server") or config.get("server", "")
    login = data.get("login") or config.get("login", "")
    password = data.get("password") or config.get("password", "")
    save_user_config(user_id, email, server, login, password)
    await state.clear()
    await message.answer(
        "✅ Настройки почты сохранены (без проверки подключения).\n"
        "Чтобы выполнить проверку SMTP/IMAP — пройдите `/setup email` до конца "
        "или `/setup` → «🧪 Проверить».",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=ReplyKeyboardRemove(),
    )
    await message.answer("🔧 Главное меню:", reply_markup=_main_menu_keyboard())


@dp.message(SetupState.email)
async def setup_email(message: Message, state: FSMContext):
    """
    Шаг 1: Email.
    Проверка через библиотечную validate_email().
    /skip — оставить текущее значение.
    """
    text = message.text.strip()
    config = get_user_config(message.from_user.id) or {}

    if text.lower() in ("/skip", "-"):
        email_val = config.get("email", "")
        if not email_val:
            await message.answer(
                "⚠️ Текущий email не задан — введите адрес или начните заново: `/setup`.",
                parse_mode=ParseMode.MARKDOWN,
            )
            return
    else:
        if not text:
            await message.answer("⚠️ Email не может быть пустым. Введите адрес электронной почты:")
            return
        err = validate_email(text)
        if err:
            await message.answer(
                f"⚠️ **{escape_md_simple(err)}**\n\n"
                "Пример правильного адреса: `ivan@example.ru`\n"
                "Email должен содержать `@` и домен (например, `.ru`, `.com`).\n\n"
                "Введите email ещё раз (или `/skip` — оставить текущий):",
                parse_mode=ParseMode.MARKDOWN,
            )
            return
        email_val = text
    await state.update_data(email=email_val)

    # Автоопределение серверов по домену email (imap/pop3/smtp).
    # Заполняет пустые поля → кнопки настроек получают 🟡 (заполнено, не проверено).
    detected = _auto_fill_mail_servers(message.from_user.id, email_val)
    if detected:
        config = get_user_config(message.from_user.id) or {}
        detected_note = (
            "✨ Определил серверы по домену:\n"
            f"• IMAP: `{detected['imap']}`\n"
            f"• POP3: `{detected['pop3']}`\n"
            f"• SMTP: `{detected['smtp']}`\n\n"
            "Можно нажать `/skip` — сервер уже подставлен.\n\n"
        )
    else:
        detected_note = ""

    current = config.get("server") or "не задан"
    await message.answer(
        f"✅ Email: `{email_val}`\n\n"
        f"{detected_note}"
        f"**IMAP-сервер** ({escape_md_simple(current)}):\n"
        "Введите адрес IMAP-сервера\n"
        "(например: `imap.yandex.ru`, `imap.mail.ru`)\n"
        "или `/skip` — оставить текущий:",
        parse_mode=ParseMode.MARKDOWN,
    )
    await state.set_state(SetupState.server)


@dp.message(SetupState.server)
async def setup_server(message: Message, state: FSMContext):
    """
    Шаг 2: IMAP-сервер.
    Проверка через библиотечную validate_hostname().
    /skip — оставить текущее значение.
    """
    text = message.text.strip()
    config = get_user_config(message.from_user.id) or {}

    # Кнопка «Готово» — досрочное завершение настройки
    if text == "Готово":
        await _finish_email_setup_early(message, state)
        return

    if text.lower() in ("/skip", "-") or text == "Оставить прежнее":
        server = config.get("server", "")
        if not server:
            await message.answer(
                "⚠️ Текущий IMAP-сервер не задан — введите адрес или начните заново: `/setup`.",
                parse_mode=ParseMode.MARKDOWN,
            )
            return
    else:
        if not text:
            await message.answer("⚠️ IMAP-сервер не может быть пустым. Введите адрес сервера:")
            return
        err = validate_hostname(text)
        if err:
            await message.answer(
                f"⚠️ **{escape_md_simple(err)}**\n\n"
                "Пример правильного адреса: `imap.yandex.ru`\n"
                "Имя сервера должно содержать домен.\n\n"
                "Введите адрес IMAP-сервера ещё раз (или `/skip` — оставить текущий):",
                parse_mode=ParseMode.MARKDOWN,
            )
            return
        server = text
    await state.update_data(server=server)

    current = config.get("login") or "не задан"
    await message.answer(
        f"✅ IMAP-сервер: `{server}`\n\n"
        f"**Логин** ({escape_md_simple(current)}):\n"
        "Введите логин для подключения к IMAP\n"
        "(обычно это полный email-адрес)\n"
        "или `/skip` — оставить текущий:",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=ReplyKeyboardRemove(),
    )
    await state.set_state(SetupState.login)


@dp.message(SetupState.login)
async def setup_login(message: Message, state: FSMContext):
    """
    Шаг 3: Логин.
    /skip — оставить текущее значение.
    """
    text = message.text.strip()
    config = get_user_config(message.from_user.id) or {}

    if text.lower() in ("/skip", "-"):
        login = config.get("login", "")
        if not login:
            await message.answer(
                "⚠️ Текущий логин не задан — введите логин или начните заново: `/setup`.",
                parse_mode=ParseMode.MARKDOWN,
            )
            return
    else:
        if not text:
            await message.answer("⚠️ Логин не может быть пустым. Введите логин:")
            return
        login = text
    await state.update_data(login=login)

    current = "••••••••" if config.get("password") else "не задан"
    await message.answer(
        f"✅ Логин: `{login}`\n\n"
        f"**Пароль** ({escape_md_simple(current)}):\n"
        "Введите пароль приложения для IMAP\n"
        "(для Яндекса — создайте пароль приложения в настройках почты)\n"
        "или `/skip` — оставить текущий:",
        parse_mode=ParseMode.MARKDOWN,
    )
    await state.set_state(SetupState.password)


@dp.message(SetupState.password)
async def setup_password(message: Message, state: FSMContext):
    """
    Шаг 4: Пароль приложения.
    После ввода проверяем IMAP-подключение. Если всё ОК — сохраняем.
    /skip — оставить текущий пароль.
    """
    text = message.text.strip()
    config = get_user_config(message.from_user.id) or {}

    if text.lower() in ("/skip", "-"):
        password = config.get("password", "")
        if not password:
            await message.answer(
                "⚠️ Текущий пароль не задан — введите пароль приложения или начните заново: `/setup`.",
                parse_mode=ParseMode.MARKDOWN,
            )
            return
    else:
        if not text:
            await message.answer("⚠️ Пароль не может быть пустым. Введите пароль приложения:")
            return
        err = validate_password(text)
        if err:
            await message.answer(
                f"⚠️ **{escape_md_simple(err)}**\n\nВведите пароль приложения (минимум 4 символа):",
                parse_mode=ParseMode.MARKDOWN,
            )
            return
        password = text

    data = await state.get_data()
    email = data.get("email", "")
    server = data.get("server", "")
    login = data.get("login", "")
    user_id = message.from_user.id

    # Проверяем IMAP и SMTP — через библиотеку
    cfg = {
        "sender": login or email,
        "password": password,
        "smtp_host": _smtp_host_for(config, server),
        "smtp_port": 465,
        "imap_host": server,
        "imap_port": 993,
    }
    status = await message.answer("🔄 Шаг 5: Проверяю SMTP и IMAP...")
    results = await test_email_connections(cfg, timeout=15)

    has_error = any(not r.success for r in results if r.service in ("SMTP", "IMAP"))
    if has_error and not any("нет данных" in r.message for r in results):
        details = "\n".join(r.short for r in results)
        await status.edit_text(
            f"❌ **Ошибка подключения:**\n\n{escape_md_simple(details)}\n\n"
            "Попробуйте ещё раз:\n"
            "• Убедитесь, что IMAP включён в настройках почты\n"
            "• Проверьте логин и пароль\n\n"
            "Начните заново: `/setup`",
            parse_mode=ParseMode.MARKDOWN,
        )
        await state.clear()
        return

    # Сохраняем настройки
    save_user_config(user_id, email, server, login, password)
    # Успешная проверка — зелёный флаг для почты
    users_flag = _load_users()
    if str(user_id) in users_flag:
        users_flag[str(user_id)]["email_checked"] = True
        _save_users(users_flag)
    await state.clear()

    report = "\n".join(r.short for r in results)
    await status.edit_text(
        f"✅ **Настройка завершена!**\n\n"
        f"{format_email_config(cfg)}\n\n"
        f"{escape_md_simple(report)}",
        parse_mode=ParseMode.MARKDOWN,
    )

    # Автоматически показываем справку — чтобы новый пользователь
    # сразу видел, какие команды доступны.
    await message.answer(_help_text(), parse_mode=ParseMode.MARKDOWN)

    # Спрашиваем, хочет ли пользователь настроить AI для Саммари
    await message.answer(
        "🤖 Хотите настроить подключение к нейросети?\n"
        "Это нужно, чтобы кнопка «Саммари» работала.",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="🤖 Да, настроить AI", callback_data="ai_after_setup:yes"),
                InlineKeyboardButton(text="🚫 Нет", callback_data="ai_after_setup:no"),
            ]
        ]),
    )


# ═══════════════════════════════════════════════════════════════════
# FSM-ХЕНДЛЕР: ОДНОШАГОВАЯ НАСТРОЙКА ПОЛЯ ПОЧТЫ
# ═══════════════════════════════════════════════════════════════════
# Позволяет изменить одно поле (email/imap/login/password) без
# повторного ввода всех остальных.


@dp.message(SetupSingleField.value)
async def setup_single_field(message: Message, state: FSMContext):
    """Сохраняет одно поле настройки (email/imap/login/password | db:host.. | ai:endpoint..).
       /skip — оставить текущее значение.
       После ввода значения — кнопки «✅ Подтвердить / ✏️ Редактировать / 🚫 Отмена»."""
    text = message.text.strip()

    data = await state.get_data()
    section = data.get("section", "email")
    field = data.get("field", "email")
    user_id = message.from_user.id

    # /skip — оставить текущее значение (сохраняем сразу, подтверждение не нужно)
    if text.lower() in ("/skip", "-"):
        users = _load_users()
        key = str(user_id)
        if key not in users:
            users[key] = {}
        cfg = users[key] if section == "email" else users[key].setdefault(section, {})
        value = cfg.get(field, "")
        if not value:
            await message.answer(
                f"⚠️ Текущее значение `{field}` не задано — введите его или начните заново.",
                parse_mode=ParseMode.MARKDOWN,
            )
            return
        _apply_single_field(user_id, section, field, value, skipped=True)
        await message.answer(
            f"✅ **{SETUP_FIELD_LABELS.get(field, escape_md_simple(field))}** сохранён (оставлено прежнее значение): `{value[:20]}...`"
            if field in ("password", "api_key") and len(value) > 20
            else f"✅ **{SETUP_FIELD_LABELS.get(field, escape_md_simple(field))}** сохранён (оставлено прежнее значение): `{value}`",
            parse_mode=ParseMode.MARKDOWN,
        )
        if section in SETUP_SECTIONS:
            await message.answer(
                _setup_section_text(user_id, section),
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=_setup_section_keyboard(user_id, section),
            )
        return

    if not text:
        await message.answer("⚠️ Значение не может быть пустым. Введите снова:")
        return

    # Запоминаем введённое значение и показываем предпросмотр с кнопками
    await state.update_data(pending_value=text)
    masked = f"`{text[:20]}...`" if field in ("password", "api_key") else f"`{text}`"
    await message.answer(
        f"Вы ввели: {masked}\n\n"
        "Проверьте значение и выберите действие:",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Подтвердить", callback_data="setup_sf:confirm"),
                InlineKeyboardButton(text="✏️ Редактировать", callback_data="setup_sf:edit"),
            ],
            [
                InlineKeyboardButton(text="🚫 Отмена", callback_data="setup_sf:cancel"),
            ],
        ]),
    )


def _setup_test_keyboard(section: str) -> InlineKeyboardMarkup:
    """Нижние кнопки после верификации: Подтверждение / Редактирование / Отмена."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Подтверждение", callback_data=f"setup_menu:{section}"),
            InlineKeyboardButton(text="✏️ Редактирование", callback_data=f"setup_menu:{section}"),
        ],
        [
            InlineKeyboardButton(text="🚫 Отмена", callback_data="setup_menu:root"),
        ],
    ])


def _single_field_prompt(field: str) -> str:
    """Текст запроса значения для одношаговой настройки поля."""
    return {
        "email": "📧 **Email** — введите новый адрес электронной почты:",
        "server": "🔌 **IMAP-сервер** — введите адрес IMAP-сервера (например, `imap.yandex.ru`):",
        "login": "👤 **Логин** — введите логин для IMAP (обычно совпадает с email):",
        "password": "🔑 **Пароль** — введите пароль приложения для IMAP:",
        "host": "🖥️ **Хост** — введите адрес PostgreSQL-сервера:",
        "port": "🔢 **Порт** — введите порт PostgreSQL (например, 5432):",
        "name": "🗄️ **Имя БД** — введите имя базы данных:",
        "user": "👤 **Пользователь** — введите имя пользователя PostgreSQL:",
        "endpoint": "🔗 **Endpoint** — введите URL OpenAI-совместимого API:",
        "api_key": "🔑 **API Key** — введите ключ API:",
        "model": "📝 **Модель** — введите название модели:",
    }.get(field, f"✏️ **{escape_md_simple(field)}** — введите значение:")


def _apply_single_field(user_id: int, section: str, field: str, value: str, skipped: bool = False) -> dict | None:
    """Сохраняет значение поля в users.json. Если ввели email — пробует
    определить серверы (imap/pop3/smtp). Возвращает detected (dict) или None."""
    users = _load_users()
    key = str(user_id)
    if key not in users:
        users[key] = {}
    cfg = users[key] if section == "email" else users[key].setdefault(section, {})
    cfg[field] = value
    # Данные изменились (или прежние) — флаг «проверено» сбрасываем,
    # кроме /skip (значение не менялось, статус сохраняется)
    if not skipped:
        if section == "email":
            users[key]["email_checked"] = False
        else:
            cfg["checked"] = False
    # Если ввели email — пробуем определить серверы (imap/pop3/smtp)
    detected = None
    if section == "email" and field == "email" and not skipped:
        detected = _auto_fill_mail_servers(user_id, value)
    _save_users(users)
    return detected


@dp.callback_query(lambda c: c.data and c.data.startswith("setup_sf:"))
async def setup_single_field_callback(callback: CallbackQuery, state: FSMContext):
    """Кнопки подтверждения одношаговой настройки: confirm / edit / cancel."""
    action = callback.data.split(":", 1)[1]
    user_id = callback.from_user.id
    data = await state.get_data()
    section = data.get("section", "email")
    field = data.get("field", "email")
    await callback.answer()

    label = SETUP_FIELD_LABELS.get(field, escape_md_simple(field))

    if action == "confirm":
        value = data.get("pending_value", "")
        if not value:
            await callback.message.answer("⚠️ Значение не найдено. Начните настройку заново.",
                                          parse_mode=ParseMode.MARKDOWN)
            return
        detected = _apply_single_field(user_id, section, field, value)
        masked = f"`{value[:20]}...`" if field in ("password", "api_key") else f"`{value}`"
        auto_note = ""
        if detected:
            auto_note = (
                "\n\n✨ Определил серверы по домену:\n"
                f"• IMAP: `{detected['imap']}`\n"
                f"• POP3: `{detected['pop3']}`\n"
                f"• SMTP: `{detected['smtp']}`"
            )
        await callback.message.answer(
            f"✅ **{label}** сохранён: {masked}"
            f"{auto_note}\n\n"
            "Проверьте настройки: `/setup show all`",
            parse_mode=ParseMode.MARKDOWN,
        )
        await state.clear()
        if section in SETUP_SECTIONS:
            await callback.message.answer(
                _setup_section_text(user_id, section),
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=_setup_section_keyboard(user_id, section),
            )
        return

    if action == "edit":
        await state.update_data(pending_value=None)
        cfg, _, _ = _section_config(user_id, section)
        current = ""
        if cfg.get(field):
            secret = field in ("password", "api_key")
            current = f"\n\nТекущее значение: `{cfg[field][:20]}...`" if secret else f"\n\nТекущее значение: `{cfg[field]}`"
        await callback.message.answer(
            f"{_single_field_prompt(field)}{current}\n\n"
            "или `/skip` — оставить текущее значение:",
            parse_mode=ParseMode.MARKDOWN,
        )
        await state.set_state(SetupSingleField.value)
        return

    if action == "cancel":
        await state.clear()
        await callback.message.answer("🚫 Отменено. Значение не изменено.", parse_mode=ParseMode.MARKDOWN)
        if section in SETUP_SECTIONS:
            await callback.message.answer(
                _setup_section_text(user_id, section),
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=_setup_section_keyboard(user_id, section),
            )
        return


# ═══════════════════════════════════════════════════════════════════
# CALLBACK-ХЕНДЛЕРЫ МЕНЮ /setup (кнопки-параметры с флагами)
# ═══════════════════════════════════════════════════════════════════


@dp.callback_query(lambda c: c.data and c.data.startswith("setup_menu:"))
async def setup_menu_callback(callback: CallbackQuery):
    """Открывает меню секции (email/db/ai) или главное меню (root)."""
    section = callback.data.split(":", 1)[1]
    user_id = callback.from_user.id
    await callback.answer()

    if section == "root":
        text, kb = _setup_root_text(user_id), _setup_root_keyboard(user_id)
    else:
        if section == "db" and user_id != db.ADMIN_USER_ID:
            await callback.message.answer("❌ Команда только для администратора.")
            return
        text, kb = _setup_section_text(user_id, section), _setup_section_keyboard(user_id, section)

    await callback.message.edit_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)


@dp.callback_query(lambda c: c.data and c.data.startswith("setup_param:"))
async def setup_param_callback(callback: CallbackQuery, state: FSMContext):
    """Кнопка-параметр: начинает одношаговую настройку конкретного поля."""
    _, section, field = callback.data.split(":", 2)
    user_id = callback.from_user.id
    await callback.answer()

    if section == "db" and user_id != db.ADMIN_USER_ID:
        await callback.message.answer("❌ Команда только для администратора.")
        return

    await state.update_data(section=section, field=field)

    cfg, _, _ = _section_config(user_id, section)
    current = ""
    if cfg.get(field):
        secret = field in ("password", "api_key")
        current = f"\n\nТекущее значение: `{cfg[field][:20]}...`" if secret else f"\n\nТекущее значение: `{cfg[field]}`"

    await callback.message.answer(
        f"{_single_field_prompt(field)}{current}\n\n"
        "или `/skip` — оставить текущее значение:",
        parse_mode=ParseMode.MARKDOWN,
    )
    await state.set_state(SetupSingleField.value)


@dp.callback_query(lambda c: c.data and c.data.startswith("setup_full:"))
async def setup_full_callback(callback: CallbackQuery, state: FSMContext):
    """«Полная настройка»: запускает полный мастер секции."""
    section = callback.data.split(":", 1)[1]
    user_id = callback.from_user.id
    await callback.answer()

    if section == "email":
        config = get_user_config(user_id)
        current = config["email"] if config else "не задан"
        await callback.message.answer(
            "📧 **Настройка подключения к почте**\n\n"
            f"**Email** ({escape_md_simple(current)}):\n"
            "Введите адрес электронной почты:",
            parse_mode=ParseMode.MARKDOWN,
        )
        await state.set_state(SetupState.email)
        return

    if section == "db":
        if user_id != db.ADMIN_USER_ID:
            await callback.message.answer("❌ Команда только для администратора.")
            return
        config = get_db_config(user_id)
        current_host = (config or {}).get("host", "не задан")
        await callback.message.answer(
            "🗄️ **Настройка PostgreSQL**\n\n"
            f"Текущий хост: `{current_host}`\n\n"
            "Введите **хост** сервера PostgreSQL:",
            parse_mode=ParseMode.MARKDOWN,
        )
        await state.set_state(DbSetupState.host)
        return

    if section == "ai":
        await cmd_setup_ai(callback.message, state)
        return


@dp.callback_query(lambda c: c.data and c.data.startswith("setup_test:"))
async def setup_test_callback(callback: CallbackQuery):
    """«Проверить»: тест подключения секции; при успехе — зелёный флаг."""
    section = callback.data.split(":", 1)[1]
    user_id = callback.from_user.id
    await callback.answer()

    if section == "email":
        config = get_user_config(user_id)
        if not config:
            await callback.message.answer("❌ **Почта не настроена.** Нажмите «▶️ Полная настройка».",
                                           parse_mode=ParseMode.MARKDOWN)
            return
        cfg = {
            "sender": config.get("login") or config.get("email"),
            "password": config.get("password"),
            "smtp_host": _smtp_host_for(config),
            "smtp_port": 465,
            "imap_host": config.get("server", "imap.yandex.ru"),
            "imap_port": 993,
        }
        status = await callback.message.answer("🔄 Проверяю SMTP и IMAP...")
        results = await test_email_connections(cfg, timeout=15)
        has_error = any(not r.success for r in results if r.service in ("SMTP", "IMAP"))
        if has_error and not any("нет данных" in r.message for r in results):
            details = "\n".join(r.short for r in results)
            await status.edit_text(
                f"❌ **Ошибка подключения:**\n\n{escape_md_simple(details)}\n\n"
                "Исправьте параметры или нажмите «▶️ Полная настройка».",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=_setup_test_keyboard(section),
            )
            return
        users = _load_users()
        key = str(user_id)
        if key in users:
            users[key]["email_checked"] = True
            _save_users(users)
        report = "\n".join(r.short for r in results)
        await status.edit_text(
            f"✅ **Почта проверена!**\n\n{escape_md_simple(report)}\n\n"
            "Статус обновлён: 🟢 все параметры проверены.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=_setup_test_keyboard(section),
        )
        return

    if section == "db":
        if user_id != db.ADMIN_USER_ID:
            await callback.message.answer("❌ Команда только для администратора.")
            return
        if not db.DB_POOL:
            await callback.message.answer(
                "❌ **PostgreSQL не подключён.**\nНажмите «▶️ Полная настройка».",
                parse_mode=ParseMode.MARKDOWN,
            )
            return
        try:
            async with db.DB_POOL.acquire() as conn:
                ver = await conn.fetchval("SELECT version()")
            users = _load_users()
            key = str(user_id)
            if key in users and "db" in users[key]:
                users[key]["db"]["checked"] = True
                _save_users(users)
            await callback.message.answer(
                f"✅ **PostgreSQL проверен!**\n\n📊 Версия: `{ver}`\n\n"
                "Статус обновлён: 🟢 все параметры проверены.",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=_setup_test_keyboard(section),
            )
        except Exception as e:
            await callback.message.answer(
                f"❌ **Ошибка:** {escape_md_simple(e)}",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=_setup_test_keyboard(section),
            )
        return

    if section == "ai":
        ai_config = get_ai_config(user_id)
        if not ai_config:
            await callback.message.answer("❌ **AI не настроен.** Нажмите «▶️ Полная настройка».",
                                          parse_mode=ParseMode.MARKDOWN)
            return
        endpoint = ai_config.get("endpoint", "")
        api_key = ai_config.get("api_key", "")
        model = ai_config.get("model", "")
        status = await callback.message.answer(
            f"⏳ Тестирую подключение к **{model}**...\n🔗 `{endpoint}`",
            parse_mode=ParseMode.MARKDOWN,
        )
        result = await _test_ai_connection(endpoint, api_key, model)
        if result.startswith("✅"):
            users = _load_users()
            key = str(user_id)
            if key in users and "ai" in users[key]:
                users[key]["ai"]["checked"] = True
                _save_users(users)
        await status.edit_text(
            f"🧪 **Результат теста AI**\n\n🔗 Endpoint: `{endpoint}`\n📝 Модель: `{model}`\n\n{escape_md_simple(result)}",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=_setup_test_keyboard(section),
        )
        return


# ═══════════════════════════════════════════════════════════════════
# КЕШ КОНСПЕКТОВ (для кнопки Саммари)
# ═══════════════════════════════════════════════════════════════════
# Когда пользователь нажимает "Саммари #3" — у нас уже нет контекста
# того /list, который он вызвал 5 минут назад. Поэтому txt-содержимое
# каждого конспекта сохраняется в notes_cache.json сразу после /list.
# Кнопка загружает конспект из кеша и отправляет в нейросеть.

NOTES_CACHE_FILE = Path(__file__).parent / "notes_cache.json"


def _save_notes_cache(user_id: int, items: list):
    """Сохраняет конспекты (с txt-содержимым) в кеш после /list или /list_all."""
    cache = {}
    if NOTES_CACHE_FILE.exists():
        try:
            cache = json.loads(NOTES_CACHE_FILE.read_text(encoding="utf-8"))
        except Exception:
            cache = {}
    # Сериализуем datetime -> str, т.к. JSON не умеет в datetime
    serialized = []
    for item in items:
        dt, display, txt = item[0], item[1], item[2]
        entry = {
            "dt": dt.isoformat() if dt else "",
            "display": display,
            "txt": txt,
        }
        # Если есть imap_msg_id (6-й элемент) — сохраняем
        if len(item) >= 6:
            entry["imap_id"] = item[5]
        serialized.append(entry)
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
    for entry in serialized:
        dt = datetime.fromisoformat(entry["dt"]) if entry.get("dt") else datetime.now()
        imap_id = entry.get("imap_id", "")
        items.append((dt, entry["display"], entry["txt"], "", "", imap_id))
    return items


# ═══════════════════════════════════════════════════════════════════
# ФУНКЦИЯ ВЫЗОВА НЕЙРОСЕТИ (call_ai)
# ═══════════════════════════════════════════════════════════════════
# Универсальный вызов любого OpenAI-совместимого API.
# Поддерживает OpenRouter, OpenAI, DeepSeek, vLLM и т.д.

async def call_ai(user_id: int, system_prompt: str, user_text: str) -> str:
    """
    Вызывает нейросеть через OpenAI-совместимый API.
    Настройки (endpoint, api_key, model) берутся из users.json для user_id.
    
    Бизнес-правила:
    - Всегда возвращает строку (ответ или ошибку) — никогда не падает
    - Таймаут 120 секунд — длинные конспекты требуют времени
    - Для OpenRouter отправляет HTTP-Referer (требование их ToS)
    
    Returns:
        str — ответ нейросети или сообщение об ошибке, начинающееся с ❌
    """
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


# ── Тестирование AI-подключения ──────────────────────────

async def _test_ai_connection(endpoint: str, api_key: str, model: str) -> str:
    """Проверяет подключение к AI-провайдеру.
       Отправляет короткий запрос и возвращает отчёт.
    """
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    if "openrouter" in endpoint.lower():
        headers["HTTP-Referer"] = "https://t.me/hunttech_protocols_bot"
        headers["X-Title"] = "HunttechProtocolsBot"

    payload = {
        "model": model,
        "messages": [
            {"role": "user", "content": "Ответь одним словом: привет"},
        ],
        "max_tokens": 10,
    }

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.post(
                f"{endpoint}/chat/completions",
                headers=headers,
                json=payload,
            )
            if response.status_code == 200:
                result = response.json()
                reply = result["choices"][0]["message"]["content"]
                return f"✅ Подключение успешно!\nОтвет модели: «{escape_md_simple(reply.strip())}»"
            elif response.status_code == 401:
                return "❌ Ошибка авторизации (401). Проверьте API-ключ."
            elif response.status_code == 404:
                return "❌ Модель не найдена (404). Проверьте название модели."
            else:
                return f"❌ Ошибка API ({response.status_code}): {response.text[:300]}"
    except httpx.TimeoutException:
        return "❌ Таймаут: сервер не ответил за 15 секунд. Проверьте endpoint."
    except httpx.ConnectError:
        return "❌ Не удалось подключиться к серверу. Проверьте endpoint."
    except Exception as e:
        return f"❌ Ошибка: {e}"


# ═══════════════════════════════════════════════════════════════════
# КОМАНДА /setup_ai — НАСТРОЙКА НЕЙРОСЕТИ
# ═══════════════════════════════════════════════════════════════════

@dp.callback_query(lambda c: c.data and c.data.startswith("ai_after_setup:"))
async def ai_after_setup_callback(callback: CallbackQuery, state: FSMContext):
    """
    Обрабатывает ответ на вопрос «настроить AI?» после завершения IMAP setup.
    Если пользователь нажал "Да" — запускаем /setup_ai.
    """
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
        await callback.message.answer("🚫 Хорошо. Если захотите — `/setup_ai`",
                                      parse_mode=ParseMode.MARKDOWN)

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
    """
    Обрабатывает выбор AI-провайдера.
    Если выбран предустановленный — endpoint известен, просим только API key.
    Если "Свой вариант" — сначала endpoint, потом API key, потом модель.
    """
    provider_key = callback.data.split(":", 1)[1]
    await callback.answer()
    await callback.message.delete()

    if provider_key == "custom":
        # Свой endpoint: сохраняем пустой endpoint, просим ввести URL
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
        _provider_key=provider_key,
        _hint_model=provider.get("hint_model", "gpt-4o"),
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
    """
    Сохраняет API key и запрашивает модель.
    Если endpoint ещё не задан (custom путь) — сначала endpoint, потом модель.
    /skip — оставить текущий API key (из сохранённого AI-конфига).
    """
    text = message.text.strip()
    ai_config = get_ai_config(message.from_user.id) or {}

    if text.lower() in ("/skip", "-"):
        api_key = ai_config.get("api_key", "")
        if not api_key:
            await message.answer(
                "⚠️ Текущий API Key не задан — введите ключ или начните заново: `/setup_ai`.",
                parse_mode=ParseMode.MARKDOWN,
            )
            return
    else:
        if not text:
            await message.answer("⚠️ API Key не может быть пустым. Введите ключ:")
            return
        api_key = text

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
        await state.set_state(AiSetupState.model)
        await state.update_data(_need_endpoint=True)
        return

    await state.update_data(ai_api_key=api_key)
    await state.update_data(_need_endpoint=False)

    # Показываем список популярных моделей для выбранного провайдера
    data = await state.get_data()
    provider_key = data.get("_provider_key", "")
    hint = data.get("_hint_model", "gpt-4o")

    models_list = AI_MODELS_PER_PROVIDER.get(provider_key, [])
    models_section = ""
    if models_list:
        items = "\n".join(f"  • `{m}`" for m in models_list)
        models_section = f"\n📋 **Популярные модели {data.get('ai_provider_label', '')}:**\n{items}\n\n"

    await message.answer(
        f"📝 **Введите название модели**:\n\n"
        f"{models_section}"
        f"💡 Например: `{hint}`\n"
        f"_(или выберите из списка выше и просто скопируйте)_",
        parse_mode=ParseMode.MARKDOWN,
    )
    await state.set_state(AiSetupState.model)


@dp.message(AiSetupState.model)
async def ai_setup_model(message: Message, state: FSMContext):
    """
    Сохраняет модель и завершает настройку AI.
    Если был выбран custom путь — сначала получаем endpoint (через _need_endpoint).
    /skip — оставить текущее значение (модель или endpoint для custom-пути).
    """
    text = message.text.strip()
    ai_config = get_ai_config(message.from_user.id) or {}

    data = await state.get_data()
    need_endpoint = data.get("_need_endpoint", False)

    is_skip = text.lower() in ("/skip", "-")

    if need_endpoint:
        # Текущее сообщение — это endpoint (custom путь)
        if is_skip:
            endpoint = ai_config.get("endpoint", "")
            if not endpoint:
                await message.answer(
                    "⚠️ Текущий endpoint не задан — введите URL или начните заново: `/setup_ai`.",
                    parse_mode=ParseMode.MARKDOWN,
                )
                return
        else:
            if not text:
                await message.answer("⚠️ Название модели не может быть пустым. Введите модель:")
                return
            endpoint = text
        model = ""
        await state.update_data(ai_endpoint=endpoint, _need_endpoint=False)
        await message.answer(
            "📝 Введите **название модели**:\n\n"
            "Например: `gpt-4o`, `deepseek-chat`, `claude-sonnet-4`\n"
            "или `/skip` — оставить текущую:",
            parse_mode=ParseMode.MARKDOWN,
        )
        await state.set_state(AiSetupState.model)
        return

    if is_skip:
        model = ai_config.get("model", "")
        if not model:
            await message.answer(
                "⚠️ Текущая модель не задана — введите модель или начните заново: `/setup_ai`.",
                parse_mode=ParseMode.MARKDOWN,
            )
            return
    else:
        if not text:
            await message.answer("⚠️ Название модели не может быть пустым. Введите модель:")
            return
        model = text

    api_key = data.get("ai_api_key", "")
    endpoint = data.get("ai_endpoint", "")

    if not endpoint:
        await message.answer("❌ Ошибка: не указан endpoint. Начните заново: `/setup_ai`",
                             parse_mode=ParseMode.MARKDOWN)
        await state.clear()
        return

    user_id = message.from_user.id
    save_ai_config(user_id, endpoint, api_key, model)
    await state.clear()

    provider_label = data.get("ai_provider_label", "Пользовательский")
    await message.answer(
        f"⏳ Проверяю подключение к **{escape_md_simple(provider_label)}**...",
        parse_mode=ParseMode.MARKDOWN,
    )

    test_result = await _test_ai_connection(endpoint, api_key, model)

    if test_result.startswith("✅"):
        # Успешная проверка — зелёный флаг для AI
        users_flag = _load_users()
        if str(user_id) in users_flag and "ai" in users_flag[str(user_id)]:
            users_flag[str(user_id)]["ai"]["checked"] = True
            _save_users(users_flag)
        await message.answer(
            f"✅ **AI-настройки сохранены!**\n\n"
            f"🧩 Провайдер: `{provider_label}`\n"
            f"🔗 Endpoint: `{endpoint}`\n"
            f"📝 Модель: `{model}`\n\n"
            f"**{escape_md_simple(test_result)}**\n\n"
            "Теперь кнопка «Саммари» будет работать!",
            parse_mode=ParseMode.MARKDOWN,
        )
    else:
        await message.answer(
            f"⚠️ **AI-настройки сохранены**, но тест не прошёл:\n\n"
            f"🧩 Провайдер: `{provider_label}`\n"
            f"🔗 Endpoint: `{endpoint}`\n"
            f"📝 Модель: `{model}`\n\n"
            f"{escape_md_simple(test_result)}\n\n"
            "Проверьте ключ и модель. Введите `/setup ai` для перенастройки "
            "или `/setup show ai` для просмотра текущих настроек.",
            parse_mode=ParseMode.MARKDOWN,
        )


# ═══════════════════════════════════════════════════════════════════
# КОМАНДА /setup_wiki — НАСТРОЙКА YANDEX WIKI (IAM через JWT)
# ═══════════════════════════════════════════════════════════════════
# Бизнес-логика: Яндекс Вики — корпоративная база знаний.
# Пользователь создаёт авторизованный ключ (Authorized Key) сервисного аккаунта
# в Yandex Cloud Console (роль wiki.editor/wiki.admin).
# Бот создаёт JWT из ключа, обменивает на IAM-токен через iam.api.cloud.yandex.net
# и проверяет подключение к API Яндекс Вики.


@dp.message(Command("setup_wiki"))
async def cmd_setup_wiki(message: Message, state: FSMContext):
    """Начинает настройку Яндекс Вики: запрашивает JSON авторизованного ключа.
       Бизнес-правило: авторизованный ключ создаётся в Yandex Cloud Console
       для сервисного аккаунта с ролью wiki.editor.
       После ввода JSON бот создаёт JWT, получает IAM-токен и проверяет Wiki API."""
    config = get_wiki_config(message.from_user.id)
    current = "✅ задан" if config and config.get("authorized_key") else "не задан"
    await message.answer(
        "📚 **Настройка Яндекс Вики (IAM через JWT)**\n\n"
        "Яндекс Вики — корпоративная база знаний. "
        "Сюда можно публиковать саммари совещаний.\n\n"
        f"🔑 **Авторизованный ключ** ({current})\n\n"
        "Вставьте **содержимое JSON-файла** авторизованного ключа\n"
        "сервисного аккаунта Яндекc Облака.\n\n"
        "**Как получить:**\n"
        "1️⃣ **Yandex Cloud Console** → Сервисные аккаунты\n"
        "2️⃣ Выберите сервисный аккаунт (с ролью **`wiki.editor`**)\n"
        "3️⃣ Вкладка **Ключи** → **Создать авторизованный ключ**\n"
        "4️⃣ Скачается JSON-файл — откройте и скопируйте ВСЁ его содержимое\n"
        "5️⃣ Вставьте сюда одной строкой или как есть (многострочный JSON)\n\n"
        "JSON должен содержать поля: `id`, `service_account_id`, `private_key`",
        parse_mode=ParseMode.MARKDOWN,
    )
    await state.set_state(WikiSetupState.api_key)


@dp.message(WikiSetupState.api_key)
async def setup_wiki_authorized_key(message: Message, state: FSMContext):
    """Принимает JSON авторизованного ключа, создаёт JWT, получает IAM-токен, тестирует.
       Бизнес-правило: authorized_key — JSON с полями id, service_account_id, private_key."""
    raw = message.text.strip()
    if not raw:
        await message.answer("⚠️ JSON авторизованного ключа не может быть пустым. Вставьте содержимое JSON-файла:")
        return

    # Пробуем распарсить JSON
    import json
    try:
        key_json = json.loads(raw)
    except json.JSONDecodeError:
        await message.answer(
            "❌ **Не удалось распарсить JSON.**\n\n"
            "Убедитесь, что вы скопировали весь JSON-файл.\n"
            "JSON должен начинаться с `{` и заканчиваться на `}`.\n\n"
            "Введите `/setup wiki` чтобы попробовать снова.",
            parse_mode=ParseMode.MARKDOWN,
        )
        await state.clear()
        return

    # Проверяем обязательные поля
    if not key_json.get("id") or not key_json.get("service_account_id") or not key_json.get("private_key"):
        missing = []
        if not key_json.get("id"): missing.append("`id`")
        if not key_json.get("service_account_id"): missing.append("`service_account_id`")
        if not key_json.get("private_key"): missing.append("`private_key`")
        await message.answer(
            f"❌ **В JSON отсутствуют поля:** {', '.join(missing)}\n\n"
            "Убедитесь, что вы скачали именно **авторизованный ключ**\n"
            "(Authorized Key), а не API-ключ.\n\n"
            "Введите `/setup wiki` чтобы попробовать снова.",
            parse_mode=ParseMode.MARKDOWN,
        )
        await state.clear()
        return

    user_id = message.from_user.id
    status = await message.answer("🔄 Создаю JWT и получаю IAM-токен...")

    # Создаём JWT из ключа
    jwt_token = _create_jwt_from_authorized_key(key_json)
    if not jwt_token:
        await state.clear()
        await status.edit_text(
            "❌ **Не удалось создать JWT.**\n\n"
            "Возможно, приватный ключ повреждён или имеет неверный формат.\n"
            "Создайте новый авторизованный ключ в Yandex Cloud Console.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    await status.edit_text("🔄 JWT создан. Получаю IAM-токен...")

    # Получаем IAM-токен
    iam_token = await _get_yandex_iam_token_from_jwt(jwt_token)
    if not iam_token:
        await state.clear()
        await status.edit_text(
            "❌ **Не удалось получить IAM-токен.**\n\n"
            "Проверьте, что сервисный аккаунт существует и активен.\n"
            "Возможно, ключ был отозван. Подробности в логах бота.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    await status.edit_text("🔄 IAM-токен получен. Проверяю подключение к Яндекс Вики...")

    # Сохраняем ключ ДО теста — чтобы можно было перетестировать без повторного ввода
    # org_id не передаём — сохранится старый, если был
    save_wiki_config(user_id, json.dumps(key_json))

    # Проверяем подключение к Wiki API (используем org_id из только что сохранённого конфига)
    saved_org_id = get_wiki_config(user_id).get("org_id", "")
    report = await _test_wiki_connection(iam_token, saved_org_id)

    if report.startswith("❌"):
        await state.clear()
        org_hint = ""
        if not saved_org_id:
            org_hint = (
                "\n\n**💡 Не указан ID организации!**\n"
                "Найдите ID в Yandex Cloud Console:\n"
                "Организация → Управление организацией\n"
                "и введите `/setup wiki org <ID_организации>`\n"
            )
        await status.edit_text(
            f"❌ **IAM-токен получен, но подключение к Вики не прошло.**\n\n"
            f"{escape_md_simple(report)}"
            f"{org_hint}",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    await state.clear()
    await status.edit_text(
        f"✅ **Яндекс Вики настроена!**\n\n"
        f"{escape_md_simple(report)}",
        parse_mode=ParseMode.MARKDOWN,
    )


# ═══════════════════════════════════════════════════════════════════
@dp.message(Command("setup_wiki_test"))
async def cmd_setup_wiki_test(message: Message):
    """Проверяет подключение к Яндекс Вики через /setup wiki test.
       Получает свежий OAuth-токен и тестирует API."""
    await cmd_wiki_test(message)


# ═══════════════════════════════════════════════════════════════════
# FSM-ДИАЛОГ: /setup db — НАСТРОЙКА POSTGRESQL
# ═══════════════════════════════════════════════════════════════════
# Бизнес-правило: только администратор (AlekseyAnanyev, ID 272980897)
# может настраивать подключение к PostgreSQL.
# Пароль в БД никогда не показывается в чате.


@dp.message(DbSetupState.host)
async def setup_db_host(message: Message, state: FSMContext):
    """Шаг 1: хост PostgreSQL. /skip — оставить текущее значение."""
    text = message.text.strip()
    db_config = get_db_config(message.from_user.id) or {}

    if text.lower() in ("/skip", "-"):
        host = db_config.get("host", "")
        if not host:
            await message.answer(
                "⚠️ Текущий хост не задан — введите хост или начните заново: `/setup db`.",
                parse_mode=ParseMode.MARKDOWN,
            )
            return
    else:
        if not text:
            await message.answer("⚠️ Хост не может быть пустым. Введите хост:")
            return
        host = text
    await state.update_data(host=host)

    current_port = db_config.get("port") or 5432
    await message.answer(
        f"✅ Хост: `{host}`\n\n"
        f"Введите **порт** (текущий: `{current_port}`, по умолчанию 5432)\n"
        "или `/skip` — оставить текущий:",
        parse_mode=ParseMode.MARKDOWN,
    )
    await state.set_state(DbSetupState.port)


@dp.message(DbSetupState.port)
async def setup_db_port(message: Message, state: FSMContext):
    """Шаг 2: порт PostgreSQL. /skip — оставить текущее значение."""
    raw = message.text.strip()
    db_config = get_db_config(message.from_user.id) or {}

    if raw.lower() in ("/skip", "-"):
        port = db_config.get("port") or 5432
    else:
        try:
            port = int(raw) if raw else 5432
        except ValueError:
            await message.answer("⚠️ Порт должен быть числом. Введите число (например, 5432):")
            return
        if port < 1 or port > 65535:
            await message.answer("⚠️ Порт должен быть от 1 до 65535. Введите снова:")
            return
    await state.update_data(port=port)

    current_name = db_config.get("name") or "не задано"
    await message.answer(
        f"✅ Порт: `{port}`\n\n"
        f"Введите **имя базы данных** (текущее: `{current_name}`)\n"
        "или `/skip` — оставить текущее:",
        parse_mode=ParseMode.MARKDOWN,
    )
    await state.set_state(DbSetupState.name)


@dp.message(DbSetupState.name)
async def setup_db_name(message: Message, state: FSMContext):
    """Шаг 3: имя базы данных. /skip — оставить текущее значение."""
    text = message.text.strip()
    db_config = get_db_config(message.from_user.id) or {}

    if text.lower() in ("/skip", "-"):
        name = db_config.get("name", "")
        if not name:
            await message.answer(
                "⚠️ Текущее имя БД не задано — введите имя или начните заново: `/setup db`.",
                parse_mode=ParseMode.MARKDOWN,
            )
            return
    else:
        if not text:
            await message.answer("⚠️ Имя БД не может быть пустым. Введите имя БД:")
            return
        name = text
    await state.update_data(name=name)

    current_user = db_config.get("user") or "не задано"
    await message.answer(
        f"✅ База данных: `{name}`\n\n"
        f"Введите **имя пользователя** (текущее: `{current_user}`)\n"
        "или `/skip` — оставить текущее:",
        parse_mode=ParseMode.MARKDOWN,
    )
    await state.set_state(DbSetupState.user)


@dp.message(DbSetupState.user)
async def setup_db_user(message: Message, state: FSMContext):
    """Шаг 4: пользователь PostgreSQL. /skip — оставить текущее значение."""
    text = message.text.strip()
    db_config = get_db_config(message.from_user.id) or {}

    if text.lower() in ("/skip", "-"):
        user = db_config.get("user", "")
        if not user:
            await message.answer(
                "⚠️ Текущее имя пользователя не задано — введите имя или начните заново: `/setup db`.",
                parse_mode=ParseMode.MARKDOWN,
            )
            return
    else:
        if not text:
            await message.answer("⚠️ Имя пользователя не может быть пустым. Введите имя:")
            return
        user = text
    await state.update_data(user=user)

    has_pw = bool(db_config.get("password"))
    current_pw = "••••••••" if has_pw else "не задан"
    await message.answer(
        f"✅ Пользователь: `{user}`\n\n"
        f"Введите **пароль** (текущий: `{current_pw}`)\n"
        "или `/skip` — оставить текущий:",
        parse_mode=ParseMode.MARKDOWN,
    )
    await state.set_state(DbSetupState.password)


@dp.message(DbSetupState.password)
async def setup_db_password(message: Message, state: FSMContext):
    """Шаг 5: пароль PostgreSQL.
       После ввода всех параметров — тестируем подключение.
       Пароль не показывается в логах. /skip — оставить текущий пароль."""
    text = message.text.strip()
    db_config = get_db_config(message.from_user.id) or {}

    if text.lower() in ("/skip", "-"):
        password = db_config.get("password", "")
        if not password:
            await message.answer(
                "⚠️ Текущий пароль не задан — введите пароль или начните заново: `/setup db`.",
                parse_mode=ParseMode.MARKDOWN,
            )
            return
    else:
        if not text:
            await message.answer("⚠️ Пароль не может быть пустым. Введите пароль:")
            return
        password = text

    data = await state.get_data()
    host = data["host"]
    port = data["port"]
    name = data["name"]
    user = data["user"]

    user_id = message.from_user.id

    # Сообщаем о тесте
    status_msg = await message.answer(
        f"🔄 Тестирую подключение к `{host}:{port}/{name}`...",
        parse_mode=ParseMode.MARKDOWN,
    )

    # Пробуем подключиться
    success, msg = await db.apply_config(host, port, name, user, password)

    if success:
        # Сохраняем конфиг
        save_db_config(user_id, host, port, name, user, password)
        # Успешная проверка — зелёный флаг для БД
        users_flag = _load_users()
        if str(user_id) in users_flag and "db" in users_flag[str(user_id)]:
            users_flag[str(user_id)]["db"]["checked"] = True
            _save_users(users_flag)
        await status_msg.edit_text(msg, parse_mode=ParseMode.MARKDOWN)
    else:
        await status_msg.edit_text(
            f"{escape_md_simple(msg)}\n\n"
            "Проверьте параметры и введите `/setup db` заново.",
            parse_mode=ParseMode.MARKDOWN,
        )

    await state.clear()


# ═══════════════════════════════════════════════════════════════════
# КОМАНДА /wiki_test — ПРОВЕРКА ПОДКЛЮЧЕНИЯ К YANDEX WIKI
# ═══════════════════════════════════════════════════════════════════
# Бизнес-логика: пользователь хочет убедиться, что wiki настроена
# и работает, прежде чем публиковать туда страницы.


# ── Команда /wiki test / /wiki stat ──────────────────────────

@dp.message(Command("wiki"))
async def cmd_wiki(message: Message, command: CommandObject):
    """Обрабатывает /wiki test и /wiki stat как синонимы /wiki_test и /wikistat."""
    if command.args and command.args.strip().lower() in ("test", "stat"):
        await cmd_wiki_test(message)
    else:
        await message.answer(
            "📚 **Яндекс Вики**\n\n"
            "• `/wiki test` — проверить подключение\n"
            "• `/wiki stat` — то же самое\n"
            "• `/setup wiki` — настроить подключение (IAM через JWT)\n"
            "• `/setup wiki test` — проверить подключение",
            parse_mode=ParseMode.MARKDOWN,
        )


@dp.message(Command("wiki_test", "wikistat"))
async def cmd_wiki_test(message: Message):
    """Проверяет подключение к Яндекс Вики и показывает отчёт.
       Получает свежий IAM-токен через API-ключ сервисного аккаунта."""
    user_id = message.from_user.id
    wiki_config = get_wiki_config(user_id)
    if not wiki_config:
        await message.answer(
            "❌ Яндекс Вики не настроена.\n\n"
            "Используйте `/setup wiki` чтобы настроить:\n"
            "1️⃣ Yandex Cloud Console → Сервисные аккаунты\n"
            "2️⃣ Создать авторизованный ключ с ролью wiki.editor",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    # Проверяем наличие любого формата ключа
    has_auth_key = bool(wiki_config.get("authorized_key"))
    has_api_key = bool(wiki_config.get("api_key"))
    has_old_oauth = bool(wiki_config.get("client_id") and wiki_config.get("client_secret"))

    if not has_auth_key and not has_api_key and not has_old_oauth:
        await message.answer(
            "❌ **Ключ не найден.**\n\n"
            "Перенастройте Яндекс Вики через `/setup wiki`.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    status = await message.answer("🔄 Получаю свежий токен...")

    # Получаем токен через универсальную функцию
    token = await _get_wiki_token(wiki_config)
    if not token:
        await status.edit_text(
            "❌ **Не удалось получить токен.**\n\n"
            "Проверьте авторизованный ключ. Возможно, он отозван.\n"
            "Перенастройте через `/setup wiki`.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    await status.edit_text("🔄 Проверяю подключение к Яндекс Вики...")
    try:
        report = await _test_wiki_connection(
            token,
            wiki_config.get("org_id", ""),
        )
        await status.edit_text(report, parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        await status.edit_text(
            f"❌ Ошибка: {e}",
            parse_mode=ParseMode.MARKDOWN,
        )


# ═══════════════════════════════════════════════════════════════════
# НЕИЗВЕСТНЫЕ КОМАНДЫ
# ═══════════════════════════════════════════════════════════════════
# Этот хендлер должен быть последним — он ловит всё, что не обработали
# другие команды.

@dp.message()
async def unknown_command(message: Message):
    """
    Ловит любые сообщения, начинающиеся с /, которые не обработали
    другие хендлеры. Показывает подсказку /help.
    """

    # ── Нижнее меню (стандарт HuntTech): кнопки ЭКВИВАЛЕНТНЫ командам ──
    # Нажатия ReplyKeyboard приходят как обычные текстовые сообщения,
    # поэтому маршрутизация кнопок живёт здесь.
    normalized = (message.text or "").strip().lower()
    cmd = SIDE_MENU_ALIASES.get(normalized)
    if cmd:
        if cmd == "notes":
            await cmd_get_notes(message)
        elif cmd == "prompt":
            ctx = dp.fsm.get_context(bot=bot, chat_id=message.chat.id,
                                     user_id=message.from_user.id)
            await cmd_list_prompts(message, ctx, CommandObject(command="prompt", args=None))
        elif cmd == "setup":
            ctx = dp.fsm.get_context(bot=bot, chat_id=message.chat.id,
                                     user_id=message.from_user.id)
            await cmd_setup_start(message, ctx, CommandObject(command="setup", args=None))
        elif cmd == "help":
            await cmd_help(message, None)
        return

    if message.text and message.text.startswith("/") and len(message.text) > 1:
        logger.info("Неизвестная команда: %s", message.text.split()[0])
        await message.answer(
            "Неизвестная команда. Введите /help для подсказки."
        )


# ═══════════════════════════════════════════════════════════════════
# ЗАПУСК
# ═══════════════════════════════════════════════════════════════════

# ── Changelog после перезапуска (однократный вывод изменений кода) ────────
# Тот же механизм, что в @hrm_hunttech_docs_bot: fingerprint кода против
# маркера last_startup.json. Выводится после приветствия админу, один раз.

def _hermes_home() -> Path:
    """~/.hermes (или HERMES_HOME) — база state-файлов ботов."""
    env = os.environ.get("HERMES_HOME")
    if env:
        return Path(env)
    return Path.home() / ".hermes"


def _code_fingerprint() -> str:
    """SHA-256 от отсортированных имён + содержимого bot.py.
    Меняется при любом изменении алгоритмов бота."""
    import hashlib

    source = Path(__file__).resolve()
    digest = hashlib.sha256()
    digest.update(source.name.encode("utf-8"))
    try:
        digest.update(source.read_bytes())
    except OSError:
        pass
    return digest.hexdigest()


def _git_head(repo: Path) -> str:
    import subprocess

    try:
        result = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=5,
        )
        return result.stdout.strip() if result.returncode == 0 else ""
    except Exception:
        return ""


def _git_changelog(repo: Path, prev_head: str) -> str:
    """Краткое описание изменений кода между prev_head и текущим HEAD.
    При пустом prev_head (первый запуск с changelog-механикой) — последние
    коммиты. Пусто, если git недоступен или изменений нет."""
    import subprocess

    head = _git_head(repo)
    if not head or head == prev_head:
        return ""
    try:
        if prev_head:
            args = ["git", "-C", str(repo), "log", "--oneline", "--no-decorate", f"{prev_head}..{head}"]
        else:
            args = ["git", "-C", str(repo), "log", "--oneline", "--no-decorate", "-15", head]
        result = subprocess.run(args, capture_output=True, text=True, timeout=5)
        if result.returncode != 0:
            return ""
        return result.stdout.strip()
    except Exception:
        return ""


def _changelog_since_last_start() -> str | None:
    """Возвращает текст «Что изменилось в боте» (или None) — однократно.
    Сравнивает fingerprint кода с сохранённым маркером last_startup.json.
    Если код не менялся с прошлого запуска — None (повторно не выводим)."""
    marker_path = _hermes_home() / "hunttechprotocols" / "last_startup.json"
    fingerprint = _code_fingerprint()

    prev = {}
    if marker_path.is_file():
        try:
            prev = json.loads(marker_path.read_text(encoding="utf-8"))
        except Exception:
            prev = {}

    # Код не менялся — ничего не выводим (однократность)
    if prev.get("fingerprint") == fingerprint:
        return None

    # Описание изменений — git log репо (если доступен), иначе кратко о факте
    repo = Path(__file__).resolve().parent
    log = _git_changelog(repo, str(prev.get("git_head") or "")) if repo.is_dir() else ""
    if log:
        lines = ["🆕 Что изменилось в боте (после перезапуска):", ""]
        for line in log.splitlines()[:15]:
            lines.append(f"• {line}")
        body = "\n".join(lines)
    else:
        body = "🆕 Алгоритмы бота обновлены.\n\nКраткое описание изменений: см. git-историю проекта."

    # Обновляем маркер — чтобы при следующем запуске без изменений не выводить повторно
    try:
        marker_path.parent.mkdir(parents=True, exist_ok=True)
        marker_path.write_text(
            json.dumps({"fingerprint": fingerprint, "git_head": _git_head(repo)}, ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception:
        logger.warning("Could not write changelog marker", exc_info=True)
    return body


async def main():
    logger.info("🤖 Бот конспектов встреч запускается...")

    # ── Инициализация PostgreSQL ─────────────────────────────
    # Сначала пробуем загрузить сохранённый конфиг администратора
    admin_db = get_db_config(db.ADMIN_USER_ID)
    if admin_db:
        logger.info("📦 Найден сохранённый конфиг PostgreSQL, подключаюсь...")
        success, msg = await db.apply_config(
            admin_db["host"], admin_db["port"],
            admin_db["name"], admin_db["user"], admin_db["password"],
        )
        if success:
            logger.info("✅ PostgreSQL подключён: %s:%s/%s",
                        admin_db["host"], admin_db["port"], admin_db["name"])
        else:
            logger.warning("❌ Не удалось подключиться к PostgreSQL: %s", msg)
    else:
        logger.info("📦 Сохранённый конфиг PostgreSQL не найден")

    # ── Фоновая проверка почты каждые 5 минут ───────────────
    async def check_new_conspects():
        """Проверяет новые конспекты для всех пользователей, у кого
           настроена почта. Отправляет уведомление в Telegram."""
        while True:
            try:
                await asyncio.sleep(300)  # 5 минут
                users = _load_users()
                if not users:
                    continue

                for uid_str in users:
                    try:
                        user_id = int(uid_str)
                        config = users[uid_str]
                        if not config.get("email") or not config.get("password"):
                            continue

                        header, items = fetch_new_notes(user_id)
                        if items:
                            # Фильтруем те, о которых уже уведомляли
                            notified = _get_notified_comms_for_user(user_id)
                            new_notifications = []
                            for item in items:
                                dt, display, txt = item[0], item[1], item[2]
                                uid = f"{dt.timestamp()}:{display}"
                                if uid not in notified:
                                    new_notifications.append(item)

                            if not new_notifications:
                                continue

                            for idx, item in enumerate(new_notifications, 1):
                                dt, display = item[0], item[1]
                                date_str = dt.strftime("%d.%m.%Y %H:%M")
                                text = (
                                    f"🔔 **Новый конспект встречи!**\n\n"
                                    f"**{idx}.** {escape_md_simple(display)}\n"
                                    f"📅 {date_str}"
                                )
                                try:
                                    await bot.send_message(
                                        chat_id=user_id,
                                        text=text,
                                        parse_mode=ParseMode.MARKDOWN,
                                    )
                                except Exception as e:
                                    logger.error(
                                        "Не удалось отправить уведомление user %s: %s",
                                        uid_str, e,
                                    )
                            # Сохраняем в кеш для кнопки Саммари
                            notified_ids = [f"{item[0].timestamp()}:{item[1]}" for item in new_notifications]
                            _mark_notified(user_id, notified_ids)
                            _save_notes_cache(user_id, items)

                    except Exception as e:
                        logger.error(
                            "Ошибка фоновой проверки для user %s: %s", uid_str, e,
                        )
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Ошибка в фоновом цикле проверки почты: %s", e)

    # Запускаем фоновую задачу
    asyncio.create_task(check_new_conspects())

    # ── Устанавливаем нижнее меню команд ──────────────────────
    try:
        cmds = [
            BotCommand(command="start", description="🚀 Запустить бота"),
            BotCommand(command="help", description="❓ Справка и команды"),
            BotCommand(command="prompt", description="📋 Список промптов"),
            BotCommand(command="setup", description="🔧 Настройка почты и AI"),
            BotCommand(command="user", description="👤 Информация о пользователе"),
        ]
        await bot.set_my_commands(commands=cmds)
        logger.info("✅ Нижнее меню команд установлено (%d команд)", len(cmds))
    except Exception as e:
        logger.warning("⚠️ Не удалось установить меню команд: %s", e)

    # ── Приветствие администратору при каждом старте ─────────
    # (стандарт HuntTech, эталон — offer: plain text, parse_mode=None,
    # reply_markup — актуальная нижняя клавиатура: Telegram кэширует
    # ReplyKeyboard по чату, иначе после изменения состава кнопок
    # пользователь продолжает видеть старые (мёртвые) кнопки)
    if _master_admin_id:
        try:
            ai_cfg = get_ai_config(_master_admin_id)
            ai_model = (ai_cfg or {}).get("model") or "не настроен"
            startup_text = (
                "🚀 HuntTech Protocols Bot\n"
                "━━━━━━━━━━━━━━━━━━━━━━\n"
                "✅ Бот запущен и готов к работе!\n"
                f"🤖 AI: {ai_model}\n"
                "━━━━━━━━━━━━━━━━━━━━━━"
            )
            await bot.send_message(
                chat_id=_master_admin_id,
                text=startup_text,
                parse_mode=None,
                reply_markup=_main_menu_keyboard(),
            )
            logger.info("Startup message sent to admin %s", _master_admin_id)
        except Exception as e:
            logger.warning("Startup message failed for admin %s: %s", _master_admin_id, e)

    # ── Однократный вывод изменений алгоритмов с прошлого перезапуска ──
    # plain text (parse_mode=None): escape_md_simple рассчитан на MarkdownV2,
    # а Legacy MARKDOWN его backslash-экранирования не рендерит — сырые
    # звёздочки/слеши «пролезали» в чат (стандарт HuntTech: приветствия и
    # changelog — только plain text, как в offer-боте)
    try:
        changelog = await asyncio.to_thread(_changelog_since_last_start)
        if changelog and _master_admin_id:
            await bot.send_message(
                chat_id=_master_admin_id,
                text=changelog,
                parse_mode=None,
                reply_markup=_main_menu_keyboard(),
            )
            logger.info("Changelog message sent to admin %s", _master_admin_id)
    except Exception as e:
        logger.warning("Changelog message failed: %s", e)

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())