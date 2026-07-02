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


def _pending_deep_domains(pool: ConnectionPool, limit: int, *, with_whois: bool = True,
                          gated: bool = True) -> list[str]:
    """Raghav companies in the $1-10M band, with a domain, that haven't been deep-enriched yet.

    gated=True (default): only domains that PASS the paid-ads gate — Transparency-primary
    (confirmed running Google Ads via DataForSEO) OR an ad pixel present OR GTM+UTM. Running-ads
    domains are ordered first so the highest-value prospects get enriched first.
    gated=False: EVERY $1-10M Raghav domain (Apollo company + decision-maker names/titles are FREE,
    business intel is a cheap LLM call) — used to give the whole core segment baseline coverage so
    the dashboard never shows an empty Apollo/intel section for a $1-10M prospect.

    with_whois=False restricts the pending set to domains missing the Apollo/business-intel
    bundle only (business_intel IS NULL) — used for bulk fills where WHOIS stays lazy/on-demand
    (auDA throttles bulk .au port-43 lookups)."""
    need = ("(e.business_intel IS NULL OR e.whois IS NULL OR (e.whois->>'found') IS DISTINCT FROM 'true')"
            if with_whois else "e.business_intel IS NULL")
    gate = ("""AND ( (e.dataforseo->>'running_google_ads')='true'           -- Transparency-confirmed (primary)
                    OR (e.website->>'runs_paid_ads')='true'
                    OR ((e.website->'trackers'->>'gtm') IS NOT NULL AND (e.website->>'uses_utm')='true') )"""
            if gated else "")
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            f"""
            WITH d AS (
              SELECT DISTINCT lower(domain) AS dom FROM companies
              WHERE source='raghav' AND revenue_musd BETWEEN 1 AND 10
                AND domain IS NOT NULL AND domain <> ''
            )
            SELECT d.dom FROM d JOIN enrichment e ON e.domain = d.dom
            WHERE {need}
              {gate}
            ORDER BY ((e.dataforseo->>'running_google_ads')='true') DESC, d.dom
            LIMIT %s
            """,
            (limit,),
        )
        return [r["dom"] for r in cur.fetchall()]


def enrich_prospects_deep(pool: ConnectionPool, settings: Settings, *, limit: int = 1000,
                          workers: int = 6, with_whois: bool = True, gated: bool = True) -> dict:
    """Deep-enrich the paid-ads prospects ($1-10M): Apollo (FREE company + decision-maker
    names/titles, no credits) + website business intel (products/services/USPs/ICP). Each
    source is stored separately on the enrichment row. Idempotent (skips already-done).

    gated=False deep-enriches EVERY $1-10M Raghav domain (not just paid-ads) so the dashboard
    never shows an empty Apollo/intel section for a core-segment prospect.
    with_whois=False skips the WHOIS step (kept lazy/on-demand — auDA throttles bulk .au)."""
    from .enrichment.apollo import ApolloClient
    from .enrichment.business_intel import extract_business_intel
    from .enrichment.whois_lookup import lookup_whois
    from .transcribe import _openai

    domains = _pending_deep_domains(pool, limit, with_whois=with_whois, gated=gated)
    if not domains:
        return {"processed": 0, "remaining": 0}
    oai = _openai(settings)
    apollo = ApolloClient(settings)  # httpx.Client is thread-safe; share across workers

    def work(domain: str) -> dict:
        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT business_intel, whois FROM enrichment WHERE domain=%s", (domain,))
            row = cur.fetchone() or {}
        updates: dict = {}
        people: list = []
        org = {}
        if not row.get("business_intel"):           # Apollo + website intel (expensive — skip if done)
            try:
                org = apollo.enrich_organization(domain)
                if org.get("found"):
                    people = apollo.search_decision_makers(domain, org_id=org.get("id"))
                    org["people"] = people
            except Exception as exc:
                log.warning("deep_apollo_failed", domain=domain, error=str(exc)[:160])
            try:
                bi = extract_business_intel(oai, settings, domain)
            except Exception as exc:
                log.warning("deep_bi_failed", domain=domain, error=str(exc)[:160])
                bi = {"found": False}
            updates["apollo"] = Json(org)
            updates["business_intel"] = Json(bi)
        if with_whois:
            _w = row.get("whois")                     # free WHOIS (cheap); retry prior failures
            if not isinstance(_w, dict) or _w.get("found") is not True:
                updates["whois"] = Json(lookup_whois(domain))
        if updates:
            cols = list(updates)
            set_sql = ", ".join(f"{c} = %s" for c in cols) + ", fetched_at = now()"
            with pool.connection() as conn, conn.cursor() as cur:
                cur.execute(f"UPDATE enrichment SET {set_sql} WHERE domain = %s",
                            [updates[c] for c in cols] + [domain])
                conn.commit()
        return {"apollo": bool(org.get("found")), "people": len(people),
                "bi": "business_intel" in updates, "whois": "whois" in updates}

    processed = apollo_ok = people_total = bi_ok = 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for i, r in enumerate(ex.map(work, domains), 1):
            processed += 1
            apollo_ok += int(r["apollo"]); people_total += r["people"]; bi_ok += int(r["bi"])
            if i % 25 == 0:
                log.info("deep_enrich_progress", done=i, total=len(domains),
                         apollo=apollo_ok, people=people_total, bi=bi_ok)
    apollo.close()
    remaining = len(_pending_deep_domains(pool, 100000, with_whois=with_whois, gated=gated))
    stats = {"processed": processed, "apollo_ok": apollo_ok, "decision_makers": people_total,
             "business_intel_ok": bi_ok, "remaining": remaining}
    log.info("enrich_prospects_deep_done", **stats)
    return stats


_FULL_ENRICH_COLS = ("website", "apollo", "business_intel", "whois")


def _pending_domains(pool: ConnectionPool, day: date, limit: int | None = None,
                     require: tuple[str, ...] = _FULL_ENRICH_COLS) -> list[str]:
    """Identified domains for the day's in-scope call prospects that are missing any tab.

    A call prospect's domain is identified from EITHER the AI-extracted website OR a
    phone -> master `prospects` match — so a prospect we have a number for (but the AI
    didn't name a site) still gets the full enrichment set. The gate is completeness-based
    (a row missing any required enrichment column), so it self-terminates once a prospect is
    fully enriched and never reprocesses a complete domain every cycle. `require` lists the
    enrichment columns that count as complete (DataForSEO is added only when it's enabled)."""
    incomplete = " OR ".join(f"e.{c} IS NULL" for c in require)
    start = datetime.combine(day, time.min)
    end = start + timedelta(days=1)
    sql = f"""
        WITH day_calls AS (
            SELECT c.dest_number, cl.prospect_website
            FROM calls c
            JOIN classifications cl ON cl.call_id = c.call_id
            WHERE c.in_scope AND c.started_at >= %(s)s AND c.started_at < %(e)s
        ),
        dom AS (
            SELECT lower(prospect_website) AS domain FROM day_calls
            WHERE prospect_website IS NOT NULL AND prospect_website <> ''
            UNION
            SELECT lower(pr.domain) AS domain
            FROM day_calls dc
            JOIN prospects pr
              ON right(regexp_replace(dc.dest_number, '[^0-9]', '', 'g'), 9) = ANY(pr.phones_norm)
            WHERE pr.domain IS NOT NULL AND pr.domain <> ''
        )
        SELECT DISTINCT d.domain
        FROM dom d
        LEFT JOIN enrichment e ON e.domain = d.domain
        WHERE d.domain IS NOT NULL AND d.domain <> ''
          AND (e.domain IS NULL OR {incomplete})
        ORDER BY 1
    """
    params: dict = {"s": start, "e": end}
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


def enrich_domain_full(pool: ConnectionPool, settings: Settings, domain: str, *,
                       with_dataforseo: bool = True, with_whois: bool = True, force: bool = False,
                       oai=None, apollo=None, dfs=None) -> dict:
    """Populate EVERY prospect-page tab for a single domain, idempotently. This is the one
    place that gives a domain its full intel set, so a 3CX/Aircall call prospect (the moment
    its domain is identified) and the on-demand 'Enrich now' button both fill the same tabs:

      * website            — Website information (trackers / paid-ads pixels / business data)
      * semrush            — organic/backlink metrics (gap-aware; skips overview if master has it)
      * apollo             — company + decision-maker names/titles (FREE, no credits)
      * business_intel     — Website business intel (products / services / USPs / ICP — LLM)
      * whois              — Domain / WHOIS (single live lookup; fine for .au on-demand)
      * dataforseo         — Digital marketing insights (Google Ads Transparency + SEO; PAID)

    Each source is independent and skipped when already present (unless force=True). Clients
    can be passed in for batch reuse; any built here are closed before returning."""
    domain = (domain or "").strip().lower()
    if not domain:
        return {"ok": False, "reason": "no domain"}
    from .enrichment.website import fetch_website_intel
    from .enrichment.business_intel import extract_business_intel
    from .enrichment.whois_lookup import lookup_whois

    built = []
    if oai is None:
        from .transcribe import _openai
        oai = _openai(settings)
    if apollo is None and settings.apollo_enabled and settings.apollo_api_key:
        apollo = ApolloClient(settings); built.append(apollo)
    if dfs is None and with_dataforseo and settings.dataforseo_enabled:
        from .enrichment.dataforseo import DataForSEOClient
        dfs = DataForSEOClient(settings); built.append(dfs)

    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT semrush, apollo, website, business_intel, whois, dataforseo "
                    "FROM enrichment WHERE domain=%s", (domain,))
        row = cur.fetchone() or {}

    updates: dict = {}
    did: dict = {}
    # Website information (trackers + paid-ads pixels + business data) — free homepage fetch.
    if force or not row.get("website"):
        try:
            updates["website"] = Json(fetch_website_intel(domain)); did["website"] = True
        except Exception as exc:
            log.warning("full_website_failed", domain=domain, error=str(exc)[:160])
    # SEMrush — gap-aware (skip the overview report if the master CSV already has organic metrics).
    if settings.semrush_api_key and (force or not row.get("semrush")):
        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT 1 FROM prospects WHERE domain=%s AND organic_traffic IS NOT NULL LIMIT 1", (domain,))
            has_overview = cur.fetchone() is not None
        sem = SemrushClient(settings)
        try:
            updates["semrush"] = Json(sem.enrich(domain, want_overview=not has_overview, want_backlinks=True))
            did["semrush"] = True
        except Exception as exc:
            log.warning("full_semrush_failed", domain=domain, error=str(exc)[:160])
        finally:
            sem.close()
    # Apollo company + decision-makers (FREE — names/titles only, never email/phone).
    _ap = row.get("apollo")
    need_apollo = force or not isinstance(_ap, dict) or _ap.get("found") is not True or not _ap.get("people")
    if apollo and need_apollo:
        try:
            org = apollo.enrich_organization(domain)
            if org.get("found"):
                org["people"] = apollo.search_decision_makers(domain, org_id=org.get("id"))
            updates["apollo"] = Json(org); did["apollo"] = bool(org.get("found"))
        except Exception as exc:
            log.warning("full_apollo_failed", domain=domain, error=str(exc)[:160])
    # Website business intel (products / services / USPs / ICP) — one LLM call.
    if force or not row.get("business_intel"):
        try:
            updates["business_intel"] = Json(extract_business_intel(oai, settings, domain)); did["business_intel"] = True
        except Exception as exc:
            log.warning("full_bi_failed", domain=domain, error=str(exc)[:160])
    # Domain / WHOIS — single live lookup (works for .au on-demand; bulk is throttled by auDA,
    # so with_whois=False in bulk backfills leaves it to fill lazily when a prospect is opened).
    _w = row.get("whois")
    if with_whois and (force or not isinstance(_w, dict) or _w.get("found") is not True):
        try:
            updates["whois"] = Json(lookup_whois(domain)); did["whois"] = True
        except Exception as exc:
            log.warning("full_whois_failed", domain=domain, error=str(exc)[:160])
    # Digital marketing insights — DataForSEO (Google Ads Transparency + SEO). PAID, low volume.
    if dfs and (force or not row.get("dataforseo")):
        try:
            updates["dataforseo"] = Json(dfs.enrich_domain(domain)); did["dataforseo"] = True
        except Exception as exc:
            log.warning("full_dataforseo_failed", domain=domain, error=str(exc)[:160])

    if updates:
        cols = list(updates)
        ins_cols = ", ".join(cols)
        ins_vals = ", ".join(["%s"] * len(cols))
        set_sql = ", ".join(f"{c}=EXCLUDED.{c}" for c in cols)
        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                f"INSERT INTO enrichment (domain, {ins_cols}, status, fetched_at) "
                f"VALUES (%s, {ins_vals}, 'ok', now()) "
                f"ON CONFLICT (domain) DO UPDATE SET {set_sql}, status='ok', fetched_at=now()",
                [domain] + [updates[c] for c in cols],
            )
            conn.commit()
    for c in built:
        try:
            c.close()
        except Exception:
            pass
    return {"ok": bool(updates), "domain": domain, "did": did}


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
    """Full-enrich every identified domain seen on `day` so every 3CX/Aircall call prospect
    gets ALL prospect-page tabs the moment its domain is known: website + SEMrush + Apollo
    (company + decision-makers) + business intel + WHOIS + DataForSEO. Idempotent — only the
    missing tabs are fetched, and a fully-enriched domain is never reprocessed."""
    require = _FULL_ENRICH_COLS + (("dataforseo",) if settings.dataforseo_enabled else ())
    domains = _pending_domains(pool, day, limit, require=require)
    if not domains:
        return {"enriched": 0, "errors": 0, "domains": 0}

    workers = workers or settings.enrich_workers
    # Shared clients (httpx.Client + the OpenAI client are thread-safe) reused across threads;
    # enrich_domain_full builds its own short-lived SemrushClient per domain (no sharing).
    from .transcribe import _openai
    oai = _openai(settings)
    apollo = ApolloClient(settings) if (settings.apollo_enabled and settings.apollo_api_key) else None
    dfs = None
    if settings.dataforseo_enabled:
        from .enrichment.dataforseo import DataForSEOClient
        dfs = DataForSEOClient(settings)

    def work(domain: str) -> str:
        try:
            r = enrich_domain_full(pool, settings, domain, oai=oai, apollo=apollo, dfs=dfs)
            return "ok" if r.get("ok") else "skip"
        except Exception as exc:
            log.warning("enrich_day_domain_failed", domain=domain, error=str(exc)[:160])
            return "error"

    enriched = errors = skipped = 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for st in ex.map(work, domains):
            if st == "ok":
                enriched += 1
            elif st == "error":
                errors += 1
            else:
                skipped += 1

    if apollo:
        apollo.close()
    if dfs:
        dfs.close()
    log.info("enrich_day_done", day=str(day), domains=len(domains),
             enriched=enriched, errors=errors, skipped=skipped)
    return {"enriched": enriched, "errors": errors, "domains": len(domains), "skipped": skipped}


def enrich_whois_trickle(pool: ConnectionPool, settings: Settings, limit: int = 12) -> dict:
    """Paced WHOIS fill for the confirmed running-ads set. auDA hard-rate-limits .au WHOIS per
    IP (HTTP 429 after a small burst), so this is deliberately SEQUENTIAL + small-batch +
    oldest-attempt-first: each refresh cycle fills a few, a just-failed domain goes to the back
    of the queue (fetched_at bumped), and over a few hours the whole set fills without throttling.
    Idempotent; safe to run every cycle. Returns {checked, found}."""
    if limit <= 0:
        return {"checked": 0, "found": 0}
    from .enrichment.whois_lookup import lookup_whois
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            # ALL running-ads-confirmed domains (any revenue band) missing WHOIS — not just the
            # $1-10M band. auDA rate-limits bulk .au WHOIS per IP, so the loop fills these a few
            # per cycle (oldest-attempt-first); over time the whole set fills without throttling.
            """
            SELECT e.domain FROM enrichment e
            WHERE (e.dataforseo->>'running_google_ads')='true'
              AND (e.whois->>'found') IS DISTINCT FROM 'true'
            ORDER BY e.fetched_at ASC NULLS FIRST
            LIMIT %s
            """,
            (limit,),
        )
        domains = [r["domain"] for r in cur.fetchall()]
    if not domains:
        return {"checked": 0, "found": 0}
    found = 0
    for dom in domains:                       # sequential — never parallel (registry rate limit)
        try:
            w = lookup_whois(dom)
        except Exception:
            w = {"found": False, "status": "error"}
        if w.get("found"):
            found += 1
        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO enrichment (domain, whois, fetched_at) VALUES (%s,%s,now()) "
                "ON CONFLICT (domain) DO UPDATE SET whois = EXCLUDED.whois, fetched_at = now()",
                (dom, Json(w)))
            conn.commit()
    log.info("enrich_whois_trickle", checked=len(domains), found=found)
    return {"checked": len(domains), "found": found}


def enrich_apollo_people_trickle(pool: ConnectionPool, settings: Settings, limit: int = 6) -> dict:
    """Paced Apollo decision-maker (people) fill for running-ads prospects whose stored people are
    MISSING or STALE (fetched with the old per_page=10/no-rank code). Apollo rate-limits bursts, so
    this is SEQUENTIAL + small-batch + oldest-attempt-first: a few per cycle, refetched with the
    current code (per_page=25 + founder-first ranking). NEVER overwrites good data with empty (a
    rate-limited / no-DM result is skipped, and its fetched_at is bumped so it rotates to the back).
    Idempotent; safe to run every cycle. Returns {checked, updated, empty}."""
    if limit <= 0 or not (settings.apollo_enabled and settings.apollo_api_key):
        return {"checked": 0, "updated": 0, "empty": 0}
    from .enrichment.apollo import ApolloClient
    with pool.connection() as conn, conn.cursor() as cur:
        # Missing people, OR people present but never ranked (old code) → refetch. Oldest first.
        cur.execute(
            """
            SELECT e.domain, e.apollo->>'id' AS org_id FROM enrichment e
            WHERE (e.dataforseo->>'running_google_ads')='true' AND e.apollo IS NOT NULL
              AND (jsonb_array_length(COALESCE(e.apollo->'people','[]'::jsonb)) = 0
                   OR (e.apollo->'people'->0->>'rank') IS NULL)
            ORDER BY e.fetched_at ASC NULLS FIRST
            LIMIT %s
            """,
            (limit,),
        )
        domains = [(r["domain"], r["org_id"]) for r in cur.fetchall()]
    if not domains:
        return {"checked": 0, "updated": 0, "empty": 0}
    apollo = ApolloClient(settings)
    updated = empty = 0
    for dom, org_id in domains:               # sequential — Apollo rate-limits bursts
        try:
            people = apollo.search_decision_makers(dom, org_id=org_id or None)
        except Exception as exc:
            people = []
            log.warning("apollo_people_trickle_failed", domain=dom, error=str(exc)[:120])
        with pool.connection() as conn, conn.cursor() as cur:
            if people:                        # only write real results — never wipe with []
                cur.execute("UPDATE enrichment SET apollo = jsonb_set(apollo, '{people}', %s::jsonb), "
                            "fetched_at = now() WHERE domain = %s", (Json(people), dom))
                updated += 1
            else:                             # bump fetched_at so it rotates to the back of the queue
                cur.execute("UPDATE enrichment SET fetched_at = now() WHERE domain = %s", (dom,))
                empty += 1
            conn.commit()
    apollo.close()
    log.info("enrich_apollo_people_trickle", checked=len(domains), updated=updated, empty=empty)
    return {"checked": len(domains), "updated": updated, "empty": empty}


def _all_call_prospect_domains(pool: ConnectionPool, require: tuple[str, ...]) -> list[str]:
    """Every identified domain across ALL in-scope call prospects (AI website OR phone->master
    match) that is missing any required enrichment column — for the one-time backfill."""
    incomplete = " OR ".join(f"e.{c} IS NULL" for c in require)
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            f"""
            WITH d9 AS (
              SELECT DISTINCT right(regexp_replace(dest_number, '[^0-9]', '', 'g'), 9) AS n
              FROM calls WHERE in_scope AND dest_number IS NOT NULL
            ),
            darr AS (SELECT array_agg(n) AS a FROM d9),
            dom AS (
              SELECT DISTINCT lower(cl.prospect_website) AS d
              FROM classifications cl JOIN calls c ON c.call_id = cl.call_id
              WHERE c.in_scope AND cl.prospect_website IS NOT NULL AND cl.prospect_website <> ''
              UNION
              SELECT DISTINCT lower(pr.domain) AS d
              FROM prospects pr, darr
              WHERE pr.domain IS NOT NULL AND pr.domain <> '' AND pr.phones_norm && darr.a
            )
            SELECT d.d FROM dom d LEFT JOIN enrichment e ON e.domain = d.d
            WHERE d.d IS NOT NULL AND d.d <> '' AND (e.domain IS NULL OR {incomplete})
            ORDER BY d.d
            """
        )
        return [r["d"] for r in cur.fetchall()]


def enrich_call_prospects(pool: ConnectionPool, settings: Settings, *, limit: int = 100000,
                          workers: int = 8, with_dataforseo: bool = False,
                          with_whois: bool = False) -> dict:
    """One-time backfill so every ALREADY-called prospect (3CX/Aircall) with an identified
    domain gets its full FREE enrichment set: website + SEMrush + Apollo company/people +
    business intel. WHOIS stays lazy by default (auDA throttles bulk .au — it fills when a
    prospect is opened) and DataForSEO is off by default (paid; the daily loop runs it forward
    for new prospects). Idempotent — only domains missing a tab are processed."""
    require = ("website", "apollo", "business_intel")
    if with_whois:
        require += ("whois",)
    if with_dataforseo and settings.dataforseo_enabled:
        require += ("dataforseo",)
    domains = _all_call_prospect_domains(pool, require)[:limit]
    if not domains:
        return {"processed": 0, "remaining": 0}

    from .transcribe import _openai
    oai = _openai(settings)
    apollo = ApolloClient(settings) if (settings.apollo_enabled and settings.apollo_api_key) else None
    dfs = None
    if with_dataforseo and settings.dataforseo_enabled:
        from .enrichment.dataforseo import DataForSEOClient
        dfs = DataForSEOClient(settings)

    def work(domain: str) -> str:
        try:
            r = enrich_domain_full(pool, settings, domain, with_dataforseo=with_dataforseo,
                                   with_whois=with_whois, oai=oai, apollo=apollo, dfs=dfs)
            return "ok" if r.get("ok") else "skip"
        except Exception as exc:
            log.warning("enrich_call_prospect_failed", domain=domain, error=str(exc)[:160])
            return "error"

    ok = err = skip = 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for i, st in enumerate(ex.map(work, domains), 1):
            if st == "ok":
                ok += 1
            elif st == "error":
                err += 1
            else:
                skip += 1
            if i % 50 == 0:
                log.info("enrich_call_prospects_progress", done=i, total=len(domains), ok=ok, errors=err)
    if apollo:
        apollo.close()
    if dfs:
        dfs.close()
    remaining = len(_all_call_prospect_domains(pool, require))
    stats = {"processed": len(domains), "ok": ok, "errors": err, "skipped": skip, "remaining": remaining}
    log.info("enrich_call_prospects_done", **stats)
    return stats


# --------------------------------------------------------------------------- #
# DataForSEO — SEO metrics + Google Ads Transparency Center (PAID)
# --------------------------------------------------------------------------- #
def _pending_dataforseo_domains(pool: ConnectionPool, limit: int) -> list[str]:
    """Raghav $1-10M, paid-ads-gated domains not yet DataForSEO-enriched."""
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            WITH d AS (
              SELECT DISTINCT lower(domain) AS dom FROM companies
              WHERE source='raghav' AND revenue_musd BETWEEN 1 AND 10
                AND domain IS NOT NULL AND domain <> ''
            )
            SELECT d.dom FROM d JOIN enrichment e ON e.domain = d.dom
            WHERE ( (e.website->>'runs_paid_ads')='true'
                    OR ((e.website->'trackers'->>'gtm') IS NOT NULL AND (e.website->>'uses_utm')='true') )
              AND e.dataforseo IS NULL
            ORDER BY d.dom LIMIT %s
            """,
            (limit,),
        )
        return [r["dom"] for r in cur.fetchall()]


def enrich_dataforseo_one(pool: ConnectionPool, client, domain: str) -> dict:
    """DataForSEO-enrich a single domain and upsert into enrichment.dataforseo. Never raises."""
    data = client.enrich_domain(domain)
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO enrichment (domain, dataforseo, fetched_at) VALUES (%s, %s, now()) "
            "ON CONFLICT (domain) DO UPDATE SET dataforseo = EXCLUDED.dataforseo, fetched_at = now()",
            (domain, Json(data)),
        )
        conn.commit()
    return data


def enrich_dataforseo_batch(pool: ConnectionPool, settings: Settings, limit: int = 1000,
                            workers: int = 6) -> dict:
    """Batch DataForSEO enrichment for Raghav $1-10M paid-ads-gated domains. Idempotent."""
    if not settings.dataforseo_enabled:
        return {"processed": 0, "remaining": 0, "skipped": "dataforseo not configured"}
    from .enrichment.dataforseo import DataForSEOClient
    domains = _pending_dataforseo_domains(pool, limit)
    if not domains:
        return {"processed": 0, "remaining": 0, "running_ads": 0}
    client = DataForSEOClient(settings)
    running = 0

    def work(dom: str) -> bool:
        try:
            d = enrich_dataforseo_one(pool, client, dom)
            return bool(d.get("running_google_ads"))
        except Exception as exc:
            log.warning("dataforseo_one_failed", domain=dom, error=str(exc)[:160])
            return False

    processed = 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for ok in ex.map(work, domains):
            processed += 1
            running += 1 if ok else 0
    client.close()
    remaining = len(_pending_dataforseo_domains(pool, 100000))
    stats = {"processed": processed, "running_ads": running, "remaining": remaining}
    log.info("enrich_dataforseo_done", **stats)
    return stats


def _pending_transparency_domains(pool: ConnectionPool, limit: int) -> list[str]:
    """ALL Raghav $1-10M domains not yet DataForSEO-checked (for transparency-only sweep)."""
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            WITH d AS (
              SELECT DISTINCT lower(domain) AS dom FROM companies
              WHERE source='raghav' AND revenue_musd BETWEEN 1 AND 10 AND domain IS NOT NULL AND domain<>''
            )
            SELECT d.dom FROM d LEFT JOIN enrichment e ON e.domain=d.dom
            WHERE e.dataforseo IS NULL ORDER BY d.dom LIMIT %s
            """, (limit,))
        return [r["dom"] for r in cur.fetchall()]


def enrich_transparency_all(pool: ConnectionPool, settings: Settings, limit: int = 10000,
                            workers: int = 10) -> dict:
    """Cheap Transparency-ONLY sweep (ads_search, ~$0.002/domain) across ALL Raghav $1-10M
    domains — the PRIMARY running-ads signal. SEO/rank metrics are added separately (on the
    confirmed-running ones / on-demand) to control cost. Idempotent."""
    if not settings.dataforseo_enabled:
        return {"processed": 0, "remaining": 0, "skipped": "not configured"}
    from datetime import datetime, timezone

    from .enrichment.dataforseo import DataForSEOClient
    domains = _pending_transparency_domains(pool, limit)
    if not domains:
        return {"processed": 0, "running_ads": 0, "remaining": 0}
    client = DataForSEOClient(settings)

    def work(dom: str) -> bool:
        try:
            ads = client.ads_search(dom)
            data = {"found": True, "transparency_only": True, "ads": ads,
                    "running_google_ads": bool(ads.get("running_ads")),
                    "fetched_at": datetime.now(timezone.utc).isoformat()}
            with pool.connection() as conn, conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO enrichment (domain, dataforseo, fetched_at) VALUES (%s,%s,now()) "
                    "ON CONFLICT (domain) DO UPDATE SET dataforseo=EXCLUDED.dataforseo, fetched_at=now()",
                    (dom, Json(data)))
                conn.commit()
            return data["running_google_ads"]
        except Exception as exc:
            log.warning("transparency_one_failed", domain=dom, error=str(exc)[:160])
            return False

    processed = running = 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for ok in ex.map(work, domains):
            processed += 1
            running += 1 if ok else 0
    client.close()
    remaining = len(_pending_transparency_domains(pool, 100000))
    stats = {"processed": processed, "running_ads": running, "remaining": remaining}
    log.info("enrich_transparency_all_done", **stats)
    return stats
