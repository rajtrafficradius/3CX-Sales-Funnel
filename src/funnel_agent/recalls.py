"""Weekly recall scheduler — 'call every week until we actually connect'.

ADDITIVE + IDEMPOTENT. Never touches funnel / RPC / classification data. Each cycle it makes
sure the prospects we are ACTIVELY pursuing but have not yet spoken to (plus the existing-agency
rotation) each have ONE open 'recall' event on the ASSIGNED BDE's calendar, dated at the next
weekly slot and rolled forward ~7 days until:
  * we connect (any call with rpc_connect = true), or
  * the prospect is marked Do-Not-Contact, or
  * we exhaust the recall window (weekly_recall_max_weeks).

Scope (deliberately NOT the whole cold P4 worklist, which is millions):
  * un-connected prospects whose latest call lands in P1 (RPC callback) or P3 (gatekeeper
    callback) — i.e. we've engaged, we just haven't reached the decision-maker yet;
  * P2 existing-agency prospects — their weekly rotation next-action, put onto the BDE calendar.

The event is picked up automatically by the prospect page's `next_action` (it reads pending
calendar_events) and by the calendar view.
"""

from __future__ import annotations

import math
from datetime import datetime, time, timedelta

from psycopg_pool import ConnectionPool

from .config import Settings
from .logging import get_logger

log = get_logger(__name__)

# One open recall per prospect, keyed on the dialled number (mirrors idx_calendar_rpc_retry).
_ENSURE_INDEX = (
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_calendar_recall "
    "ON calendar_events (dest_number) WHERE type = 'recall' AND status = 'pending'"
)

# Candidates: un-connected P1/P3 (actively pursued, DM not yet reached) OR P2 agency rotation.
_GATHER = """
WITH agg AS (
  SELECT right(regexp_replace(COALESCE(c.dest_number,''),'[^0-9]','','g'),9) AS d9,
         (array_agg(c.dest_number ORDER BY c.started_at DESC))[1]                        AS dest_number,
         bool_or(cl.rpc_connect IS TRUE)                                                 AS ever_connected,
         max(c.started_at)                                                               AS last_attempt,
         min(c.started_at)                                                               AS first_attempt,
         (array_agg(COALESCE(c.bde_name, c.bde_extension) ORDER BY c.started_at DESC)
            FILTER (WHERE COALESCE(c.bde_name, c.bde_extension) IS NOT NULL))[1]         AS last_bde,
         (array_agg(cl.pipeline_stage ORDER BY c.started_at DESC)
            FILTER (WHERE cl.pipeline_stage IS NOT NULL))[1]                             AS last_stage,
         (array_agg(cl.prospect_company ORDER BY c.started_at DESC)
            FILTER (WHERE cl.prospect_company IS NOT NULL))[1]                           AS company,
         count(*)                                                                        AS attempts
  FROM calls c LEFT JOIN classifications cl ON cl.call_id = c.call_id
  WHERE c.in_scope AND lower(c.direction) = 'outbound'
  GROUP BY 1
)
SELECT a.d9, a.dest_number, a.ever_connected, a.last_attempt, a.first_attempt, a.last_bde,
       a.last_stage, a.company, a.attempts,
       pp.pipeline AS pp_pipeline, pp.assigned_bde, pp.next_action_at, pp.cadence_days,
       pp.business_name AS pp_name
FROM agg a
LEFT JOIN prospect_pipeline pp ON pp.dest9 = a.d9
WHERE length(a.d9) = 9
  AND COALESCE(pp.dnd, false) = false
  AND (
        (a.ever_connected = false AND a.last_stage IN ('p1','p3'))   -- un-connected, actively pursued
        OR pp.pipeline = 'pipeline2_existing_agency'                 -- existing-agency rotation
      )
"""

# Cancel recalls for prospects we've since connected with or that went DND.
_CANCEL_RESOLVED = """
UPDATE calendar_events e SET status = 'cancelled'
WHERE e.type = 'recall' AND e.status = 'pending'
  AND right(regexp_replace(COALESCE(e.dest_number,''),'[^0-9]','','g'),9) IN (
        SELECT right(regexp_replace(COALESCE(c.dest_number,''),'[^0-9]','','g'),9)
        FROM calls c JOIN classifications cl ON cl.call_id = c.call_id
        WHERE cl.rpc_connect IS TRUE
        UNION
        SELECT dest9 FROM prospect_pipeline WHERE dnd
  )
"""

_UPSERT = """
INSERT INTO calendar_events (bde_name, type, title, start_at, end_at, notes, dest_number,
                             created_by, status)
VALUES (%(bde)s, 'recall', %(title)s, %(start)s, %(end)s, %(notes)s, %(dest)s, 'auto', 'pending')
ON CONFLICT (dest_number) WHERE type = 'recall' AND status = 'pending'
DO UPDATE SET bde_name = EXCLUDED.bde_name, title = EXCLUDED.title,
              start_at = EXCLUDED.start_at, end_at = EXCLUDED.end_at, notes = EXCLUDED.notes
"""


def _weekly_slot(anchor: datetime, cadence_days: int, now: datetime, hour: int) -> datetime:
    """Next cadence slot on/after today, anchored to `anchor` and stepped by whole cadence periods
    (so a missed week rolls to the NEXT week, not to today), snapped to a weekday at `hour`. If the
    anchor is ALREADY in the future (a contract-parked agency next-action), keep that date as-is.
    Naive datetime, mirroring guess_when()/the callback scheduler."""
    a = (anchor or now).date()
    if a > now.date():                            # already scheduled ahead — don't push it further
        slot = a
    else:
        days = (now.date() - a).days
        k = max(1, math.ceil((days + 1) / cadence_days))
        slot = a + timedelta(days=k * cadence_days)
        while slot < now.date():
            slot += timedelta(days=cadence_days)
    if slot.weekday() >= 5:                        # Sat/Sun -> Monday
        slot += timedelta(days=7 - slot.weekday())
    return datetime.combine(slot, time(hour=min(max(hour, 0), 23)))


def sync_weekly_recalls(pool: ConnectionPool, settings: Settings) -> dict:
    """Roll the weekly recall calendar forward for all in-scope, actively-pursued un-connected
    prospects + the P2 agency rotation. Returns {'scheduled': n, 'cancelled': m, 'skipped': k}."""
    if not getattr(settings, "weekly_recall_enabled", True):
        return {"skipped": "disabled"}
    cadence_default = int(getattr(settings, "weekly_recall_cadence_days", 7) or 7)
    max_weeks = int(getattr(settings, "weekly_recall_max_weeks", 12) or 12)
    hour = int(getattr(settings, "weekly_recall_hour", 10) or 10)
    horizon_days = int(getattr(settings, "weekly_recall_horizon_days", 45) or 45)
    now = datetime.now()
    horizon = now + timedelta(days=horizon_days)

    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(_ENSURE_INDEX)
        conn.commit()
        cur.execute(_CANCEL_RESOLVED)
        cancelled = cur.rowcount
        # also drop far-future recalls (e.g. contract-parked agency) — they'll be re-created when
        # they come within the horizon; keeps the calendar to actionable near-term calls.
        cur.execute("UPDATE calendar_events SET status='cancelled' WHERE type='recall' "
                    "AND status='pending' AND start_at > %s", (horizon,))
        cancelled += cur.rowcount
        conn.commit()
        cur.execute(_GATHER)
        rows = cur.fetchall()

    params: list[dict] = []
    skipped = 0
    for r in rows:
        is_agency = (r.get("pp_pipeline") == "pipeline2_existing_agency")
        bde = (r.get("assigned_bde") if is_agency else None) or r.get("last_bde")
        if not bde or not r.get("dest_number"):   # must land on a real BDE calendar
            skipped += 1
            continue
        # exhaust the un-connected recall window after max_weeks (agency rotation never exhausts)
        if not is_agency and r.get("first_attempt"):
            weeks = (now.date() - r["first_attempt"].date()).days / 7.0
            if weeks > max_weeks:
                skipped += 1
                continue
        cadence = int(r.get("cadence_days") or cadence_default) if is_agency else cadence_default
        anchor = r.get("next_action_at") if (is_agency and r.get("next_action_at")) else r.get("last_attempt")
        when = _weekly_slot(anchor, cadence, now, hour)
        if when > horizon:                        # contract-parked / too far out — schedule later
            skipped += 1
            continue
        who = r.get("company") or r.get("pp_name") or r.get("dest_number") or "prospect"
        if is_agency:
            title = f"🔁 Agency call-cycle: {who}"
            notes = (f"Existing-agency rotation (every {cadence} days). Attempt "
                     f"#{(r.get('attempts') or 0) + 1}. Assigned to {bde}.")
        else:
            stage = "RPC callback" if r.get("last_stage") == "p1" else "gatekeeper callback"
            title = f"🔁 Weekly recall: {who}"
            notes = (f"Weekly recall — not yet connected ({stage}). Attempt "
                     f"#{(r.get('attempts') or 0) + 1}. Keep calling weekly until we speak "
                     f"to them (auto-cancels once connected or marked DND).")
        params.append({"bde": bde, "title": title, "start": when,
                       "end": when + timedelta(minutes=30), "notes": notes,
                       "dest": r.get("dest_number")})

    scheduled = 0
    if params:
        with pool.connection() as conn, conn.cursor() as cur:   # ONE connection, batched upsert
            cur.executemany(_UPSERT, params)
            conn.commit()
            scheduled = len(params)

    out = {"scheduled": scheduled, "cancelled": cancelled, "skipped": skipped, "candidates": len(rows)}
    log.info("weekly_recalls_synced", **out)
    return out
