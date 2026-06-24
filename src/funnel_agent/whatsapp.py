"""WhatsApp meeting-nurturing engine (#13) — Meta WhatsApp Cloud API.

Sequence fired when a meeting is BOOKED (a real new booking):
  1. confirm   — immediately: "your meeting is confirmed" (template `whatsapp_tpl_confirm`)
  2. value     — shortly after: "here's what you'll get" (template `whatsapp_tpl_value`)
  3. reminder  — +2 hours: authority + fear-of-missing-out, ask them to confirm; if they
                 don't, include the reschedule link (template `whatsapp_tpl_reminder`)

Meta requires PRE-APPROVED templates for business-initiated messages, so the actual
copy lives in Meta Business Manager; we only send the template name + variables. Until
WHATSAPP_ENABLED + credentials are set the engine runs in DRY-RUN: it still schedules and
records every message (status='dry_run') so the flow is fully testable without sending.

Scheduling is idempotent (unique on call_id+step). A worker (`process_due`) sends due
messages; wire it into the refresh loop or a cron, like the rest of the pipeline.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
from psycopg_pool import ConnectionPool

from .config import Settings
from .logging import get_logger
from .prospects import normalize_au_phone

log = get_logger(__name__)

# (step, template-setting attribute, delay after booking)
_SEQUENCE = [
    ("confirm",  "whatsapp_tpl_confirm",  timedelta(0)),
    ("value",    "whatsapp_tpl_value",    timedelta(minutes=15)),
    ("reminder", "whatsapp_tpl_reminder", timedelta(hours=2)),
]


def schedule_nurture_for_booking(pool: ConnectionPool, settings: Settings, call_id: str,
                                 dest_number: str, booked_at: datetime | None = None) -> int:
    """Queue the meeting-confirmation sequence for one booking. Idempotent per call+step."""
    if not dest_number:
        return 0
    base = booked_at or datetime.now(timezone.utc)
    rows = 0
    with pool.connection() as conn, conn.cursor() as cur:
        for step, tpl_attr, delay in _SEQUENCE:
            tpl = getattr(settings, tpl_attr, None)
            cur.execute(
                "INSERT INTO whatsapp_messages (call_id, dest_number, step, template, scheduled_at, status) "
                "VALUES (%s,%s,%s,%s,%s,'scheduled') ON CONFLICT (call_id, step) WHERE call_id IS NOT NULL DO NOTHING",
                (call_id, dest_number, step, tpl, base + delay),
            )
            rows += cur.rowcount
        conn.commit()
    return rows


def schedule_due_bookings(pool: ConnectionPool, settings: Settings, lookback_days: int = 3) -> dict:
    """Find recently-booked meetings with no WhatsApp sequence yet and schedule one.

    Counts only genuine new bookings (not confirmations/reschedules), matching the funnel.
    """
    rows = []
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT c.call_id, c.dest_number, c.started_at FROM calls c "
            "JOIN classifications cl ON cl.call_id=c.call_id "
            "WHERE c.in_scope AND cl.meeting_booked "
            "  AND NOT COALESCE(cl.meeting_confirmation,false) AND NOT COALESCE(cl.meeting_rescheduled,false) "
            "  AND c.started_at >= now() - make_interval(days => %s) "
            "  AND c.dest_number IS NOT NULL AND c.dest_number <> '' "
            "  AND NOT EXISTS (SELECT 1 FROM whatsapp_messages w WHERE w.call_id=c.call_id)",
            (lookback_days,),
        )
        rows = cur.fetchall()
    scheduled = 0
    for r in rows:
        scheduled += schedule_nurture_for_booking(pool, settings, r["call_id"], r["dest_number"], r["started_at"])
    log.info("whatsapp_schedule_due", bookings=len(rows), messages=scheduled)
    return {"bookings": len(rows), "messages_scheduled": scheduled}


def _send_template(settings: Settings, to_e164: str, template: str) -> tuple[bool, str | None, str | None]:
    """Send one approved template via the Meta Cloud API. Returns (ok, message_id, error)."""
    url = f"https://graph.facebook.com/{settings.whatsapp_api_version}/{settings.whatsapp_phone_number_id}/messages"
    payload = {
        "messaging_product": "whatsapp", "to": to_e164, "type": "template",
        "template": {"name": template, "language": {"code": "en"}},
    }
    headers = {"Authorization": f"Bearer {settings.whatsapp_access_token}"}
    try:
        with httpx.Client(timeout=20.0) as c:
            resp = c.post(url, json=payload, headers=headers)
        if resp.status_code >= 400:
            return False, None, f"{resp.status_code}: {resp.text[:200]}"
        data = resp.json()
        mid = (data.get("messages") or [{}])[0].get("id")
        return True, mid, None
    except Exception as exc:
        return False, None, str(exc)[:200]


def _to_e164_au(dest_number: str) -> str | None:
    """AU dialled number -> E.164 (+61…) for WhatsApp."""
    n = normalize_au_phone(dest_number)  # trailing 9 significant digits
    return f"+61{n}" if n else None


def process_due(pool: ConnectionPool, settings: Settings, limit: int = 200) -> dict:
    """Send all due WhatsApp messages. DRY-RUN (status='dry_run') unless WHATSAPP_ENABLED
    with credentials. The reminder step appends the reschedule link only when the prospect
    hasn't already confirmed (future: wire the inbound webhook to mark confirmations)."""
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT * FROM whatsapp_messages WHERE status='scheduled' AND scheduled_at <= now() "
            "ORDER BY scheduled_at LIMIT %s", (limit,))
        due = cur.fetchall()

    live = bool(settings.whatsapp_enabled and settings.whatsapp_access_token and settings.whatsapp_phone_number_id)
    sent = dry = failed = skipped = 0
    for m in due:
        to = _to_e164_au(m["dest_number"])
        if not to or not m["template"]:
            status, mid, err = "skipped", None, "no recipient/template"
            skipped += 1
        elif not live:
            status, mid, err = "dry_run", None, None
            dry += 1
        else:
            ok, mid, err = _send_template(settings, to, m["template"])
            status = "sent" if ok else "failed"
            sent += int(ok); failed += int(not ok)
        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE whatsapp_messages SET status=%s, sent_at=now(), wa_message_id=%s, error=%s WHERE id=%s",
                (status, mid, err, m["id"]))
            conn.commit()
    out = {"due": len(due), "sent": sent, "dry_run": dry, "failed": failed, "skipped": skipped, "live": live}
    log.info("whatsapp_process_due", **out)
    return out
