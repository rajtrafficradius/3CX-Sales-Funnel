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
