"""Website-finder — auto-resolve a booked prospect's REAL website when no outbound-pool row exists.

THE PROBLEM this fixes (recurring): when Lisa books a prospect that isn't in the outbound pool
(inbound calls, referrals) there is no `lisa4_pool` row, so `build_website` has no `domain` and the
reveal sits `queued` with `domain=None` forever — no site ever gets built. The site usually EXISTS and
is findable by Googling the business name.

Strategy (accuracy over coverage — a WRONG site at a reveal is worse than none):
  1. find booked prospects with an active stage and NO usable domain (no pool row / null domain) and no
     built reveal, that still have a business identity to search.
  2. resolve a domain cheapest-first:  companies-by-PHONE  →  companies-by-NAME  →  DataForSEO organic SERP.
  3. VERIFY every candidate by fetching its homepage:
        * PHONE MATCH (gold): the booking's mobile (trailing-9) appears on the page / in a tel: link → HIGH.
        * NAME MATCH: the distinctive business-name tokens appear → MEDIUM (→ HIGH if locality also matches).
        * DIRECTORY GUARD: a multi-business directory/aggregator page is REJECTED even on a loose name match.
  4. act on confidence:  HIGH → (optionally) wire lisa4_pool + make the reveal buildable;  MEDIUM/LOW →
     flag `needs_human_review`, NEVER auto-wire.

Fully guarded — never raises into the caller; idempotent (skips prospects already wired / built); the
paid SERP is self-throttled via crm_config so it can be called every loop pass without repeated cost.
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

import httpx
from psycopg_pool import ConnectionPool

from .config import Settings
from .enrichment.website import BROWSER_HEADERS
from .enrichment.dataforseo import MEGA_DOMAINS
from .logging import get_logger

log = get_logger(__name__)


def _fetch(pool: ConnectionPool, sql: str, params=None) -> list[dict]:
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(sql, params or ())
        return cur.fetchall()


def _d9(s: str | None) -> str:
    return re.sub(r"[^0-9]", "", s or "")[-9:]


# --------------------------------------------------------------------------- #
# Directory / aggregator denylist — never present one of these as a prospect's OWN site.
# (union of the SEO mega-domain list + AU business directories / lead-gen aggregators)
# --------------------------------------------------------------------------- #
_DIRECTORY_DOMAINS = MEGA_DOMAINS | {
    "yellowpages.com.au", "whitepages.com.au", "truelocal.com.au", "localsearch.com.au",
    "hotfrog.com.au", "aussieweb.com.au", "startlocal.com.au", "wordofmouth.com.au",
    "purelocal.com.au", "dlook.com.au", "yellow.com.au", "cylex.com.au", "cylex-australia.com",
    "brownbook.net", "fyple.com.au", "australianplanet.com", "aussie.com.au", "localbd.com.au",
    "hipages.com.au", "oneflare.com.au", "airtasker.com", "serviceseeking.com.au", "serviceios.com",
    "yelp.com", "yelp.com.au", "zomato.com", "menulog.com.au", "ubereats.com", "doordash.com",
    "healthengine.com.au", "hotdoc.com.au", "healthdirect.gov.au", "cleardocs.com", "womo.com.au",
    "google.com.au", "maps.google.com", "business.site", "linktr.ee", "abr.business.gov.au",
    "trustpilot.com", "productreview.com.au", "finder.com.au", "ratemyagent.com.au",
    "realestate.com.au", "domain.com.au", "gumtree.com.au", "ebay.com.au", "seek.com.au",
    "fresha.com", "magicpin.com", "mapquest.com", "centralindex.com", "bookwell.com.au",
    "styleseat.com", "booksy.com", "clubapp.com.au", "cybo.com", "tupalo.net", "nicelocal.com.au",
}
# phrases that betray a multi-business directory / lead-marketplace page (not a single business's site)
_DIRECTORY_PHRASES = (
    "add your business", "add a business", "claim your listing", "claim this business",
    "claim this listing", "list your business", "list my business", "advertise with us",
    "get free quotes", "get quotes from", "compare quotes", "compare businesses", "browse businesses",
    "businesses near you", "find a business", "find a tradie", "find local businesses",
    "join as a business", "are you a business owner", "similar businesses", "related businesses",
    "other businesses", "business directory", "trades directory", "search results",
)
# generic tokens that are NOT distinctive identity for name-matching
_NAME_STOP = {
    "the", "and", "for", "pty", "ltd", "limited", "inc", "co", "company", "group", "holdings",
    "australia", "australian", "aust", "au", "com", "services", "service", "solutions", "trading",
    "enterprises", "industries", "international", "global", "trust", "trustee", "proprietary",
    "your", "our", "best", "local", "near", "home", "welcome", "index",
}
_NEUTRAL_ISSUE = ("Current site could be presenting the business better — a modern, mobile-first rebuild "
                  "with clear service pages and an easy quote/booking path would help win more local work.")

# strip trailing legal/entity suffix off a registered name so a clean search string survives
_LEGAL_SUFFIX_RE = re.compile(
    r"[\s,]+(PTY\.?\s*LTD\.?|PTY\.?|LTD\.?|P/?L|LIMITED|INC\.?|INCORPORATED|CORP\.?|& CO\.?|CO\.?)\.?\s*$", re.I)
_TAG_RE = re.compile(r"<(script|style|noscript)\b[^>]*>.*?</\1>", re.I | re.S)
_ANYTAG_RE = re.compile(r"<[^>]+>")
_TEL_RE = re.compile(r"""tel:([+0-9()\-.\s]+)""", re.I)
# Well-formed AU phone shapes (mobile 04xx xxx xxx / landline 0x xxxx xxxx / +61…) — informational only
# (distinct-number count is reported but NOT used to reject a page: it is far too noisy — a legit single-
# business site routinely yields many matches from ABNs, scripts and repeated formats. Directory detection
# keys on the denylist + aggregator PHRASES instead, and a gold phone-match is NEVER overridden by a count).
_AU_PHONE_RE = re.compile(
    r"(?:\+?61[ .\-]?)?(?:"
    r"0?4\d{2}[ .\-]?\d{3}[ .\-]?\d{3}"          # mobile
    r"|\(?0?[2-8]\)?[ .\-]?\d{4}[ .\-]?\d{4}"      # landline
    r")")


def _is_directory_domain(domain: str) -> bool:
    h = (domain or "").lower().strip().removeprefix("www.")
    if not h:
        return True
    return h in _DIRECTORY_DOMAINS or any(h == m or h.endswith("." + m) for m in _DIRECTORY_DOMAINS)


def _search_name(company: str | None) -> str:
    """A clean, search-friendly business string ('C & D SCHROEDER TREE SERVICES PTY LTD' →
    'C & D Schroeder Tree Services'). Keeps a descriptive tagline; only trims legal suffixes."""
    c = " ".join((company or "").replace("—", " ").replace("|", " ").split())
    if not c:
        return ""
    prev = None
    while prev != c:
        prev = c
        c = _LEGAL_SUFFIX_RE.sub("", c).strip().rstrip(",").strip()
    # a bare number placeholder ('0415816882') is not a name
    if len(re.sub(r"[^A-Za-z]", "", c)) < 2:
        return ""
    return c


def _name_from_note(note: str | None) -> str:
    """Pull a business name out of Alfred's Aircall note ('… Business: Brad Everton, Psychologist …')."""
    m = re.search(r"Business:\s*([^.\n]+)", note or "", re.I)
    return _search_name(m.group(1)) if m else ""


def _brand_tokens(name: str) -> list[str]:
    """Distinctive identity tokens for name-matching — surname/brand words, most-distinctive first.
    'C & D Schroeder Tree Services' → ['schroeder'] (drops generic 'services'/'tree' when a longer,
    rarer token exists); falls back to all non-stop tokens if nothing len>=5 survives."""
    toks = [t.lower() for t in re.split(r"[^A-Za-z0-9]+", name or "") if len(t) >= 3]
    toks = [t for t in toks if t not in _NAME_STOP]
    strong = [t for t in toks if len(t) >= 5]
    ordered = sorted(strong, key=len, reverse=True) or toks
    # de-dup, preserve order
    seen, out = set(), []
    for t in ordered:
        if t not in seen:
            seen.add(t); out.append(t)
    return out


def _visible_text(html: str) -> str:
    t = _TAG_RE.sub(" ", html or "")
    t = _ANYTAG_RE.sub(" ", t)
    return " ".join(t.split()).lower()


def _fetch_page(domain: str, *, timeout: float = 8.0, max_bytes: int = 1_500_000) -> dict:
    """Guarded homepage fetch for VERIFICATION — https then http, follow redirects, size-capped, never
    raises. Returns {found, final_url, html, text, tel_digits:set, http_status}. TLS unverified (many
    small-business sites serve a valid page behind a bad cert; public homepage only, no credentials)."""
    d = (domain or "").strip().lower().removeprefix("https://").removeprefix("http://").strip("/")
    if not d:
        return {"found": False}
    for scheme in ("https", "http"):
        try:
            with httpx.Client(timeout=timeout, follow_redirects=True, headers=BROWSER_HEADERS,
                              verify=False) as c:
                resp = c.get(f"{scheme}://{d}")
            html = resp.text or ""
            if len(html) > max_bytes:
                html = html[:max_bytes]
            tel_digits = {_d9(m) for m in _TEL_RE.findall(html) if _d9(m)}
            return {"found": True, "final_url": str(resp.url), "http_status": resp.status_code,
                    "html": html, "html_bytes": len(resp.text or ""), "text": _visible_text(html),
                    "tel_digits": tel_digits}
        except Exception as exc:
            last = str(exc)[:160]
            continue
    return {"found": False, "error": last}


def _verify_domain(page: dict, d9: str, brand_toks: list[str], suburb: str | None) -> dict:
    """Score a fetched candidate page → {ok, phone_match, name_match, locality_match, directory,
    confidence, reason}. confidence ∈ high|medium|low|none. A directory/aggregator page is rejected
    even on a loose name match; a live single-business page carrying the booking's mobile is gold."""
    sig = {"phone_match": False, "name_match": False, "locality_match": False,
           "directory": False, "confidence": "none", "reason": ""}
    if not page.get("found"):
        sig["reason"] = "fetch failed"
        return sig
    st = int(page.get("http_status") or 0)
    if st >= 400 or st == 0:
        sig["reason"] = f"http {st or 'no response'}"
        return sig
    if int(page.get("html_bytes") or 0) < 800:
        sig["reason"] = "blank/parked page"
        return sig
    text = page.get("text") or ""
    html = page.get("html") or ""
    final_host = (urlparse(page.get("final_url") or "").hostname or "").lower().removeprefix("www.")
    sig["resolved_host"] = final_host or None

    # ---- directory / aggregator guard -------------------------------------------------
    # Key on the denylist (known aggregators) + aggregator PHRASES ("claim your listing", "get free
    # quotes", "find a business near you", …). The distinct-phone count is reported for context ONLY —
    # it proved far too noisy to reject on (a legit tree-lopping / engineering site yields 20-70 matches),
    # and it must NEVER override a gold phone-match.
    phrase_hits = sum(1 for p in _DIRECTORY_PHRASES if p in text)
    distinct_phones = {_d9(m) for m in _AU_PHONE_RE.findall(html) if len(_d9(m)) == 9}
    denylisted = bool(final_host) and _is_directory_domain(final_host)
    is_dir = denylisted or phrase_hits >= 2
    sig["directory"] = is_dir
    sig["phrase_hits"] = phrase_hits
    sig["distinct_phones"] = len(distinct_phones)

    # ---- phone match (gold) -----------------------------------------------------------
    digits = re.sub(r"[^0-9]", "", html)
    phone_match = bool(d9) and (d9 in page.get("tel_digits", set()) or d9 in distinct_phones or d9 in digits)
    sig["phone_match"] = phone_match

    # ---- name / locality match --------------------------------------------------------
    def _word_present(tok: str) -> bool:
        return re.search(r"\b" + re.escape(tok) + r"\b", text) is not None
    hits = [t for t in (brand_toks or []) if _word_present(t)]
    name_match = bool(hits)
    sig["name_match"] = name_match
    loc = (suburb or "").strip().lower()
    locality_match = bool(loc) and len(loc) >= 3 and _word_present(loc)
    sig["locality_match"] = locality_match
    sig["name_hits"] = hits

    if is_dir:
        # a directory/aggregator page carrying the number/name is NOT proof it's the prospect's OWN site
        sig["confidence"] = "none"
        sig["reason"] = f"directory/aggregator page (denylisted={denylisted}, phrases={phrase_hits})"
    elif phone_match:
        sig["confidence"] = "high"
        sig["reason"] = "phone match on single-business site"
    elif name_match and locality_match:
        sig["confidence"] = "high"
        sig["reason"] = "business name + locality on single-business site"
    elif name_match:
        sig["confidence"] = "medium"
        sig["reason"] = "business-name match only (no phone / locality)"
    else:
        sig["confidence"] = "low"
        sig["reason"] = "live site but no phone/name evidence"
    return sig


_CONF_RANK = {"high": 3, "medium": 2, "low": 1, "none": 0}


def _domain_has_brand(domain: str, brand_toks: list[str]) -> bool:
    """True when the domain label itself carries a distinctive business token ('bradeverton.com' vs
    'everton'). A strong ownership signal used to pick the CANONICAL site when several candidates phone-
    match (e.g. an owner's number appears on both his own site and a partner org's)."""
    root = (domain or "").split("/")[0].split(".")[0].replace("-", "").replace("_", "").lower()
    return any(t in root for t in (brand_toks or []) if len(t) >= 4)


def _resolve_domains(pool: ConnectionPool, settings: Settings, dfs, d9: str, name: str,
                     suburb: str | None, state: str | None) -> list[tuple[str, str]]:
    """Candidate (domain, source) list, cheapest-first: companies-by-PHONE, companies-by-NAME, then
    DataForSEO organic SERP. Directory/aggregator + already-seen domains are dropped. SERP is only
    reached when the free DB look-ups didn't already yield a candidate to try."""
    out: list[tuple[str, str]] = []
    seen: set[str] = set()

    def _add(dom: str, src: str) -> None:
        h = (dom or "").strip().lower().removeprefix("www.").strip("/")
        h = h.split("/")[0]
        if not h or h in seen or _is_directory_domain(h):
            return
        seen.add(h)
        out.append((h, src))

    # a) companies by PHONE (trailing-9) — cheap, and often the prospect's own domain
    try:
        for r in _fetch(pool, "SELECT DISTINCT lower(domain) domain FROM companies "
                        "WHERE right(regexp_replace(COALESCE(phone,''),'[^0-9]','','g'),9)=%s "
                        "  AND NULLIF(domain,'') IS NOT NULL LIMIT 5", (d9,)):
            _add(r["domain"], "companies_phone")
    except Exception as exc:
        log.warning("website_finder_phone_lookup_failed", error=str(exc)[:140])

    # b) companies by NAME (exact-ish) — medium
    if name:
        try:
            for r in _fetch(pool, "SELECT lower(domain) domain, count(*) n FROM companies "
                            "WHERE lower(company_name)=lower(%s) AND NULLIF(domain,'') IS NOT NULL "
                            "GROUP BY 1 ORDER BY 2 DESC LIMIT 3", (name,)):
                _add(r["domain"], "companies_name")
        except Exception as exc:
            log.warning("website_finder_name_lookup_failed", error=str(exc)[:140])

    # c) DataForSEO organic SERP — the site is almost always googleable by name (+ suburb/state)
    if dfs is not None and name:
        q = " ".join(x for x in (name, suburb, state) if x)
        try:
            for it in dfs.serp_organic(q, depth=10)[:6]:
                _add(it.get("domain"), "serp")
        except Exception as exc:
            log.warning("website_finder_serp_failed", error=str(exc)[:140])
    return out


def _wire(pool: ConnectionPool, d9: str, company: str, domain: str) -> None:
    """HIGH-confidence action: UPSERT lisa4_pool + make the queued reveal eligible to build NOW (mirror
    the manual fix: give the reveal its domain and a created_at before the confirm-gate so the drainer
    builds it without waiting for a 'confirmed' stage). Idempotent — never touches a 'built'/'building'
    row; never overwrites an existing pool domain. Logs to crm_activity. Guarded by the caller."""
    from . import lisa4 as _l4
    _l4.ensure_lisa4_tables(pool)
    gate = _l4._confirm_gate_from(pool)
    dest_number = "0" + d9
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO lisa4_pool (dest9, dest_number, company, domain, bucket, issue, priority) "
            "VALUES (%s,%s,%s,%s,'critical_issue',%s,50) "
            "ON CONFLICT (dest9) DO UPDATE SET "
            "  domain=COALESCE(NULLIF(lisa4_pool.domain,''), EXCLUDED.domain), "
            "  company=COALESCE(NULLIF(lisa4_pool.company,''), EXCLUDED.company), "
            "  bucket=COALESCE(NULLIF(lisa4_pool.bucket,''), EXCLUDED.bucket), "
            "  issue=COALESCE(NULLIF(lisa4_pool.issue,''), EXCLUDED.issue)",
            (d9, dest_number, company or None, domain, _NEUTRAL_ISSUE))
        row = cur.execute("SELECT id, status, domain FROM lisa4_sites WHERE dest9=%s "
                          "ORDER BY id DESC LIMIT 1", (d9,)).fetchone()
        if not row:
            cur.execute("INSERT INTO lisa4_sites (dest9, domain, company, bucket, issue, status, created_at) "
                        "VALUES (%s,%s,%s,'critical_issue',%s,'queued', %s::timestamptz - interval '1 minute')",
                        (d9, domain, company or None, _NEUTRAL_ISSUE, gate))
        elif (row.get("status") or "") in ("queued", "error"):
            cur.execute("UPDATE lisa4_sites SET domain=COALESCE(NULLIF(domain,''),%s), "
                        "  company=COALESCE(NULLIF(company,''),%s), status='queued', "
                        "  build_attempts=0, created_at=LEAST(created_at, %s::timestamptz - interval '1 minute') "
                        "WHERE id=%s", (domain, company or None, gate, row["id"]))
        # status 'building'/'built' → leave the in-flight/finished build alone
        cur.execute("INSERT INTO crm_activity (dest9, kind, body, author) VALUES (%s,'website_finder',%s,'website_finder')",
                    (d9, f"Auto-resolved website {domain} (HIGH confidence) and queued the reveal build."))
        conn.commit()


def _candidate_bookings(pool: ConnectionPool, limit: int, days: int = 45) -> list[dict]:
    """Booked prospects that need a site: active stage (not lost/won/no_show), recent, NO usable domain
    (no lisa4_pool row / null domain, and none captured on the booking call), and no BUILT reveal."""
    return _fetch(pool, """
      WITH booked AS (
        SELECT DISTINCT ON (dest9) dest9, call_id, company_name, prospect_name, domain, created_at
        FROM lisa_calls WHERE COALESCE(meeting_agreed,false) AND dest9 IS NOT NULL
        ORDER BY dest9, created_at ASC)
      SELECT b.dest9, b.created_at,
             COALESCE(NULLIF(b.company_name,''), NULLIF(lp.company,''), NULLIF(cl.prospect_company,''),
                      NULLIF(bc.contact_name,'')) AS company,
             bc.note AS crm_note, bc.stage,
             st.status AS site_status,
             co.suburb, co.state
      FROM booked b
      LEFT JOIN lisa4_pool lp ON lp.dest9=b.dest9
      LEFT JOIN booked_crm bc ON bc.dest9=b.dest9
      LEFT JOIN classifications cl ON cl.call_id=b.call_id
      LEFT JOIN LATERAL (SELECT status FROM lisa4_sites s WHERE s.dest9=b.dest9
                         ORDER BY id DESC LIMIT 1) st ON true
      LEFT JOIN LATERAL (SELECT suburb, state FROM companies cc
                         WHERE right(regexp_replace(COALESCE(cc.phone,''),'[^0-9]','','g'),9)=b.dest9
                         ORDER BY (cc.source='gmaps') DESC LIMIT 1) co ON true
      WHERE COALESCE(bc.stage,'') NOT IN ('lost','won','no_show')
        AND b.created_at > now() - make_interval(days => %s)
        AND COALESCE(NULLIF(lp.domain,''), NULLIF(b.domain,''), NULLIF(cl.prospect_website,'')) IS NULL
        -- Never hunt a site for a 'no_website' prospect: that bucket is an authoritative "they have no
        -- site" from the sweep, so any domain we'd find is a namesake/directory false-positive (this
        -- grafted stevecampbellremedialmassage.com onto Specialist Massage Centre). Their reveal builds
        -- fresh from GBP photos + verified Google category — it never needs a found domain.
        AND COALESCE(lp.bucket,'') <> 'no_website'
        AND COALESCE(st.status,'') <> 'built'
      ORDER BY b.created_at DESC
      LIMIT %s
    """, (days, limit))


def _throttle_claim(pool: ConnectionPool, minutes: int) -> bool:
    """Atomic check-and-set (same pattern as bde_capture): claim a run only when the last was >N min ago,
    so the paid SERP fires at most ~every N minutes even though the loop calls us every tick."""
    try:
        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute("INSERT INTO crm_config (k,v) VALUES ('website_finder_last_run', now()::text) "
                        "ON CONFLICT (k) DO UPDATE SET v=now()::text "
                        "WHERE crm_config.v IS NULL "
                        "   OR crm_config.v::timestamptz < now() - make_interval(mins => %s) "
                        "RETURNING v", (int(minutes),))
            claimed = cur.fetchone()
            conn.commit()
        return bool(claimed)
    except Exception:
        return False


def _evaluate_candidate(pool: ConnectionPool, settings: Settings, cand: dict, *, dfs=None,
                        auto_wire: bool = True) -> dict:
    """Resolve + verify one booking → a decision record. Wires only on HIGH when auto_wire is on."""
    d9 = _d9(cand.get("dest9"))
    name = _search_name(cand.get("company")) or _name_from_note(cand.get("crm_note"))
    rec = {"dest9": d9, "company": name or (cand.get("company") or ""), "chosen_domain": None,
           "confidence": "none", "signals": {}, "action": "needs_human_review"}
    if not name:
        rec["action"] = "skipped_no_name"
        rec["signals"] = {"reason": "no business identity to search"}
        return rec
    brand_toks = _brand_tokens(name)
    suburb, state = cand.get("suburb"), cand.get("state")
    candidates = _resolve_domains(pool, settings, dfs, d9, name, suburb, state)
    if not candidates:
        rec["action"] = "skipped_no_candidate"
        rec["signals"] = {"reason": "no candidate domain from phone/name/SERP"}
        return rec

    def _score(dom: str, sig: dict) -> tuple:
        # confidence first, then gold phone-match, then name-in-domain (canonical), then name/locality.
        # Ties keep the earlier (cheaper-source) candidate since we only replace on a STRICTLY greater key.
        return (_CONF_RANK[sig["confidence"]], int(bool(sig.get("phone_match"))),
                int(_domain_has_brand(dom, brand_toks)), int(bool(sig.get("name_match"))),
                int(bool(sig.get("locality_match"))))

    best = None
    evaluated = []
    for dom, src in candidates:
        page = _fetch_page(dom)
        sig = _verify_domain(page, d9, brand_toks, suburb)
        sig["source"] = src
        sig["final_url"] = page.get("final_url")
        evaluated.append({"domain": dom, **sig})
        if best is None or _score(dom, sig) > _score(best[0], best[1]):
            best = (dom, sig)
        # stop ONLY on an ideal canonical match: gold phone-match on a domain that carries the business
        # name. A bare phone-match (number shared across the owner's/partner sites) keeps looking for a
        # name-in-domain match rather than grabbing whichever SERP result came back first.
        if sig["confidence"] == "high" and sig.get("phone_match") and _domain_has_brand(dom, brand_toks):
            break
    rec["evaluated"] = evaluated
    if best is None:
        rec["action"] = "skipped_no_candidate"
        return rec
    dom, sig = best
    # prefer the FINAL redirected host when it's a real, non-directory site (e.g. a candidate that 301s to
    # the business's canonical domain) so we store/build the domain that actually serves the page
    chosen = dom
    rhost = sig.get("resolved_host")
    if rhost and rhost != dom and not _is_directory_domain(rhost):
        chosen = rhost
    rec["chosen_domain"] = chosen
    rec["confidence"] = sig["confidence"]
    rec["signals"] = sig
    if sig["confidence"] == "high":
        if auto_wire:
            try:
                _wire(pool, d9, name, chosen)
                rec["action"] = "wired"
            except Exception as exc:
                rec["action"] = "wire_failed"
                rec["signals"] = {**sig, "wire_error": str(exc)[:160]}
                log.warning("website_finder_wire_failed", dest9=d9, error=str(exc)[:160])
        else:
            rec["action"] = "would_wire"
    else:
        rec["action"] = "needs_human_review"
    return rec


def find_missing_websites(pool: ConnectionPool, settings: Settings, limit: int = 5,
                          auto_wire: bool = True) -> list[dict]:
    """Find booked prospects with no resolvable website, resolve + verify their real site, and (on HIGH
    confidence, when auto_wire) wire lisa4_pool so the reveal builds. Returns a list of decision records
    {dest9, company, chosen_domain, confidence, signals, action}. Never raises; self-throttled (paid SERP)
    when auto_wire is on so it is safe to call every loop pass; idempotent."""
    out: list[dict] = []
    try:
        from . import lisa4 as _l4
        _l4.ensure_lisa4_tables(pool)
        try:
            from . import crm as _crm
            _crm.ensure_crm_tables(pool)
        except Exception:
            pass
        # self-throttle the PAID SERP path in production; the standalone test (auto_wire=False) always runs
        if auto_wire and not _throttle_claim(pool, 15):
            return []
        cands = _candidate_bookings(pool, max(1, int(limit)))
        dfs = None
        try:
            if getattr(settings, "dataforseo_enabled", False):
                from .enrichment.dataforseo import DataForSEOClient
                dfs = DataForSEOClient(settings)
        except Exception as exc:
            log.warning("website_finder_dfs_init_failed", error=str(exc)[:140])
            dfs = None
        try:
            for c in cands:
                try:
                    out.append(_evaluate_candidate(pool, settings, c, dfs=dfs, auto_wire=auto_wire))
                except Exception as exc:
                    log.warning("website_finder_candidate_failed", dest9=c.get("dest9"), error=str(exc)[:160])
                    out.append({"dest9": _d9(c.get("dest9")), "company": c.get("company") or "",
                                "chosen_domain": None, "confidence": "none", "signals": {"error": str(exc)[:160]},
                                "action": "error"})
        finally:
            if dfs is not None:
                try:
                    dfs.close()
                except Exception:
                    pass
        wired = sum(1 for r in out if r.get("action") == "wired")
        review = sum(1 for r in out if r.get("action") == "needs_human_review")
        log.info("find_missing_websites", seen=len(cands), wired=wired, needs_review=review, auto_wire=auto_wire)
        return out
    except Exception as exc:
        log.warning("find_missing_websites_failed", error=str(exc)[:160])
        return out
