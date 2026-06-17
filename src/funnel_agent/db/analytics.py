"""Read/write connection pool to the team's analytics Postgres."""

from __future__ import annotations

from psycopg_pool import ConnectionPool

from . import build_pool


def make_analytics_pool(
    dsn: str, max_size: int = 12, session_timezone: str | None = None
) -> ConnectionPool:
    return build_pool(
        dsn, read_only=False, min_size=1, max_size=max_size,
        session_timezone=session_timezone,
    )
