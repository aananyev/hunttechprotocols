"""Database module — asyncpg connection pool, repositories, migrations, and transactions.

Provides a complete async PostgreSQL layer for Telegram bots:

- ``DatabasePool`` — asyncpg connection pool with SSL/TLS, timeouts, health checks
- ``BaseRepository`` — generic CRUD repository for any table
- ``UnitOfWork`` — transaction management with auto commit/rollback
- ``DatabaseMigrator`` — SQL file-based versioned migrations
- ``PoolConfig`` — typed configuration with DATABASE_URL parsing

Usage::

    from hunttech_bot_common.database import DatabasePool, BaseRepository, UnitOfWork

    # Connect
    pool = DatabasePool.from_url("postgresql://user:***@host:5432/db")
    await pool.connect()

    # CRUD via BaseRepository
    repo = BaseRepository(pool, table_name="users", pk_column="id")
    user = await repo.create(name="Ivan", email="ivan@example.com")
    users = await repo.find_all(order_by="created_at DESC")

    # Transactions
    async with UnitOfWork(pool) as uow:
        await uow.conn.execute("INSERT INTO logs ...")

    # Migrations
    from hunttech_bot_common.database import DatabaseMigrator
    migrator = DatabaseMigrator(pool, migrations_dir="migrations/")
    await migrator.run()
"""

from __future__ import annotations

from hunttech_bot_common.database.pool import DatabasePool, PoolConfig
from hunttech_bot_common.database.repository import BaseRepository
from hunttech_bot_common.database.unit_of_work import UnitOfWork
from hunttech_bot_common.database.migrations import DatabaseMigrator

__all__ = [
    "DatabasePool",
    "PoolConfig",
    "BaseRepository",
    "UnitOfWork",
    "DatabaseMigrator",
]
