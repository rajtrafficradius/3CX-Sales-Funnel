"""SEMrush Analytics API client — domain marketing metrics (SEO + paid + backlinks).

Pulls the domain overview (organic/paid traffic, keywords, rank) and the backlinks
summary (authority score, backlinks, referring domains). Uses SEMrush API *units*
(billed by SEMrush — expected; this is NOT Apollo credits). Modeled on the 3CX
client (httpx + tenacity). Responses are semicolon-delimited CSV.
"""

from __future__ import annotations

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

from ..config import Settings
from ..logging import get_logger

log = get_logger(__name__)

_BASE = "https://api.semrush.com/"                  # domain/keyword analytics reports
_BACKLINKS_BASE = "https://api.semrush.com/analytics/v1/"  # backlinks analytics (separate API)
# Domain Overview columns: Domain, Rank, Organic kw, Organic traffic, Organic cost,
# Adwords kw, Adwords traffic, Adwords cost.
_OVERVIEW_COLS = "Dn,Rk,Or,Ot,Oc,Ad,At,Ac"
# Backlinks overview: authority score, total backlinks, referring domains, ref. urls, follow/nofollow.
_BACKLINK_COLS = "ascore,total,domains_num,urls_num,follows_num,nofollows_num"


def _num(d: dict, k: str):
    v = d.get(k)
    if v in (None, ""):
        return None
    try:
        f = float(v)
        return int(f) if f.is_integer() else f
    except (TypeError, ValueError):
        return None


class SemrushClient:
    def __init__(self, settings: Settings):
        self._key = settings.semrush_api_key
        # Regional database — an AU agency calling AU businesses. SEMrush requires one
        # for domain_ranks; "au" is the right default here.
        self._db = "au"
        self._client = httpx.Client(timeout=30.0)

    def __enter__(self) -> "SemrushClient":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    def close(self) -> None:
        try:
            self._client.close()
        except Exception:
            pass

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=8), reraise=True)
    def _raw(self, base: str, params: dict) -> str:
        resp = self._client.get(base, params={"key": self._key, **params})
        resp.raise_for_status()
        return resp.text

    def _report(self, base: str, params: dict) -> dict:
        """First CSV row as a dict (SEMrush reports here are 1 row). Resilient: a
        failed report (4xx, no data, ERROR text) returns {} so the other still works."""
        try:
            text = (self._raw(base, params) or "").strip()
        except Exception as exc:
            log.warning("semrush_report_failed", type=params.get("type"), error=str(exc)[:160])
            return {}
        if not text or text.upper().startswith("ERROR") or text.lower().startswith("query type"):
            return {}
        lines = text.splitlines()
        if len(lines) < 2:
            return {}
        headers = lines[0].split(";")
        values = lines[1].split(";")
        return {h: v for h, v in zip(headers, values)}

    def domain_overview(self, domain: str) -> dict:
        return self._report(_BASE, {
            "type": "domain_ranks", "domain": domain,
            "database": self._db, "export_columns": _OVERVIEW_COLS,
        })

    def backlinks_overview(self, domain: str) -> dict:
        return self._report(_BACKLINKS_BASE, {
            "type": "backlinks_overview", "target": domain,
            "target_type": "root_domain", "export_columns": _BACKLINK_COLS,
        })

    def enrich(self, domain: str, *, want_overview: bool = True, want_backlinks: bool = True) -> dict:
        """SEMrush metrics for a domain, normalized for display.

        GAP-AWARE / credit-saving: callers pass want_overview=False when the master
        DB already carries the overview metrics (organic traffic/keywords/cost,
        adwords) — that skips the domain_ranks report and fetches only the backlinks
        report (authority/backlinks/referring domains, which the master CSV lacks).
        See the 3cx-funnel-enrichment-gapfill note."""
        ov = self.domain_overview(domain) if want_overview else {}
        bl = self.backlinks_overview(domain) if want_backlinks else {}
        if not ov and not bl:
            return {"found": False, "skipped_overview": not want_overview}
        # NOTE: domain_ranks returns *human* header names; backlinks returns raw codes.
        return {
            "found": True,
            "database": self._db,
            "skipped_overview": not want_overview,
            "rank": _num(ov, "Rank"),
            "organic_keywords": _num(ov, "Organic Keywords"),
            "organic_traffic": _num(ov, "Organic Traffic"),
            "organic_cost": _num(ov, "Organic Cost"),   # est. monthly value of organic traffic (USD)
            "paid_keywords": _num(ov, "Adwords Keywords"),
            "paid_traffic": _num(ov, "Adwords Traffic"),
            "paid_cost": _num(ov, "Adwords Cost"),       # running Google Ads (paid presence) if > 0
            "authority_score": _num(bl, "ascore"),
            "backlinks": _num(bl, "total"),
            "referring_domains": _num(bl, "domains_num"),
            "raw": {"overview": ov, "backlinks": bl},
        }
