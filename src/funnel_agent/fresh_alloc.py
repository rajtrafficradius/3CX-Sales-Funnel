"""Fresh Google-Ads calling calendar — AI daily allocation.

The ONLY cold pool that goes on the calendar: prospects CONFIRMED running Google Ads (Transparency
Center). Every other calendar event is relationship-driven (RPC-connect callbacks, P2 agency rotation);
this is the one place a never-dialled prospect is scheduled.

Model: each active BDE holds a curated *rolling worklist* of up to `fresh_calls_per_day_per_bde`
PENDING `fresh_call` events — their high-value fresh slate — replenished each cycle as they dial through
it. Allocation is **value × performance matched**: the highest-revenue fresh prospects are routed to the
highest-performing BDEs (90-day booking / RPC-connect / full-pitch rates), while every BDE is filled to
the cap so nobody is starved. Idempotent: a per-number partial-unique index + "resolve once dialled"
pass keep re-runs stable.

DataForSEO membership (running_google_ads) is populated only on Railway, so this is a no-op locally.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

from psycopg_pool import ConnectionPool

from .config import Settings
from .logging import get_logger
from .next_call import _revenue_score, next_best_time
from .pipeline2 import active_bdes

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
    sql = f"""
    WITH ads AS (SELECT domain FROM enrichment WHERE (dataforseo->>'running_google_ads')='true'),
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
    """Top up every active BDE's fresh-Google-Ads worklist to the cap, value×performance matched.

    Each run: (1) resolve pending fresh_calls whose number has since been dialled, (2) compute each
    BDE's remaining capacity, (3) route the highest-value fresh prospects to the highest-performing
    BDEs (round-robin through the perf-sorted roster so high performers get the better prospects while
    everyone is filled), (4) write `fresh_call` events (created_by='auto') spread across business hours.
    Idempotent. Returns a stats dict."""
    if not getattr(settings, "fresh_alloc_enabled", True):
        return {"skipped": "disabled"}
    cap = int(getattr(settings, "fresh_calls_per_day_per_bde", 50) or 0)
    if cap <= 0:
        return {"skipped": "cap=0"}
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

    roster = active_bdes(pool)
    if not roster:
        return {"skipped": "no_active_bdes", "resolved": resolved}

    # (2) remaining capacity per BDE = cap − their currently-pending fresh_calls.
    pend = {r["bde_name"]: r["n"] for r in _fetch(pool,
        "SELECT bde_name, count(*) n FROM calendar_events "
        "WHERE type='fresh_call' AND status='pending' GROUP BY bde_name")}
    remaining = {b: max(0, cap - int(pend.get(b, 0))) for b in roster}
    need = sum(remaining.values())
    if need <= 0:
        return {"scheduled": 0, "resolved": resolved, "remaining_capacity": 0,
                "note": "all BDE worklists already full"}

    # (3) rank prospects by value; rank BDEs by 90-day performance.
    perf, team_mean = _bde_perf_90d(pool, int(getattr(settings, "fresh_alloc_perf_shrink_k", 40)))
    cands = _fresh_candidates(pool, need)
    if not cands:
        return {"scheduled": 0, "resolved": resolved, "remaining_capacity": need,
                "note": "no fresh candidates (pool empty locally / all worked)"}
    # perf-sorted roster (best first); unknown BDEs default to team mean so new hires get a fair slate.
    order = sorted(roster, key=lambda b: (-perf.get(b, team_mean), b))

    # (4) round-robin the value-sorted candidates through the perf-sorted roster: candidate 0 (highest
    #     value) → best BDE, candidate 1 → 2nd best, … so higher performers systematically receive
    #     higher-value prospects while every BDE fills evenly toward the cap.
    live = [b for b in order if remaining[b] > 0]
    assign: dict[str, list[dict]] = {b: [] for b in roster}
    i = 0
    for cand in cands:
        if not live:
            break
        b = live[i % len(live)]
        assign[b].append(cand)
        remaining[b] -= 1
        if remaining[b] <= 0:
            live.remove(b)
            i = i % len(live) if live else 0
            continue
        i += 1

    # (5) write events, spread across business hours per BDE. Guarded by the unique index (skip dupes).
    zone_open = next_best_time(datetime.now(), tz=tz)   # next real business slot (tz-aware)
    step = max(5, (8 * 60) // cap)                       # minutes between a BDE's calls (09:00–17:00)
    scheduled = 0
    for b, items in assign.items():
        for j, cand in enumerate(items):
            start = next_best_time(zone_open + timedelta(minutes=step * j), tz=tz)
            rv = cand.get("revenue_musd")
            val = _revenue_score(rv, 50.0)
            title = f"Fresh · running ads — {cand.get('business_name') or cand.get('domain') or cand['dest_number']}"
            notes = (f"Confirmed running Google Ads. Value score {val:.0f}"
                     + (f" (${round(rv)}M)" if rv else "") + f". Matched to {b} (perf {perf.get(b, team_mean):.0f}).")
            if _insert_fresh_call(pool, bde=b, title=title, start_at=start,
                                  dest_number=cand["dest_number"], notes=notes):
                scheduled += 1
    stats = {"scheduled": scheduled, "resolved": resolved, "bdes": len(roster),
             "capacity_requested": need, "candidates": len(cands)}
    log.info("fresh_alloc_done", **stats)
    return stats


def _insert_fresh_call(pool, *, bde, title, start_at, dest_number, notes) -> bool:
    """Insert one pending fresh_call, skipping if this number already has an open one (unique index)."""
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO calendar_events (bde_name, type, title, start_at, dest_number, notes, created_by, status) "
            "VALUES (%s,'fresh_call',%s,%s,%s,%s,'auto','pending') "
            f"ON CONFLICT ({_D9_DEST}) WHERE type='fresh_call' AND status='pending' DO NOTHING "
            "RETURNING id",
            (bde, title, start_at, dest_number, notes))
        got = cur.fetchone()
        conn.commit()
    return got is not None


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
