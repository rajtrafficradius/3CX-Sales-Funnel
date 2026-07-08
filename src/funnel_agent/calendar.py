"""In-dashboard calendar: event CRUD + auto-assigning interested-prospect callbacks.

Callbacks (classifications.callback_requested) become calendar_events of type
'callback' on the call's BDE, idempotent per source call_id. The requested time is
parsed best-effort from the free-text the prospect said (callback_when).
"""

from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from psycopg_pool import ConnectionPool

from .logging import get_logger

log = get_logger(__name__)

_WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
_MEL = ZoneInfo("Australia/Melbourne")

# Tokens that encode a CONCRETE when. A callback phrase with none of these (and no digit) is VAGUE
# ("after his meeting", "when he's free", "later") — we must NOT pretend the prospect named a time.
_CONCRETE_TIME_TOKENS = (
    "today", "tomorrow", "tonight", "monday", "tuesday", "wednesday", "thursday", "friday",
    "saturday", "sunday", "morning", "afternoon", "evening", "noon", "midday", "week", "fortnight",
    "month", "january", "february", "march", "april", "june", "july", "august", "september",
    "october", "november", "december")
# "after his meeting / call / appointment / lunch …" reads as LATER THE SAME DAY, not next week.
_SAME_DAY_AFTER = re.compile(
    r"\bafter\b[^.]*\b(meeting|call|appointment|appt|lunch|break|shift|class|session|surgery|"
    r"procedure|round|delivery|run|job|site|handover|standup|stand-up|catch[- ]?up)\b", re.I)


def callback_time_given(text: str | None) -> bool:
    """Did the prospect actually give a CONCRETE callback time/day (vs a vague 'after his meeting')?"""
    t = (text or "").lower().strip()
    if not t:
        return False
    if re.search(r"\d", t):                       # a digit: '5pm', 'the 27th', 'in 3 days'
        return True
    return any(tok in t for tok in _CONCRETE_TIME_TOKENS)


def resolve_callback_slot(cbw: str | None, started_at, base: date) -> tuple[datetime, bool]:
    """(when, time_was_given) for a callback phrase. When a concrete time was stated, trust
    guess_when. When VAGUE: 'after his meeting/call/etc' → LATER THE SAME DAY (a few hours after this
    call, kept inside business hours; too late → next business morning) — how a human would actually
    re-try — otherwise the next-business-day default. `when` is naive local (Australia/Melbourne)."""
    if callback_time_given(cbw):
        return guess_when(cbw, base), True
    if started_at is not None and _SAME_DAY_AFTER.search(cbw or ""):
        loc = started_at.astimezone(_MEL) if getattr(started_at, "tzinfo", None) else started_at
        cand = (loc + timedelta(hours=3)).replace(minute=0, second=0, microsecond=0, tzinfo=None)
        if cand.hour < 16 and cand.weekday() < 5:          # still a sensible business-hours slot today
            return cand, False
        return guess_when("tomorrow morning", loc.date()), False   # too late / weekend → next AM
    return guess_when(cbw, base), False               # generic vague phrase → next-business-day default


def _clean_time_noise(s: str | None) -> str:
    """Strip our-side noise from a prospect-facing phrase — e.g. '(Richard's time)' (a BDE alias)
    or '(their time)': a parenthetical mentioning 'time' is a timezone aside worth nothing here."""
    if not s:
        return ""
    return re.sub(r"\s*\([^)]*\btime\b[^)]*\)", "", s).strip(" .,-")


def guess_when(text: str | None, base: date) -> datetime:
    """Best-effort parse of a callback phrase into a concrete datetime. Falls back
    to the next business day at 10:00. The BDE can adjust it in the calendar."""
    t = (text or "").lower()
    # normalise worded quantities so the numeric rules below catch them
    t = re.sub(r"\b(?:a|an|one)\s+(week|month)", r"1 \1", t)
    t = re.sub(r"\bcouple\s+(?:of\s+)?(weeks?|months?)", r"2 \1", t)
    t = re.sub(r"\b(?:few|several)\s+(weeks?|months?)", r"3 \1", t)
    day = base + timedelta(days=1)  # default: tomorrow
    _mo = re.search(r"(\d+)\s*months?", t)
    _wk = re.search(r"(\d+)\s*weeks?", t)
    _MONTHS = {"january": 1, "february": 2, "march": 3, "april": 4, "june": 6, "july": 7,
               "august": 8, "september": 9, "october": 10, "november": 11, "december": 12}
    _mname = next((n for n in _MONTHS if n in t), None)   # 'may' handled below (too ambiguous)
    _mnum = _MONTHS[_mname] if _mname else (5 if re.search(r"\b(in|until|till|by|early|mid|late)\s+may\b", t) else None)
    _ord = re.search(r"\b(\d{1,2})(?:st|nd|rd|th)\b", t)

    def _safe(y, mo, d):
        for dd in (d, 28, 1):
            try:
                return date(y, mo, dd)
            except ValueError:
                continue
        return base + timedelta(days=1)

    if "today" in t:
        day = base
    elif "tomorrow" in t:
        day = base + timedelta(days=1)
    elif _mnum:                                  # a named month e.g. 'back in August', 'until July'
        d = int(_ord.group(1)) if _ord else 1
        y = base.year + (1 if (_mnum, d) <= (base.month, base.day) else 0)
        day = _safe(y, _mnum, d)
    elif _ord:                                   # a day-of-month like 'on the 27th' (this/next month)
        d = int(_ord.group(1))
        cand = _safe(base.year, base.month, d)
        day = cand if cand > base else _safe(base.year + (base.month == 12), (base.month % 12) + 1, d)
    elif _mo or "next month" in t:               # 'in 2 months', 'next month' (~30d/mo)
        day = base + timedelta(days=30 * (int(_mo.group(1)) if _mo else 1))
    elif _wk:                                     # 'in 4 weeks', '3 weeks' -> whole weeks
        day = base + timedelta(days=7 * int(_wk.group(1)))
    elif "next week" in t or "a week" in t or "fortnight" in t:
        day = base + timedelta(days=14 if "fortnight" in t else 7)
    elif "end of the month" in t or "end of month" in t:
        day = _safe(base.year, base.month, 28)   # ~end of the current month
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
    # NEVER show cancelled events — the recall engine cancels/supersedes thousands of them each cycle
    # (they accumulate into the hundreds of thousands); rendering them floods the calendar. Only live
    # (pending) + completed (done) events belong on the board.
    where = ["e.status <> 'cancelled'", "e.start_at >= %(s)s::date", "e.start_at < (%(e)s::date + 1)"]
    params: dict = {"s": start, "e": end}
    if bde_name:
        where.append("e.bde_name = %(b)s")
        params["b"] = bde_name
    with pool.connection() as conn, conn.cursor() as cur:
        # tier = the prospect's next-call priority tier (A), for calendar colour-coding.
        cur.execute(
            "SELECT e.id, e.bde_name, e.type, e.title, e.start_at, e.end_at, e.status, e.call_id, "
            "       e.dest_number, e.notes, nc.tier "
            "FROM calendar_events e "
            "LEFT JOIN next_call_queue nc ON e.dest_number IS NOT NULL AND nc.dest9 = "
            "  right(regexp_replace(e.dest_number,'[^0-9]','','g'),9) "
            f"WHERE {' AND '.join(where)} ORDER BY e.start_at",
            params,
        )
        return cur.fetchall()


def purge_cancelled(pool: ConnectionPool, older_than_days: int = 3) -> int:
    """Hard-delete cancelled events older than N days. The recall engine cancels/supersedes many events
    every cycle; without a purge they accumulate into the hundreds of thousands and bloat the table.
    Cancelled rows are dead (a new recall makes a new row), so this is safe. Returns rows removed."""
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "DELETE FROM calendar_events WHERE status='cancelled' "
            "AND COALESCE(created_at, start_at) < now() - make_interval(days => %s)",
            (int(older_than_days),))
        n = cur.rowcount
        conn.commit()
    return n


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


def synth_next_call_points_text(evidence: dict | None) -> str:
    """Plain-text next-call game plan from a call's evidence — prefers the AI's
    next_call_points (W), else synthesizes from unhandled objections / problem / buying
    signals / next step. Used to enrich the callback event's notes so whoever picks up the
    scheduled callback sees the ready-made talking points."""
    ev = evidence or {}
    pts = ev.get("next_call_points") if isinstance(ev, dict) else None
    lines: list[str] = []
    if isinstance(pts, list) and pts:
        for p in pts[:5]:
            if isinstance(p, dict) and p.get("point"):
                lines.append("• " + str(p["point"]))
    else:
        for o in (ev.get("objections") or [])[:2]:
            if isinstance(o, dict) and o.get("handled") is False and o.get("objection"):
                lines.append('• Pre-empt: "%s"' % o["objection"])
        if ev.get("problem_summary"):
            lines.append("• Re-open on their problem: " + str(ev["problem_summary"]))
        for s in (ev.get("buying_signals") or [])[:1]:
            lines.append("• Build on their interest: " + str(s))
        if ev.get("next_step"):
            lines.append("• " + str(ev["next_step"]))
    return "\n".join(lines)


def sync_callbacks_for_day(pool: ConnectionPool, day: date) -> dict:
    """Create/refresh a calendar 'callback' for every interested-prospect callback request on `day`.
    Idempotent via the call_id unique index; PENDING auto callbacks are also REFRESHED each run so an
    improved slot/reason (e.g. a vague 'after his meeting' rescheduled honestly) self-heals — but a
    BDE's manually-rescheduled or completed callback is never overwritten. Notes carry the game plan."""
    start = datetime.combine(day, time.min)
    end = start + timedelta(days=1)
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT c.call_id, COALESCE(c.bde_name, c.bde_extension) AS bde_name, c.dest_number, "
            "c.started_at, cl.callback_when, cl.prospect_company, cl.evidence, cl.rpc_connect, "
            "(SELECT p.source FROM prospects p WHERE "
            "   right(regexp_replace(COALESCE(c.dest_number,''),'[^0-9]','','g'),9) = ANY(p.phones_norm) "
            "   LIMIT 1) AS source "
            "FROM calls c JOIN classifications cl ON cl.call_id = c.call_id "
            "LEFT JOIN calendar_events e ON e.call_id = c.call_id AND e.type='callback' "
            "WHERE c.in_scope AND c.started_at >= %s AND c.started_at < %s "
            "  AND cl.callback_requested "
            "  AND (e.id IS NULL OR (e.created_by='auto' AND e.status='pending'))",
            (start, end),
        )
        pending = cur.fetchall()
    created = 0
    suppressed = 0
    for r in pending:
        # POLICY (user-confirmed 2026-07-08): ALL data is BDE-sourced (master_file = Raven's BDE list
        # too). We ONLY schedule a callback when we actually reached the decision-maker (an RPC-
        # connect); a gatekeeper-only "call back later" is never auto-scheduled, whatever the source.
        bde_sourced = True
        reached_dm = bool(r.get("rpc_connect"))
        if not reached_dm:
            suppressed += 1
            continue
        base = (r["started_at"].date() if r["started_at"] else day)
        # strip our-side noise like '(Richard's time)' (Richard = a BDE alias) from the display.
        cbw = _clean_time_noise(r["callback_when"])
        # Resolve the slot HONESTLY: a concrete stated time is trusted; a vague phrase ("after his
        # meeting") is NOT turned into a fake precise time — it gets a sensible same-day/next-morning
        # slot and the reason says the time wasn't actually given (so the calendar never lies).
        when, time_given = resolve_callback_slot(r["callback_when"], r["started_at"], base)
        who = r["prospect_company"] or r["dest_number"] or "prospect"
        stage = "RPC callback" if reached_dm else "gatekeeper callback"
        prov = "from BDE-sourced data" if bde_sourced else "from curated database"
        when_s = when.strftime("%a %-d %b at %-I:%M %p")
        title = f"📞 {stage}: {who}" + (f" ({cbw})" if cbw and time_given else "")
        plan = synth_next_call_points_text(r.get("evidence"))
        # lead with WHY this time + the classification/provenance, then the game plan.
        if time_given:
            reason = f"They asked us to call back ‘{cbw}’, so it’s booked for {when_s}."
        elif cbw:
            reason = (f"No exact callback time was given (they said ‘{cbw}’) — provisionally set for "
                      f"{when_s}; confirm the time when you reach them.")
        else:
            reason = f"No callback time was given — provisionally set for {when_s}; confirm on the call."
        notes = f"⏰ Why this time: {reason}\n🏷️ Classification: {stage} · {prov}.\n\n"
        notes += ("Next-call game plan:\n" + plan + "\n\n") if plan else ""
        notes = notes.strip() or cbw
        try:
            # Upsert: create new, or REFRESH an existing auto+pending callback (updated slot/reason).
            # The WHERE on the conflict guard means a BDE-edited or completed event is never clobbered.
            with pool.connection() as conn, conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO calendar_events (bde_name, type, title, start_at, end_at, notes, "
                    "  call_id, dest_number, created_by, status) "
                    "VALUES (%(bde)s,'callback',%(title)s,%(start)s,%(end)s,%(notes)s,%(cid)s,%(dest)s,'auto','pending') "
                    "ON CONFLICT (call_id) WHERE type='callback' AND call_id IS NOT NULL "
                    "DO UPDATE SET title=EXCLUDED.title, start_at=EXCLUDED.start_at, "
                    "  end_at=EXCLUDED.end_at, notes=EXCLUDED.notes, bde_name=EXCLUDED.bde_name "
                    "WHERE calendar_events.created_by='auto' AND calendar_events.status='pending'",
                    {"bde": r["bde_name"], "title": title, "start": when,
                     "end": when + timedelta(minutes=30), "notes": notes,
                     "cid": r["call_id"], "dest": r["dest_number"]})
                conn.commit()
            created += 1
        except Exception as exc:  # unique index race / already exists
            log.warning("callback_upsert_skip", call_id=r["call_id"], error=str(exc)[:120])
    if created or suppressed:
        log.info("callbacks_synced", day=str(day), created=created, suppressed_bde_gk=suppressed)
    return {"callbacks": created, "suppressed_bde_gk": suppressed}
