"""Lisa 4 — website-selling subsystem. ISOLATED from Lisa-1 (its own pool + agent + sites table).

Strategy = "pre-built reveal": Lisa 4 cold-calls AU businesses that have NO website or a website with a
CRITICAL issue (from the website-audit engine), and books a short screen-share reveal. Only AFTER a reveal
is booked does the AI designer (Claude API) build the actual site — which a human then shows + closes.

Nothing here touches Lisa-1's tables or dialer.
"""

from __future__ import annotations

import json
import urllib.request
import urllib.error
from concurrent.futures import ThreadPoolExecutor

from psycopg.types.json import Json
from psycopg_pool import ConnectionPool

from .config import Settings
from .enrichment.website import fetch_website_intel, website_audit
from .logging import get_logger

log = get_logger(__name__)

_D9 = "right(regexp_replace(COALESCE(%s,''),'[^0-9]','','g'),9)"


def _fetch(pool: ConnectionPool, sql: str, params=None) -> list[dict]:
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(sql, params or ())
        return cur.fetchall()


import re as _re  # noqa: E402
import html as _html  # noqa: E402
_LEGAL_RE = _re.compile(r"( & |\bP/?L\b|PTY\.?|LTD\.?|\bTRUST\b|\bUNIT TRUST\b|SUPERANNUATION|\bTHE TRUSTEE\b)", _re.I)
# Trailing legal/entity suffixes to strip off a registered name so a real trading name survives the
# personal-name test ('GW LOGISTICS PTY LTD' -> 'GW Logistics', not discarded as if it were a person).
_LEGAL_SUFFIX_RE = _re.compile(
    r"[\s,]+(PTY\.?\s*LTD\.?|PTY\.?|LTD\.?|P/?L|LIMITED|INC\.?|INCORPORATED|CORP\.?|& CO\.?|CO\.?)\.?\s*$", _re.I)
# HTTP-error / CDN-challenge / parked-page <title>s scraped as if they were trading names. Spoken on a call
# ("is that the owner of 403 forbidden?") they instantly out Lisa as a broken robocall — never speak them.
_JUNK_TITLE_RE = _re.compile(
    r"^\s*$|^\s*(4\d\d|5\d\d)\b|\b(40[0-9]|50[0-9])\s+(forbidden|not\s+found|unauthorized|error|bad\s+gateway|"
    r"service\s+unavailable|gateway\s+time)|forbidden|unauthorized|\bnot\s+found\b|access\s+denied|"
    r"just\s+a\s+moment|attention\s+required|cloudflare|are\s+you\s+(a\s+)?human|coming\s+soon|"
    r"under\s+construction|service\s+unavailable|bad\s+gateway|website\s+(coming|expired)|domain\s+for\s+sale|"
    r"this\s+domain|brand\s+new\s+domain|\bparked\b|default\s+(web\s+)?page|^index\s+of\b|directory\s+listing|"
    r"site\s+not\s+found|temporarily\s+unavailable|maintenance\s+mode|placeholder|account\s+suspended", _re.I)


def _clean_title(title: str | None) -> str:
    """Pull a human trading name out of a page <title> ('The Good Blinds | Melbourne Blinds' -> 'The Good
    Blinds'). Drops obvious boilerplate + HTTP-error/parked page titles; returns '' if nothing usable."""
    t = _html.unescape(title or "").strip()
    if not t:
        return ""
    for sep in ("|", " - ", " – ", " — ", "::", " • ", "•", " : "):
        if sep in t:
            t = t.split(sep)[0].strip()
            break
    # drop trailing non-Latin script segments (SEO titles append CJK/Cyrillic/Arabic) — keep accented Latin
    t = _re.sub(r"[　-鿿가-힯Ѐ-ӿ֐-ۿ].*$", "", t).strip()
    if _JUNK_TITLE_RE.search(t):
        return ""
    if len(t) < 2 or len(t) > 60 or t.lower() in ("home", "welcome", "index"):
        return ""
    return t


def _display_from_domain(domain: str | None) -> str:
    """Fallback trading name from a domain label ('the-good-blinds.com.au' -> 'The Good Blinds'). Weak for
    run-together labels but better than a legal/partnership name on a call."""
    d = (domain or "").lower().strip()
    if not d:
        return ""
    label = d.split("//")[-1].split("/")[0].removeprefix("www.").split(".")[0]
    label = _re.sub(r"[-_]+", " ", label).strip()
    if label and " " not in label:
        # split off a trailing known trade token so a run-together label is speakable
        # ('markpettitlandscaping' -> 'markpettit landscaping')
        for w in sorted(_BIZ_WORDS, key=len, reverse=True):
            wl = w.lower()
            if len(wl) >= 4 and label.endswith(wl) and len(label) > len(wl):
                label = label[:-len(wl)] + " " + wl
                break
    return label.title() if label else ""


def _looks_personal(name: str | None) -> bool:
    """True if the string reads as a person/partnership legal entity, not a trading name — e.g.
    'H BASSILIOS & S BASSILIOS', 'J.V LANDMANN & D.B VOLF', 'ENG TONG NG'. Reading these out on a call is
    an instant robocall tell, so we'd rather open with no name than with this."""
    n = (name or "").strip()
    if not n:
        return True
    if _LEGAL_RE.search(n) or " & " in n:
        return True
    if _re.match(r"^[A-Z]([.\s]|$)", n):          # leads with an initial: "H Barton", "J.V Landmann"
        return True
    if "THE TRUSTEE" in n.upper() or "THE TRUST" in n.upper():
        return True
    # Whitelist test: a short all-alpha string with NO business-y word is a person ("GUO PING TANG",
    # "ENG TONG NG") — sole traders registered under their own name. Better nameless than robocall-y.
    toks = [t for t in _re.split(r"[^A-Za-z]+", n) if t]
    # single made-up tokens (BEEJEWEL) are trading names; 2-4 plain words with no trade word are people
    if 2 <= len(toks) <= 4 and not any(t.upper() in _BIZ_WORDS for t in toks):
        return True
    return False


_BIZ_WORDS = {
    "PTY", "LTD", "CO", "GROUP", "HOLDINGS", "ENTERPRISES", "INDUSTRIES", "TRADING", "AUSTRALIA", "AUST",
    "SERVICES", "SERVICE", "SOLUTIONS", "SUPPLIES", "STORE", "SHOP", "STUDIO", "CENTRE", "CENTER", "HOUSE",
    "PLUMBING", "ELECTRICAL", "BUILDERS", "BUILDING", "CONSTRUCTION", "CONSTRUCTIONS", "ROOFING", "FENCING",
    "PAINTING", "PAINTERS", "TILING", "FLOORING", "KITCHENS", "JOINERY", "CABINETS", "GLAZING", "GLASS",
    "WELDING", "ENGINEERING", "MECHANICAL", "AUTO", "MOTORS", "TYRES", "TRANSPORT", "LOGISTICS", "REMOVALS",
    "CAFE", "RESTAURANT", "TAKEAWAY", "PIZZA", "BAKERY", "BUTCHER", "FISH", "MEATS", "FRUIT", "LIQUOR",
    "CELLARS", "HOTEL", "MOTEL", "LODGE", "SALON", "HAIR", "BEAUTY", "CLEANING", "GARDENING", "LANDSCAPING",
    "NURSERY", "FARM", "VET", "KENNELS", "DENTAL", "CLINIC", "MEDICAL", "PHYSIO", "PHARMACY", "OPTICAL",
    "LEGAL", "LAWYERS", "ACCOUNTING", "ACCOUNTANTS", "FINANCE", "FINANCIAL", "REALTY", "PROPERTY", "ESTATE",
    "PRINTING", "SIGNS", "PHOTOGRAPHY", "TRAVEL", "CONSULTING", "CHILDCARE", "KINDER", "ACADEMY", "SCHOOL",
    "GYM", "FITNESS", "DANCE", "MUSIC", "SECURITY", "ELECTRONICS", "COMPUTERS", "PLASTICS", "PACKAGING",
    "BRICKLAYING", "CONCRETE", "CONCRETING", "EXCAVATIONS", "EARTHMOVING", "DEMOLITION", "SCAFFOLDING",
    "UPHOLSTERY", "ANTENNAS", "LOCKSMITHS", "PEST", "SOLAR", "AIR", "HEATING", "COOLING", "REFRIGERATION",
    "REPAIRS", "REPAIR", "ROOF", "TILE", "TILES", "BLINDS", "DOORS", "WINDOWS", "POOLS", "SPAS",
    "BATHROOMS", "PLASTERING", "RENDERING", "CARPENTRY", "MAINTENANCE", "HIRE", "RENTALS", "CATERING",
    "CLEANERS", "GUTTERING", "SHEDS", "STONEMASONS", "JEWELLERS", "FLORIST", "FLOWERS", "FASHION",
    "CLOTHING", "FURNITURE", "BEDDING", "CURTAINS", "LIGHTING", "HARDWARE", "TOOLS", "EQUIPMENT",
    "MACHINERY", "MARINE", "BOATS", "CARAVANS", "TRAILERS", "TATTOO", "BARBER", "NAILS", "MASSAGE",
    "THERAPY", "TUTORING", "DRIVING", "PLUMBERS", "ELECTRICIANS", "MIGRATION", "INSURANCE", "MORTGAGE",
    "BROKERS", "AGENCY", "MEDIA", "DESIGN", "GRAPHICS", "MARKETING", "EVENTS", "PRODUCTIONS", "BAR",
    "GRILL", "KEBABS", "SUSHI", "CHICKEN", "BURGERS", "CHIPS", "NEWSAGENCY", "POST", "OPTOMETRIST",
    "PODIATRY", "PHYSIOTHERAPY", "OSTEOPATHY", "ACUPUNCTURE", "WELLNESS", "YOGA", "PILATES", "EXCAVATION",
}


def _title_biz(s: str) -> str:
    """Title-case a registered name for speaking, but keep short all-caps acronyms intact ('GW' stays 'GW',
    'LOGISTICS' -> 'Logistics')."""
    out = []
    for w in s.split():
        if w.isupper() and len(w) <= 3:
            out.append(w)
        elif w.isupper():
            out.append(w[:1] + w[1:].lower())
        else:
            out.append(w)
    return " ".join(out)


def _clean_company(company: str | None) -> str:
    """A speakable trading name from a registered company name: strip trailing legal suffixes so a real
    business survives ('GW LOGISTICS PTY LTD' -> 'GW Logistics'). Returns '' only for a pure person/
    partnership name (a legal person read aloud is a robocall tell)."""
    c = (company or "").strip()
    if not c:
        return ""
    c = _re.sub(r"^THE TRUSTEE FOR\s+", "", c, flags=_re.I).strip()
    c = _re.sub(r"^T/?A\s+", "", c, flags=_re.I).strip()
    prev = None
    while prev != c:                                   # strip stacked suffixes ('... PTY LTD')
        prev = c
        c = _LEGAL_SUFFIX_RE.sub("", c).strip().rstrip(",").strip()
    if len(c) < 2 or _looks_personal(c):
        return ""
    return _title_biz(c)


def _pick_name(company: str | None, title: str | None, domain: str | None) -> str:
    """Best on-call name: a clean site title, else the registered trading name (legal suffixes stripped),
    else a domain-derived trading name. A nameless open ('is that the owner of the business?') triggers an
    'owner of what?' spam loop, so prefer any speakable name over ''."""
    ct = _clean_title(title)
    if ct:
        return ct
    cc = _clean_company(company)
    if cc:
        return cc
    return _display_from_domain(domain)


def _owner_first_from_company(company: str | None) -> str:
    """For a NO-WEBSITE sole trader, the registered name IS the owner ('ROBYN JANE BETTS' -> 'Robyn') and
    that owner is who answers the listed mobile — so Lisa can open 'is that Robyn?' (free RPC, no Apollo).
    Only genuine single-person names; partnerships / initial-led names / business names return ''."""
    c = (company or "").strip()
    if not c or " & " in c or "," in c or "/" in c:      # partnership / multi-entity → ambiguous, skip
        return ""
    c = _LEGAL_SUFFIX_RE.sub("", c).strip()              # 'GW LOGISTICS PTY LTD' -> 'GW LOGISTICS' first
    toks = [t for t in _re.split(r"[^A-Za-z]+", c) if t]
    if not (2 <= len(toks) <= 4):                         # a person is 2-4 words (not 'SMITH' or a long name)
        return ""
    if any(t.upper() in _BIZ_WORDS for t in toks):        # any trade word => it's a business, not a person
        return ""
    if len(toks[0]) < 2 or toks[0].lower() in ("the", "mr", "mrs", "ms", "dr", "miss"):
        return ""                                         # leads with an initial/title => no usable first name
    return toks[0][:1].upper() + toks[0][1:].lower()


def _soft_issue(intel: dict) -> str:
    """A TRUE improvement angle for a working site (never a fabricated fault). Falls back to an opinion
    line — subjective by design, so it can't be 'caught out' like a false factual claim."""
    if intel.get("is_https") is False:
        return "coming up as not-secure in browsers"
    if intel.get("has_viewport") is False:
        return "not mobile-friendly, and most people will open it on a phone"
    try:
        if int(intel.get("load_ms") or 0) > 3000:
            return "taking ages to load"
    except Exception:
        pass
    return "looking a bit dated, honestly - it could be doing a lot more for you"


# lisa4_pool.issue doubles as an internal state/sourcing marker on some rows ('mobile-reachable owner'
# from a feeder, 'dead number' / 'wrong number - unreachable' / 'opt-out (SMS)' from post-call cleanup).
# Those are bookkeeping, not website findings — if one leaks into the brief, Lisa reads it out VERBATIM
# on the call. Blank them; the prompt then falls back to its generic no-issue framing.
_INTERNAL_ISSUES = {"mobile-reachable owner", "dead number", "wrong number - unreachable",
                    "wrong number", "opt-out (sms)", "opt-out"}


def _speakable_issue(issue: str | None) -> str:
    s = (issue or "").strip()
    return "" if s.lower() in _INTERNAL_ISSUES else s


# --------------------------------------------------------------------------- #
# Owner's STANDING EXCLUSION RULE — businesses Lisa-4 must NEVER pitch a website to.
# Classes:
#   agency           — they sell what we sell (digital/marketing/media/creative/ad agencies,
#                      advertising, SEO, web design/development, branding, PPC, lead generation)
#   franchise        — chains/franchises (gmaps._CHAIN_BLOCK is the canonical name list)
#   real_estate      — real-estate industry (franchise-heavy, corporate sites already)
#   portal_directory — a domain shared by >=3 `companies` rows is a portal/directory/aggregator
#                      listing, never the prospect's own site
# lisa4_exclusion_class() is the SINGLE source of truth. Every path that INSERTs into lisa4_pool
# or schedules Lisa4 fresh_call events must run candidates through it (plus the derived SQL
# pre-filter _L4X_SQL, same terms with \b→\y, so LIMIT'd feeder batches don't clog on rows
# Python would drop anyway).
# --------------------------------------------------------------------------- #
from .gmaps import _CHAIN_BLOCK  # noqa: E402  (gmaps has no module-level import of lisa4 — no cycle)

_L4X_AGENCY_TERMS = [
    r"\bmarketing\b", r"\bmedia\b", r"\bcreative\b",
    r"\bdigital\s+agenc", r"\bad\s+agenc", r"\badvertis",
    r"\bseo\b", r"\bsearch\s+engine\s+optimi",
    r"\bweb\s*(site)?\s*design", r"\bweb\s*(site)?\s*develop", r"\bweb\s+dev\b",
    r"\bbranding\b", r"\bppc\b", r"\blead\s*gen(eration)?\b",
]
_L4X_REALESTATE_TERMS = [
    r"\breal[\s-]*estate\b", r"\brealty\b", r"\bestate\s+agent",
    r"\bproperty\s+manage", r"\bbuyers?\s+agent",
]
_L4X_AGENCY_RE = _re.compile("|".join(_L4X_AGENCY_TERMS), _re.I)
_L4X_REALESTATE_RE = _re.compile("|".join(_L4X_REALESTATE_TERMS), _re.I)
# Postgres flavour of the SAME terms (POSIX word boundary is \y, not \b)
_L4X_NAME_INDUSTRY_RX = "|".join(t.replace("\\b", "\\y") for t in _L4X_AGENCY_TERMS + _L4X_REALESTATE_TERMS)
_L4X_CHAIN_RX = "|".join(sorted(_re.escape(t) for t in _CHAIN_BLOCK))
_L4X_SQL = ("COALESCE(co.company_name,'') !~* %s AND COALESCE(co.industry,'') !~* %s "
            "AND COALESCE(co.company_name,'') !~* %s")
_L4X_SQL_PARAMS = (_L4X_NAME_INDUSTRY_RX, _L4X_NAME_INDUSTRY_RX, _L4X_CHAIN_RX)


def lisa4_exclusion_class(company: str | None, industry: str | None = None, domain: str | None = None,
                          portal_domains: set[str] | None = None) -> str | None:
    """THE shared owner-rule gate: classify one candidate → 'agency' | 'franchise' | 'real_estate' |
    'portal_directory', or None when Lisa-4 may pitch them. `portal_domains` = precomputed set from
    lisa4_portal_domains() (portal detection is skipped when None — it needs a DB look)."""
    text = f"{company or ''} {industry or ''}"
    nn = " ".join(text.lower().replace("&amp;", "&").split())
    if any(b in nn for b in _CHAIN_BLOCK):
        return "franchise"
    if _L4X_AGENCY_RE.search(text):
        return "agency"
    if _L4X_REALESTATE_RE.search(text):
        return "real_estate"
    d = (domain or "").strip().lower().removeprefix("www.")
    if d and portal_domains and d in portal_domains:
        return "portal_directory"
    return None


def lisa4_portal_domains(pool: ConnectionPool, domains: list) -> set[str]:
    """Which of `domains` are portals/directories — i.e. appear on >=3 `companies` rows (a shared
    domain is a listing page, not the prospect's own site). One READ-ONLY aggregate query;
    www-insensitive on both sides."""
    doms = {(d or "").strip().lower().removeprefix("www.") for d in domains if d}
    doms.discard("")
    if not doms:
        return set()
    variants = sorted(doms | {"www." + d for d in doms})
    counts: dict[str, int] = {}
    for r in _fetch(pool, "SELECT lower(domain) d, count(*) n FROM companies "
                    "WHERE lower(domain) = ANY(%s) GROUP BY 1", (variants,)):
        k = r["d"].removeprefix("www.")
        counts[k] = counts.get(k, 0) + int(r["n"])
    return {d for d, n in counts.items() if n >= 3}


def lisa4_pool_quality_report(pool: ConnectionPool) -> dict:
    """READ-ONLY audit of the CURRENT lisa4_pool against the owner's standing exclusion rule:
    counts (and a few example names) per exclusion class. Never deletes or updates anything —
    the feeders' WHERE/filters stop NEW offenders; this just measures what's already inside."""
    rows = _fetch(pool, "SELECT dest9, company, domain, bucket FROM lisa4_pool")
    ind: dict[str, str] = {}
    d9s = [r["dest9"] for r in rows]
    if d9s:
        ind = {x["d9"]: x["industry"] for x in _fetch(pool,
            "SELECT right(regexp_replace(COALESCE(phone,''),'[^0-9]','','g'),9) d9, max(industry) industry "
            "FROM companies WHERE NULLIF(industry,'') IS NOT NULL "
            "  AND right(regexp_replace(COALESCE(phone,''),'[^0-9]','','g'),9) = ANY(%s) GROUP BY 1", (d9s,))}
    portal = lisa4_portal_domains(pool, [r.get("domain") for r in rows])
    counts = {"agency": 0, "franchise": 0, "real_estate": 0, "portal_directory": 0}
    examples: dict[str, list] = {k: [] for k in counts}
    for r in rows:
        cls = lisa4_exclusion_class(r.get("company"), ind.get(r["dest9"]), r.get("domain"), portal)
        if cls:
            counts[cls] += 1
            if len(examples[cls]) < 5:
                examples[cls].append(r.get("company") or r["dest9"])
    return {"pool": len(rows), "excluded_total": sum(counts.values()), "counts": counts, "examples": examples}


def ensure_lisa4_tables(pool: ConnectionPool) -> None:
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "CREATE TABLE IF NOT EXISTS lisa4_pool ("
            "  dest9 text PRIMARY KEY, dest_number text, company text, domain text,"
            "  bucket text, issue text, title text, email text, priority integer DEFAULT 0, reserved_at timestamptz DEFAULT now())")
        cur.execute("ALTER TABLE lisa4_pool ADD COLUMN IF NOT EXISTS title text")
        cur.execute("ALTER TABLE lisa4_pool ADD COLUMN IF NOT EXISTS email text")
        cur.execute("ALTER TABLE lisa4_pool ADD COLUMN IF NOT EXISTS sms_sent boolean DEFAULT false")
        cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_lisa4_sites_active ON lisa4_sites (dest9) "
                    "WHERE status IN ('queued','building','built')")
        # one row per prospect the AI designer builds a site for (queued on a booked reveal)
        cur.execute(
            "CREATE TABLE IF NOT EXISTS lisa4_sites ("
            "  id bigserial PRIMARY KEY, dest9 text, domain text, company text, bucket text, issue text,"
            "  html text, status text DEFAULT 'queued', model text, meeting_event_id bigint,"
            "  error text, created_at timestamptz DEFAULT now(), built_at timestamptz)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_lisa4_sites_dest9 ON lisa4_sites(dest9)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_lisa4_sites_status ON lisa4_sites(status)")
        cur.execute("ALTER TABLE lisa4_sites ADD COLUMN IF NOT EXISTS build_attempts integer DEFAULT 0")
        cur.execute("ALTER TABLE lisa4_sites ADD COLUMN IF NOT EXISTS building_at timestamptz")
        conn.commit()


def reserve_lisa4_pool(pool: ConnectionPool, settings: Settings, scan_batch: int = 60) -> dict:
    """Top up lisa4_pool toward lisa4_pool_size with AU prospects that have a phone AND (no website OR a
    critical website issue), EXCLUDING anyone already in Lisa-1's pool/calls or Lisa-4's own pool.
    Critical-issue candidates are scanned live (concurrently) and classified by the audit engine."""
    ensure_lisa4_tables(pool)
    target = int(getattr(settings, "lisa4_pool_size", 500))
    # D&B (raghav) backfill gate. Default OFF (LISA4_USE_DNB_BACKFILL=false): Lisa-4 fills from gmaps stock
    # ONLY (§0 below) — the D&B dataset is reserved for Lisa-1/human calling. When off, §1 (critical-issue)
    # and §2 (no-website) below are skipped. Existing lisa4_pool rows are NEVER removed by this flag.
    use_dnb = bool(getattr(settings, "lisa4_use_dnb_backfill", False))
    have = _fetch(pool, "SELECT count(*) n FROM lisa4_pool")[0]["n"]
    need = target - have
    if need <= 0:
        # Pool is full — but critical_issue prospects (the strong hook) are the scarce, perishable stock:
        # keep scanning for them whenever unworked critical supply runs low, even at full pool.
        crit_left = _fetch(pool,
            "SELECT count(*) n FROM lisa4_pool lp WHERE lp.bucket='critical_issue' "
            "  AND NOT EXISTS (SELECT 1 FROM lisa_calls lc WHERE lc.dest9=lp.dest9)")[0]["n"]
        if crit_left >= 60:
            return {"reserved": 0, "pool": have, "note": "full"}
        need = 0  # no-website fill stays closed; only the critical scan below runs

    inserted = 0
    # ---- 0) GOOGLE-MAPS stock FIRST (Lisa-4's own source): small professional-services businesses with a
    # LISTED MOBILE (at a micro firm the listed 04xx IS the owner — the top authority). The Maps display
    # name goes into `title` so it's spoken as-is on the call. D&B only fills what's left after this.
    if need > 0:
        gm = _fetch(pool,
            "SELECT co.company_name, co.industry, co.domain, co.phone, "
            "  right(regexp_replace(co.phone,'[^0-9]','','g'),9) d9 "
            "FROM companies co WHERE co.source='gmaps' AND co.phone_is_mobile "
            "  AND NULLIF(co.phone,'') IS NOT NULL "
            "  AND length(right(regexp_replace(co.phone,'[^0-9]','','g'),9))=9 "
            "  AND " + _L4X_SQL + "   "  # owner's standing exclusion (SQL pre-filter, keeps the LIMIT batch clean)
            "  AND NOT EXISTS (SELECT 1 FROM lisa4_pool p WHERE p.dest9=right(regexp_replace(co.phone,'[^0-9]','','g'),9)) "
            "  AND NOT EXISTS (SELECT 1 FROM lisa_pool lp WHERE lp.dest9=right(regexp_replace(co.phone,'[^0-9]','','g'),9)) "
            "  AND NOT EXISTS (SELECT 1 FROM lisa_calls lc WHERE lc.dest9=right(regexp_replace(co.phone,'[^0-9]','','g'),9)) "
            "  AND NOT EXISTS (SELECT 1 FROM calls hc WHERE right(regexp_replace(COALESCE(hc.dest_number,''),'[^0-9]','','g'),9)=right(regexp_replace(co.phone,'[^0-9]','','g'),9)) "
            "ORDER BY co.id LIMIT %s", (*_L4X_SQL_PARAMS, min(need, scan_batch * 2)))
        # owner's standing exclusion — the shared helper is the authority (adds franchise-substring
        # + portal/directory checks the SQL pre-filter can't do)
        if gm:
            _portal = lisa4_portal_domains(pool, [r.get("domain") for r in gm])
            _kept = [r for r in gm if not lisa4_exclusion_class(r.get("company_name"), r.get("industry"),
                                                                r.get("domain"), _portal)]
            if len(_kept) < len(gm):
                log.info("lisa4_reserve_excluded", path="gmaps", n=len(gm) - len(_kept))
            gm = _kept
        rows0 = []
        gm_sited = [r for r in gm if (r.get("domain") or "").strip()]
        for r in gm:
            if not (r.get("domain") or "").strip():
                rows0.append((r["d9"], r["phone"], r["company_name"], None, "no_website", "no website",
                              r["company_name"], None))

        def _scan0(r: dict) -> tuple[dict, dict, dict]:
            intel = fetch_website_intel(r["domain"], timeout=8.0, verify=False)
            return r, intel, website_audit(intel)

        if gm_sited:
            with ThreadPoolExecutor(max_workers=12) as ex:
                for r, intel, aud in ex.map(_scan0, gm_sited[:scan_batch]):
                    ttl = _clean_title(intel.get("title")) or r["company_name"]
                    if aud.get("is_target") and aud.get("bucket") == "critical_issue":
                        rows0.append((r["d9"], r["phone"], r["company_name"], r["domain"],
                                      "critical_issue", aud.get("issue"), ttl, None))
                    elif aud.get("bucket") == "ok":
                        # UPGRADE bucket: the site WORKS — pitch improvement, not breakage. Only ever claim
                        # a REAL soft issue; when none is measurable, the line is an opinion, never a fact.
                        rows0.append((r["d9"], r["phone"], r["company_name"], r["domain"],
                                      "upgrade", _soft_issue(intel), ttl, None))
        if rows0:
            # DOMAIN-LEVEL CHANNEL GUARD: a gmaps branch whose DOMAIN also exists in the D&B (raghav)
            # dataset belongs to Lisa-1/human territory — franchise chains (e.g. bigginscott.com.au)
            # have many branch numbers under one domain, so number-based exclusions miss them.
            doms = list({r[3] for r in rows0 if r[3]})
            if doms:
                taken = {x["domain"] for x in _fetch(pool,
                    "SELECT DISTINCT domain FROM companies WHERE source='raghav' AND domain = ANY(%s)", (doms,))}
                skipped = [r for r in rows0 if r[3] in taken]
                if skipped:
                    log.info("lisa4_reserve_domain_collision_skipped", n=len(skipped))
                    rows0 = [r for r in rows0 if r[3] not in taken]
        if rows0:
            with pool.connection() as conn, conn.cursor() as cur:
                cur.executemany(
                    "INSERT INTO lisa4_pool (dest9, dest_number, company, domain, bucket, issue, title, email, "
                    "  priority) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,100) ON CONFLICT (dest9) DO NOTHING", rows0)
                inserted += cur.rowcount
                conn.commit()
        need = target - _fetch(pool, "SELECT count(*) n FROM lisa4_pool")[0]["n"]

    # ---- 1) CRITICAL-ISSUE bucket (D&B/raghav) — GATED: only when LISA4_USE_DNB_BACKFILL is on. Scan a batch
    #         of domained AU companies, keep the ones with a real issue. Skipped (cands=[]) when gmaps-only.
    cands = _fetch(pool,
        "SELECT co.company_name, co.industry, co.domain, co.phone, "
        "  right(regexp_replace(co.phone,'[^0-9]','','g'),9) d9 "
        "FROM companies co "
        "WHERE co.source='raghav' AND NULLIF(co.domain,'') IS NOT NULL AND NULLIF(co.phone,'') IS NOT NULL "
        "  AND length(right(regexp_replace(co.phone,'[^0-9]','','g'),9))=9 "
        "  AND left(right(regexp_replace(co.phone,'[^0-9]','','g'),9),1) = '4' "  # MOBILE-FIRST: no landlines into the pool
        "  AND " + _L4X_SQL + "   "  # owner's standing exclusion (SQL pre-filter, keeps the LIMIT batch clean)
        "  AND NOT EXISTS (SELECT 1 FROM lisa4_pool p WHERE p.dest9=right(regexp_replace(co.phone,'[^0-9]','','g'),9)) "
        "  AND NOT EXISTS (SELECT 1 FROM lisa_pool lp WHERE lp.dest9=right(regexp_replace(co.phone,'[^0-9]','','g'),9)) "
        "  AND NOT EXISTS (SELECT 1 FROM lisa_calls lc WHERE lc.dest9=right(regexp_replace(co.phone,'[^0-9]','','g'),9)) "
        "  AND NOT EXISTS (SELECT 1 FROM calls hc WHERE right(regexp_replace(COALESCE(hc.dest_number,''),'[^0-9]','','g'),9)=right(regexp_replace(co.phone,'[^0-9]','','g'),9)) "
        "ORDER BY co.id DESC LIMIT %s", (*_L4X_SQL_PARAMS, scan_batch)) if use_dnb else []
    # owner's standing exclusion — shared-helper authority (franchise substrings + portal domains),
    # BEFORE the live website scans so excluded rows never cost a scan slot
    if cands:
        _portal = lisa4_portal_domains(pool, [r.get("domain") for r in cands])
        _kept = [r for r in cands if not lisa4_exclusion_class(r.get("company_name"), r.get("industry"),
                                                               r.get("domain"), _portal)]
        if len(_kept) < len(cands):
            log.info("lisa4_reserve_excluded", path="dnb_critical", n=len(cands) - len(_kept))
        cands = _kept

    def _scan(r: dict) -> tuple[dict, dict, dict]:
        intel = fetch_website_intel(r["domain"], timeout=8.0, verify=False)
        return r, intel, website_audit(intel)

    crit_rows = []
    if cands:
        with ThreadPoolExecutor(max_workers=12) as ex:
            for r, intel, aud in ex.map(_scan, cands):
                if aud.get("is_target") and aud.get("bucket") == "critical_issue":
                    title = _clean_title(intel.get("title"))
                    # harvest a contact email straight off their homepage — Lisa CONFIRMS it on the call
                    # ("I'll flick the invite to info@…, that still the best one?") instead of asking cold.
                    emails = [e for e in (intel.get("emails") or []) if "@" in e]
                    email = next((e for e in emails if e.lower().split("@")[-1].strip() ==
                                  (r["domain"] or "").lower().replace("www.", "")), emails[0] if emails else None)
                    crit_rows.append((r["d9"], r["phone"], r["company_name"], r["domain"],
                                      "critical_issue", aud.get("issue"), title or None, email))
    if crit_rows:
        with pool.connection() as conn, conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO lisa4_pool (dest9, dest_number, company, domain, bucket, issue, title, email) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (dest9) DO NOTHING", crit_rows)
            inserted += cur.rowcount
            conn.commit()

    # ---- 2) NO-WEBSITE bucket (D&B/raghav) — GATED: only when LISA4_USE_DNB_BACKFILL is on. Fill the remainder
    #         with AU companies that have a phone but no domain. Skipped entirely when gmaps-only.
    still = target - _fetch(pool, "SELECT count(*) n FROM lisa4_pool")[0]["n"]
    if use_dnb and still > 0:
        nows = _fetch(pool,
            "SELECT co.company_name, co.industry, co.phone, "
            "  right(regexp_replace(co.phone,'[^0-9]','','g'),9) d9 "
            "FROM companies co "
            "WHERE co.source='raghav' AND NULLIF(co.domain,'') IS NULL AND NULLIF(co.phone,'') IS NOT NULL "
            "  AND length(right(regexp_replace(co.phone,'[^0-9]','','g'),9))=9 "
            "  AND left(right(regexp_replace(co.phone,'[^0-9]','','g'),9),1) = '4' "  # MOBILE-FIRST: no landlines into the pool
            "  AND " + _L4X_SQL + "   "  # owner's standing exclusion (SQL pre-filter, keeps the LIMIT batch clean)
            "  AND NOT EXISTS (SELECT 1 FROM lisa4_pool p WHERE p.dest9=right(regexp_replace(co.phone,'[^0-9]','','g'),9)) "
            "  AND NOT EXISTS (SELECT 1 FROM lisa_pool lp WHERE lp.dest9=right(regexp_replace(co.phone,'[^0-9]','','g'),9)) "
            "  AND NOT EXISTS (SELECT 1 FROM lisa_calls lc WHERE lc.dest9=right(regexp_replace(co.phone,'[^0-9]','','g'),9)) "
        "  AND NOT EXISTS (SELECT 1 FROM calls hc WHERE right(regexp_replace(COALESCE(hc.dest_number,''),'[^0-9]','','g'),9)=right(regexp_replace(co.phone,'[^0-9]','','g'),9)) "
            "ORDER BY co.id DESC LIMIT %s", (*_L4X_SQL_PARAMS, still))
        # owner's standing exclusion — shared-helper authority (no domain here, so no portal check)
        _kept = [r for r in nows if not lisa4_exclusion_class(r.get("company_name"), r.get("industry"))]
        if len(_kept) < len(nows):
            log.info("lisa4_reserve_excluded", path="dnb_nowebsite", n=len(nows) - len(_kept))
        nows = _kept
        rows = [(r["d9"], r["phone"], r["company_name"], None, "no_website", "no website") for r in nows]
        if rows:
            with pool.connection() as conn, conn.cursor() as cur:
                cur.executemany(
                    "INSERT INTO lisa4_pool (dest9, dest_number, company, domain, bucket, issue) "
                    "VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT (dest9) DO NOTHING", rows)
                inserted += cur.rowcount
                conn.commit()

    pool_now = _fetch(pool, "SELECT count(*) n FROM lisa4_pool")[0]["n"]
    stats = {"reserved": inserted, "scanned": len(cands), "critical_found": len(crit_rows), "pool": pool_now}
    log.info("reserve_lisa4_pool", **stats)
    return stats


def build_brief_lisa4(pool: ConnectionPool, dest9: str) -> dict:
    """Dynamic variables for the Lisa 4 agent — the story she tells (company + which bucket + the real issue).
    Uses the real trading name (site title / domain), never a legal/partnership name, on the call."""
    r = _fetch(pool, "SELECT company, domain, bucket, issue, title, email FROM lisa4_pool WHERE dest9=%s", (dest9,))
    d = dict(r[0]) if r else {}
    # Apollo-resolved decision maker (shared lisa_dm store): first name + direct email lift RPC + trust —
    # "is Corinne about?" beats "am I talking to the owner?"
    dm = {}
    if d.get("domain"):
        dr = _fetch(pool, "SELECT dm_first, dm_name, dm_email, trading_name FROM lisa_dm WHERE domain=%s "
                    "AND source IS DISTINCT FROM 'too_big'", (d.get("domain"),))
        dm = dict(dr[0]) if dr else {}
    pn = (dm.get("dm_first") or "").strip()
    # Free RPC fallback: no Apollo DM, but a no-website sole trader's registered name IS the owner who
    # answers the listed mobile -> open with their first name ('is that Robyn?'). Only for no_website.
    if not pn and (d.get("bucket") or "no_website") == "no_website":
        pn = _owner_first_from_company(d.get("company"))
    return {
        "company_name": (_clean_title(dm.get("trading_name")) or
                         _pick_name(d.get("company"), d.get("title"), d.get("domain"))),
        # bare domain for the email-confirm move ("I'll flick it to info@<domain> — still the best one?")
        "company_domain": (d.get("domain") or "").strip().lower().removeprefix("www."),
        "website_bucket": d.get("bucket") or "no_website",
        "website_issue": _speakable_issue(d.get("issue")),   # never an internal state marker on the call
        "prospect_email": dm.get("dm_email") or d.get("email") or "",  # Lisa CONFIRMS it, never asks cold
        # an article/role leaked in as a "name" ('The') would have her open with "is The there?" — never that
        "prospect_name": "" if pn.lower() in ("the", "owner", "unknown") else pn,
    }


# --------------------------------------------------------------------------- #
# AI designer — Claude API builds the actual website AFTER a reveal is booked
# --------------------------------------------------------------------------- #
_DESIGNER_SYSTEM = (
    "You are the creative director of an elite web studio whose sites win Awwwards. You produce ONE "
    "ULTRA-PREMIUM, modern website as a SINGLE self-contained HTML file (all CSS in <style>, vanilla JS in "
    "<script>, NO external anything — visuals from layered CSS gradients, gradient-mesh backgrounds, subtle "
    "SVG noise/patterns, and hand-drawn inline SVG icons that look custom, never clip-art).\n"
    "\nDESIGN LANGUAGE (non-negotiable, 2025-premium):\n"
    "- Full-bleed cinematic HERO: layered gradient mesh + faint animated grain, oversized display headline "
    "using clamp(2.8rem,7vw,6.5rem) with tight letter-spacing and a gradient text accent on ONE word, a "
    "sharp subhead, dual CTAs (primary = filled with glow hover, secondary = ghost). NOT centered-boxy — "
    "asymmetric, editorial.\n"
    "- Sticky glass nav (backdrop-filter blur, hairline border) that compacts on scroll; mobile gets a "
    "working slide-in menu + a floating bottom action bar with tap-to-call.\n"
    "- Section rhythm ~120px; alternate light/dark sections for contrast breaks; asymmetric grids (never "
    "three identical boxes in a row everywhere); glass cards with depth (layered shadows + hairline "
    "borders + hover lift/tilt).\n"
    "- Scroll-triggered staggered reveals (IntersectionObserver, translateY+opacity, respects "
    "prefers-reduced-motion); animated stat counters (years, jobs done, rating) when scrolled into view; "
    "smooth micro-interactions on every interactive element.\n"
    "- A distinctive palette born from the trade (e.g. timber/charcoal/brass for fencing; never bootstrap "
    "blue, never default grays), defined as CSS custom properties; one accent gradient used sparingly.\n"
    "- Typography: system stack used like a pro — extreme weight contrast (300 vs 800), uppercase kickers "
    "with wide tracking above headings, fluid type scale via clamp().\n"
    "\nBANNED (instant failure): centered-everything layouts, equal three-card rows repeated, thin grey "
    "text on white, generic hero with small heading, 2010-era boxy sections, lorem ipsum, filler copy like "
    "'we offer quality services', visible section borders everywhere, default-looking buttons.\n"
    "\nCONTENT (write like you know this trade cold, Australian tone + spelling):\n"
    "hero · trust strip (rating/years/insured/licensed) · services (REAL trade-specific services, "
    "materials, job types — each with a custom SVG icon and 2-3 lines of expert copy) · signature "
    "'why us' with animated stats · process timeline (3-4 steps) · gallery placeholders built from CSS "
    "art (gradient/pattern tiles labeled with job types, marked as examples) · 2-3 realistic testimonials "
    "(marked example) · service-area · FAQ (5 REAL questions this trade gets, accordion) · conversion "
    "section: phone huge + tap-to-call + minimal quote form · footer with ABN placeholder + LocalBusiness "
    "JSON-LD schema.\n"
    "\nMULTI-PAGE (critical — the owner will CLICK EVERY NAV LINK): build a hash-router SPA inside the "
    "single file with REAL pages — Home, Services, About, Gallery, Contact — each a fully designed page "
    "(own hero band, own content), switched instantly via nav (show/hide + scroll-top + active nav state, "
    "hashchange-driven so back/forward work). On the Services page every service card CLICKS THROUGH to "
    "its own detail page (deep copy: what's included, materials, indicative process, mini-FAQ, CTA). "
    "Nothing may dead-end: every nav link, card, button and footer link must go somewhere real.\n"
    "Output ONLY raw HTML — no markdown, no code fences, no commentary."
)


def _designer_model(pool: ConnectionPool, settings: Settings) -> str:
    """Resolve the AI-designer model. A DB override (crm_config 'lisa4_designer_model') wins so we can set
    it at runtime WITHOUT an env-triggered redeploy; else the configured default; else Opus 5 (the quality
    bar for client sites — never a lighter tier)."""
    try:
        r = _fetch(pool, "SELECT v FROM crm_config WHERE k='lisa4_designer_model'")
        if r and (r[0].get("v") or "").strip():
            return r[0]["v"].strip()
    except Exception:
        pass
    return getattr(settings, "lisa4_designer_model", None) or "claude-opus-5"


def _anthropic_key(pool: ConnectionPool, settings: Settings) -> str:
    """Anthropic API key — a crm_config override wins over the ANTHROPIC_API_KEY env. This lets us point the
    cloud at a FUNDED key with a plain DB write (safe, instant) instead of an env change that would trigger a
    GitHub redeploy of stale code and kill the dialer. Falls back to the env key."""
    try:
        r = _fetch(pool, "SELECT v FROM crm_config WHERE k='anthropic_api_key'")
        if r and (r[0].get("v") or "").strip():
            return r[0]["v"].strip()
    except Exception:
        pass
    return getattr(settings, "anthropic_api_key", "") or ""


def _claude(settings: Settings, system: str, user: str, *, max_tokens: int = 60000,
            messages: list | None = None, model: str | None = None, key: str | None = None) -> str:
    """Call the Anthropic Messages API (Claude) via urllib, streaming. Returns text."""
    key = key or getattr(settings, "anthropic_api_key", "") or ""
    if not key:
        raise RuntimeError("ANTHROPIC_API_KEY not set — cannot run the Lisa-4 AI designer")
    body = {
        "model": model or getattr(settings, "lisa4_designer_model", None) or "claude-opus-5",
        "max_tokens": max_tokens,
        "system": system,
        "messages": messages or [{"role": "user", "content": user}],
    }
    body["stream"] = True   # long generations REQUIRE streaming — non-streaming times out on big sites
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=json.dumps(body).encode(), method="POST",
        headers={"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json",
                 "accept": "text/event-stream"})
    out: list[str] = []
    try:
        resp = urllib.request.urlopen(req, timeout=90)   # timeout = max idle gap between chunks
    except urllib.error.HTTPError as e:
        # Surface the REAL API error (max_tokens, model, message shape…) instead of a blind "Bad Request",
        # so a failed build records something actionable in lisa4_sites.error.
        try:
            detail = e.read().decode("utf-8", "ignore")[:400]
        except Exception:
            detail = ""
        raise RuntimeError(f"Anthropic HTTP {e.code} (model={body['model']}, max_tokens={body['max_tokens']}): {detail}") from None
    with resp:
        for raw in resp:
            line = raw.decode("utf-8", "ignore").strip()
            if line.startswith("event: error"):
                continue
            if not line.startswith("data: "):
                continue
            try:
                ev = json.loads(line[6:])
            except Exception:
                continue
            if ev.get("type") == "error":
                raise RuntimeError(f"Anthropic stream error (model={body['model']}): {str(ev.get('error'))[:300]}")
            if ev.get("type") == "content_block_delta":
                out.append((ev.get("delta") or {}).get("text", ""))
    return "".join(out)


def _strip_md_fences(s: str) -> str:
    """Some models (Opus 5 included) wrap output in ```html … ``` — strip a leading/trailing code fence
    so the site HTML is raw. Leaves unfenced output untouched."""
    t = (s or "").strip()
    if t.startswith("```"):
        nl = t.find("\n")
        t = t[nl + 1:] if nl != -1 else t[3:]
    if t.rstrip().endswith("```"):
        t = t.rstrip()[:-3]
    return t.strip()


def build_website(pool: ConnectionPool, settings: Settings, dest9: str) -> dict:
    """AI designer: generate the prospect's website with Claude, store the HTML in lisa4_sites (status
    'built'). Called when a reveal is booked. For critical-issue prospects we feed the scraped content of
    their existing site so the rebuild is faithful. Returns {status, id, bytes} or {error}."""
    ensure_lisa4_tables(pool)
    r = _fetch(pool, "SELECT company, domain, bucket, issue FROM lisa4_pool WHERE dest9=%s", (dest9,))
    if not r:
        # Not a Lisa-4 website prospect (e.g. a Lisa-5 audit booking, or stale data). Retire any queued row
        # so it can't sit at the head of the queue forever consuming a build slot every pass.
        try:
            with pool.connection() as conn, conn.cursor() as cur:
                cur.execute("UPDATE lisa4_sites SET status='error', error='prospect not in lisa4_pool' "
                            "WHERE dest9=%s AND status IN ('queued','building')", (dest9,))
                conn.commit()
        except Exception:
            pass
        return {"error": "prospect not in lisa4_pool"}
    p = dict(r[0])
    disp = _pick_name(p.get("company"), p.get("title"), p.get("domain")) or p.get("company") or ""
    context = f"Business name: {disp or p.get('company')}\nAustralian business."
    # research brief from everything we know (D&B row / Google-Maps row / phone)
    co = _fetch(pool, "SELECT company_name, industry, sub_industry, suburb, state, gmaps_rating, gmaps_reviews "
                "FROM companies WHERE right(regexp_replace(COALESCE(phone,''),'[^0-9]','','g'),9)=%s "
                "ORDER BY (source='gmaps') DESC, id DESC LIMIT 1", (dest9,))
    if co:
        c0 = dict(co[0])
        if c0.get("industry"):
            context += f"\nIndustry / category: {c0['industry']}" + (f" / {c0['sub_industry']}" if c0.get("sub_industry") else "")
        if c0.get("suburb") or c0.get("state"):
            context += f"\nLocation: {c0.get('suburb') or ''} {c0.get('state') or ''} — write the service-area section around this."
        if c0.get("gmaps_rating"):
            context += f"\nGoogle rating: {c0['gmaps_rating']}★ ({c0.get('gmaps_reviews') or 0} reviews) — feature this in the trust strip."
    ph = _fetch(pool, "SELECT dest_number FROM lisa4_pool WHERE dest9=%s", (dest9,))
    if ph:
        context += f"\nBusiness phone (use everywhere, tap-to-call): {ph[0]['dest_number']}"
    if p.get("domain"):
        context += f"\nCurrent domain: {p['domain']} (issue we're fixing: {p.get('issue')})."
        try:
            intel = fetch_website_intel(p["domain"], timeout=10.0, verify=False)
            if intel.get("found"):
                emails = ", ".join(intel.get("emails") or [])
                socials = ", ".join((intel.get("socials") or {}).values())
                if emails:
                    context += f"\nContact emails on their site: {emails}"
                if socials:
                    context += f"\nSocial links: {socials}"
        except Exception:
            pass
    user = (context + "\n\nBuild this business a brand-new website. Infer their industry + services from the "
            "name. Make it genuinely impressive so they want to publish it.")
    dmodel = _designer_model(pool, settings)
    akey = _anthropic_key(pool, settings)
    # mark building — REUSE the prospect's existing active row. The partial unique index
    # idx_lisa4_sites_active allows only ONE active (queued/building/built) row per dest9, so a blind INSERT
    # collides with the 'queued' row the booking flow already created — and that INSERT sits OUTSIDE the try
    # below, so the violation aborts the WHOLE build pass every cycle (head-of-line deadlock). Transition the
    # existing queued/building row in place; only if none exists (rebuild over an old 'built') insert fresh.
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT id FROM lisa4_sites WHERE dest9=%s AND status IN ('queued','building') "
                    "ORDER BY id DESC LIMIT 1", (dest9,))
        ex = cur.fetchone()
        if ex:
            site_id = ex["id"]
            cur.execute("UPDATE lisa4_sites SET status='building', model=%s, error=NULL, building_at=now(), "
                        "build_attempts=COALESCE(build_attempts,0)+1 WHERE id=%s", (dmodel, site_id))
        else:
            cur.execute("UPDATE lisa4_sites SET status='superseded' WHERE dest9=%s AND status='built'", (dest9,))
            cur.execute("INSERT INTO lisa4_sites (dest9, domain, company, bucket, issue, status, model, build_attempts, building_at) "
                        "VALUES (%s,%s,%s,%s,%s,'building',%s,1,now()) RETURNING id",
                        (dest9, p.get("domain"), p.get("company"), p.get("bucket"), p.get("issue"), dmodel))
            site_id = cur.fetchone()["id"]
        conn.commit()
    try:
        html = _strip_md_fences(_claude(settings, _DESIGNER_SYSTEM, user, model=dmodel, key=akey))
        # CONTINUATION: a full multi-page site can exceed one completion — resume until the file is
        # genuinely complete (</html> present AND the <script> exists so nav/pages actually work).
        tries = 0
        while tries < 3 and not (html.rstrip().endswith("</html>") and "<script" in html):
            cont = _claude(settings, _DESIGNER_SYSTEM, user, messages=[
                {"role": "user", "content": user},
                {"role": "assistant", "content": html},
                {"role": "user", "content": "You stopped mid-file. Continue EXACTLY from where you left "
                 "off — output ONLY the remaining raw HTML/CSS/JS (no repetition of anything already "
                 "written, no commentary), finishing the COMPLETE document: all remaining pages, the "
                 "full <script> with the working hash-router/nav/reveals, through to </html>."}], model=dmodel, key=akey)
            if not cont.strip():
                break
            html += _strip_md_fences(cont)
            tries += 1
        if not (html.rstrip().endswith("</html>") and "<script" in html and html.count("<section") >= 3):
            raise RuntimeError(f"generated site incomplete after {tries} continuations ({len(html)}b)")
        if "<html" not in html.lower() and "<!doctype" not in html.lower():
            html = "<!doctype html><html><body>" + html + "</body></html>"
        # SAVE with retry on a fresh connection — generation runs for many minutes and the pooled
        # connection can go stale meanwhile; losing a finished site to a dead socket is unacceptable.
        # Assign a public share_token on build (idempotent) so the finished site has a shareable link the
        # closer / autopilot can actually send — previously reveal builds were left tokenless.
        import time as _time, secrets as _secrets, re as _re2
        _slug = _re2.sub(r"[^a-z0-9]+", "-", (p.get("company") or "site").lower()).strip("-")[:24] or "site"
        _tok = f"{_slug}-reveal-" + _secrets.token_urlsafe(8)
        for _attempt in range(3):
            try:
                with pool.connection() as conn, conn.cursor() as cur:
                    cur.execute("UPDATE lisa4_sites SET html=%s, status='built', built_at=now(), "
                                "share_token=COALESCE(share_token,%s), kind=COALESCE(kind,'reveal') WHERE id=%s",
                                (html, _tok, site_id))
                    conn.commit()
                break
            except Exception as _exc:
                log.warning("lisa4_site_save_retry", attempt=_attempt, error=str(_exc)[:100])
                _time.sleep(3)
        else:
            raise RuntimeError("site save failed after retries")
        log.info("lisa4_site_built", dest9=dest9, company=p.get("company"), bytes=len(html))
        return {"status": "built", "id": site_id, "bytes": len(html)}
    except Exception as exc:
        # Retry a transient failure (an Opus stream timeout is common) up to 3 attempts before retiring the
        # row to 'error'. build_attempts was already incremented at mark-building, so this leaves the row in
        # 'queued' (retry) until the 3rd attempt, then 'error' — either way it never stays stuck blocking the
        # queue. NOTE: whatever the outcome, the row leaves the pre-generation state, so no head-of-line stall.
        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute("UPDATE lisa4_sites SET status=CASE WHEN COALESCE(build_attempts,0) >= 3 THEN 'error' "
                        "ELSE 'queued' END, error=%s WHERE id=%s", (str(exc)[:300], site_id))
            conn.commit()
        log.warning("lisa4_site_build_failed", dest9=dest9, error=str(exc)[:160])
        return {"error": str(exc)[:200], "id": site_id}


# --------------------------------------------------------------------------- #
# Autopilot — control toggle, dial path, scheduler, dial loop (all ISOLATED from Lisa-1)
# --------------------------------------------------------------------------- #
from datetime import datetime, timedelta          # noqa: E402
from zoneinfo import ZoneInfo                      # noqa: E402

_BDE = "Lisa4"   # Lisa 4's calendar owner tag — never collides with Lisa-1's 'Lisa'


def ensure_lisa4_control(pool: ConnectionPool) -> None:
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute("CREATE TABLE IF NOT EXISTS lisa4_control ("
                    "  id integer PRIMARY KEY, autodial boolean, updated_at timestamptz DEFAULT now(),"
                    "  updated_by text, heavy_at timestamptz)")
        # Lisa 4 = a real BDE on the main funnel (calls.bde_extension FK + leaderboard identity).
        # Re-asserted each cycle so a 3CX roster-sync can't drop her (she isn't a 3CX agent).
        cur.execute(
            "INSERT INTO bde_agents (extension, bde_name, email, group_name, role_name, in_scope, active, "
            "  synced_at) VALUES ('LISA4','Lisa 4','lisa4@trafficradius.com.au','AI','AI BDE',true,true,now()) "
            "ON CONFLICT (extension) DO UPDATE SET bde_name='Lisa 4', in_scope=true, active=true, "
            "  role_name='AI BDE'")
        conn.commit()


def get_lisa4_autodial(pool: ConnectionPool, settings: Settings) -> bool:
    """DB toggle wins once set; else the env default (lisa4_autodial_enabled, default OFF)."""
    ensure_lisa4_control(pool)
    r = _fetch(pool, "SELECT autodial FROM lisa4_control WHERE id=1")
    if r and r[0].get("autodial") is not None:
        return bool(r[0]["autodial"])
    return bool(getattr(settings, "lisa4_autodial_enabled", False))


def set_lisa4_autodial(pool: ConnectionPool, on: bool, by: str = "console") -> None:
    ensure_lisa4_control(pool)
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute("INSERT INTO lisa4_control (id, autodial, updated_at, updated_by) VALUES (1,%s,now(),%s) "
                    "ON CONFLICT (id) DO UPDATE SET autodial=EXCLUDED.autodial, updated_at=now(), "
                    "updated_by=EXCLUDED.updated_by", (on, by))
        conn.commit()


def _rotate_from_number(candidates: list[str], today_counts: dict[str, int], cap: int) -> str | None:
    """Pick the next caller ID from the rotation pool: least-used today first (random tie-break), and
    NEVER a number already at its per-day cap. Pure function so the rotation is dry-run testable.
    Returns None when every number is maxed (the day is over for outbound on this pool)."""
    import random
    eligible = [n for n in candidates if today_counts.get(n, 0) < max(1, cap)]
    if not eligible:
        return None
    low = min(today_counts.get(n, 0) for n in eligible)
    return random.choice([n for n in eligible if today_counts.get(n, 0) == low])


def _pick_lisa4_from(pool: ConnectionPool, settings: Settings) -> tuple[str | None, str]:
    """Resolve today's outbound caller ID: normalise LISA4_FROM_NUMBERS, count today's lisa_calls per
    from_number (settings.tz day) and rotate via _rotate_from_number honoring the ~150/day cap.
    Returns (number, "") on success or (None, reason)."""
    from . import lisa as _L1
    candidates: list[str] = []
    for n in list(getattr(settings, "lisa4_numbers", []) or []):
        e = _L1._e164_au(n)
        if e and e not in _L1._BLOCKED_FROM and e not in candidates:
            candidates.append(e)
    if not candidates:
        return None, "no valid LISA4_FROM_NUMBERS"
    tz = settings.tz
    rows = _fetch(pool,
                  "SELECT from_number f, count(*) n FROM lisa_calls WHERE from_number = ANY(%s) "
                  "AND (created_at AT TIME ZONE %s)::date=(now() AT TIME ZONE %s)::date GROUP BY from_number",
                  (candidates, tz, tz))
    counts = {r["f"]: int(r["n"]) for r in rows}
    frm = _rotate_from_number(candidates, counts, int(getattr(settings, "lisa4_per_number_daily_cap", 150)))
    return (frm, "") if frm else (None, "all LISA4_FROM_NUMBERS at daily cap")


def start_lisa4_call(pool: ConnectionPool, settings: Settings, *, to_number: str, dest9: str,
                     extra_vars: dict | None = None, allow_landline: bool = False) -> dict:
    """Place ONE Lisa-4 call: build the website brief, create the Retell call on the Lisa-4 agent from
    Lisa-4's own number, log a pending lisa_calls row (from_number distinguishes it from Lisa-1).
    The caller ID ROTATES across the LISA4_FROM_NUMBERS pool with a per-number daily cap."""
    from . import lisa as _L1                      # reuse the shared helpers (never Lisa-1's dial state)
    if not getattr(settings, "lisa_enabled", False):
        return {"error": "lisa disabled"}
    frm, why = _pick_lisa4_from(pool, settings)
    if not frm:
        return {"error": why}
    to_number = _L1._e164_au(to_number)
    if len(_L1.re.sub(r"[^0-9]", "", to_number)) < 8:
        return {"error": f"invalid phone: {to_number}"}
    # MOBILE-FIRST HARD GATE (Raj, 2026-08-10): Lisa-4 dials AU mobiles ONLY. This is the final belt —
    # whatever path reaches here (pool / calendar event / DM override), a non-mobile is refused so a
    # landline can never be dialed. Permanent-style error → the caller cancels the offending event.
    if not allow_landline and _L1.re.sub(r"[^0-9]", "", to_number)[-9:][:1] != "4":
        return {"error": "skipped non-mobile (mobile-first)"}
    d9 = _L1._d9(dest9 or to_number)
    brief = build_brief_lisa4(pool, d9)
    dyn = {k: ("" if v is None else str(v)) for k, v in brief.items()}
    # she can't hear what she dialed — tell her, so she never asks "is this the best number?"
    # on the very number that just answered (a dead giveaway on a mobile)
    dyn["phone_is_mobile"] = "yes" if to_number.startswith("+614") else "no"
    for k, v in (extra_vars or {}).items():
        dyn[k] = "" if v is None else str(v)
    # final belt: an extra_vars/event override can re-introduce an internal state marker — never speakable
    dyn["website_issue"] = _speakable_issue(dyn.get("website_issue"))
    body = {
        "from_number": frm, "to_number": to_number,
        "override_agent_id": getattr(settings, "lisa4_agent_id", "") or None,
        "retell_llm_dynamic_variables": dyn,
        "metadata": {"dest9": d9, "lisa4": "true", "bucket": brief.get("website_bucket", "")},
    }
    try:
        r = _L1._retell(settings, "POST", "v2/create-phone-call", body)
    except Exception as exc:
        log.warning("lisa4_start_call_failed", to=to_number, error=str(exc)[:200])
        return {"error": str(exc)[:200]}
    cid = r.get("call_id")
    if cid:
        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO lisa_calls (call_id, dest9, to_number, from_number, company_name, domain, "
                "  status, brief) VALUES (%s,%s,%s,%s,%s,%s,'ongoing',%s) ON CONFLICT (call_id) DO NOTHING",
                (cid, d9, to_number, frm, brief.get("company_name"), None, _L1.json.dumps(brief)))
            conn.commit()
    return r


def schedule_lisa4_fresh(pool: ConnectionPool, settings: Settings) -> dict:
    """Put reserved Lisa-4 prospects on the Lisa4 calendar (bde_name='Lisa4') as fresh_call events, filling
    up to lisa4_daily_target/day, staggered across the window. Idempotent."""
    ensure_lisa4_tables(pool)
    tz = settings.tz
    cap = int(getattr(settings, "lisa4_daily_target", 200))
    wstart = int(getattr(settings, "lisa_call_window_start", 9))
    wsmin = int(getattr(settings, "lisa_call_window_start_min", 0))
    wend = int(getattr(settings, "lisa_call_window_end", 17))
    rows = _fetch(pool,
        "SELECT lp.dest9, lp.dest_number, lp.company, lp.domain FROM lisa4_pool lp "
        "WHERE NOT EXISTS (SELECT 1 FROM calendar_events e WHERE e.bde_name=%s AND e.status IN ('pending','done') "
        "   AND right(regexp_replace(COALESCE(e.dest_number,''),'[^0-9]','','g'),9)=lp.dest9) "
        "  AND NOT EXISTS (SELECT 1 FROM lisa_calls lc WHERE lc.dest9=lp.dest9) "
        # MOBILE-FIRST (Raj, 2026-08-10): only ever schedule AU mobiles (last-9 starts with 4). Dialing a
        # business landline reaches a receptionist/voicemail, never the owner who can book — 43/48 of one
        # day's gatekeepers were landlines. Landline prospects are parked for owner-mobile enrichment.
        "  AND left(lp.dest9,1) = '4' "
        # critical-issue FIRST — they have a domain (real trading name + a concrete, verifiable hook), which
        # is a far more human opener than a no-website partnership's legal name.
        "ORDER BY COALESCE(lp.priority,0) DESC, (CASE WHEN lp.bucket='critical_issue' THEN 0 "
        "  WHEN lp.bucket='upgrade' THEN 1 ELSE 2 END), lp.reserved_at", (_BDE,))
    # owner's standing exclusion — belt for LEGACY pool rows that predate the feeder filters: an
    # excluded row just never gets a fresh_call event (no deletions; the audit report surfaces them).
    # Industry comes from a single cheap phone_norm lookup (feeders already did the full check).
    if rows:
        _ind = {x["d9"]: x["industry"] for x in _fetch(pool,
            "SELECT right(COALESCE(phone_norm,''),9) d9, max(industry) industry FROM companies "
            "WHERE NULLIF(industry,'') IS NOT NULL AND right(COALESCE(phone_norm,''),9) = ANY(%s) "
            "GROUP BY 1", ([r["dest9"] for r in rows],))}
        _portal = lisa4_portal_domains(pool, [r.get("domain") for r in rows])
        _kept = [r for r in rows if not lisa4_exclusion_class(r.get("company"), _ind.get(r["dest9"]),
                                                              r.get("domain"), _portal)]
        if len(_kept) < len(rows):
            log.info("lisa4_schedule_excluded", n=len(rows) - len(_kept))
        rows = _kept
    existing = {r["d"]: r["n"] for r in _fetch(pool,
        "SELECT (start_at AT TIME ZONE %s)::date d, count(*) n FROM calendar_events "
        "WHERE bde_name=%s AND status='pending' AND type='fresh_call' GROUP BY 1", (tz, _BDE))}
    now = datetime.now(ZoneInfo(tz))
    day = now.date() + (timedelta(days=1) if now.hour >= wend else timedelta(0))
    open_min = wstart * 60 + wsmin
    span_min = max(1, wend * 60 - open_min)
    to_insert = []
    for r in rows:
        while day.weekday() >= 5 or existing.get(day, 0) >= cap:
            day += timedelta(days=1)
        used = existing.get(day, 0)
        t = min(open_min + int(used * span_min / max(1, cap)), wend * 60 - 1)
        when = datetime(day.year, day.month, day.day, t // 60, t % 60, tzinfo=ZoneInfo(tz))
        existing[day] = used + 1
        to_insert.append((_BDE, "fresh_call", f"🌐 Lisa 4: {r['company'] or r['dest_number']}",
                          when, when + timedelta(minutes=15), r["dest_number"]))
    scheduled = 0
    if to_insert:
        # ON CONFLICT on the GLOBAL partial-unique index (one pending fresh_call per dest9 across ALL
        # bde_names — Lisa-1 included). Without it, ONE d9 collision aborted the whole executemany batch
        # every cycle ('duplicate key … idx_calendar_fresh_call') and Lisa 4 never got a calendar.
        _d9expr = "right(regexp_replace(COALESCE(dest_number,''),'[^0-9]','','g'),9)"
        with pool.connection() as conn, conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO calendar_events (bde_name, type, title, start_at, end_at, dest_number, "
                "  created_by, status) VALUES (%s,%s,%s,%s,%s,%s,'lisa4','pending') "
                "ON CONFLICT (" + _d9expr + ") WHERE type='fresh_call' AND status='pending' DO NOTHING",
                to_insert)
            scheduled = cur.rowcount
            conn.commit()
    return {"scheduled": scheduled, "candidates": len(rows)}


def run_lisa4_head(pool: ConnectionPool, settings: Settings) -> dict:
    """Reserve + schedule for Lisa 4 (throttled ~every 3 min). Isolated from Lisa-1's head_of_sales."""
    ensure_lisa4_control(pool)
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT (heavy_at IS NULL OR heavy_at < now() - make_interval(secs => 180)) go "
                    "FROM lisa4_control WHERE id=1")
        row = cur.fetchone()
        do = bool(row["go"]) if row else True
        if do and row is not None:
            cur.execute("UPDATE lisa4_control SET heavy_at=now() WHERE id=1"); conn.commit()
    if not do:
        return {"skipped": "throttled"}
    out = {}
    try:
        out["reserved"] = reserve_lisa4_pool(pool, settings, scan_batch=60).get("reserved", 0)
    except Exception as exc:
        log.warning("lisa4_reserve_failed", error=str(exc)[:140])
    try:
        out["scheduled"] = schedule_lisa4_fresh(pool, settings).get("scheduled", 0)
    except Exception as exc:
        log.warning("lisa4_schedule_failed", error=str(exc)[:140])
    try:
        out["built"] = process_lisa4_builds(pool, settings).get("built", 0)   # build any booked reveals
    except Exception as exc:
        log.warning("lisa4_builds_failed", error=str(exc)[:140])
    try:
        out["dms"] = resolve_lisa4_dms(pool, settings).get("resolved", 0)     # owner name/mobile/email → RPC
    except Exception as exc:
        log.warning("lisa4_dms_failed", error=str(exc)[:140])
    return out


def resolve_lisa4_dms(pool: ConnectionPool, settings: Settings, limit: int = 8) -> dict:
    """Apollo-resolve the decision maker (name + mobile + email) for Lisa-4's critical-issue prospects, in
    DIAL ORDER so each is enriched before the dialer reaches it. Shares Lisa-1's lisa_dm store + the async
    phone-reveal webhook — one machinery, two agents. RPC-connect lever: dial the OWNER's mobile, not the
    switchboard."""
    from . import lisa as _L1
    if not bool(getattr(settings, "apollo_paid_reveal", False)):
        return {"resolved": 0, "note": "paid reveal off"}
    rows = _fetch(pool,
        "SELECT DISTINCT ON (lp.domain) lp.domain, ce.start_at FROM lisa4_pool lp "
        "JOIN calendar_events ce ON ce.bde_name='Lisa4' AND ce.status='pending' "
        "  AND right(regexp_replace(COALESCE(ce.dest_number,''),'[^0-9]','','g'),9)=lp.dest9 "
        "LEFT JOIN lisa_dm d ON d.domain=lp.domain "
        "WHERE lp.bucket='critical_issue' AND NULLIF(lp.domain,'') IS NOT NULL AND d.domain IS NULL "
        "ORDER BY lp.domain, ce.start_at LIMIT %s", (limit,))
    rows = sorted(rows, key=lambda r: r["start_at"])
    n = 0
    for r in rows:
        try:
            _L1.apollo_resolve_dm(pool, settings, r["domain"])
            n += 1
        except Exception as exc:
            log.warning("lisa4_dm_resolve_failed", domain=r["domain"], error=str(exc)[:120])
    return {"resolved": n}


def run_lisa4_autodial(pool: ConnectionPool, settings: Settings) -> dict:
    """GATED Lisa-4 dialer — one call at a time, pipeline-paced, within the window. Mirrors Lisa-1's
    autodial but on the Lisa4 calendar + agent + number. No call fires unless the Lisa4 toggle is on."""
    from . import lisa as _L1
    ensure_lisa4_tables(pool)
    # SCHEDULED SMS (type='sms' on the Lisa4 calendar): send `notes` to dest_number once start_at is due.
    # Runs ahead of the dialer gates so a queued text fires even if the dial toggle is off. Transient
    # failures retry each tick for 30 minutes past start_at, then the event is cancelled. Each event is
    # handled in its OWN try/except — one provider error (e.g. a Twilio 401) must never block the rest
    # of the queue behind it (learned 2026-08-07).
    for s in _fetch(pool, "SELECT id, dest_number, notes, start_at, created_at FROM calendar_events "
                    "WHERE bde_name=%s AND status='pending' AND type='sms' AND start_at <= now() "
                    "LIMIT 3", (_BDE,)):
        try:
            # SELF-STOP: if the prospect texted or called us since this event was queued, they've
            # responded — cancel the automated touch instead of talking over a live conversation.
            d9g = _re.sub(r"[^0-9]", "", s.get("dest_number") or "")[-9:]
            replied = _fetch(pool,
                "SELECT 1 FROM lisa_sms WHERE dest9=%s AND direction='inbound' AND created_at > %s "
                "UNION ALL "
                "SELECT 1 FROM lisa_calls WHERE dest9=%s AND from_number LIKE '%%'||%s AND created_at > %s "
                "LIMIT 1", (d9g, s["created_at"], d9g, d9g, s["created_at"]))
            if replied:
                with pool.connection() as conn, conn.cursor() as cur:
                    cur.execute("UPDATE calendar_events SET status='cancelled', "
                                "notes=COALESCE(notes,'')||' · auto-cancelled: prospect responded' WHERE id=%s", (s["id"],))
                    conn.commit()
                log.info("lisa4_scheduled_sms_skipped_responded", event_id=s["id"])
                continue
            frm = (list(getattr(settings, "lisa4_numbers", []) or []) or [""])[0]
            ok = bool(s.get("dest_number") and (s.get("notes") or "").strip() and frm
                      and _L1._twilio_ready(settings)
                      and _L1._send_sms_twilio(settings, s["dest_number"], s["notes"], frm))
        except Exception as exc:
            log.warning("lisa4_scheduled_sms_error", event_id=s.get("id"), error=str(exc)[:140])
            ok = False
        try:
            if ok:
                with pool.connection() as conn, conn.cursor() as cur:
                    cur.execute("UPDATE calendar_events SET status='done' WHERE id=%s", (s["id"],))
                    conn.commit()
                _L1._log_sms(pool, "outbound", frm, s["dest_number"], s["notes"],
                             _re.sub(r"[^0-9]", "", s["dest_number"] or "")[-9:])
                log.info("lisa4_scheduled_sms_sent", to=s["dest_number"], event_id=s["id"])
            else:
                expired = _fetch(pool, "SELECT (now() - start_at) > interval '30 minutes' e "
                                 "FROM calendar_events WHERE id=%s", (s["id"],))
                if expired and expired[0].get("e"):
                    with pool.connection() as conn, conn.cursor() as cur:
                        cur.execute("UPDATE calendar_events SET status='cancelled', "
                                    "notes=COALESCE(notes,'')||' · sms failed >30min' WHERE id=%s", (s["id"],))
                        conn.commit()
                log.warning("lisa4_scheduled_sms_not_sent", event_id=s["id"])
        except Exception as exc:
            log.warning("lisa4_scheduled_sms_error", event_id=s.get("id"), error=str(exc)[:140])
    if not get_lisa4_autodial(pool, settings):
        return {"skipped": "lisa4 autodial off"}
    if not getattr(settings, "lisa_enabled", False):
        return {"skipped": "lisa disabled"}
    tz = settings.tz
    now = datetime.now(ZoneInfo(tz))
    wstart = int(getattr(settings, "lisa_call_window_start", 9))
    wsmin = int(getattr(settings, "lisa_call_window_start_min", 0))
    wend = int(getattr(settings, "lisa_call_window_end", 17))
    open_min = wstart * 60 + wsmin
    if now.weekday() >= 5 or not (open_min <= now.hour * 60 + now.minute < wend * 60):
        return {"skipped": "outside window"}
    placed = _fetch(pool, "SELECT count(*) n FROM lisa_calls WHERE from_number = ANY(%s) "
                    "AND (created_at AT TIME ZONE %s)::date=(now() AT TIME ZONE %s)::date",
                    (list(getattr(settings, "lisa4_numbers", []) or []), tz, tz))[0]["n"]
    if placed >= int(getattr(settings, "lisa4_daily_target", 200)):
        return {"skipped": "daily target reached", "placed": placed}
    # pipeline pacing: only one Lisa4 call in flight at a time
    inflight = _fetch(pool, "SELECT count(*) n FROM lisa_calls WHERE status='ongoing' "
                      "AND from_number = ANY(%s) AND created_at > now() - interval '8 minutes'",
                      (list(getattr(settings, "lisa4_numbers", []) or []),))[0]["n"]
    if inflight:
        return {"skipped": "call in-flight"}
    floor = int(getattr(settings, "lisa_min_call_gap_seconds", 0)) or 20
    since = _fetch(pool, "SELECT extract(epoch from (now() - max(created_at))) s FROM lisa_calls "
                   "WHERE from_number = ANY(%s) AND (created_at AT TIME ZONE %s)::date=(now() AT TIME ZONE %s)::date",
                   (list(getattr(settings, "lisa4_numbers", []) or []), tz, tz))[0]["s"]
    if since is not None and since < floor:
        return {"skipped": "floor gap"}
    due = _fetch(pool,
        "SELECT id, dest_number, type, notes, right(regexp_replace(COALESCE(dest_number,''),'[^0-9]','','g'),9) d9 "
        "FROM calendar_events WHERE bde_name=%s AND status='pending' "
        "  AND type IN ('fresh_call','callback','retry') AND start_at <= now() "
        # SELF-DIAL GUARD (Raj, 2026-08-10): never dial a number Lisa calls FROM (bot-to-bot busy-divert bridge)
        "  AND right(regexp_replace(COALESCE(dest_number,''),'[^0-9]','','g'),9) NOT IN "
        "    (SELECT DISTINCT right(regexp_replace(from_number,'[^0-9]','','g'),9) FROM lisa_calls "
        "     WHERE COALESCE(from_number,'')<>'' AND created_at > now() - interval '45 days') "
        # MOBILE-FIRST — drop legacy landline FRESH/retry events; but AGREED CALLBACKS are a promise to a
        # prospect who already engaged, so honour them even on a landline (Raj, 2026-08-11).
        "  AND (type='callback' OR left(right(regexp_replace(COALESCE(dest_number,''),'[^0-9]','','g'),9),1) = '4') "
        "  ORDER BY CASE WHEN type='callback' THEN 0 WHEN type='retry' THEN 1 ELSE 2 END, start_at "
        "  LIMIT 6", (_BDE,))
    if not due:
        # SELF-FEEDING QUEUE: nothing due but we're inside the window and under target — promote the next
        # scheduled event instead of idling (the day-spread stagger otherwise starves the dialer for an hour).
        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE calendar_events SET start_at=now() WHERE id = ("
                "  SELECT id FROM calendar_events WHERE bde_name=%s AND status='pending' "
                "    AND type='fresh_call' AND start_at > now() ORDER BY start_at LIMIT 1)", (_BDE,))
            conn.commit()
        due = _fetch(pool,
            "SELECT id, dest_number, type, notes, right(regexp_replace(COALESCE(dest_number,''),'[^0-9]','','g'),9) d9 "
            "FROM calendar_events WHERE bde_name=%s AND status='pending' "
            "  AND type IN ('fresh_call','callback','retry') AND start_at <= now() "
            "  AND right(regexp_replace(COALESCE(dest_number,''),'[^0-9]','','g'),9) NOT IN "  # self-dial guard
            "    (SELECT DISTINCT right(regexp_replace(from_number,'[^0-9]','','g'),9) FROM lisa_calls "
            "     WHERE COALESCE(from_number,'')<>'' AND created_at > now() - interval '45 days') "
            "  AND (type='callback' OR left(right(regexp_replace(COALESCE(dest_number,''),'[^0-9]','','g'),9),1) = '4') "  # MOBILE-FIRST (agreed callbacks exempt)
            "  LIMIT 1", (_BDE,))
    for e in due:
        if not e.get("dest_number"):
            continue
        # WON-DEAL GUARD (Raj, 2026-08-19): never re-dial a prospect who already booked a meeting — retire
        # any stale fresh_call/callback/retry event and skip. Defensive: on any error we fall through and
        # dial as normal, so this can only ever SUPPRESS a re-pitch, never block a legitimate call.
        try:
            if _L1._has_booked_meeting(pool, e["d9"]):
                with pool.connection() as conn, conn.cursor() as cur:
                    cur.execute("UPDATE calendar_events SET status='done' WHERE id=%s", (e["id"],))
                    conn.commit()
                continue
        except Exception:
            pass
        lh, _tz = _L1._prospect_local_hour(pool, e["d9"])
        if not (wstart <= lh < wend):
            continue
        # RPC lever: if Apollo resolved the owner's MOBILE for this prospect, dial that instead of the
        # switchboard (pool identity stays on the event's dest9).
        to = e["dest_number"]
        # For an AGREED callback, dial the number they agreed on — don't override. Only upgrade FRESH
        # calls to a resolved owner mobile.
        if e.get("type") != "callback":
            dm = _fetch(pool, "SELECT d.dm_phone FROM lisa4_pool lp JOIN lisa_dm d ON d.domain=lp.domain "
                        "WHERE lp.dest9=%s AND COALESCE(d.dm_phone,'')<>'' AND d.dm_is_mobile "
                        "AND d.source IS DISTINCT FROM 'too_big'", (e["d9"],))
            if dm:
                to = dm[0]["dm_phone"]
        extra = {}
        if e.get("type") in ("callback", "retry") and e.get("notes"):
            extra["callback_context"] = str(e["notes"])[:180]
        r = start_lisa4_call(pool, settings, to_number=to, dest9=e["d9"], extra_vars=extra,
                             allow_landline=(e.get("type") == "callback"))
        if r.get("call_id"):
            with pool.connection() as conn, conn.cursor() as cur:
                cur.execute("UPDATE calendar_events SET status='done' WHERE id=%s", (e["id"],))
                conn.commit()
            return {"dialed": 1, "placed": placed + 1}
        # PERMANENT failure → cancel so it never wedges the due queue (transient API blips stay pending)
        err = (r.get("error") or "")
        transient = any(t in err.lower() for t in ("http 5", "timed out", "timeout", "concurrency", "temporarily"))
        if err and not transient:
            with pool.connection() as conn, conn.cursor() as cur:
                cur.execute("UPDATE calendar_events SET status='cancelled', "
                            "notes=COALESCE(notes,'')||' · auto-cancelled: '||%s WHERE id=%s", (err[:120], e["id"]))
                conn.commit()
            log.info("lisa4_autodial_event_cancelled", event_id=e["id"], reason=err[:80])
    return {"dialed": 0, "candidates": len(due)}


# Confirm-gate cutover (Vysakh, 2026-08-19): everything queued BEFORE this builds automatically
# (grandfathered — today's bookings all build so Alfred can be trained on the trigger first); anything queued
# ON/AFTER it builds ONLY when the booking's CRM stage is 'confirmed'. Stored in crm_config so the date can be
# nudged without a redeploy if the training slips.
_CONFIRM_GATE_DEFAULT = "2026-08-20 00:00:00+10:00"   # start of Wed 20 Aug, AEST


def _confirm_gate_from(pool: ConnectionPool) -> str:
    try:
        r = _fetch(pool, "SELECT v FROM crm_config WHERE k='lisa4_confirm_gate_from'")
        if r and r[0].get("v"):
            return r[0]["v"]
    except Exception:
        pass
    return _CONFIRM_GATE_DEFAULT


def enqueue_lisa4_build(pool: ConnectionPool, dest9: str) -> bool:
    """Ensure a queued build row exists for a booked prospect (idempotent — skips if one is already
    queued/building/built). Called by the CRM 'confirmed' trigger. Only enqueues Lisa-4 WEBSITE prospects
    (those present in lisa4_pool); audit prospects are handled by the audit path. Returns True if newly queued."""
    import re as _re
    d9 = _re.sub(r"[^0-9]", "", dest9 or "")[-9:]
    if not d9:
        return False
    try:
        ensure_lisa4_tables(pool)
        row = _fetch(pool, "SELECT company, domain, bucket, issue FROM lisa4_pool WHERE dest9=%s", (d9,))
        if not row:
            return False   # not a Lisa-4 website prospect — nothing to build here
        if _fetch(pool, "SELECT 1 FROM lisa4_sites WHERE dest9=%s AND status IN ('queued','building','built') LIMIT 1", (d9,)):
            return False
        p = dict(row[0])
        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute("INSERT INTO lisa4_sites (dest9, domain, company, bucket, issue, status) "
                        "VALUES (%s,%s,%s,%s,%s,'queued')",
                        (d9, p.get("domain"), p.get("company"), p.get("bucket"), p.get("issue")))
            conn.commit()
        log.info("lisa4_build_enqueued_by_confirm", dest9=d9, company=p.get("company"))
        return True
    except Exception as exc:
        log.warning("lisa4_enqueue_failed", error=str(exc)[:140])
        return False


def process_lisa4_builds(pool: ConnectionPool, settings: Settings, limit: int = 2) -> dict:
    """Build any queued Lisa-4 sites (booked reveals) with the AI designer. A few per pass so a slow Claude
    call never stalls the loop. Each build is ISOLATED (a poison row can't abort the pass), rows that have
    failed 3+ times are skipped, and the confirm-gate decides eligibility: queued-before-cutover builds
    freely; queued-after only when the booking's CRM stage is 'confirmed'."""
    ensure_lisa4_tables(pool)
    # self-heal: a container restart (e.g. a deploy) can orphan a row mid-build in 'building'. The drainer
    # below only picks 'queued', so re-queue any build that has been 'building' with no HTML for >20 min so it
    # rebuilds instead of being stranded. (A live build finishes in ~10-12 min, so 20 min is safe headroom.)
    try:
        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute("UPDATE lisa4_sites SET status='queued' WHERE status='building' AND html IS NULL "
                        "AND COALESCE(building_at, created_at) < now() - interval '20 minutes'")
            conn.commit()
    except Exception:
        pass
    gate = _confirm_gate_from(pool)
    rows = _fetch(pool,
        "SELECT s.dest9 FROM lisa4_sites s "
        "WHERE s.status='queued' AND COALESCE(s.build_attempts,0) < 3 "
        "  AND ( s.created_at < %s::timestamptz "
        "        OR EXISTS (SELECT 1 FROM booked_crm b WHERE b.dest9=s.dest9 "
        "                   AND lower(COALESCE(b.stage,''))='confirmed') ) "
        "ORDER BY s.created_at LIMIT %s", (gate, limit))
    built = 0
    for r in rows:
        try:
            res = build_website(pool, settings, r["dest9"])
        except Exception as exc:
            # Belt-and-braces: even a pre-generation failure (DB hiccup) must advance the row so it can never
            # block the queue — increment attempts and retire to 'error' after the 3rd.
            log.warning("lisa4_build_exception", dest9=r.get("dest9"), error=str(exc)[:160])
            try:
                with pool.connection() as conn, conn.cursor() as cur:
                    cur.execute("UPDATE lisa4_sites SET build_attempts=COALESCE(build_attempts,0)+1, "
                                "status=CASE WHEN COALESCE(build_attempts,0)+1 >= 3 THEN 'error' ELSE 'queued' END "
                                "WHERE dest9=%s AND status IN ('queued','building')", (r["dest9"],))
                    conn.commit()
            except Exception:
                pass
            continue
        if res.get("status") == "built":
            built += 1
            try:   # AUTOPILOT: default quote + Alfred's reveal playbook + Lisa brand intro (once each)
                from . import crm as _crm
                _i = _fetch(pool, "SELECT company, domain, issue FROM lisa4_pool WHERE dest9=%s", (r["dest9"],))
                _co = (_i[0]["company"] if _i else None); _dom = (_i[0]["domain"] if _i else None); _iss = (_i[0].get("issue") if _i else None)
                _crm.ensure_quote_default(pool, r["dest9"], _co, _dom)
                _crm.ensure_reveal_guide(pool, settings, r["dest9"], _co, _dom, "", _iss or "")
                try:   # QA the freshly-built site (cached) so any link we send later is gated on it passing
                    from . import site_qa as _qa
                    _qa.qa_check(pool, settings, r["dest9"])
                except Exception:
                    pass
                _crm.send_brand_intro(pool, settings, r["dest9"], by="Lisa")   # Lisa shares the brand intro (post-booking, once)
            except Exception as exc:
                log.warning("lisa4_autopilot_docs_failed", error=str(exc)[:140])
    return {"built": built, "queued_seen": len(rows)}


def handle_lisa4_postcall(pool: ConnectionPool, settings: Settings, payload: dict) -> dict:
    """Lisa-4's OWN post-call handler (separate webhook) — record the outcome on lisa_calls and, on a booked
    reveal, QUEUE the AI designer to build the site before the meeting. Kept isolated from Lisa-1's handler."""
    ensure_lisa4_tables(pool)
    from . import lisa as _L1
    call = payload.get("call") or payload
    cid = call.get("call_id")
    if not cid:
        return {"ok": False, "error": "no call_id"}
    if (payload.get("event") or "") != "call_analyzed":
        return {"ok": True, "event": payload.get("event")}
    analysis = call.get("call_analysis") or {}
    cad = analysis.get("custom_analysis_data") or {}
    meta = call.get("metadata") or {}
    inbound = (call.get("direction") or "").lower() == "inbound"
    # inbound = a prospect returning Lisa's missed call/SMS: THEY are from_number, our line is to_number
    d9 = _L1._d9(call.get("from_number") if inbound else (meta.get("dest9") or call.get("to_number")))
    if inbound and cid:
        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute("INSERT INTO lisa_calls (call_id, dest9, to_number, from_number, company_name, "
                        "  status, brief) VALUES (%s,%s,%s,%s,%s,'ongoing','{}') "
                        "ON CONFLICT (call_id) DO NOTHING",
                        (cid, d9, call.get("to_number"), call.get("from_number"),
                         (call.get("retell_llm_dynamic_variables") or {}).get("company_name")))
            conn.commit()
    outcome = (cad.get("call_outcome") or "").strip().lower()
    booked = bool(cad.get("meeting_agreed"))
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute("UPDATE lisa_calls SET status='analyzed', call_outcome=%s, meeting_agreed=%s, "
                    "  agreed_day_time=%s, call_summary=%s, transcript=%s, recording_url=%s, "
                    "  duration_ms=%s, callback_when=%s, main_objection=%s, asked_if_ai=%s "
                    "WHERE call_id=%s",
                    (outcome or None, booked, cad.get("agreed_day_time"), cad.get("call_summary"),
                     call.get("transcript"), call.get("recording_url"),
                     call.get("duration_ms") or call.get("call_length_ms"),
                     cad.get("callback_when"), cad.get("main_objection"),
                     bool(cad.get("asked_if_ai")), cid))
        conn.commit()
    # Mirror into the MAIN funnel (calls + classifications) as her own BDE so the main reporting
    # dashboard shows Lisa 4 alongside Lisa-1 and the humans.
    try:
        _L1._write_funnel_call(pool, cid, call.get("retell_llm_dynamic_variables") or {}, cad,
                               ({**call, "to_number": call.get("from_number")} if inbound else call),
                               bde_ext="LISA4", bde_name="Lisa 4")
    except Exception as exc:
        log.warning("lisa4_funnel_write_failed", error=str(exc)[:140])
    # BOOKED REVEAL → queue the AI designer to build the site (idempotent: skip if one already queued/built)
    if booked and d9:
        exists = _fetch(pool, "SELECT 1 FROM lisa4_sites WHERE dest9=%s AND status IN ('queued','building','built') LIMIT 1", (d9,))
        if not exists:
            row = _fetch(pool, "SELECT company, domain, bucket, issue FROM lisa4_pool WHERE dest9=%s", (d9,))
            p = dict(row[0]) if row else {}
            with pool.connection() as conn, conn.cursor() as cur:
                cur.execute("INSERT INTO lisa4_sites (dest9, domain, company, bucket, issue, status) "
                            "VALUES (%s,%s,%s,%s,%s,'queued')",
                            (d9, p.get("domain"), p.get("company"), p.get("bucket"), p.get("issue")))
                conn.commit()
            log.info("lisa4_reveal_booked_build_queued", dest9=d9, company=p.get("company"))
        # put the REVEAL on the Lisa4 calendar (what the floor pipeline + the human closer see). The human
        # runs this meeting — Lisa only books it. Time parsed from the prospect's spoken words.
        try:
            when = _L1._parse_when(cad.get("agreed_day_time"))
            if when:
                who = (call.get("retell_llm_dynamic_variables") or {}).get("company_name") or d9
                ex_ev = _fetch(pool, "SELECT 1 FROM calendar_events WHERE bde_name='Lisa4' AND type='reveal' "
                               "AND status='pending' AND right(regexp_replace(COALESCE(dest_number,''),'[^0-9]','','g'),9)=%s LIMIT 1", (d9,))
                if not ex_ev:
                    with pool.connection() as conn, conn.cursor() as cur:
                        cur.execute("INSERT INTO calendar_events (bde_name,type,title,start_at,end_at,notes,"
                                    "dest_number,created_by,status) VALUES ('Lisa4','reveal',%s,%s,%s,%s,%s,'lisa4','pending')",
                                    (f"🌐 Website reveal: {who}", when, when + timedelta(minutes=15),
                                     f"Prospect said: {cad.get('agreed_day_time') or ''} · human closer runs this; site auto-builds first",
                                     (call.get("from_number") if inbound else call.get("to_number"))))
                        conn.commit()
                    log.info("lisa4_reveal_event_created", dest9=d9, when=str(when))
        except Exception as exc:
            log.warning("lisa4_reveal_event_failed", error=str(exc)[:140])
        # she promises "I'll text you the invite" on every booking — keep that promise automatically
        try:
            pn = call.get("from_number") if inbound else call.get("to_number")
            if (pn or "").startswith("+614") and _L1._twilio_ready(settings):
                frm = (list(getattr(settings, "lisa4_numbers", []) or []) or [""])[0]
                when_txt = (cad.get("agreed_day_time") or "the time we agreed").strip()
                body = (f"Hey, Lisa from DE Group - locked in for {when_txt} for the website reveal. "
                        "I'll text you the video link just before we jump on. Cheers!")
                if frm and _L1._send_sms_twilio(settings, pn, body, frm):
                    _L1._log_sms(pool, "outbound", frm, pn, body, d9)
        except Exception as exc:
            log.warning("lisa4_booking_sms_failed", error=str(exc)[:120])
    # NEVER-CONNECTED calls carry no analysis outcome — classify them off the telephony reason instead,
    # or they'd be silently one-shot lost: dead numbers get purged, busy/no-pickup get a normal retry.
    if not outcome and d9:
        reason = (call.get("disconnection_reason") or "").strip().lower()
        if reason in ("invalid_destination", "no_valid_payment", "concurrency_limit_reached"):
            if reason == "invalid_destination":
                with pool.connection() as conn, conn.cursor() as cur:
                    cur.execute("UPDATE lisa4_pool SET bucket='ok', issue='dead number' WHERE dest9=%s", (d9,))
                    cur.execute("UPDATE calendar_events SET status='cancelled' WHERE bde_name=%s AND status='pending' "
                                "AND right(regexp_replace(COALESCE(dest_number,''),'[^0-9]','','g'),9)=%s", (_BDE, d9))
                    conn.commit()
            return {"ok": True, "booked": False, "outcome": reason}
        if reason in ("dial_busy", "dial_no_answer", "dial_failed"):
            outcome = "no_answer"
    # WRONG NUMBER → the pool number doesn't reach this business (often a stranger's mobile). Kill the
    # pool row and any pending events so she never redials a random person.
    if outcome == "wrong_number" and d9:
        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute("UPDATE lisa4_pool SET bucket='ok', issue='wrong number - unreachable' WHERE dest9=%s", (d9,))
            cur.execute("UPDATE calendar_events SET status='cancelled' WHERE bde_name=%s AND status='pending' "
                        "AND right(regexp_replace(COALESCE(dest_number,''),'[^0-9]','','g'),9)=%s", (_BDE, d9))
            conn.commit()
        return {"ok": True, "booked": False, "outcome": outcome, "cleaned": "wrong_number"}
    # NOT booked → Lisa-4's own next move (callback at the promised time / paced retry). Without this,
    # every "call me back Monday" she earns would silently die — the warmest path to a booking.
    if not booked and d9:
        try:
            schedule_lisa4_followup(pool, settings, dest9=d9,
                                    dest_number=call.get("to_number") or "",
                                    outcome=outcome, cad=cad,
                                    dyn=call.get("retell_llm_dynamic_variables") or {})
        except Exception as exc:
            log.warning("lisa4_followup_failed", error=str(exc)[:140])
    return {"ok": True, "booked": booked, "outcome": outcome}


def handle_lisa4_inbound_sms(pool: ConnectionPool, settings: Settings, from_number: str, body: str) -> dict:
    """A prospect TEXTED Lisa-4's number back. Capture it, then hand off to the SHARED inbound-SMS
    auto-responder on the Lisa-4 line — curiosity + get-them-on-a-call, NEVER the website pitch pre-booking;
    a texted time books a Lisa-4 callback. (Replaces the old canned 'buzz in the morning' + auto-ASAP-callback:
    the reply now aims to get a concrete time, and only THAT books a callback.)"""
    from . import lisa as _L1
    ensure_lisa4_tables(pool)
    d9 = _L1._d9(from_number)
    frm_lisa = (list(getattr(settings, "lisa4_numbers", []) or []) or [""])[0]
    _L1._log_sms(pool, "inbound", from_number, frm_lisa, body, d9)
    return _L1.reply_to_inbound_sms(pool, settings, dest9=d9, from_line="L4", inbound_text=body)


def schedule_lisa4_followup(pool: ConnectionPool, settings: Settings, *, dest9: str, dest_number: str,
                            outcome: str, cad: dict, dyn: dict) -> None:
    """Lisa-4's next move after a non-booked call, on the Lisa4 calendar (never Lisa-1's): a callback at
    the time the prospect asked for, or a paced retry for no-pickups. Mirrors Lisa-1's follow-up rules."""
    from .calendar import create_event
    from . import lisa as _L1
    tz = settings.tz
    who = dyn.get("company_name") or dest_number
    # NEVER schedule a follow-up on a call that already BOOKED a meeting — the callback would re-pitch a
    # won deal the next day (Grandeur, 2026-08-19). Booked prospects get the reveal flow, not another dial.
    if cad.get("meeting_agreed"):
        return
    if outcome == "callback_requested" or cad.get("callback_when"):
        when = _L1._parse_when(cad.get("callback_when")) or (
            datetime.now(ZoneInfo(tz)) + timedelta(days=1)).replace(hour=10, minute=0, second=0, microsecond=0)
        create_event(pool, bde_name=_BDE, type="callback", title=f"📞 Lisa4 callback: {who}",
                     start_at=when, end_at=when + timedelta(minutes=15),
                     notes=f"Prospect asked for a callback: {cad.get('callback_when') or ''}",
                     dest_number=dest_number, created_by="lisa4")
        return
    if outcome in ("no_answer", "voicemail", "gatekeeper_only"):
        # MOBILE-ONLY (Raj, 2026-08-10): never schedule a cold retry to a landline. (Pool is mobile-only,
        # so this is belt-and-suspenders; requested callbacks are handled above and stay exempt.)
        if (dest9 or "")[:1] != "4":
            return
        # MISSED-CALL SMS (once per prospect, mobiles only): ultra-minimal callback bait — NO pitch, no
        # website talk. The reveal only works live, so the text's one job is to make the phone ring back.
        if outcome in ("no_answer", "voicemail") and (dest_number or "").startswith("+614"):
            try:
                row = _fetch(pool, "SELECT sms_sent FROM lisa4_pool WHERE dest9=%s", (dest9,))
                if row and not row[0].get("sms_sent") and _L1._twilio_ready(settings):
                    frm = (list(getattr(settings, "lisa4_numbers", []) or []) or [""])[0]
                    body = ("Hey, it's Lisa — tried to give you a buzz. Could you call me back on this "
                            "number when you get a sec? Ta!")
                    if frm and _L1._send_sms_twilio(settings, dest_number, body, frm):
                        _L1._log_sms(pool, "outbound", frm, dest_number, body, dest9)
                        with pool.connection() as conn, conn.cursor() as cur:
                            cur.execute("UPDATE lisa4_pool SET sms_sent=true WHERE dest9=%s", (dest9,))
                            conn.commit()
                        log.info("lisa4_miss_sms_sent", to=dest_number)
            except Exception as exc:
                log.warning("lisa4_miss_sms_failed", error=str(exc)[:120])
        # attempts across ALL Lisa-4 lines ever (registry regex) — a prospect first dialed from rested
        # 0256 must not get a fresh retry budget just because the rotation pool changed
        attempts = _fetch(pool, "SELECT count(*) n FROM lisa_calls WHERE COALESCE(from_number,'') ~ %s AND dest9=%s",
                          (L4_LINE_RX, dest9))[0]["n"]
        if attempts >= int(getattr(settings, "lisa_retry_max_attempts", 4)):
            return
        now_l = datetime.now(ZoneInfo(tz))
        wstart = int(getattr(settings, "lisa_call_window_start", 9))
        wend = int(getattr(settings, "lisa_call_window_end", 17))
        dt_h = int(getattr(settings, "lisa_double_tap_hours", 2))
        cand = now_l + timedelta(hours=dt_h)
        # VOICEMAIL LEVER (Raj, 2026-08-10): voicemail now ALSO gets a same-day double-tap, then
        # next-business-day retries at a VARIED hour (owners who dodge a 9am unknown mobile often answer
        # at 2pm) instead of one 3-day-out attempt — this is the top-of-funnel wall post-mobile-first.
        _vary = [9, 13, 15, 11]
        if outcome in ("no_answer", "voicemail") and attempts <= 1 and dt_h > 0 and cand.weekday() < 5 and wstart <= cand.hour < wend:
            when, label = cand, "double-tap"
        elif outcome in ("no_answer", "voicemail"):
            _hr = min(max(_vary[min(attempts, len(_vary) - 1)], wstart), wend - 1)
            when, label = _L1._future_biz(now_l + timedelta(days=1), _hr), "retry"
        else:
            when = _L1._future_biz(now_l + timedelta(days=int(getattr(settings, "lisa_retry_cadence_days", 3))), wstart)
            label = "retry"
        create_event(pool, bde_name=_BDE, type="retry", title=f"🔄 Lisa4 {label} ({attempts+1}): {who}",
                     start_at=when, end_at=when + timedelta(minutes=15),
                     notes="No pickup yet — Lisa4 retries at a different time.",
                     dest_number=dest_number, created_by="lisa4")


# --------------------------------------------------------------------------- #
# Floor snapshot — one payload that powers the Outbound-Intelligence rep rail
# --------------------------------------------------------------------------- #
# LINE ATTRIBUTION (Raj, 2026-08-12): a lisa_calls row belongs to Lisa-4 iff EITHER leg
# (from_number when she dials out, to_number when a prospect calls her back) contains the
# Lisa-4 line digits; EVERYTHING else is Lisa-1. The two agents exactly partition the table,
# so floor_today is the exact sum of the two rep cards — no row counted twice or dropped
# (caller-ID-list matching drifted: formatting differences + inbound legs mis-attributed).
# APPEND-ONLY line registry (rotation pool, 2026-08-12): every line Lisa-4 has EVER owned lives here —
# attribution over history must NOT depend on the current LISA4_FROM_NUMBERS rotation (0256 is rested
# for outbound but its historical rows and its inbound leg are still Lisa-4's). Add, never remove.
L4_LINE_DIGITS_ALL = ("468030256", "489266405", "495044526", "468091513")
L4_LINE_RX = "(" + "|".join(L4_LINE_DIGITS_ALL) + ")"
L4_LINE_DIGITS = L4_LINE_DIGITS_ALL[0]  # legacy single-line marker (pre-rotation importers)
_L4_PRED = ("(COALESCE(from_number,'') ~ %s "
            "OR COALESCE(to_number,'') ~ %s)")
# Lisa-5 line registry (append-only, same either-leg rule as Lisa-4). Lisa-5 got her own caller IDs
# AFTER the 2-agent partition above was written, so her legs were falling into the Lisa-1 bucket and
# her calls/inbound callbacks showed up on the OFF Lisa-1 card (Raj flagged 2026-08-14). Lisa-1 must be
# EVERYTHING that is neither Lisa-4 nor Lisa-5.
L5_LINE_DIGITS_ALL = ("468096730", "468008827")
L5_LINE_RX = "(" + "|".join(L5_LINE_DIGITS_ALL) + ")"
_L5_PRED = ("(COALESCE(from_number,'') ~ %s "
            "OR COALESCE(to_number,'') ~ %s)")


def _agent_today(pool: ConnectionPool, tz: str, *, lisa4: bool, out_numbers: list[str]) -> dict:
    """Today's numbers for one agent under the either-leg attribution rule.
    'calls' = OUTBOUND legs only (from_number is the line's own caller ID); convos/booked/
    callbacks/cost count BOTH directions — an inbound call-back that books (prospect is
    from_number) belongs to the line it called, otherwise inbound bookings vanish from the
    rep card (2-of-4 bug 2026-08-06; Samantha inbound-booking bug 2026-08-12)."""
    # Lisa-1 = neither Lisa-4 NOR Lisa-5 (else Lisa-5's legs leak onto the OFF Lisa-1 card).
    attr = _L4_PRED if lisa4 else f"(NOT {_L4_PRED} AND NOT {_L5_PRED})"
    if lisa4:
        out_cond, out_param = "COALESCE(from_number,'') ~ %s", L4_LINE_RX
        attr_params = (L4_LINE_RX, L4_LINE_RX)
    else:
        out_cond, out_param = "from_number = ANY(%s)", list(out_numbers or [])
        attr_params = (L4_LINE_RX, L4_LINE_RX, L5_LINE_RX, L5_LINE_RX)
    r = _fetch(pool,
        f"SELECT count(*) FILTER (WHERE {out_cond}) calls, "
        "  count(*) FILTER (WHERE call_outcome IN ('not_interested','callback_requested') OR meeting_agreed) convos, "
        "  count(*) FILTER (WHERE meeting_agreed) booked, "
        "  count(*) FILTER (WHERE call_outcome='callback_requested') callbacks, "
        "  COALESCE(sum(cost_cents),0) cost_cents "
        f"FROM lisa_calls WHERE {attr} "
        "  AND (created_at AT TIME ZONE %s)::date=(now() AT TIME ZONE %s)::date",
        (out_param, *attr_params, tz, tz))[0]
    return dict(r)


def _agent_inflight(pool: ConnectionPool, *, lisa4: bool) -> dict | None:
    """The agent's ongoing call, attributed by the same either-leg rule (an inbound live
    call on the Lisa-4 line shows on the Lisa-4 card, not Lisa-1's)."""
    attr = _L4_PRED if lisa4 else f"(NOT {_L4_PRED} AND NOT {_L5_PRED})"
    attr_params = (L4_LINE_RX, L4_LINE_RX) if lisa4 else (L4_LINE_RX, L4_LINE_RX, L5_LINE_RX, L5_LINE_RX)
    r = _fetch(pool,
        "SELECT company_name, to_number, round(extract(epoch FROM (now()-created_at))) s "
        f"FROM lisa_calls WHERE status='ongoing' AND {attr} "
        "  AND created_at > now() - interval '10 minutes' ORDER BY created_at DESC LIMIT 1",
        attr_params)
    return dict(r[0]) if r else None


def floor_snapshot(pool: ConnectionPool, settings: Settings) -> dict:
    """Everything the rep rail + floor strip needs, in one cheap payload: per-agent status (Lisa 1 + Lisa 4),
    today's numbers, in-flight call, pool/queue depth, autodial state, heartbeat, and Lisa 4's build
    pipeline. All queries are small aggregates."""
    from . import lisa as _L1
    ensure_lisa4_tables(pool)
    ensure_lisa4_control(pool)
    tz = settings.tz
    n1 = list(getattr(settings, "lisa_numbers", []) or [])
    n4 = list(getattr(settings, "lisa4_numbers", []) or [])
    now = datetime.now(ZoneInfo(tz))
    wstart = int(getattr(settings, "lisa_call_window_start", 9))
    wsmin = int(getattr(settings, "lisa_call_window_start_min", 0))
    wend = int(getattr(settings, "lisa_call_window_end", 17))
    in_window = now.weekday() < 5 and (wstart * 60 + wsmin) <= now.hour * 60 + now.minute < wend * 60

    # Lisa 1
    hb = _fetch(pool, "SELECT round(extract(epoch FROM (now()-hos_heavy_at))) s FROM lisa_control WHERE id=1")
    l1 = {
        "key": "lisa1", "name": "Lisa 1", "role": "Appointment setter", "campaign": "GAds prospects · $2-50M",
        "numbers": n1, "autodial": _L1.get_autodial_state(pool, settings), "in_window": in_window,
        "today": _agent_today(pool, tz, lisa4=False, out_numbers=n1), "inflight": _agent_inflight(pool, lisa4=False),
        "pool": _fetch(pool, "SELECT count(*) n FROM lisa_pool")[0]["n"],
        "queue_due": _fetch(pool, "SELECT count(*) n FROM calendar_events WHERE bde_name='Lisa' AND status='pending' "
                            "AND type IN ('fresh_call','retry','callback','reached_call') AND start_at<=now()")[0]["n"],
        "target": int(getattr(settings, "lisa_daily_target", 300)),
        "heartbeat_s": (hb[0]["s"] if hb and hb[0].get("s") is not None else None),
    }
    # Lisa 4
    pipe = {r["status"]: r["n"] for r in _fetch(pool, "SELECT status, count(*) n FROM lisa4_sites GROUP BY status")}
    buckets = {r["bucket"]: r["n"] for r in _fetch(pool, "SELECT bucket, count(*) n FROM lisa4_pool GROUP BY bucket")}
    reveals = _fetch(pool,
        "SELECT e.title, to_char((e.start_at AT TIME ZONE %s),'Dy DD Mon HH24:MI') at_local, "
        "  (SELECT s.status FROM lisa4_sites s WHERE s.dest9=right(regexp_replace(COALESCE(e.dest_number,''),'[^0-9]','','g'),9) "
        "     ORDER BY s.created_at DESC LIMIT 1) site_status "
        "FROM calendar_events e WHERE e.bde_name='Lisa4' AND e.status='pending' AND e.type='reveal' "
        "  AND e.start_at >= now() - interval '2 hours' ORDER BY e.start_at LIMIT 8", (tz,))
    l4 = {
        "key": "lisa4", "name": "Lisa 4", "role": "Website selling", "campaign": "No/broken-website SMBs",
        "numbers": n4, "autodial": get_lisa4_autodial(pool, settings), "in_window": in_window,
        "today": _agent_today(pool, tz, lisa4=True, out_numbers=n4), "inflight": _agent_inflight(pool, lisa4=True),
        "pool": _fetch(pool, "SELECT count(*) n FROM lisa4_pool")[0]["n"],
        "queue_due": _fetch(pool, "SELECT count(*) n FROM calendar_events WHERE bde_name='Lisa4' AND status='pending' "
                            "AND type='fresh_call' AND start_at<=now()")[0]["n"],
        "target": int(getattr(settings, "lisa4_daily_target", 200)),
        "buckets": buckets,
        "pipeline": {"queued": pipe.get("queued", 0), "building": pipe.get("building", 0),
                     "built": pipe.get("built", 0), "error": pipe.get("error", 0)},
        "reveals": [dict(r) for r in reveals],
    }
    # Lisa 5 (growth-audit reveal) — direct-scoped to her caller IDs (registry-independent)
    import re as _re5
    n5 = [s.strip() for s in str(getattr(settings, "lisa5_from_numbers", "") or "").split(",") if s.strip()]
    n5d = [_re5.sub(r"[^0-9]", "", x)[-9:] for x in n5] or ["468096730", "468008827"]
    try:
        l5_today = _fetch(pool,
            "SELECT count(*) FILTER (WHERE right(regexp_replace(COALESCE(from_number,''),'[^0-9]','','g'),9)=ANY(%s)) calls, "
            "  count(*) FILTER (WHERE COALESCE(duration_ms,0)/1000 >= 20) convos, "
            "  count(*) FILTER (WHERE meeting_agreed IS TRUE) booked "
            "FROM lisa_calls WHERE (right(regexp_replace(COALESCE(from_number,''),'[^0-9]','','g'),9)=ANY(%s) "
            "  OR right(regexp_replace(COALESCE(to_number,''),'[^0-9]','','g'),9)=ANY(%s)) "
            "  AND (created_at AT TIME ZONE %s)::date=(now() AT TIME ZONE %s)::date", (n5d, n5d, n5d, tz, tz))[0]
        l5_auto = _fetch(pool, "SELECT autodial FROM lisa5_control WHERE id=1")
        l5_pool = _fetch(pool, "SELECT count(*) n FROM lisa5_pool")[0]["n"]
        l5_queue = _fetch(pool, "SELECT count(*) n FROM calendar_events WHERE bde_name='Lisa5' AND status='pending' "
                          "AND type IN ('fresh_call','retry','callback') AND start_at<=now()")[0]["n"]
    except Exception:
        l5_today, l5_auto, l5_pool, l5_queue = {"calls": 0, "convos": 0, "booked": 0}, None, 0, 0
    l5 = {
        "key": "lisa5", "name": "Lisa 5", "role": "Growth audit", "campaign": "gmaps · has-website · 10+ reviews",
        "numbers": n5, "autodial": (bool(l5_auto[0]["autodial"]) if l5_auto else False), "in_window": in_window,
        "today": l5_today, "inflight": _agent_inflight(pool, lisa4=False) if False else None,
        "pool": l5_pool, "queue_due": l5_queue,
        "target": int(getattr(settings, "lisa5_daily_target", 200)),
    }
    floor = {k: (l1["today"].get(k, 0) or 0) + (l4["today"].get(k, 0) or 0) + (l5["today"].get(k, 0) or 0)
             for k in ("calls", "convos", "booked", "callbacks", "cost_cents")}
    return {"agents": [l1, l4, l5], "floor_today": floor, "in_window": in_window,
            "window": f"{wstart:02d}:{wsmin:02d}–{wend:02d}:00 Mon–Fri",
            "now_local": now.strftime("%a %H:%M")}


def list_sites(pool: ConnectionPool, limit: int = 50) -> list[dict]:
    """Lisa 4's AI-designed sites (meta only — HTML served separately for the preview iframe)."""
    ensure_lisa4_tables(pool)
    return _fetch(pool,
        "SELECT id, dest9, company, domain, bucket, issue, status, model, error, "
        "  to_char((created_at AT TIME ZONE 'Australia/Melbourne'),'DD Mon HH24:MI') created_local, "
        "  to_char((built_at AT TIME ZONE 'Australia/Melbourne'),'DD Mon HH24:MI') built_local, "
        "  length(COALESCE(html,'')) html_bytes "
        "FROM lisa4_sites ORDER BY created_at DESC LIMIT %s", (limit,))


def get_site_html(pool: ConnectionPool, site_id: int) -> str | None:
    r = _fetch(pool, "SELECT html FROM lisa4_sites WHERE id=%s", (site_id,))
    return (r[0].get("html") if r else None) or None
