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
import hashlib
import imaplib
import email
import logging
import httpx
import json
import re
import time
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
from hunttech_bot_common.ai import (
    UsageTracker,
    create_fallback_ai_client,
    create_multi_fallback_ai_client,
    OPENROUTER_FREE_MODELS,
)
from hunttech_bot_common.ai.usage import UsageRecord, estimate_cost
from hunttech_bot_common.telegram import escape_md_simple as _escape_md_v2  # noqa: F401


def _md(text: str) -> str:
    """Экранирует спецсимволы Markdown v1 (ParseMode.MARKDOWN): _ * [ ] ` \\.

    ВАЖНО: бот использует ParseMode.MARKDOWN (v1), а не MarkdownV2.
    В v1 точки, тире, скобки, ! и т.п. экранировать НЕ нужно — иначе
    даты отображаются как «13\\.08\\.2026» (лишние бэкслеши)."""
    return re.sub(r"([_*\[\]`\\])", r"\\\1", str(text))
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
from aiogram.types import BotCommand, BotCommandScopeChat, Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery, ChatMemberUpdated
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


def save_wiki_config(user_id: int, authorized_key: str, org_id: str = "", mode: str = "", folder: str = "", collab_id: str = "", oauth_token: str = "", prompt_slug: str = "", routing: dict | None = None):
    """Сохраняет настройки Яндекс Вики: авторизованный ключ сервисного аккаунта и ID организации.
       Бизнес-правило: authorized_key — это JSON с полями id, service_account_id, private_key.
       IAM-токен получается свежим через JWT при каждом запросе к Wiki API.
       org_id сохраняется только если передан непустой; если не передан — сохраняется старый.
       mode: 'auto' (автопубликация), 'button' (по кнопке), 'off' (выкл) — по умолчанию 'off'.
       folder: slug раздела Wiki, куда публиковать страницы (например, 'hr_meetings').
       collab_id: ID организации Яндекс 360 для заголовка X-Collab-Org-Id (обязателен для API).
       oauth_token: OAuth-токен пользователя (y0_...) — рабочий способ авторизации, если
       сервисный аккаунт не привязан к организации Вики.
       prompt_slug: slug страницы с промптом для AI-расшифровки конспектов.
       routing: маппинг «префикс темы письма → slug подраздела Вики» для автопубликации."""
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
        "collab_id": collab_id or old_wiki.get("collab_id", ""),
        "oauth_token": oauth_token or old_wiki.get("oauth_token", ""),
        "prompt_slug": prompt_slug or old_wiki.get("prompt_slug", ""),
        "routing": routing if routing is not None else old_wiki.get("routing", {}),
    }
    # Очищаем старые поля, если были
    users[key]["wiki"].pop("api_key", None)
    users[key]["wiki"].pop("client_id", None)
    users[key]["wiki"].pop("client_secret", None)
    _save_users(users)


def get_wiki_config(user_id: int) -> dict | None:
    """Возвращает настройки Яндекс Вики или None.
       Без настроек wiki команды /wiki_test, /setup wiki test и публикация не работают."""
    config = get_user_config(user_id)
    if config and "wiki" in config:
        return config["wiki"]
    return None


# ── Целевая группа для публикации протоколов ──────────────────
# Бизнес-правило (владелец, 08.2026): после публикации протокола в Вики
# бот может одним нажатием опубликовать его дайджест в группе, где он
# является администратором. Группа запоминается автоматически, когда
# пользователь добавляет бота в группу и выдаёт ему права администратора.

def save_group_target(user_id: int, chat_id: int, title: str) -> None:
    """Сохраняет группу, где бот — администратор (для кнопки «📢 Опубликовать в группе»)."""
    users = _load_users()
    key = str(user_id)
    if key not in users:
        users[key] = {}
    users[key]["group"] = {"chat_id": chat_id, "title": title, "ts": time.time()}
    _save_users(users)


def get_group_target(user_id: int) -> dict | None:
    """Возвращает целевую группу пользователя (chat_id, title) или None."""
    config = get_user_config(user_id)
    if config and config.get("group"):
        return config["group"]
    return None


def remove_group_target(user_id: int) -> None:
    """Удаляет целевую группу (бота исключили/лишили прав администратора)."""
    users = _load_users()
    key = str(user_id)
    if key in users and "group" in users[key]:
        del users[key]["group"]
        _save_users(users)


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
       Приоритет: oauth_token пользователя (y0_...) → authorized_key (JWT → IAM-токен)
       → client_id/client_secret (устаревший OAuth-флоу, fallback).
       Возвращает токен (str) или None."""
    # OAuth-токен пользователя — рабочий способ, когда сервисный аккаунт
    # не привязан к организации Вики (hasCloudOrg=false)
    oauth_token = wiki_config.get("oauth_token")
    if oauth_token:
        return oauth_token

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
        # Быстрый предфильтр по теме: сначала тянем только заголовок,
        # полное тело (с вложениями) — лишь для подходящих писем.
        # Без этого при сотнях непрочитанных писем каждая проверка
        # выкачивает весь ящик и надолго блокирует event loop бота.
        try:
            typ_h, hdr_data = server.fetch(msg_id, "(BODY.PEEK[HEADER.FIELDS (SUBJECT)])")
            if typ_h == "OK" and hdr_data and hdr_data[0]:
                raw_hdr = hdr_data[0][1] if isinstance(hdr_data[0], (tuple, list)) and len(hdr_data[0]) > 1 else b""
                subject_hdr = decode_mime_header(email.message_from_bytes(raw_hdr).get("Subject", ""))
                if subject_hdr and not subject_hdr.lower().startswith(SUBJECT_FILTER.lower()):
                    continue  # тема не подходит — полное тело не тянем
        except Exception:
            pass  # заголовок не получить — пробуем полное тело ниже (старое поведение)

        # BODY.PEEK[] — единственный правильный способ читать письмо
        # не снимая флаг UNSEEN. UID в том же FETCH — стабильный
        # идентификатор письма (см. ниже).
        typ, msg_data = server.fetch(msg_id, "(UID BODY.PEEK[])")
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
        # UID — СТАБИЛЬНЫЙ идентификатор письма. Seq-номер (позиция в INBOX)
        # меняется при любом изменении ящика (новое письмо, expunge), и
        # тогда кнопка «Да, в корзину» могла удалить НЕ то письмо
        # (08.2026: в корзину ушли CDEK-документы и рассылка Яндекса).
        # Для IMAP-операций (прочитано/корзина) используем только UID.
        imap_msg_id = ""
        try:
            meta = msg_data[0][0] if isinstance(msg_data[0], (tuple, list)) else b""
            m_uid = re.search(rb"UID (\d+)", meta if isinstance(meta, bytes) else str(meta).encode())
            if m_uid:
                imap_msg_id = m_uid.group(1).decode()
        except Exception:
            imap_msg_id = ""
        if not imap_msg_id:
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
    # OAuth-токен пользователя (y0_...) передаётся в формате OAuth,
    # IAM-токен — в формате Bearer.
    auth_scheme = "OAuth" if iam_token.startswith("y0_") else "Bearer"
    headers = {
        "Authorization": f"{auth_scheme} {iam_token}",
        "Content-Type": "application/json",
    }
    # Если указан collab_id организации — добавляем заголовок (обязателен для API)
    if org_id:
        headers["X-Collab-Org-Id"] = org_id

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
                report_parts.append(f"❌ **Ошибка API ({resp.status_code}):** {_md(resp.text[:200])}")
                all_ok = False
    except httpx.TimeoutException:
        report_parts.append("❌ **Таймаут:** Яндекс Вики не ответил за 15 секунд.")
        all_ok = False
    except Exception as e:
        report_parts.append(f"❌ **Ошибка подключения:** {_md(e)}")
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
            report_parts.append(f"⚠️ **Ошибка при получении страниц:** {_md(e)}")

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


def _slugify(title: str, max_len: int = 60) -> str:
    """Транслитерирует заголовок в slug для Яндекс Вики (латиница, дефисы)."""
    translit = {
        'а': 'a', 'б': 'b', 'в': 'v', 'г': 'g', 'д': 'd', 'е': 'e', 'ё': 'e',
        'ж': 'zh', 'з': 'z', 'и': 'i', 'й': 'y', 'к': 'k', 'л': 'l', 'м': 'm',
        'н': 'n', 'о': 'o', 'п': 'p', 'р': 'r', 'с': 's', 'т': 't', 'у': 'u',
        'ф': 'f', 'х': 'h', 'ц': 'ts', 'ч': 'ch', 'ш': 'sh', 'щ': 'sch',
        'ъ': '', 'ы': 'y', 'ь': '', 'э': 'e', 'ю': 'yu', 'я': 'ya',
    }
    out = []
    for ch in title.lower():
        if ch in translit:
            out.append(translit[ch])
        elif ch.isalnum():
            out.append(ch)
        elif out and out[-1] != '-':
            out.append('-')
    slug = ''.join(out).strip('-')
    while '--' in slug:
        slug = slug.replace('--', '-')
    return slug[:max_len].strip('-') or f"page-{int(datetime.now().timestamp())}"


def _wiki_page_url(wiki_config: dict, slug: str) -> str:
    """Собирает ссылку на страницу Яндекс Вики (с orgId для браузера).
       Единая точка — используется и при публикации, и в подтверждениях."""
    url = f"https://wiki.yandex.ru/{slug}"
    org_id = (wiki_config or {}).get("org_id", "")
    if org_id:
        url += f"?orgId={org_id}"
    return url


async def publish_to_wiki(title: str, content: str, wiki_config: dict, page_slug: str = "") -> tuple[bool, str]:
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
        return False, "❌ Не удалось получить токен для Яндекс Вики."

    # OAuth-токен пользователя (y0_...) передаётся в формате OAuth,
    # IAM-токен — в формате Bearer.
    auth_scheme = "OAuth" if token.startswith("y0_") else "Bearer"
    headers = {
        "Authorization": f"{auth_scheme} {token}",
        "Content-Type": "application/json",
    }
    # Для API обязателен заголовок X-Collab-Org-Id (ID организации Яндекс 360).
    # org_id (dir_id) используется только в ссылке на страницу.
    collab_id = wiki_config.get("collab_id", "")
    if collab_id:
        headers["X-Collab-Org-Id"] = collab_id

    if page_slug:
        # Готовый slug с путём подраздела (например, "раздел/имя-протокола")
        slug = page_slug
    else:
        # Авто-генерация: транслитерация заголовка + дата-время
        slug = f"{_slugify(title)}-{datetime.now().strftime('%Y%m%d-%H%M')}"
    payload = {
        "title": title,
        "slug": slug,
        "content": content,
    }
    # Если указана папка (slug родительского раздела) — добавляем parent.
    # Только для простых slug-ов без "/" (вложенность задаётся путём в page_slug).
    folder = wiki_config.get("folder", "")
    if folder and "/" not in folder:
        payload["parent"] = folder
    # org_id (dir_id) — для ссылки на страницу в браузере
    org_id = wiki_config.get("org_id", "")

    # ── УСИЛЕННОЕ ЛОГИРОВАНИЕ (этап разработки) ──
    logger.info("[WIKI-PUB] Публикация страницы: title=%r slug=%r len(content)=%d", title, slug, len(content or ""))

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{WIKI_API_BASE}/pages",
                headers=headers,
                json=payload,
            )
            # ── УСИЛЕННОЕ ЛОГИРОВАНИЕ: статус и тело ответа ──
            logger.info("[WIKI-PUB] POST /pages → %s (slug=%s)", resp.status_code, slug)
            if resp.status_code not in (200, 201):
                logger.warning("[WIKI-PUB] Тело ответа (до 800 симв.): %s", resp.text[:800])
            if resp.status_code in (200, 201):
                data = resp.json()
                page_slug = data.get("slug", "?")
                page_url = _wiki_page_url(wiki_config, page_slug)
                logger.info("[WIKI-PUB] Страница создана: id=%s slug=%s", data.get("id"), page_slug)
                return True, f"✅ Страница опубликована: {page_url}"
            elif resp.status_code == 401:
                logger.error("[WIKI-PUB] 401: токен недействителен (slug=%s)", slug)
                return False, "❌ Ошибка авторизации (401): IAM-токен недействителен."
            elif resp.status_code == 403:
                logger.error("[WIKI-PUB] 403: нет прав на создание (slug=%s)", slug)
                return False, (
                    "❌ Нет прав на создание страниц (403).\n"
                    "Проверьте, что сервисный аккаунт имеет роль `wiki.editor`."
                )
            else:
                return False, f"❌ Ошибка Wiki API ({resp.status_code}): {resp.text[:300]}"
    except httpx.TimeoutException:
        logger.error("[WIKI-PUB] Таймаут 30с (slug=%s)", slug)
        return False, "❌ Таймаут: Яндекс Вики не ответил за 30 секунд."
    except Exception as e:
        logger.error("[WIKI-PUB] Исключение (slug=%s): %s", slug, e, exc_info=True)
        return False, f"❌ Ошибка подключения к Wiki: {e}"


# ═══════════════════════════════════════════════════════════════════
# ФЛОУ: КОНСПЕКТ → ПРОМПТ ИЗ ВИКИ → AI → ПРОТОКОЛ В ПОДРАЗДЕЛ ВИКИ
# ═══════════════════════════════════════════════════════════════════
# Бизнес-правило (владелец, 08.2026):
# - В корне раздела «Совещания» лежит страница-промпт для расшифровки.
# - Бот читает промпт из Вики, отдаёт его в нейросеть вместе с текстом
#   конспекта и получает структурированный протокол.
# - Протокол публикуется в подраздел (определяется по префиксу темы
#   письма через маппинг wiki.routing).
# - Письма НИКОГДА не помечаются прочитанными (BODY.PEEK + снятие флага).


async def wiki_get_page_content(wiki_config: dict, slug: str) -> str:
    """Читает содержимое страницы Вики (поле content) через API.
       Возвращает пустую строку при ошибке/отсутствии содержимого."""
    token = await _get_wiki_token(wiki_config)
    if not token:
        logger.warning("[WIKI-READ] Нет токена для чтения страницы %s", slug)
        return ""
    auth_scheme = "OAuth" if token.startswith("y0_") else "Bearer"
    headers = {
        "Authorization": f"{auth_scheme} {token}",
        "Content-Type": "application/json",
    }
    collab_id = wiki_config.get("collab_id", "")
    if collab_id:
        headers["X-Collab-Org-Id"] = collab_id
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.get(
                f"{WIKI_API_BASE}/pages",
                headers=headers,
                params={"slug": slug, "fields": "content"},
            )
            if resp.status_code == 200:
                content = resp.json().get("content", "")
                logger.info("[WIKI-READ] GET %s → 200, len(content)=%d", slug, len(content or ""))
                return content
            logger.warning("[WIKI-READ] GET %s → HTTP %s (тело: %s)", slug, resp.status_code, resp.text[:300])
    except Exception as e:
        logger.error("[WIKI-READ] Исключение при чтении %s: %s", slug, e, exc_info=True)
    return ""


def wiki_extract_prompt(content: str) -> str:
    """Извлекает текст промпта из страницы Вики.
       Убирает макрос {% tree %} и заголовок «Промпт для нейросети»,
       если он есть; иначе возвращает весь текст страницы целиком."""
    if not content:
        logger.warning("[WIKI-PROMPT] Страница пустая — промпт не извлечён")
        return ""
    text = re.sub(r"\{%\s*tree\s*%\}", "", content)
    marker = re.search(r"Промпт\s+для\s+нейросети", text, re.IGNORECASE)
    if marker:
        text = text[marker.start():]
        text = re.sub(r"^[#*\s]*Промпт\s+для\s+нейросети[:*]*", "", text, flags=re.IGNORECASE)
    result = text.strip()
    logger.info("[WIKI-PROMPT] Извлечено %d символов промпта (маркер «Промпт для нейросети»: %s)",
                len(result), bool(marker))
    return result


def wiki_route_section(user_id: int, display: str) -> str:
    """Определяет подраздел Вики по префиксу темы письма.
       Правила — маппинг в конфиге wiki.routing:
       {«префикс темы»: «slug подраздела»} (проверка по startswith, без учёта регистра).
       Возвращает пустую строку, если подраздел не найден."""
    wiki = get_wiki_config(user_id) or {}
    routing = wiki.get("routing") or {}
    disp_low = display.lower()
    for prefix, slug in routing.items():
        if disp_low.startswith(prefix.lower()):
            logger.info("[WIKI-ROUTE] user=%s display=%r → подраздел=%s (правило %r)", user_id, display[:60], slug, prefix)
            return slug
    logger.warning("[WIKI-ROUTE] user=%s display=%r → подраздел НЕ НАЙДЕН (routing=%s)",
                   user_id, display[:60], routing)
    return ""


_MONTHS_RU = [
    "январь", "февраль", "март", "апрель", "май", "июнь",
    "июль", "август", "сентябрь", "октябрь", "ноябрь", "декабрь",
]


def _month_name(month: int) -> str:
    """Возвращает название месяца в именительном падеже (1..12)."""
    if 1 <= month <= 12:
        return _MONTHS_RU[month - 1]
    return ""


def _short_uid(uid: str) -> str:
    """Короткий ключ (16 hex) для callback_data.
       ВАЖНО: Telegram ограничивает callback_data 64 байтами — полный
       uid с кириллическим display не помещается (BUTTON_DATA_INVALID)."""
    return hashlib.md5(uid.encode("utf-8")).hexdigest()[:16]


async def wiki_page_exists(wiki_config: dict, slug: str) -> bool:
    """Проверяет, существует ли страница Вики с данным slug."""
    token = await _get_wiki_token(wiki_config)
    if not token:
        return False
    auth_scheme = "OAuth" if token.startswith("y0_") else "Bearer"
    headers = {"Authorization": f"{auth_scheme} {token}", "Content-Type": "application/json"}
    collab_id = wiki_config.get("collab_id", "")
    if collab_id:
        headers["X-Collab-Org-Id"] = collab_id
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.get(
                f"{WIKI_API_BASE}/pages", headers=headers, params={"slug": slug},
            )
            exists = resp.status_code == 200
            logger.info("[WIKI-EXISTS] GET %s → %s (exists=%s)", slug, resp.status_code, exists)
            return exists
    except Exception as e:
        logger.error("[WIKI-EXISTS] Исключение при проверке %s: %s", slug, e, exc_info=True)
    return False


async def _ensure_wiki_folder(wiki_config: dict, slug: str, title: str) -> bool:
    """Создаёт страницу-папку ({% tree %}), если она ещё не существует.
       Вики НЕ создаёт промежуточные папки автоматически при создании
       страницы с глубоким slug — без реальных папок страница не видна
       в дереве {% tree %} (папки года/месяца «пропадают»)."""
    if await wiki_page_exists(wiki_config, slug):
        logger.info("[WIKI-FOLDER] Папка уже существует: %s", slug)
        return True
    logger.info("[WIKI-FOLDER] Создаю папку: %s (title=%r)", slug, title)
    ok, msg = await publish_to_wiki(title, "{% tree %}", wiki_config, page_slug=slug)
    if not ok:
        logger.error("[WIKI-FOLDER] Не удалось создать папку %s: %s", slug, msg)
    return ok


class WikiProgress:
    """Живой список действий при расшифровке: ⏳ текущий → ✅ выполнен / ❌ ошибка."""

    def __init__(self, status_msg, user_id: int, display: str = ""):
        self.status_msg = status_msg
        self.user_id = user_id
        self.display = display
        self.steps: list[tuple[str, str]] = []  # (label, running|done|error)

    async def start(self, label: str) -> None:
        self.steps.append((label, "running"))
        await self._flush()

    async def done(self) -> None:
        if self.steps:
            self.steps[-1] = (self.steps[-1][0], "done")
        await self._flush()

    async def fail(self) -> None:
        if self.steps:
            self.steps[-1] = (self.steps[-1][0], "error")
        await self._flush()

    def render(self) -> str:
        lines = ["⚙️ **Обработка конспекта:**", ""]
        if self.display:
            lines.append(f"📄 {_md(self.display)}")
            lines.append("")
        for label, status in self.steps:
            if status == "done":
                lines.append(f"✅ {label}")
            elif status == "error":
                lines.append(f"❌ {label}")
            else:
                lines.append(f"⏳ {label}…")
        return "\n".join(lines)

    async def _flush(self) -> None:
        try:
            await self.status_msg.edit_text(self.render(), parse_mode=ParseMode.MARKDOWN)
        except Exception as e:
            logger.error("[PROGRESS] user=%s: не удалось обновить статус: %s", self.user_id, e)


async def process_conspect_to_wiki(user_id: int, item, progress: WikiProgress | None = None) -> tuple[bool, str, str, str]:
    """Полный флоу обработки конспекта для Вики (вызывается по кнопке):
       1) классифицируем конспект по префиксу темы → подраздел (wiki.routing);
       2) строим путь: {подраздел}/Конспекты/{год}/{месяц} и {подраздел}/Протоколы/{год}/{месяц};
       3) сохраняем ОРИГИНАЛ конспекта без изменений в папку конспектов;
       4) читаем промпт из корня подраздела (fallback: wiki.prompt_slug);
       5) расшифровываем конспект через нейросеть (call_ai);
       6) размещаем протокол в папке протоколов.
       Страницы именуются «{год}.{месяц}.{день}».
       Возвращает (ok: bool, сообщение для пользователя, текст саммари,
       ссылка на протокол в Вики)."""
    dt, display, txt_content = item[0], item[1], item[2]
    logger.info("[WIKI-FLOW] user=%s начал обработку: display=%r dt=%s txt_len=%d",
                user_id, display[:80], dt, len(txt_content or ""))
    if not txt_content:
        logger.warning("[WIKI-FLOW] user=%s: пустой txt в конспекте %r", user_id, display[:80])
        return False, "❌ В письме не найден текст конспекта (txt-вложение).", "", ""

    wiki = get_wiki_config(user_id)
    if not wiki or not wiki.get("oauth_token"):
        logger.warning("[WIKI-FLOW] user=%s: вики не настроена (oauth_token отсутствует)", user_id)
        return False, "❌ Яндекс Вики не настроена.", "", ""

    # 1) Подраздел (классификация по префиксу темы)
    if progress:
        await progress.start("Классификация конспекта по типу совещания")
    folder = wiki_route_section(user_id, display)
    if not folder:
        if progress:
            await progress.fail()
        return False, f"⚠️ Для «{_md(display)}» не задан подраздел Вики (проверьте wiki.routing).", "", ""
    if progress:
        await progress.done()

    # 2) Структура папок: {год} / {номер месяца название}
    year = dt.strftime("%Y")
    month = f"{dt.strftime('%m')} {_month_name(dt.month)}"
    page_name = dt.strftime("%Y.%m.%d")  # «2026.08.13»
    conspects_dir = f"{folder}/Конспекты/{year}/{month}"
    protocols_dir = f"{folder}/Протоколы/{year}/{month}"
    logger.info("[WIKI-FLOW] user=%s: folder=%s | year=%s | month=%r | page=%s",
                user_id, folder, year, month, page_name)

    # Материализуем папки: вики не создаёт промежуточные страницы
    # автоматически — без них год/месяц «пропадают» из дерева
    if progress:
        await progress.start("Создание папок года/месяца в Вики")
    for dir_path, dir_title in (
        (f"{folder}/Конспекты", "Конспекты"),
        (f"{folder}/Конспекты/{year}", year),
        (f"{folder}/Конспекты/{year}/{month}", month),
        (f"{folder}/Протоколы", "Протоколы"),
        (f"{folder}/Протоколы/{year}", year),
        (f"{folder}/Протоколы/{year}/{month}", month),
    ):
        if not await _ensure_wiki_folder(wiki, dir_path, dir_title):
            logger.error("[WIKI-FLOW] user=%s: не удалось создать папку %s", user_id, dir_path)
            if progress:
                await progress.fail()
            return False, f"❌ Не удалось создать папку {dir_path} в Вики.", "", ""
    if progress:
        await progress.done()

    # 3) Оригинал конспекта — без изменений
    if progress:
        await progress.start("Сохранение оригинала конспекта")
    conspect_slug = f"{conspects_dir}/{page_name}"
    if await wiki_page_exists(wiki, conspect_slug):
        ok1, msg1 = True, f"✅ Конспект уже был сохранён ранее: {_wiki_page_url(wiki, conspect_slug)}"
        logger.info("[WIKI-FLOW] user=%s: конспект уже существует: %s", user_id, conspect_slug)
    else:
        logger.info("[WIKI-FLOW] user=%s: сохраняю оригинал конспекта → %s", user_id, conspect_slug)
        ok1, msg1 = await publish_to_wiki(page_name, txt_content, wiki, page_slug=conspect_slug)
    if not ok1:
        if progress:
            await progress.fail()
        return False, msg1, "", ""
    if progress:
        await progress.done()

    # 4) Промпт: корень подраздела → fallback на wiki.prompt_slug
    if progress:
        await progress.start("Чтение промпта из Вики")
    prompt_slug = wiki.get("prompt_slug") or ""
    page_content = await wiki_get_page_content(wiki, folder)
    prompt_text = wiki_extract_prompt(page_content)
    if not prompt_text and prompt_slug and prompt_slug != folder:
        logger.info("[WIKI-FLOW] user=%s: промпта нет в %s, пробую fallback %s", user_id, folder, prompt_slug)
        page_content = await wiki_get_page_content(wiki, prompt_slug)
        prompt_text = wiki_extract_prompt(page_content)
    if not prompt_text:
        logger.error("[WIKI-FLOW] user=%s: промпт не найден (folder=%s prompt_slug=%r)", user_id, folder, prompt_slug)
        if progress:
            await progress.fail()
        return False, f"⚠️ Промпт не найден в папке {folder}.", "", ""
    if progress:
        await progress.done()
    logger.info("[WIKI-FLOW] user=%s: промпт получен (%d символов), запускаю AI...", user_id, len(prompt_text))

    # 5) AI-расшифровка по промпту
    if progress:
        await progress.start("AI-расшифровка по промпту")
    ai_text = (
        f"Конспект/стенограмма встречи «{display}» "
        f"от {dt.strftime('%d.%m.%Y')}:\n\n{txt_content}"
    )
    ai_start = time.monotonic()
    result = await call_ai(user_id, prompt_text, ai_text, task="wiki_ai_expand")
    ai_elapsed = time.monotonic() - ai_start
    logger.info("[WIKI-FLOW] user=%s: AI ответил за %.1fс, len(result)=%d, префикс=%r",
                user_id, ai_elapsed, len(result or ""), (result or "")[:60])
    if not result or result.startswith("❌"):
        logger.error("[WIKI-FLOW] user=%s: AI не вернул протокол: %r", user_id, (result or "")[:200])
        if progress:
            await progress.fail()
        return False, result or "❌ Нейросеть не вернула протокол.", "", ""
    if progress:
        await progress.done()

    # 6) Протокол — в папку протоколов
    if progress:
        await progress.start("Публикация протокола в Вики")
    protocol_slug = f"{protocols_dir}/{page_name}"
    if await wiki_page_exists(wiki, protocol_slug):
        ok2, msg2 = True, f"✅ Протокол уже был размещён ранее: {_wiki_page_url(wiki, protocol_slug)}"
        logger.info("[WIKI-FLOW] user=%s: протокол уже существует: %s", user_id, protocol_slug)
    else:
        logger.info("[WIKI-FLOW] user=%s: публикую протокол → %s", user_id, protocol_slug)
        ok2, msg2 = await publish_to_wiki(page_name, result, wiki, page_slug=protocol_slug)
    if not ok2:
        if progress:
            await progress.fail()
        return False, msg2, "", ""
    if progress:
        await progress.done()

    logger.info("[WIKI-FLOW] user=%s: УСПЕХ — конспект и протокол размещены (%s)", user_id, page_name)

    # Без дублирования «✅ Страница опубликована: …»: подписываем ссылки
    # (конспект и протокол — разные страницы, но с одинаковым шаблоном).
    def _label_published(label: str, msg: str) -> str:
        prefix = "✅ Страница опубликована: "
        if msg.startswith(prefix):
            return f"✅ {label} опубликован: {msg[len(prefix):]}"
        return msg

    summary_parts = [f"📄 {_md(display)}", "",
                     _label_published("Конспект", msg1),
                     _label_published("Протокол", msg2),
                     "", f"🗂 {page_name}"]
    return True, "\n".join(summary_parts), result, _wiki_page_url(wiki, protocol_slug)


# ═══════════════════════════════════════════════════════════════════
# ФУНКЦИЯ: ПОМЕТИТЬ ПИСЬМО ПРОЧИТАННЫМ В IMAP
# ═══════════════════════════════════════════════════════════════════


def _set_email_read(user_id: int, imap_msg_id: str) -> str:
    """Помечает письмо по UID как прочитанное (флаг \\Seen).
       После успешной цепочки AI→Wiki→БД — письмо уходит из /list.

       Возвращает статус-строку:
         "ok"            — письмо помечено прочитанным;
         "not_available" — письмо не найдено в INBOX или не протокол Телемоста
                           (чаще всего: уже перемещено в корзину ранее);
         "error"         — неполная настройка почты / ошибка IMAP.

       UID вместо seq-номера: seq сдвигается при изменении ящика и
       пометка ушла бы чужому письму (08.2026)."""
    config = get_user_config(user_id)
    if _email_config_error(config):
        return "error"
    assert config is not None
    try:
        server = _connect_imap(config)
        try:
            # ФИНАЛЬНЫЙ СТРАЖ (08.2026): помечаем прочитанным ТОЛЬКО протокол
            # Телемоста. Если UID по любой причине указывает на другое письмо —
            # не трогаем его (иначе «украдём» непрочитанное у чужого письма).
            ok_t, _ = _verify_telemost_email(server, imap_msg_id)
            if not ok_t:
                logger.warning("[GUARD] user=%s: НЕ помечаю прочитанным письмо %s — не протокол Телемоста",
                               user_id, imap_msg_id)
                return "not_available"
            server.uid("STORE", imap_msg_id, "+FLAGS", "(\\Seen)")
            logger.info("📩 Письмо %s помечено прочитанным (%s)", imap_msg_id, user_id)
            return "ok"
        finally:
            server.close()
            server.logout()
    except Exception as e:
        logger.error("❌ Пометка письма %s: %s", imap_msg_id, e)
        return "error"


# ── Перемещение письма в корзину почтового ящика ────────────
# Бизнес-правило (владелец, 08.2026): после успешной расшифровки
# конспекта бот помечает письмо прочитанным и СПРАШИВАЕТ пользователя
# (кнопки да/нет) о перемещении письма в корзину. При «да» — письмо
# перемещается в папку Trash корпоративного ящика.


def _find_trash_folder(server) -> str:
    """Находит папку корзины (Trash/Корзина) в списке папок IMAP."""
    try:
        typ, data = server.list()
        if typ != "OK":
            return "Trash"
        for raw in data or []:
            if not isinstance(raw, bytes):
                continue
            line = raw.decode("utf-8", "replace")
            # Атрибут \Trash — надёжный признак корзины
            if "\\Trash" in line:
                m = re.search(r'"([^"]+)"\s*$', line)
                if m:
                    return m.group(1)
        # Fallback: имя папки
        for raw in data or []:
            if not isinstance(raw, bytes):
                continue
            line = raw.decode("utf-8", "replace")
            m = re.search(r'"([^"]+)"\s*$', line)
            if m and ("trash" in m.group(1).lower() or "корзин" in m.group(1).lower()):
                return m.group(1)
    except Exception as e:
        logger.error("Ошибка поиска корзины: %s", e)
    return "Trash"


def _fetch_email_brief_from_server(server, imap_msg_id: str) -> str:
    """Краткое описание письма «тема» от отправителя (для сообщений бота).

    Читает только заголовки (BODY.PEEK) — письмо не помечается прочитанным.
    При ошибке возвращает imap_msg_id, чтобы сообщение бота не падало.
    """
    try:
        typ_h, hdr_data = server.uid(
            "FETCH", str(imap_msg_id), "(BODY.PEEK[HEADER.FIELDS (SUBJECT FROM)])"
        )
        if typ_h == "OK" and hdr_data and hdr_data[0]:
            raw_hdr = hdr_data[0][1] if isinstance(hdr_data[0], (tuple, list)) and len(hdr_data[0]) > 1 else b""
            msg = email.message_from_bytes(raw_hdr)
            subject = decode_mime_header(msg.get("Subject", "")).strip()
            frm = decode_mime_header(msg.get("From", "")).strip()
            parts = []
            if subject:
                parts.append(f"«{subject}»")
            if frm:
                parts.append(f"от {frm}")
            if parts:
                return " ".join(parts)
    except Exception as e:
        logger.error("[TRASH] Не удалось прочитать тему письма %s: %s", imap_msg_id, e)
    return imap_msg_id


def _verify_telemost_email(server, imap_msg_id: str) -> tuple[bool, str]:
    """Проверяет, что письмо с данным UID — протокол Телемоста.

    Бизнес-правило (владелец, 08.2026): бот имеет право работать ТОЛЬКО
    с протоколами телемоста — тема начинается с SUBJECT_FILTER
    («Конспект встречи») И отправитель — Телемост (keeper@telemost.yandex.ru).
    Любое другое письмо бот НЕ имеет права помечать прочитанным или
    перемещать в корзину: после инцидента 08.2026 (seq-сдвиг чуть не
    удалил документы CDEK и рассылки Яндекса) это финальный страж перед
    любой мутацией почты.

    Возвращает (ok, brief), где brief — «тема» от отправителя для сообщений.
    При ошибке/пустом ответе — (False, imap_msg_id) (fail-closed).
    """
    try:
        typ_h, hdr_data = server.uid(
            "FETCH", str(imap_msg_id), "(BODY.PEEK[HEADER.FIELDS (SUBJECT FROM)])"
        )
        if typ_h == "OK" and hdr_data and hdr_data[0]:
            raw_hdr = hdr_data[0][1] if isinstance(hdr_data[0], (tuple, list)) and len(hdr_data[0]) > 1 else b""
            msg = email.message_from_bytes(raw_hdr)
            subject = decode_mime_header(msg.get("Subject", "")).strip()
            frm = decode_mime_header(msg.get("From", "")).strip()
            is_telemost = (
                subject.lower().startswith(SUBJECT_FILTER.lower())
                and "telemost" in frm.lower()
            )
            parts = []
            if subject:
                parts.append(f"«{subject}»")
            if frm:
                parts.append(f"от {frm}")
            brief = " ".join(parts) if parts else str(imap_msg_id)
            if not is_telemost:
                logger.warning("[GUARD] письмо %s — НЕ протокол Телемоста (%s)", imap_msg_id, brief)
            return is_telemost, brief
    except Exception as e:
        logger.error("[GUARD] Не удалось проверить письмо %s: %s", imap_msg_id, e)
    return False, str(imap_msg_id)


def _fetch_email_brief(user_id: int, imap_msg_id: str) -> str:
    """Открывает INBOX и возвращает краткое описание письма
    («тема» от отправителя) — для сообщений бота."""
    config = get_user_config(user_id)
    if _email_config_error(config):
        return imap_msg_id
    assert config is not None
    try:
        server = _connect_imap(config)
        try:
            typ, _ = server.select("INBOX")
            if typ != "OK":
                return imap_msg_id
            return _fetch_email_brief_from_server(server, imap_msg_id)
        finally:
            server.close()
            server.logout()
    except Exception as e:
        logger.error("[TRASH] Не удалось получить описание письма %s: %s", imap_msg_id, e)
        return imap_msg_id


def _move_email_to_trash(user_id: int, imap_msg_id: str) -> tuple[bool, str, str]:
    r"""Перемещает письмо (по UID IMAP) в корзину почтового ящика.
       Сначала UID MOVE (RFC 6851), при неудаче — COPY + \Deleted + EXPUNGE.

       Возвращает (ok, reason, brief):
         ok     — True, если письмо перемещено;
         reason — причина результата:
                  "moved"        — письмо перемещено в корзину;
                  "already_gone" — письма уже нет в INBOX (уже в корзине ранее);
                  "not_telemost" — письмо в INBOX, но не протокол Телемоста;
                  "error"        — настройка почты / IMAP / папка корзины.
         brief  — «тема» от отправителя (или imap_msg_id), чтобы бот мог
                  написать, какое именно письмо переместил/не переместил.

       Различие already_gone / not_telemost нужно, чтобы нажатие на устаревшую
       кнопку «Да, в корзину» не выглядело ошибкой (жалоба владельца, 08.2026):
       письмо уже в корзине — это штатная ситуация, а не сбой.

       UID вместо seq-номера: seq сдвигается при изменении ящика и в корзину
       ушло бы ДРУГОЕ письмо (08.2026: CDEK-документы и рассылка Яндекса)."""
    config = get_user_config(user_id)
    if _email_config_error(config):
        return False, "error", imap_msg_id
    assert config is not None
    try:
        server = _connect_imap(config)
        try:
            typ, _ = server.select("INBOX")
            if typ != "OK":
                logger.error("Не удалось выбрать INBOX (user=%s)", user_id)
                return False, "error", imap_msg_id
            # Описание/проверка письма ДО перемещения (после MOVE/EXPUNGE
            # письма в INBOX уже не будет).
            # Проверяем, что письмо с таким UID ещё в INBOX: UID MOVE на
            # несуществующем UID может «успешно» ничего не сделать.
            typ_f, data_f = server.uid("FETCH", str(imap_msg_id), "(UID)")
            exists = typ_f == "OK" and bool(data_f and data_f[0])
            # ФИНАЛЬНЫЙ СТРАЖ (08.2026): перемещать в корзину можно ТОЛЬКО
            # протокол Телемоста. Даже если UID по какой-то причине указывает
            # на другое письмо (сдвиг seq/устаревшая кнопка) — бот откажется:
            # чужое письмо (документы CDEK, рассылки Яндекса и т.п.) в корзину
            # не уйдёт.
            ok_t, brief = _verify_telemost_email(server, imap_msg_id)
            if not exists:
                logger.warning("[TRASH] user=%s: письмо %s уже не в INBOX — не трогаю",
                               user_id, imap_msg_id)
                return False, "already_gone", brief
            if not ok_t:
                logger.warning("[GUARD] user=%s: отказ перемещать письмо %s — не протокол Телемоста (%s)",
                               user_id, imap_msg_id, brief)
                return False, "not_telemost", brief
            trash = _find_trash_folder(server)
            if not trash:
                logger.error("Папка корзины не найдена (user=%s)", user_id)
                return False, "error", brief
            logger.info("[TRASH] user=%s: перемещаю письмо %s → %s", user_id, imap_msg_id, trash)

            # 1) Пробуем UID MOVE (RFC 6851) — надёжно, без EXPUNGE
            typ_m, _ = server.uid("MOVE", str(imap_msg_id), trash)
            if typ_m == "OK":
                logger.info("[TRASH] user=%s: UID MOVE ok (письмо %s → %s)", user_id, imap_msg_id, trash)
                return True, "moved", brief
            logger.warning("[TRASH] user=%s: UID MOVE не удался (%s), fallback COPY+DELETE", user_id, typ_m)

            # 2) Fallback: COPY + \Deleted + EXPUNGE
            typ_c, _ = server.uid("COPY", str(imap_msg_id), trash)
            if typ_c != "OK":
                logger.error("[TRASH] user=%s: COPY письма %s в %s не удался (%s)",
                             user_id, imap_msg_id, trash, typ_c)
                return False, "error", brief
            server.uid("STORE", str(imap_msg_id), "+FLAGS", "(\\Deleted)")
            server.expunge()
            logger.info("[TRASH] user=%s: письмо %s перемещено в %s (COPY+EXPUNGE)", user_id, imap_msg_id, trash)
            return True, "moved", brief
        finally:
            server.close()
            server.logout()
    except Exception as e:
        logger.error("[TRASH] Исключение для user %s, письмо %s: %s", user_id, imap_msg_id, e, exc_info=True)
        return False, "error", imap_msg_id


async def _ask_trash_after_publish(callback: CallbackQuery, user_id: int, item) -> None:
    """После успешной расшифровки: помечает письмо прочитанным и
       спрашивает пользователя (кнопки да/нет) о перемещении в корзину."""
    imap_id = item[5] if len(item) >= 6 else ""
    if not imap_id:
        logger.info("[TRASH] user=%s: imap_id отсутствует — пометка/корзина пропущены", user_id)
        return
    # Бизнес-правило (владелец, 08.2026): в вопросе о корзине пишем,
    # КАКОЕ письмо можно удалить (тема + отправитель).
    subject = item[1] if len(item) > 1 else ""
    frm = item[4] if len(item) > 4 else ""
    brief = " ".join(
        p for p in (f"«{subject}»" if subject else "", f"от {frm}" if frm else "") if p
    ) or imap_id
    read_status = await asyncio.to_thread(_set_email_read, user_id, imap_id)
    if read_status != "ok":
        # Письмо недоступно в INBOX (чаще всего — уже перемещено в корзину
        # ранее при повторной обработке конспекта из списка) либо страж не
        # пропустил его. Вопрос «Переместить в корзину?» не задаём: кнопка
        # «Да, в корзину» для отсутствующего письма вела бы к ложной ошибке
        # «Не удалось переместить письмо в корзину» (жалоба владельца, 08.2026).
        logger.info("[TRASH] user=%s: письмо %s недоступно (status=%s) — вопрос о корзине пропущен",
                    user_id, imap_id, read_status)
        if read_status == "error":
            await callback.message.answer(
                f"❌ Не удалось пометить письмо прочитанным: {brief}. Вопрос о корзине пропущен.")
        else:
            await callback.message.answer(
                f"ℹ️ Письмо {brief} уже недоступно в почте (вероятно, перемещено в корзину ранее) — вопрос о корзине пропущен.")
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🗑 Да, в корзину", callback_data=f"trash_yes:{imap_id}"),
        InlineKeyboardButton(text="Нет, оставить", callback_data=f"trash_no:{imap_id}"),
    ]])
    await callback.message.answer(
        f"📬 Письмо помечено прочитанным.\n\n"
        f"🗑 Можно удалить письмо: {brief}.\n\n"
        f"Переместить его в корзину почтового ящика?",
        reply_markup=kb,
    )


async def _delete_trash_request(message) -> bool:
    """Удаляет сообщение-запрос «Переместить это письмо в корзину?»
    вместе с кнопками (да/нет).

    Бизнес-правило (владелец, 08.2026): после успешного перемещения
    письма в корзину почтового ящика сам запрос и его кнопки больше
    не нужны — удаляем их, чтобы не засорять чат. При ошибке удаления
    бот не падает (возвращает False).
    """
    try:
        await message.delete()
        logger.info("[TRASH-BTN] сообщение-запрос о корзине удалено")
        return True
    except Exception as e:
        logger.warning("[TRASH-BTN] не удалось удалить запрос о корзине: %s", e)
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


async def _send_logo(chat_id: int) -> bool:
    """Логотип HuntTech перед приветствием — из общей библиотеки."""
    from hunttech_bot_common.media import send_logo

    return await send_logo(bot, chat_id)


async def _hide_reply_keyboard(chat_id: int) -> None:
    """Скрывает постоянную нижнюю клавиатуру, чтобы inline-кнопки не уходили за неё.

    Отправляет пустое сообщение с ReplyKeyboardRemove.
    """
    from aiogram.types import ReplyKeyboardRemove
    try:
        await bot.send_message(chat_id, " ", reply_markup=ReplyKeyboardRemove())
    except Exception as e:
        logger.debug("Failed to hide reply keyboard: %s", e)


def _bot_version() -> str:
    """Версия бота (стандарт HuntTech): pyproject.toml → короткий SHA."""
    from hunttech_bot_common.services.startup import bot_version

    return bot_version(Path(__file__).resolve().parent)


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

# Учёт обращений к нейросети: общий реестр всех HuntTech-ботов
# (~/.hermes/hunttech_bots/ai_usage.json), отчёт — команда /usage.
_usage_tracker = UsageTracker()


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
            lines.append(f"{idx}. {_md(topic)}")
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
            await _hide_reply_keyboard(message.chat.id)
            await message.answer(
                "📭 Промптов нет. Добавить первый?", 
                reply_markup=_first_prompt_keyboard(),
            )
            return
        topics = "\n".join(f"• {_md(t)}" for t in sorted(prompts.keys()))
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
            await _hide_reply_keyboard(message.chat.id)
            await message.answer(
                "📭 Промптов нет. Добавить первый?", 
                reply_markup=_first_prompt_keyboard(),
            )
            return
        topics = "\n".join(f"• {_md(t)}" for t in sorted(prompts.keys()))
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
            await _hide_reply_keyboard(message.chat.id)
            await message.answer(
                "📭 Промптов нет. Добавить первый?", 
                reply_markup=_first_prompt_keyboard(),
            )
            return
        topics = "\n".join(f"• {_md(t)}" for t in sorted(prompts.keys()))
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


def _prompt_known_for(user_id: int | None, display: str) -> bool:
    """Промпт для расшифровки конспекта определён, если:
       1) тема промпта совпадает с началом названия конспекта (startswith, без регистра);
       2) ИЛИ настроена Вики (oauth_token) и для темы есть маршрут wiki.routing
          (промпт лежит в корне подраздела).
       Единая точка проверки для кнопок списка и отчёта «Кратко»."""
    prompts = _load_prompts() or {}
    for topic in prompts:
        if display.lower().startswith(topic.lower()):
            return True
    if user_id:
        wiki = get_wiki_config(user_id)
        if wiki and wiki.get("oauth_token") and wiki_route_section(user_id, display):
            return True
    return False


def _get_item_button(idx: int, display: str, user_id: int | None = None,
                     list_id: str = "") -> InlineKeyboardMarkup | None:
    """
    Создаёт кнопку под конспектом в списке.

    Бизнес-правило (владелец, 08.2026): если бот ЗНАЕТ промпт для
    расшифровки этого конспекта — промпт загружен в настройках
    (по префиксу темы) ИЛИ лежит в корневой папке wiki подраздела
    (маршрут wiki.routing + настроенная wiki) — кнопку «Саммари»
    НЕ выводим, остаётся только полный флоу «📝 Расшифровать и
    разместить в wiki».

    Если промпт неизвестен — показываем «🟡 Выбрать промпт»
    (и кнопку расшифровки, как раньше).

    Бизнес-правило сопоставления: название конспекта должно начинаться
    с темы промпта (без учёта регистра). Например, промпт "План развития"
    подойдёт к конспекту "План развития на Q2".

    callback_data несут list_id — идентификатор показанного списка,
    чтобы нажатие работало с тем списком, который видел пользователь
    (даже если кэш позже перезаписан другим списком).
    """
    prompts = _load_prompts() or {}

    if _prompt_known_for(user_id, display):
        # Промпт известен — кнопка «Саммари» не нужна
        return InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"📝 Расшифровать и разместить в wiki #{idx}",
                    callback_data=f"wiki_proc:{list_id}:{idx}"
                )
            ],
            [
                InlineKeyboardButton(
                    text=f"📌 Кратко #{idx}",
                    callback_data=f"brief:{list_id}:{idx}"
                )
            ],
        ])
    else:
        if not prompts:
            return None
        return InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"🟡 Выбрать промпт #{idx}",
                    callback_data=f"choose_prompt:{list_id}:{idx}"
                )
            ],
            [
                InlineKeyboardButton(
                    text=f"📝 Расшифровать и разместить в wiki #{idx}",
                    callback_data=f"wiki_proc:{list_id}:{idx}"
                )
            ],
            [
                InlineKeyboardButton(
                    text=f"📌 Кратко #{idx}",
                    callback_data=f"brief:{list_id}:{idx}"
                )
            ],
        ])


async def _mark_wiki_proc_busy(message) -> list[tuple[str, str]]:
    """Сразу после нажатия «📝 Расшифровать и разместить в wiki» помечает
    НАЖАТУЮ кнопку эмодзи ⏳ (песочные часы) — процесс начался.

    Меняется только текст кнопки (callback_data не трогается), остальные
    кнопки сообщения и другие сообщения не затрагиваются. Возвращает
    [(callback_data, старый_текст), ...] для восстановления при ошибке флоу.
    При сбое — [] и бот не падает.
    """
    try:
        markup = message.reply_markup
        if not markup or not markup.inline_keyboard:
            return []
        changed: list[tuple[str, str]] = []
        rows = []
        for row in markup.inline_keyboard:
            new_row = []
            for b in row:
                cb = b.callback_data or ""
                if cb.startswith(("wiki_proc:", "wiki_process:")):
                    if b.text.startswith("⏳"):
                        new_row.append(b)  # уже помечена (повторное нажатие)
                    else:
                        changed.append((cb, b.text))
                        new_text = re.sub(r"^\W+", "", b.text).strip()
                        new_row.append(InlineKeyboardButton(
                            text=f"⏳ {new_text}" if new_text else f"⏳ {b.text}",
                            callback_data=cb,
                        ))
                else:
                    new_row.append(b)
            rows.append(new_row)
        if changed:
            await message.edit_reply_markup(reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
            logger.info("[WIKI-BTN] кнопка «Расшифровать в wiki» помечена ⏳ (%d)", len(changed))
        return changed
    except Exception as e:
        logger.warning("[WIKI-BTN] не удалось пометить кнопку ⏳: %s", e)
        return []


async def _restore_wiki_proc_button(message, originals: list[tuple[str, str]]) -> None:
    """Возвращает кнопкам «Расшифровать в wiki» исходный текст после ошибки
    флоу (снимает пометку ⏳). При пустом списке или сбое — бот не падает."""
    if not originals:
        return
    try:
        markup = message.reply_markup
        if not markup or not markup.inline_keyboard:
            return
        orig = dict(originals)
        rows = []
        for row in markup.inline_keyboard:
            new_row = []
            for b in row:
                cb = b.callback_data or ""
                if cb in orig:
                    new_row.append(InlineKeyboardButton(text=orig[cb], callback_data=cb))
                else:
                    new_row.append(b)
            rows.append(new_row)
        await message.edit_reply_markup(reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
        logger.info("[WIKI-BTN] кнопка «Расшифровать в wiki» восстановлена после ошибки")
    except Exception as e:
        logger.warning("[WIKI-BTN] не удалось восстановить кнопку: %s", e)


async def _remove_wiki_proc_button(message) -> None:
    """После успешного прохождения сценария «Расшифровать и разместить
    в wiki» убирает эту кнопку у ТОГО сообщения, с которого она нажата.

    Остальные кнопки сообщения («📌 Кратко», «🟡 Выбрать промпт») и
    кнопки у других сообщений (другие конспекты списка, уведомления)
    не трогаем — бизнес-правило владельца (08.2026).
    """
    try:
        markup = message.reply_markup
        if not markup or not markup.inline_keyboard:
            return
        had_wiki = any(
            (b.callback_data or "").startswith("wiki_proc:")
            for row in markup.inline_keyboard for b in row
        )
        if not had_wiki:
            return
        rows = [
            [b for b in row if not (b.callback_data or "").startswith("wiki_proc:")]
            for row in markup.inline_keyboard
        ]
        rows = [row for row in rows if row]
        kb = InlineKeyboardMarkup(inline_keyboard=rows) if rows else None
        await message.edit_reply_markup(reply_markup=kb)
        logger.info("[WIKI-BTN] кнопка «Расшифровать в wiki» убрана у сообщения")
    except Exception as e:
        logger.warning("[WIKI-BTN] не удалось убрать кнопку у сообщения: %s", e)


# ── Callback-хендлер для кнопки Саммари ─────────────────────

@dp.callback_query(lambda c: c.data and c.data.startswith("summary:"))
async def summary_callback(callback: CallbackQuery, state: FSMContext):
    """
    Когда пользователь нажимает 🟢 Саммари #N:
    - Берём txt-содержимое конспекта из кеша
    - Берём текст промпта (шаблон саммари)
    - Отправляем в нейросеть через call_ai()
    - Показываем результат
    
    Формат callback_data: summary:{list_id}:{IDX} (новый) или summary:IDX:PROMPT_TOPIC (старый)
    """
    parts = callback.data.split(":", 2)
    if len(parts) < 3:
        await callback.answer("❌ Ошибка данных", show_alert=True)
        return
    _, a, b = parts
    if a.isdigit():
        # Старый формат: summary:IDX:PROMPT_TOPIC (idx 1-based)
        list_id, idx_str, prompt_topic = "", a, b
    else:
        # Новый формат: summary:{list_id}:{IDX} — тему промпта найдём по названию
        list_id, idx_str, prompt_topic = a, b, ""
    idx = int(idx_str) - 1  # 0-based
    await callback.answer()

    user_id = callback.from_user.id

    # Загружаем из кеша — конспекты с txt-содержимым
    items = _load_notes_cache(user_id, list_id)
    if idx < 0 or idx >= len(items):
        await callback.message.answer("❌ Конспект устарел. Запросите /list заново.")
        return

    item = items[idx]
    _dt = item[0]
    display = item[1]
    txt_content = item[2]
    imap_id = item[5] if len(item) >= 6 else ""

    # Загружаем промпт
    prompts = _load_prompts() or {}
    if not prompt_topic:
        for topic in prompts:
            if display.lower().startswith(topic.lower()):
                prompt_topic = topic
                break
    prompt_text = prompts.get(prompt_topic, "")
    if not prompt_text:
        await callback.message.answer(f"❌ Промпт «{_md(prompt_topic)}» не найден.")
        return

    if not txt_content:
        await callback.message.answer("❌ В письме не найден текст конспекта (txt-вложение).")
        return

    # Показываем статус — нейросеть может думать до минуты
    status_msg = await callback.message.answer(
        f"⏳ Обрабатываю «{_md(display)}» через нейросеть...",
        parse_mode=ParseMode.MARKDOWN,
    )

    # Вызываем AI: system_prompt = текст промпта, user_text = конспект
    system_prompt = prompt_text
    user_text = f"Конспект встречи: «{display}»\n\n{txt_content}"
    result = await call_ai(user_id, system_prompt, user_text, task="summarize_protocol")

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
    header = f"🧠 **Саммари: {_md(display)}**\n\n---\n\n"
    full_text = header + result

    # Telegram не принимает >4000 символов — режем
    if len(full_text) <= 4000:
        await callback.message.answer(full_text, parse_mode=ParseMode.MARKDOWN)
    else:
        # Разбиваем на части: заголовок отдельно, текст кусками
        await callback.message.answer(header, parse_mode=ParseMode.MARKDOWN)
        for i in range(0, len(result), 3500):
            await callback.message.answer(result[i:i + 3500])

    # ── Кнопка «Расшифровать и разместить в wiki» ─────────────
    wiki_config = get_wiki_config(user_id)
    if wiki_config and wiki_config.get("oauth_token"):
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(
                text=f"📝 Расшифровать и разместить в wiki #{idx}",
                callback_data=f"wiki_proc:{idx}"
            )
        ]])
        await callback.message.answer(
            "📚 Разместить конспект и саммари в Яндекс Вики?",
            reply_markup=kb,
        )

    # Письмо НЕ помечаем прочитанным (бизнес-правило:
    # «запрещается менять письма и помечать их как прочитанные»)


# ── Callback-хендлер: «📝 Расшифровать и разместить в wiki» из списка ──

@dp.callback_query(lambda c: c.data and c.data.startswith("wiki_proc:"))
async def wiki_proc_callback(callback: CallbackQuery, state: FSMContext):
    """Кнопка «Расшифровать и разместить в wiki» под конспектом в /list:
       полный флоу — классификация, оригинал в «Конспекты», промпт из Вики,
       AI-расшифровка, протокол в «Протоколы»."""
    await callback.answer()
    user_id = callback.from_user.id
    parts = callback.data.split(":", 1)[1].split(":")
    # Новый формат: wiki_proc:{list_id}:{idx}; старый: wiki_proc:{idx}
    try:
        if len(parts) == 2 and parts[0] and not parts[0].isdigit():
            # Новый формат из списка: idx 1-based (номер конспекта в сообщении)
            list_id, idx_str = parts[0], parts[1]
            idx = int(idx_str) - 1
        else:
            # Старый формат (кнопка после «Саммари»): idx уже 0-based
            list_id, idx_str = "", parts[0]
            idx = int(idx_str)
    except ValueError:
        await callback.message.answer("❌ Некорректные данные кнопки.")
        return

    items = _load_notes_cache(user_id, list_id)
    if idx < 0 or idx >= len(items):
        logger.warning("[WIKI-BTN] user=%s idx=%d вне диапазона кэша list_id=%r (len=%d)",
                       user_id, idx, list_id, len(items))
        await callback.message.answer("❌ Конспект устарел. Запросите /list заново.")
        return
    item = items[idx]
    if not item[2]:
        await callback.message.answer("❌ В письме не найден текст конспекта (txt-вложение).")
        return

    logger.info("[WIKI-BTN] user=%s нажал «Расшифровать в wiki»: idx=%d display=%r txt_len=%d",
                user_id, idx, item[1][:70], len(item[2]))
    # Сразу помечаем нажатую кнопку ⏳ — процесс начался
    # (бизнес-правило владельца, 08.2026).
    marked = await _mark_wiki_proc_busy(callback.message)
    status_msg = await callback.message.answer(
        f"⏳ Расшифровываю «{_md(item[1])}» и размещаю в wiki...",
        parse_mode=ParseMode.MARKDOWN,
    )
    try:
        progress = WikiProgress(status_msg, user_id, item[1])
        ok, msg, summary, protocol_url = await process_conspect_to_wiki(user_id, item, progress)
        logger.info("[WIKI-BTN] user=%s результат: ok=%s msg=%r", user_id, ok, (msg or "")[:150])
        final = f"{progress.render()}\n\n{msg}"
        if ok:
            key = _short_uid(f"{item[0].timestamp()}:{item[1]}")
            _save_summary_cache(user_id, key, item[1], summary, wiki_url=protocol_url)
            kb = _after_publish_keyboard(key)
            await status_msg.edit_text(final, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
            await _ask_trash_after_publish(callback, user_id, item)
            # Сценарий прошёл без ошибок — убираем кнопку у ЭТОГО сообщения
            # (у других сообщений кнопка остаётся).
            await _remove_wiki_proc_button(callback.message)
        else:
            await status_msg.edit_text(final, parse_mode=ParseMode.MARKDOWN)
            # Ошибка — снимаем пометку ⏳, чтобы кнопку можно было нажать снова.
            await _restore_wiki_proc_button(callback.message, marked)
    except Exception as e:
        logger.error("[WIKI-BTN] user=%s исключение: %s", user_id, e, exc_info=True)
        try:
            await status_msg.edit_text(f"❌ Ошибка: {e}")
        except Exception:
            pass
        await _restore_wiki_proc_button(callback.message, marked)


@dp.callback_query(lambda c: c.data and c.data.startswith("wiki_process:"))
async def wiki_process_callback(callback: CallbackQuery, state: FSMContext):
    """
    Кнопка «📝 Расшифровать и разместить в wiki» под уведомлением
    о новом конспекте. Запускает полный флоу:
    классификация → сохранение оригинала → промпт из Вики → AI → протокол.

    Формат callback_data: wiki_process:{uid}
    uid = "{dt.timestamp()}:{display}" — конспект берётся из wiki_pending.json.
    """
    await callback.answer()
    user_id = callback.from_user.id
    parts = callback.data.split(":", 1)
    uid = parts[1] if len(parts) > 1 else ""
    logger.info("[WIKI-BTN] user=%s нажал кнопку на уведомлении: uid=%r", user_id, uid[:60])

    pending = _load_wiki_pending(user_id)
    item = pending.get(uid)
    if not item:
        logger.warning("[WIKI-BTN] user=%s: конспект uid=%r не найден в pending — уже обработан или устарел",
                       user_id, uid[:60])
        await callback.message.answer(
            "❌ Конспект уже обработан или устарел. Дождитесь нового уведомления."
        )
        return

    display = item[1]
    logger.info("[WIKI-BTN] user=%s: начинаю обработку из уведомления: display=%r txt_len=%d",
                user_id, display[:70], len(item[2]))
    # Сразу помечаем нажатую кнопку ⏳ — процесс начался
    # (бизнес-правило владельца, 08.2026).
    marked = await _mark_wiki_proc_busy(callback.message)
    status_msg = await callback.message.answer(
        f"⏳ Расшифровываю «{_md(display)}» и размещаю в wiki...",
        parse_mode=ParseMode.MARKDOWN,
    )
    try:
        progress = WikiProgress(status_msg, user_id, display)
        ok, msg, summary, protocol_url = await process_conspect_to_wiki(user_id, item, progress)
        final = f"{progress.render()}\n\n{msg}"
        if ok:
            _remove_wiki_pending(user_id, uid)
            logger.info("[WIKI-BTN] user=%s: конспект «%s» обработан в wiki", user_id, display[:70])
            _save_summary_cache(user_id, uid, display, summary, wiki_url=protocol_url)
            kb = _after_publish_keyboard(uid)
            await status_msg.edit_text(final, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
            await _ask_trash_after_publish(callback, user_id, item)
        else:
            logger.warning("[WIKI-BTN] user=%s: флоу вернул ошибку: %r", user_id, (msg or "")[:150])
            await status_msg.edit_text(final, parse_mode=ParseMode.MARKDOWN)
            # Ошибка — снимаем пометку ⏳, чтобы кнопку можно было нажать снова.
            await _restore_wiki_proc_button(callback.message, marked)
    except Exception as e:
        logger.error("[WIKI-BTN] user=%s исключение: %s", user_id, e, exc_info=True)
        try:
            await status_msg.edit_text(f"❌ Ошибка обработки: {e}")
        except Exception:
            pass
        await _restore_wiki_proc_button(callback.message, marked)


@dp.callback_query(lambda c: c.data and c.data.startswith("choose_prompt:"))
async def choose_prompt_callback(callback: CallbackQuery, state: FSMContext):
    """
    Когда пользователь нажимает 🟡 Выбрать промпт #N — предлагаем
    создать подходящий промпт для этого типа конспекта.
    
    Бизнес-правило: подсказываем первое слово из названия конспекта
    как тему нового промпта.
    """
    parts = callback.data.split(":", 1)[1].split(":")
    # Новый формат: choose_prompt:{list_id}:{idx}; старый: choose_prompt:{idx}
    if len(parts) == 2 and parts[0] and not parts[0].isdigit():
        list_id, idx_str = parts[0], parts[1]
    else:
        list_id, idx_str = "", parts[0]
    idx = int(idx_str) - 1
    await callback.answer()

    user_id = callback.from_user.id
    items = _load_notes_cache(user_id, list_id)
    if idx < 0 or idx >= len(items):
        await callback.message.answer("❌ Конспект устарел. Запросите /list заново.")
        return

    _dt, display, _txt = items[idx]
    await callback.message.answer(_prompt_guidance(display), parse_mode=ParseMode.MARKDOWN)


def _prompt_guidance(display: str) -> str:
    """Подсказка, как задать промпт для конспекта (кнопка «🟡 Задать промпт»)."""
    topic = display.split()[0] if display.split() else display
    return (
        f"📝 Для конспекта «{_md(display)}» не найден подходящий промпт.\n\n"
        f"Создайте промпт с названием, которое совпадает с началом строки:\n"
        f"📌 `/add_prompt` → тема: `{topic}` → текст промпта\n\n"
        f"Или используйте `/prompt` для управления промптами."
    )


@dp.callback_query(lambda c: c.data and c.data.startswith("choose_prompt_pending:"))
async def choose_prompt_pending_callback(callback: CallbackQuery, state: FSMContext):
    """Кнопка «🟡 Задать промпт» под отчётом «📌 Кратко» на уведомлении
       о новом конспекте: промпт не определён — показываем, как его создать."""
    user_id = callback.from_user.id
    key = callback.data.split(":", 1)[1]
    pending = _load_wiki_pending(user_id)
    item = pending.get(key)
    if not item:
        await callback.answer("❌ Конспект устарел или уже обработан.", show_alert=True)
        return
    display = item[1]
    await callback.answer()
    await callback.message.answer(_prompt_guidance(display), parse_mode=ParseMode.MARKDOWN)


# ── Callback-хендлер для кнопки «📤 Опубликовать в Wiki» ────

@dp.callback_query(lambda c: c.data and c.data.startswith("publish_wiki:"))
async def publish_wiki_callback(callback: CallbackQuery, state: FSMContext):
    """Кнопка «📤 Опубликовать в Wiki» (обратная совместимость).
       Теперь ведёт на полный флоу process_conspect_to_wiki:
       классификация → оригинал в «Конспекты» → промпт из Вики →
       AI-расшифровка → протокол в «Протоколы»."""
    parts = callback.data.split(":", 2)
    if len(parts) < 2:
        await callback.answer("❌ Ошибка данных", show_alert=True)
        return
    try:
        idx = int(parts[1]) - 1
    except ValueError:
        await callback.answer("❌ Ошибка данных", show_alert=True)
        return
    await callback.answer()

    user_id = callback.from_user.id
    items = _load_notes_cache(user_id)
    if idx < 0 or idx >= len(items):
        await callback.message.answer("❌ Конспект устарел. Запросите /list заново.")
        return
    item = items[idx]
    if not item[2]:
        await callback.message.answer("❌ В письме не найден текст конспекта (txt-вложение).")
        return

    status_msg = await callback.message.answer(
        f"⏳ Расшифровываю «{_md(item[1])}» и размещаю в wiki...",
        parse_mode=ParseMode.MARKDOWN,
    )
    try:
        progress = WikiProgress(status_msg, user_id, item[1])
        ok, msg, summary, protocol_url = await process_conspect_to_wiki(user_id, item, progress)
        final = f"{progress.render()}\n\n{msg}"
        if ok:
            key = _short_uid(f"{item[0].timestamp()}:{item[1]}")
            _save_summary_cache(user_id, key, item[1], summary, wiki_url=protocol_url)
            kb = _after_publish_keyboard(key)
            await status_msg.edit_text(final, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
            await _ask_trash_after_publish(callback, user_id, item)
        else:
            await status_msg.edit_text(final, parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        logger.error("Ошибка publish_wiki для user %s: %s", user_id, e, exc_info=True)
        try:
            await status_msg.edit_text(f"❌ Ошибка: {e}")
        except Exception:
            pass


# ── Кнопки «Да/Нет» про перемещение письма в корзину ────────

@dp.callback_query(lambda c: c.data and (c.data.startswith("trash_yes:") or c.data.startswith("trash_no:")))
async def trash_callback(callback: CallbackQuery, state: FSMContext):
    """После расшифровки пользователь отвечает, перемещать ли письмо в корзину."""
    user_id = callback.from_user.id
    imap_id = callback.data.split(":", 1)[1]
    if callback.data.startswith("trash_yes:"):
        logger.info("[TRASH-BTN] user=%s: пользователь согласился переместить письмо %s в корзину",
                    user_id, imap_id)
        ok, reason, brief = await asyncio.to_thread(_move_email_to_trash, user_id, imap_id)
        if ok:
            await callback.answer("🗑 Перемещено в корзину.")
            # Бизнес-правило (владелец, 08.2026): после удаления пишем,
            # какое именно письмо удалили (тема + отправитель).
            await callback.message.answer(f"🗑 Удалил письмо в корзину почтового ящика: {brief}.")
            # Письмо в корзине — запрос «Переместить в корзину?» и его кнопки
            # больше не нужны (бизнес-правило владельца, 08.2026).
            await _delete_trash_request(callback.message)
        elif reason in ("already_gone", "not_telemost"):
            # Не ошибка, а штатная ситуация (жалоба владельца, 08.2026):
            # письма уже нет в INBOX (повторное нажатие устаревшей кнопки,
            # письмо уже в корзине) либо страж запретил трогать чужое письмо.
            # Сообщаем информативно и убираем кнопки, чтобы их нельзя было
            # нажимать повторно.
            if reason == "already_gone":
                await callback.answer("ℹ️ Письмо уже в корзине.", show_alert=True)
                await callback.message.answer(
                    f"ℹ️ Письмо уже перемещено в корзину ранее: {brief}.")
            else:
                await callback.answer("⛔️ Не протокол Телемоста.", show_alert=True)
                await callback.message.answer(
                    f"⛔️ Не перемещаю в корзину: это не протокол Телемоста ({brief}).")
            await _delete_trash_request(callback.message)
        else:
            await callback.answer("❌ Не удалось переместить письмо.", show_alert=True)
            await callback.message.answer(f"❌ Не удалось переместить письмо в корзину: {brief}.")
    else:
        logger.info("[TRASH-BTN] user=%s: пользователь оставил письмо %s в папке", user_id, imap_id)
        brief = await asyncio.to_thread(_fetch_email_brief, user_id, imap_id)
        await callback.answer("Оставляю письмо в папке.")
        await callback.message.answer(f"📥 Оставил письмо в папке: {brief}.")


# ── Кнопка «📄 Показать саммари» ─────────────────────────────

@dp.callback_query(lambda c: c.data and c.data.startswith("show_summary:"))
async def show_summary_callback(callback: CallbackQuery, state: FSMContext):
    """Показывает текст расшифрованного саммари (из кэша) по нажатию кнопки."""
    user_id = callback.from_user.id
    uid = callback.data.split(":", 1)[1]
    cache = _load_summary_cache(user_id)
    entry = cache.get(uid)
    if not entry:
        await callback.answer("❌ Саммари уже недоступно (кэш очищен).", show_alert=True)
        return
    await callback.answer()
    logger.info("[SHOW-SUMMARY] user=%s: показывает саммари «%s» (%d симв.)",
                user_id, (entry.get("display") or "")[:60], len(entry.get("summary") or ""))
    await _send_summary(callback.message, entry.get("display", ""), entry.get("summary", ""))


# ── Публикация дайджеста протокола в группу ────────────────────
# Бизнес-правило (владелец, 08.2026): после успешной публикации в Вики
# бот предлагает кнопку «📢 Опубликовать в группе» — публикует ПЕРЕРАБОТАННЫЙ
# протокол (внутренний промпт: только самое основное, не более 10 предложений,
# корректный Markdown) в группу, где бот является администратором.

def _after_publish_keyboard(key: str) -> InlineKeyboardMarkup:
    """Кнопки после успешной публикации в Вики:
       «📄 Показать саммари» и «📢 Опубликовать в группе»."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="📄 Показать саммари", callback_data=f"show_summary:{key}"),
        ],
        [
            InlineKeyboardButton(text="📢 Опубликовать в группе", callback_data=f"publish_group:{key}"),
        ],
    ])


# Внутренний промпт дайджеста (не из Вики): только основное, ≤10 предложений,
# корректный Markdown для публикации в рабочей группе.
GROUP_DIGEST_PROMPT = (
    "Ты — секретарь IT-компании HUNTTECH. Ниже — протокол встречи. "
    "Переработай его в КОРОТКИЙ ДАЙДЖЕСТ для публикации в рабочей группе:\n"
    "1) оставь только самое основное: решения, задачи, сроки, ответственные;\n"
    "2) максимум 10 предложений;\n"
    "3) используй корректное форматирование Markdown: заголовок, списки "
    "(- или 1.), жирный текст для ключевого;\n"
    "4) без воды, вводных фраз и приветствий."
)


@dp.callback_query(lambda c: c.data and c.data.startswith("publish_group:"))
async def publish_group_callback(callback: CallbackQuery, state: FSMContext):
    """Кнопка «📢 Опубликовать в группе» после публикации в Вики:
       перерабатывает протокол внутренним промптом (только основное,
       не более 10 предложений, корректный Markdown) и публикует в группу,
       где бот — администратор."""
    user_id = callback.from_user.id
    key = callback.data.split(":", 1)[1]
    cache = _load_summary_cache(user_id)
    entry = cache.get(key)
    if not entry:
        await callback.answer("❌ Протокол уже недоступен (кэш очищен).", show_alert=True)
        return
    target = get_group_target(user_id)
    if not target:
        await callback.answer(
            "❌ Бот не добавлен администратором ни в одну группу.\n"
            "Добавьте бота в группу и выдайте ему права администратора.",
            show_alert=True,
        )
        return
    await callback.answer()
    display = entry.get("display", "")
    protocol = entry.get("summary", "")
    logger.info("[GROUP-PUB] user=%s: готовлю дайджест «%s» для группы %s (%s)",
                user_id, display[:60], target.get("chat_id"), target.get("title"))
    status = await callback.message.answer(
        f"⏳ Готовлю дайджест «{_md(display)}» для публикации в группе...",
        parse_mode=ParseMode.MARKDOWN,
    )
    try:
        digest = await call_ai(user_id, GROUP_DIGEST_PROMPT, protocol, task="group_digest")
        if not digest or digest.startswith("❌"):
            await status.edit_text(digest or "❌ Нейросеть не ответила.", parse_mode=ParseMode.MARKDOWN)
            return
        header = f"📋 {_md(display)}\n\n"
        full = header + digest
        wiki_url = entry.get("wiki_url", "")
        if wiki_url:
            full += f"\n\n🔗 Полный протокол: {wiki_url}"
        try:
            await bot.send_message(
                chat_id=target["chat_id"], text=full,
                parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=True,
            )
        except Exception as e:
            # ИИ мог выдать невалидную Markdown-разметку — отправляем без разметки
            logger.warning("[GROUP-PUB] user=%s: markdown упал (%s), шлю без разметки", user_id, e)
            await bot.send_message(
                chat_id=target["chat_id"], text=full, disable_web_page_preview=True
            )
        await status.edit_text(
            f"✅ Опубликовано в группе «{_md(target.get('title') or target['chat_id'])}».",
            parse_mode=ParseMode.MARKDOWN,
        )
    except Exception as e:
        logger.error("[GROUP-PUB] user=%s исключение: %s", user_id, e, exc_info=True)
        try:
            await status.edit_text(f"❌ Ошибка публикации: {e}")
        except Exception:
            pass


@dp.my_chat_member()
async def on_my_chat_member(update: ChatMemberUpdated):
    """Запоминает группу, где бота сделали администратором (для кнопки
       «📢 Опубликовать в группе»). При исключении/снятии прав — удаляет."""
    try:
        chat = update.chat
        if chat.type not in ("group", "supergroup"):
            return
        new_status = update.new_chat_member.status
        actor_id = update.from_user.id if update.from_user else 0
        if not actor_id:
            return
        if new_status == "administrator":
            title = chat.title or f"группа {chat.id}"
            save_group_target(actor_id, chat.id, title)
            logger.info("[GROUP-TARGET] user=%s: бот стал администратором в «%s» (%s)",
                        actor_id, title, chat.id)
            try:
                await bot.send_message(
                    chat_id=actor_id,
                    text=f"✅ Бот назначен администратором группы «{title}».\n"
                         f"Теперь протоколы можно публиковать туда кнопкой «📢 Опубликовать в группе».",
                    parse_mode=ParseMode.MARKDOWN,
                )
            except Exception as e:
                logger.warning("[GROUP-TARGET] не удалось уведомить user=%s: %s", actor_id, e)
        elif new_status in ("kicked", "left", "member", "restricted"):
            remove_group_target(actor_id)
            logger.info("[GROUP-TARGET] user=%s: бот больше не администратор группы %s (%s)",
                        actor_id, chat.id, new_status)
    except Exception as e:
        logger.error("[GROUP-TARGET] Ошибка обработки my_chat_member: %s", e, exc_info=True)


# ── Кнопка «📌 Кратко» ────────────────────────────────────────

def _brief_action_keyboard(user_id: int, display: str, list_id: str, idx: int) -> InlineKeyboardMarkup:
    """Кнопки под отчётом «📌 Кратко» из списка конспектов:
       промпт определён → «📝 Расшифровать и разместить в wiki» (полный флоу);
       промпт не определён → «🟡 Задать промпт» (подсказка /add_prompt).
       idx — 0-based; в callback_data кладём 1-based (как номер в списке)."""
    n = idx + 1
    if _prompt_known_for(user_id, display):
        return InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(
                text=f"📝 Расшифровать и разместить в wiki #{n}",
                callback_data=f"wiki_proc:{list_id}:{n}",
            )
        ]])
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text=f"🟡 Задать промпт #{n}",
            callback_data=f"choose_prompt:{list_id}:{n}",
        )
    ]])


def _brief_action_keyboard_pending(user_id: int, display: str, key: str) -> InlineKeyboardMarkup:
    """Кнопки под отчётом «📌 Кратко» на уведомлении о новом конспекте:
       промпт определён → «📝 Расшифровать и разместить в wiki» (по uid уведомления);
       промпт не определён → «🟡 Задать промпт»."""
    if _prompt_known_for(user_id, display):
        return InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(
                text="📝 Расшифровать и разместить в wiki",
                callback_data=f"wiki_process:{key}",
            )
        ]])
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text="🟡 Задать промпт",
            callback_data=f"choose_prompt_pending:{key}",
        )
    ]])


BRIEF_PROMPT = (
    "Ты — секретарь IT-компании HUNTTECH. По конспекту/стенограмме встречи "
    "составь КРАТКИЙ КОНТЕКСТ ровно из 3 предложений:\n"
    "1) о чём была встреча;\n"
    "2) список участников;\n"
    "3) самое важное решение или событие встречи.\n"
    "Без заголовков, без нумерации, просто 3 коротких предложения."
)


@dp.callback_query(lambda c: c.data and c.data.startswith("brief:"))
async def brief_callback(callback: CallbackQuery, state: FSMContext):
    """Кнопка «📌 Кратко» под конспектом: AI делает краткий контекст встречи."""
    user_id = callback.from_user.id
    parts = callback.data.split(":", 1)[1].split(":")
    # Новый формат: brief:{list_id}:{idx}; старый: brief:{idx}
    if len(parts) == 2 and parts[0] and not parts[0].isdigit():
        list_id, idx_str = parts[0], parts[1]
    else:
        list_id, idx_str = "", parts[0]
    try:
        idx = int(idx_str) - 1
    except ValueError:
        await callback.answer("❌ Некорректные данные", show_alert=True)
        return
    items = _load_notes_cache(user_id, list_id)
    if idx < 0 or idx >= len(items):
        await callback.answer("❌ Конспект устарел. Запросите /list заново.", show_alert=True)
        return
    item = items[idx]
    dt, display, txt_content = item[0], item[1], item[2]
    if not txt_content:
        await callback.message.answer("❌ В письме не найден текст конспекта (txt-вложение).")
        return
    await callback.answer()
    logger.info("[BRIEF] user=%s: запрашиваю краткий контекст «%s»", user_id, display[:70])
    status = await callback.message.answer(
        f"⏳ Составляю краткий контекст «{_md(display)}»...",
        parse_mode=ParseMode.MARKDOWN,
    )
    try:
        result = await call_ai(user_id, BRIEF_PROMPT, txt_content, task="brief")
        logger.info("[BRIEF] user=%s: результат %d симв.: %r", user_id, len(result or ""), (result or "")[:60])
        if not result or result.startswith("❌"):
            await status.edit_text(result or "❌ Нейросеть не ответила.", parse_mode=ParseMode.MARKDOWN)
            return
        header = f"📌 **Кратко: {_md(display)}**\n\n"
        full = header + result
        kb = _brief_action_keyboard(user_id, display, list_id, idx)
        try:
            await status.edit_text(full, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
        except Exception:
            await status.edit_text(full, reply_markup=kb)
    except Exception as e:
        logger.error("[BRIEF] user=%s исключение: %s", user_id, e, exc_info=True)
        try:
            await status.edit_text(f"❌ Ошибка: {e}")
        except Exception:
            pass


@dp.callback_query(lambda c: c.data and c.data.startswith("brief_pending:"))
async def brief_pending_callback(callback: CallbackQuery, state: FSMContext):
    """Кнопка «📌 Кратко» на уведомлении о новом конспекте."""
    user_id = callback.from_user.id
    key = callback.data.split(":", 1)[1]
    pending = _load_wiki_pending(user_id)
    item = pending.get(key)
    if not item:
        await callback.answer("❌ Конспект устарел или уже обработан.", show_alert=True)
        return
    display, txt_content = item[1], item[2]
    if not txt_content:
        await callback.message.answer("❌ В письме не найден текст конспекта (txt-вложение).")
        return
    await callback.answer()
    logger.info("[BRIEF-PENDING] user=%s: краткий контекст «%s» (key=%r)", user_id, display[:70], key)
    status = await callback.message.answer(
        f"⏳ Составляю краткий контекст «{_md(display)}»...",
        parse_mode=ParseMode.MARKDOWN,
    )
    try:
        result = await call_ai(user_id, BRIEF_PROMPT, txt_content, task="brief")
        if not result or result.startswith("❌"):
            await status.edit_text(result or "❌ Нейросеть не ответила.", parse_mode=ParseMode.MARKDOWN)
            return
        header = f"📌 **Кратко: {_md(display)}**\n\n"
        full = header + result
        kb = _brief_action_keyboard_pending(user_id, display, key)
        try:
            await status.edit_text(full, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
        except Exception:
            await status.edit_text(full, reply_markup=kb)
    except Exception as e:
        logger.error("[BRIEF-PENDING] user=%s исключение: %s", user_id, e, exc_info=True)
        try:
            await status.edit_text(f"❌ Ошибка: {e}")
        except Exception:
            pass


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
                            f"Prompt with topic \"{_md(topic)}\" already exists! "
                            f"Current text:\n`{prompts[topic][:200]}`\n\n"
                            "Enter a **different** topic:",
                            parse_mode=ParseMode.MARKDOWN,
                        )
                        await state.set_state(AddPromptState.topic)
                        return
                    await state.update_data(topic=topic)
                    await message.answer(
                        f"Topic \"{_md(topic)}\" accepted.\n\n"
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
            topics = "\n".join(f"• {_md(t)}" for t in sorted_topics)
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
            topics = "\n".join(f"• {_md(t)}" for t in sorted_topics)
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
            topics = "\n".join(f"• {_md(t)}" for t in sorted_topics)
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
    await _hide_reply_keyboard(message.chat.id)
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
                        f"⚠️ Промпт с темой «{_md(topic)}» уже существует!\n"
                        f"Текущий текст:\n`{prompts[topic][:200]}`\n\n"
                        "Введите **другую** тему или пришлите другой файл:",
                        parse_mode=ParseMode.MARKDOWN,
                    )
                    return
                await state.update_data(topic=topic, file_text=file_text)
                await message.answer(
                    f"✅ Из файла определена тема: **«{_md(topic)}»**\\n\\n"
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
                    f"📄 Текст из файла (первые 100 символов):\n`{_md(preview)}...`\n\n"
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
            f"⚠️ Промпт с темой «{_md(topic)}» уже существует!\n"
            f"Текущий текст:\n`{prompts[topic][:200]}`\n\n"
            "Введите **другую** тему:",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    await state.update_data(topic=topic)
    await message.answer(
        f"✅ Тема «{_md(topic)}» принята.\n\n"
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
        f"🧠 **Промпт «{_md(topic)}» добавлен в память.**\n\n"
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
            f"⚠️ Промпт с темой «{_md(topic)}» уже существует!\n"
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
            f"🧠 **Промпт «{_md(topic)}» добавлен в память.**\n\n"
            f"📄 Длина: {len(file_text)} символов (из файла)",
            parse_mode=ParseMode.MARKDOWN,
        )
        await state.clear()
        list_text = _format_prompt_list()
        await message.answer(list_text, parse_mode=ParseMode.MARKDOWN, reply_markup=_prompt_keyboard())
    else:
        await message.answer(
            f"✅ Тема «{_md(topic)}» принята.\n\n"
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
                full = f"📌 **{_md(topic)}**\n\n{_md(text)}"
                if len(full) <= MAX_MSG_LEN:
                    await message.answer(full, parse_mode=ParseMode.MARKDOWN)
                else:
                    await message.answer(
                        f"📌 **{_md(topic)}**\n\n{_md(text[:MAX_MSG_LEN - 50])}\n\n"
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
                full = f"📌 **{_md(arg)}**\n\n{_md(text)}"
                if len(full) <= MAX_MSG_LEN:
                    await message.answer(full, parse_mode=ParseMode.MARKDOWN)
                else:
                    await message.answer(
                        f"📌 **{_md(arg)}**\n\n{_md(text[:MAX_MSG_LEN - 50])}\n\n"
                        f"_…текст слишком длинный, сохранён в боте_",
                        parse_mode=ParseMode.MARKDOWN,
                    )
                return

    # Без аргументов — запускаем FSM диалог выбора
    topics = "\n".join(f"• {_md(t)}" for t in sorted_topics)
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
        topics = "\n".join(f"• {_md(t)}" for t in sorted_topics)
        await message.answer(
            f"⚠️ Не найдено. Доступные промпты:\n{topics}\n\n"
            "Введите **тему** или **номер**:",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    text = prompts[topic]
    full = f"📌 **{_md(topic)}**\n\n{_md(text)}"
    if len(full) <= MAX_MSG_LEN:
        await message.answer(full, parse_mode=ParseMode.MARKDOWN)
    else:
        await message.answer(
            f"📌 **{_md(topic)}**\n\n{_md(text[:MAX_MSG_LEN - 50])}\n\n"
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
                    f"🗑 **Промпт «{_md(topic)}» удалён.**",
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
                    f"🗑 **Промпт «{_md(arg)}» удалён.**",
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=_prompt_keyboard(),
                )
                return

    # Без аргументов — FSM диалог
    topics = "\n".join(f"• {_md(t)}" for t in sorted_topics)
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
        topics = "\n".join(f"• {_md(t)}" for t in sorted_topics)
        await message.answer(
            f"⚠️ Не найдено. Доступные промпты:\n{topics}\n\n"
            "Введите **тему** или **номер**:",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    del prompts[topic]
    _save_prompts(prompts)

    await message.answer(
        f"🗑 **Промпт «{_md(topic)}» удалён.**",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=_prompt_keyboard(),
    )
    await state.clear()

    # Автоматически показываем обновлённый список
    text = _format_prompt_list()
    await _hide_reply_keyboard(message.chat.id)
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
                    f"📝 Редактирование промпта **«{_md(topic)}»**\n\n"
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
                    f"📝 Редактирование промпта **«{_md(arg)}»**\n\n"
                    f"Текущий текст:\n`{prompts[arg][:200]}`\n\n"
                    "Введите **новый текст** промпта:",
                    parse_mode=ParseMode.MARKDOWN,
                )
                await state.set_state(EditPromptState.text)
                return

    # Без аргументов — спрашиваем тему
    topics = "\n".join(f"• {_md(t)}" for t in sorted_topics)
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
        topics = "\n".join(f"• {_md(t)}" for t in sorted_topics)
        await message.answer(
            f"⚠️ Не найдено. Доступные промпты:\n{topics}\n\n"
            "Введите **тему** или **номер**:",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    await state.update_data(topic=topic)
    await message.answer(
        f"📝 Редактирование промпта **«{_md(topic)}»**\n\n"
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
    await _hide_reply_keyboard(message.chat.id)
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
    return f"❓ Раздел справки «{_md(section)}» не найден.\n\nДоступные разделы: {available}"
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
                            f"Пользователь: {_md(display_name, parse_mode=ParseMode.MARKDOWN)}\n"
                            f"ID: `{user_id}`\n"
                            f"Username: @{_md(user.username or '-')}\n\n"
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
    # Фото-логотип HuntTech перед приветствием (бренд; при ошибке — не мешаем)
    await _send_logo(message.from_user.id)
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

    # Настроенный пользователь — приветственное сообщение
    # (стандарт HuntTech: Markdown для форматирования, нижнее меню
    # актуальной клавиатуры — Telegram кэширует ReplyKeyboard по чату)
    ai_cfg = get_ai_config(user_id)
    ai_model = (ai_cfg or {}).get("model") or "не настроен"
    await _hide_reply_keyboard(message.chat.id)
    await message.answer(
        "🚀 HuntTech Protocols Bot\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "👋 Добро пожаловать!\n"
        "✅ Бот готов к работе!\n"
        f"🤖 AI: {ai_model}\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "📋 Назначение: бот для автоматизации конспектов встреч из почты.\n"
        "Извлекает отчёты из писем IMAP, генерирует саммари через нейросеть, "
        "сохраняет в базу знаний (Wiki) и присылает уведомления.\n"
        "Поддерживает несколько AI-моделей с автоматическим fallback (DeepSeek → NVIDIA → OpenRouter free).\n"
        "\n"
        "Как это работает:\n"
        "1️⃣ /list — непрочитанные конспекты встреч\n"
        "2️⃣ /prompt — список промптов для саммари\n"
        "3️⃣ /setup — настройка почты и AI\n"
        "4️⃣ /help — все команды\n"
        "\n"
        "Напиши /help — покажу все команды.",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=_main_menu_keyboard(),
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
                    f"Пользователь: {_md(display_name, parse_mode=ParseMode.MARKDOWN)}\n"
                    f"ID: `{user_id}`\n"
                    f"Username: @{_md(user.username or '-')}\n\n"
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
        header, items = await asyncio.to_thread(fetch_notes, user.id)
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
    list_id = _save_notes_cache(user.id, items)

    total = len(items)
    await message.answer(f"📋 **Новые конспекты встреч** — всего {total}", parse_mode=ParseMode.MARKDOWN)

    # Каждый конспект — отдельное сообщение с собственной кнопкой
    for idx, item in enumerate(items, 1):
        dt, display = item[0], item[1]
        date_str = dt.strftime("%d.%m.%Y %H:%M")
        text = f"**{idx}.** {_md(display)}\n📅 {date_str}"
        button = _get_item_button(idx, display, user.id, list_id)
        await message.answer(text, parse_mode=ParseMode.MARKDOWN, reply_markup=button, hide_reply_keyboard=True)


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

    list_id = _save_notes_cache(user.id, items)

    total = len(items)
    # Скрываем нижнюю клавиатуру, чтобы кнопки не уходили за неё
    await _hide_reply_keyboard(message.chat.id)
    await message.answer(f"New conspects: {total} total", parse_mode=ParseMode.MARKDOWN)

    for idx, item in enumerate(items, 1):
        dt, display = item[0], item[1]
        date_str = dt.strftime("%d.%m.%Y %H:%M")
        text = f"**{idx}.** {_md(display)}\n{date_str}"
        button = _get_item_button(idx, display, user.id, list_id)
        await message.answer(text, parse_mode=ParseMode.MARKDOWN, reply_markup=button, hide_reply_keyboard=True)


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

    list_id = _save_notes_cache(user.id, items)

    total = len(items)
    await message.answer(f"📋 **Конспекты встреч за неделю** — всего {total}", parse_mode=ParseMode.MARKDOWN)

    for idx, item in enumerate(items, 1):
        dt, display = item[0], item[1]
        date_str = dt.strftime("%d.%m.%Y %H:%M")
        text = f"**{idx}.** {_md(display)}\n📅 {date_str}"
        button = _get_item_button(idx, display, user.id, list_id)
        await message.answer(text, parse_mode=ParseMode.MARKDOWN, reply_markup=button, hide_reply_keyboard=True)


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
        f"⏳ Тестирую подключение к **{_md(model)}**...\n"
        f"🔗 `{endpoint}`",
        parse_mode=ParseMode.MARKDOWN,
    )

    result = await _test_ai_connection(endpoint, api_key, model)
    await message.answer(
        f"🧪 **Результат теста AI**\n\n"
        f"🔗 Endpoint: `{endpoint}`\n"
        f"📝 Модель: `{model}`\n\n"
        f"{_md(result)}",
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
                f"✅ **Режим публикации в Wiki:** {_md(mode_labels.get(mode, mode))}\n\n"
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
                await message.answer(f"❌ **Ошибка:** {_md(e)}", parse_mode=ParseMode.MARKDOWN)
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
                await message.answer(f"❌ **Ошибка:** {_md(e)}", parse_mode=ParseMode.MARKDOWN)
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


def _setup_field_nav_keyboard() -> "ReplyKeyboardMarkup":
    """Нижнее меню мастера настройки поля /setup email:
    «Редактировать» — ввести новое значение;
    «Оставить» — сохранить текущее значение (как /skip) и перейти дальше;
    «Следующий» — перейти к следующему полю, не меняя текущее."""
    from aiogram.types import KeyboardButton, ReplyKeyboardMarkup

    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text="Редактировать"),
                KeyboardButton(text="Оставить"),
                KeyboardButton(text="Следующий"),
            ],
        ],
        resize_keyboard=True,
    )


async def _show_field_step(
    message: Message,
    state: FSMContext,
    section: str,
    field: str,
) -> None:
    """Показывает шаг мастера настройки поля: текущее значение + нижнее меню
    «Редактировать / Оставить / Следующий». state: {section, field, fields, idx}."""
    spec = SETUP_SECTIONS[section]
    fields = [f for f, _ in spec["fields"]]
    idx = fields.index(field) if field in fields else 0
    await state.update_data(section=section, field=field, fields=fields, idx=idx)

    label = SETUP_FIELD_LABELS.get(field, _md(field))
    cfg, _, _ = _section_config(message.from_user.id, section)
    current = cfg.get(field, "")
    if current:
        masked = f"`{current[:20]}...`" if field in ("password", "api_key") else f"`{current}`"
        cur_line = f"Текущее значение: {masked}"
    else:
        cur_line = "Текущее значение не задано"

    await message.answer(
        f"{label}\n\n{cur_line}\n\n"
        "Выберите действие:",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=_setup_field_nav_keyboard(),
    )
    await state.set_state(SetupSingleField.value)


async def _next_field_step(
    message: Message,
    state: FSMContext,
    *,
    apply_current: bool = False,
) -> bool:
    """Переход к следующему полю секции в мастере /setup email.
    apply_current=True — сохранить текущее значение (кнопка «Оставить»).
    Возвращает False, если поля закончились (мастер завершён)."""
    from aiogram.types import ReplyKeyboardRemove

    data = await state.get_data()
    section = data.get("section", "email")
    fields = data.get("fields") or [f for f, _ in SETUP_SECTIONS[section]["fields"]]
    idx = data.get("idx", 0)
    field = data.get("field", fields[0] if fields else "email")
    user_id = message.from_user.id

    if apply_current:
        cfg, _, _ = _section_config(user_id, section)
        value = cfg.get(field, "")
        if value:
            _apply_single_field(user_id, section, field, value, skipped=True)

    nxt = idx + 1
    if nxt >= len(fields):
        # Мастер завершён — убираем нижнее меню, возвращаем меню секции
        await state.clear()
        await message.answer(
            "✅ Настройка завершена.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=ReplyKeyboardRemove(),
        )
        await message.answer(
            _setup_section_text(user_id, section),
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=_setup_section_keyboard(user_id, section),
        )
        return False

    await _show_field_step(message, state, section, fields[nxt])
    return True


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
    await message.answer("🔧 Главное меню:", parse_mode=ParseMode.MARKDOWN, reply_markup=_main_menu_keyboard())


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
                f"⚠️ **{_md(err)}**\n\n"
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
        f"**IMAP-сервер** ({_md(current)}):\n"
        "Введите адрес IMAP-сервера\n"
        "(например: `imap.yandex.ru`, `imap.mail.ru`)\n"
        "или `/skip` — оставить текущий:",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=_setup_skip_done_keyboard(),
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
                f"⚠️ **{_md(err)}**\n\n"
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
        f"**Логин** ({_md(current)}):\n"
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
        f"**Пароль** ({_md(current)}):\n"
        "Введите пароль приложения для IMAP\n"
        "(для Яндекса — создайте пароль приложения в настройках почты)\n"
        "или `/skip` — оставить текущий:",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=_setup_skip_done_keyboard(),
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

    # Кнопка «Готово» — досрочное завершение настройки (пароль не введён)
    if text == "Готово":
        await _finish_email_setup_early(message, state)
        return

    if text.lower() in ("/skip", "-") or text == "Оставить прежнее":
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
                f"⚠️ **{_md(err)}**\n\nВведите пароль приложения (минимум 4 символа):",
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
            f"❌ **Ошибка подключения:**\n\n{_md(details)}\n\n"
            "Попробуйте ещё раз:\n"
            "• Убедитесь, что IMAP включён в настройках почты\n"
            "• Проверьте логин и пароль\n\n"
            "Начните заново: `/setup`",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=ReplyKeyboardRemove(),
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
        f"{_md(report)}",
        parse_mode=ParseMode.MARKDOWN,
    )

    # Автоматически показываем справку — чтобы новый пользователь
    # сразу видел, какие команды доступны. ReplyKeyboardRemove — убрать
    # нижнее меню «Оставить прежнее/Готово» после завершения настройки.
    await message.answer(
        _help_text(),
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=ReplyKeyboardRemove(),
    )

    # Спрашиваем, хочет ли пользователь настроить AI для Саммари
    await message.answer(
        "🤖 Хотите настроить подключение к нейросети?\n"
        "Это нужно, чтобы кнопка «Саммари» работала.",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="🤖 Да, настроить AI", callback_data="ai_after_setup:yes", hide_reply_keyboard=True),
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
       После ввода значения — кнопки «✅ Подтвердить / ✏️ Редактировать / 🚫 Отмена».
       Кнопки мастера «Редактировать / Оставить / Следующий» — пошаговый обход полей."""
    text = message.text.strip()

    data = await state.get_data()
    section = data.get("section", "email")
    field = data.get("field", "email")
    user_id = message.from_user.id

    # ── Кнопки мастера /setup email: «Редактировать / Оставить / Следующий» ──
    if text == "Редактировать":
        cfg, _, _ = _section_config(user_id, section)
        current = ""
        if cfg.get(field):
            secret = field in ("password", "api_key")
            current = f"\n\nТекущее значение: `{cfg[field][:20]}...`" if secret else f"\n\nТекущее значение: `{cfg[field]}`"
        await message.answer(
            f"{_single_field_prompt(field)}{current}\n\n"
            "или `/skip` — оставить текущее значение:",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=_setup_field_nav_keyboard(),
        )
        return

    if text == "Оставить":
        cfg, _, _ = _section_config(user_id, section)
        value = cfg.get(field, "")
        if not value:
            await message.answer(
                f"⚠️ Текущее значение `{field}` не задано — "
                "нажмите «Редактировать», чтобы ввести его, "
                "или «Следующий» для перехода дальше.",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=_setup_field_nav_keyboard(),
            )
            return
        _apply_single_field(user_id, section, field, value, skipped=True)
        label = SETUP_FIELD_LABELS.get(field, _md(field))
        masked = f"`{value[:20]}...`" if field in ("password", "api_key") else f"`{value}`"
        await message.answer(
            f"✅ **{label}** оставлен: {masked}",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=_setup_field_nav_keyboard(),
        )
        await _next_field_step(message, state, apply_current=False)
        return

    if text == "Следующий":
        await _next_field_step(message, state, apply_current=False)
        return

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
            f"✅ **{SETUP_FIELD_LABELS.get(field, _md(field))}** сохранён (оставлено прежнее значение): `{value[:20]}...`"
            if field in ("password", "api_key") and len(value) > 20
            else f"✅ **{SETUP_FIELD_LABELS.get(field, _md(field))}** сохранён (оставлено прежнее значение): `{value}`",
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
                InlineKeyboardButton(text="✅ Подтвердить", callback_data="setup_sf:confirm", hide_reply_keyboard=True),
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
    }.get(field, f"✏️ **{_md(field)}** — введите значение:")


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

    label = SETUP_FIELD_LABELS.get(field, _md(field))

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
            f"{auto_note}",
            parse_mode=ParseMode.MARKDOWN,
        )
        # Мастер /setup email: после сохранения — следующий шаг (или завершение)
        if data.get("fields"):
            await _next_field_step(callback.message, state, apply_current=False)
            return
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
            reply_markup=_setup_field_nav_keyboard(),
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
    """Кнопка-параметр: начинает мастер настройки конкретного поля
    с нижним меню «Редактировать / Оставить / Следующий»."""
    _, section, field = callback.data.split(":", 2)
    user_id = callback.from_user.id
    await callback.answer()

    if section == "db" and user_id != db.ADMIN_USER_ID:
        await callback.message.answer("❌ Команда только для администратора.")
        return

    await _show_field_step(callback.message, state, section, field)


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
            f"**Email** ({_md(current)}):\n"
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
                f"❌ **Ошибка подключения:**\n\n{_md(details)}\n\n"
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
            f"✅ **Почта проверена!**\n\n{_md(report)}\n\n"
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
                f"❌ **Ошибка:** {_md(e)}",
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
            f"🧪 **Результат теста AI**\n\n🔗 Endpoint: `{endpoint}`\n📝 Модель: `{model}`\n\n{_md(result)}",
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


def _save_notes_cache(user_id: int, items: list) -> str:
    """Сохраняет снапшот списка конспектов в кэш (на диск, переживает рестарты).

    Каждый показ списка (/list, /list_new, /list_all) создаёт ОТДЕЛЬНУЮ
    запись с уникальным list_id — кнопки под конспектами несут этот
    list_id, поэтому нажатие всегда работает со СВОИМ списком, даже если
    позже бот показал другой список или фоновый цикл что-то сохранил.

    Возвращает list_id (короткий хеш). Хранится до 5 последних списков
    на пользователя (старые вытесняются).
    """
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

    list_id = _short_uid(f"{time.time()}:{len(items)}")[:8]
    per_user = cache.setdefault(str(user_id), {})
    if isinstance(per_user, list):
        # Миграция старого формата {user_id: [items]} → {user_id: {list_id: ...}}
        logger.info("[NOTES-CACHE] user=%s: миграция старого формата кэша (%d записей)",
                    user_id, len(per_user))
        legacy_id = _short_uid("legacy-format")[:8]
        cache[str(user_id)] = {legacy_id: {"items": per_user, "ts": 0}}
        per_user = cache[str(user_id)]
    per_user[list_id] = {"items": serialized, "ts": time.time()}
    # Оставляем 5 последних списков
    while len(per_user) > 5:
        oldest = min(per_user, key=lambda k: per_user[k].get("ts", 0))
        del per_user[oldest]
    NOTES_CACHE_FILE.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("[NOTES-CACHE] user=%s: сохранён снапшот list_id=%r (%d конспектов)",
                user_id, list_id, len(items))
    return list_id


def _load_notes_cache(user_id: int, list_id: str | None = None) -> list:
    """Загружает снапшот списка конспектов пользователя.

    list_id задан — возвращает именно этот список (кнопка из показанного
    когда-то /list); None — самый свежий снапшот (обратная совместимость).
    """
    if not NOTES_CACHE_FILE.exists():
        return []
    try:
        cache = json.loads(NOTES_CACHE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return []
    per_user = cache.get(str(user_id), {})
    if isinstance(per_user, list):
        # Миграция старого формата {user_id: [items]}
        legacy_id = _short_uid("legacy-format")[:8]
        per_user = {legacy_id: {"items": per_user, "ts": 0}}
    if not per_user:
        return []
    if list_id:
        snapshot = per_user.get(list_id)
        if not snapshot:
            logger.warning("[NOTES-CACHE] user=%s: list_id=%r не найден — список устарел", user_id, list_id)
            return []
        serialized = snapshot.get("items", [])
    else:
        latest = max(per_user.values(), key=lambda s: s.get("ts", 0))
        serialized = latest.get("items", [])
    items = []
    for entry in serialized:
        dt = datetime.fromisoformat(entry["dt"]) if entry.get("dt") else datetime.now()
        imap_id = entry.get("imap_id", "")
        items.append((dt, entry["display"], entry["txt"], "", "", imap_id))
    return items


# ═══════════════════════════════════════════════════════════════════
# КЭШ КОНСПЕКТОВ ДЛЯ КНОПКИ «Расшифровать и разместить в wiki»
# ═══════════════════════════════════════════════════════════════════
# При уведомлении о новом конспекте бот сохраняет его сюда (по uid),
# чтобы по нажатию кнопки достать полный текст без повторного IMAP-запроса.

WIKI_PENDING_FILE = Path(__file__).parent / "wiki_pending.json"
SUMMARY_CACHE_FILE = Path(__file__).parent / "summary_cache.json"


def _save_wiki_pending(user_id: int, item) -> None:
    """Сохраняет конспект в кэш ожидающих wiki-обработки."""
    cache = {}
    if WIKI_PENDING_FILE.exists():
        try:
            cache = json.loads(WIKI_PENDING_FILE.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning("[WIKI-PENDING] Файл повреждён (%s), стартуем с пустым кэшем", e)
            cache = {}
    dt, display, txt = item[0], item[1], item[2]
    imap_id = item[5] if len(item) >= 6 else ""
    full_uid = f"{dt.timestamp()}:{display}"
    key = _short_uid(full_uid)
    per_user = cache.setdefault(str(user_id), {})
    per_user[key] = {
        "dt": dt.isoformat(),
        "display": display,
        "txt": txt,
        "imap_id": imap_id,
    }
    WIKI_PENDING_FILE.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("[WIKI-PENDING] Сохранён конспект user=%s key=%r (всего у пользователя: %d)",
                user_id, key, len(per_user))


def _load_wiki_pending(user_id: int) -> dict:
    """Загружает кэш конспектов, ожидающих wiki-обработки (uid → item)."""
    if not WIKI_PENDING_FILE.exists():
        logger.info("[WIKI-PENDING] Файл кэша отсутствует — пусто (user=%s)", user_id)
        return {}
    try:
        cache = json.loads(WIKI_PENDING_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        logger.error("[WIKI-PENDING] Ошибка чтения кэша: %s", e)
        return {}
    per_user = cache.get(str(user_id), {})
    items = {}
    for uid, entry in per_user.items():
        try:
            dt = datetime.fromisoformat(entry["dt"])
        except Exception as e:
            logger.warning("[WIKI-PENDING] Пропускаю битую запись %r: %s", uid[:40], e)
            continue
        items[uid] = (dt, entry["display"], entry["txt"], "", "", entry.get("imap_id", ""))
    logger.info("[WIKI-PENDING] Загружено %d ожидающих конспектов (user=%s)", len(items), user_id)
    return items


def _remove_wiki_pending(user_id: int, uid: str) -> None:
    """Удаляет конспект из кэша ожидающих wiki-обработки."""
    if not WIKI_PENDING_FILE.exists():
        return
    try:
        cache = json.loads(WIKI_PENDING_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        logger.error("[WIKI-PENDING] Ошибка чтения кэша при удалении: %s", e)
        return
    per_user = cache.get(str(user_id), {})
    if uid in per_user:
        del per_user[uid]
        if per_user:
            cache[str(user_id)] = per_user
        else:
            cache.pop(str(user_id), None)
        WIKI_PENDING_FILE.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")
        logger.info("[WIKI-PENDING] Удалён конспект user=%s uid=%r", user_id, uid[:60])
    else:
        logger.warning("[WIKI-PENDING] uid=%r не найден в кэше (user=%s) — возможно, уже обработан", uid[:60], user_id)


# ── Кэш саммари для кнопки «Показать саммари» ───────────────
# После расшифровки текст протокола НЕ выводится сразу — сохраняем
# его в кэш и показываем только по нажатию кнопки.


def _save_summary_cache(user_id: int, uid: str, display: str, summary: str, wiki_url: str = "") -> None:
    """Сохраняет текст саммари в кэш (последние 20 на пользователя).
       wiki_url — ссылка на протокол в Вики (для кнопки «📢 Опубликовать в группе»)."""
    cache = {}
    if SUMMARY_CACHE_FILE.exists():
        try:
            cache = json.loads(SUMMARY_CACHE_FILE.read_text(encoding="utf-8"))
        except Exception:
            cache = {}
    per_user = cache.setdefault(str(user_id), {})
    per_user[uid] = {"display": display, "summary": summary, "wiki_url": wiki_url, "ts": time.time()}
    while len(per_user) > 20:
        oldest = min(per_user, key=lambda k: per_user[k].get("ts", 0))
        del per_user[oldest]
    SUMMARY_CACHE_FILE.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("[SUMMARY-CACHE] user=%s: сохранено саммари uid=%r (%d симв.)",
                user_id, uid[:40], len(summary or ""))


def _load_summary_cache(user_id: int) -> dict:
    """Загружает кэш саммари пользователя (uid → {display, summary, ts})."""
    if not SUMMARY_CACHE_FILE.exists():
        return {}
    try:
        cache = json.loads(SUMMARY_CACHE_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        logger.error("[SUMMARY-CACHE] Ошибка чтения кэша: %s", e)
        return {}
    return cache.get(str(user_id), {})


async def _send_summary(target, display: str, summary: str) -> None:
    """Выводит саммари в чат. Заголовок — с MARKDOWN; тело — с MARKDOWN,
       при невалидной разметке от AI — fallback на обычный текст."""
    header = f"📄 **Саммари: {_md(display)}**\n\n---\n\n"
    full = header + summary
    if len(full) <= 4000:
        try:
            await target.answer(full, parse_mode=ParseMode.MARKDOWN)
        except Exception:
            await target.answer(full)
    else:
        try:
            await target.answer(header, parse_mode=ParseMode.MARKDOWN)
        except Exception:
            await target.answer(header)
        for i in range(0, len(summary), 3500):
            chunk = summary[i:i + 3500]
            try:
                await target.answer(chunk, parse_mode=ParseMode.MARKDOWN)
            except Exception:
                await target.answer(chunk)


# ═══════════════════════════════════════════════════════════════════
# ФУНКЦИЯ ВЫЗОВА НЕЙРОСЕТИ (call_ai)
# ═══════════════════════════════════════════════════════════════════
# Универсальный вызов любого OpenAI-совместимого API.
# Поддерживает OpenRouter, OpenAI, DeepSeek, vLLM и т.д.

def _get_openrouter_key() -> str:
    """Возвращает OpenRouter API key из users.json._fallback, .env или admin-конфига.

    Приоритет: users.json "_fallback" → .env OPENROUTER_API_KEY → admin-конфиг (если OpenRouter).
    """
    try:
        users = _load_users()
        fb = users.get("_fallback") or {}
        if fb.get("api_key"):
            return fb["api_key"]
    except Exception:
        pass
    env_key = os.getenv("OPENROUTER_API_KEY", "").strip()
    if env_key:
        return env_key
    try:
        admin_id = 272980897  # ADMIN_USER_ID
        admin_cfg = get_user_config(admin_id) or {}
        ai = admin_cfg.get("ai", {}) or {}
        if "openrouter" in ai.get("endpoint", "").lower() and ai.get("api_key"):
            return ai["api_key"]
    except Exception:
        pass
    return ""


async def _notify_admin(message: str) -> None:
    """Отправляет уведомление администратору о переключении AI."""
    admin_id = 272980897  # ADMIN_USER_ID
    try:
        await bot.send_message(
            chat_id=admin_id,
            text=f"🤖 **AI Fallback**\n{message}",
            parse_mode=ParseMode.MARKDOWN,
        )
    except Exception as e:
        logger.warning("AI fallback notification failed: %s", e)
        logger.error("Failed to notify admin about AI fallback: %s", e, exc_info=True)


def _build_multi_fallback_ai_client(user_id: int):
    """Создаёт MultiFallbackAIClient. Защита от строковых user_id (например '_fallback')."""
    try:
        user_id = int(user_id)
    except (ValueError, TypeError):
        logger.warning("Некорректный user_id для AI fallback: %r (тип: %s)", user_id, type(user_id).__name__)
        return None
    """Создаёт MultiFallbackAIClient: primary → fallback1 → OpenRouter free-модели."""
    # Защита: user_id должен быть числом, не строкой (избегаем ошибки '_fallback')
    try:
        user_id = int(user_id)
    except (ValueError, TypeError):
        logger.error("Неверный user_id для AI fallback: %r (должен быть int)", user_id)
        return None

    ai_config = get_ai_config(user_id)
    if not ai_config:
        return None

    primary_endpoint = ai_config.get("endpoint", "").rstrip("/")
    primary_api_key = ai_config.get("api_key", "")
    primary_model = ai_config.get("model", "deepseek-v4-flash")

    if not primary_api_key:
        return None

    # Fallback1 — через OpenRouter (NVIDIA Nemotron)
    fallback1_endpoint = "https://openrouter.ai/api/v1"
    fallback1_api_key = _get_openrouter_key()
    fallback1_model = os.getenv(
        "OPENROUTER_FALLBACK_MODEL", "nvidia/nemotron-3-super-120b-a12b:free"
    )

    if not fallback1_api_key:
        # Если нет OpenRouter-ключа, fallback-chain будет пустой
        # Используем обычный FallbackAIClient (только primary)
        return create_fallback_ai_client(
            primary_endpoint=primary_endpoint,
            primary_api_key=primary_api_key,
            primary_model=primary_model,
            fallback_endpoint=fallback1_endpoint,
            fallback_api_key=fallback1_api_key,
            fallback_model=fallback1_model,
            user_id=user_id,
            username="",
            bot_name="protocols-bot",
            notify_func=_notify_admin,
        )

    # Многоуровневый fallback: primary → fallback1 → список OpenRouter free-моделей
    return create_multi_fallback_ai_client(
        primary_endpoint=primary_endpoint,
        primary_api_key=primary_api_key,
        primary_model=primary_model,
        fallback1_endpoint=fallback1_endpoint,
        fallback1_api_key=fallback1_api_key,
        fallback1_model=fallback1_model,
        openrouter_api_key=fallback1_api_key,  # тот же ключ
        openrouter_endpoint=fallback1_endpoint,
        openrouter_models=OPENROUTER_FREE_MODELS,  # строго free!
        user_id=user_id,
        username="",
        bot_name="protocols-bot",
        notify_func=_notify_admin,
        proxy="http://tWQrfq:YtJRww@209.46.2.183:8000",
    )


def _build_fallback_ai_client(user_id: int):
    """Создаёт FallbackAIClient для пользователя: primary (из конфига) → NVIDIA через OpenRouter.

    Возвращает None если primary-конфиг неполон.
    """
    ai_config = get_ai_config(user_id)
    if not ai_config:
        return None

    primary_endpoint = ai_config.get("endpoint", "").rstrip("/")
    primary_api_key = ai_config.get("api_key", "")
    primary_model = ai_config.get("model", "deepseek-v4-flash")

    fallback_endpoint = "https://openrouter.ai/api/v1"
    fallback_api_key = _get_openrouter_key()
    fallback_model = os.getenv(
        "OPENROUTER_FALLBACK_MODEL", "nvidia/nemotron-3-super-120b-a12b:free"
    )

    if not primary_api_key or not fallback_api_key:
        return None

    return create_fallback_ai_client(
        primary_endpoint=primary_endpoint,
        primary_api_key=primary_api_key,
        primary_model=primary_model,
        fallback_endpoint=fallback_endpoint,
        fallback_api_key=fallback_api_key,
        fallback_model=fallback_model,
        user_id=user_id,
        username="",
        bot_name="protocols-bot",
        notify_func=_notify_admin,
    )


async def call_ai(user_id: int, system_prompt: str, user_text: str, task: str = "call_ai") -> str:
    """
    Вызывает нейросеть через OpenAI-совместимый API с fallback-схемой:
    primary (из users.json пользователя) → fallback (NVIDIA Nemotron через OpenRouter).

    Стратегия (см. AI_FALLBACK_STRATEGY.md):
      1. Берёт primary endpoint/api_key/model из users.json (настройки пользователя).
      2. Если primary не отвечает (любая ошибка) → переключается на OpenRouter + NVIDIA.
      3. При каждом переключении уведомляет администратора в Telegram.
      4. Если обе модели упали — возвращает сообщение об ошибке.

    Бизнес-правила:
    - Всегда возвращает строку (ответ или ошибку) — никогда не падает
    - Учёт расходов: каждое обращение пишется в общий реестр (~/.hermes/hunttech_bots/ai_usage.json)
    - Для OpenRouter автоматически добавляет HTTP-Referer / X-Title

    Returns:
        str — ответ нейросети или сообщение об ошибке, начинающееся с ❌
    """
    import time
    from urllib.parse import urlparse

    ai_config = get_ai_config(user_id)
    if not ai_config:
        return "❌ AI не настроен. Используйте `/setup_ai`"

    endpoint = ai_config.get("endpoint", "").rstrip("/")
    api_key = ai_config.get("api_key", "")
    model = ai_config.get("model", "deepseek-v4-flash")

    if not endpoint or not api_key:
        return "❌ AI настроен не полностью. Проверьте endpoint и API key через `/setup_ai`"

    # Если пользователь сам использует OpenRouter (legacy) — fallback не нужен,
    # делаем прямой вызов.
    is_openrouter = "openrouter" in endpoint.lower()
    fallback_key = _get_openrouter_key()

    if is_openrouter or not fallback_key:
        # Прямой вызов (legacy путь) — без fallback.
        return await _call_ai_direct(
            user_id, endpoint, api_key, model, system_prompt, user_text, task,
        )

    # Многоуровневая fallback-схема (2026-09-01):
    # primary (конфиг пользователя) → fallback1 (NVIDIA) → OpenRouter free-модели
    client = _build_multi_fallback_ai_client(user_id)
    if not client:
        return await _call_ai_direct(
            user_id, endpoint, api_key, model, system_prompt, user_text, task,
        )

    started = time.monotonic()
    try:
        logger.info("🤖 AI вызов через MultiFallbackAIClient (user=%s, task=%s)", user_id, task)
        resp = await client.complete(
            system_prompt=system_prompt,
            user_prompt=user_text,
            task=task,
        )
        duration = (time.monotonic() - started) * 1000
        content_text = resp.content or ""
        # Трекинг расходов
        try:
            from hunttech_bot_common.ai import AIResponse  # noqa
            usage = resp.usage or {}
            provider_name = f"{urlparse(endpoint).netloc or endpoint}→multi-fallback"
            _track_usage(
                user_id, provider_name, model, task, "ok", usage, duration,
            )
        except Exception as exc:
            logger.warning("usage track failed: %s", exc)
        return content_text
    except Exception as e:
        # MultiFallbackAIClient сам уже уведомил админа. Просто сообщаем пользователю.
        logger.error("❌ MultiFallbackAIClient call failed completely (user=%s, task=%s): %s", user_id, task, e, exc_info=True)
        logger.warning("MultiFallbackAIClient call failed completely: %s", e)
        return f"❌ Все модели AI недоступны. Проверьте ключи и подключение."


async def _call_ai_direct(
    user_id: int, endpoint: str, api_key: str, model: str,
    system_prompt: str, user_text: str, task: str,
) -> str:
    """Прямой вызов LLM без fallback (для legacy OpenRouter-конфигов и тестов)."""
    import time
    from urllib.parse import urlparse

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
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_text},
        ],
    }
    provider = urlparse(endpoint).netloc or endpoint
    started = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=120) as client:
            response = await client.post(
                f"{endpoint}/chat/completions",
                headers=headers,
                json=payload,
            )
            if response.status_code != 200:
                _track_usage(user_id, provider, model, task, "error", None, 0.0)
                return f"❌ Ошибка API ({response.status_code}): {response.text[:500]}"
            result = response.json()
            duration = (time.monotonic() - started) * 1000
            usage = result.get("usage") or {}
            _track_usage(user_id, provider, model, task, "ok", usage, duration)
            return result["choices"][0]["message"]["content"]
    except httpx.TimeoutException:
        _track_usage(user_id, provider, model, task, "error", None, 0.0)
        return "❌ Таймаут: нейросеть не ответила за 120 секунд"
    except Exception as e:
        _track_usage(user_id, provider, model, task, "error", None, 0.0)
        return f"❌ Ошибка: {e}"


def _track_usage(user_id: int, provider: str, model: str, task: str,
                 status: str, usage: dict | None, duration_ms: float) -> None:
    """Запись обращения к нейросети в общий реестр расходов."""
    try:
        u = usage or {}
        input_tokens = int(u.get("prompt_tokens") or 0)
        output_tokens = int(u.get("completion_tokens") or 0)
        total = int(u.get("total_tokens") or 0)
        cost = estimate_cost(model, input_tokens, output_tokens)
        _usage_tracker.append(UsageRecord(
            bot_name="protocols",
            user_id=user_id,
            username="",
            provider=provider,
            model=model,
            task=task,
            status=status,
            prompt_tokens=input_tokens,
            completion_tokens=output_tokens,
            total_tokens=total,
            duration_ms=duration_ms,
            cost_usd=cost,
            source="личные",
        ))
    except Exception as exc:
        logger.warning("usage track failed: %s", exc)


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

    from urllib.parse import urlparse
    _test_provider = urlparse(endpoint).netloc or endpoint

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.post(
                f"{endpoint}/chat/completions",
                headers=headers,
                json=payload,
            )
            if response.status_code == 200:
                result = response.json()
                _track_usage(0, _test_provider, model,
                             "test_connection", "ok", result.get("usage"), 0.0)
                reply = result["choices"][0]["message"]["content"]
                return f"✅ Подключение успешно!\nОтвет модели: «{_md(reply.strip())}»"
            elif response.status_code == 401:
                _track_usage(0, _test_provider, model,
                             "test_connection", "error", None, 0.0)
                return "❌ Ошибка авторизации (401). Проверьте API-ключ."
            elif response.status_code == 404:
                _track_usage(0, _test_provider, model,
                             "test_connection", "error", None, 0.0)
                return "❌ Модель не найдена (404). Проверьте название модели."
            else:
                _track_usage(0, _test_provider, model,
                             "test_connection", "error", None, 0.0)
                return f"❌ Ошибка API ({response.status_code}): {response.text[:300]}"
    except httpx.TimeoutException:
        _track_usage(0, _test_provider, model,
                     "test_connection", "error", None, 0.0)
        return "❌ Таймаут: сервер не ответил за 15 секунд. Проверьте endpoint."
    except httpx.ConnectError:
        _track_usage(0, _test_provider, model,
                     "test_connection", "error", None, 0.0)
        return "❌ Не удалось подключиться к серверу. Проверьте endpoint."
    except Exception as e:
        _track_usage(0, _test_provider, model,
                     "test_connection", "error", None, 0.0)
        return f"❌ Ошибка: {e}"


# ═══════════════════════════════════════════════════════════════════
# КОМАНДА /usage — РАСХОДЫ НА НЕЙРОСЕТЬ (только администратор)
# ═══════════════════════════════════════════════════════════════════

@dp.message(Command("usage"))
async def cmd_usage(message: Message, command: CommandObject | None = None):
    """Отчёт по расходам на нейросеть (общий реестр всех HuntTech-ботов).

    Периоды: /usage — сегодня; week/month/all/N — 7/30/всё время/N дней.
    """
    user_id = message.from_user.id
    if not access_manager.is_admin(user_id):
        await message.answer("🚫 Только администратор может смотреть расходы.",
                             parse_mode=ParseMode.MARKDOWN)
        return
    from hunttech_bot_common.ai import format_usage_report, usage_period_from_args
    args = (command.args or "").split() if command else []
    period = usage_period_from_args(args)
    text = format_usage_report(_usage_tracker, period=period, bot_name="Protocols")
    for i in range(0, len(text), 4000):
        await message.answer(text[i:i + 4000], parse_mode=ParseMode.MARKDOWN)


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
        f"⏳ Проверяю подключение к **{_md(provider_label)}**...",
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
            f"**{_md(test_result)}**\n\n"
            "Теперь кнопка «Саммари» будет работать!",
            parse_mode=ParseMode.MARKDOWN,
        )
    else:
        await message.answer(
            f"⚠️ **AI-настройки сохранены**, но тест не прошёл:\n\n"
            f"🧩 Провайдер: `{provider_label}`\n"
            f"🔗 Endpoint: `{endpoint}`\n"
            f"📝 Модель: `{model}`\n\n"
            f"{_md(test_result)}\n\n"
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
            f"{_md(report)}"
            f"{org_hint}",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    await state.clear()
    await status.edit_text(
        f"✅ **Яндекс Вики настроена!**\n\n"
        f"{_md(report)}",
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
            f"{_md(msg)}\n\n"
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

                        header, items = await asyncio.to_thread(fetch_new_notes, user_id)
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

                            # ── УСИЛЕННОЕ ЛОГИРОВАНИЕ: найдены новые конспекты ──
                            logger.info("[NOTIFY] user=%s: найдено новых конспектов: %d",
                                        user_id, len(new_notifications))
                            for _item in new_notifications:
                                logger.info("[NOTIFY]   → %s | %s | txt_len=%d",
                                            _item[0], _item[1][:70], len(_item[2] or ""))

                            for idx, item in enumerate(new_notifications, 1):
                                dt, display = item[0], item[1]
                                date_str = dt.strftime("%d.%m.%Y %H:%M")
                                text = (
                                    f"🔔 **Новый конспект встречи!**\n\n"
                                    f"**{idx}.** {_md(display)}\n"
                                    f"📅 {date_str}"
                                )
                                try:
                                    # Если настроена Яндекс Вики — под уведомлением
                                    # кнопка «Расшифровать и разместить в wiki».
                                    wiki_config = get_wiki_config(user_id)
                                    reply_markup = None
                                    if wiki_config and wiki_config.get("oauth_token") and get_wiki_mode(user_id) != "off":
                                        key = _short_uid(f"{dt.timestamp()}:{display}")
                                        _save_wiki_pending(user_id, item)
                                        logger.info("[NOTIFY] user=%s: кнопка wiki добавлена к уведомлению (key=%r)",
                                                    user_id, key)
                                        reply_markup = InlineKeyboardMarkup(inline_keyboard=[[
                                            InlineKeyboardButton(
                                                text="📝 Расшифровать и разместить в wiki",
                                                callback_data=f"wiki_process:{key}",
                                            )
                                        ], [
                                            InlineKeyboardButton(
                                                text="📌 Кратко",
                                                callback_data=f"brief_pending:{key}",
                                            )
                                        ]])
                                    await bot.send_message(
                                        chat_id=user_id,
                                        text=text,
                                        parse_mode=ParseMode.MARKDOWN,
                                        reply_markup=reply_markup,
                                    )
                                except Exception as e:
                                    logger.error(
                                        "Не удалось отправить уведомление user %s: %s",
                                        uid_str, e,
                                    )
                            # Сохраняем в кеш для кнопки Саммари
                            notified_ids = [f"{item[0].timestamp()}:{item[1]}" for item in new_notifications]
                            _mark_notified(user_id, notified_ids)
                            # ВНИМАНИЕ: НЕ перезаписываем notes_cache здесь!
                            # Фоновый цикл видит другой набор писем (fetch_new_notes),
                            # и его сохранение сбивало индексы кнопок из /list.
                            # Каждый показанный список — отдельный снапшот (list_id),
                            # фоновому циклу он не нужен (кнопки уведомлений идут
                            # через pending-кэш по ключу).

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
        # Администратору — боковое меню + /usage (scope chat, эталон docs).
        try:
            admin_cmds = cmds + [
                BotCommand(command="usage", description="💰 Расходы на нейросеть"),
            ]
            await bot.set_my_commands(
                commands=admin_cmds,
                scope=BotCommandScopeChat(chat_id=_master_admin_id),
            )
            logger.info("✅ Меню администратора установлено (%d команд)", len(admin_cmds))
        except Exception as e:
            logger.warning("⚠️ Не удалось установить меню администратора: %s", e)
    except Exception as e:
        logger.warning("⚠️ Не удалось установить меню команд: %s", e)

    # ── Приветствие администратору при каждом старте ─────────
    # (стандарт HuntTech, эталон — offer: plain text, parse_mode=None,
    # reply_markup — актуальная нижняя клавиатура: Telegram кэширует
    # ReplyKeyboard по чату, иначе после изменения состава кнопок
    # пользователь продолжает видеть старые (мёртвые) кнопки)
    if _master_admin_id:
        try:
            # Фото-логотип HuntTech перед стартовым приветствием
            await _send_logo(_master_admin_id)
            ai_cfg = get_ai_config(_master_admin_id)
            ai_model = (ai_cfg or {}).get("model") or "не настроен"
            startup_text = (
                "🚀 HuntTech Protocols Bot\n"
                f"🤖 Версия бота: {_bot_version()}\n"
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

    # ── Сводка изменений с прошлого запуска (стандарт HuntTech, эталон —
    # @hunttech_open_close_vacancy_bot): после приветствия, plain text.
    if _master_admin_id:
        try:
            from hunttech_bot_common.services.startup import send_startup_changelog

            REPO_DIR = Path(__file__).resolve().parent
            STATE_PATH = Path(__file__).resolve().parent / "data" / "startup_state.json"
            await send_startup_changelog(bot, _master_admin_id, repo_dir=REPO_DIR, state_path=STATE_PATH)
        except Exception as e:
            logger.warning("Changelog message failed: %s", e)

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())