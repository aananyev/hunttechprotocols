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

import db  # Модуль PostgreSQL (включается при наличии DB_HOST в .env)

# ── Логирование ──────────────────────────────────────────────────
from integrations.logging_adapter import logger

from aiogram import Bot, Dispatcher
from aiogram.filters import Command, CommandObject
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from aiogram.enums import ParseMode

import os
from dotenv import load_dotenv
from integrations.config_adapter import settings

# ── Конфигурация ──────────────────────────────────────────────

load_dotenv()
# TG_TOKEN — ключ от @BotFather, даёт боту доступ к Telegram API.
# Хранится в .env (не в git!), чтобы не светить секрет в репозитории.
TG_TOKEN = os.getenv("TG_TOKEN", "") or exit("❌ TG_TOKEN не задан! Положи токен в .env")

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

def _connect_imap(config: dict) -> imaplib.IMAP4_SSL:
    """
    Подключается к IMAP-серверу по настройкам пользователя.
    Бизнес-правило: логин для IMAP часто совпадает с email, 
    но бывает отличается (например, логин — часть email до @).
    """
    imap_login = config.get("login", config["email"])
    logger.info("Подключение к IMAP %s (login: %s)...", config["server"], imap_login)
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
    if not config:
        return ("Mail not configured. Use /setup.", [])
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
                report_parts.append(f"❌ **Ошибка API ({resp.status_code}):** {resp.text[:200]}")
                all_ok = False
    except httpx.TimeoutException:
        report_parts.append("❌ **Таймаут:** Яндекс Вики не ответил за 15 секунд.")
        all_ok = False
    except Exception as e:
        report_parts.append(f"❌ **Ошибка подключения:** {e}")
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
            report_parts.append(f"⚠️ **Ошибка при получении страниц:** {e}")

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
    if not config:
        return False
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
            lines.append(f"{idx}. {topic}")
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
        topics = "\n".join(f"• {t}" for t in sorted(prompts.keys()))
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
        topics = "\n".join(f"• {t}" for t in sorted(prompts.keys()))
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
        await message.answer("❌ Хорошо. Если захотите — напишите `/add_prompt`")
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
        "hint_model": "deepseek-chat",
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
    "deepseek": ["deepseek-chat", "deepseek-reasoner", "deepseek-v3"],
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
        await callback.message.answer(f"❌ Промпт «{prompt_topic}» не найден.")
        return

    if not txt_content:
        await callback.message.answer("❌ В письме не найден текст конспекта (txt-вложение).")
        return

    # Показываем статус — нейросеть может думать до минуты
    status_msg = await callback.message.answer(
        f"⏳ Обрабатываю «{display}» через нейросеть...",
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
    header = f"🧠 **Саммари: {display}**\n\n---\n\n"
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
        f"📝 Для конспекта «{display}» не найден подходящий промпт.\n\n"
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
        f"⏳ Генерирую саммари для «{display}»...",
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
                            f"Prompt with topic \"{topic}\" already exists! "
                            f"Current text:\n`{prompts[topic][:200]}`\n\n"
                            "Enter a **different** topic:",
                            parse_mode=ParseMode.MARKDOWN,
                        )
                        await state.set_state(AddPromptState.topic)
                        return
                    await state.update_data(topic=topic)
                    await message.answer(
                        f"Topic \"{topic}\" accepted.\n\n"
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
                        f"⚠️ Промпт с темой «{topic}» уже существует!\n"
                        f"Текущий текст:\n`{prompts[topic][:200]}`\n\n"
                        "Введите **другую** тему или пришлите другой файл:",
                        parse_mode=ParseMode.MARKDOWN,
                    )
                    return
                await state.update_data(topic=topic, file_text=file_text)
                await message.answer(
                    f"✅ Из файла определена тема: **«{topic}»**\\n\\n"
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
                    f"📄 Текст из файла (первые 100 символов):\n`{preview}...`\n\n"
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
            f"⚠️ Промпт с темой «{topic}» уже существует!\n"
            f"Текущий текст:\n`{prompts[topic][:200]}`\n\n"
            "Введите **другую** тему:",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    await state.update_data(topic=topic)
    await message.answer(
        f"✅ Тема «{topic}» принята.\n\n"
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
        f"🧠 **Промпт «{topic}» добавлен в память.**\n\n"
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
            f"⚠️ Промпт с темой «{topic}» уже существует!\n"
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
            f"🧠 **Промпт «{topic}» добавлен в память.**\n\n"
            f"📄 Длина: {len(file_text)} символов (из файла)",
            parse_mode=ParseMode.MARKDOWN,
        )
        await state.clear()
        list_text = _format_prompt_list()
        await message.answer(list_text, parse_mode=ParseMode.MARKDOWN, reply_markup=_prompt_keyboard())
    else:
        await message.answer(
            f"✅ Тема «{topic}» принята.\n\n"
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
            # Аргумент — не число, возможно это тема промпта
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

    # Без аргументов — запускаем FSM диалог выбора
    topics = "\n".join(f"• {t}" for t in sorted_topics)
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
    return f"❓ Раздел справки «{section}» не найден.\n\nДоступные разделы: {available}"
@dp.message(Command("init"))
async def cmd_init(message: Message, state: FSMContext):
    """Сбрасывает все настройки почты и запускает настройку заново."""
    await _start_init(message, state)


@dp.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    """
    Команда /start: если пользователь новый — сразу запускаем онбординг.
    Если уже настроен — показываем приветствие и советуем /help.
    """
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

    # Сохраняем txt-содержимое в кеш — нужно для кнопки Саммари
    _save_notes_cache(user.id, items)

    total = len(items)
    await message.answer(f"📋 **Новые конспекты встреч** — всего {total}", parse_mode=ParseMode.MARKDOWN)

    # Каждый конспект — отдельное сообщение с собственной кнопкой
    for idx, item in enumerate(items, 1):
        dt, display = item[0], item[1]
        date_str = dt.strftime("%d.%m.%Y %H:%M")
        text = f"**{idx}.** {display}\n📅 {date_str}"
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
        _, items = fetch_new_notes(user.id)
    except imaplib.IMAP4.error as e:
        await sent.edit_text(f"IMAP error: {e}")
        return
    except Exception as e:
        await sent.edit_text(f"Error: {e}")
        import traceback
        traceback.print_exc()
        return

    if not items:
        await message.answer("No new conspects.")
        await sent.delete()
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
        text = f"**{idx}.** {display}\n{date_str}"
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

    for idx, item in enumerate(items, 1):
        dt, display = item[0], item[1]
        date_str = dt.strftime("%d.%m.%Y %H:%M")
        text = f"**{idx}.** {display}\n📅 {date_str}"
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
        f"⏳ Тестирую подключение к **{model}**...\n"
        f"🔗 `{endpoint}`",
        parse_mode=ParseMode.MARKDOWN,
    )

    result = await _test_ai_connection(endpoint, api_key, model)
    await message.answer(
        f"🧪 **Результат теста AI**\n\n"
        f"🔗 Endpoint: `{endpoint}`\n"
        f"📝 Модель: `{model}`\n\n"
        f"{result}",
        parse_mode=ParseMode.MARKDOWN,
    )

@dp.message(Command("setup"))
async def cmd_setup_start(message: Message, state: FSMContext, command: CommandObject):
    """Начинает настройку почты. /setup init — сброс и настройка заново."""

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
            # Перенаправляем на настройку нейросети
            await cmd_setup_ai(message, state)
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
                f"✅ **Режим публикации в Wiki:** {mode_labels.get(mode, mode)}\n\n"
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
            config = get_db_config(user_id)
            current_host = (config or {}).get("host", "не задан")
            await message.answer(
                "🗄️ **Настройка PostgreSQL**\n\n"
                f"Текущий хост: `{current_host}`\n\n"
                "Введите **хост** сервера PostgreSQL:",
                parse_mode=ParseMode.MARKDOWN,
            )
            await state.set_state(DbSetupState.host)
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
                await message.answer(f"❌ **Ошибка:** {e}", parse_mode=ParseMode.MARKDOWN)
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
                await message.answer(f"❌ **Ошибка:** {e}", parse_mode=ParseMode.MARKDOWN)
            return

        if arg in ("email", "imap", "login", "user", "password", "pass"):
            # Одношаговая настройка отдельного поля почты
            # Без FSM-диалога — только запрос значения и сохранение
            field = "email" if arg == "email" else \
                    "imap" if arg == "imap" else \
                    "login" if arg in ("login", "user") else \
                    "password"
            await state.update_data(field=field)
            labels = {
                "email": "📧 **Email** — введите новый адрес электронной почты:",
                "imap": "🔌 **IMAP-сервер** — введите адрес IMAP-сервера (например, `imap.yandex.ru`):",
                "login": "👤 **Логин** — введите логин для IMAP (обычно совпадает с email):",
                "password": "🔑 **Пароль** — введите пароль приложения для IMAP:",
            }
            config = get_user_config(message.from_user.id)
            current = ""
            if config and config.get(field):
                current = f"\n\nТекущее значение: `{config[field][:20]}...`" if field == "password" else f"\n\nТекущее значение: `{config[field]}`"
            await message.answer(
                f"{labels.get(field, '')}{current}",
                parse_mode=ParseMode.MARKDOWN,
            )
            await state.set_state(SetupSingleField.value)
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
    """
    Шаг 1: Email.
    Пользователь обязан ввести корректный email. Пустой ввод не допускается.
    Проверяем формат: local-part@domain.tld
    """
    import re
    email = message.text.strip()
    if not email:
        await message.answer("⚠️ Email не может быть пустым. Введите адрес электронной почты:")
        return

    # Проверка формата email: что-то@домен.что-то
    email_pattern = re.compile(r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$')
    if not email_pattern.match(email):
        await message.answer(
            "⚠️ Некорректный формат email.\n\n"
            "Пример правильного адреса: `ivan@example.ru`\n"
            "Email должен содержать `@` и домен (например, `.ru`, `.com`).\n\n"
            "Введите email ещё раз:",
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    await state.update_data(email=email)

    config = get_user_config(message.from_user.id)
    current = config["server"] if config else "не задан"
    await message.answer(
        f"✅ Email: `{email}`\n\n"
        f"**IMAP-сервер** ({current}):\n"
        "Введите адрес IMAP-сервера\n"
        "(например: `imap.yandex.ru`, `imap.mail.ru`):",
        parse_mode=ParseMode.MARKDOWN,
    )
    await state.set_state(SetupState.server)


@dp.message(SetupState.server)
async def setup_server(message: Message, state: FSMContext):
    """
    Шаг 2: IMAP-сервер.
    Пользователь обязан ввести корректный хост IMAP-сервера.
    Проверяем формат: valid hostname (например, imap.yandex.ru).
    """
    import re
    server = message.text.strip()
    if not server:
        await message.answer("⚠️ IMAP-сервер не может быть пустым. Введите адрес сервера:")
        return

    # Проверка формата hostname: буквы, цифры, точки, дефисы
    hostname_pattern = re.compile(r'^[a-zA-Z0-9]([a-zA-Z0-9.-]*[a-zA-Z0-9])?$')
    if not hostname_pattern.match(server):
        await message.answer(
            "⚠️ Некорректный формат IMAP-сервера.\n\n"
            "Пример правильного адреса: `imap.yandex.ru`\n"
            "Имя сервера должно содержать хотя бы одну точку\n"
            "и состоять только из букв, цифр, точек и дефисов.\n\n"
            "Введите адрес IMAP-сервера ещё раз:",
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    if "." not in server:
        await message.answer(
            "⚠️ IMAP-сервер должен содержать домен (например: `imap.yandex.ru`).\n\n"
            "Введите адрес IMAP-сервера ещё раз:",
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    await state.update_data(server=server)

    config = get_user_config(message.from_user.id)
    current = config["login"] if config else "не задан"
    await message.answer(
        f"✅ Сервер: `{server}`\n\n"
        f"**Логин** ({current}):\n"
        "Введите логин для подключения к IMAP\n"
        "(обычно совпадает с email):",
        parse_mode=ParseMode.MARKDOWN,
    )
    await state.set_state(SetupState.login)


@dp.message(SetupState.login)
async def setup_login(message: Message, state: FSMContext):
    """
    Шаг 3: Логин.
    Пользователь обязан ввести логин. Пустой ввод не допускается.
    """
    login = message.text.strip()
    if not login:
        await message.answer("⚠️ Логин не может быть пустым. Введите логин:")
        return
    await state.update_data(login=login)

    config = get_user_config(message.from_user.id)
    current = "••••••••" if config and config.get("password") else "не задан"
    await message.answer(
        f"✅ Логин: `{login}`\n\n"
        f"**Пароль** ({current}):\n"
        "Введите пароль приложения для IMAP\n"
        "(для Яндекса — создайте пароль приложения в настройках почты):",
        parse_mode=ParseMode.MARKDOWN,
    )
    await state.set_state(SetupState.password)


@dp.message(SetupState.password)
async def setup_password(message: Message, state: FSMContext):
    """
    Шаг 4: Пароль приложения.
    Пользователь обязан ввести пароль. Пустой ввод не допускается.
    После ввода проверяем IMAP-подключение. Если всё ОК — сохраняем.
    """
    password = message.text.strip()
    if not password:
        await message.answer("⚠️ Пароль не может быть пустым. Введите пароль приложения:")
        return

    data = await state.get_data()
    email = data.get("email", "")
    server = data.get("server", "")
    login = data.get("login", "")
    user_id = message.from_user.id

    # Проверяем подключение — лучше ошибиться здесь, чем при /list
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
    """Сохраняет одно поле настройки почты (email, imap, login, password)."""
    value = message.text.strip()
    if not value:
        await message.answer("⚠️ Значение не может быть пустым. Введите снова:")
        return

    data = await state.get_data()
    field = data.get("field", "email")
    user_id = message.from_user.id

    users = _load_users()
    key = str(user_id)
    if key not in users:
        users[key] = {}

    old_val = users[key].get(field, "")
    users[key][field] = value
    _save_users(users)

    await state.clear()

    label_map = {
        "email": "📧 Email",
        "imap": "🔌 IMAP-сервер",
        "login": "👤 Логин",
        "password": "🔑 Пароль",
    }
    masked = f"`{value[:20]}...`" if field == "password" else f"`{value}`"
    await message.answer(
        f"✅ **{label_map.get(field, field)}** сохранён: {masked}\n\n"
        "Проверьте настройки: `/setup show all`",
        parse_mode=ParseMode.MARKDOWN,
    )


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

from integrations.ai_adapter import call_ai_with_config, test_ai_connection


async def call_ai(user_id: int, system_prompt: str, user_text: str) -> str:
    """Wrapper: gets ai_config and delegates to adapter."""
    ai_config = get_ai_config(user_id)
    return await call_ai_with_config(ai_config, system_prompt, user_text)


async def _test_ai_connection(endpoint: str, api_key: str, model: str) -> str:
    """Wrapper: delegates to adapter."""
    return await test_ai_connection(endpoint, api_key, model)

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
    """
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
    """
    model = message.text.strip()
    if not model:
        await message.answer("⚠️ Название модели не может быть пустым. Введите модель:")
        return

    data = await state.get_data()
    api_key = data.get("ai_api_key", "")
    endpoint = data.get("ai_endpoint", "")

    # Если endpoint ещё не задан (custom путь) — текущее сообщение это endpoint
    need_endpoint = data.get("_need_endpoint", False)
    if need_endpoint:
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
        await message.answer("❌ Ошибка: не указан endpoint. Начните заново: `/setup_ai`")
        await state.clear()
        return

    user_id = message.from_user.id
    save_ai_config(user_id, endpoint, api_key, model)
    await state.clear()

    provider_label = data.get("ai_provider_label", "Пользовательский")
    await message.answer(
        f"⏳ Проверяю подключение к **{provider_label}**...",
        parse_mode=ParseMode.MARKDOWN,
    )

    test_result = await _test_ai_connection(endpoint, api_key, model)

    if test_result.startswith("✅"):
        await message.answer(
            f"✅ **AI-настройки сохранены!**\n\n"
            f"🧩 Провайдер: `{provider_label}`\n"
            f"🔗 Endpoint: `{endpoint}`\n"
            f"📝 Модель: `{model}`\n\n"
            f"**{test_result}**\n\n"
            "Теперь кнопка «Саммари» будет работать!",
            parse_mode=ParseMode.MARKDOWN,
        )
    else:
        await message.answer(
            f"⚠️ **AI-настройки сохранены**, но тест не прошёл:\n\n"
            f"🧩 Провайдер: `{provider_label}`\n"
            f"🔗 Endpoint: `{endpoint}`\n"
            f"📝 Модель: `{model}`\n\n"
            f"{test_result}\n\n"
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
            f"{report}"
            f"{org_hint}",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    await state.clear()
    await status.edit_text(
        f"✅ **Яндекс Вики настроена!**\n\n"
        f"{report}",
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
    """Шаг 1: хост PostgreSQL."""
    host = message.text.strip()
    if not host:
        await message.answer("⚠️ Хост не может быть пустым. Введите хост:")
        return
    await state.update_data(host=host)
    await message.answer(
        f"✅ Хост: `{host}`\n\n"
        "Введите **порт** (по умолчанию 5432):",
        parse_mode=ParseMode.MARKDOWN,
    )
    await state.set_state(DbSetupState.port)


@dp.message(DbSetupState.port)
async def setup_db_port(message: Message, state: FSMContext):
    """Шаг 2: порт PostgreSQL."""
    raw = message.text.strip()
    try:
        port = int(raw) if raw else 5432
    except ValueError:
        await message.answer("⚠️ Порт должен быть числом. Введите число (например, 5432):")
        return
    if port < 1 or port > 65535:
        await message.answer("⚠️ Порт должен быть от 1 до 65535. Введите снова:")
        return
    await state.update_data(port=port)
    await message.answer(
        f"✅ Порт: `{port}`\n\n"
        "Введите **имя базы данных**:",
        parse_mode=ParseMode.MARKDOWN,
    )
    await state.set_state(DbSetupState.name)


@dp.message(DbSetupState.name)
async def setup_db_name(message: Message, state: FSMContext):
    """Шаг 3: имя базы данных."""
    name = message.text.strip()
    if not name:
        await message.answer("⚠️ Имя БД не может быть пустым. Введите имя БД:")
        return
    await state.update_data(name=name)
    await message.answer(
        f"✅ База данных: `{name}`\n\n"
        "Введите **имя пользователя**:",
        parse_mode=ParseMode.MARKDOWN,
    )
    await state.set_state(DbSetupState.user)


@dp.message(DbSetupState.user)
async def setup_db_user(message: Message, state: FSMContext):
    """Шаг 4: пользователь PostgreSQL."""
    user = message.text.strip()
    if not user:
        await message.answer("⚠️ Имя пользователя не может быть пустым. Введите имя:")
        return
    await state.update_data(user=user)
    await message.answer(
        f"✅ Пользователь: `{user}`\n\n"
        "Введите **пароль**:",
        parse_mode=ParseMode.MARKDOWN,
    )
    await state.set_state(DbSetupState.password)


@dp.message(DbSetupState.password)
async def setup_db_password(message: Message, state: FSMContext):
    """Шаг 5: пароль PostgreSQL.
       После ввода всех параметров — тестируем подключение.
       Пароль не показывается в логах."""
    password = message.text.strip()
    if not password:
        await message.answer("⚠️ Пароль не может быть пустым. Введите пароль:")
        return

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
        await status_msg.edit_text(msg, parse_mode=ParseMode.MARKDOWN)
    else:
        await status_msg.edit_text(
            f"{msg}\n\n"
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
                                    f"**{idx}.** {display}\n"
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

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())