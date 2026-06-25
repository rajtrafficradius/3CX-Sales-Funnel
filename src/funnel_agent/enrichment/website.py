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

import httpx

from ..logging import get_logger

log = get_logger(__name__)

# Each tracker: (key, human label, list of regex signatures found in page HTML/JS).
_TRACKERS: list[tuple[str, str, list[str]]] = [
    ("gtm", "Google Tag Manager", [r"googletagmanager\.com/gtm\.js", r"GTM-[A-Z0-9]{4,}"]),
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
            resp = await client.get(url)
            html = resp.text or ""
            if len(html) > max_bytes:
                html = html[:max_bytes]
            det = _detect(html)
            return {
                "found": True, "status": "ok", "final_url": str(resp.url),
                "http_status": resp.status_code, "html_bytes": len(html), **det,
            }
        except Exception as exc:
            last_err = str(exc)[:160]
            continue
    return {"found": False, "status": "error", "error": last_err}


def fetch_website_intel(domain: str, *, timeout: float = 12.0) -> dict:
    """Fetch https://domain (then http fallback) and detect marketing tech. Free.

    Returns {found, fetched, status, ...detection} — never raises.
    """
    if not domain:
        return {"found": False, "status": "no_domain"}
    headers = {"User-Agent": "Mozilla/5.0 (compatible; TrafficRadiusBot/1.0; +https://trafficradius.com.au)"}
    last_err = None
    for scheme in ("https", "http"):
        url = f"{scheme}://{domain}"
        try:
            with httpx.Client(timeout=timeout, follow_redirects=True, headers=headers) as c:
                resp = c.get(url)
            html = resp.text or ""
            det = _detect(html)
            return {
                "found": True, "status": "ok", "final_url": str(resp.url),
                "http_status": resp.status_code, "html_bytes": len(html), **det,
            }
        except Exception as exc:  # try the next scheme, else report
            last_err = str(exc)[:160]
            continue
    log.info("website_fetch_failed", domain=domain, error=last_err)
    return {"found": False, "status": "error", "error": last_err}
