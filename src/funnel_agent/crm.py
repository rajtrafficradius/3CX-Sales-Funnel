"""Enterprise booked-CRM: the single source of truth for every Lisa booking.

Design (benchmarked on Salesforce/HubSpot/Pipedrive record views):
- The booking record is DERIVED from lisa_calls (source of truth for the booking) and enriched with
  everything already captured in lisa_calls/classifications/lisa4_sites/calls, PLUS an editable overlay
  in `booked_crm` (stage/owner/next action/outcome/contact/email) and a `crm_activity` timeline
  (auto call-logs + manual notes). No data needs new capture — this surfaces + makes it editable.
- Alfred's call attempts auto-log from the `calls` table (provider=aircall, matched by dest9).
- Chrome web-push subscriptions live in `push_subscriptions`.

Stages (pipeline): new -> confirming -> confirmed -> revealed -> won | lost | no_show
"""
from __future__ import annotations
import json, re
from typing import Any

STAGES = ["new", "confirming", "confirmed", "revealed", "won", "lost", "no_show"]


def ensure_crm_tables(pool) -> None:
    """Additive schema — new columns on booked_crm + timeline + push tables. Idempotent."""
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "CREATE TABLE IF NOT EXISTS booked_crm ("
            "  dest9 text PRIMARY KEY, status text, note text, updated_by text,"
            "  updated_at timestamptz DEFAULT now(), replies_seen_at timestamptz)")
        for col, typ in (
            ("stage", "text"), ("owner", "text"), ("next_action", "text"),
            ("next_action_at", "timestamptz"), ("outcome", "text"),
            ("contact_name", "text"), ("contact_email", "text"), ("created_at", "timestamptz DEFAULT now()"),
            ("quote_token", "text"), ("comparison_token", "text"), ("quote_total", "integer"),
            ("guideline_token", "text"),
            ("brand_intro_sent_at", "timestamptz"), ("brand_intro_sent_by", "text"),
            ("audit_token", "text"),
        ):
            cur.execute(f"ALTER TABLE booked_crm ADD COLUMN IF NOT EXISTS {col} {typ}")
        cur.execute(
            "CREATE TABLE IF NOT EXISTS crm_activity ("
            "  id bigserial PRIMARY KEY, dest9 text NOT NULL, kind text NOT NULL,"
            "  body text, author text, meta jsonb, created_at timestamptz DEFAULT now())")
        cur.execute("CREATE INDEX IF NOT EXISTS crm_activity_dest9_idx ON crm_activity(dest9, created_at DESC)")
        cur.execute(
            "CREATE TABLE IF NOT EXISTS push_subscriptions ("
            "  id bigserial PRIMARY KEY, user_email text, endpoint text UNIQUE, p256dh text, auth text,"
            "  ua text, created_at timestamptz DEFAULT now(), last_ok timestamptz)")
        conn.commit()
    # ---- performance: expression indexes for LIST_SQL's trailing-9 joins ----
    # Without these the booked-CRM list did a full regexp scan of the 786k-row companies table and the
    # 250k-row calls table PER booking (~24s for 39 rows). Each index EXACTLY matches the query expression
    # so the planner will use it (an expression index is only used on an identical expression). Built in
    # their own connections and individually guarded so a build failure never breaks table setup, and
    # IF NOT EXISTS so they are idempotent. CONCURRENTLY is intentionally omitted (it cannot run inside a
    # transaction and is unnecessary off-hours for these small/off-path builds).
    _perf_indexes = (
        # companies-by-phone LATERAL fallback (786k rows) — matches COALESCE(cc.phone,'') expression verbatim.
        # NOTE: kept on `phone` (NOT phone_norm): phone_norm diverges from trailing-9(phone) on 38k rows and
        # would silently change which company a booking matches — this is a pure index, zero semantic change.
        "CREATE INDEX IF NOT EXISTS idx_companies_phone9 ON companies "
        "(right(regexp_replace(coalesce(phone,''),'[^0-9]','','g'),9))",
        # Alfred/closer attempts (bde_attempts) + last_contacted over calls (250k rows).
        "CREATE INDEX IF NOT EXISTS idx_calls_dest9norm ON calls "
        "(right(regexp_replace(coalesce(dest_number,''),'[^0-9]','','g'),9))",
        # meeting_at over calendar_events — supports the trailing-9 equality that replaced the leading-% LIKE.
        "CREATE INDEX IF NOT EXISTS idx_calevents_dest9norm ON calendar_events "
        "(right(regexp_replace(coalesce(dest_number,''),'[^0-9]','','g'),9))",
        # inbound-SMS lookups (last_in_body / unread).
        "CREATE INDEX IF NOT EXISTS idx_lisa_sms_dest9_dir ON lisa_sms (dest9, direction, created_at DESC)",
    )
    for _ddl in _perf_indexes:
        try:
            with pool.connection() as _c, _c.cursor() as _cur:
                _cur.execute(_ddl)
                _c.commit()
        except Exception:
            pass


# ---------- read: the enriched booked list ----------
LIST_SQL = """
WITH booked AS (
  SELECT DISTINCT ON (dest9) dest9, call_id, company_name, prospect_name, prospect_email,
         agreed_day_time, created_at AS booked_at,
         (right(regexp_replace(COALESCE(from_number,''),'[^0-9]','','g'),9) = dest9) AS inbound,
         -- Canonical agent lines per Retell (nicknames "Lisa4/Lisa5 rotation") + call behaviour:
         -- Lisa4 = 030256 (unlabelled but does website reveals) + 266405 + 044526 + 091513;
         -- Lisa5 = 096730 + 008827. Was wrong: 091513 (a Lisa4 line) sat in Lisa5, causing the
         -- /lisa-vs-/lisa-crm count mismatch. Keep this in sync with settings.lisa4/5_numbers.
         (from_number ~ '(468030256|489266405|495044526|468091513)' OR to_number ~ '(468030256|489266405|495044526|468091513)') AS is_lisa4,
         (from_number ~ '(468096730|468008827)' OR to_number ~ '(468096730|468008827)') AS is_lisa5
  FROM lisa_calls WHERE COALESCE(meeting_agreed,false) AND dest9 IS NOT NULL
  ORDER BY dest9, created_at ASC)
SELECT b.dest9, b.call_id, b.agreed_day_time, b.booked_at, b.inbound,
       CASE WHEN b.is_lisa5 THEN 'lisa5' WHEN b.is_lisa4 THEN 'lisa4' ELSE 'lisa1' END AS agent,
       COALESCE(NULLIF(bc.contact_name,''), NULLIF(b.prospect_name,''), cl.prospect_contact_name) AS contact_name,
       COALESCE(NULLIF(bc.contact_email,''), NULLIF(b.prospect_email,''), cl.prospect_email) AS contact_email,
       COALESCE(NULLIF(b.company_name,''), lp.company, lb.company_name, cl.prospect_company,
                NULLIF(co.company_name,''), '0'||b.dest9) AS company,
       COALESCE(lp.domain, lb.domain, cl.prospect_website) AS domain,
       COALESCE(NULLIF(cl.problem_summary,''), NULLIF(lp.issue,'')) AS finding,
       (SELECT recording_url FROM lisa_calls WHERE call_id=b.call_id) AS recording_url,
       (SELECT count(*) FROM lisa_calls lc WHERE lc.dest9=b.dest9) AS total_calls,
       (SELECT ce.start_at FROM calendar_events ce
          WHERE right(regexp_replace(COALESCE(ce.dest_number,''),'[^0-9]','','g'),9) = b.dest9
          AND ce.type IN ('reveal','meeting') ORDER BY (ce.status='pending') DESC, ce.start_at DESC LIMIT 1) AS meeting_at,
       st.status AS site_status, st.share_token, st.building_at AS site_building_at, st.built_at AS site_built_at,
       bc.status AS crm_status, bc.stage, bc.owner, bc.next_action, bc.next_action_at, bc.outcome,
       bc.note AS crm_note, bc.updated_by AS crm_by, bc.updated_at AS crm_at,
       bc.quote_token, bc.comparison_token, bc.quote_total, bc.guideline_token,
       bc.brand_intro_sent_at, bc.brand_intro_sent_by, bc.audit_token,
       (SELECT count(*) FROM calls c WHERE right(regexp_replace(COALESCE(c.dest_number,''),'[^0-9]','','g'),9)=b.dest9
          AND c.bde_name = ANY(%(closers)s)) AS bde_attempts,
       (SELECT max(c.started_at) FROM calls c WHERE right(regexp_replace(COALESCE(c.dest_number,''),'[^0-9]','','g'),9)=b.dest9
          AND c.bde_name = ANY(%(closers)s) AND c.answered) AS last_contacted,
       -- LAST CONTACT = the most recent ATTEMPT on this lead (any line: Lisa 4/5 or the closer), answered OR
       -- NOT. A closer who rang to confirm and got no answer still did his job, so he must show as the last
       -- person who worked it — not the last person who happened to pick up.
       (SELECT c.bde_name FROM calls c WHERE right(regexp_replace(COALESCE(c.dest_number,''),'[^0-9]','','g'),9)=b.dest9
          ORDER BY c.started_at DESC LIMIT 1) AS last_contacted_by,
       (SELECT max(c.started_at) FROM calls c WHERE right(regexp_replace(COALESCE(c.dest_number,''),'[^0-9]','','g'),9)=b.dest9)
          AS last_contacted_any,
       -- LAST SPOKEN = who last actually had a real two-way conversation (answered + >=15s) — distinct from a
       -- no-answer attempt, so the team can read the process at a glance (Lisa booked it, Alfred confirming).
       (SELECT c.bde_name FROM calls c WHERE right(regexp_replace(COALESCE(c.dest_number,''),'[^0-9]','','g'),9)=b.dest9
          AND c.answered AND COALESCE(c.talk_seconds,0) >= 15 ORDER BY c.started_at DESC LIMIT 1) AS last_spoke_by,
       (SELECT max(c.started_at) FROM calls c WHERE right(regexp_replace(COALESCE(c.dest_number,''),'[^0-9]','','g'),9)=b.dest9
          AND c.answered AND COALESCE(c.talk_seconds,0) >= 15) AS last_spoke_at,
       -- The CLOSER's (Alfred's) most-recent confirmation-call OUTCOME — so every booking clearly shows
       -- whether Alfred reached them, hit voicemail, or got no answer (never leave it ambiguous).
       -- 'reached' requires a real two-way conversation. A ~15s "answered" call is Alfred leaving a
       -- voicemail (or the greeting counted as talk) — NEVER a confirmation. Real confirmations run 40s+
       -- (observed voicemails=15s, reached=75-171s), so the floor is 30s; err toward 'voicemail' so an
       -- unconfirmed booking keeps getting a retry instead of being falsely marked reached.
       (SELECT CASE WHEN c.is_voicemail THEN 'voicemail'
                    WHEN c.answered AND COALESCE(c.talk_seconds,0) >= 30 THEN 'reached'
                    WHEN c.answered THEN 'voicemail'
                    ELSE 'no answer' END
          FROM calls c WHERE right(regexp_replace(COALESCE(c.dest_number,''),'[^0-9]','','g'),9)=b.dest9
          AND c.bde_name = ANY(%(closers)s) ORDER BY c.started_at DESC LIMIT 1) AS closer_status,
       (SELECT left(body,160) FROM lisa_sms s WHERE s.dest9=b.dest9 AND s.direction='inbound'
        ORDER BY created_at DESC LIMIT 1) AS last_in_body,
       ((SELECT max(created_at) FROM lisa_sms s WHERE s.dest9=b.dest9 AND s.direction='inbound')
        > COALESCE(bc.replies_seen_at, 'epoch'::timestamptz)) AS unread,
       (SELECT call_summary FROM lisa_calls WHERE call_id=b.call_id) AS booking_summary
FROM booked b
LEFT JOIN lisa4_pool lp ON lp.dest9=b.dest9
LEFT JOIN lisa_briefs lb ON lb.dest9=b.dest9
LEFT JOIN classifications cl ON cl.call_id=b.call_id
LEFT JOIN LATERAL (SELECT status, share_token, building_at, built_at FROM lisa4_sites s WHERE s.dest9=b.dest9 ORDER BY (status='built') DESC, id DESC LIMIT 1) st ON true
LEFT JOIN LATERAL (SELECT company_name FROM companies cc
   WHERE right(regexp_replace(COALESCE(cc.phone,''),'[^0-9]','','g'),9)=b.dest9
   ORDER BY (cc.source='gmaps') DESC LIMIT 1) co ON true
LEFT JOIN booked_crm bc ON bc.dest9=b.dest9
ORDER BY GREATEST(b.booked_at, bc.updated_at) DESC NULLS LAST
"""


def _derived_stage(r: dict) -> str:
    """Default stage when Alfred hasn't set one — inferred from evidence."""
    if r.get("stage"):
        return r["stage"]
    st = (r.get("crm_status") or "").lower()
    if st in ("won", "lost", "revealed", "no_show"):
        return {"won": "won", "lost": "lost", "revealed": "revealed", "no_show": "no_show"}[st]
    if r.get("last_contacted"):
        return "confirmed"
    return "new"


def _stage_reason(r: dict) -> str:
    """Human 'why is it at this stage' shown on hover — so the team can see the BASIS of the stage without
    opening the record (booking source + who last spoke + the confirming action)."""
    stage = r.get("stage") or "new"
    by = (r.get("crm_by") or "").strip()
    byl = by.lower()
    if "aircall" in byl:
        src = "set by Alfred's confirmation call (Aircall)"
    elif "fireflies" in byl:
        src = "set from the reveal/confirmation transcript (Fireflies)"
    elif byl in ("recovery", "recovery2", "auto-reclassify", "seed-audit"):
        src = "auto-recovered by the classifier"
    elif "@" in by:
        src = f"set manually by {by}"
    elif not by:
        src = ("inferred from a real conversation on the call"
               if r.get("last_contacted") else "booked by Lisa; not yet confirmed by the closer")
    else:
        src = f"set by {by}"
    bits = [f"Stage “{stage}” — {src}"]
    if r.get("last_contacted_by"):
        bits.append(f"last spoke: {r['last_contacted_by']}")
    if r.get("next_action"):
        bits.append(str(r["next_action"])[:90])
    return " · ".join(bits)


_NON_NAMES = {
    "there", "yeah", "yep", "yes", "no", "nope", "hi", "hello", "hey", "hi there", "hey there",
    "there you go", "mate", "sir", "madam", "maam", "ma'am", "ok", "okay", "okey", "mhm", "mm", "uh",
    "um", "speaking", "thanks", "thank you", "cheers", "bye", "goodbye", "owner", "the owner", "manager",
    "reception", "admin", "good", "fine", "sure", "alright", "right", "hello there", "yes please",
    "no thanks", "this is he", "this is she", "that's me", "thats me", "good morning", "good afternoon",
}


def clean_person_name(name):
    """Reject the conversational fillers the classifier sometimes mis-extracts as a NAME (a greeting
    'hi there' -> 'there'). Returns a genuine name, or '' when it's garbage — so the CRM shows '—' instead
    of a fake name. Shared so the same rule applies at capture-time and at display."""
    n = (name or "").strip()
    if not n:
        return ""
    import re as _re
    low = n.lower().strip(" .,!?'\"")
    words = [w for w in low.split() if w]
    if low in _NON_NAMES or (words and all(w in _NON_NAMES for w in words)):
        return ""
    if not _re.search(r"[A-Za-z]{2,}", n):        # must contain a real alphabetic token
        return ""
    return n


def crm_rows(q, closers: list[str]) -> list[dict]:
    rows = q(LIST_SQL, {"closers": closers or ["__none__"]})
    for r in rows:
        r["stage"] = _derived_stage(r)
        r["stage_reason"] = _stage_reason(r)
        r["contact_name"] = clean_person_name(r.get("contact_name"))   # never show 'there'/'mate'/etc.
    return rows


# ---------- read: one full record + timeline ----------
def _first(rows) -> dict:
    return rows[0] if rows else {}


def crm_record(q, dest9: str, closers: list[str]) -> dict:
    d9 = re.sub(r"[^0-9]", "", dest9 or "")[-9:]
    base = q(LIST_SQL + "\n", {"closers": closers or ["__none__"]})
    rec = next((r for r in base if r["dest9"] == d9), None)
    if not rec:
        return {}
    cid = rec.get("call_id")
    dom = rec.get("domain")

    # ---- booking-call extras (transcript, objection, the AI-tell flag) ----
    bc = _first(q("SELECT transcript, main_objection, asked_if_ai, duration_ms FROM lisa_calls WHERE call_id=%s", (cid,)))
    rec["transcript"] = bc.get("transcript")
    rec["main_objection"] = bc.get("main_objection")
    rec["asked_if_ai"] = bc.get("asked_if_ai")

    # ---- qualification intelligence (BANT+): prefer the booking call, else the latest for this prospect ----
    cl = _first(q(
        """SELECT cl.* FROM classifications cl JOIN lisa_calls lc ON lc.call_id = cl.call_id
           WHERE lc.dest9 = %s ORDER BY (cl.call_id = %s) DESC, cl.classified_at DESC NULLS LAST LIMIT 1""",
        (d9, cid)))
    if cl:
        rec["qual"] = {k: cl.get(k) for k in (
            "qualified", "qual_confidence", "authority", "problem", "budget", "urgency", "aspiration",
            "open_to_listening", "lead_temperature", "booking_status", "runs_paid_ads", "has_marketing_agency",
            "problem_summary", "evidence", "rpc_connect", "full_pitch", "prospect_industry", "prospect_mobile",
            "callback_requested", "callback_when", "pipeline_stage", "recommended_cadence_days", "call_outcome")}
    else:
        rec["qual"] = {}

    # ---- firmographics / company intel (D&B + gmaps rating/reviews + Apollo contacts) ----
    rec["company_intel"] = _first(q(
        """SELECT company_name, company_profile, year_started, employees, revenue_musd, industry, sub_industry,
                  address, suburb, state, postal_code, country, source, gmaps_rating, gmaps_reviews,
                  phone_is_mobile, contacts, source_url
           FROM companies WHERE domain = %s OR phone_norm = %s ORDER BY (domain = %s) DESC NULLS LAST LIMIT 1""",
        (dom, "61" + d9, dom)))

    # ---- meeting / reveal events + follow-up cadence ----
    rec["events"] = q(
        """SELECT type, status, start_at, end_at, title, notes, bde_name
           FROM calendar_events WHERE dest_number LIKE %s ORDER BY start_at DESC LIMIT 12""", ("%" + d9,))

    # ---- SMS conversation (oldest first, so it reads like a chat) ----
    rec["sms_thread"] = q(
        "SELECT created_at, direction, body FROM lisa_sms WHERE dest9 = %s ORDER BY created_at ASC LIMIT 60", (d9,))

    # timeline: booking call + all Lisa calls + Alfred/closer calls + SMS + manual notes
    tl: list[dict] = []
    for c in q("""SELECT started_at, call_outcome, meeting_agreed, round(COALESCE(duration_ms,0)/1000.0) dur, recording_url
                  FROM lisa_calls WHERE dest9=%s ORDER BY started_at DESC LIMIT 50""", (d9,)):
        tl.append({"kind": "lisa_call", "at": c["started_at"], "body": (c["call_outcome"] or "call"),
                   "meta": {"booked": c["meeting_agreed"], "dur_s": c["dur"], "recording": c["recording_url"]}})
    for c in q("""SELECT c.started_at, c.bde_name, c.answered, c.is_voicemail, round(COALESCE(c.talk_seconds,0)) talk,
                         c.call_id, c.recording_present, cl.call_outcome
                  FROM calls c LEFT JOIN classifications cl ON cl.call_id=c.call_id
                  WHERE right(regexp_replace(COALESCE(c.dest_number,''),'[^0-9]','','g'),9)=%s AND c.bde_name = ANY(%s)
                  ORDER BY c.started_at DESC LIMIT 50""", (d9, closers or ["__none__"])):
        tl.append({"kind": "bde_call", "at": c["started_at"],
                   "body": f"{c['bde_name']}: {'answered' if c['answered'] else ('voicemail' if c['is_voicemail'] else 'no answer')}"
                           + (f" · {c['talk']}s" if c['answered'] else ""),
                   "meta": {"outcome": c["call_outcome"],
                            # BDE (Alfred) recording — streamed by the existing /api/call/{id}/recording route
                            "rec_call_id": c["call_id"] if (c.get("recording_present") and c.get("call_id")) else None}})
    for s in q("SELECT created_at, direction, left(body,300) body FROM lisa_sms WHERE dest9=%s ORDER BY created_at DESC LIMIT 30", (d9,)):
        tl.append({"kind": "sms", "at": s["created_at"], "body": f"SMS {s['direction']}: {s['body']}"})
    for a in q("SELECT created_at, kind, body, author FROM crm_activity WHERE dest9=%s ORDER BY created_at DESC LIMIT 100", (d9,)):
        tl.append({"kind": a["kind"], "at": a["created_at"], "body": a["body"], "author": a["author"]})
    tl.sort(key=lambda x: (x["at"] is not None, x["at"]), reverse=True)
    rec["timeline"] = tl
    return rec


# ---------- write: editable fields + notes ----------
_EDITABLE = {"stage", "owner", "next_action", "next_action_at", "outcome", "contact_name", "contact_email", "status", "note"}


def crm_update(pool, dest9: str, fields: dict, who: str) -> None:
    d9 = re.sub(r"[^0-9]", "", dest9 or "")[-9:]
    if not d9:
        return
    sets = {k: v for k, v in (fields or {}).items() if k in _EDITABLE}
    if sets:
        cols = ", ".join(sets.keys())
        ph = ", ".join(["%s"] * len(sets))
        upd = ", ".join(f"{k}=EXCLUDED.{k}" for k in sets)
        vals = list(sets.values())
        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                f"INSERT INTO booked_crm (dest9, {cols}, updated_by, updated_at) VALUES (%s, {ph}, %s, now()) "
                f"ON CONFLICT (dest9) DO UPDATE SET {upd}, updated_by=EXCLUDED.updated_by, updated_at=now()",
                (d9, *vals, who[:80]))
            cur.execute("INSERT INTO crm_activity (dest9, kind, body, author, meta) VALUES (%s,'system',%s,%s,%s)",
                        (d9, "updated " + ", ".join(sets.keys()), who[:80], json.dumps({k: str(v)[:200] for k, v in sets.items()})))
            conn.commit()
    # PROSPECT identity fields (name/company/email/website) live on lisa_calls — editable by admins/Alfred so a
    # booking's details can be corrected by hand (the manual override for anything auto-capture missed).
    _pm = {}
    _f = fields or {}
    if "prospect_name" in _f: _pm["prospect_name"] = (_f.get("prospect_name") or "").strip()[:120]
    if "company" in _f: _pm["company_name"] = (_f.get("company") or "").strip()[:160]
    if "email" in _f: _pm["prospect_email"] = (_f.get("email") or "").strip()[:160]
    if "website" in _f:
        _w = re.sub(r"^https?://", "", (_f.get("website") or "").strip()).strip("/").lower()
        _pm["domain"] = _w[:160]
    if _pm:
        with pool.connection() as conn, conn.cursor() as cur:
            setc = ", ".join(f"{k}=%s" for k in _pm)
            cur.execute(f"UPDATE lisa_calls SET {setc} WHERE dest9=%s", (*_pm.values(), d9))
            if "domain" in _pm:                      # keep the website-build pipeline's domain in sync
                cur.execute("UPDATE lisa4_pool SET domain=%s WHERE dest9=%s", (_pm["domain"], d9))
            if _pm.get("prospect_name"):             # mirror onto the CRM contact so it shows immediately
                cur.execute("INSERT INTO booked_crm (dest9,contact_name,updated_by,updated_at) VALUES (%s,%s,%s,now()) "
                            "ON CONFLICT (dest9) DO UPDATE SET contact_name=EXCLUDED.contact_name, updated_at=now()",
                            (d9, _pm["prospect_name"], who[:80]))
            cur.execute("INSERT INTO crm_activity (dest9,kind,body,author) VALUES (%s,'system',%s,%s)",
                        (d9, "edited prospect details: " + ", ".join(_pm.keys()), who[:80]))
            conn.commit()
    # CONFIRMED TRIGGER (Vysakh, 2026-08-19): the closer marking a booking 'confirmed' kicks off the
    # website build on autopilot (idempotent — no-op if a site is already queued/building/built). From the
    # confirm-gate cutover this is what STARTS a build; before it, builds run regardless.
    if str(sets.get("stage", "")).strip().lower() == "confirmed":
        try:
            from . import lisa4 as _l4
            if _l4.enqueue_lisa4_build(pool, d9):
                with pool.connection() as conn, conn.cursor() as cur:
                    cur.execute("INSERT INTO crm_activity (dest9, kind, body, author) VALUES (%s,'system',%s,%s)",
                                (d9, "Confirmed → website build queued (autopilot)", who[:80]))
                    conn.commit()
        except Exception:
            pass


def crm_add_note(pool, dest9: str, body: str, who: str) -> None:
    d9 = re.sub(r"[^0-9]", "", dest9 or "")[-9:]
    if not d9 or not (body or "").strip():
        return
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute("INSERT INTO crm_activity (dest9, kind, body, author) VALUES (%s,'note',%s,%s)",
                    (d9, body.strip()[:2000], who[:80]))
        conn.commit()


def ensure_quote_default(pool, dest9: str, company: str, domain: str, default_total: int = 1000):
    """Autopilot: create a DEFAULT website quotation for a booked prospect IF one doesn't exist yet, and
    link it to booked_crm so it surfaces on the booking page. NEVER overwrites an existing/edited quote.
    Fully guarded — never raises into the caller (the build loop). Returns the quote token, or None."""
    import re as _re, secrets, random
    from . import quote as _q
    d9 = _re.sub(r"[^0-9]", "", dest9 or "")[-9:]
    if not d9:
        return None
    try:
        ensure_crm_tables(pool)
        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT quote_token FROM booked_crm WHERE dest9=%s", (d9,))
            r = cur.fetchone()
            if r and (r.get("quote_token") or ""):
                return r["quote_token"]                       # already has one — leave the closer's version
            html = _q.gen_quote_html(company or "your business", domain or "", int(default_total))
            slug = _re.sub(r"[^a-z0-9]+", "-", (company or "quote").lower()).strip("-")[:28] or "quote"
            tok = f"{slug}-quote-" + secrets.token_urlsafe(8)
            synth = "9" + "".join(str(random.randint(0, 9)) for _ in range(8))
            cur.execute("INSERT INTO lisa4_sites (dest9,company,domain,html,status,share_token,kind,quote_total,built_at) "
                        "VALUES (%s,%s,%s,%s,'built',%s,'quote',%s,now())", (synth, company, domain, html, tok, int(default_total)))
            cur.execute("INSERT INTO booked_crm (dest9,quote_token,quote_total,updated_by,updated_at) "
                        "VALUES (%s,%s,%s,'autopilot',now()) ON CONFLICT (dest9) DO UPDATE SET "
                        "quote_token=EXCLUDED.quote_token, quote_total=COALESCE(booked_crm.quote_total,EXCLUDED.quote_total), updated_at=now()",
                        (d9, tok, int(default_total)))
            conn.commit()
        return tok
    except Exception:
        return None


def ensure_reveal_guide(pool, settings, dest9: str, company: str, domain: str,
                        industry: str = "", finding: str = "", quote_line: str = ""):
    """Autopilot: generate Alfred's Opus-5 reveal PLAYBOOK for a booked prospect IF one doesn't exist yet,
    and link it to booked_crm so it surfaces on the booking page. Guarded — never raises. Returns token/None."""
    import re as _re, secrets, random
    from . import reveal_guide as _rg
    d9 = _re.sub(r"[^0-9]", "", dest9 or "")[-9:]
    if not d9:
        return None
    try:
        ensure_crm_tables(pool)
        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT guideline_token FROM booked_crm WHERE dest9=%s", (d9,))
            r = cur.fetchone()
            if r and (r.get("guideline_token") or ""):
                return r["guideline_token"]
            cur.execute("SELECT k, v FROM crm_config WHERE k IN ('lisa4_designer_model','anthropic_api_key')")
            cfg = {r["k"]: r["v"] for r in cur.fetchall()}
            model = (cfg.get("lisa4_designer_model") or "claude-opus-5")
        key = (cfg.get("anthropic_api_key") or getattr(settings, "anthropic_api_key", "") or "")
        ql = quote_line or "a bespoke website build from A$900 + GST, plus web & email hosting from A$100/month + GST"
        html = _rg.gen_reveal_guide(key, model, company or "the business", domain or "", industry or "", finding or "", ql)
        if not html:
            return None
        slug = _re.sub(r"[^a-z0-9]+", "-", (company or "guide").lower()).strip("-")[:24] or "guide"
        tok = f"{slug}-guide-" + secrets.token_urlsafe(8)
        synth = "9" + "".join(str(random.randint(0, 9)) for _ in range(8))
        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute("INSERT INTO lisa4_sites (dest9,company,domain,html,status,share_token,kind,built_at) "
                        "VALUES (%s,%s,%s,%s,'built',%s,'guideline',now())", (synth, company, domain, html, tok))
            cur.execute("INSERT INTO booked_crm (dest9,guideline_token,updated_by,updated_at) "
                        "VALUES (%s,%s,'autopilot',now()) ON CONFLICT (dest9) DO UPDATE SET "
                        "guideline_token=EXCLUDED.guideline_token, updated_at=now()", (d9, tok))
            conn.commit()
        return tok
    except Exception:
        return None


def ensure_growth_audit(pool, settings, dest9: str, domain: str, company: str,
                        avg_ticket: float | None = None, force: bool = False):
    """Generate a PLAIN-LANGUAGE growth audit (Opus-5, on top of audit.assemble_audit) for a prospect whose
    domain HAS enrichment, store it (kind='audit'), and link booked_crm.audit_token so it surfaces on the
    booking page. Returns None if the domain has no enrichment (can't build a data-backed audit) or on any
    error. Guarded — never raises into the caller."""
    import re as _re, secrets, random
    d9 = _re.sub(r"[^0-9]", "", dest9 or "")[-9:]
    dom = (domain or "").strip().lower().replace("https://", "").replace("http://", "").strip("/")
    if not d9 or not dom:
        return None
    try:
        ensure_crm_tables(pool)
        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT audit_token FROM booked_crm WHERE dest9=%s", (d9,))
            r = cur.fetchone()
            if r and (r.get("audit_token") or "") and not force:
                return r["audit_token"]
            cur.execute("SELECT k, v FROM crm_config WHERE k IN ('lisa4_designer_model','anthropic_api_key')")
            cfg = {x["k"]: x["v"] for x in cur.fetchall()}
        model = cfg.get("lisa4_designer_model") or "claude-opus-5"
        key = cfg.get("anthropic_api_key") or getattr(settings, "anthropic_api_key", "") or ""
        from . import audit as _a, growth_audit as _ga
        # Firmographics ONCE — drives BOTH the service-keyword seeds (so a low-footprint domain still
        # discovers demand and the audit is never thin) and the average-job-value estimate.
        industry = sub_industry = None
        try:
            from psycopg.rows import dict_row
            with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
                cur.execute("SELECT industry, sub_industry FROM companies WHERE domain=%s "
                            "ORDER BY revenue_musd DESC NULLS LAST LIMIT 1", (dom,))
                crow = cur.fetchone() or {}
            industry, sub_industry = crow.get("industry"), crow.get("sub_industry")
        except Exception:
            pass
        try:   # full SEO dataset (paid, idempotent), SEEDED with the business's real service terms so a
               # low-footprint domain still discovers keyword demand and the report isn't thin.
            _seeds = _ga.service_seeds(settings, company or "", industry, sub_industry)
            _a.ensure_audit_data(pool, settings, dom, company or "", extra_seeds=_seeds or None)
        except Exception:
            pass
        # avg_ticket: market-grounded per-business estimate when the caller didn't supply one (recurring->
        # annual, one-off->per-job), flagged as an estimate so the report labels it honestly.
        ticket_estimated = False
        if not avg_ticket:
            avg_ticket, _bucket, _est = _ga.estimate_avg_ticket(settings, company, industry, sub_industry)
            ticket_estimated = True
        audit_model = _a.assemble_audit(pool, dom, avg_ticket=avg_ticket)
        if not audit_model:
            return None
        if ticket_estimated:   # tell the report the ticket is an industry estimate, not the owner's real number
            audit_model.setdefault("revenue", {})["avg_ticket_estimated"] = True
        html = _ga.gen_growth_audit(key, model, audit_model, avg_ticket=avg_ticket, company=company or "")
        if not html:
            return None
        # ACCURACY / THIN-DATA GATE — catch a defective or near-empty audit BEFORE it ships. We still
        # persist it (a limited-data audit is a legitimate, honestly-labelled deliverable), but we LOG the
        # issues so a thin/violating report is flagged for review rather than silently going out.
        try:
            qa = _ga.audit_qa_gate(audit_model, html)
            if not qa.get("ok"):
                from .logging import get_logger as _gl
                _gl("funnel_agent.crm").warning("growth_audit_review",
                                                domain=dom, dest9=d9, company=company or "",
                                                thin=qa.get("thin"), issues=(qa.get("issues") or [])[:12])
        except Exception:
            pass
        slug = _re.sub(r"[^a-z0-9]+", "-", (company or "growth").lower()).strip("-")[:24] or "growth"
        tok = f"{slug}-audit-" + secrets.token_urlsafe(8)
        synth = "9" + "".join(str(random.randint(0, 9)) for _ in range(8))
        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute("INSERT INTO lisa4_sites (dest9,company,domain,html,status,share_token,kind,built_at) "
                        "VALUES (%s,%s,%s,%s,'built',%s,'audit',now())", (synth, company, dom, html, tok))
            cur.execute("INSERT INTO booked_crm (dest9,audit_token,updated_by,updated_at) "
                        "VALUES (%s,%s,'autopilot',now()) ON CONFLICT (dest9) DO UPDATE SET "
                        "audit_token=EXCLUDED.audit_token, updated_at=now()", (d9, tok))
            conn.commit()
        return tok
    except Exception:
        return None


BRAND_INTRO_TOKEN = "de-group-reveal-aUGgB40aZsd9yQ"


def send_brand_intro(pool, settings, dest9: str, by: str = "Lisa",
                     base_url: str = "https://www.trmatrix.com.au") -> bool:
    """Lisa shares the DE Group brand intro to a BOOKED prospect via SMS (post-booking), ONCE — and records
    it so the closer never duplicates it. Skips if already shared or the prospect opted out. Guarded —
    never raises. Returns True only if an SMS actually went out."""
    import re as _re
    from . import lisa as _l
    d9 = _re.sub(r"[^0-9]", "", dest9 or "")[-9:]
    if not d9:
        return False
    try:
        ensure_crm_tables(pool)
        chk = _l._fetch(pool, "SELECT (SELECT brand_intro_sent_at FROM booked_crm WHERE dest9=%s) sent, "
                        "EXISTS(SELECT 1 FROM lisa_sms WHERE dest9=%s AND direction='inbound' "
                        "  AND lower(body) ~ 'stop|unsubscribe|remove|do not|opt out|wrong number') optout", (d9, d9))
        if chk and (chk[0].get("sent") or chk[0].get("optout")):
            return False
        info = _l._fetch(pool, "SELECT prospect_name FROM lisa_calls WHERE dest9=%s AND COALESCE(meeting_agreed,false) "
                         "ORDER BY started_at DESC LIMIT 1", (d9,))
        name = _l._first_name((info[0].get("prospect_name") if info else "") or "")
        to = "+61" + d9
        from . import shortlink as _sl
        link = _sl.short_url(pool, base_url.rstrip("/") + "/api/lisa4/site/public/" + BRAND_INTRO_TOKEN, dest9=d9)
        body = (f"hey{(' ' + name.lower()) if name else ''}, ahead of our chat here's a quick intro to who we "
                f"are at DE Group: {link} — looking forward to showing you what we've built. cheers, lisa")
        # SMS is PRIMARY (mobile-only); email is a SECONDARY reinforcement when we have an address on file.
        frm = _l._e164_au((list(getattr(settings, "lisa4_numbers", []) or []) or [""])[0])
        sms_ok = bool(d9.startswith("4") and _l._twilio_ready(settings) and frm
                      and _l._send_sms_twilio(settings, to, body, frm))
        if sms_ok:
            _l._log_sms(pool, "outbound", frm, to, body, d9)
        email_ok = False
        try:
            from . import emailer as _em
            addr = _em.prospect_email(pool, d9)
            if addr:
                subj, html = _em.brand_intro_email(name, link)
                email_ok = _em.send_email(settings, addr, subj, html, dest9=d9)
        except Exception:
            pass
        if not (sms_ok or email_ok):
            return False
        chan = "SMS + email" if (sms_ok and email_ok) else ("SMS" if sms_ok else "email")
        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute("INSERT INTO booked_crm (dest9,brand_intro_sent_at,brand_intro_sent_by,updated_at) "
                        "VALUES (%s,now(),%s,now()) ON CONFLICT (dest9) DO UPDATE SET "
                        "brand_intro_sent_at=now(), brand_intro_sent_by=EXCLUDED.brand_intro_sent_by, updated_at=now()", (d9, by))
            cur.execute("INSERT INTO crm_activity (dest9,kind,body,author) VALUES (%s,'system',%s,%s)",
                        (d9, f"Brand intro shared via {chan}", by))
            conn.commit()
        return True
    except Exception:
        return False


def run_booking_docs_autopilot(pool, settings, heavy_budget: int = 2) -> dict:
    """AUTOPILOT — ensure EVERY active booking reaches its full reveal document set, self-healing, with NO
    manual step (Vysakh, 2026-08-19: "it must be in autopilot… I can't check and build"). Runs continuously
    from the Lisa loop so a doc that never got created — or a doc TYPE added after the booking — still gets
    made and back-filled:
      • Website (Lisa-4): the site is queued at booking/confirm; once BUILT → old-vs-new comparison.
      • Growth  (Lisa-5): growth audit (the reveal asset).
    This pass only CREATES documents (no outbound SMS): the brand intro is a SEND, so it is NOT blasted here —
    it goes out per-new-build (once) and, for anyone who didn't show, via no-show recovery (Vysakh's rule).
    HEAVY generations (audit = DataForSEO+Opus, comparison = Chromium+Opus) are capped per pass (heavy_budget)
    so a backlog is smoothed over several passes. Only active bookings (not lost/won/no-show) are touched.
    Fully guarded — never raises into the loop."""
    from . import lisa as _l
    out = {"audit": 0, "comparison": 0, "site_queued": 0, "seen": 0}
    try:
        ensure_crm_tables(pool)
        rows = _l._fetch(pool, """
          WITH booked AS (
            SELECT DISTINCT ON (dest9) dest9, company_name, created_at,
              (from_number ~ '(468030256|489266405|495044526|468091513)' OR to_number ~ '(468030256|489266405|495044526|468091513)') AS is_lisa4,
              (from_number ~ '(468096730|468008827)' OR to_number ~ '(468096730|468008827)') AS is_lisa5
            FROM lisa_calls WHERE COALESCE(meeting_agreed,false) AND dest9 IS NOT NULL
            ORDER BY dest9, created_at ASC)
          SELECT b.dest9, b.is_lisa4, b.is_lisa5,
                 COALESCE(NULLIF(b.company_name,''), lp.company, l5.company) AS company,
                 COALESCE(lp.domain, l5.domain) AS domain,
                 bc.audit_token, bc.comparison_token, bc.brand_intro_sent_at, COALESCE(bc.stage,'') AS stage,
                 st.status AS site_status, st.share_token,
                 st.built_at AS site_built_at, cmp.built_at AS cmp_built_at
          FROM booked b
          LEFT JOIN lisa4_pool lp ON lp.dest9=b.dest9
          LEFT JOIN lisa5_pool l5 ON l5.dest9=b.dest9
          LEFT JOIN LATERAL (SELECT status, share_token, built_at FROM lisa4_sites s
                             WHERE s.dest9=b.dest9 AND s.kind IS DISTINCT FROM 'comparison'
                             ORDER BY id DESC LIMIT 1) st ON true
          LEFT JOIN booked_crm bc ON bc.dest9=b.dest9
          -- the CURRENT comparison's build time, to detect a STALE one (older than the built reveal)
          LEFT JOIN LATERAL (SELECT built_at FROM lisa4_sites c
                             WHERE c.share_token=bc.comparison_token AND COALESCE(c.kind,'')='comparison'
                             ORDER BY id DESC LIMIT 1) cmp ON true
          WHERE COALESCE(bc.stage,'') NOT IN ('lost','won','no_show')
          ORDER BY b.created_at DESC NULLS LAST
        """) or []
        out["seen"] = len(rows)
        from . import lisa4 as _l4
        for r in rows:
            d9 = r["dest9"]
            is5 = bool(r.get("is_lisa5"))
            is4 = bool(r.get("is_lisa4")) and not is5
            # 1) GROWTH audit — the reveal asset for a Lisa-5 booking (this is the "—" the closer sees)
            if is5 and not (r.get("audit_token") or "") and heavy_budget > 0:
                if ensure_growth_audit(pool, settings, d9, r.get("domain") or "", r.get("company") or ""):
                    out["audit"] += 1; heavy_budget -= 1
            # 2) WEBSITE — safety net: queue a build if one never got created for a website booking
            if is4 and not r.get("site_status"):
                if _l4.enqueue_lisa4_build(pool, d9):
                    out["site_queued"] += 1
            # 3) WEBSITE comparison — old-vs-new, once the new site is BUILT. Self-heal STALE comparisons too:
            #    if the stored comparison predates the CURRENT built reveal (the site was rebuilt/corrected
            #    after the comparison was shot), force a regen so the shots always match the live site — the
            #    safety net for when the build-time regen could not run (e.g. Chromium hiccup that pass).
            _cmp_missing = not (r.get("comparison_token") or "")
            _cmp_stale = bool(r.get("comparison_token") and r.get("site_built_at") and r.get("cmp_built_at")
                              and r["cmp_built_at"] < r["site_built_at"])
            if (is4 and r.get("site_status") == "built" and r.get("share_token")
                    and (_cmp_missing or _cmp_stale) and heavy_budget > 0):
                try:
                    from . import comparison as _cmp
                    if _cmp.ensure_comparison(pool, settings, d9, force=True):
                        out["comparison"] += 1; heavy_budget -= 1
                except Exception:
                    pass
            # 4) BRAND INTRO — send ONCE to EVERY booked prospect, autopilot (Vysakh, repeated: must not be
            #    manual, and must cover Lisa-5 audit bookings too — not just Lisa-4 site builds). send_brand_intro
            #    is idempotent (skips if already sent / opted out) and mobile-only; gate to the business-hours
            #    send window so we never text at night.
            if not r.get("brand_intro_sent_at"):
                try:
                    from . import noshow_recovery as _nsr
                    if _nsr.send_window_open() and send_brand_intro(pool, settings, d9, by="autopilot"):
                        out["brand_intro"] = out.get("brand_intro", 0) + 1
                except Exception:
                    pass
        # 5) WEBSITE FINDER — bookings that aren't in the outbound pool (inbound / referrals) have NO
        #    lisa4_pool row, so their reveal sits queued with domain=None forever and no site is built.
        #    Resolve the prospect's REAL site by phone/name/SERP, VERIFY it (phone-match = gold), and wire
        #    it in on HIGH confidence only. Self-throttled (paid SERP) + fully guarded — never blocks.
        try:
            from . import website_finder as _wf
            _wf.find_missing_websites(pool, settings, limit=5, auto_wire=True)
        except Exception as exc:
            _l.log.warning("website_finder_autopilot_failed", error=str(exc)[:140])
        return out
    except Exception as exc:
        out["error"] = str(exc)[:140]
        return out
