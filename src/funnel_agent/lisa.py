"""Lisa-1 — the AI cold-caller subsystem (talk-only architecture).

Design (owner-confirmed): Lisa is a MOUTH, not a brain or hands. Everything she might need to say is
pre-computed by the Brief-builder and injected as Retell dynamic variables BEFORE the call; everything she
"does" (booking, SMS, recording the outcome) happens HERE, server-side, AFTER the call — so she can never
hallucinate a fact she was handed or fumble a tool she doesn't have.

This subsystem is deliberately ISOLATED from the 3CX/Aircall funnel:
  • its own table `lisa_calls` (never mixed into `calls`), and
  • admin-only endpoints (Raj/Vysakh),
so Lisa's numbers never leak into team totals, the TV kiosk, or any BDE/BDM view.

Brand safety: to a prospect the brand is ALWAYS "DE Group"; "Traffic Radius" is never spoken.
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
_TABLES_READY = False


def ensure_tables(pool: ConnectionPool, force: bool = False) -> None:
    """Create/patch Lisa's tables. Runs the DDL ONCE per process then no-ops. CRITICAL: each
    `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` grabs a brief AccessExclusiveLock on the table even when the
    column already exists — running that on every autodial / orchestration / dashboard-API call DEADLOCKS
    against the dashboard's constant concurrent reads (stalls the dial loop). force=True re-runs it."""
    global _TABLES_READY
    if _TABLES_READY and not force:
        return
    if not force:
        # Cheap READ-ONLY schema probe: if the newest column already exists, the schema is current -> skip
        # ALL CREATE/ALTER. `ADD COLUMN IF NOT EXISTS` takes an AccessExclusiveLock even as a no-op, which
        # deadlocks the per-cycle refresh subprocess against the dashboard's constant reads. This probe uses
        # only a shared lock. NOTE: when adding a NEW column below, bump the sentinel column here too.
        try:
            with pool.connection() as conn, conn.cursor() as cur:
                cur.execute("SELECT 1 FROM information_schema.columns "
                            "WHERE table_name='lisa_control' AND column_name='hos_heavy_at'")
                current = cur.fetchone() is not None
            if current:
                _TABLES_READY = True
                return
        except Exception:
            pass
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
        # per-FUNNEL-STAGE dedicated coach (opener/gatekeeper/pitch/objection/close) — each learns + trains
        # for its own stage. One row per stage.
        cur.execute("CREATE TABLE IF NOT EXISTS lisa_stage_coach ("
                    "  stage text PRIMARY KEY, title text, guidance jsonb, benchmark numeric,"
                    "  built_at timestamptz DEFAULT now())")
        # LLM (OpenAI) token usage + cost per Lisa AI-staff task — so the cost page is accurate.
        cur.execute("CREATE TABLE IF NOT EXISTS lisa_llm_usage ("
                    "  id bigserial PRIMARY KEY, purpose text, model text, prompt_tokens integer,"
                    "  completion_tokens integer, cost_cents numeric, created_at timestamptz DEFAULT now())")
        # AUTONOMOUS self-improvement: the AI Sales Coach's own fixes, applied to the live agent (audit trail
        # for what changed + why + rollback). The Coach writes these itself from recurring QA flags.
        cur.execute("CREATE TABLE IF NOT EXISTS lisa_auto_adjustments ("
                    "  id bigserial PRIMARY KEY, adjustments jsonb, from_flags jsonb, applied boolean DEFAULT true,"
                    "  created_at timestamptz DEFAULT now())")
        # Public, tokenised SHARE of a prospect's audit report — the unguessable token maps to a domain so a
        # prospect can open their audit from an SMS link WITHOUT a login. Reuses the existing audit engine.
        cur.execute("CREATE TABLE IF NOT EXISTS lisa_audit_shares ("
                    "  token text PRIMARY KEY, domain text NOT NULL, name text, ticket integer DEFAULT 2000,"
                    "  dest9 text, views integer DEFAULT 0, created_at timestamptz DEFAULT now())")
        # Head of Sales · Strategist output — the current orchestration decision + what it actually did each
        # cycle (policy, directive, actions taken, compliance alerts). One row (id=1). Surfaced in the console
        # so the role shows real work, not a label.
        cur.execute("CREATE TABLE IF NOT EXISTS lisa_strategy ("
                    "  id integer PRIMARY KEY, strategy jsonb, updated_at timestamptz DEFAULT now())")
        # value-rank of the reserved pool (live ad creatives = spend proxy) so the highest-spending
        # advertisers are called first; set by the Head of Sales each cycle, consumed by schedule_lisa_fresh.
        cur.execute("ALTER TABLE lisa_pool ADD COLUMN IF NOT EXISTS priority integer DEFAULT 0")
        # runtime control switch (single row) — auto-dial ON/OFF flipped live from the console (no redeploy).
        cur.execute("CREATE TABLE IF NOT EXISTS lisa_control ("
                    "  id integer PRIMARY KEY, autodial boolean, updated_at timestamptz DEFAULT now(),"
                    "  updated_by text)")
        # last time the HEAVY orchestration (pool-reserve, coach/QA, DM-enrichment, scheduling) ran — lets
        # the dialer fire every cycle while the slow prep self-throttles to ~every few minutes (fast cadence).
        cur.execute("ALTER TABLE lisa_control ADD COLUMN IF NOT EXISTS hos_heavy_at timestamptz")
        # two-way SMS log — every outbound curiosity SMS + every inbound reply (so prospect replies are
        # captured, threaded, and Lisa can reply). Was previously lost (Twilio had them, we didn't).
        cur.execute("CREATE TABLE IF NOT EXISTS lisa_sms ("
                    "  id bigserial PRIMARY KEY, dest9 text, direction text, from_number text, to_number text,"
                    "  body text, handled boolean DEFAULT false, created_at timestamptz DEFAULT now())")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_lisa_sms_dest9 ON lisa_sms(dest9)")
        # resolved TRUE decision-maker per company (cross-checked from call-intel + BD data + Apollo), with
        # the best number (mobile preferred). Lisa dials THIS so the dialer reaches the right person.
        cur.execute("CREATE TABLE IF NOT EXISTS lisa_dm ("
                    "  domain text PRIMARY KEY, dm_name text, dm_first text, dm_title text, dm_phone text,"
                    "  dm_is_mobile boolean, source text, confidence integer, candidates jsonb,"
                    "  dm_email text, trading_name text,"
                    "  resolved_at timestamptz DEFAULT now())")
        cur.execute("ALTER TABLE lisa_dm ADD COLUMN IF NOT EXISTS dm_email text")
        cur.execute("ALTER TABLE lisa_dm ADD COLUMN IF NOT EXISTS trading_name text")
        conn.commit()
    _TABLES_READY = True   # DDL done — future calls skip the deadlock-prone ALTERs (see docstring)
    ensure_lisa_agent(pool)


def get_autodial_state(pool: ConnectionPool, settings: Settings) -> bool:
    """Effective auto-dial state. The console toggle (DB, lisa_control) is authoritative once set so it can
    be flipped live with no redeploy; before it's ever set it falls back to LISA_AUTODIAL_ENABLED (env)."""
    try:
        r = _fetch(pool, "SELECT autodial FROM lisa_control WHERE id=1")
        if r and r[0].get("autodial") is not None:
            return bool(r[0]["autodial"])
    except Exception as exc:
        log.warning("lisa_autodial_state_read_failed", error=str(exc)[:140])
    return bool(getattr(settings, "lisa_autodial_enabled", False))


def set_autodial_state(pool: ConnectionPool, on: bool, by: str = "") -> bool:
    """Flip auto-dial ON/OFF from the console (persisted; takes effect on the very next 60s cycle)."""
    ensure_tables(pool)
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute("INSERT INTO lisa_control (id, autodial, updated_at, updated_by) VALUES (1,%s,now(),%s) "
                    "ON CONFLICT (id) DO UPDATE SET autodial=EXCLUDED.autodial, updated_at=now(), updated_by=EXCLUDED.updated_by",
                    (bool(on), by or ""))
        conn.commit()
    log.info("lisa_autodial_toggled", on=bool(on), by=by)
    return bool(on)


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


def _norm_domain(s: str | None) -> str:
    """A website/URL → bare registrable-ish domain ('https://www.Feathers.com.au/x' → 'feathers.com.au')."""
    d = re.sub(r"^https?://", "", (s or "").strip().lower())
    d = re.sub(r"^www\.", "", d)
    return d.split("/")[0].strip()


# Phrases that mean the prospect asked for something IN WRITING (report / proposal / "just email me").
# We don't email — so this is what triggers the audit view-link SMS instead.
_REPORT_WORDS = ("send me an email", "send an email", "email me", "email it", "email that", "email through",
                 "send it through", "send that through", "send me the", "send me some", "send me details",
                 "send details", "send info", "send me info", "send over", "in writing", "proposal",
                 "send a report", "send me a report", "send the report", "put it in an email",
                 "shoot me an email", "flick me an email", "flick it through", "send me your details",
                 "asked to email", "requested ... email", "email the details", "email your details",
                 # softer / brush-off phrasings that still mean "give it to me in writing"
                 "by email", "contact by email", "prefer email", "prefers email", "prefer to be emailed",
                 "email instead", "email is best", "email is fine", "reach me by email", "just email",
                 "over email", "via email", "through email", "emailed", "contact via email",
                 "prefer contact by email", "get my attention")


def _wants_report(cad: dict) -> bool:
    """True if the prospect asked for a report / proposal / details in writing. Primary signal is the Retell
    analysis flag `requested_report`; falls back to scanning the objection + summary text."""
    if not isinstance(cad, dict):
        return False
    flag = cad.get("requested_report")
    if flag in (True, "true", "yes", "1"):
        return True
    blob = " ".join(str(cad.get(k) or "") for k in ("main_objection", "callback_when", "call_summary")).lower()
    return any(w in blob for w in _REPORT_WORDS)


# "text me" phrasings — the prospect asked for the link BY SMS (vs email). Checked on the DNC path so a
# "text it to me, then don't call again" still gets the link BEFORE the do-not-call suppression lands.
_SMS_WORDS = ("text me", "text it", "text that", "text the link", "text through", "send a text",
              "send me a text", "sms me", "by sms", "via sms", "by text", "via text",
              "shoot me a text", "flick me a text", "message me")


def _wants_sms_link(cad: dict) -> bool:
    """True if the prospect asked to be TEXTED the link/details on this call — the classifier's SMS flag
    first, else a 'text me' phrasing in the analysis text."""
    if not isinstance(cad, dict):
        return False
    if any(cad.get(k) in (True, "true", "yes", "1") for k in ("sms_requested", "requested_sms", "sms_request")):
        return True
    blob = " ".join(str(cad.get(k) or "") for k in ("main_objection", "callback_when", "call_summary")).lower()
    return any(w in blob for w in _SMS_WORDS)


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


def _first_name(name: str | None) -> str:
    """First name only, stripped of titles — so Lisa says 'Hi Clint', never 'Hi Clint Robinson' (robotic)
    or a wrong/mixed full name. Empty string if we have no usable name (Lisa then greets without one)."""
    n = re.sub(r"\b(mr|mrs|ms|dr|miss|mx)\b\.?", "", (name or "").strip(), flags=re.I).strip()
    parts = [p for p in re.split(r"[ ,]+", n) if p and p.isalpha()]
    first = parts[0][:40].capitalize() if parts else ""
    # an article/role leaked in as a "name" ('The' from 'The Owner') would make her say "Hi The" — never that
    return "" if first.lower() in ("the", "a", "owner", "team", "unknown") else first


def _clean_company(name: str | None) -> str:
    """Spoken-friendly company name: drop trailing legal suffixes and fix SHOUTING-CAPS so Lisa says
    'Smith Plumbing', never 'SMITH PLUMBING PTY LTD' (an instant robocall tell)."""
    n = (name or "").strip()
    toks = n.split()
    while toks and toks[-1].strip(".,()").lower() in ("pty", "ltd", "limited", "p/l"):
        toks.pop()
    n = " ".join(toks).strip(" ,.")
    if n and n == n.upper() and any(c.isalpha() for c in n):
        n = n.title()
    return n


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
    # findings kept SHORT (~15 words) so Lisa's turn is punchy, not a scripted monologue
    if running_ads and (gap or comps):
        f1 = ("I noticed you're paying for Google Ads — but a chunk of the free search traffic next to them "
              "seems to be going to competitors")
        channel = "the free side alongside your ads"
        if top.get("domain"):
            proof = f"{top.get('domain')} looks like one of the ones pulling it"
    elif comps and (gap or top):
        f1 = "a couple of competitors are showing up for searches your customers type in, and you're not"
        channel = "search / SEO"
        if top.get("domain"):
            proof = f"{top.get('domain')} is a main one"
    elif quickwin:
        f1 = "you've got pages sitting just off page one of Google — usually the fastest win"
        channel = "SEO quick-wins"
    else:
        f1 = "a lot of ready-to-buy demand comes through Google search, and some of it slips to competitors"
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
    """Best-known domain for a prospect (reserved pool, a saved brief, or the master companies DB by phone)."""
    for tbl in ("lisa_pool", "lisa_briefs"):
        r = _fetch(pool, f"SELECT domain FROM {tbl} WHERE dest9=%s AND NULLIF(domain,'') IS NOT NULL LIMIT 1", (d9,))
        if r:
            return r[0]["domain"]
    # fallback: match the prospect's phone to a company in the master companies DB — fixes fresh-queue
    # prospects seeded straight onto the calendar (not in lisa_pool/lisa_briefs), which otherwise get an
    # empty brief (no company name, runs_google_ads=false).
    r = _fetch(pool,
        "SELECT domain FROM companies WHERE right(regexp_replace(COALESCE(phone,''),'[^0-9]','','g'),9)=%s "
        "AND NULLIF(domain,'') IS NOT NULL ORDER BY COALESCE(revenue_musd,0) DESC LIMIT 1", (d9,))
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


# D&B sector labels are catalogue strings ("Specialty Contractors", "Management of Companies &
# Enterprises") — spoken aloud they're an instant robot tell. Map each to a plain spoken phrase;
# labels deliberately absent (Management of Companies & Enterprises, Religious/Membership
# Organizations, Private Households, Government) blank out — better nameless than robotic.
_DNB_SECTOR_SPOKEN = {
    "professional services sector": "professional services",
    "finance & insurance sector": "finance and insurance",
    "specialty contractors": "trades and contracting",
    "retail sector": "retail",
    "restaurants, bars & food services": "hospitality",
    "consumer services": "consumer services",
    "business services sector": "business services",
    "health care sector": "healthcare",
    "manufacturing sector": "manufacturing",
    "arts, entertainment & recreation sector": "arts and entertainment",
    "wholesale sector": "wholesale",
    "real estate": "real estate",
    "transportation services sector": "transport",
    "education sector": "education",
    "residential construction contractors": "residential building",
    "lodging": "accommodation",
    "agriculture & forestry sector": "agriculture and farming",
    "nonresidential building construction": "commercial construction",
    "media": "media",
    "heavy & civil engineering construction": "civil construction",
    "rental & leasing": "equipment rental and hire",
    "nonprofit institutions": "not-for-profit",
    "mining": "mining",
    "water & sewer utilities": "utilities",
    "electric utilities": "utilities",
    "oil & gas field services": "oil and gas services",
    "natural gas distribution & marketing": "gas distribution",
    "oil & gas well drilling": "oil and gas",
    "oil & gas exploration & production": "oil and gas",
}


def _humanize_sector(label: str | None) -> str:
    """A speakable industry phrase from a D&B sector label; '' when unmappable. Already-plain labels
    (gmaps 'physiotherapist', BDE-classified 'plumbing') pass through untouched."""
    s = (label or "").strip()
    if not s:
        return ""
    key = s.lower()
    if key in _DNB_SECTOR_SPOKEN:
        return _DNB_SECTOR_SPOKEN[key]
    if " sector" in key or "&" in s or (s != s.lower() and len(s.split()) > 1):
        return ""            # unmapped catalogue-style label → blank (brief falls back to "your industry")
    return s


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


# --------------------------------------------------------------------------- #
# Decision-maker resolver — the TRUE top DM + best number (mobile preferred)
# --------------------------------------------------------------------------- #
_DM_SENIORITY = [("owner", 100), ("founder", 100), ("co-founder", 98), ("proprietor", 96), ("principal", 95),
                 ("ceo", 94), ("chief executive", 94), ("managing director", 93), ("managing dir", 93),
                 ("partner", 86), ("director", 85), ("chief marketing", 84), ("cmo", 84), ("general manager", 82),
                 ("head of marketing", 78), ("marketing director", 78), ("marketing manager", 68), ("manager", 52)]


def _dm_rank(title: str | None) -> int:
    t = (title or "").lower()
    return max([sc for kw, sc in _DM_SENIORITY if kw in t], default=0)


def _is_mobile_au(num: str | None) -> bool:
    d9 = re.sub(r"[^0-9]", "", num or "")[-9:]
    return len(d9) == 9 and d9[0] == "4"          # AU mobiles are 04xx… → last 9 digits start with 4


def resolve_decision_maker(pool: ConnectionPool, settings: Settings, domain: str) -> dict:
    """Cross-check who the TRUE top decision-maker is for a company from three sources — (1) our own call
    intelligence (who our BDEs actually reached, + the number that worked), (2) uploaded BD / D&B contacts,
    (3) Apollo people by title — pick the most senior, choose the best number (a MOBILE first, then a number
    we've actually reached), and save it. Lisa then dials THIS. Never raises."""
    if not domain:
        return {}
    cands = []
    ci = _fetch(pool,
        "SELECT cl.prospect_contact_name nm, c.dest_number num, bool_or(cl.rpc_connect) rpc "
        "FROM calls c JOIN classifications cl ON cl.call_id=c.call_id "
        "WHERE c.in_scope AND split_part(regexp_replace(lower(coalesce(cl.prospect_website,'')),"
        "  '^https?://(www\\.)?',''),'/',1)=%s AND coalesce(cl.prospect_contact_name,'')<>'' "
        "GROUP BY 1,2 ORDER BY rpc DESC NULLS LAST LIMIT 6", (domain,))
    for r in ci:
        cands.append({"name": r["nm"], "title": "", "phone": r["num"], "source": "call_intel", "reached": bool(r["rpc"])})
    co = _fetch(pool, "SELECT contacts, phone FROM companies WHERE domain=%s AND NULLIF(phone,'') IS NOT NULL LIMIT 1", (domain,))
    cophone = (co[0].get("phone") if co else None)
    if co and isinstance(co[0].get("contacts"), list):
        for p in co[0]["contacts"][:8]:
            if isinstance(p, dict):
                cands.append({"name": p.get("name") or p.get("full_name") or "", "title": p.get("title") or p.get("role") or "",
                              "phone": p.get("mobile") or p.get("phone") or "", "source": "bd", "reached": False})
    ap = _fetch(pool, "SELECT apollo FROM enrichment WHERE domain=%s", (domain,))
    for p in (((ap[0].get("apollo") if ap else None) or {}).get("people") or [])[:10]:
        nm = p.get("name") or ((p.get("first_name") or "") + " " + (p.get("last_name") or "")).strip()
        cands.append({"name": nm, "title": p.get("title") or "",
                      "phone": p.get("mobile_phone") or p.get("direct_phone") or p.get("phone") or "",
                      "source": "apollo", "reached": False})
    cands = [c for c in cands if (c.get("name") or "").strip()]
    if not cands:
        return {}
    cands.sort(key=lambda c: (_dm_rank(c.get("title")), c.get("reached"), bool(c.get("phone"))), reverse=True)
    dm = cands[0]
    phones = [c["phone"] for c in cands if c.get("phone")] + ([cophone] if cophone else [])
    best = next((p for p in phones if _is_mobile_au(p)), None) \
        or next((c["phone"] for c in cands if c.get("reached") and c.get("phone")), None) \
        or (phones[0] if phones else "")
    conf = min(100, _dm_rank(dm.get("title")) + (20 if dm.get("reached") else 0) + (15 if _is_mobile_au(best) else 0))
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO lisa_dm (domain,dm_name,dm_first,dm_title,dm_phone,dm_is_mobile,source,confidence,candidates,resolved_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,now()) ON CONFLICT (domain) DO UPDATE SET dm_name=EXCLUDED.dm_name,"
            "dm_first=EXCLUDED.dm_first,dm_title=EXCLUDED.dm_title,dm_phone=EXCLUDED.dm_phone,dm_is_mobile=EXCLUDED.dm_is_mobile,"
            "source=EXCLUDED.source,confidence=EXCLUDED.confidence,candidates=EXCLUDED.candidates,resolved_at=now()",
            (domain, dm.get("name"), _first_name(dm.get("name")), dm.get("title") or "",
             _e164_au(best) if best else "", _is_mobile_au(best), dm.get("source"), conf, json.dumps(cands)))
        conn.commit()
    return {"domain": domain, "dm_first": _first_name(dm.get("name")), "dm_title": dm.get("title"),
            "dm_phone": _e164_au(best) if best else "", "dm_is_mobile": _is_mobile_au(best), "confidence": conf}


def apollo_resolve_dm(pool: ConnectionPool, settings: Settings, domain: str) -> dict:
    """Domain -> Apollo (PAID): the real trading business NAME + the top decision-maker + their EMAIL
    (sync) and PHONE (async via webhook). Stores into lisa_dm (trading_name, dm_email, dm_phone). Gated by
    settings.apollo_paid_reveal + a daily credit cap. Best-effort; never raises."""
    if not domain or not getattr(settings, "apollo_paid_reveal", False) or not getattr(settings, "apollo_api_key", ""):
        return {}
    ensure_tables(pool)
    try:
        used = _fetch(pool, "SELECT count(*) n FROM lisa_dm WHERE source='apollo_reveal' "
                      "AND (resolved_at AT TIME ZONE %s)::date=(now() AT TIME ZONE %s)::date",
                      (settings.tz, settings.tz))[0]["n"]
        if used >= int(getattr(settings, "apollo_reveal_max_per_day", 300)):
            return {"skipped": "daily reveal cap"}
    except Exception:
        pass
    nm = trading = email = phone = ""
    title = None
    try:
        from .enrichment.apollo import ApolloClient
        with ApolloClient(settings) as ac:
            org = ac.enrich_organization(domain) or {}
            org_id = org.get("id"); trading = (org.get("name") or "").strip()
            emp = org.get("employees")
            # BIG-COMPANY GUARD — catches big companies the null-revenue D&B filter misses (e.g. Winc):
            # drop from the pool + cancel its queued calls so Lisa never dials one.
            if isinstance(emp, int) and emp > 200:
                try:
                    with pool.connection() as conn, conn.cursor() as cur:
                        cur.execute("DELETE FROM lisa_pool WHERE domain=%s", (domain,))
                        cur.execute(
                            "UPDATE calendar_events SET status='cancelled' WHERE bde_name='Lisa' AND status='pending' "
                            "AND type IN ('fresh_call','retry','callback') AND "
                            "right(regexp_replace(COALESCE(dest_number,''),'[^0-9]','','g'),9) IN "
                            "(SELECT right(regexp_replace(COALESCE(phone,''),'[^0-9]','','g'),9) FROM companies WHERE domain=%s)",
                            (domain,))
                        # cache a too_big marker so the fast dial-path aborts without any Apollo call
                        cur.execute(
                            "INSERT INTO lisa_dm (domain,source,trading_name,confidence,resolved_at) "
                            "VALUES (%s,'too_big',%s,0,now()) ON CONFLICT (domain) DO UPDATE SET source='too_big', "
                            "trading_name=COALESCE(NULLIF(EXCLUDED.trading_name,''),lisa_dm.trading_name), resolved_at=now()",
                            (domain, trading))
                        conn.commit()
                except Exception:
                    pass
                log.info("apollo_skip_big_company", domain=domain, employees=emp)
                return {"skipped": "too_big", "employees": emp, "domain": domain}
            dms = ac.search_decision_makers(domain, org_id, limit=5) or []
            top = next((d for d in dms if d.get("rank", 4) <= 3), None)   # only a REAL decision-maker
            if top is None and (emp is None or (isinstance(emp, int) and emp <= 10)):
                # micro-company: the sole owner often isn't tagged senior — take the best available person
                # (prefer non-sales) so we still get a NAME + contact; at a 1-person shop that IS the DM.
                broad = ac.search_decision_makers(domain, org_id, limit=8, broad=True) or []
                top = next((d for d in broad if "sales" not in (d.get("title") or "").lower()), (broad[0] if broad else None))
            if top:
                nm = (top.get("name") or "").strip(); title = top.get("title")
                parts = nm.split()
                first = parts[0] if parts else ""
                last = " ".join(parts[1:]) if len(parts) > 1 else ""
                if top.get("id") or first:
                    base = (getattr(settings, "public_base_url", "") or "").rstrip("/")
                    tok = getattr(settings, "lisa_webhook_token", "") or ""
                    webhook = f"{base}/api/lisa/apollo-phone?token={tok}&domain={domain}" if base else None
                    try:
                        rev = ac.reveal_contact(first, last, apollo_id=top.get("id"),
                                                organization_name=trading or None, domain=domain, webhook_url=webhook)
                        email = rev.get("email") or ""; phone = rev.get("phone") or ""
                    except Exception as exc:
                        log.warning("apollo_reveal_failed", domain=domain, error=str(exc)[:160])
    except Exception as exc:
        log.warning("apollo_resolve_dm_failed", domain=domain, error=str(exc)[:160]); return {}
    if not (trading or nm or email or phone):
        return {}
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO lisa_dm (domain,dm_name,dm_first,dm_title,dm_phone,dm_is_mobile,dm_email,trading_name,"
            "  source,confidence,resolved_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'apollo_reveal',%s,now()) "
            "ON CONFLICT (domain) DO UPDATE SET "
            "  dm_name=COALESCE(NULLIF(EXCLUDED.dm_name,''),lisa_dm.dm_name), "
            "  dm_first=COALESCE(NULLIF(EXCLUDED.dm_first,''),lisa_dm.dm_first), "
            "  dm_title=COALESCE(NULLIF(EXCLUDED.dm_title,''),lisa_dm.dm_title), "
            "  dm_phone=COALESCE(NULLIF(EXCLUDED.dm_phone,''),lisa_dm.dm_phone), "
            "  dm_is_mobile=(EXCLUDED.dm_is_mobile OR lisa_dm.dm_is_mobile), "
            "  dm_email=COALESCE(NULLIF(EXCLUDED.dm_email,''),lisa_dm.dm_email), "
            "  trading_name=COALESCE(NULLIF(EXCLUDED.trading_name,''),lisa_dm.trading_name), "
            "  source='apollo_reveal', resolved_at=now()",
            (domain, nm, _first_name(nm), title or "", _e164_au(phone) if phone else "",
             _is_mobile_au(phone), email, trading, 80))
        conn.commit()
    return {"domain": domain, "dm_first": _first_name(nm), "dm_name": nm, "dm_title": title,
            "dm_email": email, "dm_phone": _e164_au(phone) if phone else "", "trading_name": trading}


def store_apollo_phone(pool: ConnectionPool, domain: str, payload) -> int:
    """Parse a phone out of an Apollo async phone-reveal webhook payload and save it to lisa_dm for
    `domain`. Never raises."""
    if not domain:
        return 0
    phone = ""
    try:
        if isinstance(payload, dict):
            people = payload.get("people") or ([payload.get("person")] if payload.get("person") else [payload])
        elif isinstance(payload, list):
            people = payload
        else:
            people = []
        # Take the BEST available number: mobile first, then a direct work line, then anything. Apollo's
        # webhook payload uses `type_cd` ('mobile' | 'work_direct' | …); older/sync shapes use `type`.
        _rank = {"mobile": 0, "work_mobile": 0, "direct": 1, "work_direct": 1, "work": 2}
        best_rank = 99
        for p in people:
            if not isinstance(p, dict):
                continue
            for pn in (p.get("phone_numbers") or []):
                num = (pn or {}).get("sanitized_number") or (pn or {}).get("raw_number")
                if not num:
                    continue
                tc = ((pn or {}).get("type_cd") or (pn or {}).get("type") or "").lower()
                r = _rank.get(tc, 3)
                if r < best_rank:
                    best_rank, phone = r, num
                if best_rank == 0:
                    break
            if best_rank == 0:
                break
    except Exception:
        pass
    if not phone:
        return 0
    try:
        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute("UPDATE lisa_dm SET dm_phone=%s, dm_is_mobile=%s, resolved_at=now() WHERE domain=%s",
                        (_e164_au(phone), _is_mobile_au(phone), domain))
            conn.commit()
            return cur.rowcount
    except Exception as exc:
        log.warning("apollo_phone_store_failed", domain=domain, error=str(exc)[:120])
        return 0


def get_decision_maker(pool: ConnectionPool, domain: str) -> dict:
    if not domain:
        return {}
    r = _fetch(pool, "SELECT domain,dm_name,dm_first,dm_title,dm_phone,dm_is_mobile,dm_email,trading_name,source,confidence,"
               "(resolved_at AT TIME ZONE 'Australia/Melbourne') resolved_local FROM lisa_dm WHERE domain=%s", (domain,))
    return dict(r[0]) if r else {}


def refresh_decision_makers(pool: ConnectionPool, settings: Settings, limit: int = 40) -> dict:
    """Resolve the DM for pooled prospects (a batch per cycle). Free pass = call-intel + BD + Apollo names.
    When apollo_paid_reveal is on, ALSO run the paid domain->Apollo resolve (trading name + DM email/phone)
    for a few per cycle (Apollo rate-limits bursts; a daily credit cap applies inside apollo_resolve_dm)."""
    ensure_tables(pool)
    paid = bool(getattr(settings, "apollo_paid_reveal", False))
    # Enrich in DIAL ORDER (highest pool priority first = what schedule_lisa_fresh queues + the dialer dials
    # first), so each prospect is Apollo-resolved BEFORE the dialer reaches it — background, ahead of time.
    if paid:
        # FEEDSTOCK (Raj, 2026-08-10): resolve DMs for pooled prospects AND for un-touched advertisers
        # straight from `companies` — because reserve_lisa_pool now only admits prospects that ALREADY
        # have a mobile, the advertiser universe must be Apollo-revealed here FIRST to get owner mobiles
        # into lisa_dm, which reserve then pulls into the dial pool. Excludes anything a human already called.
        rows = _fetch(pool,
                      "SELECT domain FROM ( "
                      "  SELECT lp.domain, lp.priority prio, lp.reserved_at ra "
                      "  FROM lisa_pool lp LEFT JOIN lisa_dm d ON d.domain=lp.domain "
                      "  WHERE NULLIF(lp.domain,'') IS NOT NULL AND (d.domain IS NULL "
                      "    OR (COALESCE(d.trading_name,'')='' AND d.source IS DISTINCT FROM 'too_big')) "
                      "  UNION "
                      "  SELECT co.domain, COALESCE((e.dataforseo->'ads'->>'count')::int,0) prio, now() ra "
                      "  FROM companies co JOIN enrichment e ON e.domain=co.domain "
                      "  WHERE (e.dataforseo->>'running_google_ads')='true' AND co.source='raghav' "
                      "    AND co.revenue_musd BETWEEN 2 AND 50 "
                      "    AND co.domain !~* '\\.(gov|edu|asn|org)\\.au$' "
                      "    AND (SELECT count(*) FROM companies c3 WHERE c3.domain=co.domain) <= 8 "
                      "    AND NOT EXISTS (SELECT 1 FROM lisa_dm d2 WHERE d2.domain=co.domain) "
                      "    AND NOT EXISTS (SELECT 1 FROM calls cc WHERE "
                      "      right(regexp_replace(COALESCE(cc.dest_number,''),'[^0-9]','','g'),9)="
                      "      right(regexp_replace(COALESCE(co.phone,''),'[^0-9]','','g'),9)) "
                      ") q ORDER BY prio DESC NULLS LAST, ra LIMIT %s", (limit,))
    else:
        rows = _fetch(pool, "SELECT lp.domain FROM lisa_pool lp LEFT JOIN lisa_dm d ON d.domain=lp.domain "
                      "WHERE NULLIF(lp.domain,'') IS NOT NULL AND d.domain IS NULL "
                      "ORDER BY lp.priority DESC NULLS LAST, lp.reserved_at LIMIT %s", (limit,))
    per_cycle = int(getattr(settings, "apollo_people_trickle_per_cycle", 6))
    n = revealed = 0
    for r in rows:
        if paid and revealed < per_cycle:
            apollo_resolve_dm(pool, settings, r["domain"]); revealed += 1   # trading name + DM + email/phone + big-co drop
        else:
            resolve_decision_maker(pool, settings, r["domain"])            # free: call-intel + BD + Apollo names
        n += 1
    return {"resolved": n, "revealed": revealed}


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
    # keep findings SHORT (~15 words) — a long finding = a scripted-sounding monologue = "are you an AI?"
    f1 = ("I noticed you're running Google Ads — but a fair bit of the free Google traffic right next to "
          "them looks like it's slipping to competitors")
    proof = ""
    return {"finding_1": f1, "finding_2": "", "finding_proof": proof,
            "primary_channel": "the free side next to your ads", "competitor_hook": ""}


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
            "       bool_or(cl.meeting_booked) ever_booked, bool_or(cl.callback_requested) ever_callback, "
            "       bool_or(cl.do_not_contact) ever_dnc, "
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
        try:   # prospect_pipeline schema varies; recover a domain if the column exists, else skip safely
            pr = _fetch(pool, "SELECT website FROM prospect_pipeline WHERE dest9=%s AND NULLIF(website,'') IS NOT NULL LIMIT 1", (dest9,))
            if pr:
                domain = _clean_domain(pr[0].get("website"))
        except Exception as exc:
            log.warning("lisa_brief_pipeline_lookup_skipped", error=str(exc)[:100])

    company = (row or {}).get("company") or ""
    niche = (row or {}).get("industry") or ""
    email = (row or {}).get("email") or ""
    contact = _first_name((row or {}).get("contact") or "")   # FIRST name only (never "Clint Robinson")

    # company/industry from companies table if missing
    if domain and (not company or not niche):
        cr = _fetch(pool, "SELECT company_name AS business_name, industry FROM companies WHERE domain=%s LIMIT 1", (domain,))
        if cr:
            company = company or (cr[0].get("business_name") or "")
            niche = niche or _humanize_sector(cr[0].get("industry"))   # D&B catalogue label → spoken phrase (or blank)

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

    # --- INTELLIGENT prior-contact decision (owner rule): only WARMLY reference a prior chat if it went
    #     POSITIVE (booked before / callback requested / warm-hot interest). If it was negative or cold, open
    #     fresh and keep the history PRIVATE — never remind a negative prospect that we called. ---
    lt = ((row or {}).get("lead_temp") or "").lower()
    # POSITIVE = a genuine warm signal, not just any prior chat. A callback TIME or "we have an agency"
    # is NOT positive. Requires: booked before, OR warm/hot interest, OR a callback request that wasn't cold.
    positive = bool(row and not row.get("ever_dnc") and (
        row.get("ever_booked")
        or lt in ("warm", "hot", "interested", "positive", "keen")
        or (row.get("ever_callback") and lt not in ("none", "cold", ""))))
    prior = ""
    if positive:
        prior = ("You had a genuinely positive chat with our team recently"
                 + (f" about {row.get('problem')}" if row.get("problem") else "")
                 + " — it sounded worth picking up, so this is a warm, friendly follow-up.")
    kb = []
    if row:
        if row.get("ever_agency"):
            kb.append("They likely already use a marketing agency — frame as an independent second opinion; "
                      "don't rubbish their agency")
        if row.get("problem"):
            kb.append((f"Likely pain point: {row['problem']}") if positive
                      else f"Likely pain point (PRIVATE — never say we discussed it): {row['problem']}")
        if lt and lt not in ("none", ""):
            kb.append(f"Prior signal{'' if positive else ' (private)'}: {lt}")
    known = " · ".join(kb)
    b_prior_positive = positive

    # --- objection lines + avoid-list from the LEARNED PLAYBOOK (AI Sales Coach mines won/lost calls);
    #     fall back to sensible defaults until the coach has run. ---
    pb = get_playbook(pool)
    obj_agency = pb.get("objection_agency") or (
        "Good — makes sense. The session's completely independent, so it either confirms your agency's "
        "nailing it, or gives you sharper questions to ask them. Either way you come out ahead.")
    obj_price = pb.get("objection_price") or (
        "Fair question — the session and the audit are free, and pricing only comes up later because it's "
        "tailored to what we find, which is exactly what the session is for. Morning or afternoon tomorrow?")
    obj_email = pb.get("objection_email") or (
        "Happy to send something — so it's actually useful and not just another email you ignore, what's "
        "the one thing about your online enquiries you'd most want answered?")
    avoid_list = " · ".join(pb.get("avoid") or []) if isinstance(pb.get("avoid"), list) else ""

    b.update({
        "prospect_name": contact or "",
        "company_name": _clean_company(company),   # spoken form — no 'PTY LTD', no SHOUTING-CAPS
        "prospect_website": domain or ((row or {}).get("website") or ""),
        # bare domain for the email-confirm move ("I'll send it to info@<domain> — still the best one?")
        "company_domain": _norm_domain(domain or ((row or {}).get("website") or "")),
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
        "prior_positive": "true" if b_prior_positive else "false",
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
    # APOLLO-RESOLVED CONTACT (Raj, 2026-08-10): hand Lisa the DM's email + mobile UP FRONT so she CONFIRMS
    # them on the call ("I've got your email as X — still the best one?") rather than asking cold. She only
    # ASKS for a field that's empty (Apollo had nothing). The confirmed email is what the cal.com invite
    # is sent to, so it must be captured for every booking.
    dm = get_decision_maker(pool, domain) if domain else {}
    apollo_email = (dm.get("dm_email") or "").strip()
    apollo_mobile = ((dm.get("dm_phone") or "").strip() if dm.get("dm_is_mobile") else "")
    if not b.get("prospect_email"):
        b["prospect_email"] = apollo_email
    b["apollo_email"] = apollo_email
    b["apollo_mobile"] = apollo_mobile
    if apollo_email or apollo_mobile:
        b["contact_prefill"] = (
            (f"Email on file: {apollo_email}. " if apollo_email else "")
            + (f"Mobile on file: {apollo_mobile}. " if apollo_mobile else "")
            + "CONFIRM these back to them before booking; only ASK for whichever is missing.")
        b["have_contact"] = "true"
    else:
        b["contact_prefill"] = "No email or mobile on file — ASK for the best email (and mobile) before you book."
        b["have_contact"] = "false"
    # Honesty guard: only let Lisa claim they run Google Ads when the data actually confirms it.
    # If unconfirmed, the prompt falls back to a neutral "how you're showing up on Google" opener.
    try:
        _ads = _fetch(pool, "SELECT (dataforseo->>'running_google_ads')='true' r FROM enrichment WHERE domain=%s",
                      (domain,)) if domain else []
        b["runs_google_ads"] = "true" if (_ads and _ads[0].get("r")) else "false"
    except Exception:
        b["runs_google_ads"] = "false"
    if facts.get("dm_name") and not contact:
        b["decision_maker"] = facts["dm_name"]
    if facts.get("what_they_do") and not niche:
        b["prospect_niche"] = facts["what_they_do"]
    # never leave niche blank (a blank field is what makes the model echo a placeholder)
    if not (b.get("prospect_niche") or "").strip():
        b["prospect_niche"] = "your industry"
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


def inbound_variables(pool: ConnectionPool, settings: Settings, from_number: str) -> dict:
    """INBOUND: a prospect is calling Lisa's number BACK. Look them up by their number and hand Lisa their
    saved brief + name + prior context so she recognises them and picks up where they left off (instead of
    answering as a stranger). Returns {dynamic_variables, metadata} for Retell's inbound webhook."""
    ensure_tables(pool)
    d9 = _d9(from_number)
    brief: dict = {}
    r = _fetch(pool, "SELECT brief FROM lisa_briefs WHERE dest9=%s", (d9,))
    if r and r[0].get("brief"):
        brief = dict(r[0]["brief"])
    else:
        try:
            brief = build_brief(pool, settings, dest9=d9, domain=_domain_for(pool, d9))
        except Exception as exc:
            log.warning("lisa_inbound_brief_failed", d9=d9, error=str(exc)[:120])
            brief = {}
    name = brief.get("prospect_name") or ""
    brief["inbound_callback"] = "true"          # tells the prompt this is a call-BACK, greet warmly by name
    brief["prior_contact_line"] = ("Great to hear back from you" + (f", {name}" if name else "")
                                   + " — thanks for calling back!")
    known = bool(r and r[0].get("brief"))
    log.info("lisa_inbound", d9=d9, known=known, name=name)
    return {"dynamic_variables": {k: ("" if v is None else str(v)) for k, v in brief.items()},
            "metadata": {"dest9": d9, "inbound": "true", "known": str(known).lower()}}


def create_web_call(settings: Settings, agent_id: str | None = None) -> dict:
    """Create a Retell WEB call (browser voice test / 'Voice Orb'). Returns {access_token, call_id} — the
    console uses the Retell web SDK + this token to let anyone talk to Lisa live from the page. No phone
    number, no PSTN cost. Pass agent_id to test a SPECIFIC agent (e.g. A/B an old vs rebuilt Lisa from the
    orb picker); defaults to the live LISA_AGENT_ID."""
    aid = (agent_id or "").strip() or getattr(settings, "lisa_agent_id", "")
    return _retell(settings, "POST", "v2/create-web-call", {"agent_id": aid})


def list_lisa_agents(settings: Settings) -> list[dict]:
    """List the account's Retell agents (id + name) so the Voice Orb can offer an agent picker — lets you
    A/B the live Lisa against an old or rebuilt version in the browser. The live LISA_AGENT_ID is flagged
    and floated to the top, then names containing 'lisa'. If LISA_ORB_AGENTS is set, ONLY those curated
    agents are returned (a clean two-item A/B picker), format "agent_id|Label,agent_id|Label"."""
    live = getattr(settings, "lisa_agent_id", "")
    raw = (getattr(settings, "lisa_orb_agents", "") or "").strip()
    if raw:
        picks = []
        for part in raw.split(","):
            part = part.strip()
            if not part:
                continue
            aid, _sep, label = part.partition("|")
            aid = aid.strip()
            if aid:
                picks.append({"agent_id": aid, "agent_name": (label.strip() or aid), "is_live": aid == live})
        if picks:
            return picks
    try:
        res = _retell(settings, "GET", "list-agents", None)
    except Exception:
        return []
    rows = res if isinstance(res, list) else ((res.get("agents") if isinstance(res, dict) else []) or [])
    live = getattr(settings, "lisa_agent_id", "")
    out = []
    for a in rows:
        aid = (a or {}).get("agent_id") or ""
        if not aid:
            continue
        name = a.get("agent_name") or aid
        out.append({"agent_id": aid, "agent_name": name, "is_live": aid == live})
    out.sort(key=lambda r: (not r["is_live"], "lisa" not in (r["agent_name"] or "").lower(), (r["agent_name"] or "").lower()))
    return out


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
    # DM + trading name + email/phone are resolved in the BACKGROUND (refresh_decision_makers, ahead of the
    # dialer) so the dial path stays fast and never contends on the DB. Here we only READ the pre-resolved
    # record and honour a cached big-company marker (no Apollo call, no writes at dial time).
    dm = get_decision_maker(pool, dom) if dom else {}
    if dm.get("source") == "too_big":
        return {"error": f"skipped big company: {dom}"}
    if dm.get("dm_phone") and dm.get("dm_is_mobile"):
        _cand = _e164_au(dm["dm_phone"])
        if _cand and _cand != to_number and len(re.sub(r"[^0-9]", "", _cand)) >= 11:
            to_number = _cand
            d9 = _d9(to_number)
    if getattr(settings, "lisa_audit_before_call", True) and dom:
        if ensure_audit(pool, settings, dom):
            try:
                build_and_store_brief(pool, settings, dest9=d9, domain=dom)   # rebuild with the fresh audit
            except Exception as exc:
                log.warning("lisa_rebuild_after_audit_failed", domain=dom, error=str(exc)[:160])
    brief = get_brief(pool, settings, dest9=d9, domain=dom)   # use the RESOLVED domain (fixes empty briefs on fresh-queue calls)
    # overlay the CURRENT coach playbook so the objection library + avoid-list are always the latest,
    # even on a brief that was stored before the last coaching refresh.
    pb = get_playbook(pool)
    for k in ("objection_agency", "objection_price", "objection_email", "objection_not_interested", "objection_no_time"):
        if pb.get(k):
            brief[k] = str(pb[k])
    if isinstance(pb.get("avoid"), list) and pb["avoid"]:
        brief["avoid_list"] = " · ".join(pb["avoid"])
    # greet the resolved decision-maker by name (first name) if we have one
    if dm.get("dm_first"):
        brief["prospect_name"] = dm["dm_first"]
        brief["decision_maker"] = dm["dm_first"]
        if dm.get("dm_title"):
            brief["dm_role"] = dm["dm_title"]
    # prefer the real TRADING name over a trust/legal entity (e.g. "…DISCRETIONARY TRUST"), and carry the
    # DM's Apollo-revealed email so Lisa references the business people recognise + can text the audit.
    if dm.get("trading_name"):
        brief["company_name"] = dm["trading_name"]
    if dm.get("dm_email"):
        brief["prospect_email"] = dm["dm_email"]
        brief["confirmed_email"] = dm["dm_email"]
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
    # SELF-CLEANING DM NUMBERS: a 'wrong_number' on a stored DM phone means that number is stale/wrong —
    # erase it so we never dial it again (next attempt falls back to the company main line), and the
    # background resolver can find a fresh one. This is the feedback loop that keeps DM-mobile quality
    # honest (measured 7.3% of DM mobiles answering as wrong-number before this).
    if outcome == "wrong_number":
        try:
            wrong_to = _e164_au(call.get("to_number") or "")
            if wrong_to:
                with pool.connection() as conn, conn.cursor() as cur:
                    cur.execute("UPDATE lisa_dm SET dm_phone=NULL, dm_is_mobile=false, "
                                "  candidates = COALESCE(candidates,'{}'::jsonb) || jsonb_build_object('wrong_number', %s) "
                                "WHERE dm_phone=%s", (wrong_to, wrong_to))
                    if cur.rowcount:
                        log.info("lisa_dm_phone_cleared_wrong_number", phone=wrong_to, rows=cur.rowcount)
                    conn.commit()
        except Exception as exc:
            log.warning("lisa_dm_wrongnum_clear_failed", error=str(exc)[:120])
    if outcome == "do_not_call" and d9:
        # They asked for the link on THIS call ("text it to me — and don't call again"): honour that FIRST.
        # Once DND lands we never contact them again, so this send happens BEFORE the suppression below.
        try:
            dom_dnc = _norm_domain(dyn.get("prospect_website") or "")
            if (dom_dnc and (_wants_sms_link(cad) or _wants_report(cad))
                    and _is_mobile_au(call.get("to_number") or "")
                    and not _fetch(pool, "SELECT 1 FROM lisa_audit_shares WHERE domain=%s LIMIT 1", (dom_dnc,))):
                r_dnc = send_audit_link(pool, settings, domain=dom_dnc, to_number=call.get("to_number"),
                                        name=dyn.get("prospect_name"))
                if r_dnc.get("ok"):
                    result["audit_link_sent"] = r_dnc.get("url")
                    log.info("lisa_audit_sent_before_dnd", call_id=cid, domain=dom_dnc)
        except Exception as exc:
            log.warning("lisa_dnc_sms_link_failed", call_id=cid, error=str(exc)[:160])
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

    # 2b) prospect ASKED FOR A REPORT / PROPOSAL / "just email me the details" → fully generate + verify the
    #     audit and text them the public view link (we don't email; this is how they get it in writing). Fires
    #     only on a real conversation, once per prospect, when we have a mobile + a domain.
    try:
        dom = _norm_domain(dyn.get("prospect_website") or "")
        if (_wants_report(cad) and dom and _is_mobile_au(call.get("to_number") or "")
                and outcome in ("callback_requested", "not_interested", "conversation", "gatekeeper_only")):
            already = _fetch(pool, "SELECT 1 FROM lisa_audit_shares WHERE domain=%s LIMIT 1", (dom,))
            if not already:
                r = send_audit_link(pool, settings, domain=dom, to_number=call.get("to_number"),
                                    name=dyn.get("prospect_name"))
                if r.get("ok"):
                    result["audit_link_sent"] = r.get("url")
                    log.info("lisa_audit_autosent", call_id=cid, domain=dom, url=r.get("url"))
                else:
                    log.info("lisa_audit_autosend_skipped", call_id=cid, domain=dom, why=r.get("error"))
    except Exception as exc:
        log.warning("lisa_audit_autosend_failed", call_id=cid, error=str(exc)[:160])
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
    notes = (f"🎙️ Booked by Lisa (AI) — DE Group strategy session.\n"
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
    is_pm = ("pm" in t) or ("afternoon" in t) or ("evening" in t)
    # hour: explicit '2pm'/'8 am' first; else spelled-out ('eight' -> 8); else part-of-day; else 10am
    hour = None
    m = re.search(r"(\d{1,2})\s*(?::(\d{2}))?\s*(am|pm)", t)
    if m:
        hour = int(m.group(1)) % 12 + (12 if m.group(3) == "pm" else 0)
    if hour is None:
        _NUM = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
                "nine": 9, "ten": 10, "eleven": 11, "twelve": 12}
        for w, n in _NUM.items():
            if re.search(rf"\b{w}\b", t):
                hour = n % 12 + (12 if is_pm else 0)
                break
    if hour is None:
        hour = 14 if "afternoon" in t else 17 if "evening" in t else 10
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
                if "next" in t:                        # 'next tuesday' -> the FOLLOWING week's Tuesday
                    ahead += 7
                day = now.date() + timedelta(days=ahead)
                break
    if not day and "next week" in t:                   # bare 'next week' -> next Monday (not tomorrow)
        day = now.date() + timedelta(days=((0 - now.weekday()) % 7 or 7))
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


def _twilio_ready(settings: Settings) -> bool:
    """Twilio backend SMS is usable when we have the AC account (for the URL) + a secret (API key or token)."""
    acct = getattr(settings, "twilio_account_sid", "") or ""
    secret = getattr(settings, "twilio_api_key_secret", "") or getattr(settings, "twilio_auth_token", "") or ""
    return bool(acct.startswith("AC") and secret)


def _send_sms_twilio(settings: Settings, to_number: str, body: str, from_number: str) -> bool:
    """Send one SMS DIRECTLY via the Twilio REST API (backend process — never through the Retell voice
    agent, so it can't touch Lisa's call latency). Auth prefers an API Key (SK…:secret) and falls back to
    Account SID:Auth Token; the URL always uses the AC… account. Uses a Messaging Service if configured,
    else the given from-number. Returns True on 2xx. Raises on HTTP error (caller logs)."""
    # Landlines / non-mobiles CANNOT receive SMS — never attempt one (audit links, follow-ups, any SMS).
    if not _is_mobile_au(to_number):
        log.info("sms_skipped_not_mobile", to=to_number)
        return False
    acct = getattr(settings, "twilio_account_sid", "") or ""          # AC… — the account in the URL
    key_sid = getattr(settings, "twilio_api_key_sid", "") or ""       # SK… — API key (preferred auth user)
    secret = getattr(settings, "twilio_api_key_secret", "") or getattr(settings, "twilio_auth_token", "") or ""
    user = key_sid or acct                                            # API key SID if present, else account SID
    if not (acct.startswith("AC") and user and secret):
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
    auth = base64.b64encode(f"{user}:{secret}".encode()).decode()
    req = urllib.request.Request(
        f"https://api.twilio.com/2010-04-01/Accounts/{acct}/Messages.json", data=data, method="POST",
        headers={"Authorization": f"Basic {auth}", "Content-Type": "application/x-www-form-urlencoded"})
    urllib.request.urlopen(req, timeout=30)
    return True


def _sms_optout_kind(body: str) -> str:
    """Classify a prospect text as an opt-out / bad-number signal (or '' if it's a normal reply)."""
    low = (body or "").strip().lower()
    if low in ("stop", "stop.", "unsubscribe") or low.startswith("stop "):
        return "stop"
    for k in ("remove this number", "remove my number", "take me off", "don't text", "do not text",
              "don't call", "do not call", "wrong number", "wrong person", "not interested"):
        if k in low:
            return "optout"
    return ""


def _enforce_sms_optout(pool: ConnectionPool, d9: str, from_number: str, body: str) -> None:
    """Honour it EVERYWHERE: no more texts, no more calls, out of every pool, DNC on the funnel."""
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute("UPDATE calendar_events SET status='cancelled' WHERE status='pending' "
                    "AND right(regexp_replace(COALESCE(dest_number,''),'[^0-9]','','g'),9)=%s", (d9,))
        cur.execute("DELETE FROM lisa_pool WHERE dest9=%s", (d9,))
        cur.execute("UPDATE lisa4_pool SET bucket='ok', issue='opt-out (SMS)' WHERE dest9=%s", (d9,))
        cur.execute("UPDATE classifications SET do_not_contact=true WHERE call_id IN "
                    "(SELECT call_id FROM calls WHERE right(regexp_replace(COALESCE(dest_number,''),'[^0-9]','','g'),9)=%s)", (d9,))
        conn.commit()
    log.info("sms_optout_enforced", d9=d9, body=body[:60])


def _send_followup_sms(pool: ConnectionPool, settings: Settings, call_id: str, to_number: str | None,
                       name: str | None, call_from: str | None = None) -> bool:
    """Fire the MINIMAL curiosity SMS on a missed call. This is a pure BACKEND process (Twilio direct) so
    it NEVER touches Lisa's voice latency — she only talks. The message is composed here (first name +
    callback ask, nothing that reveals a company or a sales reason). Prefers Twilio; falls back to the
    legacy Retell chat agent only if Twilio isn't configured. Gated on LISA_SMS_ENABLED."""
    if not to_number or not getattr(settings, "lisa_sms_enabled", False):
        return False
    d9 = _d9(to_number)
    recent = _fetch(pool, "SELECT 1 FROM lisa_sms WHERE dest9=%s AND (direction='inbound' "
                    "OR created_at > now() - interval '7 days') LIMIT 1", (d9,))
    if recent:
        return False   # they already got the nudge (or replied) — repeating it reads as spam
    first = (name or "").strip().split(" ")[0] if name else ""
    body = (f"Hi {first}, it's Lisa — tried to reach you, could you give me a quick call back when you get "
            "a sec? :)").replace("Hi ,", "Hi,")
    sms_from = _pick_sms_from(settings, call_from)
    sent = False
    try:
        if _twilio_ready(settings):
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
        _log_sms(pool, "outbound", sms_from, to_number, body, _d9(to_number))
        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute("UPDATE lisa_calls SET sms_sent=true, updated_at=now() WHERE call_id=%s", (call_id,))
            conn.commit()
    return sent


def _log_sms(pool: ConnectionPool, direction: str, frm: str | None, to: str | None, body: str | None,
             dest9: str | None) -> None:
    try:
        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute("INSERT INTO lisa_sms (dest9, direction, from_number, to_number, body) VALUES (%s,%s,%s,%s,%s)",
                        (dest9, direction, frm, to, body))
            conn.commit()
    except Exception as exc:
        log.warning("lisa_sms_log_failed", error=str(exc)[:120])


# --------------------------------------------------------------------------- #
# Inbound-SMS auto-responder (BOTH Lisa lines) — Vysakh's standing HARD RULES
# --------------------------------------------------------------------------- #
# These rules are fed VERBATIM as the system prompt. The #1 bar: sound like a real
# person texting, never reveal WHY we called, and only ever try to get them on a
# quick phone call (or handle pure logistics / opt-out). Post-booking texts may
# reference the already-agreed meeting.
SMS_REPLY_RULES = (
    "You compose ONE short SMS reply to a prospect who texted one of our numbers back. Owner: Vysakh. "
    "These rules are absolute.\n\n"
    "PRE-BOOKING TEXTS MUST NEVER REVEAL THE PURPOSE/TOPIC/PITCH OF THE CALL. No mention of Google Ads, "
    "marketing, website, SEO, \"your rankings\", \"we noticed\", offers, or why we called. The text's ONLY "
    "job is to get them on a quick phone call (or handle pure logistics/opt-out).\n"
    "- If they ask \"who are you?\" / \"what's this about?\": stay warm, create light curiosity, say it's quick "
    "and genuinely worth a 2-minute chat, and ask when's a good time to call. DO NOT explain the reason.\n"
    "- If they offer/ask a time (\"tomorrow morning\", \"after 5\", \"call me Friday\"): AGREE to a specific time "
    "and confirm it. That's a win.\n"
    "- POST-BOOKING texts (only once a meeting is already booked) MAY reference the agreed meeting "
    "(confirmations, reminders, the calendar invite) — that purpose is already accepted. NEVER mention or "
    "offer a 'video link' — that phrase is banned.\n"
    "- IF THEY ASK FOR THE WEBSITE / COMPANY LINK (\"send me a link to your website\", \"i want to see your "
    "site first\", \"can you send your company site\"): the site is ALREADY BUILT and ready. Reply warmly that "
    "you'll send them the link and our company details shortly, ahead of the meeting. It is a HARD RULE that "
    "you NEVER say or imply it is being built / made / prepared / 'ready soon' / 'once it's done' — to them it "
    "already exists and you're simply sending it across. Keep it to a brief 'will flick you the link shortly'. "
    "Do NOT paste any URL yourself — a separate step sends the real link once it's available.\n"
    "- Warm, brief (<=300 chars), casual AU tone, human, no emoji spam, never hard-sell over text, never invent.\n"
    "- SOUND LIKE A REAL PERSON TEXTING, NOT AN AI. This is the top quality bar. Concretely:\n"
    "  * lower-case, casual; short fragments over full sentences; contractions (i'm, you're, no worries).\n"
    "  * AU texting vocab: \"buzz\", \"quick sec\", \"arvo\", \"cheers\", \"ta\", \"no dramas\", \"yeah\". Vary it per reply.\n"
    "  * NO corporate/AI tells: never \"I wanted to reach out\", \"I hope this finds you\", \"feel free to\", \"at your "
    "earliest convenience\", \"reach out\", \"touch base\", \"assist you\", em-dashes, semicolons, or 3 exclamation "
    "marks. Max one \"!\" and often none.\n"
    "  * match THEIR energy + wording; reference what they actually said (\"all good, finished with patients then!\").\n"
    "  * a tiny bit of imperfection is fine (a trailing \"..\", \"haha\", \"yep\") — it reads human.\n"
    "  * read it back: if it sounds like a polished marketing bot, rewrite it looser."
)

# claude-opus-4-8 token cost in CENTS/token ($5 in / $25 out per 1M) — logged to lisa_llm_usage like the
# OpenAI helper, so the cost page stays accurate for the SMS auto-responder too.
_CLAUDE_SMS_RATE = {"in": 500 / 1_000_000, "out": 2500 / 1_000_000}

# The SMS auto-responder returns the reply + a structured read on the prospect's intent via a FORCED
# function/tool schema (guaranteed shape). ONE schema shared by both providers — OpenAI function-calling
# (the default LLM, same key/client the classifier uses) and the Anthropic fallback.
_SMS_TOOL_NAME = "sms_reply"
_SMS_TOOL_DESC = "Return the SMS reply to send and the read on the prospect's intent."
_SMS_INTENT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "reply": {"type": "string",
                  "description": "The exact SMS text to send back (<=300 chars, lower-case casual AU, human)."},
        "wants_callback": {"type": "boolean",
                           "description": "They proposed / agreed a time for us to call them."},
        "callback_time_text": {"type": "string",
                               "description": "Their OWN words for the time, e.g. 'tomorrow arvo', 'friday 3pm', 'after 5'. Empty if none."},
        "opted_out": {"type": "boolean",
                      "description": "They asked to stop / not be contacted / wrong number."},
        "not_interested": {"type": "boolean",
                           "description": "They declined but did not opt out."},
        "is_question": {"type": "boolean",
                        "description": "They asked who we are / what it's about."},
    },
    "required": ["reply", "wants_callback", "callback_time_text", "opted_out", "not_interested", "is_question"],
}

_L4_LINE_TOKENS = ("l4", "lisa4", "lisa 4", "lisa-4", "4")
_L5_LINE_TOKENS = ("l5", "lisa5", "lisa 5", "lisa-5", "5")
_L1_LINE_TOKENS = ("l1", "lisa1", "lisa 1", "lisa-1", "lisa", "1", "")

# TRUE opt-out phrases for the auto-responder: STOP / unsubscribe / remove / do-not-contact / bad number.
# A bare "not interested" is intentionally excluded — the responder treats it as a SOFT decline (classifier
# → polite close + soft mark), not a silent full-DNC. (_sms_optout_kind, used elsewhere, still catches both.)
_HARD_OPTOUT_PHRASES = ("stop", "unsubscribe", "remove this number", "remove my number", "take me off",
                        "don't text", "do not text", "don't call", "do not call", "wrong number", "wrong person")


def _is_hard_sms_optout(body: str) -> bool:
    """True only for unambiguous opt-out / bad-number texts (never for a plain 'not interested')."""
    low = (body or "").strip().lower()
    if low in ("stop", "stop.", "unsubscribe") or low.startswith("stop "):
        return True
    return any(p in low for p in _HARD_OPTOUT_PHRASES)


def _sms_line_is_l4(settings: Settings, from_line) -> bool:
    """Which agent owns this thread — Lisa-4 (websites) or Lisa-1. Accepts a line-type token ('L1'/'L4') or a
    raw line number (matched against the current + historical Lisa-4 numbers). Sets tone + the send-from
    number + the calendar owner (bde_name), NEVER anything that leaks into the text."""
    s = str(from_line or "").strip().lower()
    if s in _L4_LINE_TOKENS:
        return True
    if s in _L1_LINE_TOKENS:
        return False
    d9 = _d9(from_line)
    if d9:
        if any(_d9(n) == d9 for n in list(getattr(settings, "lisa4_numbers", []) or [])):
            return True
        try:                                     # rested Lisa-4 lines (append-only registry) still count
            from . import lisa4 as _l4
            if d9 in getattr(_l4, "L4_LINE_DIGITS_ALL", ()):
                return True
        except Exception:
            pass
    return False


def _sms_line_is_l5(settings: Settings, from_line) -> bool:
    """Which agent owns this thread — Lisa-5 (growth-audit) or not. Accepts a line-type token ('L5') or a raw
    line number (matched against Lisa-5's current numbers). Sets tone + the send-from number + the calendar
    owner (bde_name), NEVER anything that leaks into the text. Mirrors _sms_line_is_l4 for the Lisa-5 pool."""
    s = str(from_line or "").strip().lower()
    if s in _L5_LINE_TOKENS:
        return True
    if s in _L4_LINE_TOKENS or s in _L1_LINE_TOKENS:
        return False
    d9 = _d9(from_line)
    if d9 and any(_d9(n) == d9 for n in list(getattr(settings, "lisa5_numbers", []) or [])):
        return True
    return False


def _sms_line(settings: Settings, from_line) -> str:
    """Resolve which Lisa agent owns an SMS thread → 'L5' | 'L4' | 'L1'. THE single line-classifier used by
    the shared inbound-SMS responder and the evening sweep, so tone + send-from number + calendar owner all
    stay consistent per line. Checks Lisa-5 first (its numbers are distinct from Lisa-4's / Lisa-1's), then
    Lisa-4, else Lisa-1. `from_line` may be a token ('L5'/'L4'/'L1') or a raw line number."""
    if _sms_line_is_l5(settings, from_line):
        return "L5"
    if _sms_line_is_l4(settings, from_line):
        return "L4"
    return "L1"


def _has_booked_meeting(pool: ConnectionPool, d9: str) -> bool:
    """True when this prospect ALREADY has a booked meeting — a reveal/strategy calendar event (matched by
    number or by the booking call's booked_event_id), a booking call (lisa_calls.meeting_agreed), or a live
    booked_crm status. Flips the composer from pre-booking (no purpose) to post-booking (may reference it)."""
    if not d9:
        return False
    try:
        r = _fetch(pool,
                   "SELECT (EXISTS(SELECT 1 FROM calendar_events e WHERE e.type IN ('reveal','meeting') "
                   "         AND e.status IN ('pending','done') "
                   "         AND right(regexp_replace(COALESCE(e.dest_number,''),'[^0-9]','','g'),9)=%s) "
                   "     OR EXISTS(SELECT 1 FROM lisa_calls lc JOIN calendar_events e2 ON e2.id=lc.booked_event_id "
                   "         WHERE lc.dest9=%s AND e2.status IN ('pending','done')) "
                   "     OR EXISTS(SELECT 1 FROM lisa_calls lc2 WHERE lc2.dest9=%s AND COALESCE(lc2.meeting_agreed,false))"
                   "    ) AS booked", (d9, d9, d9))
        if r and r[0].get("booked"):
            return True
    except Exception:
        pass
    try:   # booked_crm belongs to the Lisa subsystem and is absent on some DBs — never let it break the check
        r = _fetch(pool, "SELECT 1 FROM booked_crm WHERE dest9=%s AND COALESCE(NULLIF(status,''),'') <> '' "
                   "AND lower(status) NOT IN ('lost','cancelled','canceled','no_show','no-show','dead',"
                   "'not_interested','declined') LIMIT 1", (d9,))
        if r:
            return True
    except Exception:
        pass
    return False


def _booked_meeting_when(pool: ConnectionPool, settings: Settings, d9: str) -> str:
    """Melbourne wall-clock text of the prospect's booked meeting (for a post-booking confirmation/reminder),
    or '' if we can't pin a time. Prefers a pending reveal/meeting matched by number, else via the booking
    call's booked_event_id."""
    for sql, params in (
        ("SELECT e.start_at s FROM calendar_events e WHERE e.type IN ('reveal','meeting') "
         "AND e.status='pending' AND right(regexp_replace(COALESCE(e.dest_number,''),'[^0-9]','','g'),9)=%s "
         "ORDER BY e.start_at LIMIT 1", (d9,)),
        ("SELECT e.start_at s FROM lisa_calls lc JOIN calendar_events e ON e.id=lc.booked_event_id "
         "WHERE lc.dest9=%s AND e.status='pending' ORDER BY e.start_at LIMIT 1", (d9,))):
        try:
            r = _fetch(pool, sql, params)
            if r and r[0].get("s"):
                return r[0]["s"].astimezone(ZoneInfo(settings.tz)).strftime("%A %d %b, %-I:%M%p").replace(":00", "")
        except Exception:
            continue
    return ""


def _sms_prospect_number(pool: ConnectionPool, d9: str) -> str:
    """The prospect's exact E.164 to reply to — taken from their most recent inbound row (so we text the same
    number they messaged from); falls back to reconstructing an AU mobile from the trailing-9."""
    try:
        r = _fetch(pool, "SELECT from_number FROM lisa_sms WHERE dest9=%s AND direction='inbound' "
                   "AND NULLIF(from_number,'') IS NOT NULL ORDER BY created_at DESC LIMIT 1", (d9,))
        if r and r[0].get("from_number"):
            return _e164_au(r[0]["from_number"])
    except Exception:
        pass
    return _e164_au(d9)


def _sms_already_answered(pool: ConnectionPool, d9: str, body: str) -> bool:
    """Dedupe: skip phone-auto-replies we've already handled. TRUE when (A) an outbound already landed at/after
    the latest inbound within the last hour (we answered this exact message — duplicate webhook / webhook-vs-
    sweep race), or (B) an identical inbound text within the last hour already got a reply (Twilio retry)."""
    try:
        inb = _fetch(pool, "SELECT created_at FROM lisa_sms WHERE dest9=%s AND direction='inbound' "
                     "ORDER BY created_at DESC LIMIT 1", (d9,))
        if not inb:
            return False
        latest = inb[0]["created_at"]
        a = _fetch(pool, "SELECT 1 FROM lisa_sms WHERE dest9=%s AND direction='outbound' "
                   "AND created_at >= %s AND created_at > now() - interval '1 hour' LIMIT 1", (d9, latest))
        if a:
            return True
        b = _fetch(pool, "SELECT 1 FROM lisa_sms i WHERE i.dest9=%s AND i.direction='inbound' AND i.body=%s "
                   "AND i.created_at < %s AND i.created_at > now() - interval '1 hour' "
                   "AND EXISTS(SELECT 1 FROM lisa_sms o WHERE o.dest9=i.dest9 AND o.direction='outbound' "
                   "           AND o.created_at > i.created_at) LIMIT 1", (d9, body, latest))
        if b:
            return True
    except Exception:
        pass
    return False


def _claude_sms_compose(settings: Settings, pool: ConnectionPool | None, *, system: str, user: str) -> tuple[str, dict]:
    """One LLM call that returns the SMS reply + a read on the prospect's intent, via a FORCED function/tool
    schema (guaranteed shape). Uses the DEFAULT LLM = OpenAI (the SAME key/client the classifier + coach use)
    whenever an OpenAI key is configured; Anthropic is an OPTIONAL fallback only when no OpenAI key is set.
    Logs token cost to lisa_llm_usage. Returns ('', {}) on any failure so the caller can fall back to a safe
    purpose-free reply. (Name kept for callers; no longer Anthropic-specific.)"""
    openai_key = getattr(settings, "llm_api_key", "") or ""
    provider = (getattr(settings, "llm_provider", "openai") or "openai").lower()
    # OpenAI is the default: use it unless the deployment is explicitly pinned to Anthropic or has no OpenAI key.
    if openai_key and provider != "anthropic":
        return _openai_sms_compose(settings, pool, system=system, user=user, key=openai_key)
    return _anthropic_sms_compose(settings, pool, system=system, user=user)


def _openai_sms_compose(settings: Settings, pool: ConnectionPool | None, *, system: str, user: str,
                        key: str) -> tuple[str, dict]:
    """DEFAULT path: one OpenAI function-calling call (forced tool) returning the shared SMS-intent schema."""
    model = getattr(settings, "llm_model_strong", "") or "gpt-4o"
    tool = {"type": "function",
            "function": {"name": _SMS_TOOL_NAME, "description": _SMS_TOOL_DESC, "parameters": _SMS_INTENT_SCHEMA}}
    try:
        from openai import OpenAI
        client = OpenAI(api_key=key)
        r = client.chat.completions.create(
            model=model, temperature=0,
            messages=[{"role": "system", "content": system},
                      {"role": "user", "content": user[:8000]}],
            tools=[tool], tool_choice={"type": "function", "function": {"name": _SMS_TOOL_NAME}})
        _log_sms_llm_usage_openai(pool, model, r)
        msg = r.choices[0].message
        for tc in (getattr(msg, "tool_calls", None) or []):
            if getattr(getattr(tc, "function", None), "name", "") == _SMS_TOOL_NAME:
                data = dict(json.loads(tc.function.arguments or "{}"))
                return (str(data.get("reply") or "").strip()[:320]), data
    except Exception as exc:
        log.warning("lisa_sms_llm_failed", provider="openai", error=str(exc)[:160])
    return "", {}


def _anthropic_sms_compose(settings: Settings, pool: ConnectionPool | None, *, system: str,
                           user: str) -> tuple[str, dict]:
    """FALLBACK path (only when no OpenAI key): one Claude tool-forced call, same shared SMS-intent schema."""
    key = getattr(settings, "anthropic_api_key", "") or ""
    if not key:
        return "", {}
    model = getattr(settings, "lisa_sms_model", "") or "claude-opus-4-8"
    tool = {"name": _SMS_TOOL_NAME, "description": _SMS_TOOL_DESC, "input_schema": _SMS_INTENT_SCHEMA}
    try:
        from anthropic import Anthropic
        client = Anthropic(api_key=key)
        msg = client.messages.create(          # opus-4-8: no temperature/thinking (both 400 on 4.7+)
            model=model, max_tokens=1024, system=system,
            messages=[{"role": "user", "content": user[:8000]}],
            tools=[tool], tool_choice={"type": "tool", "name": _SMS_TOOL_NAME})
        _log_sms_llm_usage(pool, model, msg)
        for block in msg.content:
            if getattr(block, "type", "") == "tool_use" and getattr(block, "name", "") == _SMS_TOOL_NAME:
                data = dict(block.input or {})
                return (str(data.get("reply") or "").strip()[:320]), data
    except Exception as exc:
        log.warning("lisa_sms_llm_failed", provider="anthropic", error=str(exc)[:160])
    return "", {}


def _log_sms_llm_usage_openai(pool: ConnectionPool | None, model: str, r) -> None:
    """Log an OpenAI SMS-compose call's token cost to lisa_llm_usage (gpt-4o-class rate _LLM_RATE)."""
    if pool is None:
        return
    try:
        u = getattr(r, "usage", None)
        if not u:
            return
        pt = getattr(u, "prompt_tokens", 0) or 0
        ct = getattr(u, "completion_tokens", 0) or 0
        cost = round(pt * _LLM_RATE["in"] + ct * _LLM_RATE["out"], 4)
        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute("INSERT INTO lisa_llm_usage (purpose, model, prompt_tokens, completion_tokens, cost_cents) "
                        "VALUES (%s,%s,%s,%s,%s)", ("sms_reply", model, pt, ct, cost))
            conn.commit()
    except Exception as exc:
        log.warning("lisa_sms_llm_usage_log_failed", error=str(exc)[:120])


def _log_sms_llm_usage(pool: ConnectionPool | None, model: str, msg) -> None:
    if pool is None:
        return
    try:
        u = getattr(msg, "usage", None)
        if not u:
            return
        pt = getattr(u, "input_tokens", 0) or 0
        ct = getattr(u, "output_tokens", 0) or 0
        cost = round(pt * _CLAUDE_SMS_RATE["in"] + ct * _CLAUDE_SMS_RATE["out"], 4)
        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute("INSERT INTO lisa_llm_usage (purpose, model, prompt_tokens, completion_tokens, cost_cents) "
                        "VALUES (%s,%s,%s,%s,%s)", ("sms_reply", model, pt, ct, cost))
            conn.commit()
    except Exception as exc:
        log.warning("lisa_sms_llm_usage_log_failed", error=str(exc)[:120])


def _upsert_sms_callback(pool: ConnectionPool, bde_name: str, d9: str, dest_number: str,
                         when: datetime, body: str, created_by: str) -> None:
    """A prospect texted a time → book/refresh a pending callback on the right calendar (Lisa-1 or Lisa-4),
    at the exact parsed Melbourne time. Idempotent: updates the existing pending callback for this prospect
    rather than stacking duplicates."""
    from .calendar import create_event, update_event
    end = when + timedelta(minutes=15)
    ex = _fetch(pool, "SELECT id FROM calendar_events WHERE bde_name=%s AND status='pending' AND type='callback' "
                "AND right(regexp_replace(COALESCE(dest_number,''),'[^0-9]','','g'),9)=%s ORDER BY id DESC LIMIT 1",
                (bde_name, d9))
    notes = f"Prospect texted a time: {body[:120]}"
    if ex:
        update_event(pool, ex[0]["id"], {"start_at": when, "end_at": end, "notes": notes})
    else:
        create_event(pool, bde_name=bde_name, type="callback",
                     title="📞 Callback: they TEXTED a time", start_at=when, end_at=end,
                     notes=notes, dest_number=dest_number, created_by=created_by)


def _mark_sms_not_interested(pool: ConnectionPool, d9: str) -> None:
    """Soft close (NOT a hard opt-out): stop cold-chasing this prospect — cancel their pending cold retries /
    fresh-call slots — and drop a CRM note. They can still be re-engaged later; we just don't keep dialling."""
    try:
        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute("UPDATE calendar_events SET status='cancelled' WHERE status='pending' "
                        "AND type IN ('retry','fresh_call') "
                        "AND right(regexp_replace(COALESCE(dest_number,''),'[^0-9]','','g'),9)=%s", (d9,))
            conn.commit()
    except Exception as exc:
        log.warning("lisa_sms_not_interested_failed", d9=d9, error=str(exc)[:120])
    try:
        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute("INSERT INTO booked_crm (dest9, status, note, updated_by, updated_at) "
                        "VALUES (%s,'not_interested','SMS: not interested (soft close)','sms-bot',now()) "
                        "ON CONFLICT (dest9) DO UPDATE SET "
                        "  note=left(COALESCE(NULLIF(booked_crm.note,'')||' | ','')||'SMS: not interested',400), "
                        "  updated_by='sms-bot', updated_at=now() "
                        "WHERE booked_crm.status NOT IN ('won','lost','revealed','booked')", (d9,))
            conn.commit()
    except Exception:
        pass


def reply_to_inbound_sms(pool: ConnectionPool, settings: Settings, *, dest9: str, from_line,
                         inbound_text: str, dry_run: bool = False) -> dict:
    """SHARED inbound-SMS auto-responder for BOTH Lisa lines. Loads the prospect's brief + full thread +
    whether they ALREADY have a booked meeting (pre-booking vs post-booking mode), composes ONE reply with
    Claude under Vysakh's HARD RULES (never reveals the purpose pre-booking; only aims to get them on a quick
    call), then ACTS on the model's intent: a texted time -> parse (emma agreed-time parser, anchored now,
    Melbourne) -> upsert a callback on the right calendar + confirm; opt-out -> full opt-out enforcement;
    not-interested -> soft close (still no purpose reveal); else send the reply. dry_run composes + parses but
    never sends/inserts. Deduped so a duplicate webhook / the evening sweep never double-texts."""
    ensure_tables(pool)
    d9 = _d9(dest9)
    body = (inbound_text or "").strip()
    # If this prospect was sent a no-show recovery text, record that they replied (campaign reporting). The
    # normal responder below then handles the reply human-like and steers to re-booking. Guarded.
    try:
        from . import noshow_recovery as _nsr
        _nsr.mark_reply(pool, d9)
    except Exception:
        pass
    line_tag = _sms_line(settings, from_line)                 # "L1" | "L4" | "L5"
    is_l4 = line_tag == "L4"
    is_l5 = line_tag == "L5"
    bde_name = {"L4": "Lisa4", "L5": "Lisa5"}.get(line_tag, "Lisa")
    created_by = {"L4": "lisa4", "L5": "lisa5"}.get(line_tag, "lisa")
    to_number = _sms_prospect_number(pool, d9)
    result: dict = {"ok": True, "line": line_tag, "dry_run": dry_run, "reply": ""}

    # 0) HARD opt-out keyword (STOP / remove me / wrong number) — enforce, DO NOT reply, no LLM. A bare
    #    "not interested" is deliberately NOT hard here: it flows to the classifier for a soft close + polite
    #    reply (the spec's not_interested path), instead of a silent full-DNC.
    if _is_hard_sms_optout(body):
        if not dry_run:
            _enforce_sms_optout(pool, d9, to_number, body)
        result["optout"] = True
        return result

    # 1) dedupe — never answer the same inbound twice (live path only; a dry_run always composes).
    if not dry_run and _sms_already_answered(pool, d9, body):
        result["skipped"] = "already answered"
        return result

    # 1.5) HUMAN-LIKE DELAY (Vysakh, 2026-08-19): a real person doesn't fire back in 2 seconds. Hold the
    #      auto-reply until the prospect's latest inbound has aged a natural, STABLE per-thread amount
    #      (~1.5-7.5 min). The live webhook runs at age~0 → we defer; the loop's sweep_unanswered_sms
    #      re-invokes us each cycle and actually sends once it's old enough. Hard opt-outs (step 0) already
    #      fired instantly above, so compliance is unaffected. dry_run (CRM preview) never defers.
    if not dry_run:
        try:
            _ar = _fetch(pool, "SELECT extract(epoch from (now()-max(created_at))) age FROM lisa_sms "
                         "WHERE dest9=%s AND direction='inbound'", (d9,))
            age = _ar[0]["age"] if (_ar and _ar[0].get("age") is not None) else 1e9
            delay = 90 + (int(d9[-3:]) % 360) if (d9 and d9[-3:].isdigit()) else 210   # stable 90-449s
            if age < delay:
                result["deferred"] = True
                result["reply_after_s"] = int(delay - age)
                return result
        except Exception:
            pass   # never let the delay gate block a reply on error

    # 2) context: brief (first name only — NEVER the company/pitch in the text), booked state, full thread.
    br = _fetch(pool, "SELECT brief FROM lisa_briefs WHERE dest9=%s", (d9,))
    brief = (br[0]["brief"] if br and br[0].get("brief") else {}) or {}
    name = _first_name(brief.get("prospect_name") or "")
    booked = _has_booked_meeting(pool, d9)
    meeting_at = _booked_meeting_when(pool, settings, d9) if booked else ""
    hist = _fetch(pool, "SELECT direction, body FROM lisa_sms WHERE dest9=%s ORDER BY created_at DESC LIMIT 8", (d9,))
    thread = "\n".join(f"{'Them' if h['direction'] == 'inbound' else 'You'}: {h['body']}" for h in reversed(hist))

    # 3) compose. Line type ONLY sets tone; it is NEVER revealed. The user block deliberately withholds the
    #    company name / finding / pitch so the model cannot leak the purpose pre-booking.
    mode = "POST-BOOKING (a meeting is already booked — you MAY confirm/remind/refer to it)" if booked \
        else "PRE-BOOKING (NO meeting yet — NEVER reveal why we called; only get them on a quick call)"
    line_label = {"L4": "Lisa-4", "L5": "Lisa-5 (growth-audit line — warm, consultative)"}.get(line_tag, "Lisa-1")
    sysp = (SMS_REPLY_RULES +
            f"\n\nLINE: {line_label} — use ONLY to pick tone; never state the line, "
            "a company name, or why you called.\n"
            f"MODE: {mode}.\n"
            "You are texting as 'Lisa'. Reply as ONE SMS. Return via the sms_reply tool.")
    usr = (f"Their first name (optional to use, warm): {name or '(unknown)'}\n"
           + (f"Booked meeting time you may reference: {meeting_at}\n" if (booked and meeting_at) else "")
           + f"\nThread so far (most recent last):\n{thread}\n\nTheir latest text: {body}")
    reply, intent = _claude_sms_compose(settings, pool, system=sysp, user=usr)
    if not isinstance(intent, dict):
        intent = {}
    if not reply:                                   # LLM unavailable → safe, purpose-free fallback
        if booked and meeting_at:
            reply = f"hey{(' ' + name.lower()) if name else ''}, lisa here — all set for {meeting_at.lower()}, just give me a shout if the time needs shifting"
        elif booked:
            reply = f"hey{(' ' + name.lower()) if name else ''}, lisa here — just shout if you need to shift our catch up, otherwise all good"
        else:
            reply = f"hey{(' ' + name.lower()) if name else ''}, it's lisa — tried giving you a buzz earlier. genuinely worth a quick 2 min call, when's good for you to chat?"
    result["reply"] = reply
    result["intent"] = intent

    # 4) ACT on intent.
    if intent.get("opted_out"):                      # model read an opt-out we keyword-missed
        result["optout"] = True
        if not dry_run:
            _enforce_sms_optout(pool, d9, to_number, body)
        return result                                # opted out → no reply

    ct = (intent.get("callback_time_text") or "").strip()
    if intent.get("wants_callback") and ct:
        from .emma import parse_agreed_time
        when = parse_agreed_time(ct, None, tz="Australia/Melbourne")   # anchor = now (Melbourne)
        if when:
            result["callback_at"] = when.isoformat()
            if not dry_run:
                try:
                    _upsert_sms_callback(pool, bde_name, d9, to_number, when, body, created_by)
                except Exception as exc:
                    log.warning("lisa_sms_callback_failed", d9=d9, error=str(exc)[:140])

    if intent.get("not_interested"):
        result["not_interested"] = True
        if not dry_run:
            _mark_sms_not_interested(pool, d9)

    # send the composed reply (a not-interested reply is still a polite close — no purpose reveal). Reply
    # FROM the matching line so the prospect sees the same 'Lisa' they've been dealing with: Lisa-4 from
    # her number, Lisa-5 from the exact Lisa-5 number they texted (falls back to the primary Lisa-5 number
    # when from_line is a bare 'L5' token), Lisa-1 from the SMS-capable set.
    if not dry_run and reply and to_number:
        if is_l4:
            sms_from = _e164_au((list(getattr(settings, "lisa4_numbers", []) or []) or [""])[0])
        elif is_l5:
            l5 = [_e164_au(n) for n in list(getattr(settings, "lisa5_numbers", []) or []) if n]
            raw = _e164_au(from_line) if from_line else ""
            sms_from = raw if raw in l5 else (l5[0] if l5 else "")
        else:
            sms_from = _pick_sms_from(settings, to_number)
        try:
            if _twilio_ready(settings) and sms_from and _send_sms_twilio(settings, to_number, reply, sms_from):
                _log_sms(pool, "outbound", sms_from, to_number, reply, d9)
                result["sent"] = True
        except Exception as exc:
            log.warning("lisa_sms_reply_failed", line=line_tag, error=str(exc)[:150])
    return result


def sweep_unanswered_sms(pool: ConnectionPool, settings: Settings) -> dict:
    """Evening catch-up across ALL Lisa lines (Lisa-1 / Lisa-4 / Lisa-5): any prospect whose LAST message is
    an inbound we never answered gets a reply now, from the correct line. SMS has no call-window restriction,
    so this runs every refresh cycle; the per-thread dedupe inside reply_to_inbound_sms keeps it from racing
    the live webhook."""
    ensure_tables(pool)
    rows = _fetch(pool,
                  "WITH last AS (SELECT dest9, max(created_at) mx FROM lisa_sms WHERE dest9 IS NOT NULL "
                  "              GROUP BY dest9) "
                  "SELECT s.dest9, s.body, s.to_number FROM lisa_sms s "
                  "JOIN last l ON l.dest9=s.dest9 AND l.mx=s.created_at "
                  "WHERE s.direction='inbound' AND s.created_at > now() - interval '2 days' "
                  "  AND NOT EXISTS (SELECT 1 FROM lisa_sms o WHERE o.dest9=s.dest9 AND o.direction='outbound' "
                  "                  AND o.created_at >= s.created_at) "
                  "ORDER BY s.created_at ASC LIMIT 50")
    handled = 0
    for r in rows:
        d9 = r.get("dest9")
        body = r.get("body") or ""
        if not d9 or _is_hard_sms_optout(body):      # a STOP was enforced on receipt; never chase it
            continue
        line = _sms_line(settings, r.get("to_number"))       # 'L1' | 'L4' | 'L5' (covers Lisa-5 numbers too)
        try:
            res = reply_to_inbound_sms(pool, settings, dest9=d9, from_line=line, inbound_text=body)
            if res.get("sent") or res.get("callback_at") or res.get("optout"):
                handled += 1
        except Exception as exc:
            log.warning("lisa_sms_sweep_item_failed", dest9=d9, error=str(exc)[:140])
    return {"unanswered": len(rows), "handled": handled}


def handle_inbound_sms(pool: ConnectionPool, settings: Settings, from_number: str, body: str) -> dict:
    """Lisa-1 inbound-SMS webhook entry point. Capture the reply, then hand off to the shared auto-responder
    (curiosity + get-on-a-call; NEVER reveals the pitch pre-booking)."""
    ensure_tables(pool)
    d9 = _d9(from_number)
    _log_sms(pool, "inbound", from_number, getattr(settings, "lisa_sms_from", ""), body, d9)
    return reply_to_inbound_sms(pool, settings, dest9=d9, from_line="L1", inbound_text=body)


# --------------------------------------------------------------------------- #
# Audit share links — a prospect who asks for a report/proposal gets the EXISTING
# audit (prospect-page Digital Marketing tab engine) at a public, tokenised URL by SMS.
# --------------------------------------------------------------------------- #
def create_audit_share(pool: ConnectionPool, domain: str, name: str | None = None,
                       ticket: int = 2000, dest9: str | None = None) -> str:
    """Mint (or reuse today's) unguessable token that maps to a domain, so the audit can be viewed without a
    login. Returns the token. Idempotent-ish: reuses an existing token for the same domain if one exists."""
    import secrets as _secrets
    ensure_tables(pool)
    dom = (domain or "").strip().lower()
    if not dom:
        raise ValueError("no domain")
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT token FROM lisa_audit_shares WHERE domain=%s ORDER BY created_at DESC LIMIT 1", (dom,))
        r = cur.fetchone()
        if r and r.get("token"):
            return r["token"]
        tok = _secrets.token_urlsafe(9)
        cur.execute("INSERT INTO lisa_audit_shares (token, domain, name, ticket, dest9) VALUES (%s,%s,%s,%s,%s)",
                    (tok, dom, name, int(ticket or 2000), dest9))
        conn.commit()
    return tok


def resolve_audit_share(pool: ConnectionPool, token: str) -> dict | None:
    """Public lookup: token -> {domain, name, ticket}. Bumps the view counter. None if unknown."""
    if not token:
        return None
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT token, domain, name, ticket FROM lisa_audit_shares WHERE token=%s", (token,))
        r = cur.fetchone()
        if not r:
            return None
        cur.execute("UPDATE lisa_audit_shares SET views=views+1 WHERE token=%s", (token,))
        conn.commit()
        return dict(r)


def ensure_full_audit(pool: ConnectionPool, settings: Settings, domain: str) -> dict:
    """FULLY generate a prospect's audit so nothing is blank before we send it: if the paid DataForSEO SEO
    audit hasn't been run for this domain yet, run it now and cache it (same path as the prospect page's
    'Generate audit'). Then assemble + VERIFY the model is real. Returns {ok, ran_seo, model?, error?}."""
    dom = (domain or "").strip().lower()
    if not dom:
        return {"ok": False, "error": "no domain"}
    ran_seo = False
    # 1) run the paid SEO audit if we don't already have it cached
    try:
        have = _fetch(pool, "SELECT (dataforseo->'audit') IS NOT NULL a FROM enrichment WHERE domain=%s", (dom,))
        has_audit = bool(have and have[0].get("a"))
    except Exception:
        has_audit = False
    if not has_audit and getattr(settings, "dataforseo_enabled", False):
        try:
            from .enrichment.dataforseo import DataForSEOClient, build_seo_audit, brand_tokens
            from psycopg.types.json import Json as _Json
            co = _fetch(pool, "SELECT company_name FROM companies WHERE domain=%s LIMIT 1", (dom,))
            company = (co[0].get("company_name") if co else "") or ""
            c = DataForSEOClient(settings)
            try:
                rk = c.ranked_keywords(dom, limit=100)
            finally:
                c.close()
            kws = (rk.get("keywords") or [])[:100]
            audit = build_seo_audit(kws, brands=brand_tokens(dom, company))
            audit["keyword_count_total"] = rk.get("count")
            patch = {"audit": audit, "ranked_kw": kws}
            with pool.connection() as conn, conn.cursor() as cur:
                cur.execute("INSERT INTO enrichment (domain, dataforseo, fetched_at) VALUES (%s,%s,now()) "
                            "ON CONFLICT (domain) DO UPDATE SET dataforseo = "
                            "COALESCE(enrichment.dataforseo,'{}'::jsonb) || %s::jsonb, fetched_at=now()",
                            (dom, _Json(patch), _Json(patch)))
                conn.commit()
            ran_seo = True
        except Exception as exc:
            log.warning("ensure_full_audit_seo_failed", domain=dom, error=str(exc)[:150])
    # 2) assemble + verify the report is real (has a business identity at minimum)
    try:
        from .audit import assemble_audit
        m = assemble_audit(pool, dom)
    except Exception as exc:
        return {"ok": False, "ran_seo": ran_seo, "error": f"assemble failed: {str(exc)[:120]}"}
    if not m or not (m.get("name") or m.get("business")):
        return {"ok": False, "ran_seo": ran_seo, "error": "no audit data for this domain"}
    return {"ok": True, "ran_seo": ran_seo, "model": m}


def send_audit_link(pool: ConnectionPool, settings: Settings, *, domain: str, to_number: str,
                    name: str | None = None, ticket: int = 2000) -> dict:
    """A prospect asked for a report/proposal → FULLY generate + verify their audit, then text them a link to
    it (the existing engine's report) at a public tokenised URL. Backend Twilio SMS. Returns {ok, url}."""
    if not _twilio_ready(settings):
        return {"ok": False, "error": "twilio not configured"}
    dom = (domain or "").strip().lower()
    if not dom:
        return {"ok": False, "error": "no domain on file"}
    if not _is_mobile_au(to_number):                 # landlines can't receive SMS — don't even generate one
        return {"ok": False, "error": "not a mobile number — audit link only sends to mobiles"}
    full = ensure_full_audit(pool, settings, dom)   # fully run + verify before we send anything
    if not full.get("ok"):
        return {"ok": False, "error": full.get("error") or "audit not ready"}
    first = _first_name(name or "")
    tok = create_audit_share(pool, dom, name=name, ticket=ticket, dest9=_d9(to_number))
    base = (getattr(settings, "public_base_url", "") or "").rstrip("/")
    url = f"{base}/r/{tok}"
    body = (f"Hi{(' ' + first) if first else ''}, it's Lisa from DE Group — as promised, here's the quick audit of "
            f"how {dom.split('.')[0].title()} is showing up on Google: {url}\nHappy to walk you through it. — Lisa")
    sms_from = _pick_sms_from(settings, to_number)
    try:
        sent = _send_sms_twilio(settings, to_number, body, sms_from)
    except Exception as exc:
        return {"ok": False, "error": f"sms failed: {str(exc)[:120]}", "url": url}
    if sent:
        _log_sms(pool, "outbound", sms_from, to_number, body, _d9(to_number))
    return {"ok": bool(sent), "url": url, "token": tok}


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


def _write_funnel_call(pool: ConnectionPool, cid: str, dyn: dict, cad: dict, call: dict,
                       *, bde_ext: str = "LISA", bde_name: str = "Lisa") -> None:
    """Mirror a completed AI-agent call into `calls` + `classifications` (provider='retell') so the
    leaderboard / funnel / reports treat her like any BDE. Defaults to Lisa-1; Lisa-4 passes her own
    identity. Isolation to admins is via PRIVATE_BDE_NAMES. Idempotent per call_id."""
    outcome = (cad.get("call_outcome") or "").strip().lower()
    booked = bool(cad.get("meeting_agreed"))
    callback = outcome == "callback_requested" or bool(cad.get("callback_when"))
    answered, reached, co, is_vm = _OUTCOME_MAP.get(outcome, (False, False, "no_answer", False))
    if booked:
        answered, reached, co = True, True, "conversation"
    dur_ms = call.get("duration_ms") or call.get("call_length_ms") or 0
    talk = int((dur_ms or 0) / 1000)
    stage = "p5" if booked else ("p1" if callback else None)
    # REAL full-pitch signal (NOT just "reached"): she got to actually deliver the pitch — i.e. booked, or a
    # genuine conversation that ended in interest/objection/callback, or a >=60s talk with a live decision-
    # maker. Gatekeeper-only, wrong-number, voicemail, no-answer and instant hang-ups do NOT count.
    full_pitch = bool(
        booked
        or (reached and outcome in ("not_interested", "callback_requested"))
        or (reached and talk >= 60 and outcome not in (
            "gatekeeper_only", "wrong_number", "voicemail", "no_answer", "do_not_call")))
    dest = call.get("to_number")
    # Only AUSTRALIAN calls belong in the funnel. A Lisa call to a non-AU number is a TEST call
    # (e.g. dialling our own +91 mobile) — mark it out-of-scope so it never counts anywhere on the
    # dashboard (calls / connected / booking). AU numbers normalise to +61… via _e164_au.
    in_scope = _e164_au(dest).startswith("+61")
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO calls (call_id, bde_extension, bde_name, direction, dest_number, started_at, "
            "  talk_seconds, answered, is_voicemail, has_transcript, recording_present, in_scope, provider, "
            "  fresh_or_followup) VALUES (%s,%s,%s,'outbound',%s,now(),%s,%s,%s,%s,%s,%s,'retell','fresh') "
            "ON CONFLICT (call_id) DO UPDATE SET talk_seconds=EXCLUDED.talk_seconds, answered=EXCLUDED.answered, "
            "  is_voicemail=EXCLUDED.is_voicemail, has_transcript=EXCLUDED.has_transcript, "
            "  recording_present=EXCLUDED.recording_present, in_scope=EXCLUDED.in_scope",
            (cid, bde_ext, bde_name, dest, talk, answered, is_vm, bool(call.get("transcript")),
             bool(call.get("recording_url")), in_scope))
        cur.execute(
            "INSERT INTO classifications (call_id, rpc_connect, full_pitch, is_lead, qualified, meeting_booked, "
            "  call_outcome, booking_status, meeting_datetime, callback_requested, callback_when, pipeline_stage, "
            "  prospect_company, prospect_website, prospect_contact_name, prospect_email, do_not_contact, "
            "  classified_at, model) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,now(),'retell') "
            "ON CONFLICT (call_id) DO UPDATE SET rpc_connect=EXCLUDED.rpc_connect, full_pitch=EXCLUDED.full_pitch, "
            "  is_lead=EXCLUDED.is_lead, meeting_booked=EXCLUDED.meeting_booked, call_outcome=EXCLUDED.call_outcome, "
            "  booking_status=EXCLUDED.booking_status, callback_requested=EXCLUDED.callback_requested, "
            "  pipeline_stage=EXCLUDED.pipeline_stage, do_not_contact=EXCLUDED.do_not_contact",
            # WEBSITE-SALE QUALIFICATION DOCTRINE (Raj, 2026-08-07): a Lisa-4 booking IS a qualified
            # lead — her flow confirms the right party (owner / website decision-maker) before any
            # booking, and website sales don't use marketing BANT. Lisa-1 stays qualified=false here
            # (her BANT verdict comes from the AI classifier pass).
            (cid, reached, full_pitch, (booked or callback),
             bool(booked and str(bde_name or "") in ("Lisa 4", "Lisa4")),
             booked, co, ("firm" if booked else None),
             cad.get("agreed_day_time"), callback, cad.get("callback_when"), stage,
             dyn.get("company_name"), dyn.get("prospect_website"), dyn.get("prospect_name"),
             cad.get("confirmed_email") or dyn.get("prospect_email"), outcome == "do_not_call"))
        # Store the transcript in the shared `transcripts` table too, so the Sales Coach (refresh_playbook,
        # which mines WON/LOST from `transcripts`) also learns from Lisa's OWN calls — not just the humans'.
        tx = call.get("transcript") or ""
        if len(tx) > 200:
            cur.execute(
                "INSERT INTO transcripts (call_id, source, diarized, text, sentiment, summary) "
                "VALUES (%s,'retell',true,%s,%s,%s) ON CONFLICT (call_id) DO UPDATE SET "
                "text=EXCLUDED.text, summary=EXCLUDED.summary",
                (cid, tx, (call.get("call_analysis") or {}).get("user_sentiment"), cad.get("call_summary")))
        conn.commit()


_AI_CLASSIFY_CONTEXT = (
    "[AGENT CONTEXT — READ FIRST] This call was made by 'Lisa', our company's AI sales agent. Lisa-1 calls "
    "as Traffic Radius (Google-Ads/SEO strategy sessions); Lisa-4 calls under DE GROUP, a white-label "
    "web-design brand of the SAME company — for those calls the product IS DE Group's offer: a brand-new "
    "website already built for the prospect, revealed in a 10-15 min screen-share. Judge EVERY stage "
    "against the offering PITCHED ON THIS CALL, never against a different brand: rpc_connect = an owner/"
    "decision-maker was on the phone; full_pitch = the core hook was delivered to them; is_lead = genuine "
    "interest; meeting_booked = the offered meeting (strategy session OR screen-share reveal) was booked. "
    "NEVER mark a stage NO merely because the call wasn't about 'Traffic Radius services'.")


def classify_ai_calls(pool: ConnectionPool, settings: Settings, limit: int = 6) -> dict:
    """Stage-by-stage classification for AI-agent calls — the SAME evidence panel human calls get — with
    brand context injected. Retell's own post-call analysis stays authoritative for the funnel FLAGS
    (re-asserted after the classifier writes its evidence), so a rubric quirk can never un-book a meeting."""
    rows = _fetch(pool,
        "SELECT c.call_id, c.answered, c.is_voicemail, c.dest_number, c.started_at, "
        "  t.text, t.sentiment, t.summary, t.diarized "
        "FROM calls c JOIN transcripts t ON t.call_id=c.call_id "
        "JOIN classifications cl ON cl.call_id=c.call_id "
        "WHERE c.bde_name IN ('Lisa','Lisa 4') AND c.in_scope AND c.answered "
        "  AND cl.evidence IS NULL AND length(t.text) > 250 LIMIT %s", (limit,))
    if not rows:
        return {"classified": 0}
    from .classify.classifier import Classifier, upsert_classification
    clf = Classifier(settings)
    l4nums = set(getattr(settings, "lisa4_numbers", []) or [])
    agent_nums = l4nums | set(getattr(settings, "lisa_numbers", []) or [])
    n = 0
    for row in rows:
        try:
            rec = clf.classify_one(dict(row), {"text": row.get("text"), "sentiment": row.get("sentiment"),
                                               "summary": row.get("summary")}, memory=_AI_CLASSIFY_CONTEXT)
            upsert_classification(pool, rec)
            lr = _fetch(pool, "SELECT from_number, to_number, call_outcome, meeting_agreed, agreed_day_time, "
                        "callback_when, call_summary, duration_ms, recording_url, brief FROM lisa_calls "
                        "WHERE call_id=%s", (row["call_id"],))
            if lr:
                r = lr[0]
                inbound = (r["to_number"] or "") in agent_nums
                cad = {"call_outcome": r["call_outcome"] or "", "meeting_agreed": r["meeting_agreed"],
                       "agreed_day_time": r["agreed_day_time"], "callback_when": r["callback_when"],
                       "call_summary": r["call_summary"]}
                call = {"to_number": (r["from_number"] if inbound else r["to_number"]),
                        "duration_ms": r["duration_ms"], "transcript": None,
                        "recording_url": r["recording_url"]}
                try:
                    dyn = json.loads(r["brief"]) if isinstance(r["brief"], str) else (r["brief"] or {})
                except Exception:
                    dyn = {}
                is_l4 = (r["from_number"] in l4nums) or ((r["to_number"] or "") in l4nums)
                if is_l4:
                    _write_funnel_call(pool, row["call_id"], dyn, cad, call, bde_ext="LISA4", bde_name="Lisa 4")
                else:
                    _write_funnel_call(pool, row["call_id"], dyn, cad, call)
            n += 1
        except Exception as exc:
            log.warning("classify_ai_call_failed", call_id=row.get("call_id"), error=str(exc)[:120])
    return {"classified": n}


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
        # MOBILE-ONLY (Raj, 2026-08-10): never schedule a cold retry to a landline (a main line reaches a
        # gatekeeper, never the owner). Prospect-requested callbacks are handled above and stay exempt.
        if (dest9 or "")[:1] != "4":
            return
        attempts = _fetch(pool, "SELECT count(*) n FROM calls WHERE provider='retell' AND "
                          "right(regexp_replace(COALESCE(dest_number,''),'[^0-9]','','g'),9)=%s", (dest9,))[0]["n"]
        if attempts >= int(getattr(settings, "lisa_retry_max_attempts", 4)):
            return
        cad_days = int(getattr(settings, "lisa_retry_cadence_days", 3))
        now_l = datetime.now(ZoneInfo(tz))
        wstart = int(getattr(settings, "lisa_call_window_start", 9))
        wend = int(getattr(settings, "lisa_call_window_end", 17))
        dt_h = int(getattr(settings, "lisa_double_tap_hours", 2))
        # DOUBLE-TAP: a no-answer gets ONE quick same-day retry (+dt_h) if still within business hours,
        # before falling back to the multi-day cadence — mirrors how a human re-tries a missed call.
        cand = now_l + timedelta(hours=dt_h)
        # VOICEMAIL LEVER (Raj, 2026-08-10): a cold mobile that goes to voicemail is usually just busy —
        # so voicemail now ALSO gets a same-day double-tap, then next-business-day retries at a VARIED hour
        # (someone who ignores a 9am unknown number often answers at 2pm) instead of one 3-day-out attempt.
        _vary = [9, 13, 15, 11]
        if outcome in ("no_answer", "voicemail") and attempts <= 1 and dt_h > 0 and cand.weekday() < 5 and wstart <= cand.hour < wend:
            when, label = cand, "double-tap"
        elif outcome in ("no_answer", "voicemail"):
            _hr = min(max(_vary[min(attempts, len(_vary) - 1)], wstart), wend - 1)
            when, label = _future_biz(now_l + timedelta(days=1), _hr), "retry"
        else:
            when, label = _future_biz(now_l + timedelta(days=cad_days), wstart), "retry"
        create_event(pool, bde_name="Lisa", type="retry", title=f"🔄 Lisa {label} ({attempts+1}): {who}",
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
    """Reserve Lisa's EXCLUSIVE pool of FRESH GAds prospects — businesses NEVER contacted by anyone: no
    human-BDE (3cx/aircall) call, no prior Lisa call, and not in the human pipeline — so Lisa never
    overlaps a prospect a human BDE is working. SMB-only (national chains / >$100M excluded),
    highest-advertising first. Purges any entry that has SINCE been contacted so the pool stays fresh.
    Idempotent."""
    ensure_tables(pool)
    size = int(getattr(settings, "lisa_pool_size", 500))
    # keep the pool FRESH + non-overlapping: drop any entry whose number has since been dialed by a human
    # BDE or by Lisa, or that entered the human pipeline.
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "DELETE FROM lisa_pool lp WHERE "
            "  EXISTS (SELECT 1 FROM calls c WHERE right(regexp_replace(COALESCE(c.dest_number,''),'[^0-9]','','g'),9)=lp.dest9) "
            "  OR EXISTS (SELECT 1 FROM lisa_calls lc WHERE lc.dest9=lp.dest9) "
            "  OR EXISTS (SELECT 1 FROM prospect_pipeline pp WHERE pp.dest9=lp.dest9) "
            "  OR lp.domain ~* '\\.(gov|edu|asn|org)\\.au$'")   # non-commercial: gov/uni/association/foundation
        conn.commit()
    have = _fetch(pool, "SELECT count(*) c FROM lisa_pool")[0]["c"]
    if have >= size:
        return {"reserved": 0, "total": have, "note": "pool at size (fresh)"}
    need = size - have
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO lisa_pool (dest9, dest_number, domain, company, phone, priority) "
            "SELECT d9, num, dom, company, num, prio FROM ("
            "  SELECT DISTINCT ON (d9) d9, num, dom, company, prio FROM ("
            # MOBILE-FIRST (Raj, 2026-08-10): dial the RESOLVED OWNER MOBILE, not the company's main line.
            # Lisa-1's advertiser universe lists landlines (switchboards → gatekeepers), so we join the
            # DM resolver (lisa_dm) and take dm_phone where it's a mobile — the owner who can actually book.
            "    SELECT right(regexp_replace(COALESCE(dm.dm_phone,''),'[^0-9]','','g'),9) d9, dm.dm_phone num, "
            "           co.domain dom, co.company_name company, "
            "           COALESCE((e.dataforseo->'ads'->>'count')::int,0) prio "
            "    FROM companies co JOIN enrichment e ON e.domain=co.domain "
            "         JOIN lisa_dm dm ON dm.domain=co.domain "
            "    WHERE (e.dataforseo->>'running_google_ads')='true' AND co.source='raghav' "
            "      AND dm.dm_is_mobile IS TRUE AND NULLIF(dm.dm_phone,'') IS NOT NULL "
            "      AND co.revenue_musd BETWEEN 2 AND 50 "   # $2M-$50M only: excludes tiny/one-man-shops, null-revenue, and big companies
            "      AND co.domain !~* '\\.(gov|edu|asn|org)\\.au$' "
            "      AND (SELECT count(*) FROM companies c3 WHERE c3.domain=co.domain) <= 8 "
            "  ) base "
            "  WHERE length(d9)=9 AND left(d9,1) = '4' "
            "    AND d9 NOT IN (SELECT dest9 FROM lisa_pool) "
            "    AND NOT EXISTS (SELECT 1 FROM calls c WHERE right(regexp_replace(COALESCE(c.dest_number,''),'[^0-9]','','g'),9)=base.d9) "
            "    AND NOT EXISTS (SELECT 1 FROM lisa_calls lc WHERE lc.dest9=base.d9) "
            "    AND NOT EXISTS (SELECT 1 FROM prospect_pipeline pp WHERE pp.dest9=base.d9) "
            "  ORDER BY d9, prio DESC NULLS LAST "
            ") dedup ORDER BY prio DESC NULLS LAST "
            "LIMIT %s ON CONFLICT (dest9) DO NOTHING", (need,))
        got = cur.rowcount
        conn.commit()
    return {"reserved": got, "total": have + got, "fresh": True}


def schedule_lisa_fresh(pool: ConnectionPool, settings: Settings) -> dict:
    """Put Lisa's reserved-pool prospects on HER calendar as fresh_call events, filling each working day up
    to lisa_daily_target, staggered across the call window. Idempotent; only schedules pool prospects not
    already on her calendar or already called by her."""
    ensure_tables(pool)
    tz = settings.tz
    # CLEANUP: cancel any pending fresh_call whose prospect has since been called (by Lisa OR a human BDE).
    # A deadlock/crash between placing a call and marking its event 'done' can leave a stale pending event
    # that would otherwise RE-DIAL an already-called prospect (this caused the same company being dialled
    # 3× today). Runs every cycle so it can never re-accumulate.
    try:
        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE calendar_events e SET status='cancelled' "
                "WHERE e.bde_name='Lisa' AND e.status='pending' AND e.type='fresh_call' AND ("
                "  EXISTS (SELECT 1 FROM lisa_calls lc WHERE lc.dest9="
                "    right(regexp_replace(COALESCE(e.dest_number,''),'[^0-9]','','g'),9)) "
                "  OR EXISTS (SELECT 1 FROM calls c WHERE "
                "    right(regexp_replace(COALESCE(c.dest_number,''),'[^0-9]','','g'),9)="
                "    right(regexp_replace(COALESCE(e.dest_number,''),'[^0-9]','','g'),9)))")
            conn.commit()
    except Exception as exc:
        log.warning("lisa_stale_event_cleanup_failed", error=str(exc)[:140])
    cap = int(getattr(settings, "lisa_daily_target", 50))
    wstart = int(getattr(settings, "lisa_call_window_start", 9))
    wsmin = int(getattr(settings, "lisa_call_window_start_min", 0))
    wend = int(getattr(settings, "lisa_call_window_end", 17))
    rows = _fetch(pool,
        "SELECT lp.dest9, lp.dest_number, lp.company FROM lisa_pool lp "
        "WHERE NOT EXISTS (SELECT 1 FROM calendar_events e WHERE e.bde_name='Lisa' AND e.status IN ('pending','done') "
        "   AND right(regexp_replace(COALESCE(e.dest_number,''),'[^0-9]','','g'),9)=lp.dest9) "
        "  AND NOT EXISTS (SELECT 1 FROM calls c WHERE "
        "   right(regexp_replace(COALESCE(c.dest_number,''),'[^0-9]','','g'),9)=lp.dest9) "
        "  AND NOT EXISTS (SELECT 1 FROM lisa_calls lc WHERE lc.dest9=lp.dest9) "
        "  AND left(lp.dest9,1) = '4' "   # MOBILE-ONLY guard: never schedule a landline onto Lisa's calendar
        # Head-of-Sales value order: highest-spend advertisers (most live ad creatives) first.
        "ORDER BY lp.priority DESC NULLS LAST, lp.reserved_at")
    existing = {r["d"]: r["n"] for r in _fetch(pool,
        "SELECT (start_at AT TIME ZONE %s)::date d, count(*) n FROM calendar_events "
        "WHERE bde_name='Lisa' AND status='pending' AND type='fresh_call' GROUP BY 1", (tz,))}
    now = datetime.now(ZoneInfo(tz))
    day = now.date() + (timedelta(days=1) if now.hour >= wend else timedelta(0))
    open_min = wstart * 60 + wsmin              # exact window open in minutes-of-day (e.g. 8:30 => 510)
    span_min = max(1, wend * 60 - open_min)     # minutes from open to close, to spread the day's calls across
    to_insert = []
    for r in rows:
        while day.weekday() >= 5 or existing.get(day, 0) >= cap:
            if day.weekday() >= 5:
                day += timedelta(days=1); continue
            day += timedelta(days=1)
        used = existing.get(day, 0)
        t = min(open_min + int(used * span_min / max(1, cap)), wend * 60 - 1)   # minute-of-day for this slot
        when = datetime(day.year, day.month, day.day, t // 60, t % 60, tzinfo=ZoneInfo(tz))
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


def sync_reveal_calls(pool: ConnectionPool, settings: Settings) -> dict:
    """AUTO reveal-tracking (Raj, 2026-08-10): when a website-reveal closer (e.g. Immanule Manoj) has an
    ANSWERED, substantive (>=2min) call to a BOOKED reveal prospect, flip that prospect's booked_crm row to
    'revealed' and log the closer/date/outcome — so reveal meetings self-track in the CRM with no manual
    step. Idempotent: transitions each prospect to 'revealed' once; never overwrites 'won'/'lost' or churns."""
    ensure_tables(pool)
    closers = list(getattr(settings, "reveal_closers", []) or [])
    if not closers:
        return {"skipped": "no reveal closers configured"}
    try:
        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "WITH rc AS ("
                "  SELECT DISTINCT ON (d9) d9, bde_name, t, mins, outcome FROM ("
                "    SELECT right(regexp_replace(COALESCE(c.dest_number,''),'[^0-9]','','g'),9) d9, "
                "           c.bde_name, (c.started_at AT TIME ZONE %s) t, "
                "           round(COALESCE(c.talk_seconds,0)/60.0)::int mins, cl.call_outcome outcome, c.started_at "
                "    FROM calls c LEFT JOIN classifications cl ON cl.call_id=c.call_id "
                "    WHERE c.bde_name = ANY(%s) AND c.answered AND COALESCE(c.talk_seconds,0) >= 120 "
                "      AND c.started_at > now() - interval '10 days' "
                "      AND EXISTS (SELECT 1 FROM calendar_events e WHERE e.type='reveal' "
                "        AND right(regexp_replace(COALESCE(e.dest_number,''),'[^0-9]','','g'),9) = "
                "            right(regexp_replace(COALESCE(c.dest_number,''),'[^0-9]','','g'),9)) "
                "  ) s ORDER BY d9, started_at DESC) "
                "INSERT INTO booked_crm (dest9, status, note, updated_by, updated_at) "
                "SELECT rc.d9, 'revealed', "
                "  left('auto: reveal call by '||rc.bde_name||' '||to_char(rc.t,'Dy DD Mon HH24:MI')||"
                "       ' ('||rc.mins||'min'||COALESCE(', '||rc.outcome,'')||')', 400), "
                "  'auto-reveal', now() FROM rc "
                "ON CONFLICT (dest9) DO UPDATE SET status='revealed', note=EXCLUDED.note, "
                "  updated_by='auto-reveal', updated_at=now() "
                "WHERE booked_crm.status NOT IN ('won','lost','revealed')",
                (settings.tz, closers))
            n = cur.rowcount
            conn.commit()
        if n:
            log.info("sync_reveal_calls", revealed=n)
        return {"revealed": n}
    except Exception as exc:
        log.warning("sync_reveal_calls_failed", error=str(exc)[:160])
        return {"error": str(exc)[:160]}


def run_lisa_autodial(pool: ConnectionPool, settings: Settings) -> dict:
    """GATED daily dialer. When lisa_autodial_enabled, dial Lisa's DUE calendar events (fresh/retry/
    callback) within the business-hours window, up to the daily target, a few at a time. Marks each dialed
    event 'done'. No call fires unless lisa_autodial_enabled is true."""
    ensure_tables(pool)
    if not get_autodial_state(pool, settings):          # console toggle (DB) wins; env is the fallback default
        return {"skipped": "autodial disabled"}
    if not getattr(settings, "lisa_enabled", False):
        return {"skipped": "lisa disabled"}
    tz = settings.tz
    now = datetime.now(ZoneInfo(tz))
    wstart = int(getattr(settings, "lisa_call_window_start", 9))
    wsmin = int(getattr(settings, "lisa_call_window_start_min", 0))
    wend = int(getattr(settings, "lisa_call_window_end", 17))
    open_min = wstart * 60 + wsmin                       # exact window open (e.g. 8:30 => 510)
    now_min = now.hour * 60 + now.minute
    if now.weekday() >= 5 or not (open_min <= now_min < wend * 60):
        return {"skipped": "outside call window"}
    # COUNT ONLY LISA-1's OWN CALLS (Raj, 2026-08-10). lisa_calls holds BOTH AI lines, so an unfiltered
    # count let Lisa-4's ~260/day eat Lisa-1's daily budget and stall her with 140+ calls still due.
    # Mirror Lisa-4's own-number gate (lisa4.py): prefer her registered caller IDs; if unset, count
    # everything that ISN'T Lisa-4 (the two AI lines are the only outbound sources in lisa_calls).
    _l1 = list(getattr(settings, "lisa_numbers", []) or [])
    if _l1:
        placed_today = _fetch(pool, "SELECT count(*) n FROM lisa_calls WHERE from_number = ANY(%s) "
                              "AND (created_at AT TIME ZONE %s)::date = (now() AT TIME ZONE %s)::date",
                              (_l1, tz, tz))[0]["n"]
    else:
        placed_today = _fetch(pool, "SELECT count(*) n FROM lisa_calls WHERE "
                              "(created_at AT TIME ZONE %s)::date = (now() AT TIME ZONE %s)::date "
                              "AND right(regexp_replace(COALESCE(from_number,''),'[^0-9]','','g'),9) <> ALL("
                              "  SELECT right(regexp_replace(x,'[^0-9]','','g'),9) FROM unnest(%s::text[]) x)",
                              (tz, tz, list(getattr(settings, "lisa4_numbers", []) or [])))[0]["n"]
    remaining = int(getattr(settings, "lisa_daily_target", 50)) - int(placed_today or 0)
    if remaining <= 0:
        return {"skipped": "daily target reached", "placed_today": placed_today}
    # NATURAL PACING — dial like a human: ONE call at a time, evenly spread so ~daily_target calls fill the
    # whole business-hours window (no concurrency, no fast bursts). The gap = window / daily_target
    # (e.g. 8h / 50 ≈ 9.6 min between calls); overridable via LISA_MIN_CALL_GAP_SECONDS.
    # PIPELINE PACING — ONE call at a time, but CONTINUOUS: dial the next only when NO Lisa call is still
    # in-flight, so it flows the moment each call ends (no artificial idle). A small floor gap prevents
    # hammering; the 8-min cap on "ongoing" prevents a stuck/mis-webhooked call from stalling the queue.
    # SCOPE TO LISA-1's OWN LINE (Raj, 2026-08-12): lisa_calls holds BOTH AI lines, so an unscoped
    # 'ongoing' count let Lisa-4 (on a call almost continuously) trip Lisa-1's in-flight guard every
    # cycle — starving her to ~2 dials/day. Count only HER own line's in-flight, like placed_today.
    if _l1:
        inflight = _fetch(pool, "SELECT count(*) n FROM lisa_calls WHERE status='ongoing' "
                          "AND from_number = ANY(%s) AND created_at > now() - interval '8 minutes'",
                          (_l1,))[0]["n"]
    else:
        inflight = _fetch(pool, "SELECT count(*) n FROM lisa_calls WHERE status='ongoing' "
                          "AND created_at > now() - interval '8 minutes' "
                          "AND right(regexp_replace(COALESCE(from_number,''),'[^0-9]','','g'),9) <> ALL(%s)",
                          ([re.sub(r'[^0-9]','',str(x))[-9:] for x in (list(getattr(settings,'lisa4_numbers',[]) or []))],))[0]["n"]
    if inflight:
        return {"skipped": "call in-flight", "inflight": inflight, "placed_today": placed_today}
    floor = int(getattr(settings, "lisa_min_call_gap_seconds", 0)) or 20
    since = _fetch(pool, "SELECT extract(epoch from (now() - max(created_at))) s FROM lisa_calls "
                   "WHERE (created_at AT TIME ZONE %s)::date=(now() AT TIME ZONE %s)::date", (tz, tz))[0]["s"]
    if since is not None and since < floor:
        return {"skipped": "floor gap", "wait_s": int(floor - since), "placed_today": placed_today}
    # look at a few due candidates but place AT MOST ONE this cycle (skipping any outside its local hours).
    # WARM FIRST: a due prospect CALLBACK (they explicitly asked us to call back) and already-reached
    # follow-ups jump ahead of cold fresh calls, then retries — so a promised callback fires at its time
    # instead of sitting behind a large fresh backlog. Within a tier, earliest promised time first.
    # SELF-DIAL GUARD numbers = Lisa's OWN caller IDs only (not prospects who rang us back). Built from
    # settings; kept dynamic by also catching any number used as from_number on an OUTBOUND call
    # (to_number is NOT one of our lines) — the divert-bridge case — while NEVER excluding inbound
    # callers (to_number IS one of our lines) so warm prospect callbacks stay dialable. (Raj, 2026-08-12)
    _own9 = [re.sub(r"[^0-9]", "", str(x))[-9:] for x in
             (list(getattr(settings, "lisa_numbers", []) or []) + list(getattr(settings, "lisa4_numbers", []) or [])) if x]
    due = _fetch(pool,
        "SELECT id, dest_number, right(regexp_replace(COALESCE(dest_number,''),'[^0-9]','','g'),9) d9 "
        "FROM calendar_events WHERE bde_name='Lisa' AND status='pending' "
        "  AND type IN ('fresh_call','retry','callback','reached_call') AND start_at <= now() "
        # MOBILE-ONLY (Raj, 2026-08-10): never dial a landline for cold fresh/retry — a business main line
        # reaches a receptionist, never the owner who can book. Prospect-REQUESTED callbacks/reached are
        # exempt (they gave us that number and expect our call).
        "  AND (type IN ('callback','reached_call') "
        "       OR left(right(regexp_replace(COALESCE(dest_number,''),'[^0-9]','','g'),9),1)='4') "
        # SELF-DIAL GUARD (Raj, 2026-08-10; fixed 08-12): NEVER dial a number Lisa herself calls FROM — a
        # carrier busy-divert twice bridged two concurrent Lisa calls into each other and she 'booked'
        # herself. Exclude only our OWN outbound caller-IDs (to_number is NOT one of our lines), so a
        # prospect who rang us back (inbound: to_number IS our line) is NOT wrongly suppressed.
        "  AND right(regexp_replace(COALESCE(dest_number,''),'[^0-9]','','g'),9) NOT IN "
        "    (SELECT DISTINCT right(regexp_replace(from_number,'[^0-9]','','g'),9) FROM lisa_calls "
        "     WHERE COALESCE(from_number,'')<>'' AND created_at > now() - interval '45 days' "
        "       AND right(regexp_replace(COALESCE(to_number,''),'[^0-9]','','g'),9) <> ALL(%s)) "
        "ORDER BY CASE WHEN type IN ('callback','reached_call') THEN 0 WHEN type='retry' THEN 1 ELSE 2 END, "
        "         start_at LIMIT 8", (_own9,))
    if not due:
        # SELF-FEEDING QUEUE: nothing due but we're inside the window and under target — promote the next
        # scheduled fresh call instead of idling (the day-spread stagger otherwise starves the dialer).
        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE calendar_events SET start_at=now() WHERE id = ("
                "  SELECT id FROM calendar_events WHERE bde_name='Lisa' AND status='pending' "
                "    AND type='fresh_call' AND start_at > now() "
                "    AND left(right(regexp_replace(COALESCE(dest_number,''),'[^0-9]','','g'),9),1)='4' "  # mobile-only
                "    ORDER BY start_at LIMIT 1)")
            conn.commit()
        due = _fetch(pool,
            "SELECT id, dest_number, right(regexp_replace(COALESCE(dest_number,''),'[^0-9]','','g'),9) d9 "
            "FROM calendar_events WHERE bde_name='Lisa' AND status='pending' "
            "  AND type IN ('fresh_call','retry','callback','reached_call') AND start_at <= now() "
            "  AND (type IN ('callback','reached_call') "
            "       OR left(right(regexp_replace(COALESCE(dest_number,''),'[^0-9]','','g'),9),1)='4') "
            "  AND right(regexp_replace(COALESCE(dest_number,''),'[^0-9]','','g'),9) NOT IN "  # self-dial guard (own IDs only)
            "    (SELECT DISTINCT right(regexp_replace(from_number,'[^0-9]','','g'),9) FROM lisa_calls "
            "     WHERE COALESCE(from_number,'')<>'' AND created_at > now() - interval '45 days' "
            "       AND right(regexp_replace(COALESCE(to_number,''),'[^0-9]','','g'),9) <> ALL(%s)) LIMIT 1",
            (_own9,))
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
            break   # ONE call per cycle — natural, human-like spacing
        # PERMANENT failures (big-company guard, invalid number) must CANCEL the event — otherwise it sits
        # at the head of the due queue and the dialer retries it every cycle forever (wedged the floor for
        # 20+ min on a too_big prospect). Transient errors (Retell API hiccup) stay pending for retry.
        err = (r.get("error") or "")
        transient = any(t in err.lower() for t in ("http 5", "timed out", "timeout", "concurrency", "temporarily"))
        if err and not transient:
            with pool.connection() as conn, conn.cursor() as cur:
                cur.execute("UPDATE calendar_events SET status='cancelled', notes=COALESCE(notes,'')||' · auto-cancelled: '||%s "
                            "WHERE id=%s", (err[:120], e["id"]))
                conn.commit()
            log.info("lisa_autodial_event_cancelled", event_id=e["id"], reason=err[:80])
            continue
    stats = {"dialed": dialed, "candidates": len(due), "skipped_tz": skipped_tz,
             "placed_today": placed_today, "floor_s": floor}
    log.info("lisa_autodial", **stats)
    return stats


# --------------------------------------------------------------------------- #
# AI Sales Coach / Trainer — learn from WON (do) + LOST (don't); QA each Lisa call
# --------------------------------------------------------------------------- #
# gpt-4o pricing (USD cents per token): $2.50 / 1M input, $10 / 1M output.
_LLM_RATE = {"in": 250 / 1_000_000, "out": 1000 / 1_000_000}


def _llm_json(settings: Settings, system: str, user: str, model: str | None = None,
              pool: ConnectionPool | None = None, purpose: str = "") -> dict:
    """One JSON-returning LLM call (reuses the classifier's OpenAI key/model). Records token cost against
    the given `purpose` (for the cost page) when a pool is passed. Returns {} on failure."""
    try:
        from openai import OpenAI
        client = OpenAI(api_key=getattr(settings, "llm_api_key", ""))
        m = model or getattr(settings, "llm_model_strong", None) or "gpt-4o"
        r = client.chat.completions.create(
            model=m, temperature=0.2, response_format={"type": "json_object"},
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user[:24000]}])
        if pool is not None and getattr(r, "usage", None):
            pt = r.usage.prompt_tokens or 0
            ct = r.usage.completion_tokens or 0
            cost = round(pt * _LLM_RATE["in"] + ct * _LLM_RATE["out"], 4)
            try:
                with pool.connection() as conn, conn.cursor() as cur:
                    cur.execute("INSERT INTO lisa_llm_usage (purpose, model, prompt_tokens, completion_tokens, cost_cents) "
                                "VALUES (%s,%s,%s,%s,%s)", (purpose or "lisa", m, pt, ct, cost))
                    conn.commit()
            except Exception as exc:
                log.warning("lisa_llm_usage_log_failed", error=str(exc)[:120])
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
    pb = _llm_json(settings, sys, usr, pool=pool, purpose="coach")
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


# --------------------------------------------------------------------------- #
# Per-stage dedicated coaches (opener / gatekeeper / pitch / objection / close)
# --------------------------------------------------------------------------- #
# (stage key, coach title, what it owns, stage-conversion benchmark % toward 5 qualified/day)
LISA_STAGES = [
    ("opener", "Opener Coach", "the first 20 seconds — a short human curiosity open, permission, not sounding scripted", 50),
    ("gatekeeper", "Gatekeeper Coach", "getting past reception to the decision-maker without sounding like a marketing pitch", 55),
    ("pitch", "Pitch Coach", "delivering the insight/hook so the decision-maker leans in", 45),
    ("objection", "Objection Handler", "handling 'we have an agency / not interested / no time / just email me'", 40),
    ("close", "Closing Coach", "agreeing a concrete time and qualifying (authority, need, timeline)", 25),
]


def refresh_stage_coaches(pool: ConnectionPool, settings: Settings, *, force: bool = False) -> dict:
    """Give EACH funnel stage a dedicated coach. One LLM pass over WON/LOST calls distils stage-specific
    do/avoid + a best line for opener, gatekeeper, pitch, objection and close. Throttled ~20h."""
    ensure_tables(pool)
    if not force:
        last = _fetch(pool, "SELECT max(built_at) b FROM lisa_stage_coach")
        if last and last[0].get("b"):
            from datetime import datetime, timezone
            if (datetime.now(timezone.utc) - last[0]["b"]).total_seconds() / 3600 < 20:
                return {"skipped": "stage coaches fresh"}
    won = _fetch(pool, "SELECT left(tr.text,2400) t FROM calls c JOIN classifications cl ON cl.call_id=c.call_id "
        "JOIN transcripts tr ON tr.call_id=c.call_id WHERE c.in_scope AND cl.rpc_connect AND cl.meeting_booked "
        "AND length(COALESCE(tr.text,''))>500 ORDER BY c.started_at DESC LIMIT 6")
    lost = _fetch(pool, "SELECT left(tr.text,2400) t FROM calls c JOIN classifications cl ON cl.call_id=c.call_id "
        "JOIN transcripts tr ON tr.call_id=c.call_id WHERE c.in_scope AND cl.rpc_connect AND NOT cl.meeting_booked "
        "AND COALESCE(cl.call_outcome,'')='conversation' AND length(COALESCE(tr.text,''))>500 ORDER BY c.started_at DESC LIMIT 6")
    if not won and not lost:
        return {"skipped": "no calls to learn from"}
    sys = ("You are the head coaching team for an Australian appointment-setting AI cold-caller (Lisa, brand "
           "'DE Group', books a free strategy session). From WON transcripts (meeting booked) and LOST ones "
           "(reached the decision-maker, no booking), produce DEDICATED coaching for each funnel stage. Return "
           "STRICT JSON with EXACTLY these keys: opener, gatekeeper, pitch, objection, close. Each value = "
           "{\"do\":[up to 4 short concrete rules], \"avoid\":[up to 4], \"best_line\":\"one spoken line\"}. "
           "objection.best_line must be the best one-sentence rebuttal to 'we already have an agency'. Keep "
           "every string short, spoken, AU tone, no fluff.")
    usr = ("=== WON ===\n" + "\n---\n".join(w["t"] for w in won) + "\n\n=== LOST ===\n" + "\n---\n".join(l["t"] for l in lost))
    out = _llm_json(settings, sys, usr, pool=pool, purpose="stage_coach")
    if not out:
        return {"skipped": "llm returned nothing"}
    n = 0
    with pool.connection() as conn, conn.cursor() as cur:
        for stage, title, _desc, bm in LISA_STAGES:
            g = out.get(stage) or {}
            if not (g.get("do") or g.get("avoid") or g.get("best_line")):
                continue
            cur.execute("INSERT INTO lisa_stage_coach (stage,title,guidance,benchmark,built_at) VALUES (%s,%s,%s,%s,now()) "
                        "ON CONFLICT (stage) DO UPDATE SET title=EXCLUDED.title, guidance=EXCLUDED.guidance, "
                        "benchmark=EXCLUDED.benchmark, built_at=now()", (stage, title, json.dumps(g), bm))
            n += 1
        conn.commit()
    return {"stages_coached": n, "won": len(won), "lost": len(lost)}


_AUTO_START = "<<<AUTO-COACH ADJUSTMENTS (auto-managed by the AI Sales Coach from live QA — do not hand-edit)>>>"
_AUTO_END = "<<<END AUTO-COACH ADJUSTMENTS>>>"
# safety net: the auto-coach may NEVER weaken these non-negotiables. If a generated line touches any of
# these, the whole update is rejected (the AI can improve tactics, never the brand/honesty/compliance).
_AUTO_BLOCKLIST = ["traffic radius", "digital expo", "don't admit", "do not admit", "never admit",
                   "never say you're an ai", "not an ai", "ignore stop", "keep pitching after", "don't honour",
                   "do not honour", "don't honor", "do not honor", "fabricate", "make up", "pretend to be human",
                   "say you are human", "claim to be human", "do not stop"]


def refresh_coach_adjustments(pool: ConnectionPool, settings: Settings, *, force: bool = False) -> dict:
    """AUTONOMOUS self-improvement: the AI Sales Coach reads the RECURRING problems QA flagged, writes concrete
    coaching fixes itself, safety-checks them (can never weaken brand / AI-honesty / stop-DNC), applies them to
    the LIVE agent (a delimited, bounded block — never the core rules), republishes, and logs it. Throttled."""
    ensure_tables(pool)
    if not force:
        last = _fetch(pool, "SELECT max(created_at) b FROM lisa_auto_adjustments")
        if last and last[0].get("b"):
            from datetime import datetime, timezone
            if (datetime.now(timezone.utc) - last[0]["b"]).total_seconds() / 3600 < 4:
                return {"skipped": "adjustments fresh (<4h)"}
    _F = ["gatekeeper_leak", "pushed_wrong_person", "brand_slip", "robotic_name", "talked_over_or_repeated",
          "over_questioned", "generic_no_data", "monologued", "answered_ai_late", "folded_on_reflex_no",
          "sounded_polished_not_human"]
    fl = _fetch(pool, "SELECT " + ", ".join(f"count(*) FILTER (WHERE scores->>'{f}'='true') {f}" for f in _F) +
                " FROM lisa_call_reviews WHERE reviewed_at >= now() - interval '7 days'")
    flags = {k: v for k, v in (dict(fl[0]) if fl else {}).items() if v and v >= 2}
    if not flags:
        return {"skipped": "no recurring issues to fix"}
    sysp = ("You are the AI Sales Coach for an Australian appointment-setting phone agent (Lisa, brand "
            "'DE Group'). Based on the RECURRING problems QA flagged on recent calls, write concise, concrete "
            "coaching adjustments that will fix them on the next calls. Additive TACTICAL guidance only, short "
            "spoken bullets, warm AU tone. You MUST NOT change or weaken any non-negotiable: the brand is "
            "always 'DE Group' (never 'Traffic Radius'/'Digital Expo'); she admits she's an AI if asked; she "
            "always honours 'stop'/DNC; she never fabricates. Return STRICT JSON {\"adjustments\": [up to 8 short strings]}.")
    usr = ("Recurring QA flags (count, last 7d): " + json.dumps(flags) + "\nExamples of the labels: "
           "generic_no_data=she opened generic instead of using the prospect's real data; monologued=too-long "
           "scripted turns; folded_on_reflex_no=gave up on a quick 'no' without a smart re-engage.")
    out = _llm_json(settings, sysp, usr, pool=pool, purpose="auto_coach")
    adj = [a.strip() for a in (out.get("adjustments") or []) if isinstance(a, str) and a.strip()][:8]
    if not adj:
        return {"skipped": "coach produced nothing"}
    block = "\n".join(f"- {a}" for a in adj)
    if any(b in block.lower() for b in _AUTO_BLOCKLIST):
        log.warning("auto_coach_blocked_unsafe", preview=block[:160])
        return {"skipped": "unsafe content blocked (touched a non-negotiable)"}
    # PROPOSE it (don't auto-apply): store as a pending recommendation for one-click approval on the console.
    # Skip if an identical recommendation is already pending.
    pend = _fetch(pool, "SELECT adjustments FROM lisa_auto_adjustments WHERE applied=false")
    if any((p.get("adjustments") or []) == adj for p in pend):
        return {"skipped": "same recommendation already pending"}
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute("INSERT INTO lisa_auto_adjustments (adjustments, from_flags, applied) VALUES (%s,%s,false)",
                    (json.dumps(adj), json.dumps(flags)))
        conn.commit()
    log.info("auto_coach_proposed", n=len(adj), flags=list(flags))
    return {"proposed": len(adj), "from_flags": list(flags)}


def get_recommendations(pool: ConnectionPool) -> list[dict]:
    """Pending Coach recommendations awaiting approval (newest first)."""
    return [dict(r) for r in _fetch(pool,
        "SELECT id, adjustments, from_flags, (created_at AT TIME ZONE 'Australia/Melbourne') u "
        "FROM lisa_auto_adjustments WHERE applied=false ORDER BY created_at DESC LIMIT 10")]


def apply_adjustment(pool: ConnectionPool, settings: Settings, adj_id: int) -> dict:
    """APPROVE a pending Coach recommendation: apply it to the live agent's delimited AUTO block (never the
    core rules) + republish + mark applied. Safety-checked again before it goes live."""
    r = _fetch(pool, "SELECT adjustments FROM lisa_auto_adjustments WHERE id=%s AND applied=false", (adj_id,))
    if not r:
        return {"error": "recommendation not found or already applied"}
    adj = r[0].get("adjustments") or []
    block = "\n".join(f"- {a}" for a in adj)
    if any(b in block.lower() for b in _AUTO_BLOCKLIST):
        return {"error": "unsafe content — not applied"}
    try:
        agent = _retell(settings, "GET", f"get-agent/{getattr(settings, 'lisa_agent_id', '')}")
        llm_id = (agent.get("response_engine") or {}).get("llm_id")
        gp = _retell(settings, "GET", f"get-retell-llm/{llm_id}").get("general_prompt", "")
        newblock = _AUTO_START + "\n(Approved coaching adjustments — follow them.)\n" + block + "\n" + _AUTO_END
        if _AUTO_START in gp and _AUTO_END in gp:
            gp2 = re.sub(re.escape(_AUTO_START) + ".*?" + re.escape(_AUTO_END), lambda _m: newblock, gp, flags=re.S)
        else:
            gp2 = gp + "\n\n" + newblock
        _retell(settings, "PATCH", f"update-retell-llm/{llm_id}", {"general_prompt": gp2})
        _retell(settings, "POST", f"publish-agent/{getattr(settings, 'lisa_agent_id', '')}", {})
    except Exception as exc:
        return {"error": f"apply failed: {str(exc)[:100]}"}
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute("UPDATE lisa_auto_adjustments SET applied=true WHERE id=%s", (adj_id,))
        conn.commit()
    log.info("auto_coach_approved_applied", id=adj_id, n=len(adj))
    return {"applied": True, "n": len(adj)}


def dismiss_adjustment(pool: ConnectionPool, adj_id: int) -> dict:
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM lisa_auto_adjustments WHERE id=%s AND applied=false", (adj_id,))
        conn.commit()
    return {"dismissed": True}


def get_stage_coaches(pool: ConnectionPool) -> list[dict]:
    rows = {r["stage"]: r for r in _fetch(pool, "SELECT stage,title,guidance,benchmark FROM lisa_stage_coach")}
    out = []
    for stage, title, desc, bm in LISA_STAGES:
        r = rows.get(stage) or {}
        g = r.get("guidance") or {}
        out.append({"stage": stage, "title": title, "owns": desc, "benchmark": r.get("benchmark") or bm,
                    "do": g.get("do") or [], "avoid": g.get("avoid") or [], "best_line": g.get("best_line") or "",
                    "trained": bool(g)})
    return out


def funnel_kpis(pool: ConnectionPool, days: int = 30) -> dict:
    """Lisa's outbound funnel by stage (dials → connected → reached DM → booked → qualified) with the daily
    qualified target, so every stage can be measured against its benchmark toward 5 qualified/day."""
    r = _fetch(pool,
        "SELECT count(*) dials, "
        "  count(*) FILTER (WHERE call_outcome NOT IN ('no_answer','voicemail','wrong_number') OR meeting_agreed) connected, "
        "  count(*) FILTER (WHERE call_outcome IN ('callback_requested','not_interested','do_not_call') OR meeting_agreed) reached_dm, "
        "  count(*) FILTER (WHERE meeting_agreed) booked, "
        "  count(*) FILTER (WHERE meeting_agreed AND qualification IS NOT NULL) qualified "
        "FROM lisa_calls WHERE created_at >= now() - make_interval(days => %s)", (days,))
    s = dict(r[0]) if r else {}
    def pct(a, b):
        return round(100 * (a or 0) / b, 1) if b else 0.0
    d = s.get("dials") or 0
    return {"dials": d, "connected": s.get("connected") or 0, "reached_dm": s.get("reached_dm") or 0,
            "booked": s.get("booked") or 0, "qualified": s.get("qualified") or 0,
            "connect_rate": pct(s.get("connected"), d), "rpc_rate": pct(s.get("reached_dm"), s.get("connected")),
            "book_rate": pct(s.get("booked"), s.get("reached_dm")), "qual_rate": pct(s.get("qualified"), s.get("booked")),
            "daily_qualified_target": 5}


def _twilio_costs(settings: Settings, days: int) -> dict:
    """Actual Twilio spend (SMS + voice) on Lisa's numbers over the period, fetched live from Twilio."""
    if not _twilio_ready(settings):
        return {"sms_cents": 0.0, "voice_cents": 0.0, "sms_count": 0, "note": "Twilio not configured"}
    from datetime import datetime, timezone, timedelta
    from email.utils import parsedate_to_datetime
    AC = getattr(settings, "twilio_account_sid", "")
    user = getattr(settings, "twilio_api_key_sid", "") or AC
    sec = getattr(settings, "twilio_api_key_secret", "") or getattr(settings, "twilio_auth_token", "")
    auth = base64.b64encode(f"{user}:{sec}".encode()).decode()
    nums = {_e164_au(n) for n in getattr(settings, "lisa_numbers", [])} | {_e164_au(n) for n in getattr(settings, "lisa_sms_number_list", [])}
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    def fetch(kind):
        url = f"https://api.twilio.com/2010-04-01/Accounts/{AC}/{kind}.json?PageSize=1000"
        try:
            req = urllib.request.Request(url, headers={"Authorization": f"Basic {auth}"})
            return json.loads(urllib.request.urlopen(req, timeout=45).read().decode())
        except Exception as exc:
            log.warning("twilio_cost_fetch_failed", kind=kind, error=str(exc)[:120])
            return {}

    def within(x):
        d = x.get("date_created") or x.get("date_sent") or x.get("start_time")
        try:
            return parsedate_to_datetime(d) >= cutoff
        except Exception:
            return True

    def cents(x):
        try:
            return abs(float(x.get("price") or 0)) * 100
        except Exception:
            return 0.0
    msgs = [m for m in fetch("Messages").get("messages", []) if within(m) and (m.get("from") in nums or m.get("to") in nums)]
    calls = [c for c in fetch("Calls").get("calls", []) if within(c) and c.get("from") in nums]
    return {"sms_cents": round(sum(cents(m) for m in msgs), 2), "voice_cents": round(sum(cents(c) for c in calls), 2),
            "sms_count": len(msgs), "note": ""}


def system_costs(pool: ConnectionPool, settings: Settings, days: int = 30) -> dict:
    """The FULL Lisa running cost for the period, accurate to the sources: Retell (per-call, stored),
    OpenAI LLM (tracked per AI-staff task), and Twilio SMS + voice (fetched live). Plus unit economics."""
    ensure_tables(pool)
    rc = _fetch(pool, "SELECT COALESCE(sum(cost_cents),0) c, count(*) n FROM lisa_calls "
                "WHERE created_at >= now() - make_interval(days => %s)", (days,))[0]
    purposes = ("coach", "stage_coach", "qa", "sms_reply")
    lu = _fetch(pool, "SELECT COALESCE(sum(cost_cents),0) c, " +
                ", ".join(f"COALESCE(sum(cost_cents) FILTER (WHERE purpose='{p}'),0) {p}" for p in purposes) +
                " FROM lisa_llm_usage WHERE created_at >= now() - make_interval(days => %s)", (days,))[0]
    bq = _fetch(pool, "SELECT count(*) FILTER (WHERE meeting_agreed) b, "
                "count(*) FILTER (WHERE meeting_agreed AND qualification IS NOT NULL) q FROM lisa_calls "
                "WHERE created_at >= now() - make_interval(days => %s)", (days,))[0]
    tw = _twilio_costs(settings, days)
    retell_c = float(rc["c"] or 0)
    llm_c = float(lu["c"] or 0)
    total = retell_c + llm_c + tw["sms_cents"] + tw["voice_cents"]
    calls = rc["n"] or 0
    return {"days": days,
            "retell_cents": round(retell_c, 1), "llm_cents": round(llm_c, 1),
            "twilio_sms_cents": tw["sms_cents"], "twilio_voice_cents": tw["voice_cents"], "twilio_note": tw.get("note", ""),
            "total_cents": round(total, 1), "calls": calls, "sms_count": tw["sms_count"],
            "llm_breakdown": {p: round(float(lu.get(p) or 0), 2) for p in purposes},
            "per_call_cents": round(total / calls, 1) if calls else 0,
            "per_booking_cents": round(total / bq["b"], 1) if bq["b"] else None,
            "per_qualified_cents": round(total / bq["q"], 1) if bq["q"] else None,
            "note": "Retell per-call (stored) + OpenAI (tracked) + Twilio SMS/voice (live). Enrichment "
                    "(DataForSEO/Apollo) is shared + mostly one-time-cached, billed separately."}


def review_lisa_call(pool: ConnectionPool, settings: Settings, call_id: str, transcript: str) -> dict:
    """AI QA reviewer: score one Lisa call for quality + brand + compliance; store for the scorecard."""
    sys = ("You are the QA reviewer + coach for an AU appointment-setting AI caller (Lisa, brand 'DE Group', "
           "who books a free strategy session). Review her transcript strictly. Return STRICT JSON: "
           "opening (1-5), discovery (1-5), objection_handling (1-5), booking (1-5), "
           "compliance (1-5 — said ONLY 'DE Group', NEVER 'Traffic Radius'/'Digital Expo'; didn't volunteer "
           "she's an AI unless directly asked; sounded human), overall (1-5), best_line (string), "
           "improve (one-sentence coaching tip). "
           "ALSO detect these SPECIFIC failure modes as booleans (true ONLY if it clearly happened): "
           "gatekeeper_leak (told a receptionist/non-decision-maker it was about marketing/advertising/sales/"
           "SEO or 'how people find the business', instead of just asking for the person by name or giving a "
           "neutral curiosity reason), pushed_wrong_person (kept pitching or asking questions after someone "
           "said they're not the right person), brand_slip (said any brand other than 'DE Group'), "
           "robotic_name (greeted using a full name or an obviously wrong/mismatched name), "
           "talked_over_or_repeated (talked over the prospect, or asked the same question twice), "
           "over_questioned (kept asking questions after the prospect signalled they were busy), "
           "generic_no_data (opened generic/scripted instead of using a SPECIFIC real detail about THIS "
           "business — their service, their situation, a real hook), "
           "monologued (delivered a long scripted-sounding turn instead of short, human lines — a big AI tell), "
           "answered_ai_late (was asked if she's an AI/robot/real person and did NOT answer honestly straight "
           "away — kept talking first), "
           "folded_on_reflex_no (accepted a quick 'not interested'/'we have an agency' as final WITHOUT one "
           "smart, specific re-engage first), "
           "sounded_polished_not_human (stiff corporate phrasing that reads as a bot rather than a real person). "
           "flags = array of short human-readable labels for every problem found (empty array if none).")
    r = _llm_json(settings, sys, transcript or "", pool=pool, purpose="qa")
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
    actions = {"prioritized": 0, "reserved": 0, "coach": "skip", "qa_reviewed": 0, "briefs_built": 0, "dms_resolved": 0, "scheduled": 0}
    alerts: list[str] = []

    # THROTTLE the heavy orchestration. Pool-reserve, coaching, QA, DM-enrichment (paid Apollo) and
    # scheduling do NOT need to run every 60s — running them ~every few minutes keeps the enriched-DM +
    # calendar buffer full while leaving each 60s cycle free to DIAL (run_lisa_autodial runs first,
    # unthrottled, in cli.refresh). This is what turns the ~4-5 min/call cadence into ~1 min/call.
    heavy_every = int(getattr(settings, "lisa_hos_heavy_interval_s", 180))
    do_heavy = True
    try:
        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT (hos_heavy_at IS NULL OR hos_heavy_at < now() - make_interval(secs => %s)) go "
                        "FROM lisa_control WHERE id=1", (heavy_every,))
            row = cur.fetchone()
            if row is not None:
                do_heavy = bool(row["go"])
                if do_heavy:                       # claim the slot NOW so overlapping cycles don't double-run
                    cur.execute("UPDATE lisa_control SET hos_heavy_at=now() WHERE id=1")
                    conn.commit()
    except Exception as exc:
        log.warning("hos_throttle_failed", error=str(exc)[:140]); do_heavy = True
    if not do_heavy:
        return {"skipped": "heavy throttled", "next_heavy_s": heavy_every}

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
    # per-stage classification for AI-agent calls (same evidence panel as human calls)
    try:
        actions["ai_classified"] = classify_ai_calls(pool, settings).get("classified", 0)
    except Exception as exc:
        log.warning("hos_ai_classify_failed", error=str(exc)[:140])
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
            refresh_stage_coaches(pool, settings)      # dedicated coach per funnel stage (throttled ~20h)
        except Exception as exc:
            log.warning("hos_stagecoach_failed", error=str(exc)[:140])
        try:
            refresh_coach_adjustments(pool, settings)  # propose fixes from recurring QA flags (throttled ~4h)
        except Exception as exc:
            log.warning("hos_autocoach_failed", error=str(exc)[:140])
    try:
        actions["briefs_built"] = refresh_lisa_briefs(pool, settings, limit=600).get("built", 0)
    except Exception as exc:
        log.warning("hos_briefs_failed", error=str(exc)[:140])
    try:
        # Cap resolves/cycle so the heavy pass stays FAST (each free resolve is an Apollo HTTP call ~1-2s).
        # Enrichment is dial-order, so ~12/heavy-cycle stays comfortably ahead of the ~1/min dial rate.
        actions["dms_resolved"] = refresh_decision_makers(
            pool, settings, limit=int(getattr(settings, "lisa_dm_resolve_per_cycle", 12))).get("resolved", 0)
    except Exception as exc:
        log.warning("hos_dm_failed", error=str(exc)[:140])
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
    # QA failure-modes: surface any that recurred so a real problem becomes visible + actionable
    _FLBL = {"gatekeeper_leak": "outed as marketing to gatekeepers", "pushed_wrong_person": "kept pitching wrong people",
             "brand_slip": "said the wrong brand", "robotic_name": "robotic/wrong names",
             "talked_over_or_repeated": "talked over / repeated", "over_questioned": "over-questioned busy prospects",
             "generic_no_data": "opened generic — didn't use the prospect data", "monologued": "monologued (long turns)",
             "answered_ai_late": "answered 'are you AI?' late", "folded_on_reflex_no": "folded on a reflex 'no'",
             "sounded_polished_not_human": "sounded polished/robotic, not human"}
    try:
        _F = list(_FLBL)
        flr = _fetch(pool, "SELECT " + ", ".join(f"count(*) FILTER (WHERE scores->>'{f}'='true') {f}" for f in _F) +
                     " FROM lisa_call_reviews WHERE reviewed_at >= now() - interval '7 days'")
        for f, n in (dict(flr[0]) if flr else {}).items():
            if n and n >= 2:
                alerts.append(f"QA: {n} calls {_FLBL[f]} — Coach to fix")
    except Exception as exc:
        log.warning("hos_qaflags_failed", error=str(exc)[:120])

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
    # --- QA failure-mode flags the reviewer caught across recent calls (the AI staff doing its duty) ---
    _F = ["gatekeeper_leak", "pushed_wrong_person", "brand_slip", "robotic_name", "talked_over_or_repeated",
          "over_questioned", "generic_no_data", "monologued", "answered_ai_late", "folded_on_reflex_no",
          "sounded_polished_not_human"]
    fl = _fetch(pool, "SELECT " + ", ".join(
        f"count(*) FILTER (WHERE scores->>'{f}' = 'true') {f}" for f in _F) +
        " FROM lisa_call_reviews WHERE reviewed_at >= now() - interval '21 days'")
    s["qa_flags"] = {k: v for k, v in (dict(fl[0]) if fl else {}).items() if v}
    # --- Head of Sales · Strategist: the persisted orchestration decision + what it last did (real work) ---
    s["strategy"] = get_strategy(pool)
    now_mel = datetime.now(ZoneInfo(tz))
    s["in_hours"] = bool(now_mel.weekday() < 5 and 9 <= now_mel.hour < 17)
    # --- per-stage dedicated coaches + the outbound funnel KPIs (goal = 5 qualified bookings/day) ---
    s["stage_coaches"] = get_stage_coaches(pool)
    s["funnel"] = funnel_kpis(pool, days)
    s["recommendations"] = get_recommendations(pool)   # pending Coach fixes awaiting your approval
    return s


def recent_calls(pool: ConnectionPool, limit: int = 100, from_numbers: list[str] | None = None,
                 agent: str | None = None, booked: bool = False, date: str | None = None) -> list[dict]:
    """Recent Lisa calls. `agent`/`booked`/`date` filter SERVER-side so a filtered view is never
    truncated by the recent-N window (at 600+ calls/day a morning booking falls outside the last
    300 rows). Line attribution = either-leg rule: a call is Lisa-4's iff from_number OR to_number
    contains her line digits (inbound bookings like Samantha's count for L4); else Lisa-1."""
    from .lisa4 import L4_LINE_RX  # single source of truth for the Lisa-4 line registry (all lines ever)
    ensure_tables(pool)
    froms = list(from_numbers or [])
    l4_pred = ("(COALESCE(lc.from_number,'') ~ %(l4d)s "
               "OR COALESCE(lc.to_number,'') ~ %(l4d)s)")
    conds, extra = [], {"l4d": L4_LINE_RX}
    if agent == "lisa4":
        conds.append(l4_pred)
    elif agent == "lisa1":
        conds.append(f"NOT {l4_pred}")
    if booked:
        conds.append("COALESCE(lc.meeting_agreed,false)")
    if date == "today":
        conds.append("(lc.created_at AT TIME ZONE 'Australia/Melbourne')::date="
                     "(now() AT TIME ZONE 'Australia/Melbourne')::date")
    elif date == "7d":
        conds.append("(lc.created_at AT TIME ZONE 'Australia/Melbourne')::date>="
                     "(now() AT TIME ZONE 'Australia/Melbourne')::date - 6")
    where = ("WHERE " + " AND ".join(conds) + " ") if conds else ""
    return _fetch(pool,
        "SELECT lc.call_id, lc.dest9, lc.prospect_name, lc.company_name, lc.domain, lc.status, lc.call_outcome, "
        # which agent owns this row (either-leg rule) — the UI scopes/chips by THIS, not by
        # comparing raw numbers client-side (formatting drift dropped inbound rows).
        f"  CASE WHEN {l4_pred} THEN 'lisa4' ELSE 'lisa1' END AS agent, "
        # ALWAYS-identifiable name: call record -> Lisa-4 pool (title/company/domain) -> the number itself
        "  COALESCE(NULLIF(lc.company_name,''), lp4.title, lp4.company, lp4.domain, NULLIF(lc.domain,''), "
        "           NULLIF(lc.prospect_name,''), lc.to_number) AS display_name, lp4.bucket AS pool_bucket, "
        "  lc.meeting_agreed, lc.agreed_day_time, lc.callback_when, lc.main_objection, lc.asked_if_ai, lc.call_summary, "
        "  lc.recording_url, lc.duration_ms, lc.cost_cents, lc.booked_event_id, lc.sms_sent, lc.qualification, "
        "  lc.from_number, lc.to_number, "
        # INBOUND = a prospect called Lisa BACK (from_number is not one of her registered caller IDs). Shows
        # in the same recent-calls list, flagged so callbacks from our voicemails/SMS are visible.
        "  (%(froms)s::text[] <> '{}' AND lc.from_number IS NOT NULL AND lc.from_number <> ALL(%(froms)s::text[])) AS inbound, "
        "  (lc.created_at AT TIME ZONE 'Australia/Melbourne') AS created_local, "
        # audit report we generated + texted for this prospect (token -> public /r/ link, built client-side)
        "  (SELECT s.token FROM lisa_audit_shares s WHERE s.domain=lc.domain ORDER BY s.created_at DESC LIMIT 1) AS audit_token, "
        # SMS attributed to THIS call only: messages in the window [this call, next call to same number),
        # so a missed-call/audit SMS shows on the call that sent it — not on every row for that number.
        "  (SELECT json_agg(json_build_object("
        "        'dir', x.direction, 'body', x.body, "
        "        'at', to_char(x.created_at AT TIME ZONE 'Australia/Melbourne','DD Mon HH24:MI')) ORDER BY x.created_at) "
        "     FROM lisa_sms x WHERE x.dest9=lc.dest9 AND x.created_at >= lc.created_at "
        "       AND x.created_at < COALESCE((SELECT min(lc2.created_at) FROM lisa_calls lc2 "
        "                                    WHERE lc2.dest9=lc.dest9 AND lc2.created_at > lc.created_at), 'infinity')) AS sms "
        "FROM lisa_calls lc LEFT JOIN lisa4_pool lp4 ON lp4.dest9=lc.dest9 "
        + where +
        "ORDER BY lc.created_at DESC LIMIT %(limit)s",
        {**extra, "froms": froms, "limit": limit})


def sync_qualifier_calls(pool: ConnectionPool, settings: Settings) -> dict:
    """AUTO qualification-tracking (Raj, 2026-08-11): Ben (BDM) qualifies Lisa-1 bookings by phone.
    When a qualifier has an ANSWERED (>=45s) call to any booked_crm prospect AFTER its booking, append a
    one-line CRM note (once per call) so qualification self-tracks. Manoj/reveals are handled separately
    by sync_reveal_calls."""
    ensure_tables(pool)
    names = [n.strip() for n in str(getattr(settings, "booking_qualifier_names", "") or "Ben").split(",") if n.strip()]
    # EVERY attempt is noted (Raj, 2026-08-12) — answered talks AND misses (voicemail / no-pickup / short),
    # so the booked-CRM page shows 'Manoj tried — voicemail' in near-realtime, not just successful quals.
    rows = _fetch(pool,
        "SELECT bc.dest9, c.bde_name, c.call_id, round(COALESCE(c.talk_seconds,0)) s, c.answered, "
        "  COALESCE(cl.call_outcome,'') outcome, "
        "  to_char(c.started_at AT TIME ZONE 'Australia/Melbourne','Dy DD Mon HH24:MI') t "
        "FROM booked_crm bc JOIN calls c "
        "  ON right(regexp_replace(COALESCE(c.dest_number,''),'[^0-9]','','g'),9)=bc.dest9 "
        "LEFT JOIN classifications cl ON cl.call_id=c.call_id "
        "WHERE c.bde_name = ANY(%s) "
        "  AND c.started_at > now() - interval '3 days' "
        "  AND COALESCE(bc.note,'') NOT LIKE '%%[q:'||c.call_id||']%%' LIMIT 30", (names,))
    n = 0
    for r in rows:
        s = int(r["s"] or 0)
        if r["answered"] and s >= 45:
            line = f" · BDM QUAL CALL by {r['bde_name']} ({s}s, {r['t']})"
        elif "voicemail" in (r["outcome"] or "").lower() or (not r["answered"] and s <= 5):
            line = f" · {r['bde_name']} tried — voicemail/no pick-up ({r['t']})"
        else:
            line = f" · {r['bde_name']} tried — short call {s}s ({r['t']})"
        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE booked_crm SET note=COALESCE(note,'')||%s||' [q:'||%s||']', "
                "  updated_by='auto-qual-sync', updated_at=now() WHERE dest9=%s",
                (line, r["call_id"], r["dest9"]))
            conn.commit()
            n += 1
    return {"noted": n}
