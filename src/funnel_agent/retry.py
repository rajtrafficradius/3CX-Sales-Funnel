"""Retry-call scheduler — re-call confirmed-Google-Ads prospects we dialed but never converted.

A prospect we called but did NOT reach the decision-maker / book / earn a gatekeeper callback (no
answer, voicemail, or "not interested") is NOT dropped. It goes on a "retry" list and gets ONE open
`retry` calendar event on the LAST BDE's calendar, scheduled at a DIFFERENT time of day than the last
attempt (a top pick-up hour), rolled forward every `retry_cadence_days` until the prospect:
  * picks up / we reach the DM / book / earn a gatekeeper callback (best-ever engaged), or
  * is marked Do-Not-Contact, or
  * hits `retry_max_attempts` (then it's a dead number — stop calling).

GAds-scoped, additive, idempotent. Never touches funnel / analysis data. The BDE can still manually
reassign a retry to a different BDE/line (a fresh number often beats a screener) from the board.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from psycopg_pool import ConnectionPool

from .config import Settings
from .logging import get_logger
from .prospects import gads_dnb_gate
from .recalls import _best_call_hours

log = get_logger(__name__)

_ENSURE_INDEX = (
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_calendar_retry "
    "ON calendar_events (dest_number) WHERE type='retry' AND status='pending'")

# A do_not_contact flag is only a HARD "stop calling" when we actually spoke to a real person who asked
# us to stop. A GATEKEEPER's brush-off ("not interested", "she's not here today") is routinely mis-flagged
# as do_not_contact by the classifier, so it is NOT treated as a hard DNC — those prospects stay retryable
# with a fresh voice/line (the whole premise of the retry engine). MUST match cancel_stale_followups so the
# gather and the canceller agree — otherwise a soft-flagged prospect is scheduled then cancelled every cycle
# (a create/cancel thrash that silently deletes thousands of next-moves).
_HARD_DNC = "(cl.do_not_contact IS TRUE AND COALESCE(cl.call_outcome,'') <> 'gatekeeper')"

# confirmed-Google-Ads phone numbers (last-9) — the calling universe.
_GADS_D9 = (
    "SELECT right(regexp_replace(COALESCE(co.phone,co.phone_norm),'[^0-9]','','g'),9) "
    "FROM enrichment ge JOIN companies co ON co.domain=ge.domain "
    "WHERE (ge.dataforseo->>'running_google_ads')='true' AND COALESCE(co.phone,co.phone_norm)<>''"
    + gads_dnb_gate("ge"))

# per-prospect CONTEXT for the calling BDE: last call outcome + what was discussed (problem_summary,
# else the evidence prospect-summary) + every BDE who has called. Aggregated in the gather's `agg` CTE.
_SUMM = "COALESCE(NULLIF(cl.problem_summary,''), NULLIF(cl.evidence->>'prospect_summary',''))"
_CTX = (
    f"(array_agg(cl.call_outcome ORDER BY c.started_at DESC) "
    f"   FILTER (WHERE cl.call_outcome IS NOT NULL))[1] AS last_outcome, "
    f"(array_agg({_SUMM} ORDER BY c.started_at DESC) FILTER (WHERE {_SUMM} IS NOT NULL))[1] AS last_summary, "
    f"array_agg(DISTINCT COALESCE(c.bde_name,c.bde_extension)) "
    f"   FILTER (WHERE COALESCE(c.bde_name,c.bde_extension) IS NOT NULL) AS prior_bdes,")


def _gather_sql(gads_only: bool) -> str:
    """Dialed-but-not-converted prospects: never reached the DM, never a gatekeeper callback, never
    booked, never agency, not DND, under the attempt cap. Includes no-answer, voicemail AND soft 'not
    interested' (anything without a positive-engagement stage). GAds-scoped when requested."""
    gads = f" AND a.d9 IN ({_GADS_D9})" if gads_only else ""
    return f"""
    WITH agg AS (
      SELECT right(regexp_replace(COALESCE(c.dest_number,''),'[^0-9]','','g'),9) AS d9,
             (array_agg(c.dest_number ORDER BY c.started_at DESC))[1] AS dest_number,
             bool_or(cl.pipeline_stage='p5')                                AS ever_booked,
             bool_or(cl.pipeline_stage IN ('p1','p3') OR cl.rpc_connect IS TRUE) AS ever_engaged,
             bool_or(cl.pipeline='pipeline2_existing_agency')               AS ever_agency,
             bool_or({_HARD_DNC})                                          AS ever_dnc,
             max(c.started_at)                                             AS last_attempt,
             (array_agg(COALESCE(c.bde_name,c.bde_extension) ORDER BY c.started_at DESC)
                FILTER (WHERE COALESCE(c.bde_name,c.bde_extension) IS NOT NULL))[1] AS last_bde,
             (array_agg(cl.prospect_company ORDER BY c.started_at DESC)
                FILTER (WHERE cl.prospect_company IS NOT NULL))[1]         AS company,
             {_CTX}
             count(*)                                                      AS attempts
      FROM calls c LEFT JOIN classifications cl ON cl.call_id=c.call_id
      WHERE c.in_scope AND lower(c.direction)='outbound'
      GROUP BY 1
    )
    SELECT a.d9, a.dest_number, a.last_attempt, a.last_bde, a.company, a.attempts,
           a.last_outcome, a.last_summary, a.prior_bdes
    FROM agg a LEFT JOIN prospect_pipeline pp ON pp.dest9=a.d9
    WHERE length(a.d9)=9 AND COALESCE(pp.dnd,false)=false
      AND COALESCE(a.ever_booked,false)=false
      AND COALESCE(a.ever_engaged,false)=false
      AND COALESCE(a.ever_agency,false)=false
      AND COALESCE(a.ever_dnc,false)=false
      AND a.attempts < %(maxatt)s{gads}
    """


def reached_no_next_sql(gads_only: bool) -> str:
    """Prospects we REACHED THE DECISION-MAKER on but who left no next step — no callback, no booking,
    not an agency (typically a soft 'not interested' or 'send me info'). Warmer than a cold no-answer, so
    they get their OWN board tab and are worked manually (not auto-scheduled). GAds-scoped when asked."""
    gads = f" AND a.d9 IN ({_GADS_D9})" if gads_only else ""
    return f"""
    WITH agg AS (
      SELECT right(regexp_replace(COALESCE(c.dest_number,''),'[^0-9]','','g'),9) AS d9,
             (array_agg(c.dest_number ORDER BY c.started_at DESC))[1]        AS dest_number,
             bool_or(cl.rpc_connect IS TRUE)                                 AS ever_dm,
             bool_or(cl.pipeline_stage='p5')                                 AS ever_booked,
             bool_or(cl.pipeline_stage IN ('p1','p3'))                       AS ever_callback,
             bool_or(cl.pipeline='pipeline2_existing_agency')                AS ever_agency,
             bool_or({_HARD_DNC})                                            AS ever_dnc,
             -- any signal the prospect wants further contact (interested) -> reserved for the ORIGINAL
             -- BDE, never handed to the pilot BDE. A CALLBACK REQUEST counts as interested (they asked
             -- us to call back), so a callback-agreed prospect stays with the BDE who earned it.
             bool_or(cl.is_lead IS TRUE OR cl.qualified IS TRUE OR cl.open_to_listening IS TRUE
                     OR COALESCE(cl.callback_requested, false) IS TRUE
                     OR cl.lead_temperature IN ('warm','hot','super_hot'))  AS ever_interested,
             max(c.started_at)                                              AS last_attempt,
             (array_agg(COALESCE(c.bde_name,c.bde_extension) ORDER BY c.started_at DESC)
                FILTER (WHERE COALESCE(c.bde_name,c.bde_extension) IS NOT NULL))[1] AS last_bde,
             (array_agg(cl.prospect_company ORDER BY c.started_at DESC)
                FILTER (WHERE cl.prospect_company IS NOT NULL))[1]           AS company,
             {_CTX}
             count(*)                                                       AS attempts
      FROM calls c JOIN classifications cl ON cl.call_id=c.call_id
      WHERE c.in_scope AND lower(c.direction)='outbound'
      GROUP BY 1
    )
    SELECT a.d9, a.dest_number, a.last_attempt, a.last_bde, a.company, a.attempts,
           a.last_outcome, a.last_summary, a.prior_bdes, a.ever_interested
    FROM agg a LEFT JOIN prospect_pipeline pp ON pp.dest9=a.d9
    WHERE length(a.d9)=9 AND COALESCE(pp.dnd,false)=false
      AND COALESCE(a.ever_dm,false)=true
      AND COALESCE(a.ever_booked,false)=false
      AND COALESCE(a.ever_callback,false)=false
      AND COALESCE(a.ever_agency,false)=false
      AND COALESCE(a.ever_dnc,false)=false{gads}
    """


# --------------------------------------------------------------------------- #
# Human-like time-of-day picker: call each prospect back near when THEY answer.
# --------------------------------------------------------------------------- #
def _prospect_hour_map(pool: ConnectionPool, tz_name: str, d9s: list) -> dict:
    """Per-prospect LOCAL call hours split by outcome: the hours we actually REACHED a
    live human (answered, real talk, not voicemail) vs the hours that FAILED (no-answer /
    voicemail). Lets the retry/reached schedulers ring each prospect back around when THEY
    tend to pick up — the way a human rep remembers "they answer mid-afternoon" — instead
    of a one-size global slot. Read-only; batched for all prospects in one query."""
    out: dict = {}
    d9s = [d for d in dict.fromkeys(d9s) if d]        # de-dupe, drop blanks, keep order
    if not d9s:
        return out
    rows = _fetch(
        pool,
        "SELECT right(regexp_replace(COALESCE(c.dest_number,''),'[^0-9]','','g'),9) AS d9, "
        "  extract(hour from c.started_at AT TIME ZONE %s)::int AS h, "
        "  (c.answered AND COALESCE(c.talk_seconds,0) >= 20 "
        "     AND COALESCE(cl.call_outcome,'') <> 'voicemail') AS reached "
        "FROM calls c LEFT JOIN classifications cl ON cl.call_id=c.call_id "
        "WHERE c.in_scope AND lower(c.direction)='outbound' "
        "  AND right(regexp_replace(COALESCE(c.dest_number,''),'[^0-9]','','g'),9) = ANY(%s)",
        (tz_name, list(d9s)),
    )
    for r in rows:
        d = out.setdefault(r["d9"], {"answered": [], "failed": []})
        (d["answered"] if r["reached"] else d["failed"]).append(int(r["h"]))
    return out


def _local_hour(dt, tz_name: str):
    """LOCAL hour of a stored (UTC) timestamp — so we compare against local best-hours, not the
    raw UTC hour (the old bug: a 2 p.m. Melbourne call read as hour 4 and skewed the next slot)."""
    if dt is None:
        return None
    try:
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=ZoneInfo("UTC"))
        return dt.astimezone(ZoneInfo(tz_name)).hour
    except Exception:
        return getattr(dt, "hour", None)


def _future_when(day, hour: int, now: datetime) -> datetime:
    """A naive-local start time that is NEVER in the past. If (day, hour) has already passed (e.g. an
    overdue prospect placed on today at a pick-up hour that's already gone), bump to the next open
    business hour today, else to the next business day at the same hour — a human never books a call
    for a time that's already gone."""
    hour = min(max(int(hour), 8), 17)
    when = datetime.combine(day, time(hour=hour))
    if when > now:
        return when
    if now.weekday() < 5 and now.hour < 16:            # still time left today → next hour
        return now.replace(hour=min(now.hour + 1, 17), minute=0, second=0, microsecond=0)
    d = now.date() + timedelta(days=1)                 # too late / weekend → next business day
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return datetime.combine(d, time(hour=min(max(hour, 9), 17)))


def _local_date(dt, tz_name: str):
    """LOCAL calendar date of a stored (UTC) timestamp, so cadence spacing lands on the right day."""
    if dt is None:
        return None
    try:
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=ZoneInfo("UTC"))
        return dt.astimezone(ZoneInfo(tz_name)).date()
    except Exception:
        return getattr(dt, "date", lambda: None)()


def _smart_hour(info: dict | None, last_local_hour, global_best: list) -> tuple:
    """Pick the next call HOUR (local) like a human would, returning (hour, why):
      1. If this prospect has PICKED UP before, call back near that hour (their most frequent
         pick-up hour), preferring one different from the attempt that just failed.
      2. Otherwise use a global top pick-up hour we have NOT already burned on this prospect,
         avoiding the hour that just failed.
    The caller clamps to business hours."""
    ans = (info or {}).get("answered") or []
    if ans:
        ranked = [h for h, _ in Counter(ans).most_common()]
        h = next((x for x in ranked if x != last_local_hour), ranked[0])
        return h, "when they've picked up before"
    failed = set((info or {}).get("failed") or [])
    for h in global_best:
        if h not in failed and h != last_local_hour:
            return h, "a top pick-up hour we haven't tried on them"
    h = next((x for x in global_best if x != last_local_hour), global_best[0])
    return h, "a top pick-up hour, different from last time"


class _DayCap:
    """Per-(BDE, local-day) capacity gate so each BDE's calendar stays at `cap` calls/day and fills
    DAY-BY-DAY. Counts every existing pending event as baseline occupancy. `place(bde, due, today)`:
      * a FUTURE-dated commitment (due > today — e.g. a callback the prospect asked for next week) is
        placed on/after its due business day, first day with spare capacity;
      * an OVERDUE backlog item (due <= today) only fills the next `horizon` business days from today —
        the rest returns None and defers to a later cycle (so we never pre-stuff weeks of calendar).
    Weekends are skipped."""
    def __init__(self, pool: ConnectionPool, cap: int, tz: str, horizon_days: int):
        self.cap = max(1, int(cap)); self.tz = tz; self.horizon = max(1, int(horizon_days))
        rows = _fetch(pool,
            "SELECT bde_name, (start_at AT TIME ZONE %s)::date d, count(*) n FROM calendar_events "
            "WHERE status='pending' AND bde_name IS NOT NULL GROUP BY 1,2", (tz,))
        self.counts = {(r["bde_name"], r["d"]): int(r["n"]) for r in rows}

    def place(self, bde, due, today):
        d = due if due and due > today else today
        business_seen = 0
        for _ in range(400):
            if d.weekday() >= 5:
                d += timedelta(days=1); continue
            if not (due and due > today):                 # overdue -> only the next `horizon` biz days
                business_seen += 1
                if business_seen > self.horizon:
                    return None
            if self.counts.get((bde, d), 0) < self.cap:
                self.counts[(bde, d)] = self.counts.get((bde, d), 0) + 1
                return d
            d += timedelta(days=1)
        return None


def _pending_dest9s(pool: ConnectionPool, ev_type: str, *, recent_done_days: int = 0) -> set:
    """dest9 of prospects that already have an OPEN event of this type (skip → don't double-schedule).
    When recent_done_days>0, ALSO skip prospects whose event of this type was marked DONE within that many
    days — so a BDE marking a call done doesn't instantly re-spawn the same follow-up before its cadence
    (the 'it reappears right after I tick it off' bug)."""
    sql = ("SELECT DISTINCT right(regexp_replace(COALESCE(dest_number,''),'[^0-9]','','g'),9) d9 "
           "FROM calendar_events WHERE type=%s AND (status='pending'")
    params: list = [ev_type]
    if recent_done_days > 0:
        sql += (" OR (status='done' AND COALESCE(start_at, created_at) "
                ">= current_date - make_interval(days => %s))")
        params.append(int(recent_done_days))
    sql += ")"
    return {r["d9"] for r in _fetch(pool, sql, tuple(params)) if r.get("d9")}


def schedule_retry_calls(pool: ConnectionPool, settings: Settings) -> dict:
    """Ensure each retry-eligible prospect has ONE open `retry` event on the calendar of the BDE who
    LAST called it (their own sourced data), scheduled at a smart time and capped per BDE per day.
    Cancels retries superseded by a later call. Idempotent; safe every cycle."""
    if not getattr(settings, "retry_enabled", True):
        return {"skipped": "disabled"}
    maxatt = int(getattr(settings, "retry_max_attempts", 5) or 5)
    cadence = int(getattr(settings, "retry_cadence_days", 3) or 3)
    limit = int(getattr(settings, "retry_per_cycle", 4000) or 4000)
    # PILOT phase: the calendar carries ONLY the confirmed-Google-Ads pool (calls_gads_only), and only
    # for the pilot BDEs (CALENDAR_ALLOC_NAMES — Mohit/Alfred/Ben). Their BDE/BDM-sourced (non-GAds)
    # data is deliberately NOT scheduled.
    gads_only = bool(getattr(settings, "calls_gads_only", False))
    _alloc = {n.strip().lower() for n in (getattr(settings, "calendar_alloc_bdes", None) or [])}
    tz = getattr(settings, "tz", "Australia/Melbourne")
    now = datetime.now()

    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(_ENSURE_INDEX)
        # SUPERSEDED: a later outbound call happened after the retry was scheduled → cancel it.
        cur.execute(
            "UPDATE calendar_events e SET status='cancelled' "
            "WHERE e.type='retry' AND e.status='pending' AND EXISTS (SELECT 1 FROM calls c "
            "  WHERE c.in_scope AND lower(c.direction)='outbound' "
            "  AND right(regexp_replace(COALESCE(c.dest_number,''),'[^0-9]','','g'),9) "
            "     = right(regexp_replace(COALESCE(e.dest_number,''),'[^0-9]','','g'),9) "
            "  AND c.started_at > e.start_at + interval '2 hours')")
        conn.commit()

    best_hours = _best_call_hours(pool, tz, 11) or [11, 14, 10]
    cap = _DayCap(pool, getattr(settings, "bde_daily_call_target", 200),
                  tz, getattr(settings, "fresh_alloc_horizon_days", 1))
    today = now.date()
    already = _pending_dest9s(pool, "retry", recent_done_days=cadence)   # skip open OR just-completed retries
    rows = _fetch(pool, _gather_sql(gads_only), {"maxatt": maxatt})[:limit]
    rows.sort(key=lambda r: r.get("last_attempt") or now)  # oldest-overdue first claim the day's slots
    hour_map = _prospect_hour_map(pool, tz, [r.get("d9") for r in rows])
    to_upsert = []
    for r in rows:
        d9 = r.get("d9")
        if not r.get("dest_number") or not d9 or d9 in already:
            continue
        bde = r.get("last_bde")                          # the BDE who last called it works their own retry
        if not bde or (_alloc and bde.strip().lower() not in _alloc):
            continue                                     # pilot phase: only the pilot BDEs' own retries
        last = r.get("last_attempt") or now
        due = (_local_date(last, tz) or today) + timedelta(days=cadence)
        day = cap.place(bde, due, today)                 # per-BDE daily cap + day-by-day (today first)
        if day is None:
            continue                                     # BDE's day(s) full → deferred to a later cycle
        hour, why = _smart_hour(hour_map.get(d9), _local_hour(last, tz), best_hours)
        hour = min(max(hour, 8), 17)
        when = _future_when(day, hour, now)
        att = int(r.get("attempts") or 0)
        who = r.get("company") or r.get("dest_number")
        title = f"🔄 Retry ({att + 1}/{maxatt}): {who}"
        lead = (f"No pickup yet — retry at ~{hour}:00 ({why}). "
                "A fresh 3CX/Aircall line often gets a screener to answer.")
        notes = _context_note(r, att, maxatt, lead=lead)
        to_upsert.append((bde, title, when, when + timedelta(minutes=15), notes, r["dest_number"]))
    scheduled = _upsert_batch(pool, to_upsert)
    stats = {"scheduled": scheduled, "candidates": len(rows), "placed": len(to_upsert)}
    log.info("retry_calls_done", **stats)
    return stats


_ENSURE_REACHED_INDEX = (
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_calendar_reached "
    "ON calendar_events (dest_number) WHERE type='reached_call' AND status='pending'")


def schedule_reached_calls(pool: ConnectionPool, settings: Settings) -> dict:
    """Re-call REACHED-DM-but-no-next-step prospects, ROTATING the BDE (a fresh voice/line often turns a
    soft 'not interested' around) at a varied time, until they show interest or hit reached_max_attempts.
    One open `reached_call` per number; a new attempt (rotated BDE) is created only after the prior one
    is superseded by an actual call. Every event carries the full call context."""
    if not getattr(settings, "reached_enabled", True):
        return {"skipped": "disabled"}
    maxatt = int(getattr(settings, "reached_max_attempts", 8) or 8)
    cadence = int(getattr(settings, "retry_cadence_days", 3) or 3)
    gads_only = bool(getattr(settings, "calls_gads_only", False))   # pilot: confirmed-Google-Ads pool only
    tz = getattr(settings, "tz", "Australia/Melbourne")
    now = datetime.now()
    # A reached-but-REJECTED prospect (not interested / already has an agency) goes to the fresh-pool
    # PILOT (Mohit) for a fresh voice/angle; an INTERESTED one stays with the pilot BDE who reached them.
    _alloc = {n.strip().lower() for n in (getattr(settings, "calendar_alloc_bdes", None) or [])}
    pilot = next((n for n in (getattr(settings, "calendar_alloc_bdes", None) or [])), None)

    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(_ENSURE_REACHED_INDEX)
        cur.execute(
            "UPDATE calendar_events e SET status='cancelled' "
            "WHERE e.type='reached_call' AND e.status='pending' AND EXISTS (SELECT 1 FROM calls c "
            "  WHERE c.in_scope AND lower(c.direction)='outbound' "
            "  AND right(regexp_replace(COALESCE(c.dest_number,''),'[^0-9]','','g'),9) "
            "     = right(regexp_replace(COALESCE(e.dest_number,''),'[^0-9]','','g'),9) "
            "  AND c.started_at > e.start_at + interval '2 hours')")
        conn.commit()

    best_hours = _best_call_hours(pool, tz, 11) or [11, 14, 10]
    cap = _DayCap(pool, getattr(settings, "bde_daily_call_target", 200),
                  tz, getattr(settings, "fresh_alloc_horizon_days", 1))
    today = now.date()
    already = _pending_dest9s(pool, "reached_call", recent_done_days=cadence)
    rows = _fetch(pool, reached_no_next_sql(gads_only))
    rows.sort(key=lambda r: r.get("last_attempt") or now)   # oldest-overdue first
    hour_map = _prospect_hour_map(pool, tz, [r.get("d9") for r in rows])
    to_insert = []
    for r in rows:
        att = int(r.get("attempts") or 0)
        d9 = r.get("d9")
        if att >= maxatt or not r.get("dest_number") or not d9 or d9 in already:
            continue
        interested = bool(r.get("ever_interested"))
        if interested:
            bde = r.get("last_bde")           # positive / callback → the BDE who reached them follows up
            if not bde or (_alloc and bde.strip().lower() not in _alloc):
                continue                       # pilot phase: skip interested prospects owned by non-pilots
        else:
            bde = pilot or r.get("last_bde")  # rejected / not-interested → the pilot (Mohit), else own BDE
            if not bde:
                continue
        la = r.get("last_attempt") or now
        due = (_local_date(la, tz) or today) + timedelta(days=cadence)
        day = cap.place(bde, due, today)      # per-BDE daily cap + day-by-day
        if day is None:
            continue
        hour, why = _smart_hour(hour_map.get(d9), _local_hour(la, tz), best_hours)
        hour = min(max(hour, 8), 17)
        when = _future_when(day, hour, now)
        who = r.get("company") or r.get("dest_number")
        title = f"👤 Reached DM · retry ({att + 1}/{maxatt}): {who}"
        if interested:
            lead = (f"Reached the decision-maker — {bde} follows up their own prospect at ~{hour}:00 ({why}).")
        else:
            lead = (f"Reached the DM but left no next step — {bde} takes a fresh voice/angle at ~{hour}:00 "
                    f"({why}).")
        notes = _context_note(r, att, maxatt, lead=lead)
        to_insert.append((bde, title, when, when + timedelta(minutes=15), notes, r["dest_number"]))
    scheduled = _insert_reached_batch(pool, to_insert)
    stats = {"scheduled": scheduled, "candidates": len(rows), "placed": len(to_insert)}
    log.info("reached_calls_done", **stats)
    return stats


def _insert_reached_batch(pool, rows: list) -> int:
    """Insert reached_call events; ON CONFLICT DO NOTHING keeps the open one (rotation happens on the
    NEXT attempt, after a real call supersedes the current event) so the BDE isn't reshuffled each cycle."""
    if not rows:
        return 0
    with pool.connection() as conn, conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO calendar_events (bde_name, type, title, start_at, end_at, notes, "
            "  dest_number, created_by, status) "
            "VALUES (%s,'reached_call',%s,%s,%s,%s,%s,'auto','pending') "
            "ON CONFLICT (dest_number) WHERE type='reached_call' AND status='pending' DO NOTHING",
            rows)
        conn.commit()
    return len(rows)


def _upsert_batch(pool, rows: list) -> int:
    """Batch upsert retry events (one open retry per number; refreshes an existing pending one)."""
    if not rows:
        return 0
    with pool.connection() as conn, conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO calendar_events (bde_name, type, title, start_at, end_at, notes, "
            "  dest_number, created_by, status) "
            "VALUES (%s,'retry',%s,%s,%s,%s,%s,'auto','pending') "
            "ON CONFLICT (dest_number) WHERE type='retry' AND status='pending' "
            "DO UPDATE SET bde_name=EXCLUDED.bde_name, title=EXCLUDED.title, "
            "  start_at=EXCLUDED.start_at, notes=EXCLUDED.notes",
            rows)
        conn.commit()
    return len(rows)


def _context_note(r: dict, att: int, maxatt: int, *, lead: str) -> str:
    """Full context for whichever BDE picks up the call: which attempt this is, everyone who has
    called before, and the last call's outcome + what was actually discussed (so a fresh BDE isn't
    calling blind). The complete history + AI 'what to say' is one click away on the prospect page."""
    priors = ", ".join(dict.fromkeys(r.get("prior_bdes") or [])) or (r.get("last_bde") or "")
    summ = (r.get("last_summary") or "").strip()
    if len(summ) > 260:
        summ = summ[:257] + "…"
    outcome = (r.get("last_outcome") or "").replace("_", " ")
    lines = [f"Attempt {att + 1}" + (f" of {maxatt}" if maxatt else "") + ".", lead]
    if priors:
        lines.append(f"Called before by: {priors}.")
    if outcome or summ:
        lines.append(f"Last call{(' — ' + outcome) if outcome else ''}: {summ or '(no notes captured)'}")
    lines.append("Full call history + what-to-say → open the prospect page.")
    return "\n".join(x for x in lines if x)


def _fetch(pool: ConnectionPool, sql: str, params=None) -> list[dict]:
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()
