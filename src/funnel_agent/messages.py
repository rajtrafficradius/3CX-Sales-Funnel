"""Inbound SMS / chat capture from 3CX -> classify -> firm up bookings.

A prospect can confirm / reschedule / cancel a meeting (or opt out) by TEXTING a 3CX number.
3CX surfaces these as chat messages (ChatMessagesHistoryView, IsExternal=true). We ingest new
inbound messages, link them to the prospect by sender phone, classify the intent with an LLM,
and for a BOOKING CONFIRMATION we FIRM UP that prospect's most recent prior call booking so it
counts in the funnel (attributed to the BDE who called them, deduped by the funnel as usual).
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from psycopg_pool import ConnectionPool

from .config import Settings
from .classify.memory import dest9
from .logging import get_logger

log = get_logger(__name__)

_MSG_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "intent": {"type": "string",
                   "enum": ["booking_confirmation", "reschedule", "cancel", "optout", "reply", "other"]},
        "is_booking_confirmation": {"type": "boolean"},
        "meeting_datetime": {"type": ["string", "null"]},
    },
    "required": ["intent", "is_booking_confirmation", "meeting_datetime"],
}

_SYS = (
    "You classify a SHORT inbound SMS/chat that a B2B prospect sent to a sales team's number. "
    "Decide the intent. Set is_booking_confirmation=true ONLY when the prospect is confirming or "
    "agreeing a meeting (a marketing strategy session / audit / appointment) at a SPECIFIC time, "
    "and put that time in meeting_datetime in their own words (e.g. 'Thu 9 July 2026 10:00 AM'). "
    "intent values: 'booking_confirmation' (agreeing/confirming a specific appointment time), "
    "'reschedule' (move an existing meeting to a new time), 'cancel' (call off a meeting), "
    "'optout' (asked to stop contact / unsubscribe / do not call / remove me), 'reply' (a generic "
    "reply, e.g. 'call me later', 'thanks'), 'other'. If no concrete appointment time is being "
    "agreed, is_booking_confirmation MUST be false."
)


def _watermark(pool: ConnectionPool):
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT max(time_sent) AS m FROM messages")
        r = cur.fetchone()
    return r["m"] if r and r.get("m") else None


def ingest_messages(client, pool: ConnectionPool, settings: Settings, *, overlap_minutes: int = 10) -> dict:
    """Poll 3CX for new inbound chat/SMS and upsert into `messages` (deduped by message_id)."""
    wm = _watermark(pool)
    since = None
    if wm:
        since = (wm.astimezone(timezone.utc) - timedelta(minutes=overlap_minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")
    new = 0
    for m in client.iter_chat_messages(since):
        mid = str(m.get("MessageId") or "").strip()
        body = (m.get("Message") or "").strip()
        if not mid or not body:
            continue
        phone = (m.get("SenderParticipantPhone") or m.get("SenderParticipantNo") or "").strip()
        d9 = dest9(phone)
        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO messages (message_id, conversation_id, sender_phone, sender_no, "
                "  sender_name, sender_email, recipient, dest9, body, time_sent, direction, provider) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'inbound','3cx') "
                "ON CONFLICT (message_id) DO NOTHING",
                (mid, str(m.get("ConversationId") or "") or None, phone or None,
                 m.get("SenderParticipantNo"), m.get("SenderParticipantName"),
                 m.get("SenderParticipantEmail"), m.get("Recipients"), d9 or None,
                 body, m.get("TimeSent")))
            new += int(cur.rowcount or 0)
            conn.commit()
    log.info("ingest_messages", new=new, since=since or "all")
    return {"new": new}


def _bde_for_dest9(pool: ConnectionPool, d9: str) -> str | None:
    """The BDE who most recently called this number — to attribute the SMS booking."""
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT COALESCE(bde_name, bde_extension) AS bde FROM calls "
            "WHERE in_scope AND right(regexp_replace(dest_number,'[^0-9]','','g'),9)=%s "
            "ORDER BY started_at DESC LIMIT 1", (d9,))
        r = cur.fetchone()
    return r["bde"] if r else None


def _firm_up_booking(pool: ConnectionPool, settings: Settings, d9: str,
                     meeting_datetime: str | None) -> str | None:
    """Promote the prospect's most recent prior call to a FIRM booking (SMS confirmation) so it
    counts. Returns the call_id firmed, or None if no prior call exists. Re-aggregates that day."""
    from .aggregate import aggregate_day
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT c.call_id, c.started_at FROM calls c "
            "JOIN classifications cl ON cl.call_id=c.call_id "
            "WHERE c.in_scope AND right(regexp_replace(c.dest_number,'[^0-9]','','g'),9)=%s "
            "  AND c.started_at > now() - interval '120 days' "
            "ORDER BY c.started_at DESC LIMIT 1", (d9,))
        row = cur.fetchone()
        if not row:
            return None
        cur.execute(
            "UPDATE classifications SET meeting_booked=true, booking_status='firm', "
            "  meeting_datetime=COALESCE(%s, meeting_datetime) WHERE call_id=%s",
            (meeting_datetime, row["call_id"]))
        conn.commit()
    day = row["started_at"].date() if row.get("started_at") else None
    if day:
        try:
            aggregate_day(pool, settings, day)
        except Exception as exc:
            log.warning("firm_up_aggregate_failed", error=str(exc)[:120])
    return row["call_id"]


def _classify_text(oai, model: str, body: str) -> dict:
    resp = oai.chat.completions.create(
        model=model,
        messages=[{"role": "system", "content": _SYS}, {"role": "user", "content": body[:1500]}],
        response_format={"type": "json_schema",
                         "json_schema": {"name": "msg_intent", "schema": _MSG_SCHEMA, "strict": True}},
        temperature=0,
    )
    return json.loads(resp.choices[0].message.content)


def classify_messages(pool: ConnectionPool, settings: Settings, *, limit: int = 100) -> dict:
    """Classify unclassified inbound messages; firm up prior bookings on confirmations."""
    from .transcribe import _openai
    with pool.connection() as conn, conn.cursor() as cur:
        # Newest-first + a recency window: a fresh booking-confirmation SMS is classified
        # immediately rather than queueing behind years of historical chat; ancient messages
        # (pre-window) are skipped (no LLM cost, no booking value).
        cur.execute("SELECT message_id, dest9, body FROM messages WHERE NOT classified "
                    "AND time_sent > now() - make_interval(days => %s) "
                    "ORDER BY time_sent DESC LIMIT %s",
                    (settings.message_classify_days, limit))
        rows = cur.fetchall()
    if not rows:
        return {"classified": 0, "bookings": 0}
    oai = _openai(settings)
    model = getattr(settings, "llm_model_cheap", "") or "gpt-4o-mini"
    classified = bookings = 0
    for m in rows:
        try:
            res = _classify_text(oai, model, m["body"])
        except Exception as exc:
            log.warning("classify_message_failed", message_id=m["message_id"], error=str(exc)[:140])
            continue
        intent = res.get("intent")
        is_conf = bool(res.get("is_booking_confirmation"))
        mdt = res.get("meeting_datetime")
        d9 = m.get("dest9")
        bde = _bde_for_dest9(pool, d9) if d9 else None
        applied = _firm_up_booking(pool, settings, d9, mdt) if (is_conf and d9) else None
        if applied:
            bookings += 1
        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE messages SET classified=true, intent=%s, is_booking_confirmation=%s, "
                "  meeting_datetime=%s, bde_name=%s, applied_call_id=%s, model=%s WHERE message_id=%s",
                (intent, is_conf, mdt, bde, applied, model, m["message_id"]))
            conn.commit()
        classified += 1
    log.info("classify_messages", classified=classified, bookings=bookings)
    return {"classified": classified, "bookings": bookings}


def process_messages(client, pool: ConnectionPool, settings: Settings) -> dict:
    """One cycle: ingest new inbound 3CX messages, then classify + firm up bookings. Never raises
    fatally upward — the caller wraps it so a messaging hiccup can't stall the refresh loop."""
    ing = ingest_messages(client, pool, settings)
    cls = classify_messages(pool, settings)
    return {**ing, **cls}
