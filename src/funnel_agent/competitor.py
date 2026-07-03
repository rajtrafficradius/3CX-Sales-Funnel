"""Competitor gap audit — ON-DEMAND, cost-capped, cached in enrichment jsonb.

One prospect per invocation. For a prospect's domain we:
  * find its top ~5 SERP competitors (BOTH paid + organic) — DataForSEO Labs competitors_domain
  * compute the KEYWORD GAP (terms competitors rank for that this domain doesn't)
  * derive the CONTENT GAP (the competitor pages/topics winning those gap keywords)
  * estimate the BACKLINK GAP (referring-domain delta vs competitors)
  * assess GEO / AEO readiness (AI-search / generative-engine — schema, FAQ, entity clarity)
  * synthesise QUICK WINS (low-effort/high-impact) and a GROWTH PLAN (bigger plays)

COST SAFETY. This is pay-per-request (DataForSEO Labs). Every audit is HARD-CAPPED at
`MAX_DFS_CALLS` (default 6) DataForSEO calls, each logged; there is NO loop over domains —
strictly one prospect per call. The result is cached under `enrichment.dataforseo->'competitor_audit'`
(reusing the existing DataForSEO jsonb column, so NO migration is needed) — re-opening is free.

We REUSE the exact DataForSEO client + auth from `enrichment.dataforseo` (DataForSEOClient) and
its already-wrapped calls (`ranked_keywords`, `backlinks_summary`). The only endpoint not already
wrapped there is Labs `competitors_domain`, added here as a thin wrapper on the SAME client
(`_competitors_domain`) — same Basic-auth, same `_post` / `_first_result` plumbing.

Anything estimated (traffic via a CTR curve, DataForSEO's ETV, competition-as-difficulty) is
LABELLED as an assumption in the returned `assumptions` list.
"""

from __future__ import annotations

import re
from collections import Counter
from datetime import datetime, timezone

import httpx
from psycopg.types.json import Json

from .config import Settings
from .logging import get_logger
from .enrichment.dataforseo import DataForSEOClient, _ctr

log = get_logger(__name__)

# --- Cost / scope caps (the whole point of this module) ------------------------------------ #
MAX_DFS_CALLS = 6          # HARD ceiling on DataForSEO calls per audit
TOP_COMPETITORS = 5        # how many SERP competitors we surface
GAP_COMPETITORS = 2        # how many competitors we deep-fetch ranked keywords for (keeps calls low)
OUR_KW_LIMIT = 300         # keywords pulled for OUR domain (one call)
COMP_KW_LIMIT = 300        # keywords pulled per competitor (one call each)

_CACHE_KEY = "competitor_audit"   # enrichment.dataforseo->'competitor_audit'


# ============================================================================================ #
# DataForSEO — thin wrapper for the ONE Labs endpoint not already wrapped in enrichment.dataforseo
# ============================================================================================ #
def _competitors_domain(client: DataForSEOClient, domain: str, limit: int = 20) -> dict:
    """SERP competitors for `domain` (DataForSEO Labs competitors_domain/live).

    Reuses the SAME authenticated client as enrichment.dataforseo — no new HTTP/auth. Returns
    the raw `items` (each carries `intersections` + `full_domain_metrics` with organic AND paid
    counts/etv, which is how we classify a competitor as paid / organic / both)."""
    body = [{
        "target": domain,
        "location_code": client.loc,
        "language_code": client.lang,
        "limit": min(max(limit, 5), 50),
        "exclude_top_domains": False,
        "order_by": ["intersections,desc"],
    }]
    d = client._post("/v3/dataforseo_labs/google/competitors_domain/live", body)
    task = (d.get("tasks") or [{}])[0]
    sc = task.get("status_code")
    if sc is not None and int(sc) >= 40000:
        raise RuntimeError(f"DataForSEO task error {sc}: {task.get('status_message')}")
    res = client._first_result(d)
    return {"items": res.get("items") or [], "cost": d.get("cost")}


class _CallBudget:
    """Enforces the per-audit DataForSEO call ceiling and records every call for auditing.

    `run(name, fn)` runs `fn` only while under the cap, catches errors (so one failing call
    can't sink the whole audit), and appends a log row. `calls_made` counts attempts that were
    actually dispatched (i.e. cost-bearing), never the skipped ones."""

    def __init__(self, cap: int = MAX_DFS_CALLS):
        self.cap = cap
        self.calls_made = 0
        self.log: list[dict] = []

    def run(self, name: str, fn):
        if self.calls_made >= self.cap:
            self.log.append({"endpoint": name, "status": "skipped_cap"})
            log.info("competitor_call_capped", endpoint=name, cap=self.cap)
            return None
        self.calls_made += 1
        try:
            out = fn()
            self.log.append({"endpoint": name, "status": "ok"})
            return out
        except Exception as exc:  # degrade gracefully, keep the audit going
            self.log.append({"endpoint": name, "status": "error", "error": str(exc)[:160]})
            log.warning("competitor_call_failed", endpoint=name, error=str(exc)[:160])
            return None


# ============================================================================================ #
# PURE helpers (no network / no DB) — unit-testable
# ============================================================================================ #
_STOP = {
    "the", "a", "an", "and", "or", "for", "to", "of", "in", "on", "with", "your", "you",
    "near", "me", "best", "top", "cheap", "how", "what", "is", "are", "buy", "get", "vs",
    "&", "-", "au", "australia", "services", "service",
}


def _norm_kw(kw) -> str:
    return re.sub(r"\s+", " ", (kw or "").strip().lower())


def _clean_host(domain: str) -> str:
    d = (domain or "").strip().lower()
    d = re.sub(r"^https?://", "", d).split("/")[0]
    return d[4:] if d.startswith("www.") else d


def _topic_label(keywords: list[str], k: int = 3) -> str:
    """Cheap topic label = the most common significant tokens across a keyword cluster."""
    toks: list[str] = []
    for kw in keywords:
        for t in re.split(r"[^a-z0-9]+", (kw or "").lower()):
            if t and t not in _STOP and not t.isdigit() and len(t) > 2:
                toks.append(t)
    common = [w for w, _ in Counter(toks).most_common(k)]
    return " ".join(common) if common else (keywords[0] if keywords else "")


def pick_competitors(items: list[dict], target: str, top: int = TOP_COMPETITORS) -> list[dict]:
    """From competitors_domain items -> [{domain, type, overlap, est_traffic, paid_keywords,
    organic_keywords}]. `type` (paid/organic/both) is DERIVED from each competitor's own
    full-domain organic+paid keyword counts; `overlap` = intersecting keywords; `est_traffic`
    = DataForSEO ETV (an estimate)."""
    tgt = _clean_host(target)
    out: list[dict] = []
    for it in items or []:
        host = _clean_host(it.get("domain") or "")
        if not host or host == tgt:
            continue
        fm = it.get("full_domain_metrics") or it.get("metrics") or {}
        org = fm.get("organic") or {}
        paid = fm.get("paid") or {}
        org_n = org.get("count") or 0
        paid_n = paid.get("count") or 0
        if org_n and paid_n:
            ctype = "both"
        elif paid_n:
            ctype = "paid"
        else:
            ctype = "organic"
        est = round((org.get("etv") or 0) + (paid.get("etv") or 0))
        out.append({
            "domain": host,
            "type": ctype,
            "overlap": it.get("intersections") or 0,
            "est_traffic": est,
            "organic_keywords": org_n,
            "paid_keywords": paid_n,
            "avg_position": round(it.get("avg_position") or 0, 1) or None,
        })
    out.sort(key=lambda r: (r["overlap"], r["est_traffic"]), reverse=True)
    return out[:top]


def compute_keyword_gap(our_keywords: list[dict],
                        competitor_keywords: dict[str, list[dict]],
                        *, limit: int = 25, min_volume: int = 10,
                        assumed_position: int = 5) -> list[dict]:
    """Keywords competitors rank for that OUR domain does not.

    `competitor_keywords` = {competitor_domain: [ranked_keyword rows]}. Aggregates by keyword:
    volume = max seen, difficulty = avg `competition` x100 (competition-as-difficulty PROXY),
    competitors_ranking = how many competitors rank for it. `est_capture_traffic` = volume x an
    assumed-position CTR (an ESTIMATE of the upside if we ranked ~pos `assumed_position`)."""
    ours = {_norm_kw(k.get("keyword")) for k in (our_keywords or [])}
    agg: dict[str, dict] = {}
    for cdom, kws in (competitor_keywords or {}).items():
        for k in kws or []:
            nk = _norm_kw(k.get("keyword"))
            if not nk or nk in ours:
                continue
            vol = k.get("search_volume") or 0
            if vol < min_volume:
                continue
            a = agg.setdefault(nk, {"keyword": k.get("keyword"), "volume": 0,
                                    "comp": [], "cpc": [], "competitors": [], "positions": []})
            a["volume"] = max(a["volume"], vol)
            if k.get("competition") is not None:
                a["comp"].append(k["competition"])
            if k.get("cpc"):
                a["cpc"].append(k["cpc"])
            if cdom not in a["competitors"]:
                a["competitors"].append(cdom)
            if k.get("position") is not None:
                a["positions"].append(k["position"])
    rows: list[dict] = []
    for a in agg.values():
        diff = round(sum(a["comp"]) / len(a["comp"]) * 100) if a["comp"] else None
        rows.append({
            "keyword": a["keyword"],
            "volume": a["volume"],
            "difficulty": diff,
            "competitors_ranking": len(a["competitors"]),
            "competitor_domains": a["competitors"],
            "best_competitor_position": min(a["positions"]) if a["positions"] else None,
            "cpc": round(sum(a["cpc"]) / len(a["cpc"]), 2) if a["cpc"] else None,
            "est_capture_traffic": round(a["volume"] * _ctr(assumed_position)),
        })
    # Most-shared, highest-volume gaps first (shared by many competitors = proven demand).
    rows.sort(key=lambda r: (r["competitors_ranking"], r["volume"]), reverse=True)
    return rows[:limit]


def compute_content_gap(our_keywords: list[dict],
                        competitor_keywords: dict[str, list[dict]],
                        *, limit: int = 15) -> list[dict]:
    """The competitor PAGES (topics) that win keywords we don't rank for — derived, no extra API
    call, by clustering the gap keywords onto the competitor URL that ranks for them."""
    ours = {_norm_kw(k.get("keyword")) for k in (our_keywords or [])}
    pages: dict[str, dict] = {}
    for cdom, kws in (competitor_keywords or {}).items():
        for k in kws or []:
            nk = _norm_kw(k.get("keyword"))
            if not nk or nk in ours:
                continue
            url = k.get("url")
            if not url:
                continue
            p = pages.setdefault(url, {"page": url, "domain": cdom, "keywords": [], "volume": 0})
            p["keywords"].append(k.get("keyword"))
            p["volume"] += k.get("search_volume") or 0
    rows = [{
        "page": p["page"],
        "domain": p["domain"],
        "topic": _topic_label(p["keywords"]),
        "keyword_count": len(p["keywords"]),
        "example_keywords": p["keywords"][:8],
        "total_volume": p["volume"],
    } for p in pages.values()]
    rows.sort(key=lambda r: r["total_volume"], reverse=True)
    return rows[:limit]


def compute_backlink_gap(our_summary: dict | None,
                         competitor_summaries: dict[str, dict]) -> dict:
    """Referring-domain delta vs competitors. If the backlinks API is unavailable (this account's
    DataForSEO plan may not include it), returns available=False with a note rather than 0s."""
    our_ref = (our_summary or {}).get("referring_domains")
    notable = []
    comp_refs = []
    for dom, s in (competitor_summaries or {}).items():
        if not s:
            continue
        rd = s.get("referring_domains")
        if rd is not None:
            comp_refs.append(rd)
        notable.append({"domain": dom, "referring_domains": rd,
                        "backlinks": s.get("backlinks"), "rank": s.get("rank")})
    avg = round(sum(comp_refs) / len(comp_refs)) if comp_refs else None
    available = our_ref is not None or bool(comp_refs)
    gap = None
    if our_ref is not None and avg is not None:
        gap = avg - our_ref
    notable.sort(key=lambda r: (r.get("referring_domains") or 0), reverse=True)
    out = {
        "available": available,
        "our_refdomains": our_ref,
        "competitor_avg": avg,
        "gap": gap,
        "notable": notable[:5],
    }
    if not available:
        out["note"] = ("Backlink profile not available (the DataForSEO plan on this account may "
                       "not include the Backlinks API) — treat authority-building as a checklist item.")
    return out


# --- GEO / AEO (AI-search / generative-engine) readiness ------------------------------------ #
def _re(pattern: str, html: str) -> bool:
    return bool(re.search(pattern, html, re.IGNORECASE | re.DOTALL))


def assess_geo_aeo(website_enr: dict | None, html: str | None) -> dict:
    """Heuristic GEO/AEO readiness — how ready is the site to be UNDERSTOOD and CITED by AI /
    generative search engines. Prefers real signal from a free homepage fetch (`html`); when
    that's unavailable it returns the same checklist with pass=None (unverified) so the UI still
    has the action list. Each check: {name, pass, note}. `score` = weighted % of passed checks."""
    checks: list[dict] = []

    def add(name, ok, note, weight=1):
        checks.append({"name": name, "pass": ok, "note": note, "weight": weight})

    if not html:
        for nm, note in (
            ("Structured data (schema.org JSON-LD)", "Machine-readable facts help AI extract entities"),
            ("Entity clarity (Organization / LocalBusiness schema)", "Tells AI who you are"),
            ("Answer content (FAQ / Q&A)", "Q&A blocks are directly quotable by AI answers"),
            ("In-depth content (Article / Blog schema)", "Citable, topical depth"),
            ("Breadcrumbs / site structure schema", "Helps engines map your site"),
            ("Meta description", "Source for AI/search snippets"),
            ("Open Graph / social metadata", "Consistent entity metadata"),
            ("Clear title + H1", "Unambiguous primary topic"),
            ("Crawlable (not noindex)", "AI crawlers must be allowed to read the page"),
        ):
            add(nm, None, note + " — homepage not fetched; verify manually")
        return {"score": None, "fetched": False, "checks": checks,
                "note": "Could not fetch the homepage for a live GEO/AEO scan."}

    jsonld = _re(r"application/ld\+json", html)
    add("Structured data (schema.org JSON-LD)", jsonld,
        "JSON-LD found — AI can extract entities" if jsonld
        else "No JSON-LD — add schema.org markup so AI can parse your facts", weight=2)

    entity = _re(r'"@type"\s*:\s*"(organization|localbusiness|corporation|professionalservice|store)"', html)
    add("Entity clarity (Organization / LocalBusiness schema)", entity,
        "Organization-type schema present" if entity
        else "No Organization/LocalBusiness schema — AI can't reliably identify the business entity", weight=2)

    faq = _re(r'"@type"\s*:\s*"(faqpage|question|qapage)"', html) or _re(r"frequently\s+asked\s+questions", html)
    add("Answer content (FAQ / Q&A)", faq,
        "FAQ / Q&A content present — directly quotable by AI answers" if faq
        else "No FAQ/Q&A — add a Q&A block (answer-engine gold: it's what AI quotes)", weight=2)

    article = _re(r'"@type"\s*:\s*"(article|blogposting|newsarticle|howto)"', html)
    add("In-depth content (Article / Blog schema)", article,
        "Article/Blog schema present" if article
        else "No Article/Blog schema — publish citable, topical long-form content")

    crumbs = _re(r'"@type"\s*:\s*"breadcrumblist"', html)
    add("Breadcrumbs / site structure schema", crumbs,
        "BreadcrumbList present" if crumbs else "No breadcrumb schema — helps engines map your site")

    meta = _re(r'<meta[^>]+name=["\']description["\']', html)
    add("Meta description", meta,
        "Meta description present" if meta else "No meta description — the snippet AI/search summarise from")

    og = _re(r'property=["\']og:', html)
    add("Open Graph / social metadata", og,
        "Open Graph tags present" if og else "No Open Graph metadata — add for consistent entity data")

    title = _re(r"<title[^>]*>\s*\S", html)
    h1 = _re(r"<h1[\s>]", html)
    add("Clear title + H1", bool(title and h1),
        "Title and H1 present" if (title and h1) else "Missing a clear <title> and/or <h1> — the page's primary topic")

    noindex = _re(r"<meta[^>]+robots[^>]+noindex", html)
    add("Crawlable (not noindex)", not noindex,
        "Page is indexable" if not noindex else "Page is set to noindex — AI crawlers are blocked", weight=2)

    scored = [c for c in checks if isinstance(c["pass"], bool)]
    tot_w = sum(c["weight"] for c in scored) or 1
    got_w = sum(c["weight"] for c in scored if c["pass"])
    return {"score": round(100 * got_w / tot_w), "fetched": True,
            "checks": [{"name": c["name"], "pass": c["pass"], "note": c["note"]} for c in checks]}


def build_quick_wins_and_growth(competitors: list[dict], keyword_gap: list[dict],
                                content_gap: list[dict], backlink_gap: dict,
                                geo_aeo: dict) -> tuple[list[dict], list[dict]]:
    """Synthesise low-effort/high-impact QUICK WINS and bigger GROWTH-PLAN plays from the gaps.
    Each item: {title, detail, effort, impact, evidence}."""
    quick: list[dict] = []
    growth: list[dict] = []

    # Quick wins — easy keyword targets (shared by competitors, low competition).
    easy = [k for k in keyword_gap if (k.get("difficulty") is None or k["difficulty"] <= 45)
            and (k.get("volume") or 0) >= 30][:5]
    for k in easy:
        quick.append({
            "title": f"Target '{k['keyword']}'",
            "detail": (f"~{k['volume']}/mo searches, {k['competitors_ranking']} competitor(s) rank, "
                       f"competition {'low' if (k.get('difficulty') or 0) <= 30 else 'moderate'}. "
                       f"You don't rank — a focused page is a fast win."),
            "effort": "low", "impact": "high",
            "evidence": {"volume": k["volume"], "difficulty": k["difficulty"],
                         "est_capture_traffic": k.get("est_capture_traffic")},
        })

    # Quick wins — GEO/AEO schema/FAQ gaps (add markup = low effort, high AI-visibility upside).
    for c in (geo_aeo.get("checks") or []):
        if c.get("pass") is False and any(t in c["name"].lower() for t in ("schema", "faq", "structured", "entity", "meta description")):
            quick.append({
                "title": f"Fix: {c['name']}",
                "detail": c.get("note") or "",
                "effort": "low", "impact": "medium",
                "evidence": {"geo_aeo_check": c["name"]},
            })
    quick = quick[:8]

    # Growth — build the missing content pages/topics.
    for p in content_gap[:3]:
        growth.append({
            "title": f"Publish content on: {p['topic'] or p['page']}",
            "detail": (f"{p['domain']} ranks a page ({p['page']}) capturing {p['keyword_count']} "
                       f"keyword(s) (~{p['total_volume']}/mo) you don't. Build a stronger equivalent."),
            "effort": "medium", "impact": "high",
            "evidence": {"competitor_page": p["page"], "total_volume": p["total_volume"]},
        })

    # Growth — authority / backlinks.
    if backlink_gap.get("available") and backlink_gap.get("gap") and backlink_gap["gap"] > 0:
        growth.append({
            "title": "Close the backlink / authority gap",
            "detail": (f"Competitors average {backlink_gap['competitor_avg']} referring domains vs "
                       f"your {backlink_gap['our_refdomains']} (gap {backlink_gap['gap']}). "
                       "Invest in digital PR, listings and partnerships."),
            "effort": "high", "impact": "high",
            "evidence": {"competitor_avg": backlink_gap["competitor_avg"],
                         "our_refdomains": backlink_gap["our_refdomains"]},
        })
    else:
        growth.append({
            "title": "Build domain authority",
            "detail": "Earn referring domains via digital PR, quality directory listings and partner links.",
            "effort": "high", "impact": "medium", "evidence": {},
        })

    # Growth — paid capture where competitors advertise.
    paid_comps = [c["domain"] for c in competitors if c.get("type") in ("paid", "both")]
    if paid_comps:
        growth.append({
            "title": "Run paid search to capture demand now",
            "detail": (f"{len(paid_comps)} competitor(s) ({', '.join(paid_comps[:3])}) run ads — a "
                       "Google Ads campaign on your gap keywords captures buyers while SEO matures."),
            "effort": "medium", "impact": "high",
            "evidence": {"advertising_competitors": paid_comps[:5]},
        })

    # Growth — GEO/AEO programme when readiness is weak.
    if isinstance(geo_aeo.get("score"), int) and geo_aeo["score"] < 60:
        growth.append({
            "title": "Stand up an AI-search (GEO/AEO) content layer",
            "detail": (f"GEO/AEO readiness is {geo_aeo['score']}/100. Add entity schema, an FAQ/answer "
                       "layer and citable topical content so AI engines surface and quote you."),
            "effort": "medium", "impact": "high", "evidence": {"geo_aeo_score": geo_aeo["score"]},
        })
    return quick, growth[:8]


# ============================================================================================ #
# Free homepage fetch (NOT a DataForSEO call) for the GEO/AEO scan
# ============================================================================================ #
def _fetch_homepage_html(domain: str, timeout: float = 8.0) -> str | None:
    """Fetch the homepage HTML for the GEO/AEO scan. Free (plain httpx), best-effort, never raises."""
    if not domain:
        return None
    headers = {"User-Agent": "Mozilla/5.0 (compatible; TrafficRadiusBot/1.0; +https://trafficradius.com.au)"}
    for scheme in ("https", "http"):
        try:
            with httpx.Client(timeout=timeout, follow_redirects=True, headers=headers) as c:
                r = c.get(f"{scheme}://{domain}")
            return (r.text or "")[:2_000_000]
        except Exception:
            continue
    return None


# ============================================================================================ #
# DB cache (reuse the existing enrichment.dataforseo jsonb — sub-key, NO migration)
# ============================================================================================ #
def _read_cache(pool, domain: str) -> tuple[dict | None, dict | None]:
    """Return (cached_audit|None, website_enrichment|None) for a domain."""
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT dataforseo->%s AS ca, website FROM enrichment WHERE domain=%s",
                    (_CACHE_KEY, domain))
        row = cur.fetchone() or {}
    return row.get("ca"), row.get("website")


def _write_cache(pool, domain: str, audit: dict) -> None:
    """Merge the audit under enrichment.dataforseo->'competitor_audit' (jsonb ||, keeps
    rank/ads/audit siblings). Mirrors the seo-audit upsert pattern exactly."""
    patch = {_CACHE_KEY: audit}
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO enrichment (domain, dataforseo, fetched_at) VALUES (%s,%s,now()) "
            "ON CONFLICT (domain) DO UPDATE SET "
            "dataforseo = COALESCE(enrichment.dataforseo,'{}'::jsonb) || %s::jsonb, fetched_at=now()",
            (domain, Json(patch), Json(patch)))
        conn.commit()


# ============================================================================================ #
# MAIN ENTRY
# ============================================================================================ #
def run_competitor_audit(pool, settings: Settings, domain: str, *, force: bool = False) -> dict:
    """On-demand competitor gap audit for ONE prospect domain (see module docstring for shape).

    Returns cache when present and not `force`. Otherwise runs a HARD-CAPPED (<= MAX_DFS_CALLS)
    set of DataForSEO calls, computes every gap, and caches the result in
    enrichment.dataforseo->'competitor_audit'. Never loops over multiple domains."""
    domain = _clean_host(domain)
    if not domain:
        raise ValueError("competitor audit requires a domain")

    cached, website_enr = _read_cache(pool, domain)
    if cached and not force:
        cached = dict(cached)
        cached["cached"] = True
        return cached

    if not settings.dataforseo_enabled:
        # No cache and no paid API configured — cannot produce a live audit.
        raise RuntimeError("DataForSEO not configured")

    budget = _CallBudget(MAX_DFS_CALLS)
    client = DataForSEOClient(settings)
    assumptions = [
        "Competitor set = DataForSEO Labs SERP competitors (top by keyword overlap); may miss "
        "brands that don't overlap in organic search.",
        "Competitor estimated traffic = DataForSEO ETV (an estimate, not measured analytics).",
        "Keyword difficulty is approximated from Google 'competition' (0-1) x100 — a proxy, not "
        "a measured Keyword Difficulty score.",
        f"Keyword & content gap computed against only the top {GAP_COMPETITORS} competitor(s) "
        "(to stay within the DataForSEO cost cap).",
        "'est_capture_traffic' = search volume x an assumed position-5 click-through rate — an "
        "estimate of upside, not a promise.",
    ]
    try:
        # (1) SERP competitors — one call, gives paid + organic + overlap + ETV.
        comps_raw = budget.run("competitors_domain", lambda: _competitors_domain(client, domain, limit=25))
        competitors = pick_competitors((comps_raw or {}).get("items") or [], domain, top=TOP_COMPETITORS)

        # (2) OUR ranked keywords — one call (reuses enrichment.dataforseo wrapper).
        our_rk = budget.run("ranked_keywords(self)", lambda: client.ranked_keywords(domain, limit=OUR_KW_LIMIT))
        our_keywords = (our_rk or {}).get("keywords") or []

        # (3) Deep-fetch ranked keywords for the top GAP_COMPETITORS — one call each.
        competitor_keywords: dict[str, list[dict]] = {}
        for c in competitors[:GAP_COMPETITORS]:
            cd = c["domain"]
            rk = budget.run(f"ranked_keywords({cd})", lambda cd=cd: client.ranked_keywords(cd, limit=COMP_KW_LIMIT))
            competitor_keywords[cd] = (rk or {}).get("keywords") or []

        keyword_gap = compute_keyword_gap(our_keywords, competitor_keywords)
        content_gap = compute_content_gap(our_keywords, competitor_keywords)

        # (4) Backlink summaries — our domain + the #1 competitor (best-effort; plan may lack it).
        our_bl = budget.run("backlinks_summary(self)", lambda: client.backlinks_summary(domain))
        comp_bl: dict[str, dict] = {}
        if competitors:
            cd = competitors[0]["domain"]
            bl = budget.run(f"backlinks_summary({cd})", lambda cd=cd: client.backlinks_summary(cd))
            if bl:
                comp_bl[cd] = bl
        backlink_gap = compute_backlink_gap(our_bl, comp_bl)
    finally:
        client.close()

    # (5) GEO/AEO — FREE homepage fetch (not a DataForSEO call) + existing website scan.
    html = _fetch_homepage_html(domain)
    geo_aeo = assess_geo_aeo(website_enr if isinstance(website_enr, dict) else None, html)

    # (6) Synthesise plan.
    quick_wins, growth_plan = build_quick_wins_and_growth(
        competitors, keyword_gap, content_gap, backlink_gap, geo_aeo)

    result = {
        "domain": domain,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "competitors": competitors,
        "keyword_gap": keyword_gap,
        "content_gap": content_gap,
        "backlink_gap": backlink_gap,
        "geo_aeo": geo_aeo,
        "quick_wins": quick_wins,
        "growth_plan": growth_plan,
        "assumptions": assumptions,
        "calls_made": budget.calls_made,
        "calls_log": budget.log,
        "cached": False,
    }
    _write_cache(pool, domain, result)
    log.info("competitor_audit_done", domain=domain, calls_made=budget.calls_made,
             competitors=len(competitors), keyword_gap=len(keyword_gap))
    return result
