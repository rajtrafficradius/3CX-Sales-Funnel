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

import base64
import re
import time
from html import unescape as _html_unescape
from urllib.parse import urljoin, urlparse

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


# --------------------------------------------------------------------------- #
# LOGO EXTRACTION — pull a business's REAL brand mark from its homepage so a
# rebuilt (Lisa-4) site uses THEIR logo instead of an AI-invented fake one.
# Everything here is fully guarded: any failure returns None, never raises.
# --------------------------------------------------------------------------- #
_LOGO_MAX_BYTES = 600_000
# ext -> mime for accepted logo formats
_LOGO_MIMES = {
    "png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
    "webp": "image/webp", "svg": "image/svg+xml", "gif": "image/gif",
}
_LOGO_ACCEPT_MIME = {"image/png", "image/jpeg", "image/jpg", "image/webp", "image/svg+xml", "image/gif"}
# obvious tracking-pixel / spacer names we must never present as a logo
_PIXEL_HINT_RE = re.compile(r"(1x1|pixel|spacer|blank|tracking|transparent|clear\.gif)", re.I)

_HEADER_RE = re.compile(r"<header\b[^>]*>(.*?)</header>", re.I | re.S)
_NAV_RE = re.compile(r"<nav\b[^>]*>(.*?)</nav>", re.I | re.S)
_IMG_RE = re.compile(r"<img\b[^>]*>", re.I)
_SVG_RE = re.compile(r"<svg\b[^>]*>.*?</svg>", re.I | re.S)
_A_RE = re.compile(r"<a\b[^>]*>.*?</a>", re.I | re.S)
_SRC_RE = re.compile(r"""\bsrc\s*=\s*["']([^"']+)["']""", re.I)
_SRCSET_RE = re.compile(r"""\bsrcset\s*=\s*["']([^"']+)["']""", re.I)
_HREF_RE = re.compile(r"""\bhref\s*=\s*["']([^"']+)["']""", re.I)
_META_OG_RE = re.compile(r"""<meta\b[^>]*property\s*=\s*["']og:image[^"']*["'][^>]*>""", re.I)
_META_CONTENT_RE = re.compile(r"""\bcontent\s*=\s*["']([^"']+)["']""", re.I)
_APPLE_ICON_RE = re.compile(r"""<link\b[^>]*rel\s*=\s*["'][^"']*apple-touch-icon[^"']*["'][^>]*>""", re.I)
_ICON_RE = re.compile(r"""<link\b[^>]*rel\s*=\s*["'][^"']*icon[^"']*["'][^>]*>""", re.I)


def _is_blocked_host(host: str) -> bool:
    """Never fetch a logo from localhost / private / link-local hosts (defence in depth)."""
    h = (host or "").lower().strip()
    if not h or h in ("localhost", "127.0.0.1", "::1"):
        return True
    return h.startswith(("127.", "10.", "192.168.", "169.254.", "172.16.", "0."))


def _logo_mime(content_type: str | None, url: str) -> str | None:
    ct = (content_type or "").split(";")[0].strip().lower()
    if ct in _LOGO_ACCEPT_MIME:
        return "image/jpeg" if ct == "image/jpg" else ct
    path = urlparse(url).path.lower()
    for ext, mime in _LOGO_MIMES.items():
        if path.endswith("." + ext):
            return mime
    return None


def _looks_tiny(mime: str, data: bytes) -> bool:
    """Best-effort reject 1x1 / obviously-tiny tracking pixels without an image library
    (read PNG/GIF dimensions straight from the header bytes)."""
    try:
        if len(data) < 70:
            return True
        if mime == "image/png" and len(data) >= 24 and data[:8] == b"\x89PNG\r\n\x1a\n":
            w = int.from_bytes(data[16:20], "big")
            h = int.from_bytes(data[20:24], "big")
            return w <= 2 or h <= 2
        if mime == "image/gif" and len(data) >= 10 and data[:3] == b"GIF":
            w = int.from_bytes(data[6:8], "little")
            h = int.from_bytes(data[8:10], "little")
            return w <= 2 or h <= 2
    except Exception:
        return False
    return False


def _img_src(tag: str) -> str:
    """Best real URL from an <img> tag: lazy-load frameworks stash it off `src`, so prefer
    data-src/srcset over a placeholder src; fall back to an inline data: URI only as a last resort."""
    for attr in ("data-src", "data-original", "data-lazy-src", "data-lazy"):
        mm = re.search(r"\b" + attr + r"""\s*=\s*["']([^"']+)["']""", tag, re.I)
        if mm and mm.group(1).strip() and not mm.group(1).strip().lower().startswith("data:"):
            return mm.group(1).strip()
    m = _SRC_RE.search(tag)
    if m and m.group(1).strip() and not m.group(1).strip().lower().startswith("data:"):
        return m.group(1).strip()
    ms = _SRCSET_RE.search(tag)
    if ms:
        first = ms.group(1).split(",")[0].strip().split()
        if first and not first[0].lower().startswith("data:"):
            return first[0]
    return m.group(1).strip() if (m and m.group(1).strip().lower().startswith("data:")) else ""


def _load_logo_src(src: str, base_url: str, *, timeout: float, verify: bool = False) -> dict | None:
    """Turn a logo <img>/icon src (relative or absolute) into {'data_uri','url'} or None.
    Downloads over the site's own client/headers, guards scheme/host/size/mime. Never raises."""
    src = (src or "").strip()
    if not src:
        return None
    try:
        # already an inline data: URI on the page
        if src.lower().startswith("data:"):
            m = re.match(r"data:([^;,]+)", src, re.I)
            mime = (m.group(1).lower() if m else "")
            if mime not in _LOGO_ACCEPT_MIME:
                return None
            if len(src) > _LOGO_MAX_BYTES * 2:   # base64 ~= 4/3 of raw bytes
                return None
            return {"data_uri": src, "url": "(inline data URI)"}
        if _PIXEL_HINT_RE.search(src):
            return None
        abs_url = urljoin(base_url, src)
        pr = urlparse(abs_url)
        if pr.scheme not in ("http", "https"):
            return None
        if _is_blocked_host(pr.hostname or ""):
            return None
        with httpx.Client(timeout=timeout, follow_redirects=True, headers=BROWSER_HEADERS, verify=verify) as c:
            resp = c.get(abs_url)
        if resp.status_code >= 400:
            return None
        try:
            clen = int(resp.headers.get("content-length") or 0)
        except Exception:
            clen = 0
        if clen and clen > _LOGO_MAX_BYTES:
            return None
        data = resp.content or b""
        if not data or len(data) > _LOGO_MAX_BYTES:
            return None
        mime = _logo_mime(resp.headers.get("content-type"), abs_url)
        if not mime:
            return None
        if _looks_tiny(mime, data):
            return None
        data_uri = f"data:{mime};base64," + base64.b64encode(data).decode("ascii")
        return {"data_uri": data_uri, "url": abs_url}
    except Exception:
        return None


def extract_logo(domain: str, *, timeout: float = 10.0, verify: bool = False) -> dict | None:
    """Find a business's REAL logo on its homepage so a rebuild uses THEIR mark, never a fake one.

    Priority order (most reliable first), resolving relative URLs against the homepage's final URL:
      a. <img> in <header>/<nav>/top-of-page whose class/id/alt/src contains 'logo' (or the header's
         home-link image, which is the logo by strong convention);
      b. inline <svg> logo in the header (returns the raw svg markup);
      c. <meta property="og:image">;
      d. <link rel="apple-touch-icon"> (usually a clean square logo);
      e. <link rel="icon"> / favicon (last resort).

    Returns {"data_uri": "data:<mime>;base64,…" | raw svg markup, "source": <rule>, "url": <abs url>}
    or None. Fully guarded — never raises; returns None on any failure or when nothing safe is found.
    """
    if not domain:
        return None
    html = None
    base_url = None
    for scheme in ("https", "http"):
        url = f"{scheme}://{domain}"
        try:
            with httpx.Client(timeout=timeout, follow_redirects=True, headers=BROWSER_HEADERS, verify=verify) as c:
                resp = c.get(url)
            if resp.text:
                html = resp.text
                base_url = str(resp.url)
                break
        except Exception:
            continue
    if not html or not base_url:
        return None
    try:
        # header / nav region (where the brand logo lives) + a top-of-page fallback window
        header = ""
        mh = _HEADER_RE.search(html)
        if mh:
            header = mh.group(1)
        mn = _NAV_RE.search(html)
        if mn:
            header += "\n" + mn.group(1)
        top = html[:20000]

        # (a) <img> whose attributes mention 'logo'/'brand', in the header then the top-of-page
        for scope in ([header, top] if header else [top]):
            for tag in _IMG_RE.findall(scope):
                low = tag.lower()
                if "logo" in low or "brand" in low:
                    got = _load_logo_src(_img_src(tag), base_url, timeout=timeout, verify=verify)
                    if got:
                        return {"data_uri": got["data_uri"], "source": "header_img_logo", "url": got["url"]}

        # (a') the header's home-link image is, by convention, the logo even without a 'logo' class
        if header:
            base_root = base_url.rstrip("/")
            for am in _A_RE.finditer(header):
                atag = am.group(0)
                hrefm = _HREF_RE.search(atag)
                href = (hrefm.group(1).strip() if hrefm else "")
                if href in ("/", "#", "./") or href.rstrip("/") == base_root:
                    im = _IMG_RE.search(atag)
                    if im:
                        got = _load_logo_src(_img_src(im.group(0)), base_url, timeout=timeout, verify=verify)
                        if got:
                            return {"data_uri": got["data_uri"], "source": "header_home_link_img", "url": got["url"]}

        # (b) inline <svg> logo in the header (svg tag or a wrapping element flags it as the logo)
        for m in _SVG_RE.finditer(header or ""):
            svg = m.group(0)
            before = header[max(0, m.start() - 180):m.start()]
            if "logo" in svg.lower()[:400] or "logo" in before.lower() or "brand" in before.lower():
                svg = svg.strip()
                if 40 <= len(svg) <= _LOGO_MAX_BYTES:
                    return {"data_uri": svg, "source": "inline_svg", "url": base_url}

        # (c) og:image
        mo = _META_OG_RE.search(html)
        if mo:
            mc = _META_CONTENT_RE.search(mo.group(0))
            if mc:
                got = _load_logo_src(mc.group(1), base_url, timeout=timeout, verify=verify)
                if got:
                    return {"data_uri": got["data_uri"], "source": "og_image", "url": got["url"]}

        # (d) apple-touch-icon — usually a clean square logo
        ma = _APPLE_ICON_RE.search(html)
        if ma:
            mhref = _HREF_RE.search(ma.group(0))
            if mhref:
                got = _load_logo_src(mhref.group(1), base_url, timeout=timeout, verify=verify)
                if got:
                    return {"data_uri": got["data_uri"], "source": "apple_touch_icon", "url": got["url"]}

        # (e) favicon / <link rel=icon> — last resort
        for m in _ICON_RE.finditer(html):
            tag = m.group(0)
            if "apple-touch" in tag.lower():
                continue
            mhref = _HREF_RE.search(tag)
            if mhref:
                got = _load_logo_src(mhref.group(1), base_url, timeout=timeout, verify=verify)
                if got:
                    return {"data_uri": got["data_uri"], "source": "favicon", "url": got["url"]}
    except Exception:
        return None
    return None


# --------------------------------------------------------------------------- #
# LOGO VISIBILITY — a mark pulled from an OLD site's header is very often a
# WHITE / reversed / transparent logo built for a DARK header. Dropped onto a
# light rebuilt header it goes invisible (white-on-white). Classify the mark so
# the rebuild can give it a guaranteed-contrast backing. Fully guarded.
# --------------------------------------------------------------------------- #
_NAMED_COLORS = {
    "white": (255, 255, 255), "whitesmoke": (245, 245, 245), "snow": (255, 250, 250),
    "ivory": (255, 255, 240), "silver": (192, 192, 192), "lightgray": (211, 211, 211),
    "lightgrey": (211, 211, 211), "gainsboro": (220, 220, 220), "gray": (128, 128, 128),
    "grey": (128, 128, 128), "black": (0, 0, 0), "darkgray": (169, 169, 169),
    "darkgrey": (169, 169, 169), "dimgray": (105, 105, 105), "dimgrey": (105, 105, 105),
    "red": (255, 0, 0), "green": (0, 128, 0), "blue": (0, 0, 255), "navy": (0, 0, 128),
    "orange": (255, 165, 0), "gold": (255, 215, 0), "yellow": (255, 255, 0),
    "maroon": (128, 0, 0), "teal": (0, 128, 128), "purple": (128, 0, 128),
}
# color values that carry no fixed tone (inherit / painted elsewhere) — ignore them
_SKIP_COLOR = {"none", "transparent", "currentcolor", "inherit", "unset", "initial", "url"}
_SVG_COLOR_RE = re.compile(
    r"""(?:fill|stroke|stop-color|color)\s*[:=]\s*["']?\s*"""
    r"""(#[0-9a-fA-F]{3,8}|rgba?\([^)]*\)|[a-zA-Z]+)""")


def _color_luminance(c: str) -> float | None:
    """Perceived luminance (0=black..255=white) of a CSS color token, or None if unusable."""
    c = (c or "").strip().lower()
    if not c or c in _SKIP_COLOR:
        return None
    if c in _NAMED_COLORS:
        r, g, b = _NAMED_COLORS[c]
        return 0.299 * r + 0.587 * g + 0.114 * b
    if c.startswith("#"):
        h = c[1:]
        try:
            if len(h) in (3, 4):
                r, g, b = (int(h[0] * 2, 16), int(h[1] * 2, 16), int(h[2] * 2, 16))
            elif len(h) in (6, 8):
                r, g, b = (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))
            else:
                return None
            return 0.299 * r + 0.587 * g + 0.114 * b
        except Exception:
            return None
    if c.startswith("rgb"):
        nums = re.findall(r"[\d.]+", c)
        if len(nums) >= 3:
            try:
                r, g, b = (min(255.0, float(nums[0])), min(255.0, float(nums[1])), min(255.0, float(nums[2])))
                return 0.299 * r + 0.587 * g + 0.114 * b
            except Exception:
                return None
    return None


def _tone_from_luminance(avg: float) -> str:
    """Bucket a mean luminance into a placement tone (with a neutral dead-band => 'unknown')."""
    if avg >= 150:
        return "light"
    if avg <= 105:
        return "dark"
    return "unknown"


def _svg_ink_tone(svg: str) -> str:
    """Best-effort tone of an inline-SVG mark from its declared fill/stroke colors."""
    try:
        lums = []
        for c in _SVG_COLOR_RE.findall(svg or ""):
            L = _color_luminance(c)
            if L is not None:
                lums.append(L)
        if not lums:
            return "unknown"   # relies on currentColor / CSS => can't tell
        return _tone_from_luminance(sum(lums) / len(lums))
    except Exception:
        return "unknown"


def _decode_data_uri(s: str) -> tuple[bytes, str]:
    """(raw bytes, mime) for a data: URI; ('', '') on any failure. Never raises."""
    try:
        header, _, payload = s.partition(",")
        if not payload:
            return b"", ""
        mime = header[5:].split(";")[0].strip().lower()   # strip 'data:'
        if "base64" in header.lower():
            raw = base64.b64decode(payload, validate=False)
        else:
            from urllib.parse import unquote_to_bytes
            raw = unquote_to_bytes(payload)
        return raw, mime
    except Exception:
        return b"", ""


def _raster_has_alpha(raw: bytes) -> bool:
    """True if the raster carries transparency (so its ink floats on the page bg). Never raises."""
    try:
        if raw[:8] == b"\x89PNG\r\n\x1a\n":
            ctype = raw[25] if len(raw) > 25 else 0   # IHDR color-type byte
            if ctype in (4, 6):                        # gray+alpha / RGBA
                return True
            return b"tRNS" in raw[:8192]               # palette/other w/ transparency chunk
        if raw[:6] in (b"GIF87a", b"GIF89a"):
            return True                                # treat GIFs as possibly-transparent
        if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
            if b"ALPH" in raw[:8192]:
                return True
            if raw[12:16] == b"VP8X" and len(raw) > 20:
                return bool(raw[20] & 0x10)            # VP8X alpha flag
            return False
        if raw[:3] == b"\xff\xd8\xff":
            return False                               # JPEG never has alpha
    except Exception:
        return False
    return False


def _raster_ink_tone(raw: bytes) -> str:
    """Mean luminance of the VISIBLE (non-transparent) ink via Pillow, or 'unknown' if unavailable."""
    try:
        from io import BytesIO
        from PIL import Image
        im = Image.open(BytesIO(raw)).convert("RGBA")
        im.thumbnail((64, 64))
        px = im.load()
        w, h = im.size
        tot = 0.0
        n = 0
        for y in range(h):
            for x in range(w):
                r, g, b, a = px[x, y]
                if a < 40:            # skip transparent pixels — measure only the drawn mark
                    continue
                tot += 0.299 * r + 0.587 * g + 0.114 * b
                n += 1
        if n == 0:
            return "unknown"
        return _tone_from_luminance(tot / n)
    except Exception:
        return "unknown"


def logo_tone(data_uri: str) -> dict:
    """Classify a logo mark for VISIBLE placement on a rebuilt site.

    Returns {"transparent": bool, "tone": "light"|"dark"|"unknown"}:
      - transparent=False -> the mark carries its own opaque rectangle (a JPEG, or a
        flattened PNG); it is self-contained and shows on any header — no backing needed.
      - transparent=True  -> the ink floats on the page background; `tone` says whether the
        visible ink is mostly light (wants a DARK backing), dark (wants a LIGHT backing), or
        `unknown` (default to a DARK backing — the reversed/white-logo case that caused the bug).
    Accepts either a `data:<mime>;base64,...` URI or raw inline `<svg>` markup. Never raises.
    """
    s = (data_uri or "").strip()
    if not s:
        return {"transparent": False, "tone": "unknown"}
    low = s.lower()
    if low.startswith("<svg"):
        return {"transparent": True, "tone": _svg_ink_tone(s)}
    if low.startswith("data:image/svg"):
        raw, _ = _decode_data_uri(s)
        return {"transparent": True, "tone": _svg_ink_tone(raw.decode("utf-8", "ignore") if raw else "")}
    if low.startswith("data:"):
        raw, _mime = _decode_data_uri(s)
        if not raw:
            return {"transparent": True, "tone": "unknown"}
        if not _raster_has_alpha(raw):
            return {"transparent": False, "tone": "unknown"}   # opaque => self-contained
        return {"transparent": True, "tone": _raster_ink_tone(raw)}
    return {"transparent": False, "tone": "unknown"}


# --------------------------------------------------------------------------- #
# REAL-MEDIA + CONTENT SCRAPER — capture the OLD site's actual content PHOTOS
# and body COPY so a Lisa-4 rebuild RETAINS them (content-parity rule) instead
# of inventing generic images/text. Fully guarded: any failure -> found False.
# --------------------------------------------------------------------------- #
_CONTENT_IMG_MAX_BYTES = 600_000      # per-image download ceiling
_CONTENT_IMG_MIN_BYTES = 5_000        # under ~5KB => icon/sprite/spacer, not a real photo
_CONTENT_MAX_CHARS = 6_000            # cap on the returned structured content string
_MAX_INTERNAL_PAGES = 6               # homepage + up to this many same-domain pages (deeper crawl = better
                                     # chance of capturing the real trade when the homepage copy is thin)

# non-photo image filenames we must never treat as real content media
_NONCONTENT_NAME_RE = re.compile(
    r"(icon|logo|sprite|pixel|spacer|favicon|badge|placeholder|blank|loader|loading|"
    r"avatar[-_]?default|swatch|thumb[-_]?default|social|arrow|chevron|bullet)", re.I)
# common two-label public suffixes so "same registrable domain" survives .com.au etc.
_TWO_LABEL_TLDS = {
    "com.au", "net.au", "org.au", "edu.au", "gov.au", "asn.au", "id.au",
    "co.nz", "org.nz", "net.nz", "co.uk", "org.uk", "me.uk", "ac.uk", "gov.uk",
    "co.za", "com.sg", "com.my", "co.in", "co.jp", "com.br",
}
# links that are not real internal HTML pages
_DOC_EXT_RE = re.compile(
    r"\.(pdf|zip|rar|docx?|xlsx?|pptx?|csv|jpe?g|png|gif|webp|svg|ico|bmp|mp4|webm|mp3|"
    r"wav|avi|mov|css|js|json|xml|rss|woff2?|ttf|eot)(\?|#|$)", re.I)

_A_OPEN_RE = re.compile(r"<a\b[^>]*>", re.I)
_SOURCE_RE = re.compile(r"<source\b[^>]*>", re.I)
_BG_URL_RE = re.compile(r"""url\(\s*['\"]?([^'\")]+)['\"]?\s*\)""", re.I)
_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)
_META_DESC_RE = re.compile(r"""<meta\b[^>]*name\s*=\s*["']description["'][^>]*>""", re.I)
_HEADING_RE = re.compile(r"<h([1-3])\b[^>]*>(.*?)</h\1>", re.I | re.S)
_LI_RE = re.compile(r"<li\b[^>]*>(.*?)</li>", re.I | re.S)
_TAG_STRIP_RE = re.compile(r"<[^>]+>")
_SCRIPT_STYLE_RE = re.compile(r"<(script|style|noscript|template)\b[^>]*>.*?</\1>", re.I | re.S)
_HEADER_BLOCK_RE = re.compile(r"<header\b[^>]*>.*?</header>", re.I | re.S)
_FOOTER_BLOCK_RE = re.compile(r"<footer\b[^>]*>.*?</footer>", re.I | re.S)
_NAV_BLOCK_RE = re.compile(r"<nav\b[^>]*>.*?</nav>", re.I | re.S)
_PRICE_RE = re.compile(r".{0,45}\$\s?\d[\d,]*(?:\.\d{1,2})?(?:\s?(?:/|per|p/)\s?\w+)?.{0,25}", re.I)


def _registrable(host: str) -> str:
    """Best-effort registrable domain (handles .com.au / .co.uk style two-label suffixes)."""
    h = (host or "").lower().strip().rstrip(".")
    if h.startswith("www."):
        h = h[4:]
    parts = [p for p in h.split(".") if p]
    if len(parts) <= 2:
        return ".".join(parts)
    if ".".join(parts[-2:]) in _TWO_LABEL_TLDS:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def _fetch_home_html(domain: str, timeout: float, verify: bool = False) -> tuple:
    """Homepage HTML + final URL, trying https then http. Returns (None, None) on failure."""
    for scheme in ("https", "http"):
        try:
            with httpx.Client(timeout=timeout, follow_redirects=True, headers=BROWSER_HEADERS, verify=verify) as c:
                resp = c.get(f"{scheme}://{domain}")
            if resp.text:
                return resp.text, str(resp.url)
        except Exception:
            continue
    return None, None


def _fetch_url_html(url: str, timeout: float, verify: bool = False) -> tuple:
    """Fetch one absolute internal page. Returns (html, final_url) or (None, None). Never raises."""
    try:
        with httpx.Client(timeout=timeout, follow_redirects=True, headers=BROWSER_HEADERS, verify=verify) as c:
            resp = c.get(url)
        if resp.status_code < 400 and resp.text:
            return resp.text, str(resp.url)
    except Exception:
        pass
    return None, None


def _internal_links(html: str, base_url: str, *, cap: int = _MAX_INTERNAL_PAGES) -> list:
    """Same-registrable-domain HTML page links from the homepage (deduped, capped, guarded)."""
    out, seen = [], set()
    base_reg = _registrable(urlparse(base_url).hostname or "")
    base_norm = base_url.split("#")[0].rstrip("/")
    for atag in _A_OPEN_RE.findall(html or ""):
        hm = _HREF_RE.search(atag)
        if not hm:
            continue
        href = (hm.group(1) or "").strip()
        if not href or href.lower().startswith(("#", "mailto:", "tel:", "javascript:", "data:")):
            continue
        abs_url = urljoin(base_url, href).split("#")[0]
        pr = urlparse(abs_url)
        if pr.scheme not in ("http", "https") or _is_blocked_host(pr.hostname or ""):
            continue
        if _registrable(pr.hostname or "") != base_reg:
            continue
        if _DOC_EXT_RE.search(pr.path or ""):
            continue
        norm = abs_url.rstrip("/")
        if not norm or norm == base_norm or norm in seen:
            continue
        seen.add(norm)
        out.append(abs_url)
        if len(out) >= cap:
            break
    return out


def _largest_srcset(srcset: str) -> str:
    """Pick the highest-resolution candidate URL from a srcset ('u 320w, u 2x' ...)."""
    best, best_score = "", -1.0
    for part in (srcset or "").split(","):
        seg = part.strip().split()
        if not seg:
            continue
        u = seg[0].strip()
        if not u or u.lower().startswith("data:"):
            continue
        score = 0.0
        if len(seg) > 1:
            d = seg[1].strip().lower()
            try:
                if d.endswith("w"):
                    score = float(d[:-1])
                elif d.endswith("x"):
                    score = float(d[:-1]) * 1000.0
            except Exception:
                score = 0.0
        if score > best_score:
            best, best_score = u, score
    return best


def _collect_image_urls(html: str, page_url: str) -> list:
    """All candidate image URLs on a page (img src/srcset, <source>, CSS background-image, og:image),
    resolved to absolute and host-guarded. Order preserved; data: URIs skipped. Never raises."""
    raw = []
    try:
        for mo in _META_OG_RE.finditer(html):
            mc = _META_CONTENT_RE.search(mo.group(0))
            if mc:
                raw.append(mc.group(1).strip())
        for tag in _IMG_RE.findall(html):
            best = ""
            ms = _SRCSET_RE.search(tag)
            if ms:
                best = _largest_srcset(ms.group(1))
            if not best:
                best = _img_src(tag)
            if best:
                raw.append(best)
        for tag in _SOURCE_RE.findall(html):
            ms = _SRCSET_RE.search(tag)
            if ms:
                b = _largest_srcset(ms.group(1))
                if b:
                    raw.append(b)
            else:
                m = _SRC_RE.search(tag)
                if m:
                    raw.append(m.group(1).strip())
        for m in _BG_URL_RE.finditer(html):
            raw.append(m.group(1).strip())
    except Exception:
        return []
    out = []
    for u in raw:
        u = (u or "").strip()
        if not u or u.lower().startswith("data:"):
            continue
        try:
            abs_url = urljoin(page_url, u)
            pr = urlparse(abs_url)
        except Exception:
            continue
        if pr.scheme not in ("http", "https") or _is_blocked_host(pr.hostname or ""):
            continue
        out.append(abs_url)
    return out


def _is_noncontent_image_url(url: str) -> bool:
    """URL-level reject: SVGs, logos, icons/sprites/spacers/pixels/favicons/badges, tracking pixels."""
    path = urlparse(url).path.lower()
    if path.endswith(".svg"):
        return True
    if _NONCONTENT_NAME_RE.search(path):
        return True
    if _PIXEL_HINT_RE.search(url.lower()):
        return True
    return False


def _load_content_image(url: str, timeout: float, verify: bool = False) -> str | None:
    """Download one candidate photo -> data URI, or None. Guards scheme/host/size/mime; drops SVGs,
    sub-5KB icons/spacers and 1x1 pixels. Never raises."""
    try:
        pr = urlparse(url)
        if pr.scheme not in ("http", "https") or _is_blocked_host(pr.hostname or ""):
            return None
        with httpx.Client(timeout=timeout, follow_redirects=True, headers=BROWSER_HEADERS, verify=verify) as c:
            resp = c.get(url)
        if resp.status_code >= 400:
            return None
        try:
            clen = int(resp.headers.get("content-length") or 0)
        except Exception:
            clen = 0
        if clen and clen > _CONTENT_IMG_MAX_BYTES:
            return None
        data = resp.content or b""
        if not data or len(data) > _CONTENT_IMG_MAX_BYTES or len(data) < _CONTENT_IMG_MIN_BYTES:
            return None
        mime = _logo_mime(resp.headers.get("content-type"), url)
        if not mime or mime == "image/svg+xml":
            return None
        if _looks_tiny(mime, data):
            return None
        return f"data:{mime};base64," + base64.b64encode(data).decode("ascii")
    except Exception:
        return None


def _text_of(fragment: str) -> str:
    """Strip tags + collapse whitespace + unescape entities -> plain visible text."""
    t = _TAG_STRIP_RE.sub(" ", fragment or "")
    try:
        t = _html_unescape(t)
    except Exception:
        pass
    return re.sub(r"\s+", " ", t).strip()


def _extract_page_content(page_url: str, html: str, *, is_home: bool, seen_headings: set) -> tuple:
    """One page -> (structured content block str, list of NEW headings). Strips script/style/nav/
    header/footer for the body so navigation chrome doesn't pollute the copy."""
    seg, new_headings = [], []
    try:
        mt = _TITLE_RE.search(html)
        title = _text_of(mt.group(1)) if mt else ""
        desc = ""
        md = _META_DESC_RE.search(html)
        if md:
            mc = _META_CONTENT_RE.search(md.group(0))
            if mc:
                desc = _text_of(mc.group(1))
        page_headings = []
        for hm in _HEADING_RE.finditer(html):
            txt = _text_of(hm.group(2))
            if txt and 2 <= len(txt) <= 160:
                page_headings.append(txt)
                key = txt.lower()
                if key not in seen_headings:
                    seen_headings.add(key)
                    new_headings.append(txt)
        body = _SCRIPT_STYLE_RE.sub(" ", html)
        body = _NAV_BLOCK_RE.sub(" ", body)
        body = _HEADER_BLOCK_RE.sub(" ", body)
        body = _FOOTER_BLOCK_RE.sub(" ", body)
        list_items, seen_li = [], set()
        for lm in _LI_RE.finditer(body):
            it = _text_of(lm.group(1))
            if it and 2 <= len(it) <= 120:
                k = it.lower()
                if k not in seen_li:
                    seen_li.add(k)
                    list_items.append(it)
        body_text = _text_of(body)
        prices, seen_pr = [], set()
        for pm in _PRICE_RE.finditer(body_text):
            s = pm.group(0).strip()
            k = s.lower()
            if s and k not in seen_pr:
                seen_pr.add(k)
                prices.append(s)
            if len(prices) >= 15:
                break

        label = "HOMEPAGE" if is_home else ("PAGE " + (urlparse(page_url).path or "/"))
        seg.append("=== %s ===" % label)
        if title:
            seg.append("TITLE: " + title)
        if desc:
            seg.append("META: " + desc)
        if page_headings:
            seg.append("HEADINGS: " + " | ".join(page_headings[:30]))
        if list_items:
            seg.append("SERVICES/LISTS: " + " • ".join(list_items[:40]))
        if prices:
            seg.append("PRICING: " + " • ".join(prices))
        if body_text:
            seg.append("BODY: " + body_text[:2500])
    except Exception:
        return "", []
    return "\n".join(seg), new_headings


def scrape_site_media(domain: str, timeout: float = 15.0, max_images: int = 18) -> dict:
    """Capture a prospect site's REAL content photos + body copy so a Lisa-4 rebuild retains them.

    Crawls the homepage plus up to ~4 same-registrable-domain internal pages, then:
      • IMAGES — collects candidates from <img src>, <img>/<source> srcset (largest), CSS inline
        background-image url(...) and og:image; resolves to absolute; filters out logos, icons,
        sprites, spacers, favicons, badges, tracking pixels, SVGs and sub-5KB/1x1 files; downloads
        each surviving photo (per-image 600KB cap, capped at `max_images`) into a base64 data URI.
      • CONTENT — page title, all h1/h2/h3 headings, main visible body text (nav/header/footer/
        script/style stripped) and any services/pricing lists, as one compact structured string
        (~6000 char cap) the site designer can faithfully reuse.
      • LOGO — reuses extract_logo(domain).

    Returns {found, images, content, headings, pages_crawled, logo}. Fully guarded: any failure
    (or a homepage that won't load) returns found=False with empty fields. Never raises.
    """
    empty = {"found": False, "images": [], "content": "", "headings": [], "pages_crawled": 0, "logo": None}
    if not domain:
        return empty
    verify = False
    img_timeout = min(timeout, 12.0)
    try:
        home_html, home_url = _fetch_home_html(domain, timeout, verify=verify)
        if not home_html or not home_url:
            return dict(empty)

        pages = [(home_url, home_html)]
        try:
            for link in _internal_links(home_html, home_url, cap=_MAX_INTERNAL_PAGES):
                h, u = _fetch_url_html(link, timeout, verify=verify)
                if h and u:
                    pages.append((u, h))
                if len(pages) >= (_MAX_INTERNAL_PAGES + 1):
                    break
        except Exception:
            pass

        # logo first, so we can also skip it from the content-photo set
        try:
            logo = extract_logo(domain, timeout=min(timeout, 10.0), verify=verify)
        except Exception:
            logo = None
        logo_url = (logo or {}).get("url") if isinstance(logo, dict) else None

        # gather + dedup candidate image URLs across all pages (order preserved)
        candidates, seen_url = [], set()
        for purl, phtml in pages:
            for iu in _collect_image_urls(phtml, purl):
                key = iu.split("#")[0]
                if key in seen_url:
                    continue
                seen_url.add(key)
                candidates.append(iu)

        images, seen_data = [], set()
        for iu in candidates:
            if len(images) >= max_images:
                break
            if logo_url and iu == logo_url:
                continue
            if _is_noncontent_image_url(iu):
                continue
            got = _load_content_image(iu, img_timeout, verify=verify)
            if got and got not in seen_data:
                seen_data.add(got)
                images.append(got)

        # structured content across pages, capped
        blocks, headings, seen_h = [], [], set()
        for idx, (purl, phtml) in enumerate(pages):
            block, new_h = _extract_page_content(purl, phtml, is_home=(idx == 0), seen_headings=seen_h)
            if block:
                blocks.append(block)
            headings.extend(new_h)
        content = "\n\n".join(blocks)
        if len(content) > _CONTENT_MAX_CHARS:
            content = content[:_CONTENT_MAX_CHARS].rstrip() + " …"

        return {
            "found": bool(images or content),
            "images": images,
            "content": content,
            "headings": headings[:60],
            "pages_crawled": len(pages),
            "logo": logo,
        }
    except Exception as exc:
        log.info("scrape_site_media_failed", domain=domain, error=str(exc)[:160])
        return dict(empty)
