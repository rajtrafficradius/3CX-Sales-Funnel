"""Database access layer.

`source.py`   -> READ-ONLY pool to the 3CX Postgres (never writes).
`analytics.py` -> read/write pool to the team's analytics Postgres.

Both wrap a lazily-opened psycopg connection pool so importing the modules and
running `healthcheck` work even when a DB is unreachable (the error surfaces at
first use, not at import).
"""

from __future__ import annotations

from collections.abc import Callable

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool


def build_pool(
    dsn: str,
    *,
    read_only: bool,
    min_size: int = 1,
    max_size: int = 4,
    session_timezone: str | None = None,
) -> ConnectionPool:
    """Build a lazily-opened connection pool.

    When `read_only` is True every connection has
    `default_transaction_read_only = on` set, so any write (INSERT/UPDATE/DDL)
    raises `psycopg.errors.ReadOnlySqlTransaction`. This is our hard guarantee
    that we never write to the 3CX database.
    """

    def _configure(conn: psycopg.Connection) -> None:
        conn.row_factory = dict_row
        if session_timezone:
            with conn.cursor() as cur:
                # SET TIME ZONE can't take a bind param; set_config() can.
                cur.execute("SELECT set_config('TimeZone', %s, false)", (session_timezone,))
            conn.commit()
        if read_only:
            with conn.cursor() as cur:
                cur.execute("SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY")
                cur.execute("SET default_transaction_read_only = on")
            conn.commit()

    return ConnectionPool(
        conninfo=dsn,
        min_size=min_size,
        max_size=max_size,
        open=False,  # lazy: opened on first use
        configure=_configure,
        kwargs={"row_factory": dict_row},
    )


def fetch_all(pool: ConnectionPool, sql: str, params: object = None) -> list[dict]:
    """Run a query and return all rows as dicts."""
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def fetch_one(pool: ConnectionPool, sql: str, params: object = None) -> dict | None:
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchone()


def execute(pool: ConnectionPool, sql: str, params: object = None) -> None:
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        conn.commit()


def in_transaction(pool: ConnectionPool, fn: Callable[[psycopg.Cursor], None]) -> None:
    """Run `fn(cursor)` inside a single committed transaction (rolls back on error)."""
    with pool.connection() as conn:
        try:
            with conn.cursor() as cur:
                fn(cur)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
