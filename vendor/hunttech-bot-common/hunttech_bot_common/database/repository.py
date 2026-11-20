"""BaseRepository — generic CRUD base class for asyncpg.

Provides standard CRUD operations for any table.
Designed to be subclassed by specific repositories.

Usage::

    class UserRepository(BaseRepository):
        def __init__(self, pool: DatabasePool):
            super().__init__(pool, table_name="users", pk_column="id")

        async def find_by_email(self, email: str) -> asyncpg.Record | None:
            return await self.fetchrow(
                "SELECT * FROM users WHERE email = $1", email
            )

    repo = UserRepository(pool)
    user = await repo.get_by_id(1)
    users = await repo.find_all()
"""

from __future__ import annotations

import logging
from typing import Any

import asyncpg

from hunttech_bot_common.database.pool import DatabasePool

logger = logging.getLogger(__name__)


class BaseRepository:
    """Generic CRUD repository for a single database table.

    Provides standard CRUD operations: get_by_id, find_all, create,
    update, delete, count, exists.

    Attributes:
        pool: DatabasePool instance.
        table_name: Name of the database table.
        pk_column: Name of the primary key column (default: "id").
    """

    def __init__(
        self,
        pool: DatabasePool,
        table_name: str,
        pk_column: str = "id",
    ) -> None:
        """Initialize the repository.

        Args:
            pool: DatabasePool instance.
            table_name: Name of the database table.
            pk_column: Primary key column name.
        """
        self._pool = pool
        self._table = table_name
        self._pk = pk_column

    @property
    def pool(self) -> DatabasePool:
        """Get the database pool."""
        return self._pool

    @property
    def table_name(self) -> str:
        """Get the table name."""
        return self._table

    @property
    def pk_column(self) -> str:
        """Get the primary key column name."""
        return self._pk

    async def get_by_id(self, pk_value: Any) -> asyncpg.Record | None:
        """Get a row by primary key.

        Args:
            pk_value: Primary key value.

        Returns:
            A Record or None if not found.
        """
        return await self._pool.fetchrow(
            f'SELECT * FROM {self._table} WHERE {self._pk} = $1',
            pk_value,
        )

    async def find_all(
        self,
        order_by: str | None = None,
        limit: int | None = None,
        offset: int | None = None,
    ) -> list[asyncpg.Record]:
        """Get all rows with optional ordering and pagination.

        Args:
            order_by: Optional ORDER BY clause (e.g. "created_at DESC").
            limit: Maximum number of rows.
            offset: Number of rows to skip.

        Returns:
            List of Records.
        """
        query = f"SELECT * FROM {self._table}"
        params: list[Any] = []
        param_idx = 0

        if order_by:
            query += f" ORDER BY {order_by}"

        if limit is not None:
            param_idx += 1
            query += f" LIMIT ${param_idx}"
            params.append(limit)

        if offset is not None:
            param_idx += 1
            query += f" OFFSET ${param_idx}"
            params.append(offset)

        return await self._pool.fetch(query, *params)

    async def find_where(
        self,
        conditions: dict[str, Any],
        order_by: str | None = None,
        limit: int | None = None,
    ) -> list[asyncpg.Record]:
        """Find rows by conditions.

        Args:
            conditions: Dict of column -> value for WHERE clause (AND).
            order_by: Optional ORDER BY clause.
            limit: Maximum number of rows.

        Returns:
            List of Records.
        """
        if not conditions:
            return await self.find_all(order_by=order_by, limit=limit)

        clauses = []
        params: list[Any] = []
        for i, (col, val) in enumerate(conditions.items(), start=1):
            clauses.append(f"{col} = ${i}")
            params.append(val)

        where = " AND ".join(clauses)
        query = f"SELECT * FROM {self._table} WHERE {where}"

        if order_by:
            query += f" ORDER BY {order_by}"
        if limit is not None:
            query += f" LIMIT ${len(params) + 1}"
            params.append(limit)

        return await self._pool.fetch(query, *params)

    async def create(self, **kwargs: Any) -> asyncpg.Record | None:
        """Insert a new row and return it.

        Args:
            **kwargs: Column -> value pairs.

        Returns:
            The created Record, or None if RETURNING not supported.
        """
        columns = list(kwargs.keys())
        values = list(kwargs.values())
        placeholders = [f"${i}" for i in range(1, len(values) + 1)]

        query = (
            f"INSERT INTO {self._table} "
            f"({', '.join(columns)}) "
            f"VALUES ({', '.join(placeholders)}) "
            f"RETURNING *"
        )

        return await self._pool.fetchrow(query, *values)

    async def bulk_create(self, rows: list[dict[str, Any]]) -> list[asyncpg.Record]:
        """Insert multiple rows in a single statement.

        Args:
            rows: List of column -> value dicts.

        Returns:
            List of created Records.
        """
        if not rows:
            return []

        columns = list(rows[0].keys())
        all_values: list[Any] = []
        value_groups: list[str] = []

        for i, row in enumerate(rows):
            base = i * len(columns)
            placeholders = [f"${base + j + 1}" for j in range(len(columns))]
            value_groups.append(f"({', '.join(placeholders)})")
            all_values.extend(row[col] for col in columns)

        query = (
            f"INSERT INTO {self._table} "
            f"({', '.join(columns)}) "
            f"VALUES {', '.join(value_groups)} "
            f"RETURNING *"
        )

        return await self._pool.fetch(query, *all_values)

    async def update(
        self,
        pk_value: Any,
        **kwargs: Any,
    ) -> asyncpg.Record | None:
        """Update a row by primary key.

        Only updates the columns passed as kwargs.

        Args:
            pk_value: Primary key value.
            **kwargs: Column -> value pairs to update.

        Returns:
            The updated Record, or None if not found.
        """
        if not kwargs:
            return await self.get_by_id(pk_value)

        set_clauses = []
        params: list[Any] = []
        for i, (col, val) in enumerate(kwargs.items(), start=1):
            set_clauses.append(f"{col} = ${i}")
            params.append(val)

        params.append(pk_value)
        pk_placeholder = f"${len(params)}"

        query = (
            f"UPDATE {self._table} "
            f"SET {', '.join(set_clauses)} "
            f"WHERE {self._pk} = {pk_placeholder} "
            f"RETURNING *"
        )

        return await self._pool.fetchrow(query, *params)

    async def delete(self, pk_value: Any) -> bool:
        """Delete a row by primary key.

        Args:
            pk_value: Primary key value.

        Returns:
            True if a row was deleted, False otherwise.
        """
        result = await self._pool.execute(
            f"DELETE FROM {self._table} WHERE {self._pk} = $1",
            pk_value,
        )
        # Result format: "DELETE N"
        count = int(result.split()[-1]) if result else 0
        return count > 0

    async def delete_where(
        self, conditions: dict[str, Any]
    ) -> int:
        """Delete rows by conditions.

        Args:
            conditions: Dict of column -> value for WHERE clause (AND).

        Returns:
            Number of deleted rows.
        """
        if not conditions:
            return 0

        clauses = []
        params: list[Any] = []
        for i, (col, val) in enumerate(conditions.items(), start=1):
            clauses.append(f"{col} = ${i}")
            params.append(val)

        where = " AND ".join(clauses)
        result = await self._pool.execute(
            f"DELETE FROM {self._table} WHERE {where}",
            *params,
        )
        return int(result.split()[-1]) if result else 0

    async def count(self, conditions: dict[str, Any] | None = None) -> int:
        """Count rows, optionally filtered.

        Args:
            conditions: Optional WHERE conditions.

        Returns:
            Row count.
        """
        if conditions:
            clauses = []
            params: list[Any] = []
            for i, (col, val) in enumerate(conditions.items(), start=1):
                clauses.append(f"{col} = ${i}")
                params.append(val)

            where = " AND ".join(clauses)
            result = await self._pool.fetchval(
                f"SELECT count(*) FROM {self._table} WHERE {where}",
                *params,
            )
        else:
            result = await self._pool.fetchval(
                f"SELECT count(*) FROM {self._table}"
            )

        return result or 0

    async def exists(self, pk_value: Any) -> bool:
        """Check if a row exists by primary key.

        Args:
            pk_value: Primary key value.

        Returns:
            True if the row exists.
        """
        result = await self._pool.fetchval(
            f"SELECT 1 FROM {self._table} WHERE {self._pk} = $1 LIMIT 1",
            pk_value,
        )
        return result is not None

    async def exists_where(self, conditions: dict[str, Any]) -> bool:
        """Check if any row matches conditions.

        Args:
            conditions: Dict of column -> value for WHERE clause.

        Returns:
            True if any matching row exists.
        """
        if not conditions:
            return (await self.count()) > 0

        clauses = []
        params: list[Any] = []
        for i, (col, val) in enumerate(conditions.items(), start=1):
            clauses.append(f"{col} = ${i}")
            params.append(val)

        where = " AND ".join(clauses)
        result = await self._pool.fetchval(
            f"SELECT 1 FROM {self._table} WHERE {where} LIMIT 1",
            *params,
        )
        return result is not None

    async def upsert(
        self,
        data: dict[str, Any],
        conflict_columns: list[str],
        update_columns: list[str] | None = None,
    ) -> asyncpg.Record | None:
        """Insert or update a row (ON CONFLICT DO UPDATE).

        Args:
            data: Column -> value pairs for insert.
            conflict_columns: Columns that define the conflict target.
            update_columns: Columns to update on conflict. If None, all data columns.

        Returns:
            The upserted Record.
        """
        columns = list(data.keys())
        values = list(data.values())
        placeholders = [f"${i}" for i in range(1, len(values) + 1)]

        if update_columns is None:
            update_columns = [c for c in columns if c not in conflict_columns]

        update_set = ", ".join(
            f"{col} = EXCLUDED.{col}" for col in update_columns
        )
        conflict_target = ", ".join(conflict_columns)

        query = (
            f"INSERT INTO {self._table} "
            f"({', '.join(columns)}) "
            f"VALUES ({', '.join(placeholders)}) "
            f"ON CONFLICT ({conflict_target}) "
            f"DO UPDATE SET {update_set} "
            f"RETURNING *"
        )

        return await self._pool.fetchrow(query, *values)

    async def raw_query(
        self, query: str, *args: Any
    ) -> list[asyncpg.Record]:
        """Execute a raw SQL query.

        Args:
            query: SQL query string.
            *args: Query parameters.

        Returns:
            List of Records.
        """
        return await self._pool.fetch(query, *args)

    async def raw_execute(self, query: str, *args: Any) -> str:
        """Execute a raw SQL statement (no rows returned).

        Args:
            query: SQL statement.
            *args: Statement parameters.

        Returns:
            Command completion tag.
        """
        return await self._pool.execute(query, *args)


__all__ = [
    "BaseRepository",
]
