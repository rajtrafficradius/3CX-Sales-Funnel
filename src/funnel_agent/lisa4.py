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
from .enrichment.website import fetch_website_intel, website_audit, extract_logo, scrape_site_media, logo_tone
from .logging import get_logger
from .qa import dynvars as _qa_dyn   # G8/G9/G10 pure in-memory dynamic-variable safety

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
    # 2026-08-27: missing trade nouns caused real businesses to be MISREAD as person names ('KINA DIVING'
    # -> rejected -> ugly domain run-on 'Kinadiving' shipped on the site). Keep extending when one recurs.
    "DIVING", "ASPHALT", "PAVING", "SURVEYING", "SURVEYORS", "RIGGING", "CRANES", "HAULAGE", "FREIGHT",
    "COURIERS", "CARTAGE", "MOWING", "ARBOR", "ARBORIST", "TREES", "TURF", "IRRIGATION", "DRAINAGE",
    "PSYCHOLOGY", "COUNSELLING", "CHIROPRACTIC", "RADIOLOGY", "PATHOLOGY", "GROOMING", "DOGGY", "PETS",
    "STABLES", "SHEARING", "FENCERS", "WELDERS", "FABRICATION", "STEEL", "ALUMINIUM", "STAINLESS",
    "MECHANICS", "DETAILING", "PANEL", "SMASH", "TOWING", "BATTERIES", "EXHAUST", "SUSPENSION",
    "CONTRACTING", "CONTRACTORS", "CIVIL", "EARTHWORKS", "PLANT", "FORMWORK", "PILING",
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
    """A speakable trading name from a registered company name: PREFER the trading name over the legal
    wrapper and strip legal-entity noise, so a real business survives ('GW LOGISTICS PTY LTD' ->
    'GW Logistics', 'ACME HOLDINGS PTY LTD T/A ACME PLUMBING' -> 'Acme Plumbing') while a raw legal
    entity is never spoken. Returns '' only for a pure person / partnership / trust name (a legal person
    or 'The trustee for … Trust' read aloud is an instant robocall tell — better nameless)."""
    c = (company or "").strip()
    if not c:
        return ""
    # PREFER THE TRADING NAME: "<legal entity> T/A <trading name>" -> keep the trading name (right side),
    # which is what customers actually know and what Lisa should say.
    ta = _re.split(r"\s+(?:T/?A|TRADING\s+AS)\s+", c, maxsplit=1, flags=_re.I)
    if len(ta) == 2 and len(ta[1].strip()) >= 2:
        c = ta[1].strip()
    # DROP THE TRUST WRAPPER: "<operating entity> ATF/AS TRUSTEE FOR <… trust>" -> keep the operating
    # entity (left side); a bare "The trustee for … Trust" (no operating entity) falls through to ''.
    c = _re.split(r"\s+(?:ATF|A\.?T\.?F\.?|AS\s+TRUSTEE\s+FOR)\s+", c, maxsplit=1, flags=_re.I)[0].strip()
    c = _re.sub(r"^THE\s+TRUSTEE\s+FOR\s+", "", c, flags=_re.I).strip()
    c = _re.sub(r"^T/?A\s+", "", c, flags=_re.I).strip()
    # strip bracketed noise Lisa should never voice: "(INT)", "(AUST)", "(VIC)", "[The]", …
    c = _re.sub(r"\s*[\(\[][^\)\]]*[\)\]]", " ", c).strip()
    prev = None
    while prev != c:                                   # strip stacked suffixes ('... PTY. LTD.')
        prev = c
        c = _LEGAL_SUFFIX_RE.sub("", c).strip().rstrip(",").strip()
    c = _re.sub(r"\s{2,}", " ", c).strip(" ,.-")
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
# POOL HYGIENE (Fix #2) — keep the dialer fed with GOOD prospects.
#   HARD-suppress  = never queue/dial (structurally dead data). Kept DELIBERATELY NARROW so the
#                    queue can never be starved: only clearly-bad records are dropped.
#   SOFT-deprioritize = still fully dialable, just ordered LAST behind stronger prospects — so the
#                    queue drains best-first and can NEVER run dry.
# --------------------------------------------------------------------------- #
# issue markers a prior call already wrote proving the number is dead / unreachable / opted-out.
_L4_DEAD_ISSUES = {"dead number", "wrong number", "wrong number - unreachable",
                   "opt-out", "opt-out (sms)"}
_NONLATIN_RE = _re.compile(r"[^\x00-\x7f]")


def _lisa4_hard_suppress(row: dict) -> bool:
    """True = NEVER queue this prospect. Only clearly-dead data: a non-mobile number (can't reach an
    owner), or a number a prior call already proved wrong/dead or that opted out. Everything else stays
    dialable (soft-deprioritized, never removed) so the queue can't be starved."""
    d9 = (row.get("dest9") or "")
    if not d9 or d9[:1] != "4":                      # no AU mobile (belt; the SQL also gates left(dest9)='4')
        return True
    if (row.get("bucket") or "").strip().lower() == "ok" and \
       (row.get("issue") or "").strip().lower() in _L4_DEAD_ISSUES:
        return True
    return False


def _lisa4_soft_rank(row: dict) -> int:
    """Ordering nudge ONLY (never removes a prospect). Lower = dial first. Strong prospects (a real,
    speakable website issue + a speakable owner/trading name) sort first; weak-but-dialable ones sort
    LAST. Signals (all conservative, all additive):
      +4  has a live site but NO concrete issue yet → can't do the proven "we looked at YOUR site,
          noticed X" hook until background enrichment finds one (Fix #1 deprioritize, never drop).
      +3  a "coming soon" / "under construction" site → they're ALREADY getting a new site (low convert).
      +2  no speakable owner/trading name to open with (personal/legal-only name; correlates with
          no-reachable-owner + ESL partnerships) → harder RPC, still dialable.
      +1  likely-ESL: a name dominated by non-Latin script (soft signal only)."""
    domain = (row.get("domain") or "").strip()
    issue = _speakable_issue(row.get("issue"))
    name = _pick_name(row.get("company"), row.get("title"), row.get("domain"))
    text = f"{row.get('company') or ''} {row.get('title') or ''} {issue or ''}"
    rank = 0
    if domain and not issue:
        rank += 4
    if _re.search(r"coming\s+soon|under\s+construction|being\s+built|site\s+coming", text, _re.I):
        rank += 3
    if not name:
        rank += 2
    letters = [c for c in text if c.isalpha()]
    if letters and sum(1 for c in letters if _NONLATIN_RE.match(c)) / len(letters) > 0.3:
        rank += 1
    return rank


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
from .gmaps import _CHAIN_BLOCK, place_photos  # noqa: E402  (gmaps has no module-level import of lisa4 — no cycle)

_L4X_AGENCY_TERMS = [
    r"\bmarketing\b", r"\bmedia\b", r"\bcreative\b",
    r"\bdigital\s+agenc", r"\bad\s+agenc", r"\badvertis",
    r"\bseo\b", r"\bsearch\s+engine\s+optimi",
    # 'desig'/'develop' as PREFIXES so stylised plurals land too (designz, designs, designers).
    r"\bweb\s*(site)?\s*desig", r"\bweb\s*(site)?\s*develop", r"\bweb\s+dev\b",
    # Stylised names put a filler token between the two words — "Web E Designz" (called twice on
    # 2026-09-02) sailed through because the pattern above needs 'design' straight after 'web'.
    # `\bweb\b` (web as its own word) keeps "Weber Design", a joinery/building name, out of it.
    r"\bweb\b\s+\w{1,3}\s+desig", r"\bweb\b\s+\w{1,3}\s+develop",
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


_L4_TABLES_READY = False


def ensure_lisa4_tables(pool: ConnectionPool, force: bool = False) -> None:
    """Create/patch Lisa-4's tables ONCE per process.

    LOCK-CONVOY FIX (2026-08-31): this ran its full DDL on EVERY call — and reserve/schedule/build/dial all
    call it, so `ALTER TABLE lisa4_pool …` fired many times a minute. Each ALTER queues for an
    AccessExclusiveLock; when it lands behind one slow reader, Postgres parks EVERY later reader behind the
    pending exclusive lock and the whole table stalls (observed live: a 444s blocked ALTER froze the pool
    count, Emma's booking query and the dialer's own reads). Same pattern already solved in
    ensure_emma_tables: probe a cheap sentinel column first and skip the DDL entirely once present."""
    global _L4_TABLES_READY
    if _L4_TABLES_READY and not force:
        return
    if not force:
        try:
            with pool.connection() as conn, conn.cursor() as cur:
                cur.execute("SELECT 1 FROM information_schema.columns "
                            "WHERE table_name='lisa4_pool' AND column_name='sms_sent'")
                if cur.fetchone() is not None:
                    _L4_TABLES_READY = True
                    return
        except Exception:
            pass
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
    _L4_TABLES_READY = True


def reserve_lisa4_pool(pool: ConnectionPool, settings: Settings, scan_batch: int = 60) -> dict:
    """Top up lisa4_pool toward lisa4_pool_size with AU prospects that have a phone AND (no website OR a
    critical website issue), EXCLUDING anyone already in Lisa-1's pool/calls or Lisa-4's own pool.
    Critical-issue candidates are scanned live (concurrently) and classified by the audit engine."""
    ensure_lisa4_tables(pool)
    target = int(getattr(settings, "lisa4_pool_size", 500))
    try:   # bump the pool target from crm_config (no env change / redeploy) so Lisa-4 never runs dry
        _t = _fetch(pool, "SELECT v FROM crm_config WHERE k='lisa4_pool_size'")
        if _t and str(_t[0].get("v") or "").strip().isdigit():
            target = int(_t[0]["v"])
    except Exception:
        pass
    try:   # how many domained gmaps prospects to website-scan per pass (crm_config, no redeploy). Higher =
        _sb = _fetch(pool, "SELECT v FROM crm_config WHERE k='lisa4_scan_batch'")  # faster refill from stock.
        if _sb and str(_sb[0].get("v") or "").strip().isdigit():
            scan_batch = max(20, min(400, int(_sb[0]["v"])))
    except Exception:
        pass
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
            # DOMAIN CHANNEL GUARD, MOVED INTO SQL (2026-08-31): a gmaps row whose DOMAIN also exists in the
            # D&B (raghav) set is Lisa-1/human territory and gets dropped after scanning. Because this batch
            # is ORDER BY co.id LIMIT n, those same rows were re-fetched and re-dropped EVERY pass — the pool
            # STALLED at 2,845/6,000 with 6k stock available and Lisa-4 ran dry (only 6 fresh calls queued for
            # Mon 31 Aug). Excluding them here lets the LIMIT pull genuinely usable candidates instead.
            "  AND NOT (NULLIF(co.domain,'') IS NOT NULL AND EXISTS ("
            "        SELECT 1 FROM companies dnb WHERE dnb.source='raghav' AND dnb.domain = co.domain)) "
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


def enrich_lisa4_pool_issues(pool: ConnectionPool, settings: Settings, limit: int = 15) -> dict:
    """OFF THE DIAL PATH (runs inside run_lisa4_head's background prep — never the dial loop): for
    HAS-SITE pool rows that are mis-bucketed as no_website OR still carry no real, speakable website
    issue, live-scan the site with the EXISTING website enrichment and update lisa4_pool.bucket/issue,
    so Lisa opens with a TRUE "we looked at YOUR site, noticed X" hook instead of the generic (and often
    false) no-site script. Never touches a genuinely domain-less row, never fabricates an issue.
    Batch-limited + concurrent like reserve_lisa4_pool; never raises. Fix #1 — the biggest lever."""
    ensure_lisa4_tables(pool)
    try:
        rows = _fetch(pool,
            "SELECT dest9, domain, bucket, issue FROM lisa4_pool "
            "WHERE NULLIF(domain,'') IS NOT NULL "                     # HAS a live site
            "  AND left(dest9,1) = '4' "                               # dialable mobile (skip dead stock)
            "  AND NOT EXISTS (SELECT 1 FROM lisa_calls lc WHERE lc.dest9=lisa4_pool.dest9) "  # not yet worked
            # mis-bucketed as site-less, OR bucket/issue missing, OR `issue` is only an internal marker
            "  AND (COALESCE(bucket,'') IN ('', 'no_website') "
            "       OR NULLIF(issue,'') IS NULL "
            "       OR lower(issue) = ANY(%s)) "
            "ORDER BY COALESCE(priority,0) DESC, reserved_at LIMIT %s",
            (list(_INTERNAL_ISSUES), limit))
    except Exception as exc:
        log.warning("enrich_lisa4_pool_issues_query_failed", error=str(exc)[:140])
        return {"enriched": 0}
    if not rows:
        return {"enriched": 0}

    def _scan(r: dict):
        try:
            intel = fetch_website_intel(r["domain"], timeout=8.0, verify=False)
            return r, intel, website_audit(intel)
        except Exception:
            return r, {}, {}

    updates = []
    with ThreadPoolExecutor(max_workers=8) as ex:
        for r, intel, aud in ex.map(_scan, rows):
            if not aud:
                continue
            if aud.get("is_target") and aud.get("bucket") == "critical_issue":
                updates.append((r["dest9"], "critical_issue", aud.get("issue")))
            elif aud.get("bucket") == "ok":
                # site WORKS → pitch improvement (never breakage): a soft opinion, never a false fact.
                updates.append((r["dest9"], "upgrade", _soft_issue(intel)))
            # a has-site scan can't return 'no_website'; anything else (unreachable this pass) → leave the
            # row untouched (never fabricate an issue) — candidate-selection just deprioritizes it.
    n = 0
    if updates:
        with pool.connection() as conn, conn.cursor() as cur:
            cur.executemany("UPDATE lisa4_pool SET bucket=%s, issue=%s WHERE dest9=%s",
                            [(b, i, d9) for (d9, b, i) in updates])
            n = cur.rowcount
            conn.commit()
    log.info("enrich_lisa4_pool_issues", scanned=len(rows), enriched=n)
    return {"enriched": n, "scanned": len(rows)}


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
    domain = (d.get("domain") or "").strip()
    has_site = bool(domain)
    bucket = (d.get("bucket") or "").strip().lower()
    issue = _speakable_issue(d.get("issue"))          # never an internal state marker on the call
    # ROUTING FIX (Fix #1 — the biggest conversion lever): a prospect with a REAL live site must get the
    # proven "we looked at YOUR site, noticed X, built you a new one" hook and must NEVER hear the (often
    # factually FALSE) "couldn't find your website" no-site framing. So whenever we hold a domain we force
    # a has-site bucket: 'critical_issue' when a concrete, speakable fault is known, else 'upgrade' (pitch
    # improvement, never breakage) carrying a TRUE-by-design opinion line — never a fabricated factual
    # fault. Only a genuinely domain-less prospect keeps 'no_website'. (Candidate-selection deprioritizes
    # has-site rows that still lack a real issue; the background enrich_lisa4_pool_issues pass fills real
    # issues in ahead of the dialer, so a has-site prospect never dials with a generic/empty brief.)
    if has_site:
        if not issue:
            bucket = "upgrade"
            issue = _soft_issue({})
        elif bucket in ("", "no_website", "ok"):
            bucket = "critical_issue"
        if bucket == "no_website":                    # belt: a has-site prospect can never be site-less
            bucket = "upgrade"
    else:
        bucket = bucket or "no_website"
    pn = (dm.get("dm_first") or "").strip()
    # Free RPC fallback: no Apollo DM, but a no-website sole trader's registered name IS the owner who
    # answers the listed mobile -> open with their first name ('is that Robyn?'). Only for no_website.
    if not pn and bucket == "no_website":
        pn = _owner_first_from_company(d.get("company"))
    return {
        # PREFER THE TRADING NAME + sanitize (Fix #3): Apollo's resolved trading name (legal noise stripped),
        # else the pool's cleaned trading name — never a raw legal entity ('… PTY LTD', 'The trustee for … Trust').
        "company_name": (_clean_company(dm.get("trading_name")) or _clean_title(dm.get("trading_name"))
                         or _pick_name(d.get("company"), d.get("title"), d.get("domain"))),
        # bare domain for the email-confirm move ("I'll flick it to info@<domain> — still the best one?")
        "company_domain": domain.lower().removeprefix("www."),
        "prospect_website": domain,                   # the prospect's REAL live URL, carried into the brief (Fix #1)
        "website_bucket": bucket,
        "website_issue": issue,                       # never an internal marker; never empty for a has-site prospect
        "prospect_email": dm.get("dm_email") or d.get("email") or "",  # Lisa CONFIRMS it, never asks cold
        # G10: a clean spoken FIRST name, or "" — never an article/role ('The'), a legal entity or junk
        "prospect_name": _qa_dyn.clean_prospect_name(pn),
    }


# --------------------------------------------------------------------------- #
# AI designer — Claude API builds the actual website AFTER a reveal is booked
# --------------------------------------------------------------------------- #
_DESIGNER_SYSTEM = (
    "You are the creative director of an elite web studio whose sites win Awwwards. You produce ONE "
    "ULTRA-PREMIUM, modern website as a SINGLE self-contained HTML file (all CSS in <style>, vanilla JS in "
    "<script>, NO external anything — visuals from layered CSS gradients, gradient-mesh backgrounds, subtle "
    "SVG noise/patterns, and hand-drawn inline SVG icons that look custom, never clip-art).\n"
    "\nDISTINCT IDENTITY PER BUILD (most important rule): you build many sites and they must NOT look like "
    "the same template recoloured. The brief assigns you ONE named LAYOUT ARCHETYPE plus a VARIETY SEED and "
    "a SIGNATURE OPENER — you MUST commit fully to that archetype's hero treatment, navigation style, "
    "section order/arrangement, card/component style, motion signature and TYPE PERSONALITY. Two businesses "
    "given different archetypes must look like two different studios built them. NEVER fall back to a "
    "generic default of {sticky glass nav + centred full-bleed hero + a row of three glass cards + the same "
    "section order}; that specific skeleton is only ONE possible treatment and must not be your reflex. Let "
    "the archetype drive the skeleton; let the trade + business drive the palette and copy.\n"
    "\nQUALITY BAR (non-negotiable, 2025-premium — these are the TOOLS every archetype must hit, not a fixed "
    "look):\n"
    "- A striking, oversized display HERO headline (fluid clamp, tight tracking, a single accent word) with "
    "a sharp subhead and dual CTAs (primary = filled with glow/hover state, secondary = ghost) — but the "
    "hero LAYOUT (split, full-bleed photographic, masthead, type-forward, dark-luxe, organic, gridded) is "
    "dictated by the assigned archetype, never a default centred box.\n"
    "- A polished, working navigation (compacts/adapts on scroll; mobile gets a real slide-in menu + a "
    "floating bottom action bar with tap-to-call) — its STYLE (glass, slim-underline, centred serif, "
    "pill, structured grid) follows the archetype.\n"
    "- Generous section rhythm; deliberate contrast breaks; ASYMMETRIC / non-repetitive grids (never the "
    "same three identical boxes in a row down the page); cards with real depth and hover life — card style "
    "per archetype.\n"
    "- Scroll-triggered staggered reveals (IntersectionObserver, translateY+opacity, respects "
    "prefers-reduced-motion); animated stat counters (years, jobs done, rating) when scrolled into view; "
    "smooth micro-interactions on every interactive element — the motion SIGNATURE varies by archetype.\n"
    "- A distinctive palette born from the trade (e.g. timber/charcoal/brass for fencing; never bootstrap "
    "blue, never default grays), defined as CSS custom properties; one accent used sparingly.\n"
    "- Typography with real personality and extreme weight contrast, uppercase kickers with wide tracking, "
    "fluid type scale via clamp() — the TYPE PERSONALITY (geometric grotesk / condensed display / "
    "high-contrast serif / single bold grotesk / elegant serif / humanist rounded / Swiss grotesk) is set "
    "by the archetype.\n"
    "\nCINEMATIC MOTION (non-negotiable — this site must FEEL alive and expensive, not a static page with a "
    "couple of fades; a flat, motionless build is an instant failure):\n"
    "- A CHOREOGRAPHED HERO ENTRANCE on load: a short, deliberate sequence (not everything at once) — the "
    "kicker, then the headline revealing line-by-line (or word-by-word) via clip-path/mask wipes, then the "
    "subhead + CTAs easing up, while the hero photo settles with a slow scale/ken-burns. Stagger with small "
    "delays; use rich easing (cubic-bezier, not linear/ease).\n"
    "- CONTINUOUS AMBIENT MOTION in the background so the page is never dead: slow-drifting gradient-mesh or "
    "aurora, gently floating decorative shapes/orbs, a faint animated grain/sheen, or a subtle conic/gradient "
    "rotation behind the hero — GPU-cheap (transform/opacity only), looping, and understated.\n"
    "- LAYERED PARALLAX & scroll choreography: hero photo, decorative layers and foreground copy move at "
    "different rates on scroll; section reveals are staggered with DIRECTIONAL intent (content enters from "
    "the side it lives on); headline numerals/stats count up when scrolled into view.\n"
    "- MICRO-INTERACTIONS everywhere with real depth: buttons with a light-sweep/glow on hover, cards that "
    "lift + tilt (or reveal an accent) on hover, nav underline that tracks, an animated scroll cue, smooth "
    "scroll-behavior.\n"
    "- A REAL motion system: define MANY distinct @keyframes (aim for ~8+) and reuse them — this is a "
    "signature, not decoration. ALL of it MUST sit inside an @media (prefers-reduced-motion: reduce) guard "
    "that disables transforms/loops for users who ask for less; nothing may break if motion is off.\n"
    "\nBANNED (instant failure): reusing the same skeleton or section order you'd use for any other business; "
    "defaulting to the generic glass-nav + centred-hero + three-glass-card template regardless of archetype; "
    "centered-everything layouts; equal three-card rows repeated; thin grey text on white; generic hero with "
    "a small heading; 2010-era boxy sections; lorem ipsum; filler copy like 'we offer quality services'; "
    "visible section borders everywhere; default-looking buttons.\n"
    "\nREQUIRED CONTENT INGREDIENTS (write like you know this trade cold, Australian tone + spelling) — the "
    "site MUST contain ALL of these, but you ARRANGE and TREAT them per the assigned archetype + signature "
    "opener; do NOT use one fixed order every time:\n"
    "hero · trust signals (rating/years/insured/licensed — ONLY flattering ones: NEVER display a Google "
    "rating below 4.2 or a tiny review count ('3.0★ from 2 reviews' harms trust; simply omit the rating "
    "and lead with insured/licensed/years instead) · services (REAL trade-specific services, "
    "materials, job types — each with a custom SVG icon and 2-3 lines of expert copy) · a signature "
    "'why us' with animated stats · a process (3-4 steps) · a real photo gallery/portfolio (use the "
    "provided {{IMG_n}} photos; only where NO real photo exists, fall back to tasteful CSS-art tiles marked "
    "as examples) · 2-3 realistic testimonials (marked example) · service-area · FAQ (5 REAL questions this "
    "trade gets, accordion) · a conversion section (phone huge + tap-to-call + minimal quote form) · footer "
    "with ABN placeholder + LocalBusiness JSON-LD schema. Order, grouping and emphasis of these follow the "
    "archetype and the SIGNATURE OPENER named in the brief.\n"
    "\nMULTI-PAGE (critical — the owner will CLICK EVERY NAV LINK): build a hash-router SPA inside the "
    "single file with REAL pages — Home, Services, About, Gallery, Contact — each a fully designed page "
    "(own hero band, own content), switched instantly via nav (show/hide + scroll-top + active nav state, "
    "hashchange-driven so back/forward work). On the Services page every service card CLICKS THROUGH to "
    "its own detail page (deep copy: what's included, materials, indicative process, mini-FAQ, CTA). "
    "Nothing may dead-end: every nav link, card, button and footer link must go somewhere real.\n"
    "\nLOGO: If the brief provides a real logo via the {{REAL_LOGO}} placeholder, put that EXACT token "
    "on its own where the logo goes in BOTH the header/nav AND the footer — it is swapped for the "
    "finished, contrast-safe logo element, so do NOT wrap it in your own <img>/<svg>/url() and do NOT "
    "draw, recreate, or approximate a logo. The real logo may be a WHITE/reversed or transparent mark "
    "(built for a dark header); it arrives with its own contrast backing, so keep the area around it "
    "clear — never place it on a same-tone or busy background, and never hide or zero-height it "
    "(~40-56px tall, clearly visible). If NO logo is provided, render the business NAME as a styled, "
    "branded wordmark as the header logo — never leave the logo area blank or invisible.\n"
    "\nREAL PHOTOS (use ALL of them): When the brief lists {{IMG_n}} placeholder tokens, those are the "
    "business's ACTUAL photos. You MUST place EVERY {{IMG_n}} token the brief provides — all of them — each "
    "used exactly ONCE, inside an <img> (or as a CSS background-image url(...)), spread across the hero, "
    "the galleries/portfolio and section/page backgrounds (including the About and Gallery pages). Do NOT "
    "skip, drop or omit any provided photo, and do NOT invent extra {{IMG_n}} tokens beyond the ones listed. "
    "Never invent stock imagery or CSS-art tiles where a real {{IMG_n}} photo is available.\n"
    "Output ONLY raw HTML — no markdown, no code fences, no commentary."
)

# --------------------------------------------------------------------------- #
# LAYOUT ARCHETYPES — give every generated site a DISTINCT visual identity.
# build_website derives a stable VARIETY SEED per business (from dest9 + name — never
# random / date-based) and picks ONE archetype + ONE signature opener deterministically,
# so two different businesses do not share the same skeleton, yet the same business always
# rebuilds to the same identity. Each spec dictates hero treatment, nav style, section
# arrangement, card style, type personality, palette leaning and motion signature.
# --------------------------------------------------------------------------- #
_LAYOUT_ARCHETYPES: tuple[tuple[str, str], ...] = (
    ("Asymmetric Split",
     "Hero = a hard vertical SPLIT (about 55/45): an oversized LEFT-aligned display headline + kicker + "
     "dual CTAs on one side, and a full-height real photo panel ({{IMG_1}}) bleeding to the screen edge on "
     "the other — never a centred box. NAV: slim, logo/wordmark left + inline text links right with a thin "
     "moving underline indicator (NOT a glass blob). SECTIONS march in an OFFSET rhythm — content blocks "
     "alternate left/right, each paired with a photo or a stat column; deliberately asymmetric, never "
     "symmetric rows. CARDS: flat with a single hairline and a bold coloured top-accent bar that slides on "
     "hover. TYPE: a strong geometric grotesk, dramatic 300-vs-800 weight jumps, wide-tracked kickers. "
     "PALETTE: a confident two-tone trade colour plus a crisp off-white. MOTION: blocks slide in from the "
     "side they sit on."),
    ("Full-Bleed Photographic",
     "Hero = a FULL-VIEWPORT real photograph ({{IMG_1}}) under a directional dark-to-clear gradient scrim; "
     "headline + subhead + CTAs anchored lower-left with a scroll cue. NAV: transparent over the hero, "
     "resolving to a solid/tinted bar on scroll. SECTIONS alternate FULL-BLEED photo BANDS (real {{IMG_n}} "
     "backgrounds with overlaid copy) against tight text sections — a cinematic, editorial cadence. CARDS: "
     "image-led tiles where the photo IS the card, caption over a gradient foot. TYPE: a condensed "
     "uppercase display for headings, clean sans body. PALETTE: photo-driven neutrals plus one vivid "
     "accent. MOTION: slow parallax / ken-burns drift on the photo bands (reduced-motion safe)."),
    ("Editorial Magazine",
     "Treat the whole site like a design magazine. Hero = a MASTHEAD: a large high-contrast SERIF title, a "
     "kicker rule above it, a standfirst paragraph, and a single lead photo ({{IMG_1}}) in a bordered "
     "frame. NAV: centred serif wordmark with fine underlined links beneath a hairline rule. SECTIONS use "
     "a multi-column editorial grid — pull-quotes, a drop-cap on the first paragraph, numbered features, "
     "thin dividing rules, generous margins. CARDS: bordered 'article' cards with a category kicker + "
     "read-more. TYPE: a high-contrast serif for display plus a clean grotesk body, italic accents. "
     "PALETTE: paper/ink plus one editorial spot colour. MOTION: restrained fades and rule draw-ins."),
    ("Bold Typographic Minimal",
     "Type IS the design. Hero is text-forward: an ENORMOUS headline (fluid, up to ~9vw) filling the "
     "viewport with generous negative space, a tiny kicker and one primary CTA — imagery minimal or a "
     "single small framed photo. NAV: tiny, wide letter-spaced, top-right. SECTIONS are spare and wide "
     "with huge section NUMERALS (01 / 02 / 03), lots of whitespace, one idea per screen; real photos "
     "appear as occasional full-width breaks. CARDS: borderless, separated by whitespace and oversized "
     "numerals only. TYPE: ONE powerful grotesk at extreme sizes, near-monochrome. PALETTE: monochrome "
     "(near-black on off-white, or inverse) plus a single restrained accent. MOTION: crisp mask/clip "
     "reveals on the big type."),
    ("Dark Luxe",
     "A dark, premium, moody build. Base = deep charcoal / near-black with a jewel or metallic accent "
     "(brass, gold, emerald or copper as fits the trade), spotlight radial gradients and fine luminous "
     "hairlines. Hero: cinematic, a glowing accent behind an elegant headline, with a real photo "
     "({{IMG_1}}) in a softly-lit frame or as a dim full-bleed backing. NAV: dark glass with a thin "
     "luminous underline. SECTIONS stay dark end-to-end with subtle tonal shifts (never a bright white "
     "flip). CARDS: dark glass with inner glow and a gold hairline; hover lifts and brightens the edge. "
     "TYPE: an elegant serif or high-end display for headings plus a refined sans body, letter-spaced "
     "small-caps labels. MOTION: soft glows, gentle float, a shimmer on accents."),
    ("Warm Organic",
     "Friendly, human, tactile. PALETTE: warm earthy tones (clay, terracotta, sand, sage, cream) — NO "
     "cold blues or greys. Hero: a big ROUNDED photo card ({{IMG_1}}) beside a warm headline, soft organic "
     "blob/wave SVG shapes behind, rounded pill CTAs. NAV: a pill-shaped floating bar with rounded links. "
     "SECTIONS use rounded containers, soft layered shadows, and blob/wave separators instead of straight "
     "lines; imagery sits in rounded frames. CARDS: fully rounded, soft-shadowed, with a gentle hover "
     "bounce. TYPE: a humanist / rounded sans, warm and approachable, medium weights. MOTION: gentle "
     "spring/bounce eases and slowly floating blobs."),
    ("Structured Swiss Grid",
     "Precise, confident, corporate-craft. A visible modular GRID governs everything — a strong baseline, "
     "boxed modules, aligned columns, crisp 1px rules. Hero: a grid layout — headline + CTA in one large "
     "cell beside a 2x2 grid of value-prop / credential cells plus a real photo cell ({{IMG_1}}). NAV: a "
     "structured top bar, logo left, evenly-gridded links, a clear divider. SECTIONS are modular boxes on "
     "a strict grid, credentials-forward (stats, certifications, guarantees in bordered cells). CARDS: "
     "sharp-cornered bordered modules that fill subtly on hover, aligned to the grid. TYPE: a neutral "
     "Swiss grotesk, tight and disciplined. PALETTE: a disciplined two-colour system plus neutrals. "
     "MOTION: precise, snappy, grid cells revealing in sequence."),
    ("Neo-Brutalist Bold",
     "Punchy, confident, modern-brutalist (still premium, never messy). HARD edges everywhere — thick 2-3px "
     "borders, hard OFFSET drop-shadows (no blur), blocky panels that overlap slightly, a visible "
     "structural grid, sticker/tag-style badges. Hero: a big blocky headline in a bordered slab with an "
     "overlapping real photo card ({{IMG_1}}) casting a hard shadow, a chunky filled CTA. NAV: a bordered "
     "bar with boxed, high-contrast links (the active one filled). SECTIONS are bold bordered blocks with "
     "generous size contrast; nothing timid. CARDS: sharp-cornered, thick-bordered, hard-shadowed; hover "
     "shifts the shadow. TYPE: a heavy grotesk paired with a monospace accent for labels/numbers. PALETTE: "
     "a strong duotone plus ONE electric accent. MOTION: snappy, tactile 'press' shifts on hover/reveal."),
    ("Vibrant Gradient Aurora",
     "Bright, energetic, premium-tech. Luminous multi-stop AURORA / mesh gradients as the signature backdrop, "
     "tasteful frosted-glass panels used with intent (this is the archetype's identity, not a lazy default), "
     "gradient TEXT accents, soft glowing orbs and floating elements, rounded-but-crisp shapes. Hero: an "
     "energetic layout with a glowing gradient orb/aurora behind an oversized headline (a gradient accent "
     "word) and a real photo ({{IMG_1}}) in a floating glass frame with a soft glow. NAV: a frosted pill "
     "that brightens on scroll. SECTIONS ride the aurora with generous colour and airy spacing; alternate "
     "bright and deep-tinted bands. CARDS: glossy glass cards with gradient borders and a lift+glow on "
     "hover. TYPE: a clean modern geometric sans with gradient-filled display accents. PALETTE: vivid, "
     "cool-to-warm gradient spectrum tuned to the trade. MOTION: drifting aurora, floating glow, smooth "
     "gradient shifts (reduced-motion safe)."),
)

# Signature OPENER — a second, independent axis so even two builds that land on the same
# archetype still differ in flow: it sets what leads immediately after the hero.
_SIGNATURE_OPENERS: tuple[str, ...] = (
    "Immediately after the hero, lead with a bold STATS / credentials band (years, jobs done, rating, "
    "guarantees) before anything else.",
    "Immediately after the hero, lead with the signature SERVICES showcase (the trade's real services, "
    "richly treated) before anything else.",
    "Immediately after the hero, lead with a large GALLERY / portfolio band of the real photos before "
    "anything else.",
    "Immediately after the hero, lead with the brand STORY / about narrative (who they are, why they're "
    "trusted locally) before anything else.",
    "Immediately after the hero, lead with a TESTIMONIAL / social-proof spotlight before anything else.",
)


def _pick_archetype(seed_key: str, archetype_idx: int | None = None) -> tuple[int, str, str, int, str]:
    """Deterministically map a stable seed string (e.g. dest9 + business name) to ONE layout archetype and
    ONE signature opener. Uses a stable hash (sha256, NOT Python's per-process hash() and NOT random/date)
    so a given business always rebuilds to the SAME identity, while different businesses diverge. Returns
    (archetype_idx, archetype_name, archetype_spec, opener_idx, opener_text).
    archetype_idx (optional) FORCES a specific archetype (ops override for a trade that wants a specific
    look — e.g. Dark Luxe for an auto shop); the signature opener still varies deterministically by seed."""
    import hashlib as _hashlib
    h = int(_hashlib.sha256((seed_key or "seed").encode("utf-8")).hexdigest(), 16)
    a_idx = (archetype_idx % len(_LAYOUT_ARCHETYPES)) if archetype_idx is not None else (h % len(_LAYOUT_ARCHETYPES))
    o_idx = (h // len(_LAYOUT_ARCHETYPES)) % len(_SIGNATURE_OPENERS)
    a_name, a_spec = _LAYOUT_ARCHETYPES[a_idx]
    return a_idx, a_name, a_spec, o_idx, _SIGNATURE_OPENERS[o_idx]


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


def _svg_sized(svg: str, height_px: int = 40) -> str:
    """Cap an inline-SVG logo to a sane header height so it can't render at its default 300x150.
    Injects/merges a `style` on the root <svg>; returns the input unchanged on any mismatch. Never raises."""
    try:
        m = _re.match(r"(<svg\b)([^>]*?)(/?>)", svg, _re.I | _re.S)
        if not m:
            return svg
        head, attrs, close = m.group(1), m.group(2), m.group(3)
        css = f"height:{height_px}px;width:auto;max-width:220px;display:block"
        sm = _re.search(r'style\s*=\s*"([^"]*)"', attrs, _re.I)
        if sm:
            attrs = attrs[:sm.start(1)] + (sm.group(1).rstrip("; ") + ";" + css) + attrs[sm.end(1):]
        else:
            attrs = attrs + f' style="{css}"'
        return head + attrs + close + svg[m.end():]
    except Exception:
        return svg


def _niche_image_queries(settings, company, industry_hint=""):
    """2-3 short photo-search phrases for the business's NICHE (e.g. a pet groomer -> ['pet grooming',
    'dog grooming salon']) so a prospect with NO real photos still gets on-trade imagery instead of empty
    slots. LLM-derived (guarded), with a keyword fallback from the industry/name."""
    import re as _re2
    words = [w for w in _re2.split(r"[^A-Za-z]+", f"{industry_hint} {company}") if len(w) > 3]
    fb = [((words[0] if words else "small") + " business")]
    key = (getattr(settings, "anthropic_api_key", "") or "").strip()
    if not key:
        return fb
    try:
        import anthropic
        import json as _j
        model = getattr(settings, "anthropic_model_cheap", "") or "claude-haiku-4-5-20251001"
        sysp = ("Given an Australian small business, return 2-3 SHORT photo-search phrases that find warm, "
                "on-trade photos of what they do (pet groomer -> ['pet grooming','dog grooming salon','cute "
                "groomed dog']; gardener -> ['garden maintenance','lawn mowing']; cafe -> ['cozy cafe','barista "
                "coffee']). No brand or location words. Output ONLY a JSON array of 2-3 lowercase strings.")
        usr = f"Business: {company}\nIndustry: {industry_hint or '(infer from the name)'}"
        r = anthropic.Anthropic(api_key=key).messages.create(
            model=model, max_tokens=120, system=sysp,
            messages=[{"role": "user", "content": usr}])
        txt = "".join(getattr(b, "text", "") for b in r.content if getattr(b, "type", None) == "text")
        m = _re2.search(r"\[.*\]", txt, _re2.S)
        arr = _j.loads(m.group(0)) if m else []
        qs = [str(s).strip().lower() for s in arr if isinstance(s, str) and 2 < len(str(s).strip()) < 50]
        return qs[:3] or fb
    except Exception:
        return fb


def _fetch_niche_images(queries, want=6, timeout=12.0):
    """On-trade imagery for a prospect with NO real photos: commercial-safe (CC0 / public-domain) images from
    Openverse (keyless), downloaded and returned as data URIs. Guarded — returns [] on any failure, so the
    build is never blocked; the site just falls back to the old CSS-art tiles as before."""
    import base64
    try:
        import httpx
    except Exception:
        return []
    out: list[str] = []
    hdr = {"User-Agent": "TrafficRadius-SiteBuilder/1.0"}
    for q in (queries or []):
        if len(out) >= want:
            break
        try:
            resp = httpx.get("https://api.openverse.org/v1/images/",
                             params={"q": q, "license": "cc0,pdm", "size": "large",
                                     "per_page": max(want, 8), "mature": "false"},
                             timeout=timeout, headers=hdr)
            for it in ((resp.json() or {}).get("results") or []):
                if len(out) >= want:
                    break
                url = it.get("url") or it.get("thumbnail")
                if not url:
                    continue
                try:
                    ir = httpx.get(url, timeout=timeout, follow_redirects=True, headers=hdr)
                except Exception:
                    continue
                ct = (ir.headers.get("content-type") or "").split(";")[0].strip()
                if ir.status_code == 200 and ct.startswith("image/") and 2000 < len(ir.content) < 3_000_000:
                    out.append("data:%s;base64,%s" % (ct, base64.b64encode(ir.content).decode("ascii")))
        except Exception:
            continue
    return out[:want]


def _wrap_bare_image_uris(html: str) -> str:
    """Self-heal a designer slip: {{IMG_n}} tokens placed as BARE TEXT inside a div (instead of an <img>
    src / url(...)) become raw base64 rendered as visible text after substitution (GJ Techtronics case,
    2026-08-28 — 16 leaked runs). Wrap any text-node data URI in a proper <img>. Guarded — input returned
    unchanged on any failure."""
    try:
        import re as _re2
        pat = _re2.compile(r"(>\s*)(data:image/[a-zA-Z0-9.+-]+;base64,[A-Za-z0-9+/=]+)(\s*<)")
        return pat.sub(lambda m: f'{m.group(1)}<img src="{m.group(2)}" alt="" loading="lazy" '
                                 f'decoding="async" style="width:100%;height:100%;object-fit:cover">{m.group(3)}',
                       html)
    except Exception:
        return html


def _recompress_embedded_images(html: str) -> str:
    """Post-process the finished site: re-encode every LARGE embedded raster (>300KB) to progressive JPEG
    q78 (bounded 2000px). The 2000px sharp-photo standard can balloon a 10-photo site past 20MB of data
    URIs (Pathway 22MB case, 2026-08-27) — this keeps full sharpness at hero size while the page stays a
    few MB. Small assets (logos/icons) untouched. Guarded — returns the input unchanged on any failure."""
    try:
        import base64 as _b64, io as _io, re as _re2
        from PIL import Image
        def _one(m):
            b64 = m.group(2)
            try:
                raw = _b64.b64decode(b64 + "=" * (-len(b64) % 4))
                if len(raw) < 300_000:
                    return m.group(0)
                im = Image.open(_io.BytesIO(raw)).convert("RGB")
                im.thumbnail((2000, 2000))
                buf = _io.BytesIO()
                im.save(buf, "JPEG", quality=78, optimize=True, progressive=True)
                out = buf.getvalue()
                if len(out) >= len(raw):
                    return m.group(0)
                return "data:image/jpeg;base64," + _b64.b64encode(out).decode("ascii")
            except Exception:
                return m.group(0)
        return _re2.sub(r"data:image/([a-zA-Z0-9.+-]+);base64,([A-Za-z0-9+/=]+)", _one, html)
    except Exception:
        return html


# Visible-text admissions that we did not have the client's real content. Matched against the page's
# VISIBLE TEXT ONLY — never raw HTML, because "placeholder" is a legitimate CSS pseudo-element
# (::placeholder) and input attribute that appears on perfectly good sites.
_PLACEHOLDER_MARKERS = (
    "example testimonial", "illustrated", "drawn example", "sample image",
    "photo coming", "photos are collected", "more photos from the run",
    "image to come", "lorem ipsum", "placeholder image", "stock placeholder",
)


def _visible_text(html: str) -> str:
    """Just the words a prospect actually reads — styles, scripts and every attribute stripped."""
    h = _re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html or "", flags=_re.S | _re.I)
    h = _re.sub(r"<[^>]+>", " ", h)
    return _re.sub(r"\s+", " ", _html.unescape(h)).lower()


def _reveal_image_problems(html: str, real_logo, images_used: int) -> list:
    """Defects that make a reveal embarrassing on the screen-share. Returns [] when the page is fine.

    NOT a defect: shipping zero photographs. A design-led, type-and-colour site is a legitimate art
    direction — Gully Rigging ships that way and is in Vysakh's own approved reference set.

    IS a defect (both shipped on M & G Prendergast, Vysakh 2026-08-31):
      * the LOGO reused inside a photo frame — their 639x222 gold-on-blue wordmark sat in the hero
        <figure> under alt="...livestock transport working in the Victorian...", a logo posing as a
        photograph of their trucks;
      * ADMITTED placeholders in the visible copy — 21 tiles stamped "Illustrated" beneath the note
        "drawn examples while more photos from the run are collected", plus invented quotes labelled
        "Example testimonial". Telling the owner we had none of their content destroys the reveal.
    """
    probs = []
    try:
        text = _visible_text(html)
        for mark in _PLACEHOLDER_MARKERS:
            if mark in text:
                probs.append(f"placeholder_text_shipped:{mark}")
        uri = (real_logo or {}).get("data_uri") if isinstance(real_logo, dict) else None
        if uri:
            head = uri[:96]
            for tag in _re.findall(r"<img[^>]*>", html or "", _re.I):
                if head and head in tag:
                    m = _re.search(r"""alt\s*=\s*["']([^"']*)""", tag, _re.I)
                    alt = (m.group(1) if m else "").lower().strip()
                    # A logo image SHOULD carry a short brand alt — alt="DeltaCert" or "Feature Fencing"
                    # is correct and must not be flagged. What is wrong is a SCENIC alt: the Prendergast
                    # hero read "MG Prendergast livestock transport working in the Victorian countryside"
                    # over a 639x222 wordmark. A description that long is claiming to be a photograph.
                    if (alt and len(alt.split()) >= 5
                            and not any(w in alt for w in ("logo", "wordmark", "brand", "mark"))):
                        probs.append(f"logo_used_as_photo:alt={alt[:52]}")
        # ONE IMAGE POSING AS SEVERAL DIFFERENT PHOTOS. The strongest signal of all, and it needs no
        # knowledge of which asset is the logo: if the SAME data URI is rendered under two or more
        # DIFFERENT scenic alt texts ("coaching a client through a strength session" / "spotting a client
        # during a heavy set"), the page is presenting one picture as a portfolio.
        seen = {}
        for tag in _re.findall(r"<img[^>]*>", html or "", _re.I):
            u = _re.search(r"""src\s*=\s*["'](data:image/[^"']{40,})""", tag, _re.I)
            a = _re.search(r"""alt\s*=\s*["']([^"']*)""", tag, _re.I)
            if not u:
                continue
            alt = (a.group(1) if a else "").lower().strip()
            if len(alt.split()) >= 4:
                seen.setdefault(u.group(1)[:96], set()).add(alt)
        for uri, alts in seen.items():
            if len(alts) >= 2:
                probs.append(f"one_image_many_scenes:{len(alts)}_alts:{sorted(alts)[0][:44]}")
    except Exception as exc:
        probs.append(f"image_gate_error:{str(exc)[:50]}")
    return probs


def _logo_mark_ok(data_uri: str) -> bool:
    """Validation gate for an EXTRACTED site logo: accept SVG marks and transparent rasters outright; an
    opaque raster must read as a brand card (flat, uniform border) and a sane logo aspect — never a
    PHOTOGRAPH. Guards against extract_logo grabbing a hero/banner photo and shipping it as the header
    logo (Kina Diving diver-photo case, 2026-08-27). On any decode error accept (fail-open: the old
    behaviour) so a weird-but-valid mark is not dropped."""
    try:
        if (data_uri or "").lstrip().lower().startswith("<svg"):
            return True
        import base64 as _b64, io as _io
        from PIL import Image
        b64 = data_uri.split(",", 1)[1]
        im = Image.open(_io.BytesIO(_b64.b64decode(b64 + "=" * (-len(b64) % 4))))
        if im.mode in ("RGBA", "LA", "P"):
            try:
                a = im.convert("RGBA").getchannel("A")
                if (a.getextrema() or (255, 255))[0] < 250:   # real transparency -> a cut-out mark
                    return True
            except Exception:
                pass
        im = im.convert("RGB")
        w, h = im.size
        if (w / max(h, 1)) > 6.5 or (h / max(w, 1)) > 3.0:    # banner-photo / skyscraper aspect -> not a logo
            return False
        px = im.load()
        m = max(2, min(w, h) // 50)
        base = px[m, m]
        def close(a, b): return sum(abs(a[i] - b[i]) for i in range(3)) < 60
        corners = [px[w - 1 - m, m], px[m, h - 1 - m], px[w - 1 - m, h - 1 - m]]
        if not all(close(c, base) for c in corners):
            return False                                       # photographic edges -> not a brand card
        import random as _rnd
        _rnd.seed(7)
        edge = 0
        for _ in range(80):
            if _rnd.random() < 0.5:
                x, y = _rnd.randrange(w), (m if _rnd.random() < 0.5 else h - 1 - m)
            else:
                x, y = (m if _rnd.random() < 0.5 else w - 1 - m), _rnd.randrange(h)
            if close(px[x, y], base):
                edge += 1
        return edge >= 64
    except Exception:
        return True


def _flat_border_graphic(data_uri: str) -> bool:
    """True when an image is a designed GRAPHIC (poster/flyer/services board), not a photograph. Signal =
    colour concentration: flat-fill artwork is dominated by a handful of colours, a photo is continuous
    tone. Measured on the Whyalla posters (.66-.84) vs real trade photos (.36-.41) — top-5 adaptive-palette
    colours covering >=55% of pixels => graphic. Guarded — False on any error."""
    try:
        import base64 as _b64, io as _io
        from PIL import Image
        b64 = data_uri.split(",", 1)[1]
        im = Image.open(_io.BytesIO(_b64.b64decode(b64 + "=" * (-len(b64) % 4)))).convert("RGB")
        im.thumbnail((128, 128))
        q = im.quantize(colors=16)
        hist = sorted(q.histogram(), reverse=True)
        total = sum(hist) or 1
        return (sum(hist[:5]) / total) >= 0.55
    except Exception:
        return False


def _looks_like_logo_photo(data_uri: str) -> bool:
    """True when a GBP photo is actually the business's LOGO/brand card, not a real photo: near-square with a
    flat, uniform background (all four corners ~the same colour covering the edges). No-website prospects
    usually have their only brand mark as a GBP 'photo' — it must go in the header as the REAL logo, never be
    wasted as a blurry hero/gallery image (Whyalla gear+wrench case, 2026-08-27). Guarded — False on any error."""
    try:
        import base64 as _b64, io as _io
        from PIL import Image
        b64 = data_uri.split(",", 1)[1]
        im = Image.open(_io.BytesIO(_b64.b64decode(b64 + "=" * (-len(b64) % 4)))).convert("RGB")
        w, h = im.size
        if not (0.72 <= (w / max(h, 1)) <= 1.38):        # logos/brand cards are near-square
            return False
        px = im.load()
        m = max(2, min(w, h) // 50)
        corners = [px[m, m], px[w - 1 - m, m], px[m, h - 1 - m], px[w - 1 - m, h - 1 - m]]
        base = corners[0]
        def close(a, b): return sum(abs(a[i] - b[i]) for i in range(3)) < 60
        if not all(close(c, base) for c in corners[1:]):
            return False
        # background must dominate the border (sample the frame): a photo has varied edges, a logo card doesn't
        import random as _rnd
        _rnd.seed(7)
        edge = 0
        for _ in range(80):
            if _rnd.random() < 0.5:
                x, y = _rnd.randrange(w), (m if _rnd.random() < 0.5 else h - 1 - m)
            else:
                x, y = (m if _rnd.random() < 0.5 else w - 1 - m), _rnd.randrange(h)
            if close(px[x, y], base):
                edge += 1
        return edge >= 64          # >=80% of the border is the flat background -> a logo card
    except Exception:
        return False


def build_website(pool: ConnectionPool, settings: Settings, dest9: str, *, dry_run: bool = False,
                  archetype_idx: int | None = None) -> dict:
    """AI designer: generate the prospect's website with Claude, store the HTML in lisa4_sites (status
    'built'). Called when a reveal is booked. For critical-issue prospects we feed the scraped content of
    their existing site so the rebuild is faithful. Returns {status, id, bytes} or {error}.

    dry_run=True runs the FULL real build (reads, scrape, archetype pick, Claude, post-process) but makes
    NO writes to lisa4_sites — for safely testing the designer against a live/prod DB without mutating it.
    In dry_run the return also carries {html, archetype, images_used, images_provided}."""
    if not dry_run:
        ensure_lisa4_tables(pool)
    r = _fetch(pool, "SELECT company, domain, bucket, issue FROM lisa4_pool WHERE dest9=%s", (dest9,))
    if r:
        p = dict(r[0])
    else:
        # A booked prospect is REMOVED from lisa4_pool once worked — so its reveal build must NOT fail
        # just because the pool row is gone (this errored real bookings: Foremore, Buraq). Fall back to
        # the booking call's company/domain — that's all the designer needs (industry/location still come
        # from `companies` by phone below). Guarded by an EXISTING lisa4_sites row so this only rescues a
        # genuine Lisa-4 build that was already queued — never a Lisa-5 audit booking (which has no site row).
        has_site = _fetch(pool, "SELECT 1 FROM lisa4_sites WHERE dest9=%s LIMIT 1", (dest9,))
        cr = _fetch(pool, "SELECT company_name, domain FROM lisa_calls WHERE dest9=%s "
                    "AND NULLIF(company_name,'') IS NOT NULL "
                    "ORDER BY (meeting_agreed IS TRUE) DESC, started_at DESC NULLS LAST LIMIT 1", (dest9,))
        if has_site and cr and cr[0].get("company_name"):
            p = {"company": cr[0].get("company_name"), "domain": cr[0].get("domain"), "bucket": None, "issue": None}
            log.info("lisa4_build_pool_fallback", dest9=dest9, company=p["company"])
        else:
            # genuinely nothing to build from (or a non-Lisa-4 booking) → retire any queued row so it can't
            # sit at the head of the queue forever consuming a build slot every pass.
            if not dry_run:
                try:
                    with pool.connection() as conn, conn.cursor() as cur:
                        cur.execute("UPDATE lisa4_sites SET status='error', error='prospect not in lisa4_pool' "
                                    "WHERE dest9=%s AND status IN ('queued','building')", (dest9,))
                        conn.commit()
                except Exception:
                    pass
            return {"error": "prospect not in lisa4_pool"}
    # A 'no_website' prospect must NEVER be built from a scraped domain. Any domain on such a row is a
    # spurious enrichment / name-match leak (e.g. a namesake site like "Steve Campbell Remedial Massage"
    # matched onto "Specialist Massage Centre") — scraping it blends ANOTHER business's brand, services and
    # claims into the reveal. The prospect already told us on the call they have no site, so build fresh from
    # THEIR OWN verified facts (Google Business category + GBP photos), never a scrape of a mis-attributed URL.
    if (p.get("bucket") or "").strip().lower() == "no_website" and p.get("domain"):
        log.info("lisa4_no_website_domain_ignored", dest9=dest9, domain=str(p.get("domain"))[:120])
        p["domain"] = None
        p["issue"] = p.get("issue") or "no website"
    disp = _pick_name(p.get("company"), p.get("title"), p.get("domain")) or p.get("company") or ""
    context = f"Business name: {disp or p.get('company')}\nAustralian business."
    known_industry = ""   # verified Google-Business category (trade) — the anti-guess fallback
    # research brief from everything we know (D&B row / Google-Maps row / phone)
    co = _fetch(pool, "SELECT company_name, industry, sub_industry, suburb, state, gmaps_rating, gmaps_reviews "
                "FROM companies WHERE right(regexp_replace(COALESCE(phone,''),'[^0-9]','','g'),9)=%s "
                "ORDER BY (source='gmaps') DESC, id DESC LIMIT 1", (dest9,))
    if co:
        c0 = dict(co[0])
        if c0.get("industry"):
            known_industry = str(c0["industry"]) + (f" / {c0['sub_industry']}" if c0.get("sub_industry") else "")
            context += (f"\nGoogle Business category (a ROUGH hint only, sometimes wrong/secondary — their "
                        f"live-site content below, when present, is the AUTHORITATIVE trade): {known_industry}")
        if c0.get("suburb") or c0.get("state"):
            context += f"\nLocation: {c0.get('suburb') or ''} {c0.get('state') or ''} — write the service-area section around this."
        if c0.get("gmaps_rating"):
            context += f"\nGoogle rating: {c0['gmaps_rating']}★ ({c0.get('gmaps_reviews') or 0} reviews) — feature this in the trust strip."
    ph = _fetch(pool, "SELECT dest_number FROM lisa4_pool WHERE dest9=%s", (dest9,))
    if ph:
        context += f"\nBusiness phone (use everywhere, tap-to-call): {ph[0]['dest_number']}"
    real_logo = None
    media = None
    have_real_trade = False           # did we capture the prospect's ACTUAL trade (scraped copy or category)?
    real_images: list[str] = []       # prospect's ACTUAL photos (data URIs) → {{IMG_n}} tokens
    real_image_descs: list[str] = []  # one-line description per image, index-aligned with real_images
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
        # REAL CONTENT + PHOTOS: scrape the prospect's live site so the rebuild retains THEIR real
        # services/copy and actual photos (content-parity rule). Fully guarded — never raises.
        try:
            media = scrape_site_media(p["domain"], timeout=15.0)
        except Exception:
            media = None
        # Decide if the scrape actually TELLS us their trade: substantial body copy OR a set of real
        # headings (headings like "Our Services / Landscaping" reveal the trade even when body text is thin).
        _scraped_content = (str(media.get("content")) if (media and media.get("content")) else "").strip()
        _scraped_headings = [h for h in (media.get("headings") or []) if h] if media else []
        _content_usable = bool(media and media.get("found") and (len(_scraped_content) >= 300 or len(_scraped_headings) >= 3))
        if media and media.get("found"):
            if _content_usable:
                _real_block = _scraped_content
                if _scraped_headings:
                    _real_block += ("\nSection headings from their real site (these name their ACTUAL "
                                    "services/trade — build around them): " + " · ".join(_scraped_headings[:40]))
                context += ("\n\nREAL CONTENT FROM THEIR CURRENT SITE (AUTHORITATIVE — this is their ACTUAL "
                            "trade; if the Google category hint above differs, follow THIS). Retain these "
                            "real services/claims and rewrite them BETTER; never invent services they do not "
                            "offer, and never change their industry:\n" + _real_block)
                have_real_trade = True
            for _uri in (media.get("images") or []):
                real_images.append(_uri)
                real_image_descs.append("real content photo from their current website")
        # ANTI-GUESS FALLBACK: the scrape can miss a site (JS-rendered, thin first fetch, WAF). When it does,
        # NEVER let the designer invent an industry from the business name (that shipped a LANDSCAPER as a
        # "building designer"). Lean on the VERIFIED Google Business category instead.
        if not have_real_trade and known_industry:
            context += ("\n\nWe could not fully re-scrape their live site, so use their VERIFIED Google "
                        f"Business category as their ACTUAL trade: {known_industry}. Build the ENTIRE site "
                        "around this real trade's genuine services — do NOT guess a different industry from "
                        "the business name.")
            have_real_trade = True
        # REAL LOGO: pull the prospect's ACTUAL brand mark so the rebuild uses THEIR logo, never an
        # AI-invented one. Guarded — extract_logo never raises; None => unchanged (no placeholder).
        try:
            real_logo = extract_logo(p["domain"], timeout=10.0)
        except Exception:
            real_logo = None
        if not (real_logo and real_logo.get("data_uri")) and media and isinstance(media.get("logo"), dict):
            real_logo = media["logo"]   # reuse the logo scrape_site_media already resolved
        # VALIDATION GATE: an extracted "logo" that is actually a photograph (hero/banner grabbed by the
        # scraper) must never ship as the header mark — drop it and fall back to a styled wordmark.
        if real_logo and real_logo.get("data_uri") and not _logo_mark_ok(real_logo["data_uri"]):
            log.info("lisa4_logo_rejected_not_a_mark", dest9=dest9, source=real_logo.get("source"))
            real_logo = None
        if real_logo and real_logo.get("data_uri"):
            # Classify the mark for VISIBILITY: a header logo pulled from the old site is very often a
            # white/reversed or transparent mark that vanishes on a light rebuilt header. Store the tone
            # so the post-process can wrap it in a guaranteed-contrast backing (and hint the designer).
            try:
                _lt = logo_tone(real_logo["data_uri"])
            except Exception:
                _lt = {"transparent": True, "tone": "unknown"}
            real_logo["tone"] = _lt.get("tone") or "unknown"
            real_logo["transparent"] = bool(_lt.get("transparent"))
            _logo_hint = ""
            if real_logo["transparent"]:
                _logo_hint = (" This logo is a TRANSPARENT / reversed mark (it may be a white or light "
                              "logo built for a dark header) — it arrives already wrapped in its own "
                              "contrast-safe backing so it stays clearly visible on ANY header colour; "
                              "just place the token and keep a plain, clear area around it.")
            context += ("\nREAL LOGO PROVIDED: the business's actual logo is available. Put the EXACT "
                        "placeholder token {{REAL_LOGO}} on its own where the logo goes in BOTH the "
                        "header/nav AND the footer — it is replaced with the finished logo element, so do "
                        "NOT wrap it in your own <img>/<svg>/url(). Keep the header logo clearly VISIBLE at "
                        "40-56px tall — never hidden, zero-height, or on a same-tone background. Do NOT "
                        "draw, recreate, invent, or approximate a logo." + _logo_hint)
            log.info("lisa4_logo_found", dest9=dest9, source=real_logo.get("source"),
                     tone=real_logo["tone"], transparent=real_logo["transparent"],
                     url=str(real_logo.get("url"))[:120])
    elif (p.get("bucket") or "") == "no_website":
        # NO-WEBSITE prospect: the Google Business Profile is the only place their genuine photos live,
        # so pull the real GBP photos for the hero/gallery instead of stock. Fully guarded — never raises.
        try:
            _sub = (dict(co[0]).get("suburb") if co else None)
            _phone = (ph[0]["dest_number"] if ph else None)
            _photos = place_photos(settings, disp or p.get("company") or "", suburb=_sub, phone=_phone)
        except Exception:
            _photos = []
        for _uri in (_photos or []):
            # A GBP "photo" that is actually the brand's LOGO card becomes the site's REAL logo (header/footer)
            # instead of a wasted blurry hero image — for a no-website prospect it's the only brand mark we have.
            if (real_logo is None or not real_logo.get("data_uri")) and _looks_like_logo_photo(_uri):
                real_logo = {"data_uri": _uri, "source": "gbp_photo", "url": ""}
                log.info("lisa4_logo_from_gbp_photo", dest9=dest9)
                continue
            # a flat-background GBP image at non-logo aspect = a POSTER/flyer graphic, not a photo. Blown up
            # as a hero/section background its text turns into noise behind the headline (Whyalla board case)
            # — steer the designer to frame it small instead.
            if _flat_border_graphic(_uri):
                real_images.append(_uri)
                real_image_descs.append("the business's own POSTER/flyer graphic — place ONLY as a small framed "
                                        "gallery/about tile; NEVER as a hero or section background")
                continue
            real_images.append(_uri)
            real_image_descs.append("real Google Business Profile photo of this business")
    # NICHE-IMAGE GUARANTEE (any prospect): if we gathered NO real photos — a no-website prospect with no GBP
    # photos, or a site we couldn't pull usable images from — the report would ship with CSS-art tiles / empty
    # slots and not look like the trade at all (the pet-groomer-with-no-images failure). Source on-trade CC0
    # imagery so every site reads as the right kind of business. Fully guarded — a fetch failure leaves it as
    # before (CSS-art fallback). These are design-concept images for the REVEAL, not claimed as the client's own.
    # TOP-UP (2026-08-27): a sparse real set (1-4 photos) also reads thin/repetitive — supplement up to ~6 with
    # clearly-labelled on-trade design-concept imagery so galleries and photo bands feel rich, never stretched.
    # known_industry is the prospect's VERIFIED trade category — pass it in. Without it the query builder
    # had to guess from the company name alone and produced junk for names that aren't self-describing:
    # "ABSOLUTE FIX-N-FINISH PTY. LTD." -> 'ABSOLUTE business'. Their own category is exactly the
    # "build from the info we DO have" input the no-website rule calls for.
    _niche_hint = known_industry or (p.get("issue") or "")
    if real_images and len(real_images) < 5:
        try:
            _iq = _niche_image_queries(settings, disp or p.get("company") or "", _niche_hint)
            for _uri in _fetch_niche_images(_iq, want=6 - len(real_images)):
                real_images.append(_uri)
                real_image_descs.append("representative on-trade photo of this kind of business (design-concept image)")
        except Exception:
            pass
    if not real_images:
        # This is the LAST line of defence for a no-website prospect: ARM Accountants shipped on
        # 2026-09-02 with images_provided=0 and a dead <img src="">, because this was one shot wrapped
        # in a silent except. A transient Openverse failure must not put an imageless page in front of
        # a prospect — retry, then widen to the plainest trade phrase we can form.
        _tries = []
        try:
            _tries.append(_niche_image_queries(settings, disp or p.get("company") or "", _niche_hint))
        except Exception:
            pass
        _plain = [q for q in ((known_industry or "").split("/")[0].strip().lower(),
                              "australian small business office") if q]
        _tries.append(_plain)
        for _qs in _tries:
            if real_images or not _qs:
                continue
            for _attempt in (1, 2):
                try:
                    for _uri in _fetch_niche_images(_qs, want=6):
                        real_images.append(_uri)
                        real_image_descs.append("representative on-trade photo of this kind of business "
                                                "(design-concept image)")
                except Exception:
                    pass
                if real_images:
                    break
        if not real_images:
            log.warning("lisa4_no_images_available", dest9=dest9,
                        company=str(p.get("company"))[:80], industry=_niche_hint[:60])
    # A verified Google category is their real trade too (has-site OR no-site) — enough to forbid guessing.
    if known_industry:
        have_real_trade = True
    # REAL PHOTOS → {{IMG_n}} tokens: cap the count so the embedded data URIs keep total HTML reasonable.
    # Raised 10 -> 18 so we actually USE the real photos we now scrape (they were being wasted/dropped).
    _MAX_REAL_IMAGES = 18
    if real_images:
        real_images = real_images[:_MAX_REAL_IMAGES]
        real_image_descs = real_image_descs[:_MAX_REAL_IMAGES]
        _img_lines = "\n".join(f"- {{{{IMG_{i + 1}}}}} — {d}"
                               for i, d in enumerate(real_image_descs))
        context += ("\n\nREAL PHOTOS PROVIDED (use the prospect's ACTUAL images — never stock/SVG/CSS-art "
                    f"where a real photo exists). There are {len(real_images)} of them. You MUST place EVERY "
                    "one of these EXACT placeholder tokens — ALL of them — each used exactly ONCE, inside an "
                    "<img> (or as a CSS background-image via inline url(...)), spread across the hero, the "
                    "galleries/portfolio and section/page backgrounds (Home, About and Gallery pages):\n"
                    + _img_lines +
                    "\nDo NOT skip or drop any token, and do NOT invent extra {{IMG_n}} tokens beyond this "
                    "list. Every token is swapped for the real photo.")
    # INDUSTRY ACCURACY: only a genuinely no-website + no-category prospect may infer the trade from the
    # name. Whenever we know the real trade (scraped copy OR verified category) OR the prospect even HAS a
    # live domain, forbid name-guessing — a wrong industry (e.g. a landscaper built as a "building designer")
    # is a client-facing failure.
    if have_real_trade:
        _trade_line = ("Build this business a brand-new website using ONLY the real services / industry "
                       "described above as their trade. Do NOT invent, guess, or infer a different industry "
                       "from the business name — their actual trade is stated above and must be honoured "
                       "exactly.")
    elif p.get("domain"):
        _trade_line = ("Build this business a brand-new website. Their site exists but we could not read "
                       "their trade — keep the industry framing CONSERVATIVE and grounded only in the "
                       "business name + location; do NOT confidently assert a specific specialised industry "
                       "you cannot verify (never mislabel their trade).")
    else:
        _trade_line = ("Build this business a brand-new website. Infer their industry + services carefully "
                       "from the business name and location.")
    # VARIETY: give THIS business a distinct visual identity from a STABLE seed (dest9 + name), so two
    # different businesses never share the same skeleton, yet a rebuild of the SAME business is consistent.
    _seed_key = f"{dest9}|{(disp or p.get('company') or '').strip().lower()}"
    _a_idx, _a_name, _a_spec, _o_idx, _o_text = _pick_archetype(_seed_key, archetype_idx)
    _archetype_block = (
        f"\n\nASSIGNED LAYOUT ARCHETYPE (variety seed {_seed_key!r} → archetype #{_a_idx + 1} of "
        f"{len(_LAYOUT_ARCHETYPES)}): \"{_a_name}\". Commit FULLY to this identity — hero treatment, "
        "navigation style, section arrangement, card/component style, motion signature and TYPE "
        f"PERSONALITY must all follow it. Do NOT default to a generic glass-nav + centred-hero + "
        f"three-glass-card template.\n{_a_spec}\nSIGNATURE OPENER: {_o_text}\nAnother business with a "
        "different archetype must look like a different studio built it.")
    log.info("lisa4_archetype", dest9=dest9, company=(disp or p.get("company")),
             archetype=_a_name, archetype_idx=_a_idx, opener_idx=_o_idx)
    user = (context + _archetype_block + "\n\n" + _trade_line +
            " Make it genuinely impressive so they want to publish it.")
    dmodel = _designer_model(pool, settings)
    akey = _anthropic_key(pool, settings)
    # mark building — REUSE the prospect's existing active row. The partial unique index
    # idx_lisa4_sites_active allows only ONE active (queued/building/built) row per dest9, so a blind INSERT
    # collides with the 'queued' row the booking flow already created — and that INSERT sits OUTSIDE the try
    # below, so the violation aborts the WHOLE build pass every cycle (head-of-line deadlock). Transition the
    # existing queued/building row in place; only if none exists (rebuild over an old 'built') insert fresh.
    if dry_run:
        site_id = 0   # no row is touched in dry_run
    else:
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
        # POST-PROCESS: swap the {{REAL_LOGO}} placeholder for the prospect's ACTUAL logo (data URI in
        # an <img>, or inline SVG markup). Safety net: if the model ignored the token, leave the HTML
        # untouched rather than corrupt it — under-replacing beats breaking markup or faking a logo.
        # VISIBILITY: a mark pulled from the OLD header is often WHITE/reversed/transparent — dropped on a
        # light rebuilt header it goes invisible. When the mark is transparent we wrap it in a contrast
        # chip that TRAVELS with it (a dark chip for a light/unknown mark, a light chip for a dark mark),
        # so it shows on BOTH a light header AND a dark footer whatever background the model chose. An
        # opaque mark carries its own rectangle → placed bare. Guarantee holds regardless of the model.
        if real_logo and real_logo.get("data_uri") and "{{REAL_LOGO}}" in html:
            _lg = real_logo["data_uri"]
            _is_svg = _lg.lstrip().lower().startswith("<svg")
            _transparent = bool(real_logo.get("transparent")) or _is_svg
            _tone = real_logo.get("tone") or "unknown"
            _alt = (p.get("company") or "").replace('"', "").replace("<", "").replace(">", "")
            if _is_svg:
                _mark = _svg_sized(_lg, 40)
            else:
                _mark = (f'<img src="{_lg}" alt="{_alt} logo" loading="eager" decoding="async" '
                         f'style="max-height:40px;width:auto;display:block;">')
            if _transparent:
                if _tone == "dark":   # dark ink → a LIGHT chip keeps it visible (incl. on a dark footer)
                    _chip = ("display:inline-flex;align-items:center;background:#ffffff;color:#111111;"
                             "padding:6px 12px;border-radius:10px;border:1px solid rgba(0,0,0,.08);"
                             "box-shadow:0 1px 4px rgba(0,0,0,.12);line-height:0;")
                else:                 # light/unknown ink → a DARK chip (fixes white-on-light-header)
                    _chip = ("display:inline-flex;align-items:center;background:#0f172a;color:#ffffff;"
                             "padding:6px 12px;border-radius:10px;border:1px solid rgba(255,255,255,.10);"
                             "line-height:0;")
                _repl = f'<span class="site-logo" style="{_chip}">{_mark}</span>'
            else:                     # opaque mark — its own background carries it, place bare
                _repl = (f'<img src="{_lg}" alt="{_alt} logo" class="site-logo" loading="eager" '
                         f'decoding="async" style="max-height:44px;width:auto;display:inline-block;">')
            html = html.replace("{{REAL_LOGO}}", _repl)
        # SAFETY: never ship a raw {{REAL_LOGO}} token. The model sometimes emits it even when NO real logo
        # was provided (nothing replaced it above) — swap any survivor for a styled NAME wordmark so the
        # header/footer show the brand, never literal placeholder text.
        if "{{REAL_LOGO}}" in html:
            _wm = (disp or p.get("company") or "").replace('"', "").replace("<", "").replace(">", "").strip() or "Home"
            _wordmark = (f'<span class="site-logo" style="font-weight:800;font-size:1.15rem;'
                         f'letter-spacing:.01em;line-height:1;white-space:nowrap;">{_wm}</span>')
            html = html.replace("{{REAL_LOGO}}", _wordmark)
        # POST-PROCESS real photos: swap each {{IMG_n}} token for its data URI. A real photo the model did
        # NOT place is NOT discarded (that silently deleted the prospect's genuine photos) — it is collected
        # and appended into a real gallery below, so every scraped photo actually ships.
        _unused_imgs: list[str] = []
        for _i, _iuri in enumerate(real_images, start=1):
            if not _iuri:
                continue
            _tok = "{{IMG_%d}}" % _i
            if _tok in html:
                html = html.replace(_tok, _iuri)
            else:
                _unused_imgs.append(_iuri)
        # Any token the model INVENTED beyond our real set has no photo behind it — strip those cleanly so
        # no raw {{IMG_n}} placeholder text ever ships.
        html = _re.sub(r"\{\{IMG_\d+\}\}", "", html)
        # ...but stripping the token out of `<img src="{{IMG_7}}">` leaves `<img src="">`, which renders as
        # a broken-image box on the prospect's screen-share. THIS is what shipped Bodyoncall's dead hero and
        # ARM Accountants' imageless page (2026-09-02). Drop any <img> left with an empty/# src, and clear
        # the same case in inline background-image:url() so no empty frame is left behind either.
        _dead = len(_re.findall(r"""<img\b[^>]*\bsrc\s*=\s*["'](?:\s*|#)["'][^>]*>""", html, _re.I))
        if _dead:
            html = _re.sub(r"""<img\b[^>]*\bsrc\s*=\s*["'](?:\s*|#)["'][^>]*>""", "", html, flags=_re.I)
            html = _re.sub(r"""background(-image)?\s*:\s*url\(\s*["']?\s*["']?\s*\)\s*;?""", "", html, flags=_re.I)
            log.warning("lisa4_dead_img_stripped", dest9=dest9, count=_dead,
                        company=str(p.get("company"))[:80])
        # GUARANTEE no real photo is lost: append any unplaced photos as a genuine <img> gallery. Self-
        # contained inline styles so it renders regardless of the site's CSS; injected just before </body>.
        if _unused_imgs:
            _galt = (p.get("company") or disp or "our work").replace('"', "").replace("<", "").replace(">", "")
            _tiles = "".join(
                f'<img src="{_u}" alt="{_galt}" loading="lazy" decoding="async" '
                'style="width:100%;height:260px;object-fit:cover;border-radius:14px;display:block;'
                'box-shadow:0 8px 30px rgba(0,0,0,.12);">'
                for _u in _unused_imgs)
            _gallery = (
                '<section aria-label="Gallery" style="padding:72px 6vw;">'
                '<div style="max-width:1200px;margin:0 auto;">'
                '<div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));'
                'gap:16px;">' + _tiles + '</div></div></section>')
            _low = html.lower()
            _pos = _low.rfind("</body>")
            if _pos == -1:
                _pos = _low.rfind("</html>")
            html = (html[:_pos] + _gallery + html[_pos:]) if _pos != -1 else (html + _gallery)
        _images_used = sum(1 for _u in real_images if _u and (_u in html))
        # REVEAL IMAGE GATE — a reveal is shown to the owner as "your new website". Two things are
        # indefensible in that moment and both shipped on M & G Prendergast (Vysakh, 2026-08-31):
        # their LOGO stretched into the hero PHOTO frame with alt text claiming it was a photo of their
        # trucks, and a gallery of 21 CSS-drawn tiles each stamped "Illustrated" under a note reading
        # "drawn examples while more photos are collected", plus invented "Example testimonial" quotes.
        # A photo-less but design-led site is fine (Gully Rigging shipped that way and was approved) —
        # ADMITTED PLACEHOLDERS and a logo posing as a photograph are not.
        _img_problems = _reveal_image_problems(html, real_logo, _images_used)
        if _img_problems:
            log.warning("lisa4_site_image_gate", dest9=dest9, company=p.get("company"),
                        problems=_img_problems[:6], images_used=_images_used)
        # self-heal bare-text data URIs (a token outside <img>/url() renders as visible base64 text)
        html = _wrap_bare_image_uris(html)
        # keep the page a few MB: sharp 2000px embeds re-encoded to progressive JPEG (Pathway 22MB case)
        html = _recompress_embedded_images(html)
        if dry_run:
            # No DB write in dry_run — hand back the finished HTML + the metrics a test needs.
            log.info("lisa4_site_dryrun_built", dest9=dest9, company=p.get("company"), bytes=len(html),
                     archetype=_a_name, images_used=_images_used, images_provided=len(real_images))
            return {"status": "built-dryrun", "id": site_id, "bytes": len(html), "html": html,
                    "archetype": _a_name, "archetype_idx": _a_idx, "signature_opener_idx": _o_idx,
                    "images_used": _images_used, "images_provided": len(real_images),
                    "have_real_trade": have_real_trade, "known_industry": known_industry}
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
                                "share_token=COALESCE(share_token,%s), kind=COALESCE(kind,'reveal'), "
                                "qa_passed=%s, qa_notes=%s, qa_at=now() WHERE id=%s",
                                (html, _tok, not _img_problems,
                                 ("image gate: " + "; ".join(_img_problems))[:600] if _img_problems else None,
                                 site_id))
                    conn.commit()
                break
            except Exception as _exc:
                log.warning("lisa4_site_save_retry", attempt=_attempt, error=str(_exc)[:100])
                _time.sleep(3)
        else:
            raise RuntimeError("site save failed after retries")
        log.info("lisa4_site_built", dest9=dest9, company=p.get("company"), bytes=len(html),
                 archetype=_a_name, images_used=_images_used, images_provided=len(real_images))
        return {"status": "built", "id": site_id, "bytes": len(html), "archetype": _a_name}
    except Exception as exc:
        if dry_run:
            raise   # surface the real error to the test; no prod row to retire
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
    # QA G8/G9 (PURE in-memory — dict/regex only, ZERO I/O): fill any known blank/missing variable so
    # Retell can never voice a raw "{{placeholder}}", scrub any residual {{...}} inside a value, then
    # enforce the no-website invariant (a site-less prospect never carries a website URL/domain). This
    # only shapes the variables dict handed to Retell — it never changes how the call is placed/paced.
    dyn = _L1._fill_dynamic_var_defaults(dyn)
    dyn = _qa_dyn.scrub_residual_placeholders(dyn)
    dyn = _qa_dyn.enforce_no_website_invariant(dyn)
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
        "SELECT lp.dest9, lp.dest_number, lp.company, lp.domain, lp.bucket, lp.issue, lp.title FROM lisa4_pool lp "
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
    # POOL HYGIENE (Fix #2) — HARD-suppress only clearly-dead data (non-mobile, or a number a prior call
    # already proved wrong/dead or that opted out), so the queue is never fed a structurally unconvertible
    # prospect. Deliberately NARROW: this removes very few rows; everything else stays dialable.
    if rows:
        _before = len(rows)
        rows = [r for r in rows if not _lisa4_hard_suppress(r)]
        if len(rows) < _before:
            log.info("lisa4_schedule_hard_suppressed", n=_before - len(rows))
    # HARD-suppress do-not-contact numbers (rude / asked to be removed on ANY prior call — human or AI).
    if rows:
        _dnc = {x["d9"] for x in _fetch(pool,
            "SELECT DISTINCT right(regexp_replace(COALESCE(c.dest_number,''),'[^0-9]','','g'),9) d9 "
            "FROM classifications cl JOIN calls c ON c.call_id=cl.call_id "
            "WHERE cl.do_not_contact IS TRUE "
            "  AND right(regexp_replace(COALESCE(c.dest_number,''),'[^0-9]','','g'),9) = ANY(%s)",
            ([r["dest9"] for r in rows],))}
        if _dnc:
            rows = [r for r in rows if r["dest9"] not in _dnc]
            log.info("lisa4_schedule_dnc_suppressed", n=len(_dnc))
    # SOFT-deprioritize (ordering ONLY — never removes a prospect, so the queue can't run dry): dial the
    # strongest prospects first (real speakable site issue + speakable owner/trading name) and push
    # weak-but-dialable ones (has-site with no real issue yet, coming-soon, likely-ESL, no speakable
    # owner) to the back. Python's sort is STABLE, so the SQL priority/bucket/reserved_at order is kept
    # WITHIN each rank.
    rows.sort(key=_lisa4_soft_rank)
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
        # Fix #1: fill REAL live-site issues for has-site prospects (off the dial loop) so the personal
        # "we looked at YOUR site" hook is populated ahead of the dialer reaching them.
        out["issues_enriched"] = enrich_lisa4_pool_issues(pool, settings).get("enriched", 0)
    except Exception as exc:
        log.warning("lisa4_issue_enrich_failed", error=str(exc)[:140])
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
    # below only picks 'queued', so re-queue any build stranded in 'building' with no HTML. Threshold is 35 min
    # (was 20): a COMPLEX site legitimately takes 15-20 min, and a 20-min reaper was killing those RIGHT before
    # they saved — burning attempts and wedging the build in the attempts-cap limbo (Vysakh's stuck reveals).
    try:
        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute("UPDATE lisa4_sites SET status='queued' WHERE status='building' AND html IS NULL "
                        "AND COALESCE(building_at, created_at) < now() - interval '35 minutes'")
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
                try:   # old-vs-new comparison (needs Chromium in the image; guarded / degrades gracefully).
                       # force=True so a REBUILD/correction re-screenshots the CURRENT site — without it the
                       # comparison keeps the STALE shots from the first build (the wrong-trade / failed-load
                       # bug: e.g. a psychologist reveal still showing a carpenter/loading screenshot).
                    from . import comparison as _cmp
                    _cmp.ensure_comparison(pool, settings, r["dest9"], force=True)
                except Exception:
                    pass
                try:   # Lisa shares the brand intro (post-booking, once) — but NEVER overnight (AU daytime only)
                    from . import noshow_recovery as _nsr
                    if _nsr.send_window_open():
                        _crm.send_brand_intro(pool, settings, r["dest9"], by="Lisa")
                except Exception:
                    pass
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
    meta = call.get("metadata") or {}
    inbound = (call.get("direction") or "").lower() == "inbound"
    # inbound = a prospect returning Lisa's missed call/SMS: THEY are from_number, our line is to_number
    d9 = _L1._d9(call.get("from_number") if inbound else (meta.get("dest9") or call.get("to_number")))
    if inbound and cid:
        # inbound caller has no brief → recover the company (+domain) from our own data by dest9 so the
        # booking is never a nameless '?' in the CRM / OTHER bucket.
        _co, _dom = _L1.inbound_company(pool, d9, (call.get("retell_llm_dynamic_variables") or {}).get("company_name"))
        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute("INSERT INTO lisa_calls (call_id, dest9, to_number, from_number, company_name, domain, "
                        "  status, brief) VALUES (%s,%s,%s,%s,%s,%s,'ongoing','{}') "
                        "ON CONFLICT (call_id) DO NOTHING",
                        (cid, d9, call.get("to_number"), call.get("from_number"), _co, _dom))
            conn.commit()
    # SOLE SOURCE OF TRUTH = OUR transcript classifier (never Retell's custom_analysis_data). It runs
    # post-call (off the dial loop) and is idempotent per call_id; {} when there is no transcript so the
    # never-connected telephony-only handling below still fires.
    dyn4 = call.get("retell_llm_dynamic_variables") or {}
    cad = _L1._lisa_postcall_cad(pool, settings, call, cid)
    outcome = (cad.get("call_outcome") or "").strip().lower()
    booked = bool(cad.get("meeting_agreed"))
    # QA G1 (same gate Lisa-5 has had; Lisa-4 was missing it — 15s fragment booked Lekcom 2026-08-28):
    # honour a "booked" only for a genuine two-party conversation (disconnect + duration) with a concrete
    # agreed time in the transcript. A gate-fail un-books so CRM/calendar/SMS/funnel all agree.
    if booked:
        from .qa import gates as _qg, audit as _qa
        _v = _qg.booking_verdict(
            transcript=call.get("transcript"), disconnect_reason=call.get("disconnection_reason"),
            duration_ms=call.get("duration_ms") or call.get("call_length_ms"),
            claimed_time=cad.get("agreed_day_time"))
        if not _v.ok:
            booked = False
            try:
                _qa.log_event(pool, gate="G1", kind="unbooked", call_id=cid, agent="Lisa 4",
                              detail={"reason": _v.reason, "classifier_meeting_agreed": True,
                                      "disconnect": call.get("disconnection_reason"),
                                      "duration_ms": call.get("duration_ms")})
            except Exception:
                pass
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
                               bde_ext="LISA4", bde_name="Lisa 4", booked_override=booked)
    except Exception as exc:
        log.warning("lisa4_funnel_write_failed", error=str(exc)[:140])
    # BOOKED REVEAL → queue the AI designer to build the site (idempotent: skip if one already queued/built)
    if booked and d9:
        # A confirmed booking must ALSO land in the booked CRM (not only the funnel + calendar). Contact/time
        # all come from OUR classifier / the dialed brief, never Retell. Idempotent + fill-blank.
        _L1._crm_mark_booked(pool, d9, contact_name=(dyn4.get("prospect_name") or cad.get("contact_name")),
                             contact_email=cad.get("confirmed_email"), agreed_day_time=cad.get("agreed_day_time"),
                             next_action_at=_L1._parse_when(cad.get("agreed_day_time")), agent="Lisa 4")
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
                        "I'll send the meeting invite through just before we jump on. Cheers!")
                if frm and _L1._send_sms_twilio(settings, pn, body, frm, pool):
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
                    if frm and _L1._send_sms_twilio(settings, dest_number, body, frm, pool):
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
# 468091513 CHANGED HANDS (confirmed from dialed-pool data, 2026-08-30): Lisa-4's rotation until
# 2026-08-17, Lisa-5's from 2026-08-18 (149-150 D&B dials/day since). The GUARD registries above keep it
# in BOTH sets (append-only, self-dial safety); ATTRIBUTION is made date-aware here instead so each row
# lands on the agent who actually made/took it. Bookings Aug-24/26 on this line were Lisa-5's.
_N091513_SWITCH = "'2026-08-18 00:00:00+10'"
_ON_091513 = "(COALESCE(from_number,'') ~ '468091513' OR COALESCE(to_number,'') ~ '468091513')"
_L4_PRED = ("((COALESCE(from_number,'') ~ %s "
            "OR COALESCE(to_number,'') ~ %s) "
            f"AND NOT ({_ON_091513} AND created_at >= {_N091513_SWITCH}))")
# Lisa-5 line registry (append-only, same either-leg rule as Lisa-4). Lisa-5 got her own caller IDs
# AFTER the 2-agent partition above was written, so her legs were falling into the Lisa-1 bucket and
# her calls/inbound callbacks showed up on the OFF Lisa-1 card (Raj flagged 2026-08-14). Lisa-1 must be
# EVERYTHING that is neither Lisa-4 nor Lisa-5.
L5_LINE_DIGITS_ALL = ("468096730", "468008827")
L5_LINE_RX = "(" + "|".join(L5_LINE_DIGITS_ALL) + ")"
_L5_PRED = ("((COALESCE(from_number,'') ~ %s "
            "OR COALESCE(to_number,'') ~ %s) "
            f"OR ({_ON_091513} AND created_at >= {_N091513_SWITCH}))")

# Lines that were ONLY ever Lisa-4's (091513 excluded — it changed hands, see above).
_L4_OWN_RX = "(" + "|".join(d for d in L4_LINE_DIGITS_ALL if d != "468091513") + ")"


def line_sql(agent: str, *, frm: str = "from_number", to: str = "to_number",
             created: str = "created_at") -> str:
    """The ONE line→agent attribution rule, as literal parameter-free SQL any module can drop into a query.

    WHY THIS EXISTS: the rule is subtle — 468091513 was Lisa-4's rotation until 2026-08-17 and Lisa-5's
    from 2026-08-18 — and it had been hand-copied into several modules. One copy (crm.py's booking-ASSET
    builder) never got the date cutoff, so a Lisa-5 booking on that line was routed to Lisa-4's deliverable:
    the CRM correctly LABELLED it 'Lisa 5' while the builder queued a WEBSITE instead of the growth AUDIT,
    and the prospect page showed both at once (Vysakh: HILLSYDE NOMINEES, 2026-08-31 — 5 bookings affected,
    each left without the audit its closer needed). Never re-type the digits; import this.

    `frm`/`to`/`created` let a caller pass table-qualified columns (e.g. frm='c.from_number').
    """
    f, t, c = frm, to, created
    on13 = f"(COALESCE({f},'') ~ '468091513' OR COALESCE({t},'') ~ '468091513')"
    if agent == "lisa4":
        return (f"((COALESCE({f},'') ~ '{_L4_OWN_RX}' OR COALESCE({t},'') ~ '{_L4_OWN_RX}')"
                f" OR ({on13} AND {c} < {_N091513_SWITCH}))")
    if agent == "lisa5":
        return (f"((COALESCE({f},'') ~ '{L5_LINE_RX}' OR COALESCE({t},'') ~ '{L5_LINE_RX}')"
                f" OR ({on13} AND {c} >= {_N091513_SWITCH}))")
    if agent == "lisa1":                      # Lisa-1 = everything that is neither
        return f"(NOT {line_sql('lisa4', frm=f, to=t, created=c)} "\
               f"AND NOT {line_sql('lisa5', frm=f, to=t, created=c)})"
    raise ValueError(f"unknown agent {agent!r}")


def _agent_today(pool: ConnectionPool, tz: str, *, lisa4: bool, out_numbers: list[str],
                 start: str | None = None, end: str | None = None) -> dict:
    """Today's numbers for one agent under the either-leg attribution rule.
    'calls' = OUTBOUND legs only (from_number is the line's own caller ID); convos/booked/
    callbacks/cost count BOTH directions — an inbound call-back that books (prospect is
    from_number) belongs to the line it called, otherwise inbound bookings vanish from the
    rep card (2-of-4 bug 2026-08-06; Samantha inbound-booking bug 2026-08-12)."""
    # Lisa-1 = neither Lisa-4 NOR Lisa-5 (else Lisa-5's legs leak onto the OFF Lisa-1 card).
    attr = _L4_PRED if lisa4 else f"(NOT {_L4_PRED} AND NOT {_L5_PRED})"
    if lisa4:
        out_cond = ("(COALESCE(from_number,'') ~ %s AND NOT (COALESCE(from_number,'') ~ '468091513' "
                    f"AND created_at >= {_N091513_SWITCH}))")
        out_param = L4_LINE_RX
        attr_params = (L4_LINE_RX, L4_LINE_RX)
    else:
        out_cond, out_param = "from_number = ANY(%s)", list(out_numbers or [])
        attr_params = (L4_LINE_RX, L4_LINE_RX, L5_LINE_RX, L5_LINE_RX)
    r = _fetch(pool,
        f"SELECT count(*) FILTER (WHERE {out_cond}) calls, "
        # CONVOS: count CONNECTED calls (>=20s) immediately, same as the Lisa-5 card — NOT call_outcome,
        # which is set by the classifier and LAGS (so Lisa-4 wrongly showed 0 convos mid-day until calls were
        # classified, while Lisa-5's duration-based count already showed 20). meeting_agreed kept as a belt.
        "  count(*) FILTER (WHERE COALESCE(duration_ms,0)/1000 >= 20 OR meeting_agreed) convos, "
        "  count(*) FILTER (WHERE meeting_agreed) booked, "
        "  count(*) FILTER (WHERE call_outcome='callback_requested') callbacks, "
        "  COALESCE(sum(cost_cents),0) cost_cents "
        f"FROM lisa_calls WHERE {attr} "
        "  AND (created_at AT TIME ZONE %s)::date BETWEEN "
        "      COALESCE(%s::date,(now() AT TIME ZONE %s)::date) AND COALESCE(%s::date,(now() AT TIME ZONE %s)::date)",
        (out_param, *attr_params, tz, start, tz, end, tz))[0]
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


def floor_snapshot(pool: ConnectionPool, settings: Settings, range_key: str = "today") -> dict:
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
    # Date-range filter for the Outbound-Intelligence cards + overall strip: today | yesterday | 3d | 7d | 30d.
    from datetime import timedelta as _td
    _today = now.date()
    _RANGES = {"today": (_today, _today), "yesterday": (_today - _td(days=1), _today - _td(days=1)),
               "3d": (_today - _td(days=2), _today), "7d": (_today - _td(days=6), _today),
               "30d": (_today - _td(days=29), _today)}
    _sd, _ed = _RANGES.get(str(range_key or "today"), _RANGES["today"])
    r_start, r_end, r_days = _sd.isoformat(), _ed.isoformat(), (_ed - _sd).days + 1
    is_today = (str(range_key or "today") == "today")
    wstart = int(getattr(settings, "lisa_call_window_start", 9))
    wsmin = int(getattr(settings, "lisa_call_window_start_min", 0))
    wend = int(getattr(settings, "lisa_call_window_end", 17))
    in_window = now.weekday() < 5 and (wstart * 60 + wsmin) <= now.hour * 60 + now.minute < wend * 60

    # Lisa 1
    hb = _fetch(pool, "SELECT round(extract(epoch FROM (now()-hos_heavy_at))) s FROM lisa_control WHERE id=1")
    l1 = {
        "key": "lisa1", "name": "Lisa 1", "role": "Appointment setter", "campaign": "GAds prospects · $2-50M",
        "numbers": n1, "autodial": _L1.get_autodial_state(pool, settings), "in_window": in_window,
        "today": _agent_today(pool, tz, lisa4=False, out_numbers=n1, start=r_start, end=r_end),
        "inflight": _agent_inflight(pool, lisa4=False) if is_today else None,
        "pool": _fetch(pool, "SELECT count(*) n FROM lisa_pool")[0]["n"],
        "queue_due": _fetch(pool, "SELECT count(*) n FROM calendar_events WHERE bde_name='Lisa' AND status='pending' "
                            "AND type IN ('fresh_call','retry','callback','reached_call') AND start_at<=now()")[0]["n"],
        "target": int(getattr(settings, "lisa_daily_target", 300)) * r_days,
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
        "today": _agent_today(pool, tz, lisa4=True, out_numbers=n4, start=r_start, end=r_end),
        "inflight": _agent_inflight(pool, lisa4=True) if is_today else None,
        "pool": _fetch(pool, "SELECT count(*) n FROM lisa4_pool")[0]["n"],
        "queue_due": _fetch(pool, "SELECT count(*) n FROM calendar_events WHERE bde_name='Lisa4' AND status='pending' "
                            "AND type='fresh_call' AND start_at<=now()")[0]["n"],
        "target": int(getattr(settings, "lisa4_daily_target", 200)) * r_days,
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
            "  AND (created_at AT TIME ZONE %s)::date BETWEEN %s::date AND %s::date", (n5d, n5d, n5d, tz, r_start, r_end))[0]
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
        "target": int(getattr(settings, "lisa5_daily_target", 200)) * r_days,
    }
    floor = {k: (l1["today"].get(k, 0) or 0) + (l4["today"].get(k, 0) or 0) + (l5["today"].get(k, 0) or 0)
             for k in ("calls", "convos", "booked", "callbacks", "cost_cents")}
    return {"agents": [l1, l4, l5], "floor_today": floor, "in_window": in_window,
            "window": f"{wstart:02d}:{wsmin:02d}–{wend:02d}:00 Mon–Fri",
            "range": str(range_key or "today"), "range_days": r_days,
            "range_start": r_start, "range_end": r_end,
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
