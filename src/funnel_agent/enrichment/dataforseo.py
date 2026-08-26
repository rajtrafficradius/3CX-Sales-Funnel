"""DataForSEO enrichment — SEO metrics + Google Ads Transparency Center.

PAID, pay-per-request (~$0.012/domain total): auto-enriched for Raghav $1-10M paid-ads-gated
prospects, on-demand for everyone else. Two endpoints:
  * dataforseo_labs/google/domain_rank_overview/live  — organic + paid keyword/traffic metrics.
  * serp/google/ads_search/live/advanced              — Google Ads Transparency Center: the actual
    ad creatives an advertiser runs (creative_id, format, preview image, verified, first/last shown).

`last_shown` within `dataforseo_ads_recent_days` => the company is CURRENTLY running Google Ads —
the definitive signal (a domain can have 0 paid search keywords yet many live Display/Shopping ads).
"""

from __future__ import annotations

import base64
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone

import httpx

from ..config import Settings
from ..logging import get_logger

log = get_logger(__name__)


def _auth_header(login: str, password: str) -> str:
    """DataForSEO Basic auth. The dashboard often shows the password AS the base64
    'login:password' token — detect that and use it directly; otherwise encode login:password."""
    try:
        dec = base64.b64decode(password + "=" * (-len(password) % 4)).decode("utf-8")
        if dec.count(":") == 1 and all(31 < ord(c) < 127 for c in dec):
            return "Basic " + password
    except Exception:
        pass
    return "Basic " + base64.b64encode(f"{login}:{password}".encode()).decode()


class DataForSEOClient:
    def __init__(self, settings: Settings):
        self.loc = settings.dataforseo_location_code
        self.lang = settings.dataforseo_language_code
        self.recent_days = settings.dataforseo_ads_recent_days
        self._client = httpx.Client(
            base_url=settings.dataforseo_base.rstrip("/"), timeout=90.0,
            headers={"Authorization": _auth_header(settings.dataforseo_login, settings.dataforseo_password),
                     "Content-Type": "application/json"})

    def close(self) -> None:
        try:
            self._client.close()
        except Exception:
            pass

    def _post(self, path: str, body: list) -> dict:
        r = self._client.post(path, json=body)
        r.raise_for_status()
        d = r.json()
        if str(d.get("status_code")) not in ("20000", "None") and d.get("status_code") != 20000:
            # top-level ok; per-task errors handled by caller
            pass
        return d

    @staticmethod
    def _first_result(d: dict) -> dict:
        t = (d.get("tasks") or [{}])[0]
        res = t.get("result") or []
        return (res[0] or {}) if res else {}

    def balance(self) -> dict:
        try:
            d = self._client.get("/v3/appendix/user_data").json()
            r = self._first_result(d)
            money = r.get("money") or {}
            return {"balance": money.get("balance"), "currency": money.get("currency") or "USD"}
        except Exception as exc:
            log.warning("dataforseo_balance_failed", error=str(exc)[:160])
            return {}

    def domain_rank_overview(self, domain: str) -> dict:
        """Organic + paid keyword/traffic metrics for a domain."""
        body = [{"target": domain, "location_code": self.loc, "language_code": self.lang}]
        d = self._post("/v3/dataforseo_labs/google/domain_rank_overview/live", body)
        res = self._first_result(d)
        items = res.get("items") or []
        m = (items[0] or {}).get("metrics") or {} if items else {}
        org = m.get("organic") or {}
        paid = m.get("paid") or {}
        def slim(x):
            return {"count": x.get("count"), "etv": round(x.get("etv") or 0),
                    "traffic_cost": round(x.get("estimated_paid_traffic_cost") or 0),
                    "pos_1": x.get("pos_1"), "is_new": x.get("is_new")}
        return {"organic": slim(org), "paid": slim(paid), "cost": d.get("cost")}

    def ads_search(self, domain: str, depth: int = 100) -> dict:
        """Google Ads Transparency Center ads for a domain (creatives + dates + verified)."""
        body = [{"target": domain, "location_code": self.loc, "depth": min(max(depth, 40), 120),
                 "platform": "all"}]
        d = self._post("/v3/serp/google/ads_search/live/advanced", body)
        res = self._first_result(d)
        items = res.get("items") or []
        advertisers, fmts, last_dt = set(), {}, None
        creatives = []
        for it in items:
            adv = it.get("advertiser_id")
            if adv:
                advertisers.add(adv)
            f = it.get("format") or "unknown"
            fmts[f] = fmts.get(f, 0) + 1
            ls = it.get("last_shown")
            for cand in (it.get("last_shown"), it.get("first_shown")):
                dtv = _parse_dt(cand)
                if dtv and (last_dt is None or dtv > last_dt):
                    last_dt = dtv
            if len(creatives) < 12:
                creatives.append({
                    "advertiser_id": adv, "creative_id": it.get("creative_id"),
                    "format": it.get("format"), "verified": it.get("verified"),
                    "first_shown": it.get("first_shown"), "last_shown": it.get("last_shown"),
                    "preview_image": (it.get("preview_image") or {}).get("url"),
                    "url": it.get("url"),
                })
        running = False
        if last_dt:
            running = (datetime.now(timezone.utc) - last_dt).days <= self.recent_days
        return {
            "count": res.get("items_count") or len(items),
            "advertisers": sorted(advertisers),
            "verified": any(c.get("verified") for c in creatives),
            "formats": fmts,
            "last_shown": str(last_dt)[:10] if last_dt else None,
            "running_ads": running,
            "items": creatives,
            "cost": d.get("cost"),
        }

    def ranked_keywords(self, domain: str, limit: int = 100) -> dict:
        """The ACTUAL organic keywords a domain ranks for (keyword + position + search volume +
        CPC + ranking URL) — the raw data for a keyword/gap audit. We take keyword DATA only and
        ESTIMATE traffic ourselves (position x volume) rather than buying DataForSEO's traffic
        metrics. Ordered by search volume so the highest-opportunity terms come first."""
        body = [{
            "target": domain, "location_code": self.loc, "language_code": self.lang,
            "limit": min(max(limit, 10), 1000),
            "order_by": ["keyword_data.keyword_info.search_volume,desc"],
            # organic SERP positions only (exclude paid/other item types)
            "filters": [["ranked_serp_element.serp_item.type", "=", "organic"]],
        }]
        d = self._post("/v3/dataforseo_labs/google/ranked_keywords/live", body)
        # Surface a task-level error (HTTP 200 + status_code>=40000, e.g. bad target / no funds)
        # instead of silently returning an empty (but paid-looking) audit.
        task = (d.get("tasks") or [{}])[0]
        sc = task.get("status_code")
        if sc is not None and int(sc) >= 40000:
            raise RuntimeError(f"DataForSEO task error {sc}: {task.get('status_message')}")
        res = self._first_result(d)
        out = []
        for it in (res.get("items") or []):
            kd = it.get("keyword_data") or {}
            ki = kd.get("keyword_info") or {}
            se = (it.get("ranked_serp_element") or {}).get("serp_item") or {}
            # CTR curve + audit buckets are keyed on ORGANIC position, so use rank_group
            # (ordinal among organic results); rank_absolute counts non-organic SERP
            # elements above (ads, featured snippet, PAA, local pack) and would inflate pos.
            pos = se.get("rank_group") or se.get("rank_absolute")
            if pos is None:
                continue
            out.append({
                "keyword": kd.get("keyword"),
                "position": pos,
                "search_volume": ki.get("search_volume") or 0,
                "cpc": round(ki.get("cpc") or 0, 2),
                "competition": ki.get("competition"),
                "url": se.get("url"),
            })
        return {"keywords": out, "count": res.get("total_count") or len(out), "cost": d.get("cost")}

    def keyword_suggestions(self, seeds: list[str], location_code: int | None = None,
                            per_seed: int = 25, max_seeds: int = 3) -> dict:
        """Keyword-DEMAND discovery — the REAL service+location searches buyers actually type around a set
        of SEED service terms. This is the signal `ranked_keywords` can't give: it only sees what a domain
        ALREADY ranks for, so a low-footprint site (a machine shop ranking for ~3 terms) never reveals its
        true addressable market ('cnc machining melbourne', 'line boring', 'engine reconditioning', …).

        ENDPOINT CHOICE — dataforseo_labs/google/keyword_suggestions/live (SEED-CONTAINING long-tail), NOT
        keyword_ideas/live: keyword_ideas returns CATEGORY-mates (for a machine-shop seed it hands back
        'amazon australia', 'sydney weather' — huge-volume but wildly off-topic noise), whereas suggestions
        returns keywords that literally CONTAIN the seed ('cnc machining' → 'cnc machining melbourne',
        'precision cnc machining', …) — the actual service demand we want. We take keyword DATA only
        (search volume + CPC + competition) and estimate capture/traffic ourselves — exactly like the
        competitor gap — never buying a traffic figure.

        One call per seed (capped at `max_seeds` to control cost); results are merged, deduped by keyword
        (keeping the higher volume), and ordered by search volume (biggest demand first).

        GUARDED: any failure, task-error, empty seed set or missing key returns {} and NEVER raises — a
        discovery miss must fall back to today's behaviour, never sink an audit or inflate a number."""
        try:
            seeds_clean: list[str] = []
            for s in (seeds or []):
                s = (s or "").strip().lower()
                if s and s not in seeds_clean:
                    seeds_clean.append(s)
            seeds_clean = seeds_clean[:max(1, max_seeds)]
            if not seeds_clean:
                return {}
            loc = int(location_code or self.loc)
            merged: dict[str, dict] = {}
            total_cost = 0.0
            for seed in seeds_clean:
                body = [{
                    "keyword": seed,
                    "location_code": loc,
                    "language_code": self.lang,
                    "limit": min(max(per_seed, 10), 100),
                    "include_seed_keyword": True,
                    "order_by": ["keyword_info.search_volume,desc"],
                    # real demand only — drop null/zero-volume suggestions server-side
                    "filters": [["keyword_info.search_volume", ">", 0]],
                }]
                try:
                    d = self._post("/v3/dataforseo_labs/google/keyword_suggestions/live", body)
                except Exception as exc:
                    log.warning("dataforseo_keyword_suggestions_call_failed", seed=seed, error=str(exc)[:120])
                    continue
                task = (d.get("tasks") or [{}])[0]
                sc = task.get("status_code")
                if sc is not None and int(sc) >= 40000:   # bad seed / no funds — skip this seed, don't raise
                    log.warning("dataforseo_keyword_suggestions_task_error", seed=seed, status=str(sc),
                                message=str(task.get("status_message"))[:120])
                    continue
                total_cost += (d.get("cost") or 0)
                res = self._first_result(d)
                for it in (res.get("items") or []):
                    ki = it.get("keyword_info") or {}
                    kw = it.get("keyword")
                    if not kw:
                        continue
                    key = kw.strip().lower()
                    vol = ki.get("search_volume") or 0
                    row = {"keyword": kw, "search_volume": vol,
                           "cpc": round(ki.get("cpc") or 0, 2), "competition": ki.get("competition")}
                    prev = merged.get(key)
                    if not prev or vol > (prev.get("search_volume") or 0):
                        merged[key] = row
            out = sorted(merged.values(), key=lambda r: r.get("search_volume") or 0, reverse=True)
            return {"keywords": out, "count": len(out), "cost": round(total_cost, 4)}
        except Exception as exc:
            log.warning("dataforseo_keyword_suggestions_failed", error=str(exc)[:160])
            return {}

    def keyword_clusters(self, seeds: list[str], location_code: int | None = None,
                         per_seed: int = 40, max_seeds: int = 6) -> dict:
        """FULL keyword-DEMAND + product/service THEME CLUSTERS around a set of SEED service/product terms.

        This is the audit's answer to a domain with NO SEO footprint of its own: keyword / search-volume /
        CPC come from Google's keyword DATABASE (via keyword_suggestions), NOT from any domain's rankings —
        so a brand-new or 'coming soon' site still yields the real shape of its market's demand. Wraps
        keyword_suggestions (seed-containing long-tail), then groups the returned keywords into
        product/service clusters (`cluster_keywords`) each carrying total monthly search volume, an average
        CPC and its top example searches.

        Returns {seeds, keywords, clusters, cost}; clusters are ordered by commercial value (volume × CPC).
        GUARDED: returns {} on any failure and NEVER raises — a discovery miss must fall back, never sink an
        audit."""
        try:
            sug = self.keyword_suggestions(seeds, location_code=location_code,
                                           per_seed=per_seed, max_seeds=max_seeds)
            kws = sug.get("keywords") or []
            return {"seeds": [s for s in (seeds or []) if s],
                    "keywords": kws, "clusters": cluster_keywords(kws), "cost": sug.get("cost")}
        except Exception as exc:
            log.warning("dataforseo_keyword_clusters_failed", error=str(exc)[:160])
            return {}

    def serp_competitors(self, keywords: list[str], limit: int = 20) -> dict:
        """Domains competing across a SET OF KEYWORDS (DataForSEO Labs serp_competitors/live).
        Used to find REAL same-vertical competitors for a low-footprint site by seeding with its
        industry+location SERVICE terms (e.g. 'cnc machining melbourne') — far more accurate than
        brand-lookalike domain-overlap matches (which returned metro-transport for a machine shop).
        Ordered by how much each domain competes on the seed keywords."""
        kws = [k for k in (keywords or []) if k][:20]
        if not kws:
            return {"items": []}
        body = [{"keywords": kws, "location_code": self.loc, "language_code": self.lang,
                 "limit": min(max(limit, 5), 50),
                 # only domains ranking organically for a meaningful share of the seed set
                 "filters": [["median_position", "<=", 50]]}]
        d = self._post("/v3/dataforseo_labs/google/serp_competitors/live", body)
        task = (d.get("tasks") or [{}])[0]
        sc = task.get("status_code")
        if sc is not None and int(sc) >= 40000:
            raise RuntimeError(f"DataForSEO serp_competitors error {sc}: {task.get('status_message')}")
        res = self._first_result(d)
        out = []
        for it in (res.get("items") or []):
            dom = it.get("domain")
            if not dom:
                continue
            met = ((it.get("full_domain_metrics") or {}).get("organic")
                   or (it.get("metrics") or {}).get("organic") or {})
            out.append({
                "domain": dom,
                "avg_position": it.get("avg_position"),
                "median_position": it.get("median_position"),
                "relevant_serp_items": it.get("relevant_serp_items"),
                "visibility": it.get("visibility"),
                "organic_keywords": met.get("count") or met.get("pos_1"),
                "etv": met.get("etv"),
            })
        return {"items": out, "cost": d.get("cost")}

    def serp_organic(self, query: str, depth: int = 10) -> list[dict]:
        """Top ORGANIC Google results for a free-text query → [{domain, url, title, rank}].
        Used by the website-finder to locate a booked prospect's real site by Googling the
        business name (+ suburb/state). AU-scoped by the client's location/language. Returns
        organic items only (ads / maps-pack / PAA stripped). Raises only on a task-level error;
        the caller guards. `depth` is clamped 10-30 (one SERP page is plenty to find a homepage)."""
        kw = " ".join((query or "").split())
        if not kw:
            return []
        body = [{"keyword": kw[:700], "location_code": self.loc, "language_code": self.lang,
                 "depth": min(max(depth, 10), 30), "device": "desktop"}]
        d = self._post("/v3/serp/google/organic/live/advanced", body)
        task = (d.get("tasks") or [{}])[0]
        sc = task.get("status_code")
        if sc is not None and int(sc) >= 40000:
            raise RuntimeError(f"DataForSEO serp_organic error {sc}: {task.get('status_message')}")
        res = self._first_result(d)
        out = []
        for it in (res.get("items") or []):
            if (it.get("type") or "") != "organic":
                continue
            dom = (it.get("domain") or "").lower().removeprefix("www.")
            if not dom:
                continue
            out.append({"domain": dom, "url": it.get("url"), "title": it.get("title"),
                        "rank": it.get("rank_group") or it.get("rank_absolute")})
        return out

    def backlinks_summary(self, domain: str) -> dict:
        """Domain authority + backlink profile (rank, referring domains, backlinks count)."""
        body = [{"target": domain, "internal_list_limit": 1, "backlinks_status_type": "live"}]
        d = self._post("/v3/backlinks/summary/live", body)
        res = self._first_result(d)
        return {"rank": res.get("rank"), "backlinks": res.get("backlinks"),
                "referring_domains": res.get("referring_domains"),
                "referring_main_domains": res.get("referring_main_domains"),
                "referring_pages": res.get("referring_pages"),
                "broken_backlinks": res.get("broken_backlinks"), "cost": d.get("cost")}

    def enrich_domain(self, domain: str, with_backlinks: bool = False) -> dict:
        """Combined SEO + Transparency Center enrichment for a domain. Never raises.
        (Backlinks API needs a separate DataForSEO subscription this account lacks -> off.)"""
        out: dict = {"found": False, "fetched_at": datetime.now(timezone.utc).isoformat()}
        try:
            out["rank"] = self.domain_rank_overview(domain)
            out["found"] = True
        except Exception as exc:
            log.warning("dataforseo_rank_failed", domain=domain, error=str(exc)[:160])
        try:
            out["ads"] = self.ads_search(domain)
            out["found"] = True
        except Exception as exc:
            log.warning("dataforseo_ads_failed", domain=domain, error=str(exc)[:160])
        if with_backlinks:
            try:
                out["backlinks"] = self.backlinks_summary(domain)
            except Exception as exc:
                log.warning("dataforseo_backlinks_failed", domain=domain, error=str(exc)[:160])
        # headline verdict
        ads = out.get("ads") or {}
        out["running_google_ads"] = bool(ads.get("running_ads"))
        return out


# CTR by organic position — INDUSTRY-AVERAGE ASSUMPTION (surfaced/labeled in the UI). Used to
# ESTIMATE organic traffic (search_volume x CTR) instead of buying DataForSEO's traffic metric.
_ORGANIC_CTR = {1: 0.28, 2: 0.15, 3: 0.10, 4: 0.07, 5: 0.05,
                6: 0.04, 7: 0.03, 8: 0.025, 9: 0.02, 10: 0.018}


def _ctr(pos: float) -> float:
    p = int(pos)
    if p <= 10:
        return _ORGANIC_CTR.get(p, 0.02)
    if p <= 20:
        return 0.010
    if p <= 30:
        return 0.006
    if p <= 50:
        return 0.003
    return 0.001


# ============================================================================================ #
# RELEVANCE — "money keyword" scoring, brand stripping, mega-domain filter
# (shared by the SEO audit here AND the competitor keyword-gap in competitor.py)
# ============================================================================================ #
MIN_MONEY_VOL = 150          # ignore tiny / no-volume terms — not worth a sales conversation
TOP_MONEY_KW = 8             # how many keywords we SURFACE per list (quality over quantity)

# Corporate/geo filler stripped when deriving a brand's distinctive tokens from its legal name.
_BRAND_STOP = {"pty", "ltd", "limited", "group", "the", "for", "trustee", "proprietary", "co",
               "company", "australia", "australian", "holdings", "inc", "llc", "corporation",
               "corp", "services", "service", "international", "global", "au", "com", "and",
               "enterprises", "industries", "solutions", "trading", "brothers"}

# Generic mega-sites that rank for EVERYTHING — never a meaningful "competitor" or gap source.
# (Specialty/vertical retailers like bunnings/myer/reece ARE kept — they're real competitors.)
MEGA_DOMAINS = {
    "youtube.com", "facebook.com", "instagram.com", "tiktok.com", "twitter.com", "x.com",
    "pinterest.com", "pinterest.com.au", "reddit.com", "linkedin.com", "snapchat.com",
    "wikipedia.org", "quora.com", "medium.com", "wordpress.com", "blogspot.com", "tumblr.com",
    "google.com", "google.com.au", "bing.com", "yahoo.com", "duckduckgo.com",
    "amazon.com", "amazon.com.au", "ebay.com", "ebay.com.au", "aliexpress.com", "temu.com",
    "wish.com", "etsy.com", "alibaba.com", "catch.com.au",
    "yelp.com", "yellowpages.com.au", "whitepages.com.au", "gumtree.com.au", "tripadvisor.com",
    "tripadvisor.com.au", "trustpilot.com", "productreview.com.au", "indeed.com", "seek.com.au",
    "glassdoor.com", "news.com.au", "abc.net.au", "theguardian.com", "dailymail.co.uk",
    "9news.com.au", "7news.com.au", "smh.com.au", "theage.com.au", "apple.com", "microsoft.com",
}
_MEGA_KW_CEILING = 1_500_000   # any domain ranking for >1.5M keywords is a generic portal, not a rival


def is_mega_domain(host: str, organic_keywords: int | None = None) -> bool:
    """True for generic-everything portals (explicit list OR absurd keyword footprint)."""
    h = (host or "").lower().lstrip("www.")
    if h in MEGA_DOMAINS or any(h == m or h.endswith("." + m) for m in MEGA_DOMAINS):
        return True
    return bool(organic_keywords and organic_keywords > _MEGA_KW_CEILING)


def brand_tokens(domain: str, company_name: str = "") -> set[str]:
    """Distinctive brand strings a domain already 'owns' (its own name) — stripped from
    OPPORTUNITY keywords so we surface demand the prospect is NOT already capturing.
    Emits the concatenated domain root (e.g. 'tripadeal', 'intrepidtravel' — matches 'trip a
    deal', 'intrepid travel' once spaces are removed) plus a prefix-stripped variant so
    'theiconic' also yields 'iconic'."""
    toks: set[str] = set()
    root = re.sub(r"^www\.", "", (domain or "").lower().split("/")[0]).split(".")[0]
    if len(root) >= 4:
        toks.add(root)
        for pre in ("the", "my", "go", "get", "shop", "buy"):   # brand often searched sans prefix
            if root.startswith(pre) and len(root) - len(pre) >= 4:
                toks.add(root[len(pre):])
    for w in re.split(r"[^a-z0-9]+", (company_name or "").lower()):
        if len(w) >= 4 and w not in _BRAND_STOP:
            toks.add(w)
    return toks


def is_branded(keyword: str, brands: set[str]) -> bool:
    """True if the keyword contains a brand token (the prospect already ranks for its own name)."""
    if not brands:
        return False
    joined = re.sub(r"[^a-z0-9]+", "", (keyword or "").lower())
    return any(b in joined for b in brands if len(b) >= 4)


def money_score(kw: dict) -> float:
    """$-weighted value of a keyword = monthly searches x commercial value (CPC). Informational
    terms (cpc=0) are kept but heavily discounted so commercial 'money' terms surface first."""
    vol = kw.get("search_volume") or 0
    cpc = kw.get("cpc") or 0
    return vol * cpc if cpc and cpc > 0 else vol * 0.12


# ============================================================================================ #
# KEYWORD CLUSTERING — group a flat keyword-demand list into product/service themes.
# Self-contained (own stop/geo sets) so there is no import cycle with audit.py; used by
# keyword_clusters() above AND by audit.discover_demand_keywords to cluster the FILTERED demand.
# ============================================================================================ #
_CLUSTER_STOP = {
    "the", "and", "for", "with", "your", "you", "our", "are", "can", "get", "from", "that", "this",
    "near", "me", "in", "on", "of", "to", "a", "an", "at", "by", "or", "is", "best", "top", "cheap",
    "price", "prices", "pricing", "cost", "costs", "new", "used", "how", "what", "why", "vs", "versus",
    "my", "we", "per", "australia", "australian", "au", "com", "www", "sale", "buy",
}
_CLUSTER_GEO = {
    "sydney", "melbourne", "brisbane", "perth", "adelaide", "canberra", "hobart", "darwin", "gold",
    "coast", "newcastle", "wollongong", "geelong", "nsw", "vic", "qld", "wa", "sa", "tas", "act", "nt",
    "australia",
}


def _cluster_stem(t: str) -> str:
    """Cheap singularisation so 'tool'/'tools' and 'scaffold'/'scaffolds' cluster together."""
    if len(t) > 4 and t.endswith("s") and not t.endswith("ss"):
        return t[:-3] if t.endswith("ies") else t[:-1]
    return t


def _cluster_sig_tokens(kw: str) -> list[str]:
    toks = re.findall(r"[a-z][a-z]+", (kw or "").lower())
    return [_cluster_stem(t) for t in toks if len(t) > 2 and t not in _CLUSTER_STOP and t not in _CLUSTER_GEO]


def cluster_keywords(keywords: list[dict], *, max_clusters: int = 10, min_cluster: int = 2) -> list[dict]:
    """Group keyword rows (each {keyword, search_volume|volume, cpc}) into product/service THEME clusters by
    their most DISTINCTIVE shared token — so a no-footprint audit can still show the SHAPE of a market's
    demand. Each cluster: {label, keywords_count, volume, avg_cpc, value, examples[]}. Ordered by commercial
    value (monthly volume × average CPC); singletons collapse into a 'Long-tail / other' bucket. Pure +
    fully guarded — returns [] on any failure and never raises."""
    try:
        rows = []
        for r in (keywords or []):
            vol = r.get("search_volume") or r.get("volume") or 0
            if not r.get("keyword") or vol <= 0:
                continue
            rows.append({"keyword": r.get("keyword"), "volume": vol, "cpc": r.get("cpc") or 0,
                         "_st": _cluster_sig_tokens(r.get("keyword"))})
        if not rows:
            return []
        freq: Counter = Counter()
        for r in rows:
            for t in set(r["_st"]):
                freq[t] += 1
        n = len(rows)
        # a token in >40% of the set is too generic to be a THEME (e.g. 'hire' for a hire company) — cluster
        # on the most distinctive shared token instead so one head word doesn't swallow everything.
        generic = {t for t, c in freq.items() if n and c > 0.40 * n}
        surface: dict = {}
        for r in rows:
            for w in re.findall(r"[a-z][a-z]+", r["keyword"].lower()):
                if len(w) > 2 and w not in _CLUSTER_STOP and w not in _CLUSTER_GEO:
                    surface.setdefault(_cluster_stem(w), w)
        buckets: dict = defaultdict(list)
        for r in rows:
            cand = [t for t in r["_st"] if t not in generic] or r["_st"]
            key = max(cand, key=lambda t: (freq[t], len(t))) if cand else "other"
            buckets[key].append(r)

        def _agg(key, brows):
            vol = sum(r["volume"] for r in brows)
            cpcs = [r["cpc"] for r in brows if r["cpc"]]
            avg_cpc = round(sum(cpcs) / len(cpcs), 2) if cpcs else 0.0
            ex = [r["keyword"] for r in sorted(brows, key=lambda x: x["volume"], reverse=True)[:5]]
            return {"label": (surface.get(key, key) or key).title(), "keywords_count": len(brows),
                    "volume": vol, "avg_cpc": avg_cpc, "value": round(vol * avg_cpc), "examples": ex}

        main, longtail = [], []
        for key, brows in buckets.items():
            (longtail.extend(brows) if (len(brows) < min_cluster or key == "other") else main.append((key, brows)))
        clusters = [_agg(k, b) for k, b in main]
        clusters.sort(key=lambda c: (c["value"], c["volume"]), reverse=True)
        clusters = clusters[:max_clusters]
        if longtail:
            lt = _agg("other", longtail); lt["label"] = "Long-tail / other"; clusters.append(lt)
        return clusters
    except Exception:
        return []


def build_seo_audit(keywords: list[dict], *, brands: set[str] | None = None,
                    top: int = TOP_MONEY_KW) -> dict:
    """Turn a domain's ranked keywords into a MONEY-keyword SEO audit — only the handful of
    high-value, non-branded, winnable terms that make a sales conversation, not a 200-row dump.

    Headline totals (traffic/value/counts) are computed over ALL keywords; the SURFACED lists are
    filtered to non-branded terms with real volume and ranked by $ value (searches x CPC):
      * money_keywords — position 4-15, high $ value: one page-1 push = a near-term money win
      * proof_winning  — position 1-3 for $ terms: proof the site CAN rank (credibility for the pitch)
      * growth         — position 16-30: bigger, longer plays
    Traffic is ESTIMATED (search_volume x an assumed position-CTR curve), labelled as such."""
    brands = brands or set()
    winning, quick, growth = [], [], []
    est_traffic = est_value = quickwin_upside = 0.0
    n_win = n_quick = n_growth = 0
    for k in keywords or []:
        vol = k.get("search_volume") or 0
        pos = k.get("position") or 999
        cpc = k.get("cpc") or 0
        et = vol * _ctr(pos)
        est_traffic += et
        est_value += et * cpc
        # headline counts over ALL keywords (before relevance filtering)
        if pos <= 3:
            n_win += 1
        elif pos <= 15:
            n_quick += 1
        elif pos <= 30:
            n_growth += 1
        # relevance/money filter for the SURFACED lists
        if vol < MIN_MONEY_VOL or is_branded(k.get("keyword"), brands):
            continue
        row = {"keyword": k.get("keyword"), "position": pos, "search_volume": vol,
               "cpc": round(cpc, 2), "url": k.get("url"), "est_traffic": round(et),
               "money_value": round(vol * cpc)}   # $ value of this keyword's traffic
        if pos <= 3:
            winning.append(row)
        elif pos <= 15:
            up = max(vol * _ctr(3) - et, 0)          # traffic if it reached position 3
            row["upside_traffic"] = round(up)
            row["upside_value"] = round(up * cpc)
            quickwin_upside += up
            quick.append(row)
        elif pos <= 30:
            growth.append(row)
    quick.sort(key=lambda r: (money_score(r), r.get("upside_traffic", 0)), reverse=True)
    winning.sort(key=lambda r: money_score(r), reverse=True)
    growth.sort(key=lambda r: money_score(r), reverse=True)
    return {
        "assumption": ("Traffic is ESTIMATED from search volume x an industry-average "
                       "click-through-rate by position — an assumption, not a measured metric. "
                       "We surface only high-value, non-branded keywords (ranked by search "
                       "volume x CPC), not every term the site ranks for."),
        "totals": {
            "keywords": len(keywords or []),
            "est_organic_traffic": round(est_traffic),
            "est_traffic_value": round(est_value),
            "winning": n_win, "quick_wins": n_quick, "growth": n_growth,
            "quickwin_upside_traffic": round(quickwin_upside),
        },
        "money_keywords": quick[:top],   # THE list — high-$ winnable terms
        "proof_winning": winning[:5],    # they already rank top-3 for these $ terms
        "growth": growth[:5],            # secondary, bigger plays
        # legacy keys kept so an un-migrated UI still renders (map to the new curated lists)
        "quick_wins": quick[:top],
        "winning": winning[:5],
    }


def _parse_dt(v) -> datetime | None:
    if not v:
        return None
    s = str(v).strip().replace("Z", "+00:00")
    for fmt in ("%Y-%m-%d %H:%M:%S %z", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(s, fmt)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    try:
        dt = datetime.fromisoformat(s)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None
