"""Lisa-1 — the AI cold-caller subsystem (talk-only architecture).

Design (owner-confirmed): Lisa is a MOUTH, not a brain or hands. Everything she might need to say is
pre-computed by the Brief-builder and injected as Retell dynamic variables BEFORE the call; everything she
"does" (booking, SMS, recording the outcome) happens HERE, server-side, AFTER the call — so she can never
hallucinate a fact she was handed or fumble a tool she doesn't have.

This subsystem is deliberately ISOLATED from the 3CX/Aircall funnel:
  • its own table `lisa_calls` (never mixed into `calls`), and
  • admin-only endpoints (Raj/Vysakh),
so Lisa's numbers never leak into team totals, the TV kiosk, or any BDE/BDM view.

Brand safety: to a prospect the brand is ALWAYS "Digital Expo"; "Traffic Radius" is never spoken.
Pieces:
  build_brief()     -> the injected dynamic variables (real audit insight + context + objection lines)
  start_call()      -> Retell create-phone-call with the brief; logs a pending lisa_calls row
  handle_postcall() -> parse Retell's call_analyzed webhook; record outcome; book if agreed; SMS if missed
"""
from __future__ import annotations

import base64
import json
import re
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from psycopg_pool import ConnectionPool

from .config import Settings
from .logging import get_logger

log = get_logger(__name__)

RETELL_BASE = "https://api.retellai.com"
_TZ = "Australia/Melbourne"

# HARD BLOCK: caller-IDs Lisa may NEVER use for any activity. The US numbers on the Retell account are
# permanently blocked — Lisa dials ONLY from her configured Australian numbers (LISA_FROM_NUMBERS).
_BLOCKED_FROM = {"+16592899020", "+17073589606"}


# --------------------------------------------------------------------------- #
# schema
# --------------------------------------------------------------------------- #
def ensure_tables(pool: ConnectionPool) -> None:
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "CREATE TABLE IF NOT EXISTS lisa_calls ("
            "  call_id text PRIMARY KEY,"
            "  dest9 text, to_number text, from_number text,"
            "  prospect_name text, company_name text, domain text, prospect_email text,"
            "  status text DEFAULT 'pending',"           # pending -> ongoing -> ended -> analyzed
            "  call_outcome text, meeting_agreed boolean DEFAULT false, agreed_day_time text,"
            "  confirmed_email text, callback_when text, main_objection text,"
            "  asked_if_ai boolean DEFAULT false, call_summary text,"
            "  transcript text, recording_url text, duration_ms integer, cost_cents numeric,"
            "  disconnect_reason text, booked_event_id integer, sms_sent boolean DEFAULT false,"
            "  brief jsonb, started_at timestamptz DEFAULT now(), ended_at timestamptz,"
            "  created_at timestamptz DEFAULT now(), updated_at timestamptz DEFAULT now())")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_lisa_calls_dest9 ON lisa_calls(dest9)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_lisa_calls_created ON lisa_calls(created_at)")
        # post-booking qualification answers (current setup / spend / goals / challenge / timeline / …)
        # captured from the call so the strategist walks in prepped + we qualify internally.
        cur.execute("ALTER TABLE lisa_calls ADD COLUMN IF NOT EXISTS qualification jsonb")
        # Lisa's EXCLUSIVE reserved prospect pool — these 500 are hers alone; the human fresh allocator
        # skips any dest9 that appears here, so no double-calling.
        cur.execute(
            "CREATE TABLE IF NOT EXISTS lisa_pool ("
            "  dest9 text PRIMARY KEY, dest_number text, domain text, company text, phone text,"
            "  reserved_at timestamptz DEFAULT now())")
        # Per-PROSPECT brief store — every variable Lisa will be handed, saved against the prospect (dest9)
        # so it's persistent, reviewable, and injected at call time (not recomputed each call).
        cur.execute(
            "CREATE TABLE IF NOT EXISTS lisa_briefs ("
            "  dest9 text PRIMARY KEY, domain text, prospect_name text, company_name text, brief jsonb,"
            "  built_at timestamptz DEFAULT now(), updated_at timestamptz DEFAULT now())")
        # AI Sales Coach output — the learned PLAYBOOK: best objection lines (from WON calls) + the
        # avoid-list (from LOST calls). One current row (id=1), refreshed on a cadence by refresh_playbook.
        cur.execute("CREATE TABLE IF NOT EXISTS lisa_playbook ("
                    "  id integer PRIMARY KEY, playbook jsonb, built_at timestamptz DEFAULT now())")
        # per-call QA / coaching scores (the Head-of-Sales scorecard).
        cur.execute("CREATE TABLE IF NOT EXISTS lisa_call_reviews ("
                    "  call_id text PRIMARY KEY, scores jsonb, reviewed_at timestamptz DEFAULT now())")
        # Head of Sales · Strategist output — the current orchestration decision + what it actually did each
        # cycle (policy, directive, actions taken, compliance alerts). One row (id=1). Surfaced in the console
        # so the role shows real work, not a label.
        cur.execute("CREATE TABLE IF NOT EXISTS lisa_strategy ("
                    "  id integer PRIMARY KEY, strategy jsonb, updated_at timestamptz DEFAULT now())")
        # value-rank of the reserved pool (live ad creatives = spend proxy) so the highest-spending
        # advertisers are called first; set by the Head of Sales each cycle, consumed by schedule_lisa_fresh.
        cur.execute("ALTER TABLE lisa_pool ADD COLUMN IF NOT EXISTS priority integer DEFAULT 0")
        conn.commit()
    ensure_lisa_agent(pool)


def ensure_lisa_agent(pool: ConnectionPool) -> None:
    """Register Lisa as an in-scope, active BDE so the leaderboard / funnel / reports treat her like any
    other rep (isolation to admins is handled separately by PRIVATE_BDE_NAMES). Idempotent; re-asserted
    each cycle so a 3CX roster-sync can't drop her (she isn't a 3CX agent)."""
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO bde_agents (extension, bde_name, email, group_name, role_name, in_scope, active, "
            "  synced_at) VALUES ('LISA','Lisa','lisa@trafficradius.com.au','AI','AI BDE',true,true,now()) "
            "ON CONFLICT (extension) DO UPDATE SET bde_name='Lisa', in_scope=true, active=true, "
            "  role_name='AI BDE'")
        conn.commit()


def _fetch(pool, sql, params=None):
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(sql, params or ())
        return cur.fetchall()


def _d9(s: str | None) -> str:
    return re.sub(r"[^0-9]", "", s or "")[-9:]


def _e164_au(num: str | None) -> str:
    """Normalise a phone number to E.164 (Retell requires it — it 400s on '0433…' or spaced numbers).
    Handles the AU formats people actually type: '0433 136 022' → '+61433136022', '61…' → '+61…',
    a bare 9-digit mobile → '+61…'. Anything already starting with '+' is kept."""
    s = (num or "").strip()
    if s.startswith("+"):
        return "+" + re.sub(r"[^0-9]", "", s)
    d = re.sub(r"[^0-9]", "", s)
    if not d:
        return ""
    if d.startswith("0"):
        return "+61" + d[1:]
    if d.startswith("61"):
        return "+" + d
    if len(d) == 9:              # mobile/local without the leading 0
        return "+61" + d
    return "+" + d


# --------------------------------------------------------------------------- #
# Brief-builder — the heart of talk-only: pre-compute everything Lisa will say
# --------------------------------------------------------------------------- #
def _money(v) -> str:
    try:
        v = float(v)
    except Exception:
        return ""
    if v >= 1000:
        return f"${round(v/1000):,}k"
    return f"${int(v):,}"


def _spoken_findings(audit: dict, niche: str) -> dict:
    """Turn the audit model into 1-2 SPOKEN, curiosity-friendly insights + a plain proof point + the one
    channel to lead with. Conversational, never a data dump — Lisa reads these as her own observation."""
    niche = niche or "your industry"
    running_ads = bool((audit.get("ads") or {}).get("running") or (audit.get("ads") or {}).get("count"))
    opp = audit.get("opportunity") or {}
    comps = audit.get("competitors") or []
    top = comps[0] if comps else {}
    gap = opp.get("gap_capturable") or opp.get("gap_value") or 0
    quickwin = opp.get("quickwin_value") or 0

    f1 = f2 = proof = ""
    channel = "your online presence"
    if running_ads and (gap or comps):
        f1 = ("from what I'm seeing you're already paying for Google Ads — but there's a fair bit of the "
              "free search demand, people actively looking for what you do, that seems to be landing on "
              "competitors instead of you")
        channel = "the free/organic side alongside your ads"
        if top.get("domain"):
            proof = (f"for instance {top.get('domain')} looks like it's pulling a good chunk of those "
                     "searches in your space that you're not showing up for")
        elif gap:
            proof = f"there's roughly {_money(gap)} a month of that search demand within reach"
    elif comps and (gap or top):
        f1 = (f"a couple of your competitors seem to be showing up for searches your customers are "
              f"actually typing in — and from what I can see, you're not on that page for a lot of them")
        channel = "search / SEO"
        if top.get("domain"):
            proof = (f"{top.get('domain')} is one of the main ones capturing that demand")
    elif quickwin:
        f1 = ("you've actually got pages sitting just off the first page of Google for terms people search "
              "a lot — really close to the top, which is usually the fastest win")
        channel = "SEO quick-wins"
        proof = f"there's around {_money(quickwin)} a month of traffic value sitting right there"
    else:
        # generic but niche-shaped fallback (used only if the audit has no strong signal)
        f1 = (f"in {niche}, a lot of ready-to-buy demand comes through search — and often some of it slips "
              "to competitors when the path from search to enquiry isn't clean")
        channel = "search / SEO"

    # a second, supporting insight only if it strengthens the same story
    if running_ads and quickwin and "ads" in channel:
        f2 = ("and there are a few pages already close to page one that a small push would lift, which "
              "usually brings the cost-per-lead down on the paid side too")
    elif comps and len(comps) > 1:
        f2 = ""
    return {"finding_1": f1, "finding_2": f2, "finding_proof": proof, "primary_channel": channel,
            "competitor_hook": (top.get("domain") or "")}


def _domain_for(pool: ConnectionPool, d9: str) -> str | None:
    """Best-known domain for a prospect (from the reserved pool or a saved brief)."""
    for tbl in ("lisa_pool", "lisa_briefs"):
        r = _fetch(pool, f"SELECT domain FROM {tbl} WHERE dest9=%s AND NULLIF(domain,'') IS NOT NULL LIMIT 1", (d9,))
        if r:
            return r[0]["domain"]
    return None


_AU_TZ = {"new south wales": "Australia/Sydney", "victoria": "Australia/Melbourne",
          "queensland": "Australia/Brisbane", "south australia": "Australia/Adelaide",
          "western australia": "Australia/Perth", "northern territory": "Australia/Darwin",
          "tasmania": "Australia/Hobart", "australian capital territory": "Australia/Sydney",
          "nsw": "Australia/Sydney", "vic": "Australia/Melbourne", "qld": "Australia/Brisbane",
          "sa": "Australia/Adelaide", "wa": "Australia/Perth", "nt": "Australia/Darwin",
          "tas": "Australia/Hobart", "act": "Australia/Sydney"}


def _prospect_local_hour(pool: ConnectionPool, d9: str, default_tz: str = "Australia/Melbourne") -> tuple[int, str]:
    """The prospect's CURRENT local hour, from their state (companies/apollo) → IANA tz (DST-aware). So we
    never ring a WA prospect at 7am Perth time. Falls back to Melbourne when the state is unknown."""
    dom = _domain_for(pool, d9)
    st = ""
    if dom:
        r = _fetch(pool, "SELECT state FROM companies WHERE domain=%s AND NULLIF(state,'') IS NOT NULL LIMIT 1", (dom,))
        st = (r[0]["state"] if r else "") or ""
        if not st:
            a = _fetch(pool, "SELECT apollo->>'state' s FROM enrichment WHERE domain=%s", (dom,))
            st = (a[0]["s"] if a else "") or ""
    tz = _AU_TZ.get(st.strip().lower(), default_tz)
    return datetime.now(ZoneInfo(tz)).hour, tz


def ensure_audit(pool: ConnectionPool, settings: Settings, domain: str) -> bool:
    """Run the Digital-Marketing-Insight audit (competitor gap + SEO) for a domain so Lisa can open with
    real hooks. run_competitor_audit is CACHED (free if already done) and hard cost-capped. Returns True
    if the domain now has competitor-audit data. Never raises."""
    if not domain:
        return False
    try:
        from .competitor import run_competitor_audit
        run_competitor_audit(pool, settings, domain)   # cached; caps DataForSEO calls per domain
    except Exception as exc:
        log.warning("lisa_ensure_audit_failed", domain=domain, error=str(exc)[:160])
    r = _fetch(pool, "SELECT (dataforseo ? 'competitor_audit') a FROM enrichment WHERE domain=%s", (domain,))
    return bool(r and r[0].get("a"))


_DM_TITLES = ["owner", "founder", "co-founder", "director", "managing director", "chief marketing",
              "cmo", "head of marketing", "marketing manager", "marketing director", "ceo",
              "general manager", "principal", "proprietor"]


def _prospect_facts(pool: ConnectionPool, domain: str) -> dict:
    """Spoken-friendly facts about the business from apollo + business_intel + companies (D&B) — what they
    actually do, where they are, their likely decision-maker's role, and their ideal customer. These let
    Lisa be SPECIFIC and LOCAL instead of generic. Reliable across the pool; used as context, not a script."""
    if not domain:
        return {"what_they_do": "", "location": "", "dm_role": "", "icp": ""}
    r = _fetch(pool, "SELECT apollo, business_intel FROM enrichment WHERE domain=%s", (domain,))
    ap = (r[0].get("apollo") if r else None) or {}
    bi = (r[0].get("business_intel") if r else None) or {}
    cr = _fetch(pool, "SELECT suburb, state FROM companies WHERE domain=%s AND source='raghav' LIMIT 1", (domain,))
    cro = cr[0] if cr else {}
    svc = [s for s in (bi.get("services") or []) if s][:2]
    prod = [p for p in (bi.get("products") or []) if p][:2]
    what = ", ".join(dict.fromkeys(svc + prod)) or ", ".join([k for k in (ap.get("keywords") or []) if k][:3])
    loc = ", ".join([x for x in (ap.get("city"), ap.get("state")) if x]) \
        or ", ".join([x for x in (cro.get("suburb"), cro.get("state")) if x])
    people = ap.get("people") or []
    dm_role = ""; dm_name = ""
    for kw in _DM_TITLES:
        for p in people:
            if kw in (p.get("title") or "").lower():
                dm_role = p.get("title")
                dm_name = (p.get("first_name") or (p.get("name") or "").split(" ")[0] or "").strip()
                break
        if dm_role:
            break
    icp = (bi.get("icp") or bi.get("target_audience") or "").strip()
    return {"what_they_do": what[:180], "location": loc[:60], "dm_role": (dm_role or "")[:60],
            "dm_name": dm_name[:40], "icp": icp[:200]}


def _enrichment_signals(pool: ConnectionPool, domain: str) -> dict:
    """Cheap, already-stored DataForSEO signals for a domain (no paid call): is it running Google Ads,
    and how many live creatives. Available for the whole GAds pool even without a full SEO audit."""
    r = _fetch(pool, "SELECT dataforseo df FROM enrichment WHERE domain=%s", (domain,))
    if not r or not r[0].get("df"):
        return {}
    df = r[0]["df"] or {}
    ads = df.get("ads") or {}
    return {"running_ads": str(df.get("running_google_ads")).lower() == "true",
            "ads_count": ads.get("count")}


def _ads_finding(sig: dict, niche: str) -> dict:
    """A REAL, specific hook for a confirmed Google-Ads advertiser (true for the whole pilot pool): they're
    paying for clicks, and the free/organic demand next to those ads is the gap worth a look."""
    n = sig.get("ads_count")
    f1 = ("from what I can see you're actively running Google Ads — so you're clearly investing in getting "
          "found, which is great; the thing I noticed is there's usually a good slice of free, organic "
          "search demand sitting right next to those ads that tends to leak across to competitors")
    proof = (f"you've actually got around {n} ad creatives live right now, so you're definitely serious "
             "about it" if n else "")
    return {"finding_1": f1, "finding_2": "", "finding_proof": proof,
            "primary_channel": "the free/organic side next to your ads", "competitor_hook": ""}


def build_brief(pool: ConnectionPool, settings: Settings, *, dest9: str | None = None,
                domain: str | None = None, skip_history: bool = False) -> dict:
    """Compute the Retell dynamic variables for one prospect: identity + the REAL audit insight + prior
    context + tailored objection lines. Everything Lisa is allowed to say, handed to her up front.
    Returns a flat {str: str} map (Retell dynamic variables must be strings).

    skip_history=True skips the (un-indexed) calls-history scan — use it for FRESH pool prospects (never
    called → no history to summarise), which keeps briefing the whole 500 fast."""
    dest9 = _d9(dest9) if dest9 else None
    b: dict = {}

    # --- identity from our own call intelligence / master / companies ---
    row = None
    if dest9 and not skip_history:
        r = _fetch(pool,
            "SELECT (array_agg(cl.prospect_company ORDER BY c.started_at DESC) "
            "          FILTER (WHERE NULLIF(cl.prospect_company,'') IS NOT NULL))[1] company, "
            "       (array_agg(cl.prospect_website ORDER BY c.started_at DESC) "
            "          FILTER (WHERE NULLIF(cl.prospect_website,'') IS NOT NULL))[1] website, "
            "       (array_agg(cl.prospect_contact_name ORDER BY c.started_at DESC) "
            "          FILTER (WHERE NULLIF(cl.prospect_contact_name,'') IS NOT NULL))[1] contact, "
            "       (array_agg(cl.prospect_industry ORDER BY c.started_at DESC) "
            "          FILTER (WHERE NULLIF(cl.prospect_industry,'') IS NOT NULL))[1] industry, "
            "       (array_agg(cl.prospect_email ORDER BY c.started_at DESC) "
            "          FILTER (WHERE NULLIF(cl.prospect_email,'') IS NOT NULL))[1] email, "
            "       bool_or(cl.rpc_connect) ever_rpc, bool_or(cl.has_marketing_agency) ever_agency, "
            "       (array_agg(cl.lead_temperature ORDER BY c.started_at DESC) "
            "          FILTER (WHERE NULLIF(cl.lead_temperature,'') IS NOT NULL))[1] lead_temp, "
            "       (array_agg(cl.callback_when ORDER BY c.started_at DESC) "
            "          FILTER (WHERE NULLIF(cl.callback_when,'') IS NOT NULL))[1] cb_when, "
            "       (array_agg(cl.problem_summary ORDER BY c.started_at DESC) "
            "          FILTER (WHERE NULLIF(cl.problem_summary,'') IS NOT NULL))[1] problem "
            "FROM calls c JOIN classifications cl ON cl.call_id=c.call_id "
            "WHERE c.in_scope AND right(regexp_replace(COALESCE(c.dest_number,''),'[^0-9]','','g'),9)=%s",
            (dest9,))
        row = r[0] if r else None
    if row and not domain:
        domain = _clean_domain(row.get("website"))
    if not domain and dest9 and not skip_history:
        pr = _fetch(pool, "SELECT domain, business_name FROM prospect_pipeline WHERE dest9=%s AND NULLIF(domain,'') IS NOT NULL LIMIT 1", (dest9,))
        if pr:
            domain = pr[0].get("domain")

    company = (row or {}).get("company") or ""
    niche = (row or {}).get("industry") or ""
    email = (row or {}).get("email") or ""
    contact = (row or {}).get("contact") or ""

    # company/industry from companies table if missing
    if domain and (not company or not niche):
        cr = _fetch(pool, "SELECT company_name AS business_name, industry FROM companies WHERE domain=%s LIMIT 1", (domain,))
        if cr:
            company = company or (cr[0].get("business_name") or "")
            niche = niche or (cr[0].get("industry") or "")

    # --- the REAL insight, best source first: full SEO audit > confirmed-Google-Ads signal > niche generic ---
    findings = None
    if domain:
        try:
            # CHEAP gate: only run the (heavy) full audit for domains that actually hold competitor SEO
            # data — otherwise skip straight to the fast running-ads finding. Keeps brief-building quick
            # across the whole 500 (the full audit exists for only a handful of domains).
            has_ca = _fetch(pool, "SELECT (dataforseo ? 'competitor_audit') a FROM enrichment WHERE domain=%s", (domain,))
            if has_ca and has_ca[0].get("a"):
                from .audit import assemble_audit
                audit = assemble_audit(pool, domain)
                _opp = (audit or {}).get("opportunity") or {}
                if audit and (audit.get("competitors") or _opp.get("quickwin_value") or _opp.get("gap_capturable")):
                    niche = niche or ((audit.get("business") or {}).get("industry") or "")
                    findings = _spoken_findings(audit, niche)
        except Exception as exc:
            log.warning("lisa_brief_audit_failed", domain=domain, error=str(exc)[:160])
    if (not findings or not findings.get("finding_1")) and domain:
        sig = _enrichment_signals(pool, domain)
        if sig.get("running_ads"):
            findings = _ads_finding(sig, niche)
    if not findings or not findings.get("finding_1"):
        findings = _spoken_findings({}, niche)

    # --- prior-contact hook (warm) — reference the last chat so a repeat call feels like a follow-up ---
    prior = ""
    if row and row.get("ever_rpc"):
        topic = row.get("problem")
        prior = ("I think one of our team had a quick chat with someone there recently"
                 + (f" about {topic}" if topic else "") + " — I'm really just following that up")

    # --- known context so Lisa never re-asks + tailors a warm call (prior problem / agency / interest / callback) ---
    kb = []
    if row:
        if row.get("problem"):
            kb.append(f"Last time they mentioned: {row['problem']}")
        if row.get("ever_agency"):
            kb.append("They already have a marketing agency (so position the session as an independent second opinion)")
        if row.get("lead_temp"):
            kb.append(f"Prior interest level: {row['lead_temp']}")
        if row.get("cb_when"):
            kb.append(f"They'd asked us to call back around: {row['cb_when']}")
    known = " · ".join(kb)

    # --- objection lines + avoid-list from the LEARNED PLAYBOOK (AI Sales Coach mines won/lost calls);
    #     fall back to sensible defaults until the coach has run. ---
    pb = get_playbook(pool)
    obj_agency = pb.get("objection_agency") or (
        "Good — makes sense. The session's completely independent, so it either confirms your agency's "
        "nailing it, or gives you sharper questions to ask them. Either way you come out ahead.")
    obj_price = pb.get("objection_price") or (
        "The session and the audit are free — pricing only comes up later and it's tailored, which is "
        "exactly what the fifteen minutes is for.")
    obj_email = pb.get("objection_email") or (
        "Happy to send something — so it's actually useful and not just another email you ignore, what's "
        "the one thing about your online enquiries you'd most want answered?")
    avoid_list = " · ".join(pb.get("avoid") or []) if isinstance(pb.get("avoid"), list) else ""

    b.update({
        "prospect_name": contact or "",
        "company_name": company or "",
        "prospect_website": domain or ((row or {}).get("website") or ""),
        "prospect_niche": niche or "",
        "prospect_email": email or "",
        "decision_maker": contact or "",
        "finding_1": findings["finding_1"],
        "finding_2": findings["finding_2"],
        "finding_proof": findings["finding_proof"],
        "primary_channel": findings["primary_channel"],
        "competitor_hook": findings["competitor_hook"],
        "known_context": known,
        "prior_contact_line": prior,
        "objection_agency": obj_agency,
        "objection_price": obj_price,
        "objection_email": obj_email,
        "objection_not_interested": pb.get("objection_not_interested") or "",
        "objection_no_time": pb.get("objection_no_time") or "",
        "avoid_list": avoid_list,   # what our lost-call analysis says NOT to do
    })
    # rich, spoken facts (apollo + business_intel + D&B) so Lisa is specific & local, not generic
    facts = _prospect_facts(pool, domain) if domain else {}
    b.update({
        "what_they_do": facts.get("what_they_do", ""),   # tailor examples to their actual products/services
        "location": facts.get("location", ""),           # a local touch ("customers around <city>…")
        "dm_role": facts.get("dm_role", ""),             # ask the gatekeeper for the right role
        "dm_name": facts.get("dm_name", ""),             # ask the gatekeeper for them BY NAME (bypass)
        "ideal_customer": facts.get("icp", ""),          # who their buyers are
    })
    if facts.get("dm_name") and not contact:
        b["decision_maker"] = facts["dm_name"]
    if facts.get("what_they_do") and not niche:
        b["prospect_niche"] = facts["what_they_do"]
    # Retell wants strings; drop Nones
    return {k: ("" if v is None else str(v)) for k, v in b.items()}


def build_and_store_brief(pool: ConnectionPool, settings: Settings, *, dest9: str, domain: str | None = None,
                          skip_history: bool = False) -> dict:
    """Compute a prospect's brief and PERSIST it against that prospect (dest9) in lisa_briefs, so every
    variable Lisa will say is saved in the DB, reviewable, and reused at call time. Returns the brief."""
    ensure_tables(pool)
    d9 = _d9(dest9)
    brief = build_brief(pool, settings, dest9=d9, domain=domain, skip_history=skip_history)
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO lisa_briefs (dest9, domain, prospect_name, company_name, brief, built_at, updated_at) "
            "VALUES (%s,%s,%s,%s,%s,now(),now()) "
            "ON CONFLICT (dest9) DO UPDATE SET domain=EXCLUDED.domain, prospect_name=EXCLUDED.prospect_name, "
            "  company_name=EXCLUDED.company_name, brief=EXCLUDED.brief, updated_at=now()",
            (d9, brief.get("prospect_website"), brief.get("prospect_name"), brief.get("company_name"),
             json.dumps(brief)))
        conn.commit()
    return brief


def get_brief(pool: ConnectionPool, settings: Settings, *, dest9: str, domain: str | None = None) -> dict:
    """Return a prospect's SAVED brief (building + storing it once if missing)."""
    d9 = _d9(dest9)
    r = _fetch(pool, "SELECT brief FROM lisa_briefs WHERE dest9=%s", (d9,))
    if r and r[0].get("brief"):
        return r[0]["brief"]
    return build_and_store_brief(pool, settings, dest9=d9, domain=domain)


def refresh_lisa_briefs(pool: ConnectionPool, settings: Settings, *, limit: int = 200) -> dict:
    """Pre-compute + save a brief for every reserved-pool prospect that doesn't have one yet (a batch per
    cycle so it never blocks the loop). After a few cycles all 500 have their variables saved in the DB."""
    ensure_tables(pool)
    rows = _fetch(pool,
        "SELECT lp.dest9, lp.domain FROM lisa_pool lp "
        "LEFT JOIN lisa_briefs b ON b.dest9=lp.dest9 WHERE b.dest9 IS NULL LIMIT %s", (limit,))
    n = 0
    for r in rows:
        try:
            # pool prospects are FRESH (never called) → skip the un-indexed calls-history scan.
            build_and_store_brief(pool, settings, dest9=r["dest9"], domain=r.get("domain"), skip_history=True)
            n += 1
        except Exception as exc:
            log.warning("lisa_brief_store_failed", dest9=r.get("dest9"), error=str(exc)[:120])
    return {"built": n, "remaining_without_brief": max(0, len(rows) - n)}


def _clean_domain(url: str | None) -> str | None:
    if not url:
        return None
    u = re.sub(r"^https?://", "", url.strip().lower()).split("/")[0]
    u = u[4:] if u.startswith("www.") else u
    return u or None


# --------------------------------------------------------------------------- #
# Retell API
# --------------------------------------------------------------------------- #
def _retell(settings: Settings, method: str, path: str, body: dict | None = None) -> dict:
    key = getattr(settings, "retellai_api_key", "") or ""
    req = urllib.request.Request(
        f"{RETELL_BASE}/{path}",
        data=(json.dumps(body).encode() if body is not None else None), method=method,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    resp = urllib.request.urlopen(req, timeout=45)
    t = resp.read().decode()
    return json.loads(t) if t.strip() else {}


def create_web_call(settings: Settings) -> dict:
    """Create a Retell WEB call (browser voice test / 'Voice Orb') for the Lisa agent. Returns
    {access_token, call_id} — the console uses the Retell web SDK + this token to let anyone talk to Lisa
    live from the page. No phone number, no cost of a PSTN call."""
    return _retell(settings, "POST", "v2/create-web-call", {"agent_id": getattr(settings, "lisa_agent_id", "")})


def start_call(pool: ConnectionPool, settings: Settings, *, to_number: str, dest9: str | None = None,
               domain: str | None = None, from_number: str | None = None) -> dict:
    """Kick off ONE Lisa call: build the brief, create the Retell phone call with the brief injected as
    dynamic variables, and log a pending lisa_calls row. Returns {call_id,...} or {error}."""
    ensure_tables(pool)
    if not getattr(settings, "lisa_enabled", False):
        return {"error": "lisa disabled (set LISA_ENABLED=true)"}
    froms = list(getattr(settings, "lisa_numbers", []) or [])
    # rotate the caller number DETERMINISTICALLY per prospect (spreads volume across numbers to protect
    # deliverability, but the SAME prospect always sees the SAME number — familiar on a callback).
    _d = _d9(dest9 or to_number)
    frm = from_number or (froms[(int(_d[-1]) if _d[-1:].isdigit() else 0) % len(froms)] if froms else None)
    if not frm:
        return {"error": "no LISA_FROM_NUMBERS configured"}
    # HARD BLOCK: Lisa dials ONLY from her configured AU numbers. Any blocked caller-ID (US numbers) or any
    # override outside the configured set is rejected outright — no US number is ever used for any activity.
    frm = _e164_au(frm)
    if frm in _BLOCKED_FROM or (froms and frm not in {_e164_au(x) for x in froms}):
        log.warning("lisa_blocked_caller_id", frm=frm)
        return {"error": f"blocked caller-ID {frm} — Lisa dials only from her configured AU numbers"}
    to_number = _e164_au(to_number)            # Retell 400s on non-E.164 (e.g. "0433…") — normalise first
    if len(re.sub(r"[^0-9]", "", to_number)) < 8:
        return {"error": f"invalid phone number: {to_number or '(empty)'}"}
    d9 = _d9(dest9 or to_number)
    # AUDIT-BEFORE-CALL: run the Digital-Marketing-Insight audit for this prospect (cached → free if done),
    # then REBUILD the brief so Lisa opens with the real audit hooks (competitor gap / keyword gap / quick
    # wins) instead of the generic running-ads line. Falls back gracefully if there's no domain/audit.
    dom = domain or _domain_for(pool, d9)
    if getattr(settings, "lisa_audit_before_call", True) and dom:
        if ensure_audit(pool, settings, dom):
            try:
                build_and_store_brief(pool, settings, dest9=d9, domain=dom)   # rebuild with the fresh audit
            except Exception as exc:
                log.warning("lisa_rebuild_after_audit_failed", domain=dom, error=str(exc)[:160])
    brief = get_brief(pool, settings, dest9=d9, domain=domain)   # saved per-prospect brief (builds once if missing)
    # overlay the CURRENT coach playbook so the objection library + avoid-list are always the latest,
    # even on a brief that was stored before the last coaching refresh.
    pb = get_playbook(pool)
    for k in ("objection_agency", "objection_price", "objection_email", "objection_not_interested", "objection_no_time"):
        if pb.get(k):
            brief[k] = str(pb[k])
    if isinstance(pb.get("avoid"), list) and pb["avoid"]:
        brief["avoid_list"] = " · ".join(pb["avoid"])
    body = {
        "from_number": frm,
        "to_number": to_number,
        "override_agent_id": getattr(settings, "lisa_agent_id", "") or None,
        "retell_llm_dynamic_variables": brief,
        "metadata": {"dest9": d9, "domain": brief.get("prospect_website") or (domain or "")},
    }
    try:
        r = _retell(settings, "POST", "v2/create-phone-call", body)
    except Exception as exc:
        log.warning("lisa_start_call_failed", to=to_number, error=str(exc)[:200])
        return {"error": str(exc)[:200]}
    cid = r.get("call_id")
    if cid:
        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO lisa_calls (call_id, dest9, to_number, from_number, prospect_name, "
                "  company_name, domain, prospect_email, status, brief) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'ongoing',%s) ON CONFLICT (call_id) DO NOTHING",
                (cid, d9, to_number, frm, brief.get("prospect_name"), brief.get("company_name"),
                 brief.get("prospect_website"), brief.get("prospect_email"), json.dumps(brief)))
            conn.commit()
    return r


# --------------------------------------------------------------------------- #
# Post-call webhook handling — record outcome, book if agreed, SMS if missed
# --------------------------------------------------------------------------- #
def handle_postcall(pool: ConnectionPool, settings: Settings, payload: dict) -> dict:
    """Process a Retell webhook. On call_analyzed we persist the structured outcome, book the agreed
    meeting server-side, and (if we only reached voicemail / no-answer) fire the minimal curiosity SMS.
    Idempotent per call_id."""
    ensure_tables(pool)
    event = payload.get("event") or ""
    call = payload.get("call") or payload
    cid = call.get("call_id")
    if not cid:
        return {"ok": False, "error": "no call_id"}

    analysis = call.get("call_analysis") or {}
    cad = analysis.get("custom_analysis_data") or {}
    meta = call.get("metadata") or {}
    dyn = call.get("retell_llm_dynamic_variables") or {}
    d9 = _d9(meta.get("dest9") or call.get("to_number"))
    outcome = (cad.get("call_outcome") or "").strip().lower()
    meeting_agreed = bool(cad.get("meeting_agreed"))
    cost_cents = None
    try:
        cost_cents = (call.get("call_cost") or {}).get("combined_cost")
    except Exception:
        cost_cents = None

    status = {"call_started": "ongoing", "call_ended": "ended", "call_analyzed": "analyzed"}.get(event, "ended")
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO lisa_calls (call_id, dest9, to_number, from_number, prospect_name, company_name, "
            "  domain, prospect_email, status, call_outcome, meeting_agreed, agreed_day_time, "
            "  confirmed_email, callback_when, main_objection, asked_if_ai, call_summary, transcript, "
            "  recording_url, duration_ms, cost_cents, disconnect_reason, brief, ended_at, updated_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,now(),now()) "
            "ON CONFLICT (call_id) DO UPDATE SET status=EXCLUDED.status, call_outcome=EXCLUDED.call_outcome, "
            "  meeting_agreed=EXCLUDED.meeting_agreed, agreed_day_time=EXCLUDED.agreed_day_time, "
            "  confirmed_email=EXCLUDED.confirmed_email, callback_when=EXCLUDED.callback_when, "
            "  main_objection=EXCLUDED.main_objection, asked_if_ai=EXCLUDED.asked_if_ai, "
            "  call_summary=EXCLUDED.call_summary, transcript=EXCLUDED.transcript, "
            "  recording_url=EXCLUDED.recording_url, duration_ms=EXCLUDED.duration_ms, "
            "  cost_cents=COALESCE(EXCLUDED.cost_cents, lisa_calls.cost_cents), "
            "  disconnect_reason=EXCLUDED.disconnect_reason, ended_at=now(), updated_at=now()",
            (cid, d9, call.get("to_number"), call.get("from_number"),
             dyn.get("prospect_name"), dyn.get("company_name"), dyn.get("prospect_website"),
             cad.get("confirmed_email") or dyn.get("prospect_email"), status, outcome, meeting_agreed,
             cad.get("agreed_day_time"), cad.get("confirmed_email"), cad.get("callback_when"),
             cad.get("main_objection"), bool(cad.get("asked_if_ai")), cad.get("call_summary"),
             call.get("transcript"), call.get("recording_url"),
             call.get("duration_ms") or call.get("call_length_ms"), cost_cents,
             call.get("disconnection_reason"), json.dumps(dyn) if dyn else None))
        conn.commit()

    # save the full custom-analysis payload (incl. the post-booking qualification answers) for the strategist.
    if cad:
        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute("UPDATE lisa_calls SET qualification=%s, updated_at=now() WHERE call_id=%s",
                        (json.dumps(cad), cid))
            conn.commit()

    result = {"ok": True, "call_id": cid, "event": event, "outcome": outcome}
    if event != "call_analyzed":
        return result  # actions only run on the final analyzed event

    # 0) mirror into the funnel so Lisa appears as a BDE in the leaderboard/reports (admin-isolated).
    try:
        _write_funnel_call(pool, cid, dyn, cad, call)
    except Exception as exc:
        log.warning("lisa_funnel_write_failed", call_id=cid, error=str(exc)[:160])
    # 0a-bis) COMPLIANCE: if the prospect asked not to be contacted, suppress them for good — drop from the
    # reserved pool and cancel every pending Lisa event so she never dials them again.
    if outcome == "do_not_call" and d9:
        try:
            with pool.connection() as conn, conn.cursor() as cur:
                cur.execute("DELETE FROM lisa_pool WHERE dest9=%s", (d9,))
                cur.execute("UPDATE calendar_events SET status='cancelled' WHERE bde_name='Lisa' "
                            "AND status='pending' AND right(regexp_replace(COALESCE(dest_number,''),"
                            "'[^0-9]','','g'),9)=%s", (d9,))
                conn.commit()
            result["suppressed_dnc"] = True
        except Exception as exc:
            log.warning("lisa_dnc_suppress_failed", call_id=cid, error=str(exc)[:160])
    else:
        # 0b) Lisa's own next-move (callback / retry) on her calendar.
        try:
            schedule_lisa_followup(pool, settings, dest9=d9, dest_number=call.get("to_number"),
                                   outcome=outcome, cad=cad, dyn=dyn)
        except Exception as exc:
            log.warning("lisa_followup_failed", call_id=cid, error=str(exc)[:160])

    # 1) book the agreed meeting server-side (Lisa never booked it herself)
    if meeting_agreed and cad.get("agreed_day_time"):
        try:
            eid = _book_meeting(pool, settings, cid, dyn, cad, d9)
            result["booked_event_id"] = eid
        except Exception as exc:
            log.warning("lisa_book_failed", call_id=cid, error=str(exc)[:160])

    # 2) minimal curiosity SMS if we didn't reach a real conversation
    if outcome in ("no_answer", "voicemail", "") and not meeting_agreed:
        try:
            if _send_followup_sms(pool, settings, cid, call.get("to_number"), dyn.get("prospect_name"),
                                  call_from=call.get("from_number")):
                result["sms_sent"] = True
        except Exception as exc:
            log.warning("lisa_sms_failed", call_id=cid, error=str(exc)[:160])
    return result


def _book_meeting(pool: ConnectionPool, settings: Settings, call_id: str, dyn: dict, cad: dict,
                  d9: str) -> int | None:
    """Create the agreed strategy session as a calendar event on Lisa's (isolated) calendar. We store the
    agreed time verbatim + a best-effort parsed datetime; a human strategist confirms the exact slot."""
    from .calendar import create_event
    when_txt = cad.get("agreed_day_time") or "time TBC"
    start = _parse_when(when_txt) or (datetime.now(ZoneInfo(_TZ)) + timedelta(days=1)).replace(
        hour=10, minute=0, second=0, microsecond=0)
    who = dyn.get("company_name") or dyn.get("prospect_name") or "prospect"
    email = cad.get("confirmed_email") or dyn.get("prospect_email") or ""
    # Full STRATEGIST BRIEF on the booked meeting: the audit hook Lisa used + what they do + the
    # qualification she captured — so the human closer walks in fully prepped.
    qbits = [(lbl, cad.get(k)) for lbl, k in (
        ("Current setup", "q_current_setup"), ("Biggest challenge", "q_biggest_challenge"),
        ("Monthly spend", "q_monthly_spend"), ("Goals (12–18mo)", "q_goals"),
        ("Timeline", "q_timeline"), ("Other decision-makers", "q_other_decision_makers"),
        ("Wants covered", "q_session_expectations")) if cad.get(k)]
    qual = "\n".join(f"  • {lbl}: {v}" for lbl, v in qbits) or "  • (not captured)"
    notes = (f"🎙️ Booked by Lisa (AI) — Digital Expo strategy session.\n"
             f"Agreed time (prospect's words): {when_txt}\n"
             f"Contact: {dyn.get('prospect_name') or ''}  ·  {email}\n"
             f"Company: {who}  ·  {dyn.get('prospect_website') or ''}\n"
             f"What they do: {dyn.get('what_they_do') or dyn.get('prospect_niche') or ''}"
             + (f"  ·  Based in {dyn.get('location')}" if dyn.get('location') else "") + "\n\n"
             f"🔬 Audit hook used: {dyn.get('finding_1') or ''}\n"
             f"   Proof: {dyn.get('finding_proof') or ''}"
             + (f"  ·  Competitor: {dyn.get('competitor_hook')}" if dyn.get('competitor_hook') else "") + "\n\n"
             f"📋 Qualification captured:\n{qual}\n\n"
             f"Call summary: {cad.get('call_summary') or ''}")
    eid = create_event(
        pool, bde_name="Lisa", type="meeting",
        title=f"📅 Strategy session (Lisa-booked): {who}",
        start_at=start, end_at=start + timedelta(minutes=int(getattr(settings, "lisa_session_minutes", 45))),
        notes=notes, dest_number=None, created_by="lisa")
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute("UPDATE lisa_calls SET booked_event_id=%s, updated_at=now() WHERE call_id=%s", (eid, call_id))
        conn.commit()
    return eid


_WD = {"monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3, "friday": 4, "saturday": 5, "sunday": 6,
       "mon": 0, "tue": 1, "tues": 1, "wed": 2, "thu": 3, "thur": 3, "thurs": 3, "fri": 4, "sat": 5, "sun": 6}


def _parse_when(txt: str) -> datetime | None:
    """Best-effort parse of a spoken time like 'Tuesday 2pm', 'tomorrow 10am', 'Friday afternoon' into a
    future Melbourne datetime. Never authoritative — the human strategist confirms — just seeds the slot."""
    if not txt:
        return None
    t = txt.lower().strip()
    now = datetime.now(ZoneInfo(_TZ))
    hour = 10
    m = re.search(r"(\d{1,2})\s*(?::(\d{2}))?\s*(am|pm)", t)
    if m:
        hour = int(m.group(1)) % 12 + (12 if m.group(3) == "pm" else 0)
    elif "afternoon" in t:
        hour = 14
    elif "morning" in t:
        hour = 10
    elif "evening" in t:
        hour = 17
    day = None
    if "tomorrow" in t:
        day = now.date() + timedelta(days=1)
    elif "today" in t:
        day = now.date()
    else:
        for name, wd in _WD.items():
            if re.search(rf"\b{name}\b", t):
                ahead = (wd - now.weekday()) % 7
                ahead = ahead or 7
                day = now.date() + timedelta(days=ahead)
                break
    if not day:
        day = now.date() + timedelta(days=1)
    return datetime(day.year, day.month, day.day, min(max(hour, 8), 18), 0, tzinfo=ZoneInfo(_TZ))


def _pick_sms_from(settings: Settings, call_from: str | None = None) -> str:
    """Choose the SMS-capable number to text from. If the number Lisa CALLED from is itself SMS-capable,
    text from that (familiar to the prospect); otherwise use the primary SMS number. All from the
    SMS-capable set only."""
    nums = [_e164_au(n) for n in getattr(settings, "lisa_sms_number_list", []) if n]
    if not nums:
        return ""
    cf = _e164_au(call_from) if call_from else ""
    return cf if cf in nums else nums[0]


def _send_sms_twilio(settings: Settings, to_number: str, body: str, from_number: str) -> bool:
    """Send one SMS DIRECTLY via the Twilio REST API (backend process — never through the Retell voice
    agent, so it can't touch Lisa's call latency). Uses a Messaging Service if configured, else the given
    from-number. Returns True on 2xx. Raises on HTTP error (caller logs)."""
    sid = getattr(settings, "twilio_account_sid", "") or ""
    tok = getattr(settings, "twilio_auth_token", "") or ""
    if not (sid and tok):
        return False
    params = {"To": _e164_au(to_number), "Body": body}
    msvc = getattr(settings, "twilio_messaging_service_sid", "") or ""
    if msvc:
        params["MessagingServiceSid"] = msvc
    elif from_number:
        params["From"] = from_number
    else:
        return False
    data = urllib.parse.urlencode(params).encode()
    auth = base64.b64encode(f"{sid}:{tok}".encode()).decode()
    req = urllib.request.Request(
        f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json", data=data, method="POST",
        headers={"Authorization": f"Basic {auth}", "Content-Type": "application/x-www-form-urlencoded"})
    urllib.request.urlopen(req, timeout=30)
    return True


def _send_followup_sms(pool: ConnectionPool, settings: Settings, call_id: str, to_number: str | None,
                       name: str | None, call_from: str | None = None) -> bool:
    """Fire the MINIMAL curiosity SMS on a missed call. This is a pure BACKEND process (Twilio direct) so
    it NEVER touches Lisa's voice latency — she only talks. The message is composed here (first name +
    callback ask, nothing that reveals a company or a sales reason). Prefers Twilio; falls back to the
    legacy Retell chat agent only if Twilio isn't configured. Gated on LISA_SMS_ENABLED."""
    if not to_number or not getattr(settings, "lisa_sms_enabled", False):
        return False
    first = (name or "").strip().split(" ")[0] if name else ""
    body = (f"Hi {first}, it's Lisa — tried to reach you, could you give me a quick call back when you get "
            "a sec? :)").replace("Hi ,", "Hi,")
    sms_from = _pick_sms_from(settings, call_from)
    sent = False
    try:
        if getattr(settings, "twilio_account_sid", "") and getattr(settings, "twilio_auth_token", ""):
            sent = _send_sms_twilio(settings, to_number, body, sms_from)          # backend Twilio (preferred)
        elif getattr(settings, "lisa_sms_agent_id", "") and sms_from:            # legacy Retell fallback
            _retell(settings, "POST", "create-sms-chat", {
                "from_number": sms_from, "to_number": _e164_au(to_number),
                "override_agent_id": getattr(settings, "lisa_sms_agent_id", ""),
                "retell_llm_dynamic_variables": {"prospect_name": first},
                "metadata": {"lisa_call_id": call_id}})
            sent = True
        else:
            log.info("lisa_sms_skipped_not_configured", call_id=call_id)
            return False
    except Exception as exc:
        log.warning("lisa_sms_failed", call_id=call_id, error=str(exc)[:200])
        return False
    if sent:
        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute("UPDATE lisa_calls SET sms_sent=true, updated_at=now() WHERE call_id=%s", (call_id,))
            conn.commit()
    return sent


# --------------------------------------------------------------------------- #
# Lisa as a BDE in the funnel — mirror each completed call into calls+classifications
# --------------------------------------------------------------------------- #
_OUTCOME_MAP = {
    # outcome -> (answered, reached_dm, call_outcome, is_voicemail)
    "booked": (True, True, "conversation", False),
    "callback_requested": (True, True, "conversation", False),
    "not_interested": (True, True, "conversation", False),
    "do_not_call": (True, True, "conversation", False),
    "gatekeeper_only": (True, False, "gatekeeper", False),
    "voicemail": (False, False, "voicemail", True),
    "no_answer": (False, False, "no_answer", False),
    "wrong_number": (False, False, "wrong_number", False),
}


def _write_funnel_call(pool: ConnectionPool, cid: str, dyn: dict, cad: dict, call: dict) -> None:
    """Mirror a completed Lisa call into `calls` + `classifications` (bde_name='Lisa', provider='retell')
    so the leaderboard / funnel / reports treat her like any BDE. Isolation to admins is via
    PRIVATE_BDE_NAMES. Idempotent per call_id."""
    outcome = (cad.get("call_outcome") or "").strip().lower()
    booked = bool(cad.get("meeting_agreed"))
    callback = outcome == "callback_requested" or bool(cad.get("callback_when"))
    answered, reached, co, is_vm = _OUTCOME_MAP.get(outcome, (False, False, "no_answer", False))
    if booked:
        answered, reached, co = True, True, "conversation"
    dur_ms = call.get("duration_ms") or call.get("call_length_ms") or 0
    talk = int((dur_ms or 0) / 1000)
    stage = "p5" if booked else ("p1" if callback else None)
    dest = call.get("to_number")
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO calls (call_id, bde_extension, bde_name, direction, dest_number, started_at, "
            "  talk_seconds, answered, is_voicemail, has_transcript, recording_present, in_scope, provider, "
            "  fresh_or_followup) VALUES (%s,'LISA','Lisa','outbound',%s,now(),%s,%s,%s,%s,%s,true,'retell','fresh') "
            "ON CONFLICT (call_id) DO UPDATE SET talk_seconds=EXCLUDED.talk_seconds, answered=EXCLUDED.answered, "
            "  is_voicemail=EXCLUDED.is_voicemail, has_transcript=EXCLUDED.has_transcript, "
            "  recording_present=EXCLUDED.recording_present",
            (cid, dest, talk, answered, is_vm, bool(call.get("transcript")), bool(call.get("recording_url"))))
        cur.execute(
            "INSERT INTO classifications (call_id, rpc_connect, full_pitch, is_lead, qualified, meeting_booked, "
            "  call_outcome, booking_status, meeting_datetime, callback_requested, callback_when, pipeline_stage, "
            "  prospect_company, prospect_website, prospect_contact_name, prospect_email, do_not_contact, "
            "  classified_at, model) "
            "VALUES (%s,%s,%s,%s,false,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,now(),'retell') "
            "ON CONFLICT (call_id) DO UPDATE SET rpc_connect=EXCLUDED.rpc_connect, full_pitch=EXCLUDED.full_pitch, "
            "  is_lead=EXCLUDED.is_lead, meeting_booked=EXCLUDED.meeting_booked, call_outcome=EXCLUDED.call_outcome, "
            "  booking_status=EXCLUDED.booking_status, callback_requested=EXCLUDED.callback_requested, "
            "  pipeline_stage=EXCLUDED.pipeline_stage, do_not_contact=EXCLUDED.do_not_contact",
            (cid, reached, reached, (booked or callback), booked, co, ("firm" if booked else None),
             cad.get("agreed_day_time"), callback, cad.get("callback_when"), stage,
             dyn.get("company_name"), dyn.get("prospect_website"), dyn.get("prospect_name"),
             cad.get("confirmed_email") or dyn.get("prospect_email"), outcome == "do_not_call"))
        conn.commit()


def schedule_lisa_followup(pool: ConnectionPool, settings: Settings, *, dest9: str, dest_number: str,
                           outcome: str, cad: dict, dyn: dict) -> None:
    """Lisa's own next-move on her calendar (self-contained; does not touch the human pilot allocators):
    a requested callback at the asked time, or a retry after a no-answer/voicemail up to the cap. Booked /
    not-interested / do-not-call get nothing."""
    from .calendar import create_event
    tz = settings.tz
    who = dyn.get("company_name") or dyn.get("prospect_name") or dest_number
    if outcome == "callback_requested" or cad.get("callback_when"):
        when = _parse_when(cad.get("callback_when")) or (datetime.now(ZoneInfo(tz)) + timedelta(days=1)).replace(
            hour=10, minute=0, second=0, microsecond=0)
        create_event(pool, bde_name="Lisa", type="callback", title=f"📞 Lisa callback: {who}",
                     start_at=when, end_at=when + timedelta(minutes=15),
                     notes=f"Prospect asked for a callback: {cad.get('callback_when') or ''}",
                     dest_number=dest_number, created_by="lisa")
        return
    if outcome in ("no_answer", "voicemail", "gatekeeper_only"):
        attempts = _fetch(pool, "SELECT count(*) n FROM calls WHERE provider='retell' AND "
                          "right(regexp_replace(COALESCE(dest_number,''),'[^0-9]','','g'),9)=%s", (dest9,))[0]["n"]
        if attempts >= int(getattr(settings, "lisa_retry_max_attempts", 4)):
            return
        cad_days = int(getattr(settings, "lisa_retry_cadence_days", 3))
        when = _future_biz(datetime.now(ZoneInfo(tz)) + timedelta(days=cad_days),
                           int(getattr(settings, "lisa_call_window_start", 9)))
        create_event(pool, bde_name="Lisa", type="retry", title=f"🔄 Lisa retry ({attempts+1}): {who}",
                     start_at=when, end_at=when + timedelta(minutes=15),
                     notes="No pickup yet — Lisa retries at a different time.", dest_number=dest_number,
                     created_by="lisa")


def _future_biz(dt: datetime, hour: int) -> datetime:
    d = dt.date()
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return datetime(d.year, d.month, d.day, min(max(hour, 8), 17), 0, tzinfo=dt.tzinfo)


# --------------------------------------------------------------------------- #
# Lisa's reserved pool + calendar + auto-dialer
# --------------------------------------------------------------------------- #
def reserve_lisa_pool(pool: ConnectionPool, settings: Settings) -> dict:
    """Reserve up to lisa_pool_size GAds-CONFIRMED prospects EXCLUSIVELY for Lisa (highest-advertising
    first), skipping anyone already called in-scope, already on a calendar, or DND. Idempotent — tops the
    pool up to size. The human fresh allocator excludes every dest9 in lisa_pool (no double-calling)."""
    ensure_tables(pool)
    from .prospects import gads_dnb_gate
    size = int(getattr(settings, "lisa_pool_size", 500))
    have = _fetch(pool, "SELECT count(*) c FROM lisa_pool")[0]["c"]
    if have >= size:
        return {"reserved": 0, "total": have, "note": "pool already at size"}
    need = size - have
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO lisa_pool (dest9, dest_number, domain, company, phone) "
            "SELECT z.d9, z.phone, z.domain, z.company, z.phone FROM ("
            # DISTINCT ON (phone) collapses to ONE row per physical number (highest ad-count) BEFORE the
            # limit, so two GAds domains that share a phone can't shrink the reserved count.
            "  SELECT DISTINCT ON (right(regexp_replace(COALESCE(co.phone,co.phone_norm),'[^0-9]','','g'),9)) "
            "         right(regexp_replace(COALESCE(co.phone,co.phone_norm),'[^0-9]','','g'),9) d9, "
            "         COALESCE(co.phone,co.phone_norm) phone, co.domain, co.company_name company, "
            "         COALESCE((ge.dataforseo->'ads'->>'count')::int,0) ads "
            "  FROM enrichment ge JOIN companies co ON co.domain=ge.domain "
            "  WHERE (ge.dataforseo->>'running_google_ads')='true' AND COALESCE(co.phone,co.phone_norm)<>'' "
            + gads_dnb_gate("ge") + " "
            "  ORDER BY right(regexp_replace(COALESCE(co.phone,co.phone_norm),'[^0-9]','','g'),9), "
            "           COALESCE((ge.dataforseo->'ads'->>'count')::int,0) DESC) z "
            "WHERE length(z.d9)=9 AND z.d9 NOT IN (SELECT dest9 FROM lisa_pool) "
            "  AND NOT EXISTS (SELECT 1 FROM calls c WHERE c.in_scope AND "
            "     right(regexp_replace(COALESCE(c.dest_number,''),'[^0-9]','','g'),9)=z.d9) "
            "  AND NOT EXISTS (SELECT 1 FROM calendar_events e WHERE e.status='pending' AND "
            "     right(regexp_replace(COALESCE(e.dest_number,''),'[^0-9]','','g'),9)=z.d9) "
            "  AND NOT EXISTS (SELECT 1 FROM prospect_pipeline pp WHERE pp.dest9=z.d9 AND COALESCE(pp.dnd,false)) "
            "ORDER BY z.ads DESC NULLS LAST LIMIT %s ON CONFLICT (dest9) DO NOTHING", (need,))
        got = cur.rowcount
        conn.commit()
    return {"reserved": got, "total": have + got}


def schedule_lisa_fresh(pool: ConnectionPool, settings: Settings) -> dict:
    """Put Lisa's reserved-pool prospects on HER calendar as fresh_call events, filling each working day up
    to lisa_daily_target, staggered across the call window. Idempotent; only schedules pool prospects not
    already on her calendar or already called by her."""
    ensure_tables(pool)
    tz = settings.tz
    cap = int(getattr(settings, "lisa_daily_target", 50))
    wstart = int(getattr(settings, "lisa_call_window_start", 9))
    wend = int(getattr(settings, "lisa_call_window_end", 17))
    rows = _fetch(pool,
        "SELECT lp.dest9, lp.dest_number, lp.company FROM lisa_pool lp "
        "WHERE NOT EXISTS (SELECT 1 FROM calendar_events e WHERE e.bde_name='Lisa' AND e.status IN ('pending','done') "
        "   AND right(regexp_replace(COALESCE(e.dest_number,''),'[^0-9]','','g'),9)=lp.dest9) "
        "  AND NOT EXISTS (SELECT 1 FROM calls c WHERE c.provider='retell' AND "
        "   right(regexp_replace(COALESCE(c.dest_number,''),'[^0-9]','','g'),9)=lp.dest9) "
        # Head-of-Sales value order: highest-spend advertisers (most live ad creatives) first.
        "ORDER BY lp.priority DESC NULLS LAST, lp.reserved_at")
    existing = {r["d"]: r["n"] for r in _fetch(pool,
        "SELECT (start_at AT TIME ZONE %s)::date d, count(*) n FROM calendar_events "
        "WHERE bde_name='Lisa' AND status='pending' AND type='fresh_call' GROUP BY 1", (tz,))}
    now = datetime.now(ZoneInfo(tz))
    day = now.date() + (timedelta(days=1) if now.hour >= wend else timedelta(0))
    span = max(1, wend - wstart)
    to_insert = []
    for r in rows:
        while day.weekday() >= 5 or existing.get(day, 0) >= cap:
            if day.weekday() >= 5:
                day += timedelta(days=1); continue
            day += timedelta(days=1)
        used = existing.get(day, 0)
        mins = int(used * (span * 60) / max(1, cap))
        when = datetime(day.year, day.month, day.day, min(wstart + mins // 60, wend - 1), mins % 60,
                        tzinfo=ZoneInfo(tz))
        existing[day] = used + 1
        to_insert.append(("Lisa", "fresh_call", f"🎙️ Lisa call: {r['company'] or r['dest_number']}",
                          when, when + timedelta(minutes=15), None, r["dest_number"]))
    scheduled = 0
    if to_insert:
        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute(_ENSURE_FRESH_INDEX)
            cur.executemany(
                "INSERT INTO calendar_events (bde_name, type, title, start_at, end_at, notes, dest_number, "
                "  created_by, status) VALUES (%s,%s,%s,%s,%s,%s,%s,'lisa','pending') "
                "ON CONFLICT (" + _FRESH_D9 + ") WHERE type='fresh_call' AND status='pending' DO NOTHING",
                to_insert)
            scheduled = cur.rowcount
            conn.commit()
    return {"scheduled": scheduled, "candidates": len(rows)}


_FRESH_D9 = "right(regexp_replace(COALESCE(dest_number,''),'[^0-9]','','g'),9)"
_ENSURE_FRESH_INDEX = (
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_calendar_fresh_call ON calendar_events (" + _FRESH_D9 +
    ") WHERE type='fresh_call' AND status='pending'")


def run_lisa_autodial(pool: ConnectionPool, settings: Settings) -> dict:
    """GATED daily dialer. When lisa_autodial_enabled, dial Lisa's DUE calendar events (fresh/retry/
    callback) within the business-hours window, up to the daily target, a few at a time. Marks each dialed
    event 'done'. No call fires unless lisa_autodial_enabled is true."""
    ensure_tables(pool)
    if not getattr(settings, "lisa_autodial_enabled", False):
        return {"skipped": "autodial disabled"}
    if not getattr(settings, "lisa_enabled", False):
        return {"skipped": "lisa disabled"}
    tz = settings.tz
    now = datetime.now(ZoneInfo(tz))
    wstart = int(getattr(settings, "lisa_call_window_start", 9))
    wend = int(getattr(settings, "lisa_call_window_end", 17))
    if now.weekday() >= 5 or not (wstart <= now.hour < wend):
        return {"skipped": "outside call window"}
    placed_today = _fetch(pool, "SELECT count(*) n FROM lisa_calls WHERE "
                          "(created_at AT TIME ZONE %s)::date = (now() AT TIME ZONE %s)::date", (tz, tz))[0]["n"]
    remaining = int(getattr(settings, "lisa_daily_target", 50)) - int(placed_today or 0)
    if remaining <= 0:
        return {"skipped": "daily target reached", "placed_today": placed_today}
    batch = min(remaining, int(getattr(settings, "lisa_max_concurrent", 3)))
    due = _fetch(pool,
        "SELECT id, dest_number, right(regexp_replace(COALESCE(dest_number,''),'[^0-9]','','g'),9) d9 "
        "FROM calendar_events WHERE bde_name='Lisa' AND status='pending' "
        "  AND type IN ('fresh_call','retry','callback','reached_call') AND start_at <= now() "
        "ORDER BY start_at LIMIT %s", (batch,))
    dialed = 0
    skipped_tz = 0
    for e in due:
        if not e.get("dest_number"):
            continue
        # STATE-AWARE: only dial within the prospect's OWN local business hours (WA ≠ NSW).
        lh, _tz = _prospect_local_hour(pool, e["d9"])
        if not (wstart <= lh < wend):
            skipped_tz += 1
            continue
        r = start_call(pool, settings, to_number=e["dest_number"], dest9=e["d9"])
        if r.get("call_id"):
            with pool.connection() as conn, conn.cursor() as cur:
                cur.execute("UPDATE calendar_events SET status='done' WHERE id=%s", (e["id"],))
                conn.commit()
            dialed += 1
    stats = {"dialed": dialed, "due": len(due), "skipped_tz": skipped_tz, "placed_today": placed_today, "remaining": remaining}
    log.info("lisa_autodial", **stats)
    return stats


# --------------------------------------------------------------------------- #
# AI Sales Coach / Trainer — learn from WON (do) + LOST (don't); QA each Lisa call
# --------------------------------------------------------------------------- #
def _llm_json(settings: Settings, system: str, user: str, model: str | None = None) -> dict:
    """One JSON-returning LLM call (reuses the classifier's OpenAI key/model). Returns {} on failure."""
    try:
        from openai import OpenAI
        client = OpenAI(api_key=getattr(settings, "llm_api_key", ""))
        m = model or getattr(settings, "llm_model_strong", None) or "gpt-4o"
        r = client.chat.completions.create(
            model=m, temperature=0.2, response_format={"type": "json_object"},
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user[:24000]}])
        return json.loads(r.choices[0].message.content or "{}")
    except Exception as exc:
        log.warning("lisa_llm_json_failed", error=str(exc)[:160])
        return {}


def refresh_playbook(pool: ConnectionPool, settings: Settings, *, force: bool = False) -> dict:
    """AI Sales Coach: mine the team's WON calls (a meeting was booked → what TO do) and LOST calls
    (reached the decision-maker but no booking → what NOT to do), and distil a current PLAYBOOK — the best
    objection rebuttals + an avoid-list — that build_brief injects into Lisa. This is how she trains
    automatically off REAL calls: the human team's winning + losing moments now, and her own once she has
    volume. Runs on a daily cadence (throttled ~20h); stored in lisa_playbook."""
    ensure_tables(pool)
    if not force:
        last = _fetch(pool, "SELECT built_at FROM lisa_playbook WHERE id=1")
        if last and last[0].get("built_at"):
            from datetime import datetime, timezone
            age_h = (datetime.now(timezone.utc) - last[0]["built_at"]).total_seconds() / 3600
            if age_h < 20:
                return {"skipped": f"playbook fresh ({round(age_h,1)}h old)"}
    won = _fetch(pool, "SELECT left(tr.text,2600) t FROM calls c JOIN classifications cl ON cl.call_id=c.call_id "
        "JOIN transcripts tr ON tr.call_id=c.call_id WHERE c.in_scope AND cl.rpc_connect AND cl.meeting_booked "
        "AND NOT COALESCE(cl.meeting_confirmation,false) AND c.talk_seconds>=120 AND length(COALESCE(tr.text,''))>600 "
        "ORDER BY c.started_at DESC LIMIT 8")
    lost = _fetch(pool, "SELECT left(tr.text,2600) t FROM calls c JOIN classifications cl ON cl.call_id=c.call_id "
        "JOIN transcripts tr ON tr.call_id=c.call_id WHERE c.in_scope AND cl.rpc_connect AND NOT cl.meeting_booked "
        "AND NOT COALESCE(cl.callback_requested,false) AND COALESCE(cl.call_outcome,'')='conversation' "
        "AND c.talk_seconds>=90 AND length(COALESCE(tr.text,''))>600 ORDER BY c.started_at DESC LIMIT 8")
    if not won and not lost:
        return {"skipped": "no calls to learn from"}
    sys = ("You are the sales coach for an Australian digital-marketing agency's appointment-setting AI "
           "(named Lisa; she books a free strategy session). From WON transcripts (a meeting WAS booked) and "
           "LOST transcripts (reached the decision-maker but NO booking), distil a concise CURRENT playbook. "
           "Return STRICT JSON: objection_agency, objection_price, objection_email, objection_not_interested, "
           "objection_no_time (each = the single best one-sentence rebuttal in warm AU spoken style, taken from "
           "what actually worked in the WON calls); do = array of up to 6 short 'what to DO' rules that "
           "correlate with booking; avoid = array of up to 6 short 'what NOT to do' rules that lost the LOST "
           "calls. Every string ONE sentence, spoken, concrete, no fluff.")
    usr = ("=== WON CALLS (meeting booked) ===\n" + "\n---\n".join(w["t"] for w in won) +
           "\n\n=== LOST CALLS (reached DM, no booking) ===\n" + "\n---\n".join(l["t"] for l in lost))
    pb = _llm_json(settings, sys, usr)
    if not pb:
        return {"skipped": "llm returned nothing"}
    pb["_won"], pb["_lost"] = len(won), len(lost)
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute("INSERT INTO lisa_playbook (id, playbook, built_at) VALUES (1,%s,now()) "
                    "ON CONFLICT (id) DO UPDATE SET playbook=EXCLUDED.playbook, built_at=now()", (json.dumps(pb),))
        conn.commit()
    return {"learned_from_won": len(won), "learned_from_lost": len(lost),
            "objection_lines": len([k for k in pb if k.startswith("objection_")]),
            "do": len(pb.get("do") or []), "avoid": len(pb.get("avoid") or [])}


def get_playbook(pool: ConnectionPool) -> dict:
    r = _fetch(pool, "SELECT playbook FROM lisa_playbook WHERE id=1")
    return (r[0].get("playbook") if r else None) or {}


def review_lisa_call(pool: ConnectionPool, settings: Settings, call_id: str, transcript: str) -> dict:
    """AI QA reviewer: score one Lisa call for quality + brand + compliance; store for the scorecard."""
    sys = ("Score this AI cold-caller (Lisa, an AU digital-marketing appointment setter) from her call "
           "transcript. Return STRICT JSON: opening (1-5), discovery (1-5), objection_handling (1-5), "
           "booking (1-5), compliance (1-5 — did she say 'Digital Expo' only, NOT volunteer she's an AI "
           "unless directly asked, sound human), overall (1-5), best_line (string), improve (one-sentence "
           "coaching tip), flags (array of any compliance/quality issues, empty if none).")
    r = _llm_json(settings, sys, transcript or "")
    if r:
        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute("INSERT INTO lisa_call_reviews (call_id, scores, reviewed_at) VALUES (%s,%s,now()) "
                        "ON CONFLICT (call_id) DO UPDATE SET scores=EXCLUDED.scores, reviewed_at=now()",
                        (call_id, json.dumps(r)))
            conn.commit()
    return r


def review_pending_lisa_calls(pool: ConnectionPool, settings: Settings, limit: int = 8) -> dict:
    """QA the most recent analysed Lisa calls that don't have a review yet (a small batch per cycle)."""
    ensure_tables(pool)
    rows = _fetch(pool, "SELECT call_id, transcript FROM lisa_calls lc WHERE status='analyzed' "
        "AND length(COALESCE(transcript,''))>200 AND NOT EXISTS "
        "(SELECT 1 FROM lisa_call_reviews r WHERE r.call_id=lc.call_id) ORDER BY created_at DESC LIMIT %s", (limit,))
    n = sum(1 for r in rows if review_lisa_call(pool, settings, r["call_id"], r["transcript"]))
    return {"reviewed": n}


# --------------------------------------------------------------------------- #
# Head of Sales · Strategist — the AI orchestrator (runs every cycle, automatically)
# --------------------------------------------------------------------------- #
def _biz_now(settings: Settings) -> tuple[bool, datetime]:
    """(in AU business hours?, now) — Mon–Fri within the call window. The strategist uses this to switch
    between LIVE mode (dial/pace) and PREP mode (brief/coach/QA for tomorrow), so the team's rhythm follows
    the working day on its own."""
    now = datetime.now(ZoneInfo(_TZ))
    ws = int(getattr(settings, "lisa_call_window_start", 9))
    we = int(getattr(settings, "lisa_call_window_end", 17))
    return (now.weekday() < 5 and ws <= now.hour < we), now


def run_head_of_sales(pool: ConnectionPool, settings: Settings) -> dict:
    """Head of Sales · Strategist (AI) — the ORCHESTRATOR, run automatically every refresh cycle (24/7).
    It doesn't wait for instructions: each pass it (1) value-ranks Lisa's reserved pool so the highest-spend
    advertisers are called first, (2) DIRECTS the other AI staff — keeps the pool topped up, has the Coach
    re-learn the playbook, the QA reviewer score new calls, the Researcher fill any missing briefs, and the
    calendar filled in priority order, (3) guards brand/compliance, and (4) persists the decision + exactly
    what it did (lisa_strategy) so the console shows real work. Safe + idempotent; never raises."""
    ensure_tables(pool)
    in_hours, now = _biz_now(settings)
    actions = {"prioritized": 0, "reserved": 0, "coach": "skip", "qa_reviewed": 0, "briefs_built": 0, "scheduled": 0}
    alerts: list[str] = []

    # 1) value-rank the pool — live ad creatives = spend proxy (all pool prospects are GAds-confirmed)
    try:
        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute("UPDATE lisa_pool lp SET priority=COALESCE((e.dataforseo->'ads'->>'count')::int,0) "
                        "FROM enrichment e WHERE e.domain=lp.domain")
            actions["prioritized"] = cur.rowcount
            conn.commit()
    except Exception as exc:
        log.warning("hos_priority_failed", error=str(exc)[:140])

    # 2–3) direct the team (each idempotent; refresh_playbook self-throttles ~20h, briefs only fill gaps)
    try:
        actions["reserved"] = reserve_lisa_pool(pool, settings).get("reserved", 0)
    except Exception as exc:
        log.warning("hos_reserve_failed", error=str(exc)[:140])
    if getattr(settings, "lisa_coaching_enabled", True):
        try:
            pb = refresh_playbook(pool, settings)
            actions["coach"] = "skipped" if pb.get("skipped") else f"learned {pb.get('learned_from_won',0)}w/{pb.get('learned_from_lost',0)}l"
        except Exception as exc:
            log.warning("hos_coach_failed", error=str(exc)[:140])
        try:
            actions["qa_reviewed"] = review_pending_lisa_calls(pool, settings).get("reviewed", 0)
        except Exception as exc:
            log.warning("hos_qa_failed", error=str(exc)[:140])
    try:
        actions["briefs_built"] = refresh_lisa_briefs(pool, settings, limit=600).get("built", 0)
    except Exception as exc:
        log.warning("hos_briefs_failed", error=str(exc)[:140])
    try:
        actions["scheduled"] = schedule_lisa_fresh(pool, settings).get("scheduled", 0)
    except Exception as exc:
        log.warning("hos_schedule_failed", error=str(exc)[:140])

    # 4) read the funnel + guardrails
    snap = _fetch(pool,
        "SELECT count(*) calls, count(*) FILTER (WHERE meeting_agreed) booked, "
        "  count(*) FILTER (WHERE asked_if_ai) asked_ai "
        "FROM lisa_calls WHERE created_at >= now() - interval '30 days'")[0]
    calls = snap["calls"] or 0
    booked = snap["booked"] or 0
    book_rate = round(100 * booked / calls, 1) if calls else 0.0
    due = _fetch(pool, "SELECT count(*) c FROM calendar_events WHERE bde_name='Lisa' AND status='pending' "
        "AND type IN ('fresh_call','retry','callback','reached_call') AND start_at<=now()")[0]["c"]
    due_cb = _fetch(pool, "SELECT count(*) c FROM calendar_events WHERE bde_name='Lisa' AND status='pending' "
        "AND type IN ('callback','retry') AND start_at<=now()")[0]["c"]
    comp = _fetch(pool, "SELECT avg(NULLIF(scores->>'compliance','')::float) "
        "FILTER (WHERE scores->>'compliance' ~ '^[0-9.]+$') c FROM lisa_call_reviews")[0]["c"]
    pool_left = _fetch(pool, "SELECT count(*) c FROM lisa_pool")[0]["c"]
    if comp is not None and comp < 4:
        alerts.append(f"Compliance {round(comp,1)}/5 — tighten brand / AI-disclosure")
    if calls and (snap["asked_ai"] or 0) / calls > 0.25:
        alerts.append("Prospects often ask 'are you AI?' — soften the opener")
    if pool_left < 50:
        alerts.append(f"Pool low ({pool_left}) — reserving more GAds prospects")

    # 5) decide mode + directive (follows the working day on its own)
    tgt = int(getattr(settings, "lisa_daily_target", 50))
    autodial = bool(getattr(settings, "lisa_autodial_enabled", False))
    if not autodial:
        policy = "staged"
        directive = f"Pool primed & value-ranked ({pool_left} ready) — awaiting go-live, pacing {tgt}/day"
    elif not in_hours:
        policy = "prep"
        directive = f"Off-hours — prepping briefs & coaching for tomorrow · {book_rate}% booking"
    elif due_cb:
        policy = "callbacks_first"
        directive = f"Live — clearing {due_cb} due callbacks first, then top-spend fresh"
    elif calls and book_rate < 12:
        policy = "tune_opener"
        directive = f"Live — book rate {book_rate}%; Coach sharpening the opener, top-spenders first"
    else:
        policy = "value_rank"
        directive = f"Live — pacing {tgt}/day, highest-spend advertisers first · {book_rate}% booking"

    strategy = {"directive": directive, "policy": policy, "in_hours": in_hours, "book_rate": book_rate,
                "booked": booked, "calls": calls, "due": due, "due_callbacks": due_cb, "pool": pool_left,
                "compliance": (round(comp, 1) if comp is not None else None),
                "actions": actions, "alerts": alerts}
    try:
        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute("INSERT INTO lisa_strategy (id, strategy, updated_at) VALUES (1,%s,now()) "
                        "ON CONFLICT (id) DO UPDATE SET strategy=EXCLUDED.strategy, updated_at=now()",
                        (json.dumps(strategy),))
            conn.commit()
    except Exception as exc:
        log.warning("hos_persist_failed", error=str(exc)[:140])
    log.info("lisa_head_of_sales", policy=policy, in_hours=in_hours, **actions)
    return strategy


def get_strategy(pool: ConnectionPool) -> dict:
    r = _fetch(pool, "SELECT strategy, (updated_at AT TIME ZONE 'Australia/Melbourne') u FROM lisa_strategy WHERE id=1")
    if not r:
        return {}
    s = (r[0].get("strategy") or {})
    s["updated_local"] = str(r[0].get("u") or "").replace("T", " ")[:16]
    return s


# --------------------------------------------------------------------------- #
# reporting (admin-only console + funnel)
# --------------------------------------------------------------------------- #
def summary(pool: ConnectionPool, days: int = 30) -> dict:
    ensure_tables(pool)
    r = _fetch(pool,
        "SELECT count(*) calls, "
        "  count(*) FILTER (WHERE status='analyzed') completed, "
        "  count(*) FILTER (WHERE meeting_agreed) booked, "
        "  count(*) FILTER (WHERE call_outcome='callback_requested') callbacks, "
        "  count(*) FILTER (WHERE call_outcome IN ('no_answer','voicemail')) missed, "
        "  count(*) FILTER (WHERE sms_sent) sms, "
        "  count(*) FILTER (WHERE asked_if_ai) asked_ai, "
        "  COALESCE(sum(cost_cents),0) cost_cents, "
        "  COALESCE(sum(duration_ms),0) duration_ms "
        "FROM lisa_calls WHERE created_at >= now() - make_interval(days => %s)", (days,))
    s = dict(r[0]) if r else {}
    calls = s.get("calls") or 0
    s["book_rate"] = round(100 * (s.get("booked") or 0) / calls, 1) if calls else 0.0
    # campaign / reserved-pool status for the console
    tz = "Australia/Melbourne"
    s["pool_reserved"] = _fetch(pool, "SELECT count(*) c FROM lisa_pool")[0]["c"]
    s["briefs_built"] = _fetch(pool, "SELECT count(*) c FROM lisa_briefs")[0]["c"]
    s["pool_scheduled"] = _fetch(pool,
        "SELECT count(*) c FROM calendar_events WHERE bde_name='Lisa' AND status='pending' AND type='fresh_call'")[0]["c"]
    s["pool_dialed"] = _fetch(pool,
        "SELECT count(DISTINCT dest9) c FROM lisa_calls WHERE dest9 IS NOT NULL")[0]["c"]
    s["dialed_today"] = _fetch(pool,
        "SELECT count(*) c FROM lisa_calls WHERE (created_at AT TIME ZONE %s)::date=(now() AT TIME ZONE %s)::date",
        (tz, tz))[0]["c"]
    s["due_now"] = _fetch(pool,
        "SELECT count(*) c FROM calendar_events WHERE bde_name='Lisa' AND status='pending' "
        "AND type IN ('fresh_call','retry','callback','reached_call') AND start_at <= now()")[0]["c"]
    # --- AI Sales Coach: learned playbook + QA scorecard ---
    s["playbook"] = get_playbook(pool)
    pbb = _fetch(pool, "SELECT built_at FROM lisa_playbook WHERE id=1")
    s["playbook_built_at"] = (pbb[0]["built_at"] if pbb else None)
    qa = _fetch(pool,
        "SELECT count(*) n, "
        "  avg(NULLIF(scores->>'overall','')::float) FILTER (WHERE scores->>'overall' ~ '^[0-9.]+$') overall, "
        "  avg(NULLIF(scores->>'compliance','')::float) FILTER (WHERE scores->>'compliance' ~ '^[0-9.]+$') compliance "
        "FROM lisa_call_reviews")
    s["qa"] = dict(qa[0]) if qa else {}
    # --- Head of Sales · Strategist: the persisted orchestration decision + what it last did (real work) ---
    s["strategy"] = get_strategy(pool)
    now_mel = datetime.now(ZoneInfo(tz))
    s["in_hours"] = bool(now_mel.weekday() < 5 and 9 <= now_mel.hour < 17)
    return s


def recent_calls(pool: ConnectionPool, limit: int = 100) -> list[dict]:
    ensure_tables(pool)
    return _fetch(pool,
        "SELECT call_id, dest9, prospect_name, company_name, domain, status, call_outcome, "
        "  meeting_agreed, agreed_day_time, callback_when, main_objection, asked_if_ai, call_summary, "
        "  recording_url, duration_ms, cost_cents, booked_event_id, sms_sent, qualification, "
        "  (created_at AT TIME ZONE 'Australia/Melbourne') AS created_local "
        "FROM lisa_calls ORDER BY created_at DESC LIMIT %s", (limit,))
