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

from datetime import datetime, time, timedelta

from psycopg_pool import ConnectionPool

from .config import Settings
from .logging import get_logger
from .recalls import _best_call_hours

log = get_logger(__name__)

_ENSURE_INDEX = (
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_calendar_retry "
    "ON calendar_events (dest_number) WHERE type='retry' AND status='pending'")

# confirmed-Google-Ads phone numbers (last-9) — the calling universe.
_GADS_D9 = (
    "SELECT right(regexp_replace(COALESCE(co.phone,co.phone_norm),'[^0-9]','','g'),9) "
    "FROM enrichment ge JOIN companies co ON co.domain=ge.domain "
    "WHERE (ge.dataforseo->>'running_google_ads')='true' AND COALESCE(co.phone,co.phone_norm)<>''")

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
           a.last_outcome, a.last_summary, a.prior_bdes
    FROM agg a LEFT JOIN prospect_pipeline pp ON pp.dest9=a.d9
    WHERE length(a.d9)=9 AND COALESCE(pp.dnd,false)=false
      AND COALESCE(a.ever_dm,false)=true
      AND COALESCE(a.ever_booked,false)=false
      AND COALESCE(a.ever_callback,false)=false
      AND COALESCE(a.ever_agency,false)=false{gads}
    """


def schedule_retry_calls(pool: ConnectionPool, settings: Settings) -> dict:
    """Ensure each retry-eligible prospect has ONE open `retry` event (same BDE, varied time). Cancels
    retries superseded by a later call. Idempotent; safe every cycle. Returns {scheduled, candidates}."""
    if not getattr(settings, "retry_enabled", True):
        return {"skipped": "disabled"}
    maxatt = int(getattr(settings, "retry_max_attempts", 5) or 5)
    cadence = int(getattr(settings, "retry_cadence_days", 3) or 3)
    limit = int(getattr(settings, "retry_per_cycle", 4000) or 4000)
    gads_only = bool(getattr(settings, "calls_gads_only", False))
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
    rows = _fetch(pool, _gather_sql(gads_only), {"maxatt": maxatt})[:limit]
    to_upsert = []
    for i, r in enumerate(rows):
        if not r.get("last_bde") or not r.get("dest_number"):
            continue
        last = r.get("last_attempt") or now
        last_hour = getattr(last, "hour", 11)
        # vary the time-of-day: a top pick-up hour DIFFERENT from the last attempt's hour
        hour = next((h for h in best_hours if h != last_hour), best_hours[i % len(best_hours)])
        base = getattr(last, "date", lambda: now.date())() + timedelta(days=cadence)
        while base < now.date():
            base += timedelta(days=cadence)
        if base.weekday() >= 5:                          # Sat/Sun → Monday
            base += timedelta(days=7 - base.weekday())
        when = datetime.combine(base, time(hour=min(max(hour, 8), 17)))
        att = int(r.get("attempts") or 0)
        who = r.get("company") or r.get("dest_number")
        title = f"🔄 Retry ({att + 1}/{maxatt}): {who}"
        lead = (f"No pickup yet — retry at ~{hour}:00 (a top pick-up hour, a different time than last). "
                "A fresh 3CX/Aircall line often gets a screener to answer — reassign to another BDE if "
                "it keeps ringing out.")
        notes = _context_note(r, att, maxatt, lead=lead)
        to_upsert.append((r["last_bde"], title, when, when + timedelta(minutes=15), notes, r["dest_number"]))
    scheduled = _upsert_batch(pool, to_upsert)
    stats = {"scheduled": scheduled, "candidates": len(rows)}
    log.info("retry_calls_done", **stats)
    return stats


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
