"""Live site-signal fetchers for the growth audit (PageSpeed/Core Web Vitals first; GEO/AEO/technical
to follow). Each fetcher is GUARDED (never raises, returns {} on failure) and JSON-serialisable, and is
cached per-domain in the `enrichment` table so a regenerated audit doesn't re-hit the APIs. Real data
only — an empty result means "omit the section", never "fabricate"."""
from __future__ import annotations

import json
import time as _time

try:
    import structlog
    _log = structlog.get_logger("audit_signals")
except Exception:                                   # pragma: no cover
    import logging
    _log = logging.getLogger("audit_signals")

import httpx

# --------------------------------------------------------------------------- #
# PageSpeed Insights (Lighthouse lab + CrUX field)
# --------------------------------------------------------------------------- #
_PSI_URL = "https://www.googleapis.com/pagespeedonline/v5/runPagespeed"
_LAB_KEYS = {
    "largest-contentful-paint": "lcp_ms", "cumulative-layout-shift": "cls",
    "total-blocking-time": "tbt_ms", "first-contentful-paint": "fcp_ms",
    "speed-index": "si_ms", "interactive": "tti_ms",
}
_OPP_FRIENDLY = {
    "render-blocking-resources": "Scripts &amp; styles are blocking the first paint",
    "uses-optimized-images": "Images aren't compressed for the web",
    "uses-responsive-images": "Over-sized images are being shipped to phones",
    "offscreen-images": "Below-the-fold images load too early",
    "unused-css-rules": "Unused CSS is being downloaded",
    "unused-javascript": "Unused JavaScript is being downloaded",
    "unminified-css": "Stylesheets aren't minified",
    "unminified-javascript": "Scripts aren't minified",
    "modern-image-formats": "Images aren't in modern formats (WebP/AVIF)",
    "uses-text-compression": "Text files aren't gzip/Brotli compressed",
    "server-response-time": "Your server is slow to send the first byte",
    "redirects": "Extra redirects are slowing the first load",
    "total-byte-weight": "The page is simply too heavy",
    "dom-size": "The page has an excessively large DOM",
}
# audits Technical health reads back out of the same PSI run (no second call)
_TECH_AUDIT_IDS = ("viewport", "is-crawlable", "robots-txt", "structured-data", "image-alt",
                   "document-title", "meta-description", "http-status-code", "crawlable-anchors",
                   "hreflang", "canonical", "is-on-https")


def _psi_call(client, url, strategy, key):
    params = [("url", url), ("strategy", strategy)]
    for cat in ("performance", "seo", "accessibility", "best-practices"):
        params.append(("category", cat))
    if key:
        params.append(("key", key))
    try:
        r = client.get(_PSI_URL, params=params)
        if r.status_code != 200:
            return {}
        data = r.json()
        return data if isinstance(data, dict) and data.get("lighthouseResult") else {}
    except Exception:
        return {}


def _extract_strategy(data):
    lh = data.get("lighthouseResult") or {}
    cats = lh.get("categories") or {}

    def _score(name):
        s = (cats.get(name) or {}).get("score")
        return round(float(s) * 100) if isinstance(s, (int, float)) else None

    audits = lh.get("audits") or {}
    lab = {}
    for aud_id, field in _LAB_KEYS.items():
        v = (audits.get(aud_id) or {}).get("numericValue")
        if isinstance(v, (int, float)):
            lab[field] = round(v, 3) if field == "cls" else int(round(v))
    opps = []
    for aud_id, aud in audits.items():
        det = aud.get("details") or {}
        if det.get("type") != "opportunity":
            continue
        saved = det.get("overallSavingsMs")
        if not isinstance(saved, (int, float)) or saved < 150:
            continue
        opps.append({"id": aud_id, "label": _OPP_FRIENDLY.get(aud_id) or (aud.get("title") or aud_id),
                     "savings_ms": int(round(saved))})
    opps.sort(key=lambda o: o["savings_ms"], reverse=True)
    # slim technical audit slice Technical-health reads (score + verdict), no second PSI call
    tech = {}
    for aud_id in _TECH_AUDIT_IDS:
        a = audits.get(aud_id)
        if not a:
            continue
        tech[aud_id] = {"score": a.get("score"), "title": a.get("title")}
    return {"performance": _score("performance"), "seo": _score("seo"),
            "accessibility": _score("accessibility"), "best_practices": _score("best-practices"),
            "lab": lab, "opportunities": opps[:5], "seo_audits": tech}


def _extract_field(data):
    """Real-user CrUX. Prefer page-level, fall back to origin-level; {} when Google has none."""
    for scope, block in (("page", data.get("loadingExperience")),
                         ("origin", data.get("originLoadingExperience"))):
        metrics = (block or {}).get("metrics") or {}
        if not metrics:
            continue
        lcp = metrics.get("LARGEST_CONTENTFUL_PAINT_MS") or {}
        inp = (metrics.get("INTERACTION_TO_NEXT_PAINT")
               or metrics.get("EXPERIMENTAL_INTERACTION_TO_NEXT_PAINT") or {})
        cls = metrics.get("CUMULATIVE_LAYOUT_SHIFT_SCORE") or {}
        out = {"scope": scope, "overall_category": (block or {}).get("overall_category")}
        if lcp.get("percentile") is not None:
            out["lcp_ms"] = int(lcp["percentile"]); out["lcp_cat"] = lcp.get("category")
        if inp.get("percentile") is not None:
            out["inp_ms"] = int(inp["percentile"]); out["inp_cat"] = inp.get("category")
        if cls.get("percentile") is not None:
            out["cls"] = round(cls["percentile"] / 100.0, 3); out["cls_cat"] = cls.get("category")
        cats = [out.get(k) for k in ("lcp_cat", "inp_cat", "cls_cat") if out.get(k)]
        if cats:
            out["cwv_pass"] = all(c == "FAST" for c in cats)
        return out
    return {}


def fetch_pagespeed(url, settings):
    """GUARDED. Google PageSpeed Insights v5 for MOBILE + DESKTOP — lab metrics, CrUX field data and the
    biggest speed opportunities. Key first (google_places_api_key, if PSI enabled), keyless fallback.
    JSON-serialisable; returns {} when nothing was measurable."""
    if not url:
        return {}
    if not url.startswith(("http://", "https://")):
        url = "https://" + str(url).strip()
    key = (getattr(settings, "google_places_api_key", "") or "").strip() or None
    key_attempts = [key, None] if key else [None]
    out = {"url": url, "fetched_at": int(_time.time()), "used_key": False,
           "mobile": {}, "desktop": {}, "field": {}}
    got_any = False
    try:
        with httpx.Client(timeout=60.0, follow_redirects=True,
                          headers={"User-Agent": "TrafficRadius-Audit/1.0"}) as client:
            for strategy in ("mobile", "desktop"):
                data = {}
                for attempt_key in key_attempts:
                    data = _psi_call(client, url, strategy, attempt_key)
                    if data:
                        out["used_key"] = out["used_key"] or bool(attempt_key)
                        break
                if not data:
                    continue
                got_any = True
                out[strategy] = _extract_strategy(data)
                if strategy == "mobile" or not out["field"]:
                    fld = _extract_field(data)
                    if fld:
                        out["field"] = fld
    except Exception as exc:
        _log.warning("pagespeed_fetch_failed", error=str(exc)[:160])
    if not got_any:
        return {}
    out["field_available"] = bool(out.get("field"))
    return out


# --------------------------------------------------------------------------- #
# Per-domain cache (reuses the existing `enrichment` table — additive columns)
# --------------------------------------------------------------------------- #
def _ensure_cache_columns(pool):
    try:
        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute("ALTER TABLE enrichment ADD COLUMN IF NOT EXISTS pagespeed jsonb")
            cur.execute("ALTER TABLE enrichment ADD COLUMN IF NOT EXISTS pagespeed_at timestamptz")
            conn.commit()
    except Exception as exc:
        _log.warning("audit_signals_ensure_columns_failed", error=str(exc)[:140])


def get_or_fetch_pagespeed(pool, domain, settings, *, ttl_days: int = 7, force: bool = False) -> dict:
    """Cached PageSpeed for a domain. Fresh cache (< ttl_days) is returned as-is; otherwise fetch live and
    upsert. Never raises; returns {} if PSI yields nothing."""
    if not domain:
        return {}
    _ensure_cache_columns(pool)
    if not force:
        try:
            with pool.connection() as conn, conn.cursor() as cur:
                cur.execute("SELECT pagespeed FROM enrichment WHERE domain=%s "
                            "AND pagespeed IS NOT NULL AND pagespeed_at > now() - (%s || ' days')::interval",
                            (domain, str(int(ttl_days))))
                row = cur.fetchone()
                if row and row[0]:
                    return row[0] if isinstance(row[0], dict) else json.loads(row[0])
        except Exception as exc:
            _log.warning("pagespeed_cache_read_failed", domain=domain, error=str(exc)[:140])
    data = fetch_pagespeed("https://" + str(domain).strip().lstrip("https://").lstrip("http://"), settings)
    if not data:
        return {}
    try:
        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO enrichment (domain, pagespeed, pagespeed_at, fetched_at) "
                "VALUES (%s, %s, now(), now()) "
                "ON CONFLICT (domain) DO UPDATE SET pagespeed=EXCLUDED.pagespeed, pagespeed_at=now()",
                (domain, json.dumps(data)))
            conn.commit()
    except Exception as exc:
        _log.warning("pagespeed_cache_write_failed", domain=domain, error=str(exc)[:140])
    return data
