"""Apply the analytics schema (idempotent)."""

from __future__ import annotations

from importlib import resources

from psycopg_pool import ConnectionPool

from ..logging import get_logger

log = get_logger(__name__)


def load_schema_sql() -> str:
    """Read the bundled schema.sql."""
    return resources.files("funnel_agent.db").joinpath("schema.sql").read_text(encoding="utf-8")


def apply_schema(pool: ConnectionPool) -> None:
    """Run schema.sql. Every statement is CREATE ... IF NOT EXISTS, so re-running is a no-op."""
    sql = load_schema_sql()
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(sql)
        conn.commit()
    log.info("schema_applied")
