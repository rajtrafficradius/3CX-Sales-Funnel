"""FREE website intelligence — fetch a domain's homepage and detect marketing tech.

No paid API. We pull the raw HTML (and inline/script src URLs) over plain HTTP and
pattern-match the publicly-visible tracking snippets every site ships in its source:
Google Tag Manager, GA4, Google Ads conversion/remarketing, Meta/Facebook Pixel,
LinkedIn Insight, TikTok Pixel, Microsoft/Bing UET, Hotjar. The presence of a Google
Ads tag or Meta Pixel is a strong, free signal that the business is actively running
PERFORMANCE MARKETING (paid ads) — which we surface on the prospect page and feed the
"runs_paid_ads" intelligence. Also harvests contact emails and social links.

This is intentionally dependency-light (httpx + regex, both already in the project) so
it scales to many domains for free. Robust by design: any fetch error returns a
structured 'error' result rather than raising.
"""

from __future__ import annotations

import re
import time

import httpx

from ..logging import get_logger

log = get_logger(__name__)

# Present as a real browser. A self-identifying bot UA gets 403'd by Cloudflare/WAF on a large
# share of small-business sites, which was silently zeroing their website scan + business intel.
# We only read publicly-served homepages, so a standard browser UA + Accept headers is correct.
BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-AU,en;q=0.9",
}

# Each tracker: (key, human label, list of regex signatures found in page HTML/JS).
_TRACKERS: list[tuple[str, str, list[str]]] = [
    ("gtm", "Google Tag Manager", [r"googletagmanager\.com/gtm\.js", r"GTM-[A-Z0-9]{4,}"]),
    ("gsc", "Google Search Console (site verification)", [r"google-site-verification"]),
    ("ga4", "Google Analytics 4", [r"googletagmanager\.com/gtag/js", r"gtag/js\?id=G-", r"\bG-[A-Z0-9]{8,}\b"]),
    ("ua", "Universal Analytics (legacy)", [r"google-analytics\.com/analytics\.js", r"\bUA-\d{4,}-\d+\b"]),
    ("google_ads", "Google Ads (conversion/remarketing)",
        [r"googleadservices\.com/pagead/conversion", r"\bAW-\d{6,}\b", r"googletagmanager\.com/gtag/js\?id=AW-"]),
    ("meta_pixel", "Meta / Facebook Pixel",
        [r"connect\.facebook\.net/[^\"']*/fbevents\.js", r"\bfbq\(", r"facebook\.com/tr\?id="]),
    ("linkedin", "LinkedIn Insight Tag", [r"snap\.licdn\.com", r"_linkedin_partner_id"]),
    ("tiktok", "TikTok Pixel", [r"analytics\.tiktok\.com", r"ttq\.load\("]),
    ("bing_uet", "Microsoft/Bing UET", [r"bat\.bing\.com", r"\buetq\b"]),
    ("hotjar", "Hotjar", [r"static\.hotjar\.com", r"\bhj\("]),
]
# Trackers that specifically indicate PAID ADVERTISING (not just analytics).
_PAID_ADS_KEYS = {"google_ads", "meta_pixel", "tiktok", "bing_uet", "linkedin"}

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_SOCIAL_RE = {
    "facebook": re.compile(r"https?://(?:www\.)?facebook\.com/[A-Za-z0-9_.\-/]+"),
    "instagram": re.compile(r"https?://(?:www\.)?instagram\.com/[A-Za-z0-9_.\-/]+"),
    "linkedin": re.compile(r"https?://(?:www\.)?linkedin\.com/(?:company|in)/[A-Za-z0-9_.\-/]+"),
    "youtube": re.compile(r"https?://(?:www\.)?youtube\.com/[A-Za-z0-9_.\-/@]+"),
}
_UTM_RE = re.compile(r"utm_(?:source|medium|campaign)=")


def _detect(html: str) -> dict:
    found, ids = {}, {}
    for key, label, sigs in _TRACKERS:
        hit = any(re.search(s, html, re.IGNORECASE) for s in sigs)
        if hit:
            found[key] = label
            # capture the first concrete id where the signature exposes one
            for idre in (r"GTM-[A-Z0-9]{4,}", r"\bG-[A-Z0-9]{8,}\b", r"\bAW-\d{6,}\b", r"\bUA-\d{4,}-\d+\b"):
                m = re.search(idre, html)
                if m and key in ("gtm", "ga4", "google_ads", "ua"):
                    ids.setdefault(key, m.group(0))
    paid = sorted(found[k] for k in found if k in _PAID_ADS_KEYS)
    emails = sorted({e.lower() for e in _EMAIL_RE.findall(html)
                     if not e.lower().endswith((".png", ".jpg", ".gif", ".svg", ".webp"))})[:10]
    socials = {}
    for net, rx in _SOCIAL_RE.items():
        m = rx.search(html)
        if m:
            socials[net] = m.group(0)
    return {
        "trackers": found,           # {key: label}
        "tracker_ids": ids,          # {key: GTM-XXXX / AW-XXXX ...}
        "runs_paid_ads": bool(paid), # any ad/remarketing pixel present
        "paid_ad_platforms": paid,   # human labels of the ad pixels found
        "uses_utm": bool(_UTM_RE.search(html)),
        "emails": emails,
        "socials": socials,
    }


async def afetch_website_intel(client, domain: str, *, max_bytes: int = 2_000_000) -> dict:
    """Async twin of fetch_website_intel for high-concurrency bulk scanning.

    Reuses the SAME `_detect()` so output matches the sync scanner. Uses a plain
    (non-streaming) GET: streaming + a manual early-break left a dangling connection
    that hung the client when a slow-trickle host was cancelled — non-streaming GET
    cancels cleanly, which is essential for unattended 1M-scale runs. The total time
    per domain is bounded by the caller's `asyncio.wait_for`. `max_bytes` truncates the
    in-memory HTML before detection (caps regex cost; 2 MB preserves parity). `client`
    is a shared httpx.AsyncClient (timeouts/limits/UA set by the caller). Never raises.
    """
    if not domain:
        return {"found": False, "status": "no_domain"}
    last_err = None
    for scheme in ("https", "http"):
        url = f"{scheme}://{domain}"
        try:
            _t0 = time.perf_counter()
            resp = await client.get(url)
            _load_ms = int((time.perf_counter() - _t0) * 1000)
            html = resp.text or ""
            if len(html) > max_bytes:
                html = html[:max_bytes]
            det = _detect(html)
            return {
                "found": True, "status": "ok", "final_url": str(resp.url),
                "http_status": resp.status_code, "html_bytes": len(html),
                "load_ms": _load_ms, **_health(html, str(resp.url)), **det,
            }
        except Exception as exc:
            last_err = str(exc)[:160]
            continue
    return {"found": False, "status": "error", "error": last_err}


def _health(html: str, final_url: str) -> dict:
    """Website-health signals for the Lisa-4 audit: is it secure (HTTPS), mobile-ready (viewport), titled."""
    h = html or ""
    hl = h.lower()
    m = re.search(r"<title[^>]*>(.*?)</title>", hl, re.S)
    return {
        "is_https": str(final_url or "").lower().startswith("https://"),
        "has_viewport": ('name="viewport"' in hl) or ("name='viewport'" in hl),
        "title": (m.group(1).strip()[:120] if m else ""),
    }


def website_audit(intel: dict) -> dict:
    """Classify a scanned domain into a Lisa-4 target bucket → {bucket, issue, is_target}.
    bucket = 'no_website' | 'critical_issue' | 'ok'. Missing new signals (records scanned before the
    health fields existed) default to healthy, so we NEVER claim a false issue on the call."""
    if not intel or intel.get("status") == "no_domain":
        return {"bucket": "no_website", "issue": "no website", "is_target": True}
    if not intel.get("found"):
        return {"bucket": "critical_issue", "issue": "site not loading", "is_target": True}
    st = int(intel.get("http_status") or 0)
    if st == 0 or st >= 400:
        return {"bucket": "critical_issue", "issue": "site error (%s)" % (st or "no response"), "is_target": True}
    if int(intel.get("html_bytes") or 0) < 1500:
        return {"bucket": "critical_issue", "issue": "site is blank / parked", "is_target": True}
    if intel.get("is_https") is False:
        return {"bucket": "critical_issue", "issue": "site is not secure (no HTTPS)", "is_target": True}
    if intel.get("has_viewport") is False:
        return {"bucket": "critical_issue", "issue": "site isn't mobile-friendly", "is_target": True}
    lm = intel.get("load_ms")
    if isinstance(lm, (int, float)) and lm > 7000:
        return {"bucket": "critical_issue", "issue": "site is very slow to load", "is_target": True}
    return {"bucket": "ok", "issue": None, "is_target": False}


def fetch_website_intel(domain: str, *, timeout: float = 12.0, verify: bool = True) -> dict:
    """Fetch https://domain (then http fallback) and detect marketing tech. Free.

    Returns {found, fetched, status, ...detection} — never raises. verify=False skips TLS
    certificate validation, for the many small-business sites that serve a valid page behind
    an expired/misconfigured cert (public homepages only — we send no credentials).
    """
    if not domain:
        return {"found": False, "status": "no_domain"}
    headers = BROWSER_HEADERS
    last_err = None
    for scheme in ("https", "http"):
        url = f"{scheme}://{domain}"
        try:
            with httpx.Client(timeout=timeout, follow_redirects=True, headers=headers, verify=verify) as c:
                _t0 = time.perf_counter()
                resp = c.get(url)
                _load_ms = int((time.perf_counter() - _t0) * 1000)
            html = resp.text or ""
            det = _detect(html)
            return {
                "found": True, "status": "ok", "final_url": str(resp.url),
                "http_status": resp.status_code, "html_bytes": len(html),
                "load_ms": _load_ms, **_health(html, str(resp.url)), **det,
            }
        except Exception as exc:  # try the next scheme, else report
            last_err = str(exc)[:160]
            continue
    log.info("website_fetch_failed", domain=domain, error=last_err)
    return {"found": False, "status": "error", "error": last_err}
