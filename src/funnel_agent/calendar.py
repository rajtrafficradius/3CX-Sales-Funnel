"""In-dashboard calendar: event CRUD + auto-assigning interested-prospect callbacks.

Callbacks (classifications.callback_requested) become calendar_events of type
'callback' on the call's BDE, idempotent per source call_id. The requested time is
parsed best-effort from the free-text the prospect said (callback_when).
"""

from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta

from psycopg_pool import ConnectionPool

from .logging import get_logger

log = get_logger(__name__)

_WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


def guess_when(text: str | None, base: date) -> datetime:
    """Best-effort parse of a callback phrase into a concrete datetime. Falls back
    to the next business day at 10:00. The BDE can adjust it in the calendar."""
    t = (text or "").lower()
    day = base + timedelta(days=1)  # default: tomorrow
    if "today" in t:
        day = base
    elif "tomorrow" in t:
        day = base + timedelta(days=1)
    elif "next week" in t:
        day = base + timedelta(days=7)
    else:
        m = re.search(r"in (\d+) days?", t)
        if m:
            day = base + timedelta(days=int(m.group(1)))
        else:
            for i, wd in enumerate(_WEEKDAYS):
                if wd in t:
                    ahead = (i - base.weekday()) % 7
                    day = base + timedelta(days=ahead or 7)
                    break
    # skip weekends -> Monday
    if day.weekday() >= 5:
        day += timedelta(days=7 - day.weekday())
    hour = 10
    if "morning" in t:
        hour = 9
    elif "afternoon" in t:
        hour = 14
    elif "evening" in t:
        hour = 17
    m = re.search(r"(\d{1,2})\s*(am|pm)", t)
    if m:
        h = int(m.group(1)) % 12
        hour = h + (12 if m.group(2) == "pm" else 0)
    return datetime.combine(day, time(hour=min(max(hour, 0), 23)))


def list_events(pool: ConnectionPool, start: str, end: str, bde_name: str | None = None) -> list[dict]:
    where = ["start_at >= %(s)s::date", "start_at < (%(e)s::date + 1)"]
    params: dict = {"s": start, "e": end}
    if bde_name:
        where.append("bde_name = %(b)s")
        params["b"] = bde_name
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT id, bde_name, type, title, start_at, end_at, status, call_id, dest_number, notes "
            f"FROM calendar_events WHERE {' AND '.join(where)} ORDER BY start_at",
            params,
        )
        return cur.fetchall()


def create_event(pool: ConnectionPool, *, bde_name, type, title, start_at, end_at=None,
                 notes=None, call_id=None, dest_number=None, created_by=None) -> int:
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO calendar_events (bde_name, type, title, start_at, end_at, notes, "
            "call_id, dest_number, created_by) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
            (bde_name, type, title, start_at, end_at, notes, call_id, dest_number, created_by),
        )
        eid = cur.fetchone()["id"]
        conn.commit()
    return eid


def update_event(pool: ConnectionPool, eid: int, fields: dict, *, restrict_bde: str | None = None) -> bool:
    allowed = {"title", "start_at", "end_at", "status", "notes", "type"}
    sets = {k: v for k, v in fields.items() if k in allowed}
    if not sets:
        return False
    cols = ", ".join(f"{k} = %({k})s" for k in sets)
    where = "id = %(id)s" + (" AND bde_name = %(rb)s" if restrict_bde else "")
    params = {**sets, "id": eid, "rb": restrict_bde}
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(f"UPDATE calendar_events SET {cols} WHERE {where}", params)
        n = cur.rowcount
        conn.commit()
    return bool(n)


def delete_event(pool: ConnectionPool, eid: int, *, restrict_bde: str | None = None) -> bool:
    where = "id = %s" + (" AND bde_name = %s" if restrict_bde else "")
    params = (eid, restrict_bde) if restrict_bde else (eid,)
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(f"DELETE FROM calendar_events WHERE {where}", params)
        n = cur.rowcount
        conn.commit()
    return bool(n)


def sync_callbacks_for_day(pool: ConnectionPool, day: date) -> dict:
    """Create a calendar 'callback' for every interested-prospect callback request on
    `day` that doesn't already have one (idempotent via the call_id unique index)."""
    start = datetime.combine(day, time.min)
    end = start + timedelta(days=1)
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT c.call_id, COALESCE(c.bde_name, c.bde_extension) AS bde_name, c.dest_number, "
            "c.started_at, cl.callback_when, cl.prospect_company "
            "FROM calls c JOIN classifications cl ON cl.call_id = c.call_id "
            "LEFT JOIN calendar_events e ON e.call_id = c.call_id AND e.type='callback' "
            "WHERE c.in_scope AND c.started_at >= %s AND c.started_at < %s "
            "  AND cl.callback_requested AND e.id IS NULL",
            (start, end),
        )
        pending = cur.fetchall()
    created = 0
    for r in pending:
        base = (r["started_at"].date() if r["started_at"] else day)
        when = guess_when(r["callback_when"], base)
        who = r["prospect_company"] or r["dest_number"] or "prospect"
        title = f"📞 Callback: {who}" + (f" ({r['callback_when']})" if r["callback_when"] else "")
        try:
            create_event(pool, bde_name=r["bde_name"], type="callback", title=title,
                         start_at=when, end_at=when + timedelta(minutes=30),
                         notes=(r["callback_when"] or ""), call_id=r["call_id"],
                         dest_number=r["dest_number"], created_by="auto")
            created += 1
        except Exception as exc:  # unique index race / already exists
            log.warning("callback_create_skip", call_id=r["call_id"], error=str(exc)[:120])
    if created:
        log.info("callbacks_synced", day=str(day), created=created)
    return {"callbacks": created}
