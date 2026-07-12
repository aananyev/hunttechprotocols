#!/usr/bin/env python3
"""
📦 Модуль работы с PostgreSQL для HuntTech Protocols Bot.

Сохраняет:
- Конспекты встреч (из IMAP) — таблица meetings
- Историю AI-саммари (кто, когда, какой промпт) — таблица summary_log

Безопасность: все запросы через параметризованные ($1, $2, ...),
никаких f-строк в SQL.
"""

import os
import logging
from typing import Optional
from dotenv import load_dotenv
import asyncpg

load_dotenv()

logger = logging.getLogger("db")

DB_POOL: Optional[asyncpg.Pool] = None
BOT_DB_HOST = os.getenv("DB_HOST", "")
BOT_DB_PORT = int(os.getenv("DB_PORT", "5432"))
BOT_DB_NAME = os.getenv("DB_NAME", "hunttech_protocols")
BOT_DB_USER = os.getenv("DB_USER", "postgres")
BOT_DB_PASS = os.getenv("DB_PASSWORD", "")

DB_ENABLED = bool(BOT_DB_HOST and BOT_DB_PASS)

ADMIN_USER_ID = 272980897  # Telegram ID администратора (AlekseyAnanyev)


# ═══════════════════════════════════════════════════════════════════
# ПУЛ ПОДКЛЮЧЕНИЙ
# ═══════════════════════════════════════════════════════════════════


async def apply_config(host: str, port: int, name: str, user: str, password: str) -> tuple[bool, str]:
    """Пробует подключиться к PostgreSQL с переданными параметрами.
       Если успешно — обновляет пул и создаёт таблицы.
       Возвращает (success, message)."""
    global DB_POOL, BOT_DB_HOST, BOT_DB_PORT, BOT_DB_NAME, BOT_DB_USER, BOT_DB_PASS, DB_ENABLED
    try:
        # Пробуем создать новое подключение
        pool = await asyncpg.create_pool(
            host=host, port=port, user=user, password=password,
            database=name, min_size=1, max_size=5,
        )
    except Exception as e:
        return False, f"❌ **Ошибка подключения:** {e}"

    # Закрываем старый пул, если был
    if DB_POOL:
        await DB_POOL.close()

    DB_POOL = pool
    BOT_DB_HOST = host
    BOT_DB_PORT = port
    BOT_DB_NAME = name
    BOT_DB_USER = user
    BOT_DB_PASS = password
    DB_ENABLED = True

    try:
        await ensure_tables()
    except Exception as e:
        return False, f"❌ Подключено, но не удалось создать таблицы: {e}"

    return True, f"✅ **PostgreSQL подключён!**\n`{host}:{port}/{name}` — таблицы готовы."


async def init_db_pool() -> None:
    """Создаёт пул подключений к PostgreSQL.
       Если DB_HOST не задан — пул не создаётся, бот работает без БД."""
    global DB_POOL
    if not DB_ENABLED:
        logger.info(
            "📦 PostgreSQL не настроен (DB_HOST не задан). "
            "Хранение в БД отключено."
        )
        return
    try:
        DB_POOL = await asyncpg.create_pool(
            host=BOT_DB_HOST,
            port=BOT_DB_PORT,
            user=BOT_DB_USER,
            password=BOT_DB_PASS,
            database=BOT_DB_NAME,
            min_size=1,
            max_size=5,
        )
        logger.info(
            "✅ Пул PostgreSQL создан: %s:%d/%s",
            BOT_DB_HOST, BOT_DB_PORT, BOT_DB_NAME,
        )
    except Exception as e:
        logger.error("❌ Не удалось создать пул PostgreSQL: %s", e)
        DB_POOL = None


async def close_db_pool() -> None:
    """Закрывает пул подключений."""
    global DB_POOL
    if DB_POOL:
        await DB_POOL.close()
        DB_POOL = None
        logger.info("🔌 Пул PostgreSQL закрыт")


async def ensure_tables() -> None:
    """Создаёт таблицы, если их нет. Безопасно — IF NOT EXISTS.
       Можно вызывать при каждом запуске — ничего не сломает."""
    if not DB_POOL:
        return
    async with DB_POOL.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS meetings (
                id              SERIAL PRIMARY KEY,
                email_msg_id    VARCHAR(255) NOT NULL UNIQUE,
                imap_msg_id     VARCHAR(255),
                user_id         BIGINT NOT NULL,
                email_from      VARCHAR(255),
                subject         VARCHAR(500),
                received_at     TIMESTAMPTZ NOT NULL,
                raw_text        TEXT NOT NULL,
                prompt_topic    VARCHAR(500),
                summary_text    TEXT,
                summary_created_at TIMESTAMPTZ,
                wiki_url        VARCHAR(500),
                wiki_published  BOOLEAN DEFAULT FALSE,
                created_at      TIMESTAMPTZ DEFAULT NOW()
            );

            CREATE TABLE IF NOT EXISTS summary_log (
                id              SERIAL PRIMARY KEY,
                meeting_id      INT NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
                user_id         BIGINT NOT NULL,
                prompt_topic    VARCHAR(500),
                ai_model        VARCHAR(100),
                summary_text    TEXT NOT NULL,
                wiki_published  BOOLEAN DEFAULT FALSE,
                wiki_url        VARCHAR(500),
                created_at      TIMESTAMPTZ DEFAULT NOW()
            );
        """)
        logger.info("✅ Таблицы meetings / summary_log готовы")


# ═══════════════════════════════════════════════════════════════════
# CRUD — КОНСПЕКТЫ (meetings)
# ═══════════════════════════════════════════════════════════════════


async def save_meeting(
    email_msg_id: str,
    user_id: int,
    email_from: str,
    subject: str,
    received_at,
    raw_text: str,
    imap_msg_id: str = "",
) -> Optional[int]:
    """Сохраняет конспект встречи в БД.
       Если запись с таким email_msg_id уже есть — пропускает (ON CONFLICT DO NOTHING).
       Возвращает meeting_id или None при ошибке."""
    if not DB_POOL:
        return None
    try:
        async with DB_POOL.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO meetings (email_msg_id, imap_msg_id, user_id, email_from, subject, received_at, raw_text)
                VALUES ($1, $2, $3, $4, $5, $6, $7)
                ON CONFLICT (email_msg_id) DO NOTHING
                RETURNING id
                """,
                email_msg_id, imap_msg_id, user_id, email_from, subject, received_at, raw_text,
            )
            if row:
                meeting_id = row["id"]
                logger.info("📝 Сохранён конспект #%d: %s", meeting_id, subject)
                return meeting_id
            else:
                # Уже был — получаем его id
                existing = await conn.fetchval(
                    "SELECT id FROM meetings WHERE email_msg_id = $1",
                    email_msg_id,
                )
                return existing
    except Exception as e:
        logger.error("❌ Ошибка save_meeting: %s", e)
        return None


async def get_meeting_by_msg_id(email_msg_id: str) -> Optional[dict]:
    """Находит meeting по Message-ID письма."""
    if not DB_POOL:
        return None
    try:
        async with DB_POOL.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM meetings WHERE email_msg_id = $1",
                email_msg_id,
            )
            return dict(row) if row else None
    except Exception as e:
        logger.error("❌ Ошибка get_meeting_by_msg_id: %s", e)
        return None


# ═══════════════════════════════════════════════════════════════════
# CRUD — ИСТОРИЯ САММАРИ (summary_log)
# ═══════════════════════════════════════════════════════════════════


async def save_summary(
    meeting_id: int,
    user_id: int,
    prompt_topic: str,
    ai_model: str,
    summary_text: str,
    wiki_published: bool = False,
    wiki_url: str = "",
) -> Optional[int]:
    """Сохраняет запись о генерации саммари.
       Также обновляет summary_text в таблице meetings — датой последней генерации.
       Возвращает log_id или None при ошибке."""
    if not DB_POOL:
        return None
    try:
        async with DB_POOL.acquire() as conn:
            log_id = await conn.fetchval(
                """
                INSERT INTO summary_log (meeting_id, user_id, prompt_topic, ai_model, summary_text, wiki_published, wiki_url)
                VALUES ($1, $2, $3, $4, $5, $6, $7)
                RETURNING id
                """,
                meeting_id, user_id, prompt_topic, ai_model,
                summary_text, wiki_published, wiki_url,
            )
            # Обновляем сводку в meetings
            await conn.execute(
                """
                UPDATE meetings
                SET summary_text = $1, summary_created_at = NOW(),
                    prompt_topic = COALESCE($2, prompt_topic),
                    wiki_url = COALESCE($3, wiki_url),
                    wiki_published = CASE WHEN $4 THEN TRUE ELSE wiki_published END
                WHERE id = $5
                """,
                summary_text, prompt_topic, wiki_url if wiki_published else None,
                wiki_published, meeting_id,
            )
            logger.info(
                "🧠 Сохранено саммари #%d для встречи #%d (промпт: %s)",
                log_id, meeting_id, prompt_topic,
            )
            return log_id
    except Exception as e:
        logger.error("❌ Ошибка save_summary: %s", e)
        return None


# ═══════════════════════════════════════════════════════════════════
# ЧТЕНИЕ — ИСТОРИЯ
# ═══════════════════════════════════════════════════════════════════


async def get_recent_meetings(limit: int = 10) -> list[dict]:
    """Последние N встреч со сводкой (JOIN summary_log)."""
    if not DB_POOL:
        return []
    try:
        async with DB_POOL.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT m.id, m.subject, m.received_at,
                       m.prompt_topic, m.summary_text, m.wiki_url,
                       m.summary_created_at,
                       COUNT(sl.id) AS summary_count
                FROM meetings m
                LEFT JOIN summary_log sl ON sl.meeting_id = m.id
                GROUP BY m.id
                ORDER BY m.received_at DESC
                LIMIT $1
                """,
                limit,
            )
            return [dict(r) for r in rows]
    except Exception as e:
        logger.error("❌ Ошибка get_recent_meetings: %s", e)
        return []


async def get_summaries_for_meeting(meeting_id: int) -> list[dict]:
    """Все генерации саммари для конкретной встречи."""
    if not DB_POOL:
        return []
    try:
        async with DB_POOL.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT sl.id, sl.user_id, sl.prompt_topic, sl.ai_model,
                       sl.summary_text, sl.wiki_published, sl.wiki_url,
                       sl.created_at
                FROM summary_log sl
                WHERE sl.meeting_id = $1
                ORDER BY sl.created_at DESC
                """,
                meeting_id,
            )
            return [dict(r) for r in rows]
    except Exception as e:
        logger.error("❌ Ошибка get_summaries_for_meeting: %s", e)
        return []


async def get_stats() -> dict:
    """Статистика: сколько встреч, саммари, по дням."""
    if not DB_POOL:
        return {"total_meetings": 0, "total_summaries": 0, "days": []}
    try:
        async with DB_POOL.acquire() as conn:
            total_m = await conn.fetchval("SELECT COUNT(*) FROM meetings")
            total_s = await conn.fetchval("SELECT COUNT(*) FROM summary_log")
            by_day = await conn.fetch(
                """
                SELECT DATE(received_at) AS day, COUNT(*) AS cnt
                FROM meetings
                GROUP BY DATE(received_at)
                ORDER BY day DESC
                LIMIT 14
                """,
            )
            return {
                "total_meetings": total_m,
                "total_summaries": total_s,
                "days": [{"date": str(r["day"]), "count": r["cnt"]} for r in by_day],
            }
    except Exception as e:
        logger.error("❌ Ошибка get_stats: %s", e)
        return {"total_meetings": 0, "total_summaries": 0, "days": []}
