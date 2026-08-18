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


# ---------- read: the enriched booked list ----------
LIST_SQL = """
WITH booked AS (
  SELECT DISTINCT ON (dest9) dest9, call_id, company_name, prospect_name, prospect_email,
         agreed_day_time, created_at AS booked_at,
         (right(regexp_replace(COALESCE(from_number,''),'[^0-9]','','g'),9) = dest9) AS inbound,
         (from_number ~ '(468030256|489266405|495044526)' OR to_number ~ '(468030256|489266405|495044526)') AS is_lisa4,
         (from_number ~ '(468096730|468008827|468091513)' OR to_number ~ '(468096730|468008827|468091513)') AS is_lisa5
  FROM lisa_calls WHERE COALESCE(meeting_agreed,false) AND dest9 IS NOT NULL
  ORDER BY dest9, created_at ASC)
SELECT b.dest9, b.call_id, b.agreed_day_time, b.booked_at, b.inbound,
       CASE WHEN b.is_lisa5 THEN 'lisa5' WHEN b.is_lisa4 THEN 'lisa4' ELSE 'lisa1' END AS agent,
       COALESCE(NULLIF(bc.contact_name,''), NULLIF(b.prospect_name,''), cl.prospect_contact_name) AS contact_name,
       COALESCE(NULLIF(bc.contact_email,''), NULLIF(b.prospect_email,''), cl.prospect_email) AS contact_email,
       COALESCE(NULLIF(b.company_name,''), lp.company, lb.company_name, cl.prospect_company, '(unknown)') AS company,
       COALESCE(lp.domain, lb.domain, cl.prospect_website) AS domain,
       COALESCE(NULLIF(cl.problem_summary,''), NULLIF(lp.issue,'')) AS finding,
       (SELECT recording_url FROM lisa_calls WHERE call_id=b.call_id) AS recording_url,
       (SELECT count(*) FROM lisa_calls lc WHERE lc.dest9=b.dest9) AS total_calls,
       (SELECT ce.start_at FROM calendar_events ce WHERE ce.dest_number LIKE '%%'||b.dest9
          AND ce.type IN ('reveal','meeting') ORDER BY (ce.status='pending') DESC, ce.start_at DESC LIMIT 1) AS meeting_at,
       st.status AS site_status, st.share_token,
       bc.status AS crm_status, bc.stage, bc.owner, bc.next_action, bc.next_action_at, bc.outcome,
       bc.note AS crm_note, bc.updated_by AS crm_by, bc.updated_at AS crm_at,
       (SELECT count(*) FROM calls c WHERE right(regexp_replace(COALESCE(c.dest_number,''),'[^0-9]','','g'),9)=b.dest9
          AND c.bde_name = ANY(%(closers)s)) AS bde_attempts,
       (SELECT max(c.started_at) FROM calls c WHERE right(regexp_replace(COALESCE(c.dest_number,''),'[^0-9]','','g'),9)=b.dest9
          AND c.bde_name = ANY(%(closers)s) AND c.answered) AS last_contacted,
       (SELECT left(body,160) FROM lisa_sms s WHERE s.dest9=b.dest9 AND s.direction='inbound'
        ORDER BY created_at DESC LIMIT 1) AS last_in_body,
       ((SELECT max(created_at) FROM lisa_sms s WHERE s.dest9=b.dest9 AND s.direction='inbound')
        > COALESCE(bc.replies_seen_at, 'epoch'::timestamptz)) AS unread,
       (SELECT call_summary FROM lisa_calls WHERE call_id=b.call_id) AS booking_summary
FROM booked b
LEFT JOIN lisa4_pool lp ON lp.dest9=b.dest9
LEFT JOIN lisa_briefs lb ON lb.dest9=b.dest9
LEFT JOIN classifications cl ON cl.call_id=b.call_id
LEFT JOIN LATERAL (SELECT status, share_token FROM lisa4_sites s WHERE s.dest9=b.dest9 ORDER BY id DESC LIMIT 1) st ON true
LEFT JOIN booked_crm bc ON bc.dest9=b.dest9
ORDER BY b.booked_at DESC
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


def crm_rows(q, closers: list[str]) -> list[dict]:
    rows = q(LIST_SQL, {"closers": closers or ["__none__"]})
    for r in rows:
        r["stage"] = _derived_stage(r)
    return rows


# ---------- read: one full record + timeline ----------
def crm_record(q, dest9: str, closers: list[str]) -> dict:
    d9 = re.sub(r"[^0-9]", "", dest9 or "")[-9:]
    base = q(LIST_SQL + "\n", {"closers": closers or ["__none__"]})
    rec = next((r for r in base if r["dest9"] == d9), None)
    if not rec:
        return {}
    rec["transcript"] = (q("SELECT transcript FROM lisa_calls WHERE call_id=%s", (rec["call_id"],)) or [{}])[0].get("transcript")
    # timeline: booking call + all Lisa calls + Alfred/closer calls + SMS + manual notes
    tl: list[dict] = []
    for c in q("""SELECT started_at, call_outcome, meeting_agreed, round(COALESCE(duration_ms,0)/1000.0) dur, recording_url
                  FROM lisa_calls WHERE dest9=%s ORDER BY started_at DESC LIMIT 50""", (d9,)):
        tl.append({"kind": "lisa_call", "at": c["started_at"], "body": (c["call_outcome"] or "call"),
                   "meta": {"booked": c["meeting_agreed"], "dur_s": c["dur"], "recording": c["recording_url"]}})
    for c in q("""SELECT c.started_at, c.bde_name, c.answered, c.is_voicemail, round(COALESCE(c.talk_seconds,0)) talk, cl.call_outcome
                  FROM calls c LEFT JOIN classifications cl ON cl.call_id=c.call_id
                  WHERE right(regexp_replace(COALESCE(c.dest_number,''),'[^0-9]','','g'),9)=%s AND c.bde_name = ANY(%s)
                  ORDER BY c.started_at DESC LIMIT 50""", (d9, closers or ["__none__"])):
        tl.append({"kind": "bde_call", "at": c["started_at"],
                   "body": f"{c['bde_name']}: {'answered' if c['answered'] else ('voicemail' if c['is_voicemail'] else 'no answer')}"
                           + (f" · {c['talk']}s" if c['answered'] else ""),
                   "meta": {"outcome": c["call_outcome"]}})
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
    sets = {k: v for k, v in (fields or {}).items() if k in _EDITABLE}
    if not d9 or not sets:
        return
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


def crm_add_note(pool, dest9: str, body: str, who: str) -> None:
    d9 = re.sub(r"[^0-9]", "", dest9 or "")[-9:]
    if not d9 or not (body or "").strip():
        return
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute("INSERT INTO crm_activity (dest9, kind, body, author) VALUES (%s,'note',%s,%s)",
                    (d9, body.strip()[:2000], who[:80]))
        conn.commit()
