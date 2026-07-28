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
        # per-FUNNEL-STAGE dedicated coach (opener/gatekeeper/pitch/objection/close) — each learns + trains
        # for its own stage. One row per stage.
        cur.execute("CREATE TABLE IF NOT EXISTS lisa_stage_coach ("
                    "  stage text PRIMARY KEY, title text, guidance jsonb, benchmark numeric,"
                    "  built_at timestamptz DEFAULT now())")
        # LLM (OpenAI) token usage + cost per Lisa AI-staff task — so the cost page is accurate.
        cur.execute("CREATE TABLE IF NOT EXISTS lisa_llm_usage ("
                    "  id bigserial PRIMARY KEY, purpose text, model text, prompt_tokens integer,"
                    "  completion_tokens integer, cost_cents numeric, created_at timestamptz DEFAULT now())")
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
                    "  resolved_at timestamptz DEFAULT now())")
        conn.commit()
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
    return parts[0][:40].capitalize() if parts else ""


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


def get_decision_maker(pool: ConnectionPool, domain: str) -> dict:
    if not domain:
        return {}
    r = _fetch(pool, "SELECT domain,dm_name,dm_first,dm_title,dm_phone,dm_is_mobile,source,confidence,"
               "(resolved_at AT TIME ZONE 'Australia/Melbourne') resolved_local FROM lisa_dm WHERE domain=%s", (domain,))
    return dict(r[0]) if r else {}


def refresh_decision_makers(pool: ConnectionPool, settings: Settings, limit: int = 40) -> dict:
    """Resolve the DM for any pooled prospect that doesn't have one yet (a batch per cycle). Automatic."""
    ensure_tables(pool)
    rows = _fetch(pool, "SELECT DISTINCT lp.domain FROM lisa_pool lp LEFT JOIN lisa_dm d ON d.domain=lp.domain "
                  "WHERE NULLIF(lp.domain,'') IS NOT NULL AND d.domain IS NULL LIMIT %s", (limit,))
    n = sum(1 for r in rows if resolve_decision_maker(pool, settings, r["domain"]))
    return {"resolved": n, "pending": max(0, len(rows) - n)}


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
    contact = _first_name((row or {}).get("contact") or "")   # FIRST name only (never "Clint Robinson")

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

    # --- PRIVATE background (never spoken): Lisa must NOT reveal we've called/spoken before. She uses this
    #     only to sound relevant + pre-empt objections, as if she'd done her homework — not "we called you". ---
    prior = ""   # never say "our team called before / following up" (owner rule)
    kb = []
    if row:
        if row.get("ever_agency"):
            kb.append("They likely already use a marketing agency — frame the session as an independent second "
                      "opinion, and don't rubbish their agency (do NOT say we know this or that anyone told us)")
        if row.get("problem"):
            kb.append(f"Likely pain point to weave in naturally (never say 'you mentioned'): {row['problem']}")
        if row.get("lead_temp") and row["lead_temp"] not in ("none", "None"):
            kb.append(f"Prior signal (private): {row['lead_temp']}")
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
    # Prefer the resolved decision-maker's MOBILE — reach the RIGHT person on a good number.
    dm = get_decision_maker(pool, dom) if dom else {}
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
    brief = get_brief(pool, settings, dest9=d9, domain=domain)   # saved per-prospect brief (builds once if missing)
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


def handle_inbound_sms(pool: ConnectionPool, settings: Settings, from_number: str, body: str) -> dict:
    """A prospect TEXTED BACK. Capture it (was previously lost), then reply as Lisa — short, human, aiming
    to lock a quick call — using their saved brief for context. Two-way SMS, backend-driven."""
    ensure_tables(pool)
    d9 = _d9(from_number)
    _log_sms(pool, "inbound", from_number, getattr(settings, "lisa_sms_from", ""), body, d9)
    br = _fetch(pool, "SELECT brief FROM lisa_briefs WHERE dest9=%s", (d9,))
    brief = (br[0]["brief"] if br and br[0].get("brief") else {}) or {}
    name = _first_name(brief.get("prospect_name") or "")
    hist = _fetch(pool, "SELECT direction, body FROM lisa_sms WHERE dest9=%s ORDER BY created_at DESC LIMIT 6", (d9,))
    thread = "\n".join(f"{'Them' if h['direction']=='inbound' else 'Lisa'}: {h['body']}" for h in reversed(hist))
    sys = ("You are Lisa from DE Group, an Australian digital-marketing team, replying by SMS to a prospect who "
           "texted back after you tried to call them. Reply SHORT (1-2 sentences), warm, human and casual — like "
           "a real person texting. Your goal: get them to agree to a quick 15-minute chat with our strategist "
           "about how they're showing up on Google (they run Google Ads). If they ask who you are or how they "
           "know you, be honest and friendly (you'd tried to reach them about their online presence). If they "
           "ask whether you're an AI, admit it briefly and warmly. Never pushy, never long, never a placeholder. "
           "Return STRICT JSON: {\"reply\": \"...\"}.")
    usr = (f"Prospect first name: {name or 'there'}\nCompany: {brief.get('company_name') or ''}\n"
           f"What I'd flag: {brief.get('finding_1') or 'how they show up on Google next to their ads'}\n\nThread so far:\n{thread}")
    r = _llm_json(settings, sys, usr, pool=pool, purpose="sms_reply")
    reply = ((r.get("reply") or "").strip() if isinstance(r, dict) else "") or (
        f"Hi{(' ' + name) if name else ''}! It's Lisa from DE Group — I'd tried you about how you're showing up "
        "on Google next to your ads. Worth a quick 15-min chat? What time roughly suits you?")
    sms_from = _pick_sms_from(settings)
    try:
        if _twilio_ready(settings) and _send_sms_twilio(settings, from_number, reply, sms_from):
            _log_sms(pool, "outbound", sms_from, from_number, reply, d9)
    except Exception as exc:
        log.warning("lisa_sms_reply_failed", error=str(exc)[:150])
    return {"ok": True, "reply": reply}


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
        now_l = datetime.now(ZoneInfo(tz))
        wstart = int(getattr(settings, "lisa_call_window_start", 9))
        wend = int(getattr(settings, "lisa_call_window_end", 17))
        dt_h = int(getattr(settings, "lisa_double_tap_hours", 2))
        # DOUBLE-TAP: a no-answer gets ONE quick same-day retry (+dt_h) if still within business hours,
        # before falling back to the multi-day cadence — mirrors how a human re-tries a missed call.
        cand = now_l + timedelta(hours=dt_h)
        if outcome == "no_answer" and attempts <= 1 and dt_h > 0 and cand.weekday() < 5 and wstart <= cand.hour < wend:
            when, label = cand, "double-tap"
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
    """Reserve Lisa's EXCLUSIVE pool from already-WORKED GAds prospects — ones our human BDEs have already
    REACHED (rpc_connect), so we have a proven, dialable number + a real contact name (call intelligence).
    SMB-only: national chains (many branches / very high revenue) are excluded. Highest-advertising first.
    Also PURGES any fresh (never-human-called) entries so the pool becomes worked-data only. Idempotent."""
    ensure_tables(pool)
    size = int(getattr(settings, "lisa_pool_size", 500))
    # switch to worked data: drop fresh (never human-reached) entries so only proven prospects remain
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM lisa_pool lp WHERE NOT EXISTS (SELECT 1 FROM calls c WHERE c.in_scope "
                    "AND c.provider IN ('3cx','aircall') AND "
                    "right(regexp_replace(COALESCE(c.dest_number,''),'[^0-9]','','g'),9)=lp.dest9)")
        conn.commit()
    have = _fetch(pool, "SELECT count(*) c FROM lisa_pool")[0]["c"]
    if have >= size:
        return {"reserved": 0, "total": have, "note": "pool at size (worked)"}
    need = size - have
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "WITH worked AS ("
            "  SELECT DISTINCT ON (d9) d9, dest_number, dom, company FROM ("
            "    SELECT right(regexp_replace(COALESCE(c.dest_number,''),'[^0-9]','','g'),9) d9, c.dest_number, "
            "      split_part(regexp_replace(lower(COALESCE(cl.prospect_website,'')),'^https?://(www\\.)?',''),'/',1) dom, "
            "      cl.prospect_company company, c.started_at, cl.rpc_connect, cl.do_not_contact, cl.meeting_booked "
            "    FROM calls c JOIN classifications cl ON cl.call_id=c.call_id "
            "    WHERE c.in_scope AND c.provider IN ('3cx','aircall') AND c.answered "
            "      AND COALESCE(c.dest_number,'')<>'' AND COALESCE(cl.prospect_contact_name,'')<>'' "
            "  ) x WHERE x.rpc_connect AND NOT COALESCE(x.do_not_contact,false) "
            "    AND NOT COALESCE(x.meeting_booked,false) AND length(x.d9)=9 AND x.dom<>'' "
            "  ORDER BY d9, started_at DESC) "
            "INSERT INTO lisa_pool (dest9, dest_number, domain, company, phone, priority) "
            "SELECT w.d9, w.dest_number, w.dom, w.company, w.dest_number, "
            "       COALESCE((e.dataforseo->'ads'->>'count')::int,0) FROM worked w "
            "JOIN enrichment e ON e.domain=w.dom "
            "WHERE (e.dataforseo->>'running_google_ads')='true' "
            # SMB only: exclude national chains (many branch rows) + very large businesses
            "  AND (SELECT count(*) FROM companies c3 WHERE c3.domain=w.dom) <= 8 "
            "  AND NOT EXISTS (SELECT 1 FROM companies c2 WHERE c2.domain=w.dom AND COALESCE(c2.revenue_musd,0)>100) "
            "  AND w.d9 NOT IN (SELECT dest9 FROM lisa_pool) "
            "  AND NOT EXISTS (SELECT 1 FROM prospect_pipeline pp WHERE pp.dest9=w.d9 AND COALESCE(pp.dnd,false)) "
            "ORDER BY COALESCE((e.dataforseo->'ads'->>'count')::int,0) DESC NULLS LAST "
            "LIMIT %s ON CONFLICT (dest9) DO NOTHING", (need,))
        got = cur.rowcount
        conn.commit()
    return {"reserved": got, "total": have + got, "worked": True}


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
    if not get_autodial_state(pool, settings):          # console toggle (DB) wins; env is the fallback default
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
    # NATURAL PACING — dial like a human: ONE call at a time, evenly spread so ~daily_target calls fill the
    # whole business-hours window (no concurrency, no fast bursts). The gap = window / daily_target
    # (e.g. 8h / 50 ≈ 9.6 min between calls); overridable via LISA_MIN_CALL_GAP_SECONDS.
    gap = int(getattr(settings, "lisa_min_call_gap_seconds", 0)) or \
        int(((wend - wstart) * 3600) / max(1, int(getattr(settings, "lisa_daily_target", 50))))
    since = _fetch(pool, "SELECT extract(epoch from (now() - max(created_at))) s FROM lisa_calls "
                   "WHERE (created_at AT TIME ZONE %s)::date=(now() AT TIME ZONE %s)::date", (tz, tz))[0]["s"]
    if since is not None and since < gap:
        return {"skipped": "natural pacing", "wait_s": int(gap - since), "gap_s": gap, "placed_today": placed_today}
    # look at a few due candidates but place AT MOST ONE this cycle (skipping any outside its local hours)
    due = _fetch(pool,
        "SELECT id, dest_number, right(regexp_replace(COALESCE(dest_number,''),'[^0-9]','','g'),9) d9 "
        "FROM calendar_events WHERE bde_name='Lisa' AND status='pending' "
        "  AND type IN ('fresh_call','retry','callback','reached_call') AND start_at <= now() "
        "ORDER BY start_at LIMIT 8")
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
    stats = {"dialed": dialed, "candidates": len(due), "skipped_tz": skipped_tz,
             "placed_today": placed_today, "gap_s": gap}
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
           "over_questioned (kept asking questions after the prospect signalled they were busy). "
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
            refresh_stage_coaches(pool, settings)      # dedicated coach per funnel stage (throttled ~20h)
        except Exception as exc:
            log.warning("hos_stagecoach_failed", error=str(exc)[:140])
    try:
        actions["briefs_built"] = refresh_lisa_briefs(pool, settings, limit=600).get("built", 0)
    except Exception as exc:
        log.warning("hos_briefs_failed", error=str(exc)[:140])
    try:
        actions["dms_resolved"] = refresh_decision_makers(pool, settings, limit=60).get("resolved", 0)
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
             "talked_over_or_repeated": "talked over / repeated", "over_questioned": "over-questioned busy prospects"}
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
    _F = ["gatekeeper_leak", "pushed_wrong_person", "brand_slip", "robotic_name", "talked_over_or_repeated", "over_questioned"]
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
    return s


def recent_calls(pool: ConnectionPool, limit: int = 100) -> list[dict]:
    ensure_tables(pool)
    return _fetch(pool,
        "SELECT call_id, dest9, prospect_name, company_name, domain, status, call_outcome, "
        "  meeting_agreed, agreed_day_time, callback_when, main_objection, asked_if_ai, call_summary, "
        "  recording_url, duration_ms, cost_cents, booked_event_id, sms_sent, qualification, "
        "  (created_at AT TIME ZONE 'Australia/Melbourne') AS created_local "
        "FROM lisa_calls ORDER BY created_at DESC LIMIT %s", (limit,))
