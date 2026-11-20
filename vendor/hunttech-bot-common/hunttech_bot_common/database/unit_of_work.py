"""Unit of Work — transaction management for asyncpg.

Provides:
- UnitOfWork context manager for automatic commit/rollback
- Nested transaction support (savepoints)
- Manual commit/rollback control

Usage::

    async with UnitOfWork(pool) as uow:
        user = await uow.conn.fetchrow("SELECT ...")
        await uow.conn.execute("INSERT INTO ...")
        # Auto-commits on success, auto-rollbacks on exception

    # Manual control:
    uow = UnitOfWork(pool)
    await uow.start()
    try:
        await uow.conn.execute("INSERT INTO ...")
        await uow.commit()
    except Exception:
        await uow.rollback()
        raise
    finally:
        await uow.close()
"""

from __future__ import annotations

import logging
from types import TracebackType
from typing import Any

import asyncpg

from hunttech_bot_common.database.pool import DatabasePool
from hunttech_bot_common.exceptions import DatabaseError

logger = logging.getLogger(__name__)


class UnitOfWork:
    """Transaction context manager for database operations.

    Provides a single connection with transaction management.
    Automatically commits on success, rolls back on exception.

    Can be used as an async context manager or with manual start/commit/rollback.
    """

    def __init__(self, pool: DatabasePool) -> None:
        """Initialize UnitOfWork.

        Args:
            pool: DatabasePool instance.
        """
        self._pool = pool
        self._conn: asyncpg.Connection | None = None
        self._acquire_cm: Any = None  # PoolAcquireContext, stored for release
        self._transaction: asyncpg.transaction.Transaction | None = None
        self._closed = False
        self._committed = False

    @property
    def conn(self) -> asyncpg.Connection:
        """Get the underlying connection.

        Raises:
            RuntimeError: If transaction has not been started.
        """
        if self._conn is None:
            raise RuntimeError(
                "UnitOfWork has not been started. "
                "Use 'async with UnitOfWork(pool)' or "
                "await uow.start() first."
            )
        return self._conn

    @property
    def is_active(self) -> bool:
        """Check if the transaction is active."""
        return (
            self._conn is not None
            and not self._conn.is_closed()
            and self._transaction is not None
            and not getattr(self._transaction, "_closed", True)
        )

    async def start(self) -> None:
        """Start a new transaction.

        Acquires a connection from the pool and begins a transaction.
        """
        if self._conn is not None:
            raise DatabaseError("UnitOfWork already started")

        try:
            self._acquire_cm = self._pool.pool.acquire()  # type: ignore
            self._conn = await self._acquire_cm.__aenter__()
            self._transaction = self._conn.transaction()
            await self._transaction.start()
            logger.debug("UnitOfWork started")
        except asyncpg.PostgresError as e:
            raise DatabaseError(f"Failed to start transaction: {e}") from e

    async def commit(self) -> None:
        """Commit the current transaction."""
        if self._transaction is None or getattr(self._transaction, "_closed", True):
            raise DatabaseError("No active transaction to commit")

        try:
            await self._transaction.commit()
            self._committed = True
            logger.debug("UnitOfWork committed")
        except asyncpg.PostgresError as e:
            raise DatabaseError(f"Failed to commit transaction: {e}") from e

    async def rollback(self) -> None:
        """Rollback the current transaction."""
        if self._transaction is None or getattr(self._transaction, "_closed", True):
            return

        try:
            await self._transaction.rollback()
            logger.debug("UnitOfWork rolled back")
        except asyncpg.PostgresError as e:
            raise DatabaseError(f"Failed to rollback transaction: {e}") from e

    async def close(self) -> None:
        """Release the connection back to the pool."""
        if self._closed:
            return

        self._closed = True

        # Rollback if not committed and transaction still open
        if (
            not self._committed
            and self._transaction is not None
            and not getattr(self._transaction, "_closed", False)
        ):
            try:
                await self._transaction.rollback()
            except Exception:
                pass

        # Release connection via PoolAcquireContext.__aexit__
        if self._acquire_cm is not None:
            try:
                await self._acquire_cm.__aexit__(None, None, None)
            except Exception:
                pass

        self._conn = None
        self._transaction = None
        self._acquire_cm = None
        logger.debug("UnitOfWork closed")

    async def __aenter__(self) -> UnitOfWork:
        await self.start()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        try:
            if exc_type is None:
                await self.commit()
            else:
                await self.rollback()
        finally:
            await self.close()


__all__ = [
    "UnitOfWork",
]
