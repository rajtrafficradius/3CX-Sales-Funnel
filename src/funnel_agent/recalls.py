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

from .calendar import guess_when
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
         -- BEST-EVER stage guards (no-revert rule): once we ever reached the decision-maker (p1 /
         -- rpc_connect) or booked (p5), a later gatekeeper-only call must NOT drag them back into
         -- the gatekeeper recall loop. These let us exclude/cancel such prospects explicitly.
         bool_or(cl.pipeline_stage = 'p5')                                               AS ever_booked,
         bool_or(cl.pipeline_stage = 'p1' OR cl.rpc_connect IS TRUE)                      AS ever_reached_dm,
         max(c.started_at)                                                               AS last_attempt,
         min(c.started_at)                                                               AS first_attempt,
         (array_agg(COALESCE(c.bde_name, c.bde_extension) ORDER BY c.started_at DESC)
            FILTER (WHERE COALESCE(c.bde_name, c.bde_extension) IS NOT NULL))[1]         AS last_bde,
         (array_agg(cl.pipeline_stage ORDER BY c.started_at DESC)
            FILTER (WHERE cl.pipeline_stage IS NOT NULL))[1]                             AS last_stage,
         (array_agg(cl.prospect_company ORDER BY c.started_at DESC)
            FILTER (WHERE cl.prospect_company IS NOT NULL))[1]                           AS company,
         -- the most recent 'when to call next' hint from the transcript (gatekeeper said
         -- 'next week' / 'in 20 min' / 'after emailing Jarrod', or the DM's availability)
         (array_agg(cl.callback_when ORDER BY c.started_at DESC)
            FILTER (WHERE cl.callback_when IS NOT NULL AND cl.callback_when <> ''))[1]    AS callback_when,
         -- availability window (holiday/leave) from the transcript — floors the next call so we
         -- don't ring into a dead window even when no callback time was given.
         (array_agg(cl.evidence->>'unavailable_until' ORDER BY c.started_at DESC)
            FILTER (WHERE NULLIF(cl.evidence->>'unavailable_until','') IS NOT NULL))[1]   AS unavailable_until,
         count(*)                                                                        AS attempts
  FROM calls c LEFT JOIN classifications cl ON cl.call_id = c.call_id
  WHERE c.in_scope AND lower(c.direction) = 'outbound'
  GROUP BY 1
)
SELECT a.d9, a.dest_number, a.ever_connected, a.ever_booked, a.ever_reached_dm,
       a.last_attempt, a.first_attempt, a.last_bde,
       a.last_stage, a.company, a.callback_when, a.unavailable_until, a.attempts,
       pp.pipeline AS pp_pipeline, pp.assigned_bde, pp.next_action_at, pp.cadence_days,
       pp.business_name AS pp_name,
       -- data provenance: 'master_file' = curated database (higher trust) vs 'call_capture' =
       -- a number the BDE dialled ad-hoc (lower trust, may have data-quality issues).
       (SELECT p.source FROM prospects p WHERE a.d9 = ANY(p.phones_norm) LIMIT 1)         AS source
FROM agg a
LEFT JOIN prospect_pipeline pp ON pp.dest9 = a.d9
WHERE length(a.d9) = 9
  AND COALESCE(pp.dnd, false) = false
  -- NO-REVERT (best-ever stage wins): never re-enter the gatekeeper recall loop for a prospect we
  -- already booked or already reached the decision-maker on — even if their most recent call only
  -- got the gatekeeper. Those are owned by the booking / RPC-callback pipelines, not weekly recalls.
  AND COALESCE(a.ever_booked, false) = false
  AND COALESCE(a.ever_reached_dm, false) = false
  AND (
        (a.ever_connected = false AND a.last_stage IN ('p1','p3'))   -- un-connected, actively pursued
        OR pp.pipeline = 'pipeline2_existing_agency'                 -- existing-agency rotation
      )
"""

# Cancel recalls for prospects we've since connected with / booked / reached the DM on, or that went
# DND. Booking (p5) and reaching the decision-maker (p1) are "best-ever" stages that permanently
# retire the gatekeeper recall — a later gatekeeper-only call must never revive it (no-revert rule).
_CANCEL_RESOLVED = """
UPDATE calendar_events e SET status = 'cancelled'
WHERE e.type = 'recall' AND e.status = 'pending'
  AND right(regexp_replace(COALESCE(e.dest_number,''),'[^0-9]','','g'),9) IN (
        SELECT right(regexp_replace(COALESCE(c.dest_number,''),'[^0-9]','','g'),9)
        FROM calls c JOIN classifications cl ON cl.call_id = c.call_id
        WHERE cl.rpc_connect IS TRUE OR cl.pipeline_stage IN ('p1','p5')
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


def _best_call_hours(pool: ConnectionPool, tz_name: str, fallback: int) -> list[int]:
    """Data-driven best times to call: the business hours (8am-5pm local) with the highest
    historical ANSWER rate across all outbound calls — i.e. when prospects actually pick up.
    Used for the weekly-fallback recall slot (when the transcript gave no callback time), so we
    stop dialling into dead hours and spread recalls across the hours that convert."""
    try:
        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT extract(hour from started_at AT TIME ZONE %s)::int h, "
                "avg(CASE WHEN answered THEN 1.0 ELSE 0.0 END) rate, count(*) n "
                "FROM calls WHERE in_scope AND lower(direction)='outbound' "
                "GROUP BY 1 HAVING count(*) >= 50", (tz_name,))
            rows = cur.fetchall()
        biz = sorted(((r["h"], r["rate"]) for r in rows if 8 <= r["h"] <= 17),
                     key=lambda x: x[1], reverse=True)
        return [h for h, _ in biz[:3]] or [fallback]
    except Exception:
        return [fallback]


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
        # POLICY (user-confirmed 2026-07-08): all data is BDE-sourced, so we never chase a number we
        # didn't reach — cancel ALL pending auto-created RPC retries (they only ever target un-connected
        # numbers). The only scheduled calls are RPC-connect callbacks + the P2 agency rotation. This
        # self-heals any retry created before the policy (e.g. the reported daisypoolcovers case).
        cur.execute("UPDATE calendar_events SET status='cancelled' "
                    "WHERE type='rpc_retry' AND status='pending' AND COALESCE(created_by,'') <> 'user'")
        cancelled += cur.rowcount
        # SUPERSEDED: a later outbound call happened AFTER a scheduled call/callback/retry, so that
        # scheduled call was overtaken — cancel it (stops the prospect page showing a stale PAST
        # 'next call'). Recalls are future-dated so this never touches an active recall.
        cur.execute(
            "UPDATE calendar_events e SET status='cancelled' "
            "WHERE e.status='pending' AND e.type IN ('callback','rpc_retry','recall') "
            "AND EXISTS (SELECT 1 FROM calls c WHERE c.in_scope AND lower(c.direction)='outbound' "
            "  AND right(regexp_replace(COALESCE(c.dest_number,''),'[^0-9]','','g'),9) "
            "    = right(regexp_replace(COALESCE(e.dest_number,''),'[^0-9]','','g'),9) "
            "  AND c.started_at > e.start_at + interval '2 hours')")
        cancelled += cur.rowcount
        # DEDUPE callbacks: each call that requested a callback made its own event — keep only the
        # LATEST pending callback per prospect (cancel the older siblings).
        cur.execute(
            "UPDATE calendar_events e SET status='cancelled' "
            "WHERE e.status='pending' AND e.type='callback' AND EXISTS (SELECT 1 FROM calendar_events e2 "
            "  WHERE e2.status='pending' AND e2.type='callback' AND e2.id <> e.id "
            "  AND right(regexp_replace(COALESCE(e2.dest_number,''),'[^0-9]','','g'),9) "
            "    = right(regexp_replace(COALESCE(e.dest_number,''),'[^0-9]','','g'),9) "
            "  AND (e2.start_at > e.start_at OR (e2.start_at = e.start_at AND e2.id > e.id)))")
        cancelled += cur.rowcount
        conn.commit()
        cur.execute(_GATHER)
        rows = cur.fetchall()

    best_hours = _best_call_hours(pool, getattr(settings, "tz", "Australia/Melbourne"), hour)
    params: list[dict] = []
    skipped = 0
    suppressed_bde_gk = 0
    for i, r in enumerate(rows):
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
        cbw = (r.get("callback_when") or "").strip()
        # INTELLIGENT TIMING: if the transcript gave a 'when to call next' hint (gatekeeper said
        # 'next week' / 'in 20 min' / DM back on a date), schedule to THAT. Weekly is only the
        # fallback when there's no such signal. (Agency prospects rarely give a time -> cadence.)
        best_hour = best_hours[i % len(best_hours)]   # data-driven pick-up hour, spread across top-3
        if not is_agency and cbw:
            when = guess_when(cbw, (r.get("last_attempt") or now).date())
            # the signal usually gives a DAY ('Monday', 'next week') but no time-of-day; when it
            # doesn't, call on that day at the best historical pick-up hour instead of a flat 10am.
            if not any(m in cbw.lower() for m in ("am", "pm", "morning", "afternoon",
                                                  "evening", "noon", "midday", ":")):
                when = when.replace(hour=best_hour, minute=0)
            if when.date() < now.date():          # the stated time already passed -> next slot
                when = _weekly_slot(now, cadence, now, best_hour)
        else:
            when = _weekly_slot(anchor, cadence, now, best_hour if not is_agency else hour)
        # AVAILABILITY FLOOR: if they told us they'll be away (holiday/leave), never call before
        # they're back — push the next call past that window (e.g. a four-week holiday).
        unavail = (r.get("unavailable_until") or "").strip()
        if unavail:
            floor = guess_when(unavail, (r.get("last_attempt") or now).date())
            if floor > when:
                when = floor
        if when > horizon:                        # contract-parked / away too long — schedule later
            skipped += 1
            continue
        who = r.get("company") or r.get("pp_name") or r.get("dest_number") or "prospect"
        # POLICY (user-confirmed 2026-07-08): ALL prospect data is BDE-sourced — the master_file is
        # Raven's BDE-collected list too, not a curated/high-trust DB. So NOTHING is high-trust, and
        # we never chase a weekly gatekeeper (P3) recall on any of it; the only scheduled calls are
        # RPC-connect callbacks (they reached the DM + asked) and the P2 agency contract rotation.
        high_trust = False
        # BDE-SOURCED GATEKEEPER SUPPRESSION (user rule): on BDE-dialled data we do NOT chase a weekly
        # gatekeeper (P3) recall — the number/decision-maker is unverified. We only pursue such a
        # prospect if we actually reached the DM, which fires a separate RPC-connect 'callback' event
        # (kept). Curated master/D&B gatekeeper numbers still get the weekly recall.
        if not is_agency and not high_trust:
            suppressed_bde_gk += 1
            continue
        # WHY THIS TIME (#2): a human, accurate one-liner explaining the chosen slot, shown on the
        # calendar drill-down + prospect card so the reason for the timing is never a mystery.
        when_s = when.strftime("%a %-d %b at %-I:%M %p")
        gave_time = bool(cbw) and any(m in cbw.lower() for m in
                        ("am", "pm", "morning", "afternoon", "evening", "noon", "midday", ":"))
        if is_agency:
            reason = (f"Existing-agency contract rotation — cycle every {cadence} days, so the next "
                      f"touch lands {when_s} (attempt #{(r.get('attempts') or 0) + 1}).")
        elif cbw:
            reason = (f"The last call told us to call ‘{cbw}’, so it’s scheduled for {when_s}"
                      + ("" if gave_time else f" — at {best_hour}:00, our best historical pick-up "
                         "hour, since no exact time was given") + ".")
        else:
            reason = (f"No callback time was given, so it falls to weekly cadence — placed {when_s}, "
                      f"because {best_hour}:00 is when prospects answer most often.")
        if unavail:
            reason = (f"They’re unavailable until ‘{unavail}’, so the next call is pushed past that "
                      f"window. ") + reason
        if is_agency:
            title = f"🔁 Agency call-cycle: {who}"
            notes = (f"⏰ Why this time: {reason}  ·  Existing-agency rotation, assigned to {bde}.")
        else:
            title = f"🔷 🔁 Recall: {who}"
            notes = (f"⏰ Why this time: {reason}  ·  Recall — not yet connected (gatekeeper callback, "
                     f"🔷 database-sourced / high-trust). Attempt #{(r.get('attempts') or 0) + 1}. "
                     f"Keep calling until we speak to them (auto-cancels once connected, booked, or "
                     f"marked DND).")
        params.append({"bde": bde, "title": title, "start": when,
                       "end": when + timedelta(minutes=30), "notes": notes,
                       "dest": r.get("dest_number")})

    scheduled = 0
    if params:
        with pool.connection() as conn, conn.cursor() as cur:   # ONE connection, batched upsert
            cur.executemany(_UPSERT, params)
            conn.commit()
            scheduled = len(params)

    out = {"scheduled": scheduled, "cancelled": cancelled, "skipped": skipped,
           "suppressed_bde_gk": suppressed_bde_gk, "candidates": len(rows)}
    log.info("weekly_recalls_synced", **out)
    return out
