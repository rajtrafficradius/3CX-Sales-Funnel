"""FREE WHOIS lookup via RDAP (the modern, public WHOIS protocol).

No paid API and no scraping — RDAP is a free JSON-over-HTTPS registry service.
`rdap.org` routes each domain to its registry (incl. auDA for .au). Returns registrar,
key dates, status, nameservers and registrant/owner where the registry exposes it
(often redacted for privacy/GDPR — we keep whatever is public). Never raises.
"""

from __future__ import annotations

import threading
import time

import httpx

from ..logging import get_logger

log = get_logger(__name__)

_UA = {"User-Agent": "Mozilla/5.0 (compatible; TrafficRadiusBot/1.0; +https://trafficradius.com.au)"}

# Route each TLD to its REGISTRY's own RDAP server (IANA bootstrap) instead of the shared
# rdap.org aggregator, which rate-limits (429) under bulk load. Cached after first fetch.
_BOOTSTRAP: dict | None = None
_BOOT_LOCK = threading.Lock()


def _rdap_base(domain: str) -> str | None:
    global _BOOTSTRAP
    tld = domain.rsplit(".", 1)[-1].lower()
    if _BOOTSTRAP is None:
        with _BOOT_LOCK:
            if _BOOTSTRAP is None:
                try:
                    _BOOTSTRAP = httpx.get("https://data.iana.org/rdap/dns.json", timeout=15.0,
                                           headers=_UA).json()
                except Exception:
                    _BOOTSTRAP = {"services": []}
    for svc in (_BOOTSTRAP.get("services") or []):
        tlds, urls = (svc + [[], []])[:2]
        if tld in [t.lower() for t in tlds] and urls:
            return urls[0].rstrip("/")
    return None


def _vcard(entity: dict) -> tuple[str | None, str | None]:
    """Pull (full name, email) from an RDAP entity's jCard."""
    name = email = None
    arr = entity.get("vcardArray")
    if isinstance(arr, list) and len(arr) == 2:
        for field in arr[1]:
            if not isinstance(field, list) or len(field) < 4:
                continue
            key, val = field[0], field[3]
            if key == "fn" and val:
                name = val if isinstance(val, str) else None
            elif key == "email" and val and not email:
                email = val if isinstance(val, str) else None
    return name, email


def lookup_whois(domain: str) -> dict:
    """Free RDAP WHOIS for a domain. Returns {found, registrar, created, expires, ...}."""
    if not domain:
        return {"found": False, "status": "no_domain"}
    base = _rdap_base(domain)
    urls = [f"{base}/domain/{domain}"] if base else []
    urls.append(f"https://rdap.org/domain/{domain}")  # fallback to the aggregator
    d = None
    last = "error"
    for url in urls:
        for attempt in range(3):
            try:
                r = httpx.get(url, timeout=12.0, follow_redirects=True, headers=_UA)
            except Exception as exc:
                last = str(exc)[:120]; break
            if r.status_code == 200:
                d = r.json(); break
            last = f"http_{r.status_code}"
            if r.status_code == 429:
                time.sleep(1.5 * (attempt + 1))  # back off on rate-limit, then retry
                continue
            break
        if d is not None:
            break
    if d is None:
        log.info("whois_failed", domain=domain, status=last)
        return {"found": False, "status": last}

    events = {e.get("eventAction"): e.get("eventDate") for e in (d.get("events") or [])}
    registrar = registrant = owner_email = None
    for ent in (d.get("entities") or []):
        roles = ent.get("roles") or []
        nm, em = _vcard(ent)
        if "registrar" in roles and nm:
            registrar = nm
        if any(r_ in roles for r_ in ("registrant", "administrative", "technical")):
            registrant = registrant or nm
            owner_email = owner_email or em
    return {
        "found": True,
        "registrar": registrar,
        "registrant": registrant,                 # often redacted by privacy/GDPR
        "owner_email": owner_email,
        "created": events.get("registration"),
        "updated": events.get("last changed") or events.get("last update of RDAP database"),
        "expires": events.get("expiration"),
        "status": d.get("status") or [],
        "nameservers": [ns.get("ldhName") for ns in (d.get("nameservers") or []) if ns.get("ldhName")],
    }
