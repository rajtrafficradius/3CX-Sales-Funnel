"""Date-bounded, read-only reads of the 3CX `cdr_output` table.

Column names come from `CdrSchema` (config, filled by Phase B discovery), never
hardcoded here. Identifiers are quoted via psycopg.sql to stay injection-safe
even though they originate from trusted config.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

from psycopg import sql
from psycopg_pool import ConnectionPool

from ..config import CdrSchema


def _day_bounds(day: date) -> tuple[datetime, datetime]:
    start = datetime.combine(day, time.min)
    return start, start + timedelta(days=1)


def fetch_calls_for_day(
    pool: ConnectionPool, cdr: CdrSchema, day: date, *, outbound_only: bool = True
) -> list[dict]:
    """Return canonical call dicts for one day from `cdr_output`.

    Each dict has canonical keys regardless of the underlying column names:
    call_id, bde_extension, direction, dest_number, started_at,
    ring_seconds, talk_seconds, disposition.
    """
    start, end = _day_bounds(day)

    select = sql.SQL(
        "SELECT {call_id} AS call_id, {ext} AS bde_extension, {dir} AS direction, "
        "{dest} AS dest_number, {started} AS started_at, {ring} AS ring_seconds, "
        "{talk} AS talk_seconds, {disp} AS disposition "
        "FROM {table} WHERE {started} >= %(start)s AND {started} < %(end)s"
    ).format(
        call_id=sql.Identifier(cdr.col_call_id),
        ext=sql.Identifier(cdr.col_extension),
        dir=sql.Identifier(cdr.col_direction),
        dest=sql.Identifier(cdr.col_dest_number),
        started=sql.Identifier(cdr.col_started_at),
        ring=sql.Identifier(cdr.col_ring_seconds),
        talk=sql.Identifier(cdr.col_talk_seconds),
        disp=sql.Identifier(cdr.col_disposition),
        table=sql.Identifier(cdr.table),
    )

    params: dict = {"start": start, "end": end}
    if outbound_only:
        select = select + sql.SQL(" AND {dir} = %(outbound)s").format(
            dir=sql.Identifier(cdr.col_direction)
        )
        params["outbound"] = cdr.outbound_value

    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(select, params)
        return cur.fetchall()


def earliest_outbound_times(
    pool: ConnectionPool, cdr: CdrSchema, dest_numbers: list[str]
) -> dict[str, datetime]:
    """Map each dialled number -> the earliest outbound call time in ALL history.

    Used to label Fresh vs Followup deterministically (a call is Fresh iff it is
    the earliest outbound call to that number), independent of ingestion order.
    """
    if not dest_numbers:
        return {}
    q = sql.SQL(
        "SELECT {dest} AS dest, MIN({started}) AS first_time "
        "FROM {table} WHERE {dir} = %(outbound)s AND {dest} = ANY(%(nums)s) "
        "GROUP BY {dest}"
    ).format(
        dest=sql.Identifier(cdr.col_dest_number),
        started=sql.Identifier(cdr.col_started_at),
        table=sql.Identifier(cdr.table),
        dir=sql.Identifier(cdr.col_direction),
    )
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(q, {"outbound": cdr.outbound_value, "nums": dest_numbers})
        return {str(r["dest"]): r["first_time"] for r in cur.fetchall() if r["dest"] is not None}


def min_call_date(pool: ConnectionPool, cdr: CdrSchema) -> date | None:
    """Earliest call date in `cdr_output` — used to auto-detect BACKFILL_START."""
    q = sql.SQL("SELECT MIN({started})::date AS d FROM {table}").format(
        started=sql.Identifier(cdr.col_started_at), table=sql.Identifier(cdr.table)
    )
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(q)
        row = cur.fetchone()
        return row["d"] if row else None
