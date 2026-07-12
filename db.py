#!/usr/bin/env python3
"""
📦 Модуль работы с PostgreSQL для HuntTech Protocols Bot.

Использует hunttech_bot_common.database.DatabaseManager под капотом.
Сохраняет:
- Конспекты встреч (из IMAP) — таблица meetings
- Историю AI-саммари (кто, когда, какой промпт) — таблица summary_log

Безопасность: все запросы через параметризованные ($1, $2, ...),
никаких f-строк в SQL.
"""
import logging
from typing import Optional

# Import from adapter which uses common library DatabaseManager
from integrations.db_adapter import (
    DB_ENABLED,
    DB_POOL,
    ADMIN_USER_ID,
    apply_config,
    init_db_pool,
    close_db_pool,
    ensure_tables,
    save_meeting,
    get_meeting_by_msg_id,
    save_summary,
    get_recent_meetings,
    get_summaries_for_meeting,
    get_stats,
)

logger = logging.getLogger("db")

# Also export for backward compatibility
__all__ = [
    "DB_ENABLED", "DB_POOL", "ADMIN_USER_ID",
    "apply_config", "init_db_pool", "close_db_pool", "ensure_tables",
    "save_meeting", "get_meeting_by_msg_id",
    "save_summary", "get_recent_meetings", "get_summaries_for_meeting", "get_stats",
]
