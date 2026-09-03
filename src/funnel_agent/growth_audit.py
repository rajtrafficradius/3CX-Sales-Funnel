"""GROWTH AUDIT — the premium "Confidential Growth Audit" (DE Group Growth Intelligence).

ONE generator, the design Vysakh approved (the cstda green editorial look), with the UNION of every section
from the three sources he asked us to merge:
  • the liked cstda audit  — cover, executive summary, methodology, DEEP FINDINGS (visual severity cards),
    competitor benchmark + share-of-visibility, revenue opportunity, 90-day plan, next step;
  • the structured audit    — health-score gauge, where-you-rank, money-searches, search-area clusters,
    Google Ads, Google/AI readiness;
  • the full audit engine    — keyword universe + marketing funnel, competitor scoreboard, content gaps,
    backlinks/authority, the grounded revenue maths.

Design = warm paper + forest/gold/rust, serif headings + Inter body (system fonts only — no webfonts).
Findings are VISUAL cards (numbered chip · severity tag · "why it costs you / the fix" split · evidence line),
kept short. Every figure is GROUNDED in assemble_audit — the LLM writes prose only and is forbidden from
emitting numbers; a guard discards any narrative string whose numbers aren't in the data. Fully guarded:
returns None on any failure; each data section is gated and omitted when its data is empty.
"""
import html as _h
import re as _re
import json as _json
from .reveal_guide import _claude_text

try:
    from .logging import get_logger
    _log = get_logger("funnel_agent.growth_audit")
except Exception:   # logging must never break audit generation
    class _Noop:
        def __getattr__(self, _):
            def _f(*a, **k):
                return None
            return _f
    _log = _Noop()


# --------------------------------------------------------------------------- helpers ---
def _n(v) -> str:
    try:
        return format(int(round(float(v))), ",")
    except Exception:
        return "0"


def _fmt_money(v) -> str:
    try:
        return "A$" + format(int(round(float(v))), ",")
    except Exception:
        return ""


def _e(s):
    return _h.escape(str(s if s is not None else ""))


def _mono(s):
    return f'<span class="mono">{_e(s)}</span>'


def _band(score):
    """(word, hex) for a 0-100 score — severity-mapped to the cstda palette."""
    try:
        s = float(score)
    except Exception:
        s = 0
    if s >= 75:
        return ("Strong", "#2f7d52")       # green
    if s >= 55:
        return ("Solid", "#2f6b52")         # forest-2
    if s >= 40:
        return ("Developing", "#c8802a")    # amber
    if s >= 30:
        return ("Emerging", "#c8802a")      # amber
    return ("At risk", "#a6432c")           # rust


_LEGAL = _re.compile(r"\b(the trustee for|pty\.?\s*ltd\.?|p/?l|ltd\.?|proprietary|unit trust|family trust|"
                     r"trust|t/?as|trading as|holdings|enterprises)\b", _re.I)


_TRADING_AS = _re.compile(r"\b(?:t/?as|trading as)\s+(.+)$", _re.I)


def _strip_legal_boilerplate(raw: str) -> str:
    """Reduce a legal / trust entity name to its recognizable core so an audit shows a REAL name, never a
    placeholder. 'The trustee for The Hutchinson Family Trust & The trustee for The Page Family Trust'
    -> 'Hutchinson Family Trust'; 'Smith Pty Ltd' -> 'Smith'; "X Co t/as Joe's Plumbing" -> "Joe's Plumbing"."""
    s = (raw or "").strip()
    if not s:
        return ""
    m = _TRADING_AS.search(s)                       # 'trading as NAME' -> the real trading name
    if m and m.group(1).strip():
        return m.group(1).strip(" .,&-")
    # multiple 'trustee for ... trust' clauses joined by & / and -> keep only the FIRST entity
    s = _re.split(r"\s*(?:&|/|,|\band\b)\s+the trustee for\b", s, flags=_re.I)[0]
    s = _re.sub(r"^\s*(the\s+)?(as\s+)?(trustee for|a\.?t\.?f\.?)\s+", "", s, flags=_re.I)  # drop 'the trustee for'
    s = _re.sub(r"^\s*the\s+", "", s, flags=_re.I)                                          # drop leading 'The'
    s = _re.sub(r"\s*\b(pty\.?\s*ltd\.?|p/?l|proprietary( limited)?|ltd\.?|limited|inc(orporated)?\.?)\.?\s*$",
                "", s, flags=_re.I)                                                          # drop trailing co. suffix
    # drop a trailing legal wrapper ('... Unit Trust' -> '...', 'Hutchinson Family Trust' -> 'Hutchinson'),
    # but only when a distinctive name remains in front of it.
    _stripped = _re.sub(r"\s*\b((unit|family|discretionary|hybrid)\s+)?trust\b\.?\s*$", "", s, flags=_re.I).strip(" .,&-")
    if _stripped and any(len(t) >= 3 for t in _stripped.split()):
        s = _stripped
    return s.strip(" .,&-")


def _looks_placeholder(s: str) -> bool:
    """True for a domain-derived name that reads like a blank/placeholder — e.g. 'Scale X' (a bare single-letter
    token) or a stub under 3 chars. Such a string must NEVER be shown as the business name on an audit."""
    toks = [t for t in (s or "").split() if t]
    return (not toks) or any(len(t) == 1 for t in toks) or len(s.replace(" ", "")) < 3


def _clean_name(name: str, company: str, domain: str) -> str:
    # 1) a clean, non-legal trading name always wins
    for cand in (company, name):
        c = (cand or "").strip()
        if c and not _LEGAL.search(c) and c.lower() not in ("none", ""):
            return c
    # 2) the recognizable core of the legal/trust name — this is the REAL brand and beats a domain run-on:
    #    'KINA DIVING PTY LTD' -> 'Kina Diving' (not the domain 'kinacommercialdiving'); the trustee case
    #    -> 'Hutchinson Family Trust'. Require a token with real letters so stubs ('A1 Pty Ltd') fall through.
    for cand in (company, name):
        core = _strip_legal_boilerplate(cand)
        if core and len(core) >= 3 and any(len(t) >= 3 for t in core.split()):
            return core.title() if (core.islower() or core.isupper()) else core
    # 3) a descriptive domain brand — only if it doesn't read like a placeholder ('scale-x' -> 'Scale X')
    base = (domain or "").split("/")[0].split(".")[0].replace("-", " ").strip()
    if base and not _looks_placeholder(base):
        return base.title()
    return (company or name or "your business")


def _nums_in(text) -> set:
    """All numbers in a piece of text, NORMALISED to comma-free digit strings so a grounded '2,900' in the
    brief matches a '2900' (or '2,900') in prose. Trailing JSON separators never leak into a token."""
    return {tok.replace(",", "") for tok in _re.findall(r"\d[\d,]*\d|\d", str(text or "")) if tok.replace(",", "")}


def _allowed_nums(brief: dict) -> set:
    return _nums_in(_json.dumps(brief, default=str))


def _clean_prose(text, allowed: set, fallback: str = "") -> str:
    """Anti-fabrication guard: the LLM must not invent numbers. If a returned prose string contains any number
    token NOT present in the grounding brief, we discard it and fall back (numbers are injected deterministically)."""
    t = (text or "").strip()
    if not t:
        return fallback
    bad = {tok for tok in _nums_in(t) if tok not in allowed and len(tok.replace(",", "")) >= 2}
    if bad:
        return fallback
    return t


# --------------------------------------------------------------------------- avg-ticket defaults ---
# When we don't yet know a prospect's REAL average sale/job value we ESTIMATE it from their industry, so the
# enquiry→job→dollars bridge (and the hero loss figure) always renders instead of collapsing to the tiny
# SEO click-value. Every number here is a deliberately mid-range AUD figure, ALWAYS surfaced to the owner as
# an estimate ("tell us yours and we'll refine it"). Ordered most-specific first; first keyword hit wins.
_TICKET_DEFAULTS = [
    (r"machin|fabricat|weld|steel|metal|cnc|tooling|foundry|casting|industrial|manufactur|engineer", 6000, "engineering / industrial"),
    (r"real ?estate|realty|property manage|conveyanc", 8000, "property"),
    (r"plumb|electric|hvac|air.?condition|roof|concret|paving|fenc|landscap|builder|building|construct|"
     r"carpent|renovat|tiling|paint|glazier|glazing|excavat|solar|garage|gutter|decking|pergola|joiner|cabinet",
     3500, "trades / construction"),
    (r"law|legal|solicit|attorney|barrister|account|bookkeep|financ|advisor|advisory|consult|architect|"
     r"survey|insur|mortgage|taxation", 5000, "professional services"),
    (r"wedding|event|photograph|videograph|florist", 2500, "events / creative"),
    (r"dent|orthodon|medical|clinic|physio|chiro|health|surg|cosmetic|dermat|optom|podiatr|veterin|aesthet|"
     r"fertility", 1400, "healthcare"),
    (r"auto|mechanic|panel|tyre|smash repair|automotive|dealership|vehicle", 1200, "automotive"),
    (r"educat|training|tutor|coaching|academy|college|course|driving school", 1500, "education / training"),
    (r"software|saas|technolog|web design|web development|digital|marketing|agency|\bseo\b|advertis", 4000, "technology / services"),
    (r"\bhire\b|\brental\b|\brentals\b|\bhiring\b|equipment hire|tool hire|plant hire|scaffold|\bleasing\b", 600, "equipment / tool hire"),
    (r"clean|pest|removal|storage|logistic|freight|transport|courier|waste|security|maintenance", 900, "commercial services"),
    (r"salon|spa|beauty|hairdress|barber|nail|massage|waxing|lash|brow|tanning", 220, "beauty / wellness"),
    (r"restaurant|cafe|catering|bakery|hospitality|brewery|winery|takeaway", 130, "hospitality"),
    (r"retail|store|ecommerce|e-commerce|boutique|apparel|fashion|jewel|furnitur|homeware", 250, "retail"),
]
_TICKET_FALLBACK = (2000, "small business")


def _default_ticket(industry, sub_industry=None):
    """Estimate a typical average sale/job value (AUD) + a plain bucket label from the industry text."""
    text = f"{industry or ''} {sub_industry or ''}".lower()
    for pat, val, label in _TICKET_DEFAULTS:
        if _re.search(pat, text):
            return val, label
    return _TICKET_FALLBACK


def default_avg_ticket(industry, sub_industry=None):
    """Public alias — crm.ensure_growth_audit derives the SAME ticket to feed assemble_audit's revenue
    model, so the audit-wide figures and this report agree. Returns (value, bucket_label)."""
    return _default_ticket(industry, sub_industry)


def service_seeds(settings, company, industry=None, sub_industry=None, limit=14, services_hint=""):
    """Derive the business's core buyer-intent SERVICE/PRODUCT search terms (AU market) so keyword-demand
    discovery ALWAYS has real seeds — even when the domain has no SEO footprint. This is the durable fix for
    THIN audits (a low-footprint builder/gardener/etc. otherwise discovers nothing and the report collapses).
    LLM-derived (guarded), with a light fallback from the industry text. Returns short keywords (no brand,
    no suburb/location, no filler); [] only if there's truly nothing to go on."""
    def _fallback():
        # NEVER emit a bare industry NOUN: 'Animal Production' seeded 'animal' -> DataForSEO returned
        # wildlife searches ('quokka animal', 'capybara animal') on the Prendergast audit (2026-08-28).
        # Only the full COMPOUND label (2+ words) is safe enough to seed; a bare sector word is not.
        # A thin audit is honest; a wildlife audit destroys credibility. [] => seed-less (thin) path.
        seeds = []
        for label in (sub_industry, industry):
            t = (label or "").lower()
            t = _re.sub(r"\b(sector|other|general|misc(ellaneous)?|n\.?e\.?c\.?)\b", " ", t)
            t = _re.sub(r"[^a-z ]+", " ", t)
            t = _re.sub(r"\s+", " ", t).strip()
            if t and " " in t and len(t) >= 8:          # compound phrase only, never one bare noun
                seeds.append(t)
        return list(dict.fromkeys(seeds))[:limit]
    key = (getattr(settings, "anthropic_api_key", "") or "").strip()
    if not key and not (industry or sub_industry):
        return []
    if not key:
        return _fallback()
    try:
        import anthropic
        import json as _json
        model = getattr(settings, "anthropic_model_cheap", "") or "claude-haiku-4-5-20251001"
        sys_p = (
            "You are an SEO analyst for AUSTRALIAN small businesses. Given a business, list the SHORT, "
            "buyer-intent search terms its customers actually type into Google. Rules: "
            "no brand names, no suburb/city/location words, no generic filler ('services', 'company', 'best'). "
            "Concrete services/products only (e.g. for a home builder: 'custom home builder', 'knockdown rebuild', "
            "'home extensions', 'new home designs', 'duplex builder').\n"
            "CRITICAL — MATCH THE BUYER, NOT THE NOUN. If the business PERFORMS a service, return terms for "
            "someone HIRING that service, never for someone BUYING the product it works with. A commercial "
            "TILING CONTRACTOR is found by 'tiling contractor', 'wall and floor tiler', 'waterproofing "
            "contractor', 'stone cladding installation' — NOT by 'terracotta tiles' or 'paver tiles', which "
            "are retail shoppers who will never hire them. A print-equipment supplier is found by 'uv curing "
            "system' and 'anilox roll', NOT 'commercial printing'. Getting this wrong points the whole report "
            "at the wrong market, so prefer the hiring term every time.\n"
            f"Output ONLY a JSON array of {limit} lowercase strings.")
        # The business's OWN words about what it does — its site headings/services. Without this the model
        # only has a company name to guess from, and a name like "RB Tile" reads as a tile shop rather than
        # the commercial tiling contractor it is (Raj, 2026-09-04).
        hint = (services_hint or "").strip()
        usr = (f"Business name: {company or '?'}\nIndustry: {industry or '?'} / {sub_industry or '?'}"
               + (f"\nWhat they say they do (from their own website): {hint[:900]}" if hint else ""))
        r = anthropic.Anthropic(api_key=key).messages.create(
            model=model, max_tokens=400,
            system=sys_p, messages=[{"role": "user", "content": usr}])
        txt = "".join(getattr(b, "text", "") for b in r.content if getattr(b, "type", None) == "text")
        mobj = _re.search(r"\[.*\]", txt, _re.S)
        arr = _json.loads(mobj.group(0)) if mobj else []
        seeds = [str(s).strip().lower() for s in arr if isinstance(s, str) and 2 < len(str(s).strip()) < 60]
        seeds = list(dict.fromkeys(seeds))[:limit]
        if len(seeds) >= 3:
            return seeds
    except Exception:
        pass
    return _fallback()


def estimate_avg_ticket(settings, company, industry=None, sub_industry=None, services_hint="", location=""):
    """Market-GROUNDED estimate of the average value of ONE NEW CUSTOMER (AUD) for an Australian SMB — asks
    Anthropic to apply real AU market standards and, crucially, to use the ANNUAL customer value for a
    RECURRING service (gardening/cleaning/maintenance) and the per-job value for one-off work, so the audit's
    revenue model stops mis-pricing (e.g. a gardening-maintenance client valued as a $3,500 landscaping build).
    Falls back to the industry table on any failure. Returns (value:int, label:str, estimated:bool=True)."""
    fb_val, fb_label = _default_ticket(industry, sub_industry)
    key = (getattr(settings, "anthropic_api_key", "") or "").strip()
    if not key or not (company or industry or sub_industry):
        return fb_val, fb_label, True
    try:
        import anthropic
        import json as _json
        model = getattr(settings, "anthropic_model_cheap", "") or "claude-haiku-4-5-20251001"
        sys_p = (
            "You are a market analyst for AUSTRALIAN small businesses. Estimate the realistic AVERAGE VALUE OF "
            "ONE NEW CUSTOMER in AUD, to CURRENT Australian market standards. Rules: if the business is a "
            "RECURRING service (gardening, lawn/garden maintenance, cleaning, pest, pool care, bookkeeping), use "
            "the customer's typical ANNUAL value (per-visit price x realistic visits/year), NOT one visit. If it "
            "is one-off/project work (a landscaping build, a legal matter, a renovation), use the typical per-job "
            "value. Be realistic and mid-market, not the top end. Output ONLY compact JSON: "
            '{"avg_customer_value_aud": <number>, "recurring": <true|false>, "basis": "<max 8 words>"}.')
        usr = (f"Business name: {company or '?'}\nIndustry: {industry or '?'} / {sub_industry or '?'}\n"
               f"Services / notes: {services_hint or '(infer from the name/industry)'}\n"
               f"Location: {location or 'Australia'}")
        r = anthropic.Anthropic(api_key=key).messages.create(
            model=model, max_tokens=200,
            system=sys_p, messages=[{"role": "user", "content": usr}])
        txt = "".join(getattr(b, "text", "") for b in r.content if getattr(b, "type", None) == "text")
        mobj = _re.search(r"\{.*\}", txt, _re.S)
        data = _json.loads(mobj.group(0)) if mobj else {}
        val = data.get("avg_customer_value_aud")
        if isinstance(val, (int, float)) and 40 <= val <= 250000:
            basis = str(data.get("basis") or "").strip()[:60]
            recurring = bool(data.get("recurring"))
            label = basis or (f"{fb_label} (recurring, annual)" if recurring else fb_label)
            return int(round(val)), label, True
    except Exception:
        pass
    return fb_val, fb_label, True


# --------------------------------------------------------------------------- keyword scrub / accuracy ---
# Equipment-PURCHASE-intent terms: for a SERVICE business ("we machine / bore / repair for you"), a search
# for buying/hiring the equipment itself ("line boring machine for sale", "…for hire") is NOT demand they
# can win — it's a product shopper. These leak in from the competitor gap and must never be shown as a
# money keyword the prospect should rank for.
_BUY_INTENT_RE = _re.compile(
    r"\bfor\s+(sale|hire|rent|lease)\b"
    r"|\bsecond[\s-]?hand\b"
    r"|\b(buy|buying|purchase|purchasing)\b"
    r"|\b(machines?|machinery|equipment|tools?|parts?)\s+for\s+(sale|hire)\b", _re.I)
# a bare equipment-product noun with no service/where signal ("portable line boring machine") is also a
# buy-a-machine search, not a service enquiry — dropped for service businesses unless it names a service.
_EQUIP_NOUN_RE = _re.compile(r"\b(machine|machines|machinery|equipment)\b", _re.I)
_SERVICE_WORD_RE = _re.compile(
    r"\b(service|services|servicing|hire|repair|repairs|rebuild|reline|relining|"
    r"contractor|contractors|company|specialist|specialists|shop|workshop|machining|"
    r"maintenance|installation|supplier|suppliers|near\s+me)\b", _re.I)
# product-selling verticals — everyone else is treated as a service provider (the default for our SMB base)
_RETAIL_RE = _re.compile(r"retail|ecommerce|e-commerce|\bstore\b|\bshop\b|boutique|apparel|fashion|"
                         r"jewel|furnitur|homeware|marketplace|grocer|supermarket|liquor|florist", _re.I)


def _service_business(industry, sub_industry=None) -> bool:
    """True when the prospect SELLS A SERVICE (so buy-a-product searches are off-intent). Defaults True —
    the overwhelming majority of our cold-called SMBs are service/trade/professional businesses."""
    text = f"{industry or ''} {sub_industry or ''}"
    if not text.strip():
        return True
    return not bool(_RETAIL_RE.search(text))


def _is_buy_intent_kw(kw: str) -> bool:
    """True for equipment purchase/hire searches that a service business shouldn't chase."""
    k = (kw or "")
    if not k:
        return False
    if _BUY_INTENT_RE.search(k):
        return True
    # bare product-noun search with no service signal = someone shopping for the machine, not the service
    if _EQUIP_NOUN_RE.search(k) and not _SERVICE_WORD_RE.search(k):
        return True
    return False


def _norm_kw_join(s: str) -> str:
    return _re.sub(r"[^a-z0-9]+", "", (s or "").lower())


# Generic industry / geo words that appear inside rival COMPANY NAMES ("Rotomotion Engineering") but are
# NOT distinctive brands — never strip a real keyword just because a competitor's name contains them.
_GENERIC_BRAND_STOP = {
    "engineering", "engineers", "services", "service", "solutions", "industries", "industrial",
    "manufacturing", "fabrication", "fabricators", "machining", "welding", "tooling", "mechanical",
    "plumbing", "electrical", "electricians", "roofing", "concrete", "construction", "constructions",
    "builders", "building", "landscaping", "landscapes", "fencing", "painting", "painters", "cleaning",
    "removals", "transport", "logistics", "freight", "consulting", "consultants", "medical", "dental",
    "legal", "accounting", "accountants", "marketing", "digital", "design", "supplies", "supply",
    "products", "trades", "contractors", "maintenance", "installations", "installation", "repairs",
    "repair", "specialists", "specialist", "australia", "australian", "group", "company", "national",
    "melbourne", "sydney", "brisbane", "perth", "adelaide", "canberra", "hobart", "darwin", "geelong",
    "automotive", "hydraulics", "hydraulic", "pumps", "valves", "equipment", "machinery",
    # hire / rental industry words — generic category words, NOT distinctive brands. Without these, a
    # business whose NAME contains the service word (e.g. "Planet Hire", "ABC Rentals") would have EVERY
    # real keyword (they all contain "hire"/"rental") wrongly stripped from its universe as its own brand.
    "hire", "hires", "hiring", "rental", "rentals", "rent", "leasing", "lease",
}


def _competitor_brand_tokens(comps, exclude=None) -> set:
    """Distinctive brand strings the RIVALS own (their own names/domains) — stripped from the gap so a
    competitor's brand ("rotomotion engineering") is never surfaced as a money keyword the prospect should
    rank for. Uses the domain root (the concatenated, distinctive brand, e.g. 'rotomotion') plus non-generic
    name words; normalised, kept ≥5 chars, generic industry/geo words filtered out so legitimate keywords
    ('precision engineering') are never nuked. The prospect's own brand tokens are excluded."""
    out: set = set()
    try:
        from .enrichment.dataforseo import brand_tokens
    except Exception:
        return out
    exclude = {_norm_kw_join(x) for x in (exclude or set())}
    for c in (comps or []):
        try:
            # domain root — always distinctive, safe to strip
            dom = _re.sub(r"^www\.", "", (c.get("domain") or "").lower().split("/")[0])
            root = _norm_kw_join(dom.split(".")[0])
            if len(root) >= 5 and root not in exclude:
                out.add(root)
            # name words — keep only distinctive (non-generic) ones
            for t in brand_tokens(c.get("domain") or "", c.get("name") or ""):
                nt = _norm_kw_join(t)
                if len(nt) >= 5 and nt not in exclude and nt not in _GENERIC_BRAND_STOP:
                    out.add(nt)
        except Exception:
            continue
    return out


def _kw_is_scrubbed(kw, comp_brands, is_service) -> bool:
    """A gap/opportunity keyword we must NOT show: a rival's brand term, or (for a service business) a
    buy-a-machine term."""
    try:
        from .enrichment.dataforseo import is_branded
    except Exception:
        def is_branded(k, b):
            return False
    if comp_brands and is_branded(kw, comp_brands):
        return True
    if is_service and _is_buy_intent_kw(kw):
        return True
    return False


def _is_low_value_gap(row) -> bool:
    """True for a keyword-gap / money row with NO demonstrable commercial value — money_value, cap_value
    AND cpc all falsy (0/None). Such a row is a brand or NAVIGATIONAL search (e.g. "rotomotion
    engineering" — a RIVAL's company name someone typed to find THEM), not a "money search you're
    missing", so it must never be surfaced as a target. ALL three must be falsy, so a genuine keyword that
    merely lacks a CPC but still shows a money_value is kept."""
    if not isinstance(row, dict):
        return False
    return (not row.get("money_value")) and (not row.get("cap_value")) and (not row.get("cpc"))


def _scrub_keyword_rows(rows, comp_brands, is_service) -> list:
    return [r for r in (rows or [])
            if not _is_low_value_gap(r)
            and not _kw_is_scrubbed(r.get("keyword") or "", comp_brands, is_service)]


_PLAN_TARGET_RE = _re.compile(r"""['"]([^'"]+)['"]""")


def _plan_row_keyword(row) -> str:
    """The keyword a plan / quick-win row TARGETS: an explicit 'keyword' field, else the term quoted in a
    "Target '<kw>'" title (the exact idiom competitor.build_quick_wins_and_growth emits). Returns '' when
    the row targets no specific keyword (a backlink / schema / paid-search play) — those are never touched."""
    if not isinstance(row, dict):
        return ""
    kw = row.get("keyword")
    if kw:
        return str(kw)
    mo = _PLAN_TARGET_RE.search(row.get("title") or "")
    return mo.group(1) if mo else ""


def _scrub_plan_rows(rows, comp_brands, is_service, scrubbed_norms) -> list:
    """Drop a plan / quick-win row that TARGETS a keyword we stripped from the gap — a rival brand, a
    buy-a-machine term, or a zero-value navigational search — so "Target 'rotomotion engineering'" can't
    reappear in the 90-day plan after the gap itself was cleaned. Rows with no target keyword pass through."""
    scrubbed_norms = scrubbed_norms or set()
    out = []
    for r in (rows or []):
        kw = _plan_row_keyword(r)
        if kw and (_kw_is_scrubbed(kw, comp_brands, is_service) or _norm_kw_join(kw) in scrubbed_norms):
            continue
        out.append(r)
    return out


def _scrub_model_keywords(m: dict) -> dict:
    """Defensive, render-time scrub of every keyword surface on the model (competitor brands + buy-a-machine
    terms + ZERO-VALUE navigational searches) so the prospect-facing tables AND the 90-day plan are clean
    even if the caller passed a pre-assembled/cached model that skipped assemble_audit's scrub. Returns a
    shallow copy; recomputes nothing here (totals are recomputed at the point of display). Idempotent."""
    biz = m.get("business") or {}
    comp_brands = _competitor_brand_tokens(m.get("competitors") or [], exclude=set())
    is_service = _service_business(biz.get("industry"), biz.get("sub_industry"))
    out = dict(m)
    # gap + outranked: rival brands, buy-a-machine terms AND zero-value navigational searches
    orig_gap_norms = {_norm_kw_join(r.get("keyword") or "") for r in (m.get("keyword_gap") or [])}
    if m.get("keyword_gap"):
        out["keyword_gap"] = _scrub_keyword_rows(m["keyword_gap"], comp_brands, is_service)
    if m.get("outranked"):
        out["outranked"] = _scrub_keyword_rows(m["outranked"], comp_brands, is_service)
    # a keyword we removed from the gap must not resurface as a "Target 'X'" quick-win / plan step
    kept_gap_norms = {_norm_kw_join(r.get("keyword") or "") for r in (out.get("keyword_gap") or [])}
    scrubbed_norms = orig_gap_norms - kept_gap_norms
    for k in ("quick_wins", "growth_plan"):
        if m.get(k):
            out[k] = _scrub_plan_rows(m[k], comp_brands, is_service, scrubbed_norms)
    seo = dict(m.get("seo") or {})
    if seo.get("money_keywords"):
        # rival brands + zero-value terms (a service business's own service terms are legitimate money kws)
        seo["money_keywords"] = [r for r in seo["money_keywords"]
                                 if not _is_low_value_gap(r)
                                 and not (comp_brands and _kw_is_scrubbed(r.get("keyword") or "", comp_brands, False))]
        out["seo"] = seo
    out["_scrub_meta"] = {"comp_brands": sorted(comp_brands), "is_service": is_service}
    return out


# --------------------------------------------------------------------------- number / range formatting ---
def _n1(v) -> str:
    """Format a count that may be sub-1: <10 keeps one decimal (so 0.3 sales/mo shows honestly instead of
    rounding to a self-contradicting "0"); ≥10 is a plain integer with thousands separators."""
    try:
        f = float(v)
    except Exception:
        return "0"
    if f >= 10 or f == int(f):
        return format(int(round(f)), ",")
    return f"{f:.1f}"


def _traffic_range(v) -> str:
    """Single-digit monthly-traffic estimates are below meaningful resolution — present a band, not a
    false-precise "1/mo". Returns a display string (no unit)."""
    try:
        f = float(v or 0)
    except Exception:
        return "—"
    if f <= 0:
        return "—"
    if f < 10:
        return "under 10"
    if f < 30:
        return "10–30"
    return _n(f)


def _cadence(sales_per_mo) -> str:
    """Plain-English cadence for a sub-1 monthly sale rate — the human way to read "0.3 sales/mo": about
    one new job every N months. Reconciles the chain without a confusing "0 customers"."""
    try:
        s = float(sales_per_mo or 0)
    except Exception:
        s = 0
    if s <= 0:
        return ""
    if s >= 1:
        return f"about {_n1(s)} new jobs a month"
    months = max(1, int(round(1.0 / s)))
    if months <= 1:
        return "about one new job a month"
    return f"about one new job every {months} months"


# --------------------------------------------------------------------------- source / proof labels ---
def _src(text) -> str:
    """A small, unobtrusive provenance caption placed beneath a figure/section — Vysakh's core rule that
    every number carries a visible source/justification. Never fabricates: it states where the figure came
    from (DataForSEO search data, Google Ads Transparency Center, on-page checks) or that it's an estimate."""
    return f'<div class="srccap">{_e(text)}</div>'


def _assumptions_caption(rev: dict, addr=None) -> str:
    """The one honest sentence behind every dollar figure: the searches × enquiry-rate × close-rate ×
    ticket that build it — each factor tagged as an estimate. Applied under every $ loss figure."""
    rev = rev or {}
    addr = addr if addr is not None else (rev.get("addressable_traffic") or 0)
    v2l = int(round((rev.get("visitor_to_lead") or 0.03) * 100))
    l2s = int(round((rev.get("lead_to_sale") or 0.25) * 100))
    ticket = rev.get("avg_ticket")
    tkt = _fmt_money(ticket) if ticket else "your job value"
    est = " est." if rev.get("avg_ticket_estimated") else ""
    return (f'Illustrative — [{_n(addr)} est. searches/mo] × [{v2l}% become enquiries] × '
            f'[{l2s}% become customers] × [{tkt}{est} per job]. Adjust any factor and it changes.')


def _normalise_revenue(m: dict, avg_ticket=None) -> dict:
    """Guarantee a revenue block with a ticket + the monthly enquiry→job→dollars bridge so the money story
    ALWAYS renders. Uses the REAL ticket when known, otherwise an industry ESTIMATE (flagged). Never invents
    the traffic base — the bridge is (addressable_traffic × the model's conservative funnel × ticket), every
    factor sourced from live data. Returns a shallow copy; the caller's model is untouched."""
    rev = dict(m.get("revenue") or {})
    biz = m.get("business") or {}
    ticket = rev.get("avg_ticket") or avg_ticket
    estimated = bool(rev.get("avg_ticket_estimated"))
    bucket = rev.get("avg_ticket_bucket")
    if not ticket:
        ticket, bucket = _default_ticket(biz.get("industry"), biz.get("sub_industry"))
        estimated = True
    if estimated and not bucket:
        _, bucket = _default_ticket(biz.get("industry"), biz.get("sub_industry"))
    addr = rev.get("addressable_traffic") or 0
    v2l = rev.get("visitor_to_lead") or 0.03
    l2s = rev.get("lead_to_sale") or 0.25
    if not rev.get("monthly") and ticket and addr:
        rev["leads_per_mo"] = round(addr * v2l, 3)          # 3-dp so the chain reconciles (see _n1/_cadence)
        rev["sales_per_mo"] = round(addr * v2l * l2s, 3)
        rev["monthly"] = round(addr * v2l * l2s * ticket)
        rev["annual"] = rev["monthly"] * 12
    rev["avg_ticket"] = ticket
    rev["avg_ticket_estimated"] = estimated
    rev["avg_ticket_bucket"] = bucket
    out = dict(m)
    out["revenue"] = rev
    return out


def _hero_loss(m: dict) -> dict:
    """The single biggest RECURRING loss, in the owner's own dollar terms — the report's hero number. Built
    from the (grounded) revenue bridge; the named rival is the strongest competitor we actually found."""
    rev = m.get("revenue") or {}
    comps = [c for c in (m.get("competitors") or []) if (c.get("domain") or c.get("name"))]
    monthly = int(rev.get("monthly") or 0)
    return {
        "monthly": monthly,
        "annual": int(rev.get("annual") or monthly * 12),
        "rival": (comps[0].get("domain") or comps[0].get("name")) if comps else None,
        "ticket": rev.get("avg_ticket"),
        "estimated": bool(rev.get("avg_ticket_estimated")),
        "bucket": rev.get("avg_ticket_bucket"),
    }


# --------------------------------------------------------------------------- distil ---
def _kw(rows, n):
    out = []
    for r in (rows or [])[:n]:
        k = (r.get("keyword") or "").strip()
        if k:
            out.append(r)
    return out


def _distill(m: dict, name: str) -> dict:
    opp = m.get("opportunity") or {}
    biz = m.get("business") or {}
    seo = m.get("seo") or {}
    uni = m.get("universe") or {}
    rev = m.get("revenue") or {}
    grounded = int((opp.get("gap_capturable") or 0) + (opp.get("quickwin_value") or 0))
    miss = 0
    for r in (seo.get("money_keywords") or []) + (m.get("keyword_gap") or []):
        miss += int(r.get("volume") or r.get("search_volume") or 0)
    comps = [c for c in (m.get("competitors") or []) if (c.get("domain") or c.get("name"))]
    return {
        "business": name,
        "industry": biz.get("industry"),
        "location": biz.get("location"),
        "domain": m.get("domain"),
        "overall_health": (m.get("health") or {}).get("overall"),
        "sov": m.get("sov"),
        "ai_score": (m.get("geo_aeo") or {}).get("score"),
        "grounded_gap_month": grounded or None,
        "revenue_month": rev.get("monthly"),
        "revenue_year": rev.get("annual"),
        "avg_ticket": rev.get("avg_ticket"),
        "visitors_month": opp.get("est_org_traffic"),
        "searches_ranked": opp.get("org_keywords") or (uni.get("totals") or {}).get("ranked"),
        "searches_missing": miss or None,
        "competitor_count": len(comps),
        "top_competitor": (comps[0].get("domain") or comps[0].get("name")) if comps else None,
        "competitors": [(c.get("domain") or c.get("name")) for c in comps[:5]],
        "ads_running": bool((m.get("ads") or {}).get("running")),
        "diagnosis": m.get("diagnosis"),
        "top_missing": [ (r.get("keyword") or "") for r in _kw(m.get("keyword_gap"), 6) ],
        "engine_findings": [ (f.get("text") if isinstance(f, dict) else str(f)) for f in (m.get("findings") or [])[:6] ],
    }


# --------------------------------------------------------------------------- finding seeds ---
def _finding_seeds(m: dict) -> list:
    """Build GROUNDED finding seeds from the real data. Each seed carries its own evidence + severity + a
    sort $ so the LLM only writes prose; it never decides the facts. Deduped, sorted by $, capped at 11."""
    seeds = []
    seo = m.get("seo") or {}
    ads = m.get("ads") or {}
    geo = m.get("geo_aeo") or {}
    tech = m.get("tech") or {}
    opp = m.get("opportunity") or {}

    def add(key, kind, sev, hint, evidence, val=0):
        seeds.append({"key": key, "kind": kind, "sev": sev, "hint": hint, "evidence": evidence, "val": val})

    # engine's own quantified findings (already grounded)
    for f in (m.get("findings") or [])[:6]:
        t = (f.get("text") if isinstance(f, dict) else str(f)) or ""
        k = (f.get("kind") if isinstance(f, dict) else "seo") or "seo"
        if t.strip():
            add("f_" + _re.sub(r"\W+", "", t.lower())[:24], k, "high", t.strip(),
                "From your live search + ads data.", 0)
    # money keywords one push from page 1
    for r in _kw(seo.get("money_keywords"), 4):
        kw = r.get("keyword"); pos = r.get("position"); vol = r.get("search_volume") or r.get("volume")
        val = int(r.get("upside_value") or r.get("money_value") or 0)
        ev = f"You rank #{_n(pos)} for “{_e(kw)}”" + (f" — {_n(vol)} searches/mo." if vol else ".")
        add("mk_" + str(kw), "quickwin", "med", f"page-2 keyword ‘{kw}’ one push from page one", ev, val)
    # keyword gap — competitors rank, you don't
    for r in _kw(m.get("keyword_gap"), 4):
        kw = r.get("keyword"); vol = r.get("volume") or r.get("search_volume")
        val = int(r.get("cap_value") or r.get("money_value") or 0)
        ev = f"Competitors rank for “{_e(kw)}”" + (f" ({_n(vol)} searches/mo)" if vol else "") + " — you don't appear."
        add("gap_" + str(kw), "gap", "high", f"missing money keyword ‘{kw}’ competitors own", ev, val)
    # outranked
    for r in _kw(m.get("outranked"), 2):
        kw = r.get("keyword"); op = r.get("our_position"); cp = r.get("competitor_position")
        val = int(r.get("cap_value") or r.get("money_value") or 0)
        ev = f"For “{_e(kw)}” you sit #{_n(op)} while a rival holds #{_n(cp)}."
        add("or_" + str(kw), "competitor", "med", f"a rival outranks you for ‘{kw}’", ev, val)
    # not running ads
    if not ads.get("running"):
        add("no_ads", "ads", "high", "not running Google Ads for ready-to-buy searches",
            "No live Google Ads found in the Transparency Center for your domain.", 0)
    # https
    if tech.get("https") is False:
        add("no_https", "tech", "high", "site is not served securely over HTTPS",
            "Your site did not resolve over HTTPS — browsers flag this and Google demotes it.", 0)
    # failing GEO/AEO checks — plain-English: "when someone asks Google's AI or ChatGPT for a business like
    # yours, your site is hard for it to pick up and quote"
    for c in (geo.get("checks") or []):
        if not c.get("pass") and c.get("note"):
            add("geo_" + str(c.get("name")), "tech", "med",
                "hard for Google's AI answers and chatbots to find and recommend you",
                "When people ask Google's AI or a chatbot for a business like yours, your site is hard for it "
                "to read and quote. " + _e(c.get("note")), 0)
    # content gap
    for r in (m.get("content_gap") or [])[:2]:
        topic = r.get("topic") or r.get("page"); vol = r.get("total_volume")
        ev = f"Competitors publish on “{_e(topic)}”" + (f" ({_n(vol)} searches/mo)" if vol else "") + " — you have no page for it."
        add("cg_" + str(topic), "content", "med", f"content gap: ‘{topic}’", ev, int(vol or 0))
    # backlinks
    bg = m.get("backlink_gap") or {}
    if isinstance(bg, dict) and bg.get("referring_domains") is not None:
        rd = bg.get("referring_domains")
        add("bl", "authority", "med", "few other websites link to yours, so Google trusts you less than rivals",
            f"Only {_n(rd)} other websites link to yours — a big reason Google ranks competitors above you.", 0)

    # dedupe by key, sort high-severity + $ first, cap 11
    seen = {}
    for s in seeds:
        if s["key"] not in seen:
            seen[s["key"]] = s
    ordered = sorted(seen.values(), key=lambda s: (0 if s["sev"] == "high" else 1, -s["val"]))
    return ordered[:11]


# --------------------------------------------------------------------------- LLM narrative ---
_SYS = (
    "You are a senior digital-marketing strategist writing a PREMIUM, confidential growth audit for an "
    "Australian business owner. Tone: precise, warm, plain-English, quietly authoritative — never salesy or "
    "generic. HARD RULES: (1) NEVER write any number, percentage, dollar amount, position or count — those "
    "are inserted automatically; write words only. (2) Focus ONLY on getting more customers from Google "
    "(search visibility, missed searches, competitors, Google Ads, being found by Google/AI). (3) Keep every "
    "field SHORT — one or two sentences, no filler. (4) Australian spelling. "
    "Return ONLY a JSON object with EXACTLY these keys: "
    "cover_subtitle (a 4-6 word line under the business name), cover_lede (1 sentence framing the audit), "
    "exec_headline (a short serif headline, ≤9 words), exec_intro (1-2 sentences building on the diagnosis), "
    "pillar_leak (1 sentence — the biggest revenue leak), pillar_blindspot (1 sentence — the biggest blind "
    "spot), pillar_opportunity (1 sentence — the biggest opportunity), methodology_intro (1 sentence), "
    "intro_visibility, intro_money, intro_areas, intro_benchmark, intro_content, intro_ads, intro_ai, "
    "intro_backlinks, intro_revenue (one short intro line each), plan_intro (1 sentence), next_step (2 "
    "sentences for the reveal call), findings (an ARRAY, one object per finding I give you, IN THE SAME "
    "ORDER, each: {title: a short specific headline ≤10 words, obs: 1 sentence explaining it, cost: 1 "
    "sentence on why it costs customers, fix: 1 sentence on what we'd do}). Output ONLY the JSON."
)


def _narrative(key, model, brief, seeds) -> dict:
    if not key:
        return {}
    try:
        payload = {"business": brief, "findings_to_write": [
            {"about": s["hint"], "kind": s["kind"]} for s in seeds]}
        txt = _claude_text(key, model, _SYS, "AUDIT DATA (write words only, never numbers):\n"
                           + _json.dumps(payload, default=str)[:6000], max_tokens=4000).strip()
        if txt.startswith("```"):
            txt = txt.split("\n", 1)[1] if "\n" in txt else txt[3:]
            if txt.rstrip().endswith("```"):
                txt = txt.rstrip()[:-3]
        i, j = txt.find("{"), txt.rfind("}")
        return _json.loads(txt[i:j + 1]) if i >= 0 and j > i else {}
    except Exception:
        return {}


# --------------------------------------------------------------------------- deterministic renderers ---
def _sec(eyebrow, title, intro, body) -> str:
    ih = f'<p class="sec-intro">{_e(intro)}</p>' if intro else ""
    return (f'<section><div class="wrap"><div class="eyebrow">{_e(eyebrow)}</div>'
            f'<h2 class="sec">{_e(title)}</h2>{ih}{body}</div></section>')


def _gauge(overall) -> str:
    try:
        sc = max(0, min(100, int(round(float(overall)))))
    except Exception:
        sc = 0
    word, col = _band(sc)
    C = 515.0
    dash = round(sc / 100.0 * C, 1)
    return (
        '<div class="gauge"><svg viewBox="0 0 200 200" width="220" height="220">'
        '<circle cx="100" cy="100" r="82" fill="none" stroke="#eee7d8" stroke-width="20"/>'
        f'<circle cx="100" cy="100" r="82" fill="none" stroke="{col}" stroke-width="20" stroke-linecap="round" '
        f'stroke-dasharray="{dash} {C - dash:.1f}" transform="rotate(-90 100 100)"/></svg>'
        f'<div class="num"><b>{sc}</b><span>Growth health</span><em style="color:{col}">{word}</em></div></div>')


def _health_bar(label, sc, *, flag=None, share=False) -> str:
    """One scorecard bar. Floor-clamped / sentinel / unmeasured dimensions render a QUALITATIVE band (or an
    honest phrase like 'Not advertising') instead of dressing a placeholder number up as a precise grade;
    share-of-visibility renders as a '% share', not on a misleading /100 scale (defects 5 & 7)."""
    sc = max(0, min(100, int(round(float(sc or 0)))))
    word, col = _band(sc)
    if share:
        val = f'{sc}<i>% share</i>'
    elif flag == "not_advertising":
        val, col = 'Not advertising', "#a6432c"
    elif flag == "unmeasured":
        val, col = 'Limited data', "#6f7a72"
    elif flag == "floor":
        val = word                                  # qualitative only — the raw score is a floor clamp
    else:
        val = f'{word} <i>{sc}/100</i>'             # measured dim: band word + small score
    return (f'<div class="bar"><div class="top"><span>{label}</span><b>{val}</b></div>'
            f'<div class="track"><span class="fill" style="width:{max(3, sc)}%;background:{col}"></span></div></div>')


def _exec_summary(m, d, nv, allowed) -> str:
    h = m.get("health") or {}
    flags = h.get("flags") or {}
    bars = ""
    for label, key in [("Getting found on Google", "seo"), ("Google Ads", "ads"),
                       ("Standing out from rivals", "competitive"), ("Website &amp; tech health", "technical")]:
        sc = h.get(key)
        if sc is None:
            continue
        bars += _health_bar(label, sc, flag=flags.get(key))
    if m.get("sov") is not None:
        bars += _health_bar("Share of visibility", m.get("sov"), share=True)
    if (m.get("geo_aeo") or {}).get("score") is not None:
        bars += _health_bar("Found by Google &amp; AI", (m["geo_aeo"]).get("score"))
    illus = ('<div class="srccap">Illustrative composite — a directional read from the public signals we could '
             'measure (search rankings, ads, on-page checks), not an exact grade.</div>')
    # stat strip (grounded) — LEAD with the recurring $ loss in the owner's terms, never the SEO click value.
    # No "+" on the search total: it is the exact sum of the (brand/off-intent-scrubbed) gaps we found.
    stats = []
    stat_src = ""
    if d.get("revenue_month"):
        stats.append((_fmt_money(d["revenue_month"]), "in new work slipping to competitors every month"))
        stat_src = _assumptions_caption(m.get("revenue") or {}, (m.get("revenue") or {}).get("addressable_traffic"))
    elif d.get("grounded_gap_month"):
        stats.append((_fmt_money(d["grounded_gap_month"]), "of customer searches going to rivals, a month"))
        stat_src = "Illustrative — realistically capturable search value (volume × capture-rate × CPC, DataForSEO)."
    if d.get("searches_missing"):
        stats.append((_n(d["searches_missing"]), "monthly searches across the gaps we found"))
    if d.get("competitor_count"):
        stats.append((str(d["competitor_count"]), "competitors ahead of you online"))
    stat_html = "".join(f'<div class="oppstat light"><b>{v}</b><span>{lab}</span></div>' for v, lab in stats[:3])
    strip_src = (f'<div class="srccap">{_e(stat_src)} Search counts: Google search volumes (DataForSEO).</div>'
                 if stat_html else "")
    # pillars (narrative, grounded fallback)
    leak = _clean_prose(nv.get("pillar_leak"), allowed,
                        "Customers are searching for what you do — and finding your competitors first.")
    blind = _clean_prose(nv.get("pillar_blindspot"), allowed,
                         "The everyday searches that bring ready-to-buy customers aren't yet working for you.")
    opp = _clean_prose(nv.get("pillar_opportunity"), allowed,
                       "Closing a handful of specific gaps would put you in front of that demand.")
    pillars = (
        '<div class="grid g3" style="margin-top:24px">'
        f'<div class="card"><div class="eyebrow" style="color:var(--rust)">Biggest leak</div><p style="margin:0">{_e(leak)}</p></div>'
        f'<div class="card"><div class="eyebrow" style="color:var(--amber)">Biggest blind spot</div><p style="margin:0">{_e(blind)}</p></div>'
        f'<div class="card"><div class="eyebrow" style="color:var(--green)">Biggest opportunity</div><p style="margin:0">{_e(opp)}</p></div></div>')
    strip = (f'<div class="oppgrid" style="margin-top:22px">{stat_html}</div>{strip_src}') if stat_html else ""
    intro = _clean_prose(nv.get("exec_intro"), allowed, d.get("diagnosis") or
                         "Here's a clear read on how your business shows up online today — and where the growth is.")
    headline = _clean_prose(nv.get("exec_headline"), allowed, "Where you stand, and where the growth is")
    body = (f'<p class="sec-intro">{_e(intro)}</p>'
            f'<div class="scorewrap card" style="padding:26px 28px 20px;margin-top:8px">{_gauge((h or {}).get("overall"))}'
            f'<div class="bars">{bars}{illus}</div></div>{strip}{pillars}')
    return (f'<section><div class="wrap"><div class="eyebrow">Executive Summary</div>'
            f'<h2 class="sec">{_e(headline)}</h2>{body}</div></section>')


def _methodology(m, nv, allowed) -> str:
    intro = _clean_prose(nv.get("methodology_intro"), allowed,
                         "Every finding below is tied to something we could open, read or search for ourselves.")
    left = ['Your website — pages, titles, headings, structure and the enquiry path',
            'Your on-page SEO signals and how Google reads each page',
            'The real Google searches your customers type in your category']
    right = ['The competitors who show up when your future customers search',
            'Your Google Ads presence (or absence) in the Transparency Center',
            'How ready your site is for Google &amp; AI answer engines']
    def ul(items):
        return '<ul class="clean">' + "".join(f'<li>{i}</li>' for i in items) + '</ul>'
    body = (f'<div class="grid g2" style="margin-top:20px"><div class="card">{ul(left)}</div>'
            f'<div class="card">{ul(right)}</div></div>'
            '<p class="small muted" style="margin-top:16px">Where a figure isn\'t published we say so rather than '
            'invent one; external statistics are attributed. Estimated figures are labelled as such.</p>')
    return _sec("Methodology — what we actually reviewed", "Nothing in this report is guessed.", intro, body)


def _findings(seeds, nv, allowed) -> str:
    if not seeds:
        return ""
    written = nv.get("findings") if isinstance(nv.get("findings"), list) else []
    HEADLINE = 4   # ruthless focus: a few winnable fixes lead; the rest are secondary, not a 30-point dump

    def _title(i, s):
        w = written[i - 1] if i - 1 < len(written) and isinstance(written[i - 1], dict) else {}
        return _clean_prose(w.get("title"), allowed, s["hint"][:1].upper() + s["hint"][1:])

    cards = ""
    for i, s in enumerate(seeds[:HEADLINE], 1):
        w = written[i - 1] if i - 1 < len(written) and isinstance(written[i - 1], dict) else {}
        title = _title(i, s)
        obs = _clean_prose(w.get("obs"), allowed, "")
        cost = _clean_prose(w.get("cost"), allowed,
                            "This quietly sends ready-to-buy customers to competitors instead of you.")
        fix = _clean_prose(w.get("fix"), allowed,
                           "We'd close this as part of the plan below — it's a known, fixable gap.")
        sev = "sev-high" if s["sev"] == "high" else "sev-med"
        tag = '<span class="tag high">Priority</span>' if s["sev"] == "high" else '<span class="tag med">Opportunity</span>'
        obs_html = f'<p class="obs">{_e(obs)}</p>' if obs else ""
        cards += (
            f'<div class="finding {sev}"><div class="fhead"><div class="fnum">{i}</div>'
            f'<h3>{_e(title)}</h3>{tag}</div>{obs_html}'
            f'<div class="row"><div class="miniblock cost"><div class="ml">Why it costs you</div><p>{_e(cost)}</p></div>'
            f'<div class="miniblock fix"><div class="ml">The fix</div><p>{_e(fix)}</p></div></div>'
            f'<div class="evidence"><b>Evidence:</b> {s["evidence"]}</div></div>')
    # secondary — smaller gaps kept available but out of the headline
    rest = ""
    for j, s in enumerate(seeds[HEADLINE:], HEADLINE + 1):
        rest += (f'<li><b>{_e(_title(j, s))}</b><span>{s["evidence"]}</span></li>')
    rest_html = ('<div class="alsofix"><div class="subttl">Also on our list — smaller gaps we\'d mop up next</div>'
                 f'<ul class="alsolist">{rest}</ul></div>') if rest else ""
    n_head = min(HEADLINE, len(seeds))
    return (f'<section><div class="wrap"><div class="eyebrow">Deep Findings</div>'
            f'<h2 class="sec">The {n_head} fixes that will move the needle</h2>'
            f'<p class="sec-intro">We could hand you a 30-point list. Instead, here are the few that actually '
            f'cost you customers — what we saw, why it leaks revenue, and the fix.</p>'
            f'<div style="margin-top:22px">{cards}</div>{rest_html}</div></section>')


def _kw_table(rows, cols):
    """cols = list of (header, fn, numeric). Renders only if rows exist."""
    if not rows:
        return ""
    head = "".join(f'<th{" class=num" if num else ""}>{h}</th>' for h, _, num in cols)
    body = ""
    for r in rows:
        body += "<tr>" + "".join(
            f'<td{" class=num" if num else ""}>{fn(r)}</td>' for _, fn, num in cols) + "</tr>"
    return f'<div class="tablewrap"><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>'


def _visibility(m, nv, allowed) -> str:
    opp = m.get("opportunity") or {}
    seo = m.get("seo") or {}
    vis = opp.get("est_org_traffic")
    ranked = opp.get("org_keywords") or ((m.get("universe") or {}).get("totals") or {}).get("ranked")
    if not (vis or ranked or seo.get("proof_winning")):
        return ""
    chips = ""
    if vis:
        chips += f'<div class="chip"><b>{_traffic_range(vis)}</b><span>visitors/mo from Google (unpaid, est.)</span></div>'
    if ranked:
        chips += f'<div class="chip"><b>{_n(ranked)}</b><span>searches you currently show for</span></div>'
    chip_html = (f'<div class="chips">{chips}</div>{_src("Estimated from your live Google rankings — search volume × position CTR (DataForSEO).")}'
                 if chips else "")
    proof = _kw(seo.get("proof_winning"), 6)
    table = ""
    if proof:
        table = ('<div class="subttl">Searches you already rank near the top for</div>'
                 + _kw_table(proof, [
                     ("Search", lambda r: _e(r.get("keyword")), False),
                     ("Where you rank", lambda r: "#" + _n(r.get("position")) if r.get("position") else "—", True),
                     ("Searches / mo", lambda r: _n(r.get("search_volume")) if r.get("search_volume") else "—", True),
                 ])
                 + _src("Source: your Google rankings + Google search volumes (DataForSEO)."))
    intro = _clean_prose(nv.get("intro_visibility"), allowed,
                         "Here's where you already show up on Google — proof the site can rank when it's pointed right.")
    return _sec("Your visibility", "Where you show up on Google today", intro, chip_html + table)


def _money_tables(m, nv, allowed) -> str:
    seo = m.get("seo") or {}
    near = _kw(seo.get("money_keywords"), 8)
    # the "competitors rank for this" table shows only CONFIRMED competitor-gap terms; discovered demand
    # (where we didn't verify a specific rival ranks) is honestly reserved for the full search-picture table.
    gap = _kw([r for r in (m.get("keyword_gap") or []) if not r.get("discovered")], 8)
    if not (near or gap):
        return ""
    body = ""
    if near:
        body += ('<div class="subttl">One push from page one (you\'re already on page 2)</div>'
                 + _kw_table(near, [
                     ("Search", lambda r: _e(r.get("keyword")), False),
                     ("You rank", lambda r: "#" + _n(r.get("position")) if r.get("position") else "—", True),
                     ("Searches / mo", lambda r: _n(r.get("search_volume") or r.get("volume")) if (r.get("search_volume") or r.get("volume")) else "—", True),
                 ]))
        body += _src("Source: Google search volume × CPC (DataForSEO). Your own brand terms and competitors' brand names are excluded.")
    if gap:
        body += ('<div class="subttl" style="margin-top:16px">You don\'t show at all — competitors do</div>'
                 + _kw_table(gap, [
                     ("Search", lambda r: _e(r.get("keyword")), False),
                     ("Searches / mo", lambda r: _n(r.get("volume") or r.get("search_volume")) if (r.get("volume") or r.get("search_volume")) else "—", True),
                 ])
                 + _src("Source: searches your rivals rank for and you don't (DataForSEO). Rivals' brand names and buy-a-product terms removed."))
    ticket = (m.get("revenue") or {}).get("avg_ticket")
    if ticket:
        body += ('<p class="reframe" style="margin-top:16px">Don\'t be fooled by small search counts. In your '
                 'line of work you don\'t need thousands of clicks — a handful of people searching for exactly '
                 f'what you do, ready to buy, can each turn into a job worth {_fmt_money(ticket)}. A few of these a '
                 'month is real money.</p>')
    intro = _clean_prose(nv.get("intro_money"), allowed,
                         "These are the searches your customers actually type — and where the demand is going instead.")
    return _sec("The gap", "The money searches you're missing", intro, body)


def _clusters_funnel(m, nv, allowed) -> str:
    uni = m.get("universe") or {}
    clusters = (uni.get("clusters") or [])[:8]
    funnel = uni.get("funnel") or {}
    if not (clusters or funnel):
        return ""
    body = ""
    if clusters:
        rows = ""
        for c in clusters:
            top = c.get("top") or []
            ex = ", ".join([t.get("keyword") for t in top[:3] if t.get("keyword")])
            label = c.get("label") or (top[0].get("keyword") if top else "")
            if not label or not c.get("volume"):
                continue
            rows += (f'<tr><td><b>{_e(label)}</b>{("<span class=ex>"+_e(ex)+"</span>") if ex else ""}</td>'
                     f'<td class="num">{_n(c.get("volume"))}</td>'
                     f'<td class="num"><span class="yes">{_n(c.get("ranked") or 0)}</span></td>'
                     f'<td class="num"><span class="no">{_n(c.get("gap") or 0)}</span></td></tr>')
        if rows:
            body += ('<div class="tablewrap"><table><thead><tr><th>Product / service area</th>'
                     '<th class="num">Searches/mo</th><th class="num">You rank</th>'
                     f'<th class="num">You\'re missing</th></tr></thead><tbody>{rows}</tbody></table></div>')
    # funnel tiers
    tiers = [("TOFU", "Discovering the problem"), ("MOFU", "Comparing options"), ("BOFU", "Ready to buy")]
    frows = ""
    for keyk, lab in tiers:
        f = funnel.get(keyk) or {}
        if not f.get("volume"):
            continue
        frows += (f'<div class="bar"><div class="top"><span>{lab}</span>'
                  f'<b>{_n(f.get("ranked") or 0)}<i>/{_n(f.get("keywords") or 0)} searches</i></b></div>'
                  f'<div class="track"><span class="fill" style="width:{_ratio(f.get("ranked"), f.get("keywords"))}%;'
                  f'background:var(--forest-2)"></span></div></div>')
    if frows:
        body += f'<div class="subttl" style="margin-top:18px">How much of each buying stage you cover</div><div class="bars">{frows}</div>'
    if not body:
        return ""
    body += _src("Grouped from the real searches in your category and mapped to buyer intent (DataForSEO search volumes).")
    intro = _clean_prose(nv.get("intro_areas"), allowed,
                         "Grouped by what your customers are actually looking for, from first research to ready-to-buy.")
    return _sec("Your customers' searches", "Your customers' search areas", intro, body)


def _seed_cluster_strategy(m, nv, allowed) -> str:
    """The SEED-KEYWORD → CLUSTER table for a domain with little/no SEO footprint of its own. Every figure is
    REAL Google keyword-database demand (search volume + cost-per-click) discovered from the business's
    service/product seeds — so the audit shows the SHAPE of the market's demand instead of an empty section.
    Self-gating: renders only when seed clusters exist (otherwise the honest limited-data state stands alone)."""
    clusters = [c for c in (m.get("seed_clusters") or [])
                if c.get("label") and c.get("volume") and (c.get("label") or "").lower() != "long-tail / other"]
    if not clusters:
        return ""
    rows = ""
    for c in clusters[:8]:
        ex = ", ".join((c.get("examples") or [])[:3])
        cpc = c.get("avg_cpc") or 0
        rows += (f'<tr><td><b>{_e(c.get("label"))}</b>{("<span class=ex>"+_e(ex)+"</span>") if ex else ""}</td>'
                 f'<td class="num">{_n(c.get("volume"))}</td>'
                 f'<td class="num">{_fmt_money(cpc) if cpc else "—"}</td></tr>')
    table = ('<div class="tablewrap"><table><thead><tr><th>Category your customers search for</th>'
             '<th class="num">Searches / mo</th><th class="num">Value / click</th></tr></thead>'
             f'<tbody>{rows}</tbody></table></div>'
             + _src("Grouped from the REAL Google searches in your category — monthly search volume and "
                    "cost-per-click come from DataForSEO's keyword database, which exists even though your own "
                    "site has no search footprint yet. 'Value/click' is what advertisers pay for that click — a "
                    "proxy for how commercial (ready-to-buy) the search is."))
    intro = _clean_prose(nv.get("intro_areas"), allowed,
                         "Your site barely appears in Google yet — but the demand is already there. Here's the "
                         "market you're not in front of, grouped into the categories your customers actually search.")
    return _sec("Your market's demand", "The searches your customers are already making — grouped", intro, table)


def _ratio(a, b):
    try:
        return max(3, min(100, round(100 * float(a or 0) / float(b or 1))))
    except Exception:
        return 3


def _benchmark(m, nv, allowed) -> str:
    comps = [c for c in (m.get("competitors") or []) if (c.get("domain") or c.get("name"))][:5]
    if not comps:
        return ""
    # scoreboard table (real per-competitor metrics)
    def cell(v, num=True):
        return _n(v) if (num and v is not None) else (_e(v) if v is not None else "—")
    rows = ""
    metrics = [("Est. visitors / mo", lambda c: _traffic_range(c.get("est_traffic")) if c.get("est_traffic") else "—"),
               ("Keywords ranked", lambda c: _n(c.get("organic_keywords")) if c.get("organic_keywords") else "—"),
               ("Avg. position", lambda c: "#" + _n(c.get("avg_position")) if c.get("avg_position") else "—")]
    header = '<th>&nbsp;</th><th class="you">You</th>' + "".join(
        f'<th>{_e((c.get("domain") or c.get("name")))}</th>' for c in comps)
    you = m.get("opportunity") or {}
    you_vals = {"Est. visitors / mo": (_traffic_range(you.get("est_org_traffic")) if you.get("est_org_traffic") else "—"),
                "Keywords ranked": (_n(you.get("org_keywords")) if you.get("org_keywords") else "—"),
                "Avg. position": "—"}
    for label, fn in metrics:
        rows += (f'<tr><th>{label}</th><td class="you">{you_vals.get(label, "—")}</td>'
                 + "".join(f'<td>{fn(c)}</td>' for c in comps) + "</tr>")
    table = (f'<div class="tablewrap"><table><thead><tr>{header}</tr></thead><tbody>{rows}</tbody></table></div>'
             + _src("Estimated from search rankings — organic visits are modelled from ranking positions × search "
                    "volume (DataForSEO); small single-digit estimates are shown as bands, not exact counts."))
    # share of visibility bars (as a % share of the field — not a misleading raw traffic count)
    sov_bars = ""
    ours = m.get("our_traffic") or you.get("est_org_traffic") or 0
    field = [((c.get("domain") or c.get("name")), (c.get("est_traffic") or 0)) for c in comps]
    field.append(("You", ours))
    total_field = sum(v for _, v in field) or 0
    mx = max([v for _, v in field] + [1])
    palette = ["#1f4a3a", "#2f6b52", "#2f7d52", "#8aa596", "#8aa596"]
    field_sorted = sorted(field, key=lambda x: -x[1])
    for idx, (nm, v) in enumerate(field_sorted):
        is_you = (nm == "You")
        w = max(6, round(100 * v / mx))
        col = "#a6432c" if is_you else palette[min(idx, len(palette) - 1)]
        share = round(100 * v / total_field) if total_field else 0
        lab = (f'{share}% share' if v else "—")
        sov_bars += (f'<div class="rankrow"><div class="name{" you" if is_you else ""}">{_e(nm)}</div>'
                     f'<div class="rtrack"><div class="rfill" style="width:{w}%;background:{col}">{lab}</div></div></div>')
    # competitor tension — the one line that stings: how far ahead the strongest rival is
    tension = ""
    top_rival = max(((c.get("domain") or c.get("name")), (c.get("est_traffic") or 0)) for c in comps) \
        if comps else (None, 0)
    if ours and top_rival[1] and top_rival[1] > ours:
        mult = top_rival[1] / max(1, ours)
        if mult >= 1.5:
            tension = (f'<div class="tension"><b>{_e(top_rival[0])}</b> is pulling roughly '
                       f'<b>{mult:.0f}× the search visibility you are</b> — that gap is customers, every month.</div>')
    elif ours == 0 and top_rival[1]:
        tension = (f'<div class="tension"><b>{_e(top_rival[0])}</b> is capturing this demand while your site is '
                   'barely visible in these searches — that gap is customers, every month.</div>')
    sov_html = ""
    if any(v for _, v in field):
        sov_html = ('<h3 style="margin:32px 0 14px;font-size:20px">Share of visibility</h3>'
                    f'{tension}<div class="card">{sov_bars}'
                    f'{_src("Each bar is that site\'s estimated share of the total organic visits across you and the rivals we found (DataForSEO). A directional read of who the demand flows to, not exact counts.")}</div>')
    intro = _clean_prose(nv.get("intro_benchmark"), allowed,
                         "The businesses your future customers find when they search — and how you compare on the numbers that matter.")
    return _sec("Competitor Benchmark", "You vs. the businesses customers find first", intro, table + sov_html)


def _content_gap(m, nv, allowed) -> str:
    rows = (m.get("content_gap") or [])[:6]
    rows = [r for r in rows if (r.get("topic") or r.get("page"))]
    if not rows:
        return ""
    cards = ""
    for r in rows:
        topic = r.get("topic") or r.get("page")
        vol = r.get("total_volume")
        kwc = r.get("keyword_count")
        ex = ", ".join((r.get("example_keywords") or [])[:3])
        meta = []
        if vol:
            meta.append(f'{_n(vol)} searches/mo')
        if kwc:
            meta.append(f'{_n(kwc)} keywords')
        cards += (f'<div class="card"><b>{_e(topic)}</b>'
                  f'{("<div class=ex>"+_e(ex)+"</div>") if ex else ""}'
                  f'<div class="small muted" style="margin-top:6px">{" · ".join(meta)}</div></div>')
    intro = _clean_prose(nv.get("intro_content"), allowed,
                         "Topics your competitors publish and rank for — each one an entry point into your site you don't have yet.")
    return _sec("Content gaps", "Content your competitors own and you don't", intro,
                f'<div class="grid g3" style="margin-top:20px">{cards}</div>'
                + _src("Source: topics your rivals rank for and you have no page for (DataForSEO)."))


def _ads_block(m, nv, allowed) -> str:
    ads = m.get("ads") or {}
    running = ads.get("running")
    if not running:
        body = ('<div class="oppcard" style="background:var(--paper-2);color:var(--ink);border:1px solid var(--line)">'
                '<h3 style="color:var(--ink);margin-top:0">You\'re not running Google Ads right now</h3>'
                '<p style="color:var(--ink-2);margin:0">That\'s the fastest way to appear at the very top for the '
                '"ready to buy" searches while your unpaid rankings grow — you\'re leaving that top slot to competitors.</p></div>')
        intro = _clean_prose(nv.get("intro_ads"), allowed, "")
        return _sec("Google Ads", "Your Google Ads", intro, body)
    chips = []
    if ads.get("years_active"):
        chips.append((f"{_n(ads['years_active'])} yrs", "advertising consistently"))
    if ads.get("count"):
        chips.append((_n(ads["count"]), "live ads right now"))
    for k, v in (ads.get("formats") or {}).items():
        chips.append((_n(v), _e(k)))
    ch = "".join(f'<div class="chip"><b>{v}</b><span>{lab}</span></div>' for v, lab in chips)
    # creative gallery (only items with an image)
    gallery = ""
    imgs = [c for c in (ads.get("creatives") or []) if c.get("image_data") or c.get("preview_image")][:4]
    if imgs:
        cells = "".join(f'<img src="{_e(c.get("image_data") or c.get("preview_image"))}" alt="" '
                        'style="width:100%;border:1px solid var(--line);border-radius:10px;display:block"/>' for c in imgs)
        gallery = f'<div class="grid g3" style="margin-top:16px">{cells}</div>'
    intro = _clean_prose(nv.get("intro_ads"), allowed, "")
    return _sec("Google Ads", "Your Google Ads", intro,
                f'<div class="chips">{ch}</div>{gallery}'
                + _src("Source: Google Ads Transparency Center (Google's own public record of the ads you run)."))


def _geo(m, nv, allowed) -> str:
    geo = m.get("geo_aeo") or {}
    score = geo.get("score")
    checks = geo.get("checks") or []
    if score is None and not checks:
        return ""
    bar = ""
    if score is not None:
        _, col = _band(score)
        s = max(0, min(100, int(round(float(score)))))
        bar = (f'<div class="bar" style="margin-bottom:16px"><div class="top"><span>AI-search readiness</span>'
               f'<b style="color:{col}">{s}<i>/100</i></b></div><div class="track">'
               f'<span class="fill" style="width:{max(3,s)}%;background:{col}"></span></div></div>')
    have = [c.get("name") for c in checks if c.get("pass")][:6]
    missing = [c.get("note") or c.get("name") for c in checks if not c.get("pass")][:6]
    cols = ""
    if have:
        cols += ('<div><div class="glab">Already in place</div><ul class="glist">'
                 + "".join(f'<li class="c">{_e(x)}</li>' for x in have) + '</ul></div>')
    if missing:
        cols += ('<div><div class="glab">Simple things you\'re missing</div><ul class="glist">'
                 + "".join(f'<li class="x">{_e(x)}</li>' for x in missing) + '</ul></div>')
    body = bar + (f'<div class="gcols">{cols}</div>' if cols else "")
    body += _src("Source: automated on-page checks of your own website (structured data, headings, entity signals).")
    intro = _clean_prose(nv.get("intro_ai"), allowed,
                         "How ready your site is to be picked up by Google's AI answers and other AI search tools.")
    return _sec("Google &amp; AI", "Getting found by Google &amp; AI", intro, body)


def _backlinks(m, nv, allowed) -> str:
    bg = m.get("backlink_gap") or {}
    if not isinstance(bg, dict) or not bg:
        return ""
    if bg.get("note") and bg.get("referring_domains") is None:
        body = f'<div class="note-b">{_e(bg.get("note"))}</div>'
    else:
        tiles = []
        for lab, k in (("Domain authority", "domain_rank"), ("Referring domains", "referring_domains"),
                       ("Total backlinks", "backlinks")):
            if bg.get(k) is not None:
                tiles.append(f'<div class="chip"><b>{_n(bg.get(k))}</b><span>{lab}</span></div>')
        if not tiles:
            return ""
        body = f'<div class="chips">{"".join(tiles)}</div>{_src("Source: DataForSEO backlink index.")}'
    intro = _clean_prose(nv.get("intro_backlinks"), allowed,
                         "The other sites linking to you — a big driver of how much Google trusts your domain.")
    return _sec("Authority", "Backlinks &amp; authority", intro, body)


def _revenue_opp(m, nv, allowed) -> str:
    opp = m.get("opportunity") or {}
    rev = m.get("revenue") or {}
    monthly = int(rev.get("monthly") or 0)
    grounded = int((opp.get("gap_capturable") or 0) + (opp.get("quickwin_value") or 0))
    if not monthly and not grounded:
        return ""
    # HERO — the recurring loss in the owner's own dollar terms (never the tiny SEO click value)
    stats = ""
    cap_txt = ""
    if monthly:
        stats = ('<div class="oppgrid">'
                 f'<div class="oppstat"><b>{_fmt_money(monthly)}</b><span>in new work going to competitors — every month</span></div>'
                 f'<div class="oppstat"><b>{_fmt_money(monthly*3)}</b><span>gone every quarter you wait</span></div>'
                 f'<div class="oppstat"><b>{_fmt_money(monthly*12)}</b><span>a year of work handed to rivals</span></div></div>')
        cap_txt = _assumptions_caption(rev, rev.get("addressable_traffic"))
    elif grounded:
        stats = ('<div class="oppgrid">'
                 f'<div class="oppstat"><b>{_fmt_money(grounded)}</b><span>in customer searches going to rivals — a month</span></div>'
                 f'<div class="oppstat"><b>{_fmt_money(grounded*3)}</b><span>a quarter you\'re leaving on the table</span></div>'
                 f'<div class="oppstat"><b>{_fmt_money(grounded*12)}</b><span>over a year of missed demand</span></div></div>')
        cap_txt = "Illustrative — realistically capturable search value (search volume × capture-rate × CPC, DataForSEO)."
    cap_html = f'<div class="srccap onopp">{_e(cap_txt)}</div>' if cap_txt else ""
    bridge = _clean_prose(nv.get("intro_revenue"), allowed,
                          "Here's how that number is built — from searches already happening, using deliberately "
                          "cautious rates, and only your live Google data. Nothing here is guessed.")
    # enquiry → job → dollars bridge — ALWAYS renders now (a ticket is always present, real or estimated).
    # Sub-1 sales are shown as an HONEST decimal + a plain-English cadence so the chain reconciles with the
    # dollar figure instead of the old "0 paying customers → A$1,935" contradiction.
    math = ""
    if monthly and rev.get("avg_ticket"):
        v2l = rev.get("visitor_to_lead") or 0.03
        l2s = rev.get("lead_to_sale") or 0.25
        lead_pct = max(1, int(round(v2l * 100)))
        sale_den = max(2, int(round(1 / l2s))) if l2s else 4
        sales = float(rev.get("sales_per_mo") or 0)
        cadence = _cadence(sales)
        tkt_note = ""
        if rev.get("avg_ticket_estimated"):
            bkt = rev.get("avg_ticket_bucket") or "business"
            tkt_note = (f' <span class="tktnote">— estimated from a typical {_e(bkt)} job; tell us your real '
                        'number and every figure here updates</span>')
        cadence_row = (f'<div class="mathnote">In plain terms: <b>{_e(cadence)}</b> won back from search — '
                       f'worth about {_fmt_money(monthly*12)} a year at this job value.</div>') if cadence else ""
        math = ('<h3 style="margin:26px 0 12px">How that adds up — in your terms, not SEO jargon</h3>'
                f'<div class="mathrow"><span>Ready-to-buy searchers we\'d aim to win back each month</span><span>{_n(rev.get("addressable_traffic"))}</span></div>'
                f'<div class="mathrow"><span>Turn into enquiries (a cautious {lead_pct} in 100)</span><span>{_n1(rev.get("leads_per_mo"))}</span></div>'
                f'<div class="mathrow"><span>Become paying customers (about 1 in {sale_den} enquiries)</span><span>{_n1(rev.get("sales_per_mo"))}</span></div>'
                f'<div class="mathrow"><span>At a job worth {_fmt_money(rev.get("avg_ticket"))}{tkt_note}</span><span>{_fmt_money(monthly)} / mo</span></div>'
                f'{cadence_row}')
    # realistic-gain payoff — the "if you fix this, here's the credible upside" the owner actually wants
    payoff = ""
    if monthly:
        half = round(monthly / 2)
        payoff = ('<div class="payoff"><div class="pl">If we fix this</div>'
                  f'<p>You won\'t win every one of these searches — nobody does. But capture even <b>half</b> and '
                  f'that\'s about <b>{_fmt_money(half)}/mo</b> ({_fmt_money(half*12)}/yr) in new work, built over '
                  '6–12 months as your rankings compound and hold.</p></div>')
    disc = ('<p class="disclaim">Careful, deliberately conservative estimates built from live Google search data '
            'for your site and your competitors — a way to size the prize, not a promise. This is the demand you '
            'lose again every month it isn\'t fixed; the wins build over roughly 6–12 months as the work compounds.</p>')
    body = f'<div class="oppcard">{stats}{cap_html}<p>{_e(bridge)}</p>{math}{payoff}{disc}</div>'
    return _sec("The Revenue Opportunity", "What you lose every month competitors show up first", "", body)


def _search_picture(m, nv, allowed) -> str:
    """The COMPLETE, auditable keyword universe — EVERY money / gap / informational search in one
    transparent table so the revenue math is fully checkable (nothing hidden; competitor brand names and
    $0 navigational terms already scrubbed out upstream). Discovered demand is marked 'new find'."""
    uni = m.get("universe") or {}
    pool = [r for r in (uni.get("keywords") or [])
            if r.get("keyword") and (r.get("vol") or 0) >= 20]
    if not pool:
        return ""
    rev = m.get("revenue") or {}
    comps = m.get("competitors") or []
    top_comp = (comps[0].get("domain") or comps[0].get("name")) if comps else None

    def _rank_cell(r):
        return ("#" + _n(r.get("pos"))) if r.get("pos") else '<span class="no">Not ranking</span>'

    def _comp_cell(r):
        if r.get("ranked") or r.get("discovered"):
            return "—"                       # you already rank / discovered via demand, not a rival's page
        c = r.get("competitor") or top_comp
        return _e(c) if c else "—"

    def _val_cell(r):
        v = int(r.get("est_value") or 0)
        return _fmt_money(v) if v else "—"

    def _type_cell(r):
        if r.get("stage") == "TOFU":
            return '<span class="ktag info">Info</span>'
        if r.get("ranked"):
            return '<span class="ktag money">Money</span>'
        tag = '<span class="ktag gap">Missing</span>'
        if r.get("discovered"):
            tag += '<span class="ktag new">new find</span>'
        return tag

    def _table(rows):
        tot_vol = tot_val = 0
        body = ""
        for r in sorted(rows, key=lambda x: (int(x.get("est_value") or 0), int(x.get("vol") or 0)), reverse=True):
            tot_vol += int(r.get("vol") or 0)
            tot_val += int(r.get("est_value") or 0)
            body += ("<tr>"
                     f'<td>{_e(r.get("keyword"))}</td>'
                     f'<td class="num">{_n(r.get("vol"))}</td>'
                     f'<td class="num">{_rank_cell(r)}</td>'
                     f'<td>{_comp_cell(r)}</td>'
                     f'<td class="num">{_val_cell(r)}</td>'
                     f'<td>{_type_cell(r)}</td></tr>')
        body += ('<tr class="krow-tot"><td><b>Total</b></td>'
                 f'<td class="num"><b>{_n(tot_vol)}</b></td><td class="num">—</td><td>—</td>'
                 f'<td class="num"><b>{_fmt_money(tot_val)}</b></td><td>—</td></tr>')
        head = ('<thead><tr><th>Search</th><th class="num">Searches/mo</th><th class="num">You rank</th>'
                '<th>Top competitor</th><th class="num">Est. value/mo</th><th>Type</th></tr></thead>')
        return f'<div class="tablewrap"><table>{head}<tbody>{body}</tbody></table></div>'

    commercial = [r for r in pool if r.get("stage") in ("MOFU", "BOFU")]
    info = [r for r in pool if r.get("stage") == "TOFU"]
    body = ""
    if commercial:
        body += ('<div class="subttl">Commercial searches — ready-to-buy &amp; comparison</div>'
                 + _table(commercial))
    if info:
        body += ('<div class="subttl" style="margin-top:16px">Informational searches — research &amp; how-to</div>'
                 + _table(info))
    # tie the table to the cover hero using the EXACT model figures (no re-derivation)
    monthly = int(rev.get("monthly") or 0)
    addr = int(rev.get("addressable_traffic") or 0)
    if monthly and addr and rev.get("avg_ticket"):
        v2l = int(round((rev.get("visitor_to_lead") or 0.03) * 100))
        l2s = int(round((rev.get("lead_to_sale") or 0.25) * 100))
        body += (f'<p class="reframe" style="margin-top:16px">The figure on the cover comes straight from this '
                 f'table: the <b>{_n(addr)}</b> ready-to-buy visits/mo we’d aim to win from the commercial '
                 f'searches above (the page-2 ‘money’ terms plus the ‘missing’ gap, at an assumed '
                 f'page-1 click-through) × <b>{v2l}%</b> become enquiries × <b>{l2s}%</b> become customers '
                 f'× your job value ({_fmt_money(rev.get("avg_ticket"))}) = <b>{_fmt_money(monthly)}/mo</b>.</p>')
    body += _src("Every search here is real Google search-volume data (DataForSEO); ‘searches/mo’ is measured, "
                 "while traffic and value are conservative estimates (search volume × position click-through × CPC). "
                 "Competitor brand names and $0 navigational terms are excluded. ‘New find’ = market demand we "
                 "discovered that isn’t in your or your rivals’ current rankings.")
    intro = _clean_prose(nv.get("intro_picture"), allowed,
                         "Every search we found in your category — what you rank for, what you’re missing, and "
                         "what it’s worth — laid out in full so the numbers above are yours to check.")
    return _sec("The complete search picture", "Every search, and what it’s worth — in full view",
                intro, body)


def _plan_phases(m, nv, allowed) -> str:
    qw = (m.get("quick_wins") or [])
    gp = (m.get("growth_plan") or [])
    steps = (m.get("recommendation") or {}).get("steps") or []
    if not (qw or gp or steps):
        return ""
    def items(rows, fallback):
        rows = [r for r in rows if (r.get("title") if isinstance(r, dict) else None)]
        if not rows:
            return fallback
        return "".join(f'<li><b>{_e(r.get("title"))}</b>{(" — "+_e(r.get("detail"))) if r.get("detail") else ""}</li>' for r in rows[:4])
    p1 = items(qw, "".join(f'<li><b>{_e(s.get("title") if isinstance(s,dict) else s[0])}</b></li>' for s in steps[:3]) or "<li>Fix the highest-impact gaps first.</li>")
    mid = gp[:len(gp)//2] if gp else []
    late = gp[len(gp)//2:] if gp else []
    p2 = items(mid, "<li>Build the pages and content for the searches you're missing.</li>")
    p3 = items(late, "<li>Compound the wins — content rhythm, reviews, and double down on what works.</li>")
    body = (
        '<div class="phase"><div class="ph">Days 1–30<small>Stop the leaks</small></div>'
        f'<ul>{p1}</ul></div>'
        '<div class="phase"><div class="ph">Days 31–60<small>Build the front doors</small></div>'
        f'<ul>{p2}</ul></div>'
        '<div class="phase"><div class="ph">Days 61–90<small>Compound the growth</small></div>'
        f'<ul>{p3}</ul></div>'
        '<p class="small muted" style="margin-top:16px">The work starts on day one, but search is a compounding '
        'game — expect meaningful movement in your rankings and enquiries over roughly 6–12 months, building '
        'from there. Anyone promising overnight results isn\'t being straight with you.</p>')
    intro = _clean_prose(nv.get("plan_intro"), allowed, "Sequenced so the fastest wins come first.")
    return _sec("90-Day Action Plan", "A clear path, fastest wins first", intro, body)


# --------------------------------------------------------------------------- growth strategy ---
# The tailored "where to start" strategy. GROUNDED in the audit model (industry, location, competitors,
# money/gap/discovered keywords, ads, tech/AI-readiness, thin-ness). For a THIN-data prospect this becomes
# the centrepiece — turning "we found little search data" into a concrete roadmap instead of an apology.
# ONE Anthropic call (Opus via _claude_text); strict JSON out; a data-driven deterministic fallback covers a
# missing key or any failure. The LLM writes WORDS ONLY — the same anti-fabrication guard (_clean_prose)
# strips any invented number, so nothing here is guessed.
_STRAT_SYS = (
    "You are a senior growth strategist writing the 'where to start' section of a premium, confidential "
    "growth audit for an Australian small-business owner. You are given the REAL audit data for ONE specific "
    "business. Write a CONCRETE, TAILORED growth strategy for THIS business — name their industry/services, "
    "their location, their actual competitors and the real searches in the data. Build the strategy AROUND "
    "'their_market_search_clusters' (the product/service themes their customers actually search for) — name "
    "the real clusters and shape the roadmap so they systematically own those categories. When the data says "
    "public search data is thin for this niche, LEAN ON these clusters as the plan's backbone rather than "
    "apologising for missing domain metrics. Never generic filler, never a boilerplate list. HARD RULES: (1) "
    "NEVER write any specific number, percentage, dollar amount or count "
    "— describe things in words (figures are handled elsewhere). (2) Focus purely on how they win more "
    "customers: positioning, the FEW channels that genuinely fit THEIR situation, and a practical first 90 "
    "days. (3) Be specific and actionable — every channel and step is something we could start this month. "
    "(4) Australian spelling. Keep every field tight — one or two sentences, no fluff. "
    "Return ONLY a JSON object with EXACTLY these keys: "
    "positioning (1-2 sentences: the core opportunity and how a business like theirs should position in their "
    "market), channels (an ARRAY of 3-5 objects — ONLY the channels that actually fit THEIR industry and "
    "data; do NOT list every possible channel — each {name: short channel name, no more than 6 words; why: 1 "
    "sentence on why it fits THEM specifically; first_step: 1 concrete first action}), roadmap (an object "
    "with keys d30, d60, d90, each an ARRAY of 2-4 short, concrete steps for that phase). Output ONLY the JSON."
)


def _strategy_brief(m: dict, name: str, thin: bool) -> dict:
    """The grounding facts fed to the strategy LLM — pulled straight from the (scrubbed) audit model."""
    biz = m.get("business") or {}
    seo = m.get("seo") or {}
    geo = m.get("geo_aeo") or {}
    tech = m.get("tech") or {}
    comps = [(c.get("domain") or c.get("name")) for c in (m.get("competitors") or [])
             if (c.get("domain") or c.get("name"))][:5]
    money_kw = [r.get("keyword") for r in (seo.get("money_keywords") or []) if r.get("keyword")][:8]
    gap_kw = [r.get("keyword") for r in (m.get("keyword_gap") or [])
              if r.get("keyword") and not r.get("discovered")][:10]
    discovered = [r.get("keyword") for r in (m.get("keyword_gap") or [])
                  if r.get("keyword") and r.get("discovered")][:8]
    ai_missing = [(c.get("note") or c.get("name")) for c in (geo.get("checks") or [])
                  if not c.get("pass") and (c.get("note") or c.get("name"))][:5]
    # SEED-KEYWORD CLUSTERS — the market's demand grouped into product/service themes. Present even when the
    # domain has NO SEO footprint of its own (built from keyword-demand discovery), so the strategy is always
    # grounded in the REAL searches their customers make, not generic filler. Prefer the rich universe
    # clusters (with ranked/gap coverage); fall back to the seed-only clusters for a no-data domain.
    service_clusters = []
    for c in ((m.get("universe") or {}).get("clusters") or [])[:8]:
        lab = c.get("label")
        if not lab or (lab or "").lower() == "long-tail / other" or not c.get("volume"):
            continue
        ex = [t.get("keyword") for t in (c.get("top") or [])[:3] if t.get("keyword")]
        service_clusters.append({"service_area": lab, "example_searches": ex,
                                 "you_already_rank_for_some": bool(c.get("ranked")),
                                 "you_are_missing_most": (c.get("gap") or 0) > (c.get("ranked") or 0)})
    if not service_clusters:
        for c in (m.get("seed_clusters") or [])[:8]:
            lab = c.get("label")
            if lab and (lab or "").lower() != "long-tail / other" and c.get("volume"):
                service_clusters.append({"service_area": lab,
                                         "example_searches": (c.get("examples") or [])[:3]})
    return {
        "business_name": name,
        "industry": biz.get("industry"),
        "sub_industry": biz.get("sub_industry"),
        "location": biz.get("location"),
        "website": biz.get("website"),
        "already_running_google_ads": bool((m.get("ads") or {}).get("running")),
        "site_is_secure_https": tech.get("https"),
        "money_keywords_they_nearly_rank_for": money_kw,
        "searches_competitors_win_that_they_miss": gap_kw,
        "extra_buyer_demand_we_discovered": discovered,
        # the spine of the strategy: their market's demand as product/service clusters
        "their_market_search_clusters": service_clusters,
        "competitors_found": comps,
        "google_and_ai_readiness_gaps": ai_missing,
        "public_search_data_is_thin_for_this_niche": bool(thin),
        "diagnosis": m.get("diagnosis"),
    }


def _fallback_strategy(m: dict, name: str, thin: bool) -> dict:
    """A genuinely useful, data-driven default strategy built from the model — used when the key is missing
    or the LLM call/parse fails. References the real industry, location, money/gap keywords, competitor and
    tech signals so even the fallback is specific to THIS business, never a stub. Contains no numbers."""
    biz = m.get("business") or {}
    seo = m.get("seo") or {}
    tech = m.get("tech") or {}
    industry = (biz.get("industry") or "").strip()
    sub = (biz.get("sub_industry") or "").strip()
    location = (biz.get("location") or "").strip()
    ads_running = bool((m.get("ads") or {}).get("running"))
    money_kw = [r.get("keyword") for r in (seo.get("money_keywords") or []) if r.get("keyword")]
    gap_kw = [r.get("keyword") for r in (m.get("keyword_gap") or []) if r.get("keyword")]
    kws = [k for k in (money_kw + gap_kw) if k][:3]
    ind = industry or sub or "local business"
    where = f" in {location}" if location else ""
    kw_phrase = ", ".join(f"“{k}”" for k in kws) if kws else "the exact services you offer"
    # cluster labels — the product/service themes their market actually searches for (works with no SEO data)
    cluster_labels = [c.get("label") for c in ((m.get("universe") or {}).get("clusters") or [])
                      if c.get("label") and (c.get("label") or "").lower() != "long-tail / other"][:5]
    if not cluster_labels:
        cluster_labels = [c.get("label") for c in (m.get("seed_clusters") or [])
                          if c.get("label") and (c.get("label") or "").lower() != "long-tail / other"][:5]
    cats_phrase = ", ".join(cluster_labels) if cluster_labels else ""

    if cluster_labels:
        positioning = (
            f"For a {ind}{where}, the win isn't mass traffic — it's owning the category searches your customers "
            f"already make ({cats_phrase.lower()}) and being the most obvious, most-trusted choice when they do. "
            f"Claim those categories first and a few new jobs a month is real money.")
    else:
        positioning = (
            f"For a {ind}{where}, the win isn't mass traffic — it's owning the handful of high-intent searches "
            f"from people who are ready to buy, and being the most obvious, most-trusted choice in your area. Win "
            f"those and a few new jobs a month is real money.")

    channels = []
    channels.append({
        "name": "Google Business Profile & reviews",
        "why": ("For a local service business your Google Business Profile is often seen before your website, and "
                "a steady flow of recent reviews is the fastest way to win the local map results and earn trust."),
        "step": ("Fully complete and verify your profile — services, service area, photos and hours — then set "
                 "up a simple routine to ask every happy customer for a Google review."),
    })
    if cluster_labels:
        channels.append({
            "name": "Own your core service categories",
            "why": (f"Your market's demand groups into a handful of clear categories — {cats_phrase.lower()} — and "
                    "buyers search these by name. Owning a dedicated, well-built page for each is how you turn that "
                    "existing demand into enquiries."),
            "step": ("Build a focused, well-written page for each core category, written around the exact words "
                     "your customers use, so Google has an obvious, relevant result to show them."),
        })
    elif kws:
        channels.append({
            "name": "Rank for your money searches",
            "why": (f"A small number of buyer-ready searches like {kw_phrase} are worth far more to you than broad "
                    "traffic — each one can become a real job."),
            "step": ("Build a focused, well-written page for each of those searches so Google has an obvious, "
                     "relevant result to put in front of buyers."),
        })
    else:
        channels.append({
            "name": "A page for every service you sell",
            "why": ("Right now there's very little of your business in Google — a dedicated page for each service "
                    "gives Google and your buyers a clear reason to find and choose you."),
            "step": ("List every service you offer and give each its own clear page, written around the words your "
                     "customers actually use."),
        })
    if not ads_running:
        channels.append({
            "name": "Google Ads for ready-to-buy searches",
            "why": ("Ads put you at the very top for the searches that signal someone is ready to buy today, while "
                    "your unpaid rankings are still building."),
            "step": ("Start a tight campaign on your highest-intent service-and-location searches, pointed at the "
                     "matching service page."),
        })
    else:
        channels.append({
            "name": "Sharpen your Google Ads",
            "why": ("You're already advertising, so the leverage now is making every click land on a page built to "
                    "turn it into an enquiry rather than a bounce."),
            "step": ("Match each ad to a dedicated landing page for that exact service, and stop spending on "
                     "searches that never turn into jobs."),
        })
    channels.append({
        "name": "Local citations & directories",
        "why": ("Consistent listings across the directories and local sites Google and your customers trust build "
                "the local authority that lifts you in the map results."),
        "step": ("Get your business name, address and phone listed identically across the main Australian and "
                 "industry directories."),
    })
    channels.append({
        "name": "Referrals & repeat work",
        "why": ("Your past customers are your warmest, cheapest source of new work — a light, deliberate "
                "follow-up keeps you front of mind when they or a mate need you again."),
        "step": ("Set a simple reminder to check back in with past customers and make it easy for them to refer you."),
    })
    channels = channels[:5]

    d30 = [
        "Fully complete and verify your Google Business Profile, and start collecting fresh reviews.",
        "List every service you sell and note the exact words your customers use for each one.",
    ]
    if tech.get("https") is False:
        d30.append("Fix the basics on your site — get it loading securely so Google and buyers trust it.")
    d30.append("Pick the services worth the most to you and focus there first.")
    d60 = [
        (f"Build a clear, well-written page for each core category — {cats_phrase.lower()} — written around the "
         "words your customers search." if cluster_labels
         else "Build a clear, well-written page for each of your priority services."),
        "Get listed consistently across the main local and industry directories.",
    ]
    if not ads_running:
        d60.append("Launch a tight Google Ads campaign on your highest-intent searches to win jobs while rankings build.")
    else:
        d60.append("Point each of your ads at a dedicated page for that service so more clicks become enquiries.")
    d90 = [
        "Add a steady rhythm of content and keep the reviews coming so your rankings compound.",
        "Reconnect with past customers to unlock referrals and repeat work.",
        "Track which searches and pages bring enquiries, and double down on what works.",
    ]
    return {"positioning": positioning, "channels": channels,
            "roadmap": {"d30": d30, "d60": d60, "d90": d90}}


def _llm_strategy(key, model, m, name, thin) -> dict | None:
    """ONE guarded Anthropic (Opus) call → strict JSON strategy. None on any failure (caller falls back)."""
    if not key:
        return None
    try:
        brief = _strategy_brief(m, name, thin)
        txt = _claude_text(key, model, _STRAT_SYS,
                           "AUDIT DATA (ground the strategy in this; write words only, never numbers):\n"
                           + _json.dumps(brief, default=str)[:6000], max_tokens=2500).strip()
        if txt.startswith("```"):
            txt = txt.split("\n", 1)[1] if "\n" in txt else txt[3:]
            if txt.rstrip().endswith("```"):
                txt = txt.rstrip()[:-3]
        i, j = txt.find("{"), txt.rfind("}")
        data = _json.loads(txt[i:j + 1]) if i >= 0 and j > i else {}
        return data or None
    except Exception:
        return None


def _clean_strategy(data: dict, allowed: set, default_pos: str = "") -> dict | None:
    """Normalise + anti-fabrication-scrub the LLM strategy. Returns a clean {positioning, channels, roadmap}
    or None when it came back too sparse (so the caller uses the deterministic fallback instead)."""
    pos = _clean_prose(data.get("positioning"), allowed, default_pos or "")
    channels = []
    for c in (data.get("channels") or []):
        if not isinstance(c, dict):
            continue
        nm = _clean_prose(c.get("name"), allowed, "")
        why = _clean_prose(c.get("why"), allowed, "")
        step = _clean_prose(c.get("first_step") or c.get("step"), allowed, "")
        if nm and (why or step):
            channels.append({"name": nm, "why": why, "step": step})
    rm_in = data.get("roadmap") or {}
    roadmap = {}
    for k in ("d30", "d60", "d90"):
        items = []
        for it in (rm_in.get(k) or []):
            raw = it if isinstance(it, str) else (it.get("text") if isinstance(it, dict) else "")
            cl = _clean_prose(raw, allowed, "")
            if cl:
                items.append(cl)
        roadmap[k] = items
    if len(channels) >= 2 and any(roadmap.get(k) for k in ("d30", "d60", "d90")):
        return {"positioning": pos, "channels": channels, "roadmap": roadmap}
    return None


def _render_strategy(strat: dict, thin: bool) -> str:
    pos = strat.get("positioning")
    channels = strat.get("channels") or []
    roadmap = strat.get("roadmap") or {}
    pos_html = (f'<div class="strat-pos"><div class="sl">The opportunity</div><p>{_e(pos)}</p></div>') if pos else ""
    ch_cards = ""
    for c in channels[:6]:
        nm = c.get("name")
        if not nm:
            continue
        why = c.get("why"); step = c.get("step")
        why_html = f'<p class="why">{_e(why)}</p>' if why else ""
        step_html = f'<div class="step"><b>First step</b>{_e(step)}</div>' if step else ""
        ch_cards += f'<div class="chan"><h3>{_e(nm)}</h3>{why_html}{step_html}</div>'
    ch_html = (f'<div class="subttl" style="margin-top:22px">The channels we\'d prioritise for you</div>'
               f'<div class="grid g2 stratgrid">{ch_cards}</div>') if ch_cards else ""
    phases = [("Days 1–30", "Set the foundations", roadmap.get("d30") or []),
              ("Days 31–60", "Build the front doors", roadmap.get("d60") or []),
              ("Days 61–90", "Compound the growth", roadmap.get("d90") or [])]
    road_rows = ""
    for lab, sub, items in phases:
        lis = "".join(f'<li>{_e(it)}</li>' for it in items[:5] if it)
        if not lis:
            continue
        road_rows += (f'<div class="phase"><div class="ph">{lab}<small>{sub}</small></div><ul>{lis}</ul></div>')
    road_html = (f'<div class="subttl" style="margin-top:26px">Your 30 / 60 / 90-day roadmap</div>'
                 f'<div class="stratroad">{road_rows}</div>'
                 '<p class="small muted" style="margin-top:14px">A starting plan, not a straitjacket — the fastest '
                 'wins come first, and search compounds over roughly 6–12 months as the work builds.</p>') if road_rows else ""
    body = pos_html + ch_html + road_html
    if not body:
        return ""
    if thin:
        eyebrow, title = "Where to start", "Your growth strategy — where we'd start"
        intro = ("Public search data is limited for your niche, so rather than pad this report with thin numbers, "
                 "here's the concrete strategy we'd start with to win customers in your market.")
    else:
        eyebrow, title = "Your growth strategy", "Where to start — your growth roadmap"
        intro = ("Beyond fixing what's above, here's the tailored plan we'd run to grow your enquiries — the "
                 "positioning, the channels that actually fit your business, and the first 90 days.")
    return _sec(eyebrow, title, intro, body)


def _growth_strategy(key, model, m: dict, name: str, allowed: set, thin: bool = False) -> str:
    """Compose + render the tailored growth strategy. One LLM call, guarded; deterministic fallback on any
    failure or missing key. Never raises — returns "" only if even the fallback render fails."""
    try:
        fb = _fallback_strategy(m, name, thin)
    except Exception:
        fb = {"positioning": "", "channels": [], "roadmap": {}}
    strat = fb
    try:
        data = _llm_strategy(key, model, m, name, thin)
        if isinstance(data, dict):
            cleaned = _clean_strategy(data, allowed, fb.get("positioning"))
            if cleaned:
                strat = cleaned
    except Exception:
        strat = fb
    try:
        return _render_strategy(strat, thin)
    except Exception:
        return ""


# --------------------------------------------------------------------------- shell + CSS ---
_BRANDMARK = ('<svg class="brandmark" viewBox="0 0 48 48" fill="none" aria-hidden="true">'
              '<circle cx="24" cy="24" r="23" stroke="#b6862c" stroke-width="1.5"/>'
              '<path d="M24 11c-5 6-9 9-9 15a9 9 0 0018 0c0-6-4-9-9-15z" fill="#2f6b52"/>'
              '<path d="M24 20c-2.5 3-4.5 4.5-4.5 7.5a4.5 4.5 0 009 0c0-3-2-4.5-4.5-7.5z" fill="#e9d9a8"/></svg>')

_CSS = """
:root{--ink:#14201c;--ink-2:#2b3a34;--paper:#f6f3ec;--paper-2:#fffdf8;--card:#ffffff;--line:#e3ddce;
--forest:#1f4a3a;--forest-2:#2f6b52;--gold:#b6862c;--gold-soft:#e9d9a8;--rust:#a6432c;--rust-soft:#f0d3c9;
--amber:#c8802a;--amber-soft:#f2ddb8;--green:#2f7d52;--muted:#6f7a72;
--shadow:0 1px 2px rgba(20,32,28,.05),0 8px 30px rgba(20,32,28,.07);
--serif:"Iowan Old Style","Palatino Linotype","Palatino","Book Antiqua",Georgia,"Times New Roman",serif;
--sans:"Inter","Helvetica Neue","Segoe UI",Roboto,Arial,system-ui,sans-serif}
*{box-sizing:border-box}html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--paper);color:var(--ink);font-family:var(--sans);line-height:1.6;font-size:16px;-webkit-font-smoothing:antialiased}
.wrap{max-width:960px;margin:0 auto;padding:0 22px}
h1,h2,h3,h4{font-family:var(--serif);font-weight:600;line-height:1.2;letter-spacing:-.01em;color:var(--ink)}
p{margin:0 0 1em}a{color:var(--forest-2)}.small{font-size:13px}.muted{color:var(--muted)}
.mono{font-family:"SFMono-Regular",Menlo,Consolas,monospace}
.cover{background:radial-gradient(1100px 500px at 80% -10%,rgba(47,107,82,.35),transparent 60%),radial-gradient(900px 500px at -10% 110%,rgba(182,134,44,.25),transparent 55%),linear-gradient(160deg,#0f1c17 0%,#14261f 55%,#182e24 100%);color:#f2efe6;padding:60px 0 52px;position:relative;overflow:hidden}
.cover:before{content:"";position:absolute;inset:0;background-image:radial-gradient(rgba(255,255,255,.05) 1px,transparent 1px);background-size:22px 22px;opacity:.5}
.cover .wrap{position:relative}
.brandrow{display:flex;align-items:center;gap:14px;margin-bottom:48px}
.brandmark{width:40px;height:40px;flex:none}
.brandtext{font-family:var(--serif);font-size:19px;letter-spacing:.02em;color:#fff}
.brandtext b{color:var(--gold-soft);font-weight:600}
.brandtext .sub{display:block;font-family:var(--sans);font-size:11px;letter-spacing:.18em;text-transform:uppercase;color:#9db3a7;margin-top:2px}
.kicker{font-family:var(--sans);font-size:12px;letter-spacing:.28em;text-transform:uppercase;color:var(--gold-soft);margin-bottom:18px}
.cover h1{color:#fff;font-size:clamp(32px,6vw,54px);margin:0 0 10px;letter-spacing:-.02em}
.cover h1 .thin{display:block;font-size:clamp(19px,3.4vw,28px);color:#cfe0d6;font-weight:500;margin-top:8px}
.cover .lede{font-size:18px;color:#d8e2da;max-width:640px;margin:22px 0 40px}
.heroloss{margin:34px 0 40px;background:linear-gradient(160deg,rgba(182,134,44,.16),rgba(166,67,44,.14));border:1px solid rgba(233,217,168,.32);border-left:4px solid var(--gold);border-radius:16px;padding:24px 26px;max-width:640px}
.heroloss .hl-lab{font-size:13px;letter-spacing:.12em;text-transform:uppercase;color:#e6c98c;font-weight:600}
.heroloss .hl-num{font-family:var(--serif);font-size:clamp(40px,8vw,60px);color:#fff;line-height:1.02;margin:6px 0 2px;letter-spacing:-.02em}
.heroloss .hl-num span{font-family:var(--sans);font-size:20px;font-weight:600;color:#e6c98c;margin-left:6px;letter-spacing:0}
.heroloss .hl-sub{font-size:16px;color:#e4ece6;margin-top:8px;line-height:1.5}
.heroloss .hl-cav{font-size:12px;color:#a9c1b5;margin-top:12px;font-style:italic}
.coverfacts{display:flex;flex-wrap:wrap;gap:14px 40px;border-top:1px solid rgba(255,255,255,.14);padding-top:26px}
.coverfacts div{min-width:120px}.coverfacts .l{font-size:11px;letter-spacing:.14em;text-transform:uppercase;color:#8fa89b;margin-bottom:5px}
.coverfacts .v{font-size:15px;color:#f2efe6;font-family:var(--serif)}
section{padding:50px 0;border-bottom:1px solid var(--line)}section:last-of-type{border-bottom:none}
.eyebrow{font-size:12px;letter-spacing:.2em;text-transform:uppercase;color:var(--gold);font-weight:600;margin-bottom:12px}
h2.sec{font-size:clamp(23px,4vw,32px);margin:0 0 8px}
.sec-intro{font-size:17px;color:var(--ink-2);max-width:680px;margin-bottom:8px}
.grid{display:grid;gap:18px}.g2{grid-template-columns:repeat(2,1fr)}.g3{grid-template-columns:repeat(3,1fr)}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:22px;box-shadow:var(--shadow)}
@media(max-width:720px){.g2,.g3{grid-template-columns:1fr}}
.scorewrap{display:grid;grid-template-columns:230px 1fr;gap:34px;align-items:center}
@media(max-width:720px){.scorewrap{grid-template-columns:1fr;gap:24px}}
.gauge{position:relative;width:220px;height:220px;margin:0 auto}
.gauge .num{position:absolute;inset:0;display:flex;flex-direction:column;align-items:center;justify-content:center}
.gauge .num b{font-family:var(--serif);font-size:52px;color:var(--ink);line-height:1}
.gauge .num span{font-size:11px;letter-spacing:.16em;text-transform:uppercase;color:var(--muted);margin-top:6px}
.gauge .num em{font-style:normal;font-size:13px;font-weight:600;margin-top:4px}
.bars{display:flex;flex-direction:column;gap:14px}
.bar .top{display:flex;justify-content:space-between;font-size:14px;margin-bottom:5px}
.bar .top b{font-weight:600}.bar .top b i{font-style:normal;font-size:11px;color:var(--muted);font-weight:600}
.bar .track{height:9px;background:#eee7d8;border-radius:6px;overflow:hidden}.bar .fill{height:100%;border-radius:6px}
.finding{background:var(--card);border:1px solid var(--line);border-left:4px solid var(--rust);border-radius:12px;padding:22px 24px;margin-bottom:16px;box-shadow:var(--shadow)}
.finding.sev-high{border-left-color:var(--rust)}.finding.sev-med{border-left-color:var(--amber)}
.finding .fhead{display:flex;align-items:flex-start;gap:14px;margin-bottom:10px}
.fnum{flex:none;width:34px;height:34px;border-radius:9px;background:var(--forest);color:#fff;font-family:var(--serif);font-size:17px;display:flex;align-items:center;justify-content:center;margin-top:1px}
.finding h3{font-size:19px;margin:2px 0 0;flex:1}
.tag{font-size:10.5px;letter-spacing:.1em;text-transform:uppercase;font-weight:700;padding:4px 9px;border-radius:20px;white-space:nowrap;margin-top:3px}
.tag.high{background:var(--rust-soft);color:var(--rust)}.tag.med{background:var(--amber-soft);color:#8a5410}
.finding .obs{margin:0 0 12px}
.finding .row{display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-top:12px}
@media(max-width:600px){.finding .row{grid-template-columns:1fr}}
.miniblock{background:var(--paper-2);border:1px solid var(--line);border-radius:9px;padding:12px 14px}
.miniblock .ml{font-size:11px;letter-spacing:.12em;text-transform:uppercase;font-weight:700;margin-bottom:5px}
.miniblock.cost .ml{color:var(--rust)}.miniblock.fix .ml{color:var(--green)}.miniblock p{margin:0;font-size:14.5px}
.evidence{font-size:12.5px;color:var(--muted);margin-top:12px;padding-top:10px;border-top:1px dashed var(--line)}
.evidence b{color:var(--ink-2)}
.subttl{font-size:12px;font-weight:700;letter-spacing:.05em;text-transform:uppercase;color:var(--gold);margin:16px 0 8px}
.tablewrap{overflow-x:auto;border:1px solid var(--line);border-radius:12px;box-shadow:var(--shadow)}
table{border-collapse:collapse;width:100%;min-width:520px;background:var(--card);font-size:14px}
th,td{padding:12px 15px;text-align:left;border-bottom:1px solid var(--line);vertical-align:top}
thead th{background:var(--forest);color:#fff;font-family:var(--sans);font-weight:600;font-size:12.5px;letter-spacing:.02em}
thead th.you{background:var(--gold)}tbody tr:last-child td{border-bottom:none}
td.you{background:#fbf6ea;font-weight:600}td.num,th.num{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
.yes{color:var(--green);font-weight:700}.no{color:var(--rust);font-weight:700}.part{color:var(--amber);font-weight:700}
tbody th{background:var(--paper-2);font-family:var(--sans);font-size:13px;font-weight:600;color:var(--ink-2)}
.ex{display:block;font-size:12px;color:var(--muted);margin-top:3px}
.ktag{display:inline-block;font-size:10.5px;font-weight:700;letter-spacing:.03em;padding:2px 8px;border-radius:20px;margin-right:5px;white-space:nowrap}
.ktag.money{background:#dcefe4;color:var(--forest)}.ktag.gap{background:var(--rust-soft);color:var(--rust)}
.ktag.info{background:#eee7d8;color:#6b5a2a}.ktag.new{background:var(--gold-soft);color:#5a4410}
.krow-tot td{border-top:2px solid var(--forest-2);border-bottom:none;background:var(--paper-2)}
.chips{display:flex;flex-wrap:wrap;gap:12px}
.chip{background:var(--paper-2);border:1px solid var(--line);border-radius:12px;padding:13px 17px;min-width:104px}
.chip b{display:block;font-family:var(--serif);font-size:22px;color:var(--forest);line-height:1}.chip span{font-size:12px;color:var(--muted);display:block;margin-top:4px}
.rankrow{display:flex;align-items:center;gap:14px;margin-bottom:12px}
.rankrow .name{width:190px;flex:none;font-size:14px;word-break:break-word}.rankrow .name.you{font-weight:700;color:var(--gold)}
.rankrow .rtrack{flex:1;height:26px;background:#eee7d8;border-radius:6px;overflow:hidden;position:relative}
.rankrow .rfill{height:100%;border-radius:6px;display:flex;align-items:center;justify-content:flex-end;padding-right:10px;color:#fff;font-size:12px;font-weight:700;white-space:nowrap}
@media(max-width:600px){.rankrow .name{width:110px;font-size:12.5px}}
.gcols{display:grid;grid-template-columns:1fr 1fr;gap:18px;margin-top:6px}@media(max-width:560px){.gcols{grid-template-columns:1fr}}
.glab{font-size:12px;font-weight:700;letter-spacing:.04em;text-transform:uppercase;color:var(--gold);margin-bottom:6px}
.glist{list-style:none;margin:0;padding:0}.glist li{position:relative;padding:5px 0 5px 24px;font-size:13.5px;color:var(--ink-2)}
.glist li:before{position:absolute;left:0;top:5px;font-weight:800}.glist li.c:before{content:"✓";color:var(--green)}.glist li.x:before{content:"→";color:var(--amber)}
.note-b{background:var(--paper-2);border:1px solid var(--line);border-left:3px solid var(--forest-2);border-radius:10px;padding:14px 16px;font-size:14.5px;color:var(--ink-2)}
.oppcard{background:linear-gradient(160deg,#1f4a3a,#123027);color:#eef4f0;border-radius:16px;padding:30px;box-shadow:var(--shadow)}
.oppcard h3{color:#fff}.oppgrid{display:grid;grid-template-columns:repeat(3,1fr);gap:16px;margin:6px 0 18px}
@media(max-width:600px){.oppgrid{grid-template-columns:1fr}}
.oppstat{background:rgba(255,255,255,.07);border:1px solid rgba(255,255,255,.14);border-radius:12px;padding:18px}
.oppstat.light{background:var(--paper-2);border:1px solid var(--line)}
.oppstat b{display:block;font-family:var(--serif);font-size:28px;color:var(--gold-soft);line-height:1}
.oppstat.light b{color:var(--forest)}
.oppstat span{font-size:12.5px;color:#cfe0d6;display:block;margin-top:8px}.oppstat.light span{color:var(--muted)}
.disclaim{font-size:12.5px;color:#a9c1b5;border-top:1px solid rgba(255,255,255,.14);padding-top:14px;margin-top:6px}
.mathrow{display:flex;justify-content:space-between;gap:12px;padding:9px 0;border-bottom:1px dashed rgba(255,255,255,.18);font-size:14px}
.mathrow:last-child{border-bottom:none;font-weight:700;color:#fff}
.mathrow .tktnote{font-weight:400;font-style:italic;color:#a9c1b5;font-size:12.5px}
.reframe{background:var(--gold-soft);border:1px solid #e3cf94;border-left:4px solid var(--gold);border-radius:10px;padding:14px 16px;font-size:14.5px;color:#5a4410;line-height:1.55;margin:0}
.alsofix{margin-top:22px}
.alsolist{list-style:none;margin:8px 0 0;padding:0;display:grid;gap:10px}
.alsolist li{background:var(--paper-2);border:1px solid var(--line);border-radius:10px;padding:12px 15px}
.alsolist li b{display:block;font-size:14.5px;color:var(--ink)}
.alsolist li span{display:block;font-size:12.5px;color:var(--muted);margin-top:3px}
.phase{display:grid;grid-template-columns:130px 1fr;gap:20px;padding:20px 0;border-bottom:1px solid var(--line)}
.phase:last-child{border-bottom:none}@media(max-width:600px){.phase{grid-template-columns:1fr;gap:8px}}
.phase .ph{font-family:var(--serif);font-size:15px;color:var(--gold);font-weight:600}
.phase .ph small{display:block;color:var(--muted);font-family:var(--sans);font-size:12px;margin-top:2px}
.phase ul{margin:0;padding-left:18px}.phase li{margin-bottom:6px}.phase li b{color:var(--forest-2)}
.cta{background:var(--ink);color:#eef4f0;border-radius:18px;padding:40px 34px;text-align:center;margin:8px 0 0}
.cta h2{color:#fff;font-size:30px;margin:0 0 12px}.cta p{color:#cbd8d0;max-width:560px;margin:0 auto 8px}
.cta .rev{display:inline-flex;align-items:center;gap:10px;background:var(--gold);color:#241a02;font-weight:700;padding:12px 22px;border-radius:30px;margin-top:22px;font-size:15px;text-decoration:none}
.foot{padding:32px 0 60px;color:var(--muted);font-size:12.5px}.foot b{color:var(--ink-2)}
ul.clean{list-style:none;padding:0;margin:0}ul.clean li{padding-left:26px;position:relative;margin-bottom:10px}
ul.clean li:before{content:"";position:absolute;left:0;top:8px;width:9px;height:9px;border-radius:50%;background:var(--gold)}
/* source / proof caption — the unobtrusive provenance line under every figure & section */
.srccap{font-size:11.5px;line-height:1.5;color:var(--muted);margin:9px 2px 0;padding-left:16px;position:relative;font-style:italic}
.srccap:before{content:"";position:absolute;left:0;top:7px;width:8px;height:8px;border:1.5px solid var(--gold);border-radius:50%}
.srccap.onopp{color:#bcd3c6;margin:2px 0 14px}.srccap.onopp:before{border-color:#e6c98c}
.tablewrap + .srccap,.chips + .srccap{margin-top:8px}
.hl-src{font-size:11.5px;color:#a9c1b5;margin-top:10px;line-height:1.5;font-style:italic;max-width:560px}
/* revenue: plain-English cadence + realistic-gain payoff */
.mathnote{margin-top:14px;font-size:14.5px;color:#eef4f0;line-height:1.55;background:rgba(255,255,255,.06);border-radius:10px;padding:12px 15px}
.mathnote b{color:#fff}
.payoff{margin-top:18px;background:rgba(74,222,128,.13);border:1px solid rgba(74,222,128,.38);border-radius:12px;padding:16px 18px}
.payoff .pl{font-size:11px;letter-spacing:.14em;text-transform:uppercase;color:#8ce0a6;font-weight:700;margin-bottom:6px}
.payoff p{margin:0;color:#e4ece6;font-size:14.5px;line-height:1.6}.payoff b{color:#bbf7d0}
/* competitor tension callout */
.tension{background:var(--rust-soft);border:1px solid #e2b3a6;border-left:4px solid var(--rust);border-radius:10px;padding:13px 16px;font-size:15px;color:#6a2a1a;line-height:1.5;margin-bottom:14px}
.tension b{color:var(--rust)}
/* growth strategy — the tailored roadmap (centrepiece for thin-data audits) */
.strat-pos{background:linear-gradient(160deg,#1f4a3a,#123027);color:#eef4f0;border-radius:16px;padding:24px 26px;box-shadow:var(--shadow);margin-top:16px}
.strat-pos .sl{font-size:11px;letter-spacing:.16em;text-transform:uppercase;color:#e6c98c;font-weight:700;margin-bottom:8px}
.strat-pos p{margin:0;font-size:17px;line-height:1.6;color:#eef4f0}.strat-pos p b{color:#fff}
.stratgrid{margin-top:6px}
.chan{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:20px 22px;box-shadow:var(--shadow)}
.chan h3{font-size:17px;margin:0 0 7px}
.chan .why{margin:0 0 12px;font-size:14.5px;color:var(--ink-2);line-height:1.55}
.chan .step{background:var(--paper-2);border:1px solid var(--line);border-left:3px solid var(--green);border-radius:9px;padding:11px 13px;font-size:13.5px;color:var(--ink-2);line-height:1.5}
.chan .step b{display:block;font-size:10.5px;letter-spacing:.1em;text-transform:uppercase;color:var(--green);margin-bottom:4px;font-weight:700}
.stratroad{margin-top:4px}
/* thin-data (limited public data) honest state */
.thinwrap{background:linear-gradient(160deg,#1f4a3a,#123027);color:#eef4f0;border-radius:18px;padding:34px 32px;box-shadow:var(--shadow)}
.thinwrap .tl-lab{font-size:12px;letter-spacing:.16em;text-transform:uppercase;color:#e6c98c;font-weight:700}
.thinwrap h2{color:#fff;font-size:clamp(24px,4vw,32px);margin:10px 0 12px}
.thinwrap .tl-lede{font-size:17px;color:#d8e2da;max-width:640px;line-height:1.6;margin:0 0 8px}
.thinfacts{display:grid;grid-template-columns:repeat(3,1fr);gap:14px;margin:22px 0 6px}
@media(max-width:600px){.thinfacts{grid-template-columns:1fr}}
.thinfacts .tf{background:rgba(255,255,255,.07);border:1px solid rgba(255,255,255,.14);border-radius:12px;padding:16px}
.thinfacts .tf b{display:block;font-family:var(--serif);font-size:24px;color:#e9d9a8;line-height:1.05}
.thinfacts .tf span{font-size:12.5px;color:#cfe0d6;display:block;margin-top:7px;line-height:1.4}
.thinlist{list-style:none;margin:18px 0 0;padding:0;display:grid;gap:10px}
.thinlist li{position:relative;padding:2px 0 2px 26px;font-size:14.5px;color:#e4ece6;line-height:1.55}
.thinlist li:before{content:"";position:absolute;left:0;top:8px;width:9px;height:9px;border-radius:50%;background:#e6c98c}
@media print{body{background:#fff}section{break-inside:avoid;padding:26px 0}.finding,.card,.tablewrap,.oppcard,.thinwrap,.chan,.strat-pos,.phase{break-inside:avoid}.cover,*{-webkit-print-color-adjust:exact;print-color-adjust:exact}}
"""


def _cover(name, d, m, nv, allowed, reveal, thin=False) -> str:
    biz = m.get("business") or {}
    subtitle = _clean_prose(nv.get("cover_subtitle"), allowed, "Digital growth audit")
    lede = _clean_prose(nv.get("cover_lede"), allowed,
                        "A clear, plain-English look at where you show up on Google today, the searches you're "
                        "missing, and how we'd close the gap.")
    facts = [("Prepared for", name),
             ("Subject site", biz.get("website") or (("https://" + m.get("domain")) if m.get("domain") else "—")),
             ("Location", biz.get("location") or "—"),
             ("Reveal", reveal or "On request")]
    fh = "".join(f'<div><div class="l">{_e(l)}</div><div class="v">{_e(v)}</div></div>' for l, v in facts if v)
    # HERO — the one number a 10-second scan must land: the recurring monthly loss + the named rival.
    # Suppressed on a THIN audit (we won't headline a shaky dollar figure when the data is limited).
    hero = _hero_loss(m)
    hero_html = ""
    if hero["monthly"] > 0 and not thin:
        rival_line = (f'to {_e(hero["rival"])} and other rivals showing up ahead of you'
                      if hero["rival"] else "to rivals showing up ahead of you")
        caveat = ""
        if hero.get("estimated"):
            caveat = (f'<div class="hl-cav">Based on a typical {_e(hero.get("bucket") or "business")} job value — '
                      'tell us yours and we\'ll refine it.</div>')
        src = _e(_assumptions_caption(m.get("revenue") or {}))
        hero_html = (
            '<div class="heroloss"><div class="hl-lab">Every month you wait, you\'re losing about</div>'
            f'<div class="hl-num">{_fmt_money(hero["monthly"])}<span>/ month</span></div>'
            f'<div class="hl-sub">in new work {rival_line} — for searches your customers are already typing into Google.</div>'
            f'{caveat}<div class="hl-src">{src}</div></div>')
    return (f'<header class="cover"><div class="wrap">'
            f'<div class="brandrow">{_BRANDMARK}<div class="brandtext"><b>DE Group</b> Growth Intelligence'
            f'<span class="sub">A Traffic Radius Company · Melbourne</span></div></div>'
            f'<div class="kicker">Confidential Growth Audit</div>'
            f'<h1>{_e(name)}<span class="thin">{_e(subtitle)}</span></h1>'
            f'<p class="lede">{_e(lede)}</p>{hero_html}'
            f'<div class="coverfacts">{fh}</div></div></header>')


def _cta(name, m, allowed, nv) -> str:
    ns = _clean_prose(nv.get("next_step"), allowed,
                      "At the reveal we'll open your site and your competitors' side by side, show you exactly where "
                      "the customers are going, and map the fixes we'd action first — and what each is worth.")
    return (f'<section style="border-bottom:none"><div class="wrap"><div class="cta">'
            f'<div class="eyebrow" style="color:var(--gold-soft)">The Next Step</div>'
            f'<h2>Let\'s walk through this together</h2><p>{_e(ns)}</p>'
            f'<a class="rev" href="tel:0370209196">📅 Book your reveal walkthrough · (03) 7020 9196</a></div>'
            f'<div class="foot"><p><b>DE Group Growth Intelligence</b> — a Traffic Radius company · Melbourne, '
            f'Australia. Prepared for {_e(name)}.</p><p>Findings are based on live Google search data and public '
            f'information reviewed at the time of writing. External statistics are attributed; illustrative figures '
            f'are labelled as such. Rankings change over time. This document is confidential.</p></div></div></section>')


# --------------------------------------------------------------------------- thin-data guard ---
def _data_signals(m: dict) -> dict:
    """Count the SUBSTANTIVE public-data signals we actually have for this domain (post-scrub, so a gap
    made only of rival-brand/off-intent junk counts as empty)."""
    seo = m.get("seo") or {}
    opp = m.get("opportunity") or {}
    uni = (m.get("universe") or {}).get("totals") or {}
    comps = [c for c in (m.get("competitors") or []) if (c.get("domain") or c.get("name"))]
    ranked = int(opp.get("org_keywords") or uni.get("ranked") or 0)
    return {
        "ranked": ranked,
        "money": len(seo.get("money_keywords") or []),
        "proof": len(seo.get("proof_winning") or []),
        "gap": len(m.get("keyword_gap") or []),
        "content": len([x for x in (m.get("content_gap") or []) if isinstance(x, dict)]),
        "competitors": len(comps),
        "clusters": len((m.get("universe") or {}).get("clusters") or []),
        "ads": bool((m.get("ads") or {}).get("running")),
        "est_traffic": int(opp.get("est_org_traffic") or 0),
    }


def _audit_is_thin(m: dict):
    """A prospect with almost no public search footprint. Definition (documented threshold): FEWER THAN
    3 real non-branded ranked keywords AND fewer than 2 gap keywords AND no real competitors AND not
    advertising AND no content-gap topics. When ALL hold, the full section stack would render nearly
    empty — so we switch to the honest 'limited public data' state instead. Returns (thin, signals)."""
    s = _data_signals(m)
    ranked_signal = max(s["ranked"], s["money"] + s["proof"])
    thin = (ranked_signal < 3 and s["gap"] < 2 and s["competitors"] == 0
            and not s["ads"] and s["content"] == 0 and s["clusters"] == 0)
    return thin, s


def _thin_state(name, d, m, nv, allowed) -> str:
    """The HONEST, still-premium 'we found little public search data' state. Every figure here is a REAL
    signal (or a plainly-labelled band) — nothing invented to fill space. The point it makes IS the
    finding: you're close to invisible in the searches your customers run, and that demand is going to
    whoever does show up."""
    biz = m.get("business") or {}
    s = _data_signals(m)
    # real facts only — a "0" here is a true, powerful finding, not a fabrication
    facts = []
    facts.append((_n(s["ranked"]), "everyday searches your website currently shows up for on Google"))
    facts.append((_traffic_range(s["est_traffic"]), "estimated visits from Google search a month (unpaid)"))
    if biz.get("location"):
        facts.append((_e(biz.get("location")), "the market where this demand is up for grabs"))
    facts_html = "".join(f'<div class="tf"><b>{v}</b><span>{lab}</span></div>' for v, lab in facts[:3])
    points = [
        "When we searched the everyday terms your customers type, your website barely came up — so those "
        "enquiries are going to whoever Google does show.",
        "That isn't a failure — it's the opening: the demand already exists and, right now, you're not "
        "competing for it. Being first to properly claim it is the whole opportunity.",
        "The fastest fix is a focused set of pages built around the exact searches your buyers use, plus the "
        "Google Business and on-page basics that get you found and quotable.",
    ]
    pts_html = "".join(f"<li>{p}</li>" for p in points)
    lede = _clean_prose(nv.get("exec_intro"), allowed,
                        "We went looking for your business in Google's search data — and found very little of it. "
                        "That scarcity is itself the headline: the searches are happening, but not for you.")
    body = (
        '<div class="thinwrap">'
        '<div class="tl-lab">The finding</div>'
        "<h2>There's very little of your business in Google right now</h2>"
        f'<p class="tl-lede">{_e(lede)}</p>'
        f'<div class="thinfacts">{facts_html}</div>'
        f'{_src("Sourced live from Google search data for your domain (DataForSEO); small counts shown as bands, not exact figures.")}'
        f'<ul class="thinlist">{pts_html}</ul>'
        '</div>')
    return (f'<section><div class="wrap"><div class="eyebrow">Limited public data</div>'
            f'<h2 class="sec">Where you show up on Google today</h2>{body}</div></section>')


# --------------------------------------------------------------------------- accuracy / QA gate ---
def audit_qa_gate(audit_model: dict, html: str = "") -> dict:
    """Automated accuracy gate — run at generation time so a defective audit is CAUGHT, never silently
    shipped. Asserts: the revenue chain reconciles (sales × ticket ≈ monthly); no competitor-brand or
    buy-a-machine term survives in the gap; the hero dollar figure that's rendered traces to the model;
    every figure section carries a source label; and flags a THIN audit (insufficient public data) so the
    caller can log / route it. Returns {ok, issues, thin, signals}. Pure + guarded — never raises."""
    issues = []
    try:
        m = audit_model or {}
        rev = m.get("revenue") or {}
        # 1) revenue chain reconciles
        monthly = float(rev.get("monthly") or 0)
        ticket = float(rev.get("avg_ticket") or 0)
        sales = float(rev.get("sales_per_mo") or 0)
        if monthly and ticket and sales:
            implied = sales * ticket
            if monthly and abs(implied - monthly) / monthly > 0.20:
                issues.append(f"revenue_chain_mismatch:sales×ticket={implied:.0f}~monthly={monthly:.0f}")
        # 2) competitor-brand + 3) buy-intent leakage in the visible gap
        biz = m.get("business") or {}
        comp_brands = _competitor_brand_tokens(m.get("competitors") or [], exclude=set())
        is_service = _service_business(biz.get("industry"), biz.get("sub_industry"))
        gap_only = m.get("keyword_gap") or []
        # rival brands must not appear anywhere we surface keywords (gap OR the prospect's money keywords)
        for r in gap_only + ((m.get("seo") or {}).get("money_keywords") or []):
            kw = r.get("keyword") or ""
            if comp_brands and _kw_is_scrubbed(kw, comp_brands, False):
                issues.append(f"competitor_brand_in_gap:{kw[:40]}")
        # buy-a-machine terms only matter in the GAP (what we tell them to chase), not their own rankings
        if is_service:
            for r in gap_only:
                kw = r.get("keyword") or ""
                if _is_buy_intent_kw(kw):
                    issues.append(f"buy_intent_in_gap:{kw[:40]}")
        # zero-value / navigational keywords must never be presented as a "money search you're missing"
        # (defect: "rotomotion engineering" — a rival's brand, $0 value — shown as a recommended target)
        for r in gap_only + (m.get("outranked") or []) + ((m.get("seo") or {}).get("money_keywords") or []):
            if _is_low_value_gap(r):
                issues.append(f"zero_value_or_brand_keyword:{(r.get('keyword') or '')[:40]}")
        # thin-data
        thin, signals = _audit_is_thin(m)
        if thin:
            issues.append("insufficient_data")
        # 6) TRADE-RELEVANCE + NON-EMPTY keyword data — the two failures that reached clients (a concrete
        # contractor's audit full of 'retirement village' terms; and a keyword table that came out empty).
        # Flag OFF-TRADE care/retirement terms when the business itself is NOT in that sector, and flag an
        # audit that presents NO keyword data at all — so the caller logs/routes it for a clean rebuild.
        uni = m.get("universe") or {}
        if (not thin and not (uni.get("clusters") or []) and not (m.get("keyword_gap") or [])
                and not (m.get("seed_clusters") or [])):
            issues.append("empty_keyword_universe")
        _ind = ((biz.get("industry") or "") + " " + (biz.get("sub_industry") or "")).lower()
        _biz_is_care = any(w in _ind for w in ("aged", "care", "retire", "nursing", "senior", "health", "medical"))
        if not _biz_is_care:
            _kwtext = " ".join((r.get("keyword") or "") for r in gap_only).lower()
            _off = [w for w in ("retirement", "aged care", "nursing home", "retirement village") if w in _kwtext]
            if _off:
                issues.append("off_trade_keywords:" + ",".join(_off))
        # 4) hero dollar figure traces to model + 5) source labels present (only meaningful on full reports)
        if html and not thin:
            if monthly and _fmt_money(monthly) not in html:
                issues.append("hero_number_not_in_html")
            if html.count('class="srccap"') + html.count("class='srccap'") < 3:
                issues.append("missing_source_labels")
        hard = _audit_hard_failures(m, html)
        return {"ok": not issues and not hard, "issues": issues + hard, "hard": hard,
                "thin": thin, "signals": signals}
    except Exception as exc:   # a gate crash must not sink the audit — report it as an issue
        return {"ok": False, "issues": [f"qa_gate_error:{str(exc)[:80]}"], "hard": [],
                "thin": False, "signals": {}}


# Bare nouns that carry huge encyclopedic search volume and are NEVER what a paying local customer types.
# A universe that collapses to one of these is the "animal" defect: 60,500/mo of Wikipedia traffic driving a
# six-figure loss claim. Extend when a new one is caught rather than widening the rule.
_GENERIC_ONE_WORD = {
    "animal", "animals", "building", "construction", "transport", "food", "health", "medical", "care",
    "education", "training", "engineering", "manufacturing", "energy", "mining", "farming", "agriculture",
    "fitness", "beauty", "travel", "finance", "insurance", "property", "retail", "cleaning", "design",
    "marketing", "software", "technology", "law", "legal", "music", "sport", "sports", "garden", "water",
}
# A monthly "you are losing" figure above this is not credible to an Australian SMB owner and reads as a
# scare number, whatever the arithmetic says. A$40k/mo = A$480k/yr of NEW work from search alone.
_MAX_CREDIBLE_MONTHLY = 40_000.0


def _audit_hard_failures(m: dict, html: str = "") -> list:
    """BLOCKING defects — the four classes that actually reached closers and cost us credibility
    (Vysakh, 2026-08-31: Prendergast, Hillsyde). Unlike the advisory issues above, ANY of these means the
    report must not ship: it is not "thin but honest", it is WRONG. Pure + guarded.

    The standing rule this enforces: no audit is far better than a wrong audit in a closer's hands.
    """
    out = []
    try:
        biz = m.get("business") or {}
        tech = m.get("tech") or {}
        rev = m.get("revenue") or {}

        # 1) ORIGIN NEVER SERVED US ANYTHING. Prendergast's site TCP-connects then hangs (0 bytes) and
        #    Hillsyde's domain is a parked "for sale" page -- yet both audits reported a ~100/100 speed
        #    score and congratulated them on it, because Lighthouse grades an empty document as perfect.
        #    An empty final_url means the page fetcher never got a document at all.
        if not (tech.get("final_url") or "").strip():
            out.append("origin_unreachable:no_page_was_ever_fetched")
        else:
            ps = m.get("pagespeed") or {}
            perf = ((ps.get("mobile") or {}).get("performance"))
            geo_score = (m.get("geo_aeo") or {}).get("score")
            # Perfect speed + almost nothing readable on the page = an empty/parked document, not a fast site.
            if (perf is not None and geo_score is not None
                    and float(perf) >= 90 and float(geo_score) <= 40):
                out.append(f"origin_empty_or_parked:speed={perf}_but_readability={geo_score}")

        # 2) DEGENERATE KEYWORD UNIVERSE. The existing gate only catches a TOTALLY empty universe; the
        #    Prendergast failure was ONE surviving keyword ("animal"), which is worse than empty because
        #    every headline number is then derived from it and looks authoritative.
        kws = []
        for rows in ((m.get("keyword_gap") or []), (m.get("outranked") or []),
                     ((m.get("seo") or {}).get("money_keywords") or [])):
            for r in rows:
                k = ((r.get("keyword") if isinstance(r, dict) else str(r)) or "").strip().lower()
                if k:
                    kws.append(k)
        uniq = sorted(set(kws))
        if uniq and len(uniq) <= 2:
            out.append(f"degenerate_keyword_universe:only_{len(uniq)}_terms:{','.join(uniq)[:60]}")
        for k in uniq:
            if k in _GENERIC_ONE_WORD:
                out.append(f"generic_one_word_keyword:{k}")

        # 3) NAMESAKE "COMPETITOR". prendergast.com.au (an unrelated earthmoving firm, est. 1972) was named
        #    "your strongest organic competitor" purely on a shared surname, and the report then wrote prose
        #    about "your family name". A rival sharing the prospect's OWN distinctive token is a name
        #    collision until proven otherwise -- never assert it.
        own = set()
        for src in (biz.get("name"), m.get("domain"), biz.get("company")):
            for w in _re.findall(r"[a-z]{5,}", (src or "").lower()):
                if w not in ("group", "trust", "trustee", "australia", "services", "holdings", "nominees"):
                    own.add(w)
        for cmp_ in (m.get("competitors") or [])[:6]:
            blob = ((cmp_.get("domain") or "") + " " + (cmp_.get("name") or "")).lower()
            hit = [w for w in own if w in blob]
            if hit:
                out.append(f"namesake_competitor:{(cmp_.get('domain') or cmp_.get('name'))[:40]}"
                           f"_shares_{hit[0]}")

        # 4) IMPLAUSIBLE MONEY. Two independent tests: an absolute ceiling, and consistency with the
        #    report's OWN evidence table -- Prendergast's cover said A$192,844/mo over a table totalling
        #    A$1,543, and the report explicitly invites the reader to check that.
        monthly = float(rev.get("monthly") or 0)
        if monthly > _MAX_CREDIBLE_MONTHLY:
            out.append(f"revenue_claim_implausible:{monthly:.0f}/mo")
        evidence = 0.0
        for rows in ((m.get("keyword_gap") or []), (m.get("outranked") or [])):
            for r in rows:
                evidence += float(r.get("cap_value") or r.get("money_value") or 0)
        if monthly and evidence and monthly > evidence * 10:
            out.append(f"revenue_exceeds_own_evidence:{monthly:.0f}_vs_table_{evidence:.0f}")
    except Exception as exc:
        out.append(f"hard_gate_error:{str(exc)[:60]}")
    return out


# --------------------------------------------------------------------------- entry point ---
def gen_growth_audit(key: str, model: str, audit_model: dict, avg_ticket: float | None = None,
                     company: str = "", reveal: str = "") -> str | None:
    if not audit_model:
        return None
    try:
        m = _normalise_revenue(audit_model, avg_ticket)   # ensure a ticket + monthly bridge ALWAYS exist
        m = _scrub_model_keywords(m)                       # defensive: strip rival brands + buy-a-machine terms
        domain = m.get("domain") or ""
        name = _clean_name(m.get("name") or "", company, domain)
        d = _distill(m, name)                              # totals (searches_missing) recomputed on scrubbed data
        allowed = _allowed_nums(d)
        seeds = _finding_seeds(m)
        nv = _narrative(key, model, d, seeds) or {}

        thin, _sig = _audit_is_thin(m)
        if thin:
            # THIN data → seed-keyword CLUSTERS + the tailored strategy become the centrepiece, right after the
            # honest hero state. The seed-cluster table pivots "we found little on your domain" into the real
            # market demand (from service/product keyword clusters) — so the audit is never empty even here.
            body = (_thin_state(name, d, m, nv, allowed)   # honest limited-data state — no empty sections
                    + _pagespeed(m, nv, allowed)           # site speed is independent of keyword data — show it even here
                    + _seed_cluster_strategy(m, nv, allowed)
                    + _growth_strategy(key, model, m, name, allowed, thin=True))
        else:
            secs = [
                _exec_summary(m, d, nv, allowed),
                _methodology(m, nv, allowed),
                _findings(seeds, nv, allowed),
                _visibility(m, nv, allowed),
                _pagespeed(m, nv, allowed),
                _money_tables(m, nv, allowed),
                _clusters_funnel(m, nv, allowed),
                _benchmark(m, nv, allowed),
                _content_gap(m, nv, allowed),
                _ads_block(m, nv, allowed),
                _geo(m, nv, allowed),
                _backlinks(m, nv, allowed),
                _search_picture(m, nv, allowed),
                _revenue_opp(m, nv, allowed),
                _plan_phases(m, nv, allowed),
                _growth_strategy(key, model, m, name, allowed, thin=False),  # complements the data, lower down
            ]
            body = "".join(s for s in secs if s)
        cover = _cover(name, d, m, nv, allowed, reveal, thin=thin)
        cta = _cta(name, m, allowed, nv)
        html = (f'<!doctype html><html lang="en"><head><meta charset="utf-8"/>'
                f'<meta name="viewport" content="width=device-width, initial-scale=1"/>'
                f'<title>Confidential Growth Audit — {_e(name)}</title><style>{_CSS}</style></head>'
                f'<body>{cover}{body}{cta}</body></html>')
        # ACCURACY GATE — never ship a violating audit silently: log the issues + annotate (invisibly).
        try:
            qa = audit_qa_gate(m, html)
            if not qa.get("ok"):
                _log.warning("growth_audit_qa_gate", domain=domain, thin=qa.get("thin"),
                             issues=qa.get("issues"))
                note = _h.escape("; ".join(qa.get("issues") or []))[:600]
                html = html.replace("<body>", f"<body><!-- QA-GATE: {note} -->", 1)
        except Exception:
            pass
        return html
    except Exception:
        return None


# --------------------------------------------------------------------------- #
# PageSpeed & Core Web Vitals section (DataForSEO Lighthouse — lab-only; reads m['pagespeed'])
# --------------------------------------------------------------------------- #
_CWV = {
    "lcp": {"good": 2500, "poor": 4000, "short": "Loading speed", "name": "Largest Contentful Paint",
            "plain": "how long until the main thing on your page actually appears", "unit": "s", "div": 1000.0},
    "inp": {"good": 200, "poor": 500, "short": "Responsiveness", "name": "Interaction to Next Paint",
            "plain": "how quickly the page reacts when someone taps or clicks", "unit": "ms", "div": 1.0},
    "cls": {"good": 0.1, "poor": 0.25, "short": "Visual stability", "name": "Cumulative Layout Shift",
            "plain": "how much the page jumps around while it loads", "unit": "", "div": 1.0},
}


def _grade_cwv(metric, value):
    t = _CWV[metric]
    if value < t["good"]:
        return ("Good", "#2f7d52", "This is where Google wants it — nothing to fix here.")
    if value < t["poor"]:
        return ("Needs work", "#c8802a", "Over Google's bar for a good experience — worth tightening.")
    return ("Poor", "#a6432c", "Well past Google's limit — this is actively costing you visitors.")


def _fmt_cwv(metric, value):
    t = _CWV[metric]
    if t["unit"] == "s":
        return f'{value / t["div"]:.1f}s'
    if t["unit"] == "ms":
        return f'{int(round(value))}ms'
    return f'{value:.2f}'


def _ps_gauge(score, label):
    """Speed gauge — mirrors growth_audit._gauge (same geometry/palette) with a custom caption."""
    try:
        sc = max(0, min(100, int(round(float(score)))))
    except Exception:
        sc = 0
    word, col = _band(sc)
    C = 515.0
    dash = round(sc / 100.0 * C, 1)
    return ('<div class="gauge"><svg viewBox="0 0 200 200" width="220" height="220">'
            '<circle cx="100" cy="100" r="82" fill="none" stroke="#eee7d8" stroke-width="20"/>'
            f'<circle cx="100" cy="100" r="82" fill="none" stroke="{col}" stroke-width="20" stroke-linecap="round" '
            f'stroke-dasharray="{dash} {C - dash:.1f}" transform="rotate(-90 100 100)"/></svg>'
            f'<div class="num"><b>{sc}</b><span>{_e(label)}</span><em style="color:{col}">{word}</em></div></div>')


def _pagespeed(m, nv, allowed):
    """PageSpeed & Core Web Vitals — mobile speed gauge, the three Core Web Vitals graded in plain
    English against Google's thresholds (from a Lighthouse LAB test — we have no real-user data, and
    say so), the biggest speed wins, and the revenue cost of a slow phone load framed on avg_ticket.
    Returns '' when there's nothing measurable (never an empty shell)."""
    ps = m.get("pagespeed") or {}
    mob = ps.get("mobile") or {}
    dsk = ps.get("desktop") or {}
    lab = mob.get("lab") or {}
    perf = mob.get("performance")
    # nothing measurable at all → omit the whole section
    if perf is None and not lab and not (mob.get("opportunities") or []):
        return ""
    rev = m.get("revenue") or {}
    ticket = rev.get("avg_ticket")

    # DataForSEO Lighthouse is lab-only — the honest, repeated provenance line for this section.
    _lab_src = ("Source: Google Lighthouse (via DataForSEO) — a lab test on a simulated mid-tier phone. "
                "It's a controlled diagnostic, not a reading from your real visitors, so treat the numbers "
                "as directional. Lab scores naturally vary a few points run-to-run.")

    # 1) Prominent mobile speed gauge + desktop / category chips ----------------------------------
    gauge = _ps_gauge(perf, "Mobile speed score") if perf is not None else ""
    chips = []
    if dsk.get("performance") is not None:
        _w, c = _band(dsk["performance"])
        chips.append(f'<div class="ps-chip"><b style="color:{c}">{dsk["performance"]}</b><span>Desktop speed</span></div>')
    for lab_name, k in (("SEO", "seo"), ("Accessibility", "accessibility"), ("Best practices", "best_practices")):
        v = mob.get(k)
        if v is not None:
            _w, c = _band(v)
            chips.append(f'<div class="ps-chip"><b style="color:{c}">{v}</b><span>{lab_name}</span></div>')
    chip_html = f'<div class="ps-chips">{"".join(chips)}</div>' if chips else ""
    verdict_line = ""
    if perf is not None:
        if perf < 50:
            verdict_line = ("On a phone — how most of your customers arrive — your site scores "
                            f"<b>{perf}/100</b> for speed. That's slow enough that people feel the wait, "
                            "and many won't hang around for it.")
        elif perf < 90:
            verdict_line = (f"On a phone your site scores <b>{perf}/100</b> for speed — usable, but with clear "
                            "room to get faster and hold more of the visitors you're already paying to attract.")
        else:
            verdict_line = (f"On a phone your site scores <b>{perf}/100</b> for speed — genuinely fast. "
                            "That's an advantage worth protecting.")
    top = (f'<div class="ps-hero"><div>{gauge}</div><div class="ps-hero-txt">'
           f'<p style="margin:0 0 12px">{verdict_line}</p>{chip_html}</div></div>') if gauge else chip_html

    # 2) The three Core Web Vitals, graded in plain English (LAB values) ---------------------------
    measured = {}
    if lab.get("lcp_ms") is not None:
        measured["lcp"] = lab["lcp_ms"]
    if lab.get("cls") is not None:
        measured["cls"] = lab["cls"]
    inp_proxy = ("inp" not in measured and lab.get("tbt_ms") is not None)   # INP has no lab metric — TBT is the honest proxy
    cwv_cards = ""
    for metric in ("lcp", "inp", "cls"):
        if metric == "inp":
            if not inp_proxy:
                continue
            tbt = lab["tbt_ms"]
            v_word, col = (("Good", "#2f7d52") if tbt < 200 else
                           ("Needs work", "#c8802a") if tbt < 600 else ("Poor", "#a6432c"))
            cwv_cards += ('<div class="cwv-card"><div class="cwv-top">'
                          '<span class="cwv-name">Responsiveness</span>'
                          f'<span class="cwv-verdict" style="color:{col}">{v_word}</span></div>'
                          f'<div class="cwv-val" style="color:{col}">{int(round(tbt))}ms <small>blocking</small>'
                          '<span class="cwv-lab">lab proxy</span></div>'
                          '<p class="cwv-plain"><b>How quickly the page reacts when someone taps or clicks.</b> '
                          'Google measures this from real visitors (Interaction to Next Paint); we don\'t have that '
                          'feed, so this is the closest lab stand-in — total blocking time.</p></div>')
            continue
        if metric not in measured:
            continue
        value = measured[metric]
        t = _CWV[metric]
        v_word, col, read = _grade_cwv(metric, value)
        cwv_cards += ('<div class="cwv-card"><div class="cwv-top">'
                      f'<span class="cwv-name">{_e(t["short"])}</span>'
                      f'<span class="cwv-verdict" style="color:{col}">{v_word}</span></div>'
                      f'<div class="cwv-val" style="color:{col}">{_fmt_cwv(metric, value)} '
                      f'<small>target &lt; {_fmt_cwv(metric, t["good"])}</small>'
                      '<span class="cwv-lab">lab estimate</span></div>'
                      f'<p class="cwv-plain"><b>{_e(t["plain"][:1].upper() + t["plain"][1:])}.</b> {_e(read)}</p></div>')
    cwv_block = ""
    if cwv_cards:
        n_measured = len(measured) + (1 if inp_proxy else 0)
        overall_note = ('<div class="cwv-overall">These are single lab measurements of the three metrics Google '
                        'groups as Core Web Vitals — a reliable early read on the experience it rewards in search, '
                        'but not its official real-visitor verdict (that needs more live traffic than a controlled '
                        'test can stand in for).</div>') if n_measured else ""
        cwv_block = ('<h3 class="ps-h3">Your three Core Web Vitals — the experience Google actually grades</h3>'
                     f'{overall_note}<div class="cwv-grid">{cwv_cards}</div>{_src(_lab_src)}')

    # 3) Biggest speed opportunities --------------------------------------------------------------
    opp_block = ""
    opps = mob.get("opportunities") or []
    if opps:
        rows = ""
        for o in opps:
            ms = o.get("savings_ms") or 0
            secs = ms / 1000.0
            saved = f'{secs:.1f}s faster' if secs >= 0.1 else f'{ms}ms faster'
            rows += (f'<li><div class="opp-lab">{o.get("label") or ""}</div>'
                     f'<div class="opp-save">up to <b>{_e(saved)}</b></div></li>')
        opp_block = ('<h3 class="ps-h3">The biggest wins — where the seconds are hiding</h3>'
                     f'<ul class="opp-list">{rows}</ul>'
                     f'{_src("Source: Google Lighthouse (via DataForSEO) — estimated load-time saved on mobile for each fix. Every item is a specific, fixable thing on the page.")}')

    # 4) Revenue framing on avg_ticket + credible public benchmarks -------------------------------
    rev_block = ""
    load_ms = lab.get("lcp_ms")
    if load_ms is not None:
        load_s = load_ms / 1000.0
        ticket_txt = _fmt_money(ticket) if ticket else "a typical job"
        tkt_est = rev.get("avg_ticket_estimated")
        stat = None
        if load_s >= 3.0:
            headline = (f"Your main content takes about <b>{load_s:.1f} seconds</b> to appear on a phone. Google's "
                        "own research found <b>53% of mobile visits are abandoned when a page takes longer than 3 "
                        "seconds to load</b>.")
            stat = ("53%", "of mobile visitors give up before a 3s+ page loads",
                    "Source: Google / DoubleClick mobile speed research, 2016.")
        elif load_s >= 2.5:
            headline = (f"Your main content takes about <b>{load_s:.1f} seconds</b> on a phone — just over Google's "
                        "2.5-second 'good' bar. Bounce risk climbs steeply in exactly this 2–4 second band.")
            stat = ("+32%", "bounce risk as mobile load goes from 1s to 3s",
                    "Source: Google / SOASTA page-speed research, 2017.")
        else:
            headline = (f"Your main content appears in about <b>{load_s:.1f} seconds</b> on a phone — inside Google's "
                        "2.5-second target. That speed is quietly winning you visitors your slower competitors lose.")
        uplift = ""
        pct = 0
        if load_s > 2.5:
            gap = load_s - 2.5
            pct = min(60, round(gap * 10 * 8.4))   # ~8.4% conversion lift per 0.1s, capped conservatively
            job_line = (f" On a job worth {ticket_txt}{' (est.)' if tkt_est else ''}, that lift lands straight on "
                        "your bottom line — same ads, same calls, more of them turning into paid work.") if ticket else ""
            uplift = ('<p style="margin:14px 0 0">Google and Deloitte\'s <i>Milliseconds Make Millions</i> study '
                      'found every 0.1s of mobile speed lifts conversions ~8.4%. Getting you back under the 2.5s bar '
                      f'is roughly a <b>{pct}% lift in enquiries from the traffic you already have</b>.{job_line}</p>')
        stat_html = f'<div class="oppstat light"><b>{stat[0]}</b><span>{_e(stat[1])}</span></div>' if stat else ""
        abs_html = ""
        monthly = rev.get("monthly")
        if monthly and pct:   # absolute $ ONLY off a real model figure — never fabricated here
            recover = round(float(monthly) * (pct / 100.0))
            if recover >= 1:
                abs_html = (f'<div class="oppstat light"><b>{_fmt_money(recover)}<i>/mo</i></b><span>illustrative work '
                            'recoverable from speed alone, on your current pipeline</span></div>')
        grid = f'<div class="oppgrid" style="margin-top:6px">{stat_html}{abs_html}</div>' if (stat_html or abs_html) else ""
        cap = ("Public benchmarks applied illustratively to your measured mobile load — a way to size the cost of "
               "slowness, not a promise. Uplift uses the Deloitte/Google 8.4%-per-0.1s figure, capped conservatively.")
        rev_block = ('<div class="oppcard ps-cost"><h3 style="margin-top:0">What a slow phone experience costs you</h3>'
                     f'<p>{headline}</p>{grid}{uplift}'
                     f'<div class="srccap onopp">{_e((stat[2] + " " if stat else "") + cap)}</div></div>')

    body = f'{top}{cwv_block}{opp_block}{rev_block}'
    intro = _clean_prose(nv.get("intro_pagespeed"), allowed,
                         "Most of your customers meet you on a phone first. Here's how fast that first impression "
                         "loads — measured with Google's own Lighthouse engine — and what the wait is costing you.")
    return _sec("Speed &amp; Core Web Vitals",
                "How fast your site feels on a phone — and what slow costs you", intro, body)


# --- extra CSS to append to the _CSS string (uses the existing cstda tokens) ------------------
_PAGESPEED_CSS = """
.ps-hero{display:flex;gap:28px;align-items:center;flex-wrap:wrap;margin-top:6px}
.ps-hero-txt{flex:1;min-width:240px}
.ps-h3{font-size:19px;margin:30px 0 12px}
.ps-chips{display:flex;gap:12px;flex-wrap:wrap;margin-top:14px}
.ps-chip{background:var(--paper-2);border:1px solid var(--line);border-radius:12px;padding:11px 16px;min-width:96px}
.ps-chip b{display:block;font-family:var(--serif);font-size:22px;line-height:1}
.ps-chip span{font-size:11.5px;color:var(--muted);display:block;margin-top:4px}
.cwv-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:16px}
@media(max-width:640px){.cwv-grid{grid-template-columns:1fr}.ps-hero{gap:16px}}
.cwv-card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:18px 20px;box-shadow:var(--shadow)}
.cwv-top{display:flex;justify-content:space-between;align-items:baseline;gap:10px}
.cwv-name{font-size:13px;font-weight:700;color:var(--ink-2)}
.cwv-verdict{font-size:12px;font-weight:800;letter-spacing:.04em;text-transform:uppercase}
.cwv-val{font-family:var(--serif);font-size:30px;color:var(--ink);margin:8px 0 6px;line-height:1}
.cwv-val small{font-family:var(--sans);font-size:12px;color:var(--muted);font-weight:600;margin-left:6px}
.cwv-lab{display:inline-block;font-family:var(--sans);font-size:10.5px;font-weight:700;letter-spacing:.04em;
  text-transform:uppercase;color:#8a5410;background:var(--amber-soft);border-radius:6px;padding:2px 7px;margin-left:8px;vertical-align:middle}
.cwv-plain{font-size:13px;color:var(--ink-2);margin:0;line-height:1.5}
.cwv-overall{font-size:14px;border-radius:10px;padding:12px 15px;margin:2px 0 16px;background:var(--paper-2);border:1px solid var(--line);color:var(--ink-2)}
.opp-list{list-style:none;margin:8px 0 0;padding:0;display:grid;gap:10px}
.opp-list li{display:flex;justify-content:space-between;align-items:center;gap:16px;background:var(--paper-2);border:1px solid var(--line);border-radius:10px;padding:13px 16px}
.opp-lab{font-size:14.5px;color:var(--ink);font-weight:600}
.opp-save{font-size:13px;color:var(--muted);white-space:nowrap}.opp-save b{color:var(--forest)}
.ps-cost{margin-top:26px}
"""


_CSS = _CSS + _PAGESPEED_CSS  # register the PageSpeed section styles into the page stylesheet
