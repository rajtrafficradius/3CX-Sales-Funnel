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

    def ranked_keywords(self, domain: str, limit: int = 200) -> dict:
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


def build_seo_audit(keywords: list[dict]) -> dict:
    """Turn a domain's ranked keywords into a quick-wins -> growth SEO audit.

    Traffic is ESTIMATED (search_volume x an assumed position-CTR curve) — NOT a measured metric;
    the caller/UI labels it as an assumption. Buckets each keyword:
      * winning   — position 1-3 (already on page-1 top)
      * quick_win — position 4-15 with real volume: one page-1 push = a big, near-term traffic gain
      * growth    — position 16-50: longer-term opportunities
    Also estimates total monthly organic traffic, its $ value (traffic x CPC), and the EXTRA
    traffic available if the quick-wins reached position 3 (the actionable upside)."""
    winning, quick, growth = [], [], []
    est_traffic = est_value = quickwin_upside = 0.0
    for k in keywords or []:
        vol = k.get("search_volume") or 0
        pos = k.get("position") or 999
        cpc = k.get("cpc") or 0
        et = vol * _ctr(pos)
        est_traffic += et
        est_value += et * cpc
        row = {**k, "est_traffic": round(et)}
        if pos <= 3:
            winning.append(row)
        elif pos <= 15:
            up = max(vol * _ctr(3) - et, 0)          # traffic if it reached position 3
            row["upside_traffic"] = round(up)
            row["upside_value"] = round(up * cpc)
            quickwin_upside += up
            quick.append(row)
        elif pos <= 50:
            growth.append(row)
    quick.sort(key=lambda r: r.get("upside_traffic", 0), reverse=True)
    growth.sort(key=lambda r: r.get("search_volume", 0), reverse=True)
    winning.sort(key=lambda r: r.get("est_traffic", 0), reverse=True)
    return {
        "assumption": ("Traffic is ESTIMATED from search volume x an industry-average "
                       "click-through-rate by position — an assumption, not a measured metric."),
        "totals": {
            "keywords": len(keywords or []),
            "est_organic_traffic": round(est_traffic),
            "est_traffic_value": round(est_value),
            "winning": len(winning), "quick_wins": len(quick), "growth": len(growth),
            "quickwin_upside_traffic": round(quickwin_upside),
        },
        "winning": winning[:15],
        "quick_wins": quick[:20],
        "growth": growth[:20],
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
