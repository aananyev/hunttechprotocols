"""Database pool module — asyncpg connection pool management.

Supports:
- Remote PostgreSQL servers with SSL/TLS
- Connection pooling (min/max size)
- Connection timeout and statement timeout
- SSL modes: disable, allow, prefer, require, verify-ca, verify-full
- Automatic reconnection on failure
- URI parameters via DATABASE_URL query string
"""

from __future__ import annotations

import logging
import os
import ssl
from dataclasses import dataclass, field
from typing import Any, Callable

import asyncpg
from asyncpg.pool import Pool, PoolAcquireContext

from hunttech_bot_common.exceptions import DatabaseError

logger = logging.getLogger(__name__)


@dataclass
class PoolConfig:
    """Configuration for the database connection pool.

    All parameters can be set via ``DATABASE_URL`` environment variable
    with query parameters, or directly via constructor.

    Example DATABASE_URL::

        postgresql://user:password@host:5432/dbname?sslmode=require&connect_timeout=10

    Supported URL query parameters:
        - sslmode: disable | allow | prefer | require | verify-ca | verify-full
        - connect_timeout: seconds (default: 10)
        - statement_timeout: milliseconds (default: 30000)
        - pool_min: min pool size (default: 2)
        - pool_max: max pool size (default: 10)
        - application_name: name shown in pg_stat_activity
    """

    dsn: str
    min_size: int = 2
    max_size: int = 10
    connect_timeout: int = 10
    command_timeout: int = 30
    statement_timeout_ms: int = 30000
    sslmode: str = "prefer"
    ssl_root_cert: str | None = None
    application_name: str = "hunttech-bot"

    @classmethod
    def from_url(cls, url: str) -> PoolConfig:
        """Create PoolConfig from a DATABASE_URL.

        Parses query parameters from the URL.
        """
        # Parse query params from URL
        params: dict[str, str] = {}
        if "?" in url:
            base, qs = url.split("?", 1)
            for pair in qs.split("&"):
                if "=" in pair:
                    k, v = pair.split("=", 1)
                    params[k.lower()] = v
            url = base  # Strip query from DSN

        min_size = int(params.get("pool_min", "2"))
        max_size = int(params.get("pool_max", "10"))
        connect_timeout = int(params.get("connect_timeout", "10"))
        statement_timeout_ms = int(params.get("statement_timeout", "30000"))
        sslmode = params.get("sslmode", "prefer")
        app_name = params.get("application_name", "hunttech-bot")

        return cls(
            dsn=url,
            min_size=min_size,
            max_size=max_size,
            connect_timeout=connect_timeout,
            statement_timeout_ms=statement_timeout_ms,
            sslmode=sslmode,
            application_name=app_name,
        )

    def _make_ssl_context(self) -> ssl.SSLContext | None:
        """Create SSL context based on sslmode."""
        if self.sslmode in ("disable",):
            return None

        if self.sslmode in ("allow", "prefer"):
            # Don't enforce SSL, but use it if server supports
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            return ctx

        if self.sslmode in ("require",):
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            return ctx

        if self.sslmode == "verify-ca":
            ctx = ssl.create_default_context()
            if self.ssl_root_cert:
                ctx.load_verify_cities(self.ssl_root_cert)
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_REQUIRED
            return ctx

        if self.sslmode == "verify-full":
            ctx = ssl.create_default_context()
            if self.ssl_root_cert:
                ctx.load_verify_cities(self.ssl_root_cert)
            ctx.check_hostname = True
            ctx.verify_mode = ssl.CERT_REQUIRED
            return ctx

        return None


class DatabasePool:
    """Asyncpg connection pool wrapper.

    Provides a ready-to-use connection pool with health checks,
    automatic connection validation, and clean shutdown.

    Usage::

        config = PoolConfig.from_url("postgresql://user:pass@host:5432/db")
        pool = DatabasePool(config)
        await pool.connect()

        async with pool.acquire() as conn:
            row = await conn.fetchrow("SELECT 1 AS ok")
            print(row["ok"])

        await pool.close()
    """

    def __init__(self, config: PoolConfig) -> None:
        """Initialize the pool wrapper.

        Args:
            config: Pool configuration.
        """
        self._config = config
        self._pool: Pool | None = None
        self._connected = False

    @property
    def is_connected(self) -> bool:
        """Check if the pool is connected."""
        return self._connected and self._pool is not None and not self._pool._closed

    @property
    def pool(self) -> Pool | None:
        """Get the underlying asyncpg pool."""
        return self._pool

    @property
    def config(self) -> PoolConfig:
        """Get pool configuration."""
        return self._config

    async def connect(self) -> None:
        """Create the connection pool.

        Raises:
            DatabaseError: If connection fails.
        """
        if self._connected:
            return

        ssl_ctx = self._config._make_ssl_context()

        try:
            self._pool = await asyncpg.create_pool(
                dsn=self._config.dsn,
                min_size=self._config.min_size,
                max_size=self._config.max_size,
                timeout=self._config.connect_timeout,
                command_timeout=self._config.command_timeout,
                ssl=ssl_ctx,
                init=self._init_connection,
            )
            self._connected = True
            logger.info(
                "PostgreSQL pool created: min=%d, max=%d, sslmode=%s",
                self._config.min_size,
                self._config.max_size,
                self._config.sslmode,
            )
        except asyncpg.PostgresError as e:
            raise DatabaseError(
                f"Failed to create database pool: {e}"
            ) from e
        except OSError as e:
            raise DatabaseError(
                f"Failed to connect to database server: {e}"
            ) from e

    async def _init_connection(self, conn: asyncpg.Connection) -> None:
        """Initialize a new connection with session parameters."""
        if self._config.statement_timeout_ms:
            await conn.execute(
                f"SET statement_timeout = '{self._config.statement_timeout_ms}ms'"
            )

    async def close(self) -> None:
        """Close the connection pool."""
        if self._pool and not self._pool._closed:
            await self._pool.close()
        self._connected = False
        logger.info("PostgreSQL pool closed")

    def acquire(self) -> PoolAcquireContext:
        """Acquire a connection from the pool (async context manager).

        Usage::

            async with pool.acquire() as conn:
                await conn.fetch("SELECT ...")

        Returns:
            An async context manager that yields a connection.

        Raises:
            DatabaseError: If pool is not connected.
        """
        if not self._pool or self._pool._closed:
            raise DatabaseError("Database pool is not connected")
        return self._pool.acquire()

    async def execute(self, query: str, *args: Any) -> str:
        """Execute a query and return command completion tag.

        Args:
            query: SQL query string.
            *args: Query parameters.

        Returns:
            Command completion tag.
        """
        async with self.acquire() as conn:
            return await conn.execute(query, *args)

    async def fetch(self, query: str, *args: Any) -> list[asyncpg.Record]:
        """Fetch multiple rows.

        Args:
            query: SQL query string.
            *args: Query parameters.

        Returns:
            List of asyncpg.Record.
        """
        async with self.acquire() as conn:
            return await conn.fetch(query, *args)

    async def fetchrow(self, query: str, *args: Any) -> asyncpg.Record | None:
        """Fetch a single row.

        Args:
            query: SQL query string.
            *args: Query parameters.

        Returns:
            An asyncpg.Record or None.
        """
        async with self.acquire() as conn:
            return await conn.fetchrow(query, *args)

    async def fetchval(self, query: str, *args: Any) -> Any:
        """Fetch a single value.

        Args:
            query: SQL query string.
            *args: Query parameters.

        Returns:
            A single value.
        """
        async with self.acquire() as conn:
            return await conn.fetchval(query, *args)

    async def health_check(self) -> dict[str, Any]:
        """Check database connectivity and return status info.

        Returns:
            Dict with keys: status, latency_ms, pool_stats.
        """
        import time

        if not self._connected or not self._pool or self._pool._closed:
            return {
                "status": "disconnected",
                "latency_ms": None,
                "pool_stats": None,
            }

        try:
            start = time.monotonic()
            async with self.acquire() as conn:
                await conn.fetchval("SELECT 1")
            latency_ms = round((time.monotonic() - start) * 1000, 2)

            pool_stats = {
                "min_size": self._config.min_size,
                "max_size": self._config.max_size,
                "current_size": self._pool.get_size(),
                "available": self._pool.get_idle_size(),
                "size": self._pool.get_size(),
            }

            return {
                "status": "connected",
                "latency_ms": latency_ms,
                "pool_stats": pool_stats,
            }
        except Exception as e:
            return {
                "status": "error",
                "latency_ms": None,
                "pool_stats": None,
                "error": str(e),
            }

    async def recreate(self) -> None:
        """Close and recreate the pool."""
        await self.close()
        await self.connect()

    def __repr__(self) -> str:
        return (
            f"DatabasePool(connected={self._connected}, "
            f"min={self._config.min_size}, max={self._config.max_size}, "
            f"sslmode={self._config.sslmode})"
        )


__all__ = [
    "PoolConfig",
    "DatabasePool",
]
