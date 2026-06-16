"""READ-ONLY connection pool to the 3CX Postgres database.

Every connection is forced read-only at the session level (see `build_pool`),
so any accidental write raises instead of mutating the PBX database.
"""

from __future__ import annotations

from psycopg_pool import ConnectionPool

from . import build_pool


def make_source_pool(dsn: str) -> ConnectionPool:
    return build_pool(dsn, read_only=True, min_size=1, max_size=2)
