"""FREE WHOIS lookup — RDAP first, then a pure-Python port-43 WHOIS fallback.

No paid API and no scraping. RDAP (JSON-over-HTTPS) covers .com/.net/.org/.io etc. via
each registry. For .au, auDA rate-limits/blocks anonymous RDAP (429) AND redacts the
registrant there — so we fall back to the classic WHOIS protocol (TCP port 43), which
auDA serves freely with the registrant NAME, ORG and ABN. The port-43 path is plain
stdlib sockets (works on the cloud container with no `whois` binary). Never raises.
"""

from __future__ import annotations

import re
import socket
import threading
import time

import httpx

from ..logging import get_logger

log = get_logger(__name__)

# TLD -> WHOIS (port 43) server for the common cases; anything else is resolved live
# from whois.iana.org. auDA serves rich, un-redacted .au data here (unlike its RDAP).
_WHOIS43 = {
    "au": "whois.auda.org.au",
    "com": "whois.verisign-grs.com",
    "net": "whois.verisign-grs.com",
    "org": "whois.pir.org",
    "io": "whois.nic.io",
    "co": "whois.nic.co",
    "nz": "whois.irs.net.nz",
}
_W43_CACHE: dict[str, str | None] = {}
_W43_LOCK = threading.Lock()

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
    """Pull (full name, email) from an RDAP entity's jCard (back-compat 2-tuple)."""
    v = _vcard_full(entity)
    return v.get("name"), v.get("email")


def _vcard_full(entity: dict) -> dict:
    """Pull name, org, email, phone, address from an RDAP entity's jCard."""
    out: dict = {}
    arr = entity.get("vcardArray")
    if isinstance(arr, list) and len(arr) == 2:
        for field in arr[1]:
            if not isinstance(field, list) or len(field) < 4:
                continue
            key, val = field[0], field[3]
            if key == "fn" and isinstance(val, str) and val:
                out["name"] = val
            elif key == "org" and val:
                out["org"] = val if isinstance(val, str) else " ".join(str(x) for x in val if x)
            elif key == "email" and isinstance(val, str) and not out.get("email"):
                out["email"] = val
            elif key == "tel" and not out.get("tel"):
                out["tel"] = val if isinstance(val, str) else (str(val[0]) if isinstance(val, list) and val else None)
            elif key == "adr":
                # jCard adr value is a 7-part list: [pobox, ext, street, city, region, postcode, country]
                parts = val if isinstance(val, list) else (entity.get(key) or [])
                if isinstance(parts, list) and len(parts) >= 7:
                    out["city"] = parts[3] or out.get("city")
                    out["state"] = parts[4] or out.get("state")
                    out["country"] = parts[6] or out.get("country")
    return out


def _whois43_server(domain: str) -> str | None:
    """Resolve the port-43 WHOIS server for a domain's TLD (cached; IANA fallback)."""
    tld = domain.rsplit(".", 1)[-1].lower()
    if tld in _WHOIS43:
        return _WHOIS43[tld]
    if tld in _W43_CACHE:
        return _W43_CACHE[tld]
    server = None
    try:
        raw = _whois43_query("whois.iana.org", tld)
        m = re.search(r"(?im)^whois:\s*(\S+)", raw or "")
        if m:
            server = m.group(1).strip()
    except Exception:
        server = None
    with _W43_LOCK:
        _W43_CACHE[tld] = server
    return server


def _whois43_query(server: str, query: str, timeout: float = 12.0) -> str:
    """Raw WHOIS protocol request (TCP/43). Returns the text response (may be empty)."""
    chunks = []
    with socket.create_connection((server, 43), timeout=timeout) as sock:
        sock.settimeout(timeout)
        sock.sendall((query + "\r\n").encode("idna" if False else "utf-8", "ignore"))
        while True:
            try:
                data = sock.recv(8192)
            except socket.timeout:
                break
            if not data:
                break
            chunks.append(data)
    return b"".join(chunks).decode("utf-8", "replace")


def _first(text: str, *keys: str) -> str | None:
    for key in keys:
        m = re.search(rf"(?im)^\s*{re.escape(key)}\s*:\s*(.+?)\s*$", text)
        if m and m.group(1).strip() and m.group(1).strip().upper() not in ("N/A", "NOT DISCLOSED"):
            return m.group(1).strip()
    return None


def _all(text: str, key: str) -> list[str]:
    return [m.strip() for m in re.findall(rf"(?im)^\s*{re.escape(key)}\s*:\s*(.+?)\s*$", text) if m.strip()]


def _whois43(domain: str) -> dict:
    """Port-43 WHOIS fallback — rich for .au (registrant name/org/ABN), free, no binary."""
    server = _whois43_server(domain)
    if not server:
        return {"found": False, "status": "no_whois43_server"}
    try:
        text = _whois43_query(server, domain)
    except Exception as exc:
        return {"found": False, "status": f"w43:{str(exc)[:80]}"}
    if not text or re.search(r"(?i)no data found|no match|not found|no entries found", text):
        return {"found": False, "status": "w43_no_match"}
    registrant = _first(text, "Registrant", "Registrant Name", "Registrant Organization",
                         "Registrant Organisation", "Registrant Contact Name", "org")
    abn = _first(text, "Registrant ID", "Eligibility ID")
    ns = list(dict.fromkeys(_all(text, "Name Server") + _all(text, "nserver")))
    return {
        "found": True,
        "source": "whois43",
        "registrar": _first(text, "Registrar Name", "Registrar", "registrar"),
        "registrar_url": _first(text, "Registrar URL", "Registrar Website"),
        "registrar_abuse_email": _first(text, "Registrar Abuse Contact Email"),
        "registrar_abuse_phone": _first(text, "Registrar Abuse Contact Phone"),
        "registrant": registrant,
        "registrant_name": _first(text, "Registrant Contact Name", "Registrant Name"),
        "registrant_id": abn,            # .au: includes the ABN/ACN ("OTHER 066 397 420")
        "eligibility": _first(text, "Eligibility Type", "Registrant Eligibility Type", "Eligibility Name"),
        "owner_email": _first(text, "Registrant Contact Email", "Registrant Email"),
        "owner_name": _first(text, "Registrant Contact Name"),
        # Technical contact (the person/role who manages the domain's DNS/hosting — often the
        # web/IT provider; a useful second lead when the registrant is privacy-protected).
        "tech_name": _first(text, "Tech Contact Name", "Technical Contact Name", "Tech Name", "Admin Contact Name"),
        "tech_email": _first(text, "Tech Contact Email", "Technical Contact Email", "Tech Email", "Admin Contact Email"),
        "tech_phone": _first(text, "Tech Contact Phone", "Technical Contact Phone", "Tech Phone"),
        "tech_org": _first(text, "Tech Organization", "Tech Organisation", "Technical Organization", "Tech Contact Organisation"),
        "registrant_city": _first(text, "Registrant City"),
        "registrant_state": _first(text, "Registrant State/Province", "Registrant State"),
        "registrant_country": _first(text, "Registrant Country", "Registrant Country/Economy"),
        "created": _first(text, "Creation Date", "Registration Date", "created"),
        "updated": _first(text, "Last Modified", "Updated Date", "last-modified", "modified"),
        "expires": _first(text, "Registry Expiry Date", "Expiration Date", "Expiry Date", "expires"),
        "status": _all(text, "Status") or _all(text, "status"),
        "nameservers": ns,
        "dnssec": _first(text, "DNSSEC"),
        "reseller": _first(text, "Reseller"),
    }


def lookup_whois(domain: str) -> dict:
    """Free WHOIS for a domain: RDAP first, then port-43 fallback (rich for .au)."""
    if not domain:
        return {"found": False, "status": "no_domain"}
    # .au RDAP is rate-limited/redacted by auDA — go straight to port-43 (rich, free).
    if domain.lower().rsplit(".", 1)[-1] == "au":
        w = _whois43(domain)
        if w.get("found"):
            return w
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
        w = _whois43(domain)  # RDAP failed (429/blocked) -> try classic port-43 WHOIS
        if w.get("found"):
            return w
        log.info("whois_failed", domain=domain, status=last)
        return {"found": False, "status": last}

    events = {e.get("eventAction"): e.get("eventDate") for e in (d.get("events") or [])}
    out: dict = {"found": True, "source": "rdap"}
    for ent in (d.get("entities") or []):
        roles = ent.get("roles") or []
        v = _vcard_full(ent)
        if "registrar" in roles:
            out["registrar"] = out.get("registrar") or v.get("name") or v.get("org")
            # registrar IANA id + abuse contact (nested sub-entity)
            for pub in (ent.get("publicIds") or []):
                if pub.get("type") == "IANA Registrar ID":
                    out["registrar_iana_id"] = pub.get("identifier")
            for sub in (ent.get("entities") or []):
                if "abuse" in (sub.get("roles") or []):
                    sv = _vcard_full(sub)
                    out["registrar_abuse_email"] = out.get("registrar_abuse_email") or sv.get("email")
                    out["registrar_abuse_phone"] = out.get("registrar_abuse_phone") or sv.get("tel")
        if any(r_ in roles for r_ in ("registrant", "administrative", "technical")):
            out["registrant"] = out.get("registrant") or v.get("org") or v.get("name")
            out["registrant_name"] = out.get("registrant_name") or v.get("name")
            out["owner_email"] = out.get("owner_email") or v.get("email")
            out["registrant_city"] = out.get("registrant_city") or v.get("city")
            out["registrant_state"] = out.get("registrant_state") or v.get("state")
            out["registrant_country"] = out.get("registrant_country") or v.get("country")
    for link in (d.get("links") or []):
        href = link.get("href")
        if (link.get("rel") == "related" and isinstance(href, str) and "://" in href
                and "rdap" not in href.lower()):   # skip RDAP-endpoint links — want the registrar's site
            out.setdefault("registrar_url", href)
    sd = d.get("secureDNS") or {}
    out["dnssec"] = ("signed" if sd.get("delegationSigned") else "unsigned") if sd else None
    out["created"] = events.get("registration")
    out["updated"] = events.get("last changed") or events.get("last update of RDAP database")
    out["expires"] = events.get("expiration")
    out["status"] = d.get("status") or []
    out["nameservers"] = [ns.get("ldhName") for ns in (d.get("nameservers") or []) if ns.get("ldhName")]
    return out
