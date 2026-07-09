"""Smart callback / next-call priority — a read-only "who do I ring next?" engine.

Ranks every ACTIONABLE prospect's NEXT call by blending three 0..100 signals:

    score = INTENT (0.5) x ATTENTION (0.3) x REVENUE (0.2)      (weights configurable)

  * INTENT     — how badly the prospect wants us to call: lead temperature, an
                 explicit callback request, an OPEN RPC "next move" required action,
                 and the pipeline stage (p1 interested / p3 gatekeeper callback).
                 Intent-led, so it carries the biggest weight.
  * ATTENTION  — how overdue the call is: grows with time since the last dial
                 (a just-dialled number needs no attention yet; one that has gone
                 quiet for a fortnight does).
  * REVENUE    — deal size: the company's revenue (companies.revenue_musd) on a
                 diminishing curve against a configurable cap.

The recompute mirrors `prospects.sync_prospect_pipelines` EXACTLY: aggregate the
call signals by dialled number FIRST (callagg), explode each prospect's phone list
(unnest) and hash-JOIN on the number — never a per-row `= ANY(phones_norm)` nested
loop — read `calls`/`classifications`/`companies`/`rpc_actions` READ-ONLY, gate on
`min_interval_minutes`, and log stats. The blend itself is pure Python (the
`_*_score` / `blend` helpers) so the numeric scoring is testable in isolation.

The ONLY tables this module writes are `next_call_queue` (its own worklist) and,
through `calendar.py`, `calendar_events` (moving/reassigning a linked event). It
never reads or writes any booking / qualification predicate.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from psycopg.types.json import Json
from psycopg_pool import ConnectionPool

from .calendar import guess_when, update_event
from .config import Settings
from .db import fetch_all
from .logging import get_logger

log = get_logger(__name__)

# --------------------------------------------------------------------------- #
# Tunables that aren't (yet) worth a Settings knob — documented assumptions.
# --------------------------------------------------------------------------- #
_BUSINESS_OPEN_HOUR = 9      # next_best_time() snaps calls into 09:00–17:00 local
_BUSINESS_CLOSE_HOUR = 17
# ATTENTION reaches 100 once a prospect has gone this many days without a dial.
_ATTENTION_OVERDUE_DAYS = 14
# "recently-called-not-booked" stays actionable for this many days after the last
# dial (so a called-but-unresolved prospect keeps surfacing without a callback).
_RECENTLY_CALLED_DAYS = 45

# Intent base by lead temperature (classifications.lead_temperature).
_TEMP_INTENT = {"super_hot": 90.0, "hot": 75.0, "warm": 55.0, "none": 25.0}
# Intent floor by materialized pipeline stage (prospects.pipeline_stage).
_STAGE_INTENT = {"p1": 70.0, "p3": 45.0, "p2": 35.0, "p4": 20.0, "p5": 0.0}


# --------------------------------------------------------------------------- #
# Pure scoring helpers (no DB) — each returns a 0..100 float.
# --------------------------------------------------------------------------- #
def _revenue_score(revenue_musd: float | None, cap: float) -> float:
    """Deal-size signal: revenue (millions USD) on a sqrt (diminishing) curve vs `cap`.

    A $100M account shouldn't dwarf a $25M one by 4x — the sqrt keeps the spread
    sane. Unknown / non-positive revenue scores 0 (revenue only ever *adds* signal)."""
    if not revenue_musd or revenue_musd <= 0 or not cap or cap <= 0:
        return 0.0
    frac = min(float(revenue_musd), float(cap)) / float(cap)
    return round(100.0 * math.sqrt(frac), 1)


def _attention_score(
    last_called_at: datetime | None,
    overdue_days: float = _ATTENTION_OVERDUE_DAYS,
    *,
    now: datetime | None = None,
) -> float:
    """How overdue the next touch is: 0 right after a dial, ramping to 100 once the
    number has sat untouched for `overdue_days`. A never-dialled but actionable
    prospect is treated as already waiting on us (70)."""
    now = now or datetime.now(timezone.utc)
    if last_called_at is None:
        return 70.0
    if last_called_at.tzinfo is None:
        last_called_at = last_called_at.replace(tzinfo=timezone.utc)
    days = max((now - last_called_at).total_seconds() / 86400.0, 0.0)
    if not overdue_days or overdue_days <= 0:
        overdue_days = _ATTENTION_OVERDUE_DAYS
    return round(min(days / overdue_days, 1.0) * 100.0, 1)


def _intent_score(
    lead_temperature: str | None,
    callback_requested: bool,
    has_open_rpc_action: bool,
    pipeline_stage: str | None,
) -> float:
    """Buyer-intent signal (the heaviest weight). Take the stronger of the lead
    temperature vs the pipeline stage as a base, then add explicit-intent bumps for
    a requested callback and an OPEN RPC required action, clamped to 100."""
    temp = (lead_temperature or "none").strip().lower()
    base = _TEMP_INTENT.get(temp, 25.0)
    stage_base = _STAGE_INTENT.get((pipeline_stage or "").strip().lower(), 20.0)
    score = max(base, stage_base)
    if callback_requested:
        score += 20.0
    if has_open_rpc_action:
        score += 15.0
    return round(min(max(score, 0.0), 100.0), 1)


def blend(
    intent: float,
    attention: float,
    revenue: float,
    weights: dict,
    *,
    revenue_musd: float | None = None,
    extra_drivers: list[str] | None = None,
) -> tuple[float, str, dict]:
    """Combine the three 0..100 component scores into (score, tier, reason).

    `weights` carries BOTH the blend weights (`intent`/`attention`/`revenue`, from
    settings) and the tier cutoffs (`tier_hot`/`tier_warm`). Weights are normalised
    so the blended score stays on 0..100 even if they don't sum to 1.

    tier = hot (>= tier_hot) / warm (>= tier_warm) / cool (below). The reason dict is
    `{drivers:[human strings], revenue_musd, intent, attention, revenue}` — the caller
    passes signal-specific `extra_drivers` (e.g. "Callback requested") which lead the
    list, followed by generic numeric drivers.
    """
    wi = float(weights.get("intent", 0.5))
    wa = float(weights.get("attention", 0.3))
    wr = float(weights.get("revenue", 0.2))
    wsum = wi + wa + wr or 1.0
    score = round((intent * wi + attention * wa + revenue * wr) / wsum, 1)

    hot = float(weights.get("tier_hot", 70))
    warm = float(weights.get("tier_warm", 40))
    tier = "hot" if score >= hot else ("warm" if score >= warm else "cool")

    drivers: list[str] = list(extra_drivers or [])
    if intent >= 70:
        drivers.append("High buyer intent")
    if attention >= 70:
        drivers.append("Overdue for a call")
    if revenue >= 60 and revenue_musd:
        drivers.append(f"High-revenue account (~${revenue_musd:.0f}M)")
    reason = {
        "drivers": drivers,
        "revenue_musd": float(revenue_musd) if revenue_musd else None,
        "intent": intent,
        "attention": attention,
        "revenue": revenue,
    }
    return score, tier, reason


def _weights_from_settings(settings: Settings) -> dict:
    """Pack the blend weights + tier cutoffs from Settings (with safe defaults so the
    module works even before the config knobs are added)."""
    return {
        "intent": getattr(settings, "next_call_w_intent", 0.5),
        "attention": getattr(settings, "next_call_w_attention", 0.3),
        "revenue": getattr(settings, "next_call_w_revenue", 0.2),
        "tier_hot": getattr(settings, "next_call_tier_hot", 70),
        "tier_warm": getattr(settings, "next_call_tier_warm", 40),
    }


# --------------------------------------------------------------------------- #
# Business-hours snap
# --------------------------------------------------------------------------- #
def next_best_time(base_dt: datetime | None, *, tz: str) -> datetime:
    """Snap `base_dt` to the next real calling slot: inside 09:00–17:00 local, never
    in the past, weekends rolled to Monday. Returns a tz-aware datetime.

    Mirrors calendar.guess_when's weekend/hour semantics but works from a concrete
    datetime instead of free text (feed guess_when's output in for callback phrases)."""
    zone = ZoneInfo(tz) if isinstance(tz, str) else tz
    now_local = datetime.now(zone)
    if base_dt is None:
        base_dt = now_local
    if base_dt.tzinfo is None:
        base_dt = base_dt.replace(tzinfo=zone)
    local = base_dt.astimezone(zone)
    if local < now_local:  # never schedule a call in the past
        local = now_local
    # Snap into business hours.
    if local.hour < _BUSINESS_OPEN_HOUR:
        local = local.replace(hour=_BUSINESS_OPEN_HOUR, minute=0, second=0, microsecond=0)
    elif local.hour >= _BUSINESS_CLOSE_HOUR:
        local = (local + timedelta(days=1)).replace(
            hour=_BUSINESS_OPEN_HOUR, minute=0, second=0, microsecond=0)
    else:
        local = local.replace(second=0, microsecond=0)
    # Skip weekends -> Monday morning.
    while local.weekday() >= 5:
        local = (local + timedelta(days=1)).replace(
            hour=_BUSINESS_OPEN_HOUR, minute=0, second=0, microsecond=0)
    return local


# --------------------------------------------------------------------------- #
# Set-based recompute
# --------------------------------------------------------------------------- #
# One heavy read: gather, per ACTIONABLE prospect, the raw signals the Python blend
# needs. Same shape as sync_prospect_pipelines — aggregate call/rpc signals by the
# dialled number FIRST, then explode prospect phones (prphones) and hash-JOIN on the
# number, plus a domain-level company_key rollup for signals attached to a sibling
# contact. The materialized prospects.last_called_at / pipeline_stage / p4_subpipeline
# (written by sync_prospect_pipelines just before this in the refresh) are read directly.
_GATHER_SQL = """
WITH callagg AS (
  SELECT right(regexp_replace(c.dest_number,'[^0-9]','','g'),9)  AS dest9,
         max(c.started_at)                                       AS last_at,
         bool_or(COALESCE(cl.callback_requested,false))          AS cb_req,
         (array_agg(cl.callback_when ORDER BY c.started_at DESC)
            FILTER (WHERE cl.callback_when IS NOT NULL AND cl.callback_when <> ''))[1] AS cb_when,
         (array_agg(cl.lead_temperature ORDER BY c.started_at DESC)
            FILTER (WHERE cl.lead_temperature IS NOT NULL AND cl.lead_temperature <> 'none'))[1] AS lead_temp,
         bool_or(COALESCE(cl.do_not_contact,false))              AS dnc
  FROM calls c LEFT JOIN classifications cl ON cl.call_id = c.call_id
  WHERE c.in_scope AND c.dest_number <> ''
    AND length(right(regexp_replace(c.dest_number,'[^0-9]','','g'),9)) >= 8
  GROUP BY 1
),
prphones AS (
  SELECT pr.id, ph AS phone FROM prospects pr, unnest(pr.phones_norm) AS ph
),
callroll AS (
  SELECT p.id,
         bool_or(ca.cb_req)                                                  AS cb_req,
         (array_agg(ca.cb_when   ORDER BY ca.last_at DESC) FILTER (WHERE ca.cb_when   IS NOT NULL))[1] AS cb_when,
         (array_agg(ca.lead_temp ORDER BY ca.last_at DESC) FILTER (WHERE ca.lead_temp IS NOT NULL))[1] AS lead_temp,
         bool_or(ca.dnc)                                                     AS dnc,
         (array_agg(ca.dest9 ORDER BY ca.last_at DESC))[1]                   AS dest9
  FROM callagg ca JOIN prphones p ON p.phone = ca.dest9
  GROUP BY p.id
),
compkey AS (
  SELECT pr.id,
         bool_or(COALESCE(cl.callback_requested,false))          AS cb_req,
         (array_agg(cl.callback_when ORDER BY cl.classified_at DESC NULLS LAST)
            FILTER (WHERE cl.callback_when IS NOT NULL AND cl.callback_when <> ''))[1] AS cb_when,
         (array_agg(cl.lead_temperature ORDER BY cl.classified_at DESC NULLS LAST)
            FILTER (WHERE cl.lead_temperature IS NOT NULL AND cl.lead_temperature <> 'none'))[1] AS lead_temp
  FROM prospects pr
  JOIN classifications cl ON pr.domain IS NOT NULL AND cl.company_key = 'dom:' || pr.domain
  GROUP BY pr.id
),
rpcagg AS (
  SELECT p.id,
         true                                                                AS has_open_rpc,
         (array_agg(ra.required_action ORDER BY ra.last_attempt_at DESC NULLS LAST))[1] AS rpc_action_text,
         max(ra.last_attempt_at)                                             AS rpc_last_at,
         (array_agg(ra.dest9 ORDER BY ra.last_attempt_at DESC NULLS LAST))[1] AS dest9
  FROM rpc_actions ra JOIN prphones p ON p.phone = ra.dest9
  WHERE ra.status = 'open'
  GROUP BY p.id
),
rev AS (
  SELECT pr.id, max(co.revenue_musd) AS revenue_musd
  FROM prospects pr JOIN companies co ON co.domain = pr.domain
  WHERE pr.domain IS NOT NULL AND co.revenue_musd IS NOT NULL
  GROUP BY pr.id
),
evt AS (
  -- Link the queue row to an existing pending callback/rpc_retry event so a reassign
  -- or reschedule can move the real calendar entry. Small set -> plain hash join.
  SELECT p.id, (array_agg(e.id ORDER BY e.start_at DESC))[1] AS event_id
  FROM prphones p
  JOIN calendar_events e
    ON right(regexp_replace(e.dest_number,'[^0-9]','','g'),9) = p.phone
   AND e.type IN ('callback','rpc_retry') AND e.status = 'pending'
  GROUP BY p.id
)
SELECT base.id AS prospect_id, base.domain, base.business_name, base.assigned_bde,
       base.last_called_at, base.pipeline_stage, base.p4_subpipeline,
       (COALESCE(cr.cb_req,false) OR COALESCE(ck.cb_req,false))            AS callback_requested,
       COALESCE(cr.cb_when, ck.cb_when)                                    AS callback_when,
       COALESCE(cr.lead_temp, ck.lead_temp)                               AS lead_temperature,
       COALESCE(rp.has_open_rpc,false)                                    AS has_open_rpc,
       rp.rpc_action_text, rp.rpc_last_at,
       COALESCE(cr.dnc,false)                                             AS dnc,
       COALESCE(rp.dest9, cr.dest9, base.phones_norm[1])                  AS dest9,
       rev.revenue_musd, evt.event_id
FROM prospects base
LEFT JOIN callroll cr ON cr.id = base.id
LEFT JOIN compkey  ck ON ck.id = base.id
LEFT JOIN rpcagg   rp ON rp.id = base.id
LEFT JOIN rev         ON rev.id = base.id
LEFT JOIN evt         ON evt.id = base.id
WHERE base.pipeline_stage IS DISTINCT FROM 'p5'          -- not booked
  AND COALESCE(base.p4_subpipeline,'') <> 'dead'         -- not DND / dead
  AND NOT COALESCE(cr.dnc,false)
  AND (                                                  -- has at least one signal
        COALESCE(cr.cb_req,false) OR COALESCE(ck.cb_req,false)
     OR COALESCE(rp.has_open_rpc,false)
     OR base.pipeline_stage IN ('p1','p3')
     OR (base.last_called_at IS NOT NULL
         AND base.last_called_at >= now() - make_interval(days => %(recent_days)s))
  )
"""

_UPSERT_SQL = """
INSERT INTO next_call_queue (
  prospect_id, dest9, domain, business_name, score, tier, revenue_musd,
  rev_score, att_score, int_score, source_signal, reason, next_best_time,
  assigned_bde, linked_event_id, status, synced_at)
VALUES (
  %(prospect_id)s, %(dest9)s, %(domain)s, %(business_name)s, %(score)s, %(tier)s, %(revenue_musd)s,
  %(rev_score)s, %(att_score)s, %(int_score)s, %(source_signal)s, %(reason)s, %(next_best_time)s,
  %(assigned_bde)s, %(linked_event_id)s, 'open', now())
ON CONFLICT (prospect_id) DO UPDATE SET
  dest9 = EXCLUDED.dest9, domain = EXCLUDED.domain, business_name = EXCLUDED.business_name,
  score = EXCLUDED.score, tier = EXCLUDED.tier, revenue_musd = EXCLUDED.revenue_musd,
  rev_score = EXCLUDED.rev_score, att_score = EXCLUDED.att_score, int_score = EXCLUDED.int_score,
  source_signal = EXCLUDED.source_signal, reason = EXCLUDED.reason,
  linked_event_id = COALESCE(EXCLUDED.linked_event_id, next_call_queue.linked_event_id),
  -- Preserve a manual reassignment / reschedule (override_by set) across re-syncs;
  -- otherwise refresh the owner + suggested time from the fresh signals.
  assigned_bde   = CASE WHEN next_call_queue.override_by IS NOT NULL
                        THEN next_call_queue.assigned_bde   ELSE EXCLUDED.assigned_bde END,
  next_best_time = CASE WHEN next_call_queue.override_by IS NOT NULL
                        THEN next_call_queue.next_best_time ELSE EXCLUDED.next_best_time END,
  -- A call the BDE has marked done stays done until its signal clears (it then drops
  -- out of the actionable set and is pruned); everything else is (re)opened.
  status = CASE WHEN next_call_queue.status = 'done' THEN 'done' ELSE 'open' END,
  synced_at = now()
"""


def sync_next_call_scores(pool: ConnectionPool, settings: Settings,
                          min_interval_minutes: int = 0) -> dict:
    """Recompute the `next_call_queue` from scratch (idempotent). Mirrors
    `sync_prospect_pipelines`: gate on freshness, one set-based read of the signals
    (callagg-first + unnest/equijoin, no nested phone loops), score in Python, then a
    single batched upsert + prune of rows that are no longer actionable.

    Read-only on calls/classifications/companies/rpc_actions; writes ONLY
    next_call_queue. Returns {prospects, hot, warm, cool, pruned}.
    """
    if min_interval_minutes > 0:
        row = fetch_all(
            pool,
            "SELECT max(synced_at) AS last FROM next_call_queue WHERE synced_at IS NOT NULL",
        )
        last = row[0]["last"] if row else None
        if last is not None:
            age_min = (datetime.now(timezone.utc) - last).total_seconds() / 60.0
            if age_min < min_interval_minutes:
                return {"skipped": True, "age_min": round(age_min, 1)}

    tz = settings.tz
    weights = _weights_from_settings(settings)
    cap = float(getattr(settings, "next_call_revenue_cap_musd", 100.0))
    today = datetime.now(ZoneInfo(tz)).date()

    rows = fetch_all(pool, _GATHER_SQL, {"recent_days": _RECENTLY_CALLED_DAYS})

    params: list[dict] = []
    tier_counts = {"hot": 0, "warm": 0, "cool": 0}
    for r in rows:
        intent = _intent_score(
            r["lead_temperature"], r["callback_requested"],
            r["has_open_rpc"], r["pipeline_stage"])
        attention = _attention_score(r["last_called_at"])
        revenue = _revenue_score(r["revenue_musd"], cap)

        # Signal-specific human drivers + which signal made this actionable.
        drivers: list[str] = []
        source_signal = "recent_call"
        if r["callback_requested"]:
            source_signal = "callback"
            when = f" ({r['callback_when']})" if r.get("callback_when") else ""
            drivers.append(f"Callback requested{when}")
        elif r["has_open_rpc"]:
            source_signal = "rpc_action"
            drivers.append(f"Open next move: {r['rpc_action_text'] or 'follow up the last dial'}")
        elif r["pipeline_stage"] == "p1":
            source_signal = "pipeline_p1"
            drivers.append("Interested prospect (Pipeline 1)")
        elif r["pipeline_stage"] == "p3":
            source_signal = "pipeline_p3"
            drivers.append("Gatekeeper callback (Pipeline 3)")
        if r["has_open_rpc"] and source_signal != "rpc_action":
            drivers.append(f"Open next move: {r['rpc_action_text'] or 'follow up the last dial'}")
        temp = (r["lead_temperature"] or "").lower()
        if temp in ("hot", "super_hot", "warm"):
            drivers.append(f"{temp.replace('_', ' ')} lead")

        score, tier, reason = blend(
            intent, attention, revenue, weights,
            revenue_musd=r["revenue_musd"], extra_drivers=drivers)
        tier_counts[tier] += 1

        # Suggested next slot: from the callback phrase when present, else "now".
        if r.get("callback_when"):
            base = guess_when(r["callback_when"], today)
        else:
            base = datetime.now(ZoneInfo(tz))
        nbt = next_best_time(base, tz=tz)

        params.append({
            "prospect_id": r["prospect_id"],
            "dest9": r["dest9"],
            "domain": r["domain"],
            "business_name": r["business_name"],
            "score": score,
            "tier": tier,
            "revenue_musd": r["revenue_musd"],
            "rev_score": revenue,
            "att_score": attention,
            "int_score": intent,
            "source_signal": source_signal,
            "reason": Json(reason),
            "next_best_time": nbt,
            "assigned_bde": r["assigned_bde"],
            "linked_event_id": r["event_id"],
        })

    fresh_ids = [p["prospect_id"] for p in params]
    with pool.connection() as conn, conn.cursor() as cur:
        if params:
            cur.executemany(_UPSERT_SQL, params)
        # Prune rows that are no longer actionable (booked, DND, signal cleared).
        if fresh_ids:
            cur.execute(
                "DELETE FROM next_call_queue WHERE prospect_id <> ALL(%s)", (fresh_ids,))
        else:
            cur.execute("DELETE FROM next_call_queue")
        pruned = cur.rowcount
        conn.commit()

    stats = {"prospects": len(params), "pruned": pruned, **tier_counts}
    log.info("sync_next_call_scores", **stats)
    return stats


# --------------------------------------------------------------------------- #
# Read + mutate helpers for the API
# --------------------------------------------------------------------------- #
# GAds scope for the next-call queue: the prospect's dialled number matches a confirmed-Google-Ads
# business phone, OR its domain is a GAds domain. companies.phone_norm is already last-9-digits and
# indexed (idx_companies_phone) -> match it directly, no regexp.
_NC_GADS_FILTER = (
    "(q.dest9 IN (SELECT co.phone_norm FROM enrichment ge JOIN companies co ON co.domain=ge.domain "
    "   WHERE (ge.dataforseo->>'running_google_ads')='true' AND COALESCE(co.phone_norm,'') <> '') "
    " OR COALESCE(q.domain, p.domain) IN "
    "   (SELECT domain FROM enrichment WHERE (dataforseo->>'running_google_ads')='true'))")


def _nc_where(bde, tier, due_only, gads_only, params):
    """Shared WHERE for the next-call list + summary so both count the SAME set."""
    where = ["q.status = 'open'"]
    if bde:
        where.append("q.assigned_bde = %(bde)s")
        params["bde"] = bde
    if tier:
        where.append("q.tier = %(tier)s")
        params["tier"] = tier
    if due_only:
        where.append("(q.next_best_time IS NULL OR q.next_best_time <= now())")
    if gads_only:
        where.append(_NC_GADS_FILTER)
    return where


def list_next_calls(pool: ConnectionPool, *, bde: str | None = None,
                    due_only: bool = False, tier: str | None = None,
                    gads_only: bool = False, limit: int = 200, offset: int = 0) -> list[dict]:
    """The ranked next-call worklist for the API. Joins `prospects` for the live
    business_name/domain and orders by score DESC. `due_only` keeps rows whose
    suggested time has arrived; `bde`/`tier` narrow the list. `gads_only` scopes to the
    confirmed-Google-Ads pool (matched by phone or domain). `offset` paginates."""
    params: dict = {"limit": limit, "offset": offset}
    where = _nc_where(bde, tier, due_only, gads_only, params)
    return fetch_all(
        pool,
        f"""
        SELECT q.prospect_id, q.dest9, q.score, q.tier, q.revenue_musd,
               q.rev_score, q.att_score, q.int_score, q.source_signal, q.reason,
               q.next_best_time, q.assigned_bde, q.override_by, q.linked_event_id,
               q.status, q.synced_at,
               COALESCE(q.business_name, p.business_name) AS business_name,
               COALESCE(q.domain, p.domain)               AS domain,
               -- Best-ever pipeline stage for this prospect (same precedence the whole app uses:
               -- p5>p2>p1>p3, else p4 fresh) + the last BDE who actually called them (the natural
               -- owner when nobody has been explicitly assigned). Matched by the indexed company_key.
               COALESCE(ps.stage, 'p4')                   AS pipeline_stage,
               ps.last_bde                                AS last_bde,
               -- The BDE who will actually make the NEXT call: the owner of the scheduled calendar
               -- event if one is linked, else the queue assignment, else the last BDE who called.
               COALESCE(ce.bde_name, q.assigned_bde, ps.last_bde) AS next_call_bde,
               (ce.id IS NOT NULL)                        AS next_call_scheduled
        FROM next_call_queue q
        JOIN prospects p ON p.id = q.prospect_id
        LEFT JOIN calendar_events ce ON ce.id = q.linked_event_id AND ce.status = 'pending'
        LEFT JOIN LATERAL (
          SELECT (CASE WHEN bool_or(cl.pipeline_stage='p5') THEN 'p5'
                       WHEN bool_or(cl.pipeline='pipeline2_existing_agency') THEN 'p2'
                       WHEN bool_or(cl.pipeline_stage='p1') THEN 'p1'
                       WHEN bool_or(cl.pipeline_stage='p3') THEN 'p3'
                       WHEN count(*) > 0 THEN 'p4'
                       ELSE NULL END) AS stage,
                 (array_agg(COALESCE(c.bde_name, c.bde_extension) ORDER BY c.started_at DESC)
                    FILTER (WHERE COALESCE(c.bde_name, c.bde_extension) IS NOT NULL))[1] AS last_bde
          FROM classifications cl JOIN calls c ON c.call_id = cl.call_id
          WHERE c.in_scope AND cl.company_key = 'dom:' || COALESCE(q.domain, p.domain)
        ) ps ON COALESCE(q.domain, p.domain) IS NOT NULL AND COALESCE(q.domain, p.domain) <> ''
        WHERE {' AND '.join(where)}
        ORDER BY q.score DESC NULLS LAST, q.next_best_time NULLS LAST
        LIMIT %(limit)s
        """,
        params,
    )


def reassign_next_call(pool: ConnectionPool, key: int, bde: str, *,
                       by: str, notes: str | None = None) -> bool:
    """Reassign a prospect's next call to another BDE (key = prospects.id).

    Sets next_call_queue.assigned_bde + override_by (so the next sync won't revert the
    owner). `calendar.update_event` CANNOT change bde_name, so the linked calendar
    event's owner is moved with a DIRECT UPDATE here (notes appended when given).
    Returns True when the queue row existed."""
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE next_call_queue SET assigned_bde=%s, override_by=%s, synced_at=now() "
            "WHERE prospect_id=%s RETURNING linked_event_id",
            (bde, by, key),
        )
        row = cur.fetchone()
        if row is None:
            conn.commit()
            return False
        eid = row["linked_event_id"]
        if eid:
            if notes:
                cur.execute(
                    "UPDATE calendar_events SET bde_name=%s, "
                    "notes = COALESCE(notes,'') || %s WHERE id=%s",
                    (bde, f"\n[reassigned by {by}] {notes}", eid),
                )
            else:
                cur.execute("UPDATE calendar_events SET bde_name=%s WHERE id=%s", (bde, eid))
        conn.commit()
    return True


def set_next_best_time(pool: ConnectionPool, key: int, start_at: datetime) -> int | None:
    """Pin the suggested next-call time (key = prospects.id). Marks the row overridden
    so a re-sync won't move it, and reschedules the linked calendar event via
    `calendar.update_event` (which DOES support start_at). Returns the linked event id
    that was moved, or None if the key was unknown / had no linked event."""
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE next_call_queue SET next_best_time=%s, "
            "override_by=COALESCE(override_by,'reschedule'), synced_at=now() "
            "WHERE prospect_id=%s RETURNING linked_event_id",
            (start_at, key),
        )
        row = cur.fetchone()
        conn.commit()
    if row is None:
        return None
    eid = row["linked_event_id"]
    if eid:
        update_event(pool, eid, {"start_at": start_at, "end_at": start_at + timedelta(minutes=30)})
    return eid


def mark_done(pool: ConnectionPool, key: int) -> bool:
    """Mark a prospect's next call handled (key = prospects.id). The row stays 'done'
    across re-syncs until its underlying signal clears (then it's pruned). Also marks
    the linked calendar event done. Returns True when the queue row existed."""
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE next_call_queue SET status='done', synced_at=now() "
            "WHERE prospect_id=%s RETURNING linked_event_id",
            (key,),
        )
        row = cur.fetchone()
        conn.commit()
    if row is None:
        return False
    if row["linked_event_id"]:
        update_event(pool, row["linked_event_id"], {"status": "done"})
    return True
