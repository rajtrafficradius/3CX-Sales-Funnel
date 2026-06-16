"""Read/write connection pool to the team's analytics Postgres."""

from __future__ import annotations

from psycopg_pool import ConnectionPool

from . import build_pool


def make_analytics_pool(dsn: str) -> ConnectionPool:
    return build_pool(dsn, read_only=False, min_size=1, max_size=4)
