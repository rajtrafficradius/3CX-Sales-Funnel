"""Per-domain marketing enrichment (SEMrush + Apollo), cached in `enrichment`.

For each in-scope call that has an AI-extracted website, fetch the domain's SEMrush
metrics and Apollo (free) company data once and cache it keyed by domain, so every
call to the same business reuses one lookup. Idempotent + cheap on re-runs (only
new / stale domains are fetched). Apollo is hard-guarded to never spend credits.
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, time, timedelta, timezone

from psycopg.types.json import Json
from psycopg_pool import ConnectionPool

from .config import Settings
from .enrichment.apollo import ApolloClient
from .enrichment.semrush import SemrushClient
from .logging import get_logger

log = get_logger(__name__)


# --------------------------------------------------------------------------- #
# FREE website tracking-pixel detection — BULK across the whole database.
# No paid API: fetches each domain's homepage and detects GTM/GA4/Google Ads/Meta
# Pixel/etc. to flag paid-ads activity. Runs for ALL domains (called prospects first,
# then the wider companies DB), progressively via the refresh loop. The per-prospect
# "Enrich website now" button is just an on-demand extra; this is the bulk coverage.
# --------------------------------------------------------------------------- #
def _pending_website_domains(pool: ConnectionPool, limit: int) -> list[str]:
    """Domains with no website scan yet — called prospects (prio 0) before the wider
    companies universe (prio 1), so relevant prospects get paid-ads signals first."""
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT d.domain FROM (
              SELECT domain, min(prio) AS prio FROM (
                SELECT domain, 0 AS prio FROM prospects  WHERE domain IS NOT NULL AND domain <> ''
                UNION ALL
                SELECT domain, 1 AS prio FROM companies  WHERE domain IS NOT NULL AND domain <> ''
              ) u GROUP BY domain
            ) d
            LEFT JOIN enrichment e ON e.domain = d.domain
            WHERE e.domain IS NULL OR e.website IS NULL
            ORDER BY d.prio, d.domain
            LIMIT %s
            """,
            (limit,),
        )
        return [r["domain"] for r in cur.fetchall()]


def enrich_websites(pool: ConnectionPool, limit: int = 200, workers: int = 8) -> dict:
    """Scan up to `limit` not-yet-scanned domains for tracking pixels (FREE). Idempotent:
    re-running only picks up domains still missing website intel. Returns stats."""
    from .enrichment.website import fetch_website_intel

    domains = _pending_website_domains(pool, limit)
    if not domains:
        return {"scanned": 0, "runs_paid_ads": 0, "errors": 0, "remaining": 0}

    def work(domain: str) -> dict:
        intel = fetch_website_intel(domain)
        status = "ok" if intel.get("found") else (intel.get("status") or "error")
        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO enrichment (domain, website, status, fetched_at) VALUES (%s,%s,%s,now()) "
                "ON CONFLICT (domain) DO UPDATE SET website = EXCLUDED.website, "
                "  status = COALESCE(enrichment.status, EXCLUDED.status), fetched_at = now()",
                (domain, Json(intel), status),
            )
            conn.commit()
        return intel

    scanned = runs = errors = 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for intel in ex.map(work, domains):
            scanned += 1
            if intel.get("found"):
                runs += 1 if intel.get("runs_paid_ads") else 0
            else:
                errors += 1
    # how many still pending after this batch (for progress visibility)
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) AS n FROM ("
            "  SELECT DISTINCT domain FROM ("
            "    SELECT domain FROM prospects WHERE domain IS NOT NULL AND domain<>'' "
            "    UNION ALL SELECT domain FROM companies WHERE domain IS NOT NULL AND domain<>'') u) d "
            "LEFT JOIN enrichment e ON e.domain=d.domain WHERE e.domain IS NULL OR e.website IS NULL"
        )
        remaining = cur.fetchone()["n"]
    stats = {"scanned": scanned, "runs_paid_ads": runs, "errors": errors, "remaining": remaining}
    log.info("enrich_websites", **stats)
    return stats


def _remaining_website_domains(pool: ConnectionPool) -> int:
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) AS n FROM ("
            "  SELECT DISTINCT domain FROM ("
            "    SELECT domain FROM prospects WHERE domain IS NOT NULL AND domain<>'' "
            "    UNION ALL SELECT domain FROM companies WHERE domain IS NOT NULL AND domain<>'') u) d "
            "LEFT JOIN enrichment e ON e.domain=d.domain WHERE e.domain IS NULL OR e.website IS NULL"
        )
        return cur.fetchone()["n"]


def enrich_websites_async(pool: ConnectionPool, *, limit: int = 5000, concurrency: int = 300,
                          batch: int = 1000, scan_all: bool = False, per_timeout: float = 8.0,
                          max_bytes: int = 2_000_000) -> dict:
    """High-throughput async twin of enrich_websites — for 100k–1M-scale scanning.

    Fetches up to `concurrency` homepages at once (vs 8 threads), streams only the
    first `max_bytes`, fails fast on the slow tail, and writes each batch in ONE bulk
    upsert. Identical detection + output to the sync scanner (reuses afetch/_detect).
    Idempotent. Run as a dedicated batch job, not inside the live dashboard container.
    """
    import asyncio

    import httpx

    from .enrichment.website import afetch_website_intel

    ua = {"User-Agent": "Mozilla/5.0 (compatible; TrafficRadiusBot/1.0; +https://trafficradius.com.au)"}

    async def scan_batch(domains: list[str]) -> list[tuple[str, dict]]:
        timeout = httpx.Timeout(connect=5.0, read=per_timeout, write=5.0, pool=per_timeout)
        limits = httpx.Limits(max_connections=concurrency, max_keepalive_connections=min(concurrency, 100))
        sem = asyncio.Semaphore(concurrency)
        # Hard wall-clock cap PER domain: httpx's read-timeout doesn't bound total time
        # (a slow-trickle server can stream tiny chunks forever), so without this one bad
        # host stalls the whole batch. wait_for guarantees every task finishes.
        hard_cap = per_timeout * 2 + 4
        client = httpx.AsyncClient(timeout=timeout, follow_redirects=True, headers=ua, limits=limits)

        async def one(dn: str) -> tuple[str, dict]:
            async with sem:
                try:
                    return dn, await asyncio.wait_for(
                        afetch_website_intel(client, dn, max_bytes=max_bytes), timeout=hard_cap)
                except Exception as exc:
                    kind = "timeout" if isinstance(exc, asyncio.TimeoutError) else "error"
                    return dn, {"found": False, "status": kind, "error": str(exc)[:160] or kind}
        try:
            return await asyncio.gather(*[one(d) for d in domains])
        finally:
            # Bound the client close too: a cancelled (timed-out) connection can make
            # aclose() hang, which would stall the whole run. Force on after 10s.
            try:
                await asyncio.wait_for(client.aclose(), timeout=10)
            except Exception:
                pass

    scanned = runs = errors = 0
    while True:
        take = batch if scan_all else min(batch, max(0, limit - scanned))
        if take <= 0:
            break
        domains = _pending_website_domains(pool, take)
        if not domains:
            break
        results = asyncio.run(scan_batch(domains))
        rows = [(dn, Json(intel), ("ok" if intel.get("found") else (intel.get("status") or "error")))
                for dn, intel in results]
        with pool.connection() as conn, conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO enrichment (domain, website, status, fetched_at) VALUES (%s,%s,%s,now()) "
                "ON CONFLICT (domain) DO UPDATE SET website = EXCLUDED.website, "
                "  status = COALESCE(enrichment.status, EXCLUDED.status), fetched_at = now()",
                rows,
            )
            conn.commit()
        for _dn, intel in results:
            scanned += 1
            if intel.get("found"):
                runs += 1 if intel.get("runs_paid_ads") else 0
            else:
                errors += 1
        log.info("enrich_websites_async_batch", batch=len(domains), scanned=scanned, runs=runs, errors=errors)
        if not scan_all and scanned >= limit:
            break
    stats = {"scanned": scanned, "runs_paid_ads": runs, "errors": errors,
             "remaining": _remaining_website_domains(pool), "mode": "async", "concurrency": concurrency}
    log.info("enrich_websites_async_done", **stats)
    return stats


def _pending_domains(pool: ConnectionPool, day: date, refresh_days: int,
                     limit: int | None = None) -> list[str]:
    start = datetime.combine(day, time.min)
    end = start + timedelta(days=1)
    sql = """
        SELECT DISTINCT cl.prospect_website AS domain
        FROM calls c
        JOIN classifications cl ON cl.call_id = c.call_id
        LEFT JOIN enrichment e ON e.domain = cl.prospect_website
        WHERE c.in_scope AND c.started_at >= %(s)s AND c.started_at < %(e)s
          AND cl.prospect_website IS NOT NULL AND cl.prospect_website <> ''
          AND (e.domain IS NULL OR e.fetched_at IS NULL
               OR e.fetched_at < now() - make_interval(days => %(r)s))
        ORDER BY 1
    """
    params: dict = {"s": start, "e": end, "r": refresh_days}
    if limit:
        sql += " LIMIT %(l)s"
        params["l"] = limit
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        return [r["domain"] for r in cur.fetchall()]


def _upsert(pool: ConnectionPool, domain: str, semrush, apollo, status: str) -> None:
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO enrichment (domain, semrush, apollo, status, fetched_at)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (domain) DO UPDATE SET
                semrush = EXCLUDED.semrush, apollo = EXCLUDED.apollo,
                status = EXCLUDED.status, fetched_at = EXCLUDED.fetched_at
            """,
            (domain,
             Json(semrush) if semrush is not None else None,
             Json(apollo) if apollo is not None else None,
             status, datetime.now(timezone.utc)),
        )
        conn.commit()


def enrich_single(pool: ConnectionPool, settings: Settings, domain: str) -> dict:
    """Gap-aware enrichment of ONE domain (used when a user sets a prospect's website).
    Skips the SEMrush overview report if the master already has organic metrics."""
    domain = (domain or "").strip().lower()
    if not domain:
        return {"ok": False, "reason": "no domain"}
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT 1 FROM prospects WHERE domain=%s AND organic_traffic IS NOT NULL LIMIT 1", (domain,))
        has_overview = cur.fetchone() is not None
    sem = SemrushClient(settings) if settings.semrush_api_key else None
    apo = (ApolloClient(settings)
           if (settings.apollo_enabled and settings.apollo_api_key) else None)
    semrush = apollo = None
    if sem:
        try:
            semrush = sem.enrich(domain, want_overview=not has_overview, want_backlinks=True)
        except Exception as exc:
            log.warning("semrush_failed", domain=domain, error=str(exc)[:160])
        sem.close()
    if apo:
        try:
            apollo = apo.enrich_organization(domain)
        except Exception as exc:
            log.warning("apollo_failed", domain=domain, error=str(exc)[:160])
        apo.close()
    ok = bool((semrush and semrush.get("found")) or (apollo and apollo.get("found")))
    _upsert(pool, domain, semrush, apollo, "ok" if ok else "error")
    return {"ok": ok, "domain": domain}


def matched_prospect_domains(pool: ConnectionPool, limit: int = 10,
                             pipeline: str | None = None, refresh_days: int = 30) -> list[dict]:
    """Master-matched prospect domains worth enriching — those we've actually
    called, that aren't already freshly enriched. Returns rows with the master
    overview metrics so the orchestrator knows what SEMrush can SKIP.

    `pipeline` optionally restricts to a pipeline (e.g. 'pipeline1_interested').
    Ordered by most recent call so a small `limit` covers the hottest prospects.
    """
    where = ["pr.domain IS NOT NULL", "c.in_scope",
             "c.dest_number IS NOT NULL", "c.dest_number <> ''",
             "(e.domain IS NULL OR e.fetched_at IS NULL "
             " OR e.fetched_at < now() - make_interval(days => %(r)s))"]
    params: dict = {"r": refresh_days, "lim": limit}
    if pipeline:
        where.append("cl.pipeline = %(pl)s")
        params["pl"] = pipeline
    sql = f"""
        SELECT pr.domain,
               max(pr.organic_traffic)  AS organic_traffic,
               max(pr.organic_keywords) AS organic_keywords,
               max(pr.organic_cost)     AS organic_cost,
               max(pr.adwords_keywords) AS adwords_keywords,
               max(c.started_at)        AS last_call
        FROM calls c
        JOIN classifications cl ON cl.call_id = c.call_id
        JOIN prospects pr
          ON right(regexp_replace(c.dest_number, '[^0-9]', '', 'g'), 9) = ANY(pr.phones_norm)
        LEFT JOIN enrichment e ON e.domain = pr.domain
        WHERE {' AND '.join(where)}
        GROUP BY pr.domain
        ORDER BY max(c.started_at) DESC
        LIMIT %(lim)s
    """
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def enrich_prospects(pool: ConnectionPool, settings: Settings, *, limit: int = 10,
                     pipeline: str | None = None, dry_run: bool = False) -> dict:
    """Gap-aware enrichment of master-matched prospect domains.

    Per the credit rule: SEMrush is called ONLY for metrics the master CSV lacks
    (skips the overview report when the master already has organic/adwords data),
    and Apollo (free company data) fills the rest. `dry_run` plans without calling
    any API (no credits, for verifying selection)."""
    targets = matched_prospect_domains(pool, limit=limit, pipeline=pipeline,
                                       refresh_days=settings.enrich_refresh_days)
    plan = []
    for t in targets:
        has_overview = any(t.get(k) is not None for k in
                           ("organic_traffic", "organic_keywords", "organic_cost", "adwords_keywords"))
        plan.append({"domain": t["domain"], "skip_semrush_overview": has_overview})
    if dry_run:
        return {"selected": len(plan), "plan": plan,
                "semrush_overview_skipped": sum(1 for p in plan if p["skip_semrush_overview"])}

    sem = SemrushClient(settings) if settings.semrush_api_key else None
    apollo_budget = settings.apollo_max_per_day if (settings.apollo_enabled and settings.apollo_api_key) else 0
    apo = ApolloClient(settings) if apollo_budget else None
    enriched = errors = sem_overview_calls = sem_backlink_calls = apollo_calls = 0
    for p in plan:
        domain = p["domain"]
        semrush = apollo = None
        ok = False
        if sem:
            want_ov = not p["skip_semrush_overview"]  # skip overview if master already has it
            try:
                semrush = sem.enrich(domain, want_overview=want_ov, want_backlinks=True)
                ok = ok or bool(semrush.get("found"))
                if want_ov:
                    sem_overview_calls += 1
                sem_backlink_calls += 1
            except Exception as exc:
                log.warning("semrush_failed", domain=domain, error=str(exc)[:160])
        if apo and apollo_calls < apollo_budget:
            try:
                apollo = apo.enrich_organization(domain)
                apollo_calls += 1
                ok = ok or bool(apollo.get("found"))
            except Exception as exc:
                log.warning("apollo_failed", domain=domain, error=str(exc)[:160])
        try:
            _upsert(pool, domain, semrush, apollo, "ok" if ok else "error")
            if ok:
                enriched += 1
            else:
                errors += 1
        except Exception as exc:
            log.warning("enrich_upsert_failed", domain=domain, error=str(exc)[:160])
            errors += 1
    if sem:
        sem.close()
    if apo:
        apo.close()
    stats = {"selected": len(plan), "enriched": enriched, "errors": errors,
             "semrush_overview_calls": sem_overview_calls,
             "semrush_backlink_calls": sem_backlink_calls,
             "semrush_overview_skipped": sum(1 for p in plan if p["skip_semrush_overview"]),
             "apollo_calls": apollo_calls}
    log.info("enrich_prospects_done", **stats)
    return stats


def enrich_day(pool: ConnectionPool, settings: Settings, day: date,
               limit: int | None = None, workers: int | None = None) -> dict:
    """Enrich every not-yet-cached domain seen on `day`. Idempotent."""
    domains = _pending_domains(pool, day, settings.enrich_refresh_days, limit)
    if not domains:
        return {"enriched": 0, "errors": 0, "domains": 0, "apollo_calls": 0}

    workers = workers or settings.enrich_workers
    sem = SemrushClient(settings) if settings.semrush_api_key else None
    apollo_budget = settings.apollo_max_per_day if (settings.apollo_enabled and settings.apollo_api_key) else 0
    apo = ApolloClient(settings) if apollo_budget else None

    lock = threading.Lock()
    used = {"apollo": 0}

    def work(domain: str) -> str:
        semrush = apollo = None
        ok = False
        if sem:
            try:
                semrush = sem.enrich(domain)
                ok = ok or bool(semrush.get("found"))
            except Exception as exc:
                log.warning("semrush_failed", domain=domain, error=str(exc)[:160])
        if apo:
            with lock:
                spend = used["apollo"] < apollo_budget
                if spend:
                    used["apollo"] += 1
            if spend:
                try:
                    apollo = apo.enrich_organization(domain)
                    ok = ok or bool(apollo.get("found"))
                except Exception as exc:
                    log.warning("apollo_failed", domain=domain, error=str(exc)[:160])
        status = "ok" if ok else "error"
        try:
            _upsert(pool, domain, semrush, apollo, status)
        except Exception as exc:
            log.warning("enrich_upsert_failed", domain=domain, error=str(exc)[:160])
            return "error"
        return status

    enriched = errors = 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for st in ex.map(work, domains):
            if st == "ok":
                enriched += 1
            else:
                errors += 1

    if sem:
        sem.close()
    if apo:
        apo.close()
    log.info("enrich_day_done", day=str(day), domains=len(domains),
             enriched=enriched, errors=errors, apollo_calls=used["apollo"])
    return {"enriched": enriched, "errors": errors, "domains": len(domains),
            "apollo_calls": used["apollo"]}
