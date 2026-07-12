"""
Database adapter for hunttech-bot-common.

Replaces direct asyncpg pool management with DatabaseManager from the
common library while preserving all existing function signatures in db.py.
"""
import logging
from typing import Optional

from hunttech_bot_common.database import DatabaseManager

logger = logging.getLogger("db")

# Global DatabaseManager instance (replaces global DB_POOL)
_db_manager: Optional[DatabaseManager] = None

# Backward-compatible DB_POOL — truthy when connected
class _DBPoolProxy:
    """Simple proxy that is truthy when connected."""
    def __bool__(self):
        return _db_manager is not None and _db_manager.connected

DB_POOL = _DBPoolProxy()

# Expose DB_ENABLED for backward compatibility in bot.py
DB_ENABLED = False
ADMIN_USER_ID = 272980897


async def _get_db_manager() -> DatabaseManager:
    """Get the underlying DatabaseManager, raising if not connected."""
    if not _db_manager or not _db_manager.connected:
        raise RuntimeError("Database not connected")
    return _db_manager


async def init_db_pool() -> None:
    """Create DatabaseManager and connect. Replaces direct asyncpg pool creation."""
    global _db_manager, DB_ENABLED
    import os
    host = os.getenv("DB_HOST", "")
    password = os.getenv("DB_PASSWORD", "")

    if not host or not password:
        logger.info("📦 PostgreSQL не настроен (DB_HOST не задан). Хранение в БД отключено.")
        DB_ENABLED = False
        return

    try:
        _db_manager = DatabaseManager(
            host=host,
            port=int(os.getenv("DB_PORT", "5432")),
            database=os.getenv("DB_NAME", "hunttech_protocols"),
            user=os.getenv("DB_USER", "postgres"),
            password=password,
            min_size=1,
            max_size=5,
        )
        await _db_manager.connect()
        DB_ENABLED = True
        logger.info("✅ Database manager создан: %s:%s/%s",
                     host, os.getenv("DB_PORT", "5432"), os.getenv("DB_NAME", "hunttech_protocols"))
    except Exception as e:
        logger.error("❌ Не удалось создать DatabaseManager: %s", e)
        _db_manager = None
        DB_ENABLED = False


async def close_db_pool() -> None:
    """Close the DatabaseManager. Replaces direct pool close."""
    global _db_manager, DB_ENABLED
    if _db_manager:
        await _db_manager.close()
        _db_manager = None
        DB_ENABLED = False
        logger.info("🔌 Database manager закрыт")


async def ensure_tables() -> None:
    """Create tables if they don't exist."""
    if not _db_manager or not _db_manager.connected:
        return
    async with _db_manager.session() as conn:
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


async def apply_config(host: str, port: int, name: str, user: str, password: str) -> tuple[bool, str]:
    """Try to connect with provided params and create tables."""
    global _db_manager, DB_ENABLED
    try:
        new_mgr = DatabaseManager(
            host=host, port=port, database=name,
            user=user, password=password,
            min_size=1, max_size=5,
        )
        await new_mgr.connect()
    except Exception as e:
        return False, f"❌ **Ошибка подключения:** {e}"

    # Close old manager
    if _db_manager:
        await _db_manager.close()

    _db_manager = new_mgr
    DB_ENABLED = True

    try:
        await ensure_tables()
    except Exception as e:
        return False, f"❌ Подключено, но не удалось создать таблицы: {e}"

    return True, f"✅ **PostgreSQL подключён!**\\n`{host}:{port}/{name}` — таблицы готовы."


async def save_meeting(
    email_msg_id: str,
    user_id: int,
    email_from: str,
    subject: str,
    received_at,
    raw_text: str,
    imap_msg_id: str = "",
) -> Optional[int]:
    """Save a meeting to DB."""
    if not _db_manager or not _db_manager.connected:
        return None
    try:
        async with _db_manager.session() as conn:
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
                existing = await conn.fetchval(
                    "SELECT id FROM meetings WHERE email_msg_id = $1",
                    email_msg_id,
                )
                return existing
    except Exception as e:
        logger.error("❌ Ошибка save_meeting: %s", e)
        return None


async def get_meeting_by_msg_id(email_msg_id: str) -> Optional[dict]:
    """Find meeting by Message-ID."""
    if not _db_manager or not _db_manager.connected:
        return None
    try:
        async with _db_manager.session() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM meetings WHERE email_msg_id = $1",
                email_msg_id,
            )
            return dict(row) if row else None
    except Exception as e:
        logger.error("❌ Ошибка get_meeting_by_msg_id: %s", e)
        return None


async def save_summary(
    meeting_id: int,
    user_id: int,
    prompt_topic: str,
    ai_model: str,
    summary_text: str,
    wiki_published: bool = False,
    wiki_url: str = "",
) -> Optional[int]:
    """Save summary record."""
    if not _db_manager or not _db_manager.connected:
        return None
    try:
        async with _db_manager.session() as conn:
            log_id = await conn.fetchval(
                """
                INSERT INTO summary_log (meeting_id, user_id, prompt_topic, ai_model, summary_text, wiki_published, wiki_url)
                VALUES ($1, $2, $3, $4, $5, $6, $7)
                RETURNING id
                """,
                meeting_id, user_id, prompt_topic, ai_model,
                summary_text, wiki_published, wiki_url,
            )
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
            logger.info("🧠 Сохранено саммари #%d для встречи #%d (промпт: %s)",
                         log_id, meeting_id, prompt_topic)
            return log_id
    except Exception as e:
        logger.error("❌ Ошибка save_summary: %s", e)
        return None


async def get_recent_meetings(limit: int = 10) -> list[dict]:
    """Recent meetings with summaries."""
    if not _db_manager or not _db_manager.connected:
        return []
    try:
        async with _db_manager.session() as conn:
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
    """All summaries for a meeting."""
    if not _db_manager or not _db_manager.connected:
        return []
    try:
        async with _db_manager.session() as conn:
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
    """Statistics."""
    if not _db_manager or not _db_manager.connected:
        return {"total_meetings": 0, "total_summaries": 0, "days": []}
    try:
        async with _db_manager.session() as conn:
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

