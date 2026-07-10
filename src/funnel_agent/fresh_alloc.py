"""Fresh Google-Ads calling calendar — AI daily allocation.

The ONLY cold pool that goes on the calendar: prospects CONFIRMED running Google Ads (Transparency
Center). Every other calendar event is relationship-driven (RPC-connect callbacks, P2 agency rotation);
this is the one place a never-dialled prospect is scheduled.

Model: each active BDE holds a curated *rolling worklist* filled to `bde_daily_call_target`
PENDING `fresh_call` events — their high-value fresh slate — replenished each cycle as they dial through
it. Allocation is **value × performance matched**: the highest-revenue fresh prospects are routed to the
highest-performing BDEs (90-day booking / RPC-connect / full-pitch rates), while every BDE is filled to
the cap so nobody is starved. Idempotent: a per-number partial-unique index + "resolve once dialled"
pass keep re-runs stable.

DataForSEO membership (running_google_ads) is populated only on Railway, so this is a no-op locally.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from psycopg_pool import ConnectionPool

from .config import Settings
from .logging import get_logger
from .next_call import _revenue_score, next_best_time
from .pipeline2 import calling_bdes
from .prospects import gads_dnb_gate

log = get_logger(__name__)

_D9 = "right(regexp_replace(COALESCE({col},''),'[^0-9]','','g'),9)"
# canonical number identity used everywhere in the fresh path (index, ON CONFLICT, exclusions).
_D9_DEST = _D9.format(col="dest_number")

# One open fresh_call per physical NUMBER (last-9-digits, not the raw string — same number in two phone
# formats must collide). Mirrors idx_calendar_recall; also in schema.sql, ensured here so the allocator
# is self-sufficient on a fresh DB.
_ENSURE_INDEX = (
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_calendar_fresh_call "
    f"ON calendar_events ({_D9_DEST}) WHERE type = 'fresh_call' AND status = 'pending'"
)


def _bde_perf_90d(pool: ConnectionPool, shrink_k: int) -> tuple[dict[str, float], float]:
    """90-day per-BDE performance score (0..100) = blended booking / RPC-connect / full-pitch rate.

    Rates are computed from the SAME daily_funnel columns as /api/leaderboard (booking-dedup already
    baked into meetings_booked), then shrunk toward the pooled team rate by connect volume so a BDE
    with a handful of connects can't top the ranking on noise. Returns ({bde: score}, team_mean)."""
    rows = _fetch(pool,
        "SELECT bde_name, COALESCE(SUM(connected),0) c, COALESCE(SUM(rpc_connect),0) r, "
        "  COALESCE(SUM(full_pitch),0) fp, COALESCE(SUM(meetings_booked),0) mb "
        "FROM daily_funnel WHERE track='combined' AND bde_name<>'ALL' "
        "  AND report_date >= (CURRENT_DATE - 90) GROUP BY bde_name")
    tc = sum(r["c"] for r in rows) or 0
    tr = sum(r["r"] for r in rows) or 0
    tfp = sum(r["fp"] for r in rows) or 0
    tmb = sum(r["mb"] for r in rows) or 0

    def raw(c, r, fp, mb):
        conv_rpc = r / c if c else 0.0            # connect → decision-maker
        conv_pitch = fp / r if r else 0.0         # DM → full pitch
        conv_booked = mb / fp if fp else 0.0      # pitch → booking
        return 0.5 * conv_booked + 0.3 * conv_rpc + 0.2 * conv_pitch

    team_raw = raw(tc, tr, tfp, tmb)
    scores: dict[str, float] = {}
    for r in rows:
        c = r["c"]
        # empirical-Bayes shrink toward the team rate by connect volume
        w = c / (c + shrink_k) if (c + shrink_k) else 0.0
        s = team_raw + (raw(c, r["r"], r["fp"], r["mb"]) - team_raw) * w
        scores[r["bde_name"]] = round(100.0 * s, 2)
    return scores, round(100.0 * team_raw, 2)


def _fresh_candidates(pool: ConnectionPool, limit: int) -> list[dict]:
    """Callable, never-dialled, CONFIRMED-running-ads prospects with no competing pending call, ranked
    by value (SMB $1-50M first, biggest revenue first). Excludes DND and any number already on a
    pending calendar event. Never-dialled ⇒ excludes the whole agency/RPC-connected set (that pool is
    calls-derived) and every already-worked number."""
    d9_calls = _D9.format(col="c.dest_number")
    d9_ce = _D9.format(col="ce.dest_number")
    d9_pp = _D9.format(col="pp.dest9")  # prospect_pipeline.dest9 is already 9-digit; regexp is a no-op
    # FULLY-ENRICHED gate (user rule): a BDE only calls confirmed-ads prospects whose enrichment is
    # complete — website + Apollo + business intel present (DataForSEO is implied by pool membership).
    sql = f"""
    WITH ads AS (SELECT e.domain FROM enrichment e
                 WHERE (e.dataforseo->>'running_google_ads')='true'
                   {gads_dnb_gate('e')}
                   AND e.website IS NOT NULL AND e.apollo IS NOT NULL AND e.business_intel IS NOT NULL),
    picked AS (
      SELECT DISTINCT ON (a.domain) a.domain,
        co.company_name AS business_name,
        COALESCE(NULLIF(co.phone,''), co.phone_norm) AS dest_number,
        co.industry, co.revenue_musd
      FROM ads a LEFT JOIN companies co ON co.domain=a.domain
      ORDER BY a.domain, (NULLIF(co.phone,'') IS NOT NULL) DESC, co.revenue_musd DESC NULLS LAST
    ),
    cand AS (
      SELECT p.*, {_D9.format(col="p.dest_number")} AS d9 FROM picked p WHERE p.dest_number IS NOT NULL
    ),
    -- collapse to ONE row per physical number (last-9-digits): two confirmed-ads domains that share a
    -- phone must not both schedule a fresh_call. Keep the highest-value row per number.
    dedup AS (
      SELECT DISTINCT ON (c.d9) c.domain, c.business_name, c.dest_number, c.industry, c.revenue_musd
      FROM cand c
      WHERE c.d9 <> ''
        AND NOT EXISTS (SELECT 1 FROM calls cc WHERE {_D9.format(col="cc.dest_number")} = c.d9 AND cc.in_scope)
        AND NOT EXISTS (SELECT 1 FROM calendar_events ce
                         WHERE ce.status='pending' AND {d9_ce} = c.d9)
        AND NOT EXISTS (SELECT 1 FROM prospect_pipeline pp
                         WHERE {d9_pp} = c.d9 AND COALESCE(pp.dnd,false))
      ORDER BY c.d9, CASE WHEN c.revenue_musd BETWEEN 1 AND 50 THEN 0
                          WHEN c.revenue_musd IS NULL THEN 1 ELSE 2 END,
               c.revenue_musd DESC NULLS LAST
    )
    SELECT domain, business_name, dest_number, industry, revenue_musd
    FROM dedup
    ORDER BY CASE WHEN revenue_musd BETWEEN 1 AND 50 THEN 0
                  WHEN revenue_musd IS NULL THEN 1 ELSE 2 END,
             revenue_musd DESC NULLS LAST
    LIMIT %s
    """
    return _fetch(pool, sql, (limit,))


def schedule_fresh_calls(pool: ConnectionPool, settings: Settings) -> dict:
    """Fill every active BDE's daily calling worklist to the target (default 200 calls), value×
    performance matched. The target is the WHOLE worklist = due follow-ups (callbacks/recalls already
    scheduled for that BDE) + fresh; this tops up the remainder with fresh confirmed-Google-Ads,
    fully-enriched prospects so the BDE always has ~200 calls to make.

    Each run: (1) resolve pending fresh_calls whose number has since been dialled, (2) compute each
    BDE's remaining need = target − (their pending callbacks + recalls + fresh calls), (3) route the
    highest-value fresh prospects to the highest-performing BDEs, (4) write `fresh_call` events. Idempotent."""
    if not getattr(settings, "fresh_alloc_enabled", True):
        return {"skipped": "disabled"}
    target = int(getattr(settings, "bde_daily_call_target", 200) or 0)
    if target <= 0:
        return {"skipped": "target=0"}
    tz = getattr(settings, "tz", "Australia/Melbourne")

    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(_ENSURE_INDEX)
        conn.commit()

    # (1) resolve fresh_calls that have since been dialled → mark done (they're no longer "to call").
    resolved = _execute(pool,
        "UPDATE calendar_events e SET status='done' "
        "WHERE e.type='fresh_call' AND e.status='pending' AND EXISTS "
        f"(SELECT 1 FROM calls c WHERE c.in_scope AND {_D9.format(col='c.dest_number')} "
        f"= {_D9.format(col='e.dest_number')})")

    roster = calling_bdes(pool, settings)   # excludes the BDM (non-calling) names
    if not roster:
        return {"skipped": "no_active_bdes", "resolved": resolved}

    zone = ZoneInfo(tz)
    horizon = int(getattr(settings, "fresh_alloc_horizon_days", 14) or 14)
    # the next N WORKING days (skip weekends) — we fill each BDE to `target` on each of these.
    days: list[date] = []
    d = datetime.now(zone).date()
    while len(days) < horizon:
        if d.weekday() < 5:
            days.append(d)
        d += timedelta(days=1)

    # (2) existing pending calls per (BDE, day) — the shortfall to `target` on each day is what fresh fills.
    pend: dict[tuple, int] = {}
    for r in _fetch(pool,
            "SELECT bde_name, (start_at AT TIME ZONE %s)::date d, count(*) n FROM calendar_events "
            "WHERE status='pending' AND type IN ('callback','recall','retry','reached_call','fresh_call') "
            "GROUP BY 1,2", (tz,)):
        if r["bde_name"] and r["d"]:
            pend[(r["bde_name"], r["d"])] = int(r["n"])
    total_need = sum(max(0, target - pend.get((b, dy), 0)) for b in roster for dy in days)
    if total_need <= 0:
        return {"scheduled": 0, "resolved": resolved, "note": "every BDE already at target across the horizon"}

    # (3) rank prospects by value; rank BDEs by 90-day performance.
    perf, team_mean = _bde_perf_90d(pool, int(getattr(settings, "fresh_alloc_perf_shrink_k", 40)))
    cands = _fresh_candidates(pool, total_need)
    if not cands:
        return {"scheduled": 0, "resolved": resolved, "remaining_capacity": total_need,
                "note": "no fresh candidates (pool empty locally / all worked)"}
    order = sorted(roster, key=lambda b: (-perf.get(b, team_mean), b))

    # (4)+(5) fill DAY BY DAY (near days first, across all BDEs), value×performance matched, so the
    #     calendar shows ~target×BDEs per working day until the finite fresh pool is exhausted. Each
    #     BDE's calls that day are spread across business hours.
    step = max(1, (8 * 60) // max(1, target))
    to_insert = []
    ci = 0
    for dy in days:
        if ci >= len(cands):
            break
        for b in order:                                   # highest-perf BDE picks first each day
            need = max(0, target - pend.get((b, dy), 0))
            for k in range(need):
                if ci >= len(cands):
                    break
                cand = cands[ci]; ci += 1
                start = datetime.combine(dy, time(9, 0), tzinfo=zone) + timedelta(minutes=step * k)
                rv = cand.get("revenue_musd")
                val = _revenue_score(rv, 50.0)
                title = f"Fresh · running ads — {cand.get('business_name') or cand.get('domain') or cand['dest_number']}"
                notes = (f"Confirmed running Google Ads. Value score {val:.0f}"
                         + (f" (${round(rv)}M)" if rv else "") + f". Matched to {b} (perf {perf.get(b, team_mean):.0f}).")
                to_insert.append((b, title, start, cand["dest_number"], notes))
    scheduled = _insert_fresh_calls_batch(pool, to_insert)
    stats = {"scheduled": scheduled, "resolved": resolved, "bdes": len(roster),
             "need": total_need, "candidates": len(cands), "days": len(days)}
    log.info("fresh_alloc_done", **stats)
    return stats


def _insert_fresh_calls_batch(pool, rows: list) -> int:
    """Batch-insert pending fresh_calls; the partial unique index (normalized d9) skips any number that
    already has an open fresh_call. Candidates are d9-deduped upstream, so no intra-batch conflicts."""
    if not rows:
        return 0
    with pool.connection() as conn, conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO calendar_events (bde_name, type, title, start_at, dest_number, notes, created_by, status) "
            "VALUES (%s,'fresh_call',%s,%s,%s,%s,'auto','pending') "
            f"ON CONFLICT ({_D9_DEST}) WHERE type='fresh_call' AND status='pending' DO NOTHING",
            rows)
        conn.commit()
    return len(rows)


# --- tiny local DB helpers (avoid importing app-level q) ---
def _fetch(pool: ConnectionPool, sql: str, params=None) -> list[dict]:
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def _execute(pool: ConnectionPool, sql: str, params=None) -> int:
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        n = cur.rowcount
        conn.commit()
    return n
