"""Phase E — ingest one day of CDR + transcripts into the analytics DB.

Idempotent: every row upserts on PK `call_id`, so re-ingesting a day changes
nothing. A no-transcript call is still recorded (Calls Made) but flagged
`has_transcript = false` so the classifier skips it forever.
"""

from __future__ import annotations

from datetime import date

from psycopg_pool import ConnectionPool

from .config import Settings
from .logging import get_logger
from .threecx.api import ThreeCXClient
from .threecx.cdr import earliest_outbound_times, fetch_calls_for_day
from .threecx.recordings import fetch_outbound_calls_for_day
from .threecx.transcripts import fetch_transcripts_for_calls

log = get_logger(__name__)

# Best-effort voicemail markers in the CDR disposition (override per instance).
_VOICEMAIL_HINTS = ("voicemail", "vmail", "vm ")


def _load_roster(pool: ConnectionPool) -> dict[str, dict]:
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT extension, bde_name, in_scope, active FROM bde_agents")
        return {str(r["extension"]): r for r in cur.fetchall()}


def _is_voicemail(disposition: str | None) -> bool:
    if not disposition:
        return False
    d = disposition.lower()
    return any(h in d for h in _VOICEMAIL_HINTS)


def ingest_day(
    source_pool: ConnectionPool,
    analytics_pool: ConnectionPool,
    settings: Settings,
    day: date,
) -> dict:
    """Ingest all outbound calls for `day`. Returns simple counts."""
    cdr = settings.cdr
    roster = _load_roster(analytics_pool)

    rows = fetch_calls_for_day(source_pool, cdr, day, outbound_only=True)
    if not rows:
        log.info("ingest_day_empty", day=str(day))
        return {"calls": 0, "transcribed": 0, "in_scope": 0}

    call_ids = [str(r["call_id"]) for r in rows]
    dest_numbers = sorted({str(r["dest_number"]) for r in rows if r["dest_number"]})
    earliest = earliest_outbound_times(source_pool, cdr, dest_numbers)
    transcripts = fetch_transcripts_for_calls(source_pool, settings.transcript, call_ids)

    n_transcribed = 0
    n_in_scope = 0

    with analytics_pool.connection() as conn:
        with conn.cursor() as cur:
            for r in rows:
                call_id = str(r["call_id"])
                ext = str(r["bde_extension"]) if r["bde_extension"] is not None else None
                dest = str(r["dest_number"]) if r["dest_number"] else None
                talk = int(r["talk_seconds"] or 0)
                disposition = r.get("disposition")

                agent = roster.get(ext or "")
                in_scope = bool(agent and agent["in_scope"] and agent["active"])
                bde_name = agent["bde_name"] if agent else None
                if in_scope:
                    n_in_scope += 1

                # Fresh = this is the earliest outbound call to that number in all history.
                first_time = earliest.get(dest or "")
                fresh_or_followup = (
                    "fresh"
                    if (dest and first_time is not None and r["started_at"] == first_time)
                    else "followup"
                )

                tr = transcripts.get(call_id)
                has_transcript = tr is not None
                if has_transcript:
                    n_transcribed += 1

                cur.execute(
                    """
                    INSERT INTO calls (
                        call_id, bde_extension, bde_name, direction, dest_number,
                        started_at, ring_seconds, talk_seconds, answered, is_voicemail,
                        call_type, recording_present, has_transcript, fresh_or_followup,
                        in_scope, lead_id)
                    VALUES (
                        %(call_id)s, %(ext)s, %(bde_name)s, %(direction)s, %(dest)s,
                        %(started)s, %(ring)s, %(talk)s, %(answered)s, %(voicemail)s,
                        %(call_type)s, %(rec)s, %(has_t)s, %(ff)s, %(in_scope)s, %(lead_id)s)
                    ON CONFLICT (call_id) DO UPDATE SET
                        bde_extension = EXCLUDED.bde_extension,
                        bde_name = EXCLUDED.bde_name,
                        direction = EXCLUDED.direction,
                        dest_number = EXCLUDED.dest_number,
                        started_at = EXCLUDED.started_at,
                        ring_seconds = EXCLUDED.ring_seconds,
                        talk_seconds = EXCLUDED.talk_seconds,
                        answered = EXCLUDED.answered,
                        is_voicemail = EXCLUDED.is_voicemail,
                        call_type = EXCLUDED.call_type,
                        recording_present = EXCLUDED.recording_present,
                        has_transcript = EXCLUDED.has_transcript,
                        fresh_or_followup = EXCLUDED.fresh_or_followup,
                        in_scope = EXCLUDED.in_scope,
                        lead_id = EXCLUDED.lead_id
                    """,
                    {
                        "call_id": call_id,
                        "ext": ext,
                        "bde_name": bde_name,
                        "direction": r.get("direction"),
                        "dest": dest,
                        "started": r["started_at"],
                        "ring": int(r["ring_seconds"] or 0),
                        "talk": talk,
                        "answered": talk > 0,
                        "voicemail": _is_voicemail(disposition),
                        "call_type": disposition,
                        "rec": has_transcript or None,
                        "has_t": has_transcript,
                        "ff": fresh_or_followup,
                        "in_scope": in_scope,
                        "lead_id": None,
                    },
                )

                if tr is not None:
                    cur.execute(
                        """
                        INSERT INTO transcripts (call_id, source, diarized, text, sentiment, summary)
                        VALUES (%(call_id)s, '3cx', %(diarized)s, %(text)s, %(sentiment)s, %(summary)s)
                        ON CONFLICT (call_id) DO UPDATE SET
                            diarized = EXCLUDED.diarized,
                            text = EXCLUDED.text,
                            sentiment = EXCLUDED.sentiment,
                            summary = EXCLUDED.summary
                        """,
                        {
                            "call_id": call_id,
                            "diarized": tr.get("diarized"),
                            "text": tr.get("text"),
                            "sentiment": tr.get("sentiment"),
                            "summary": tr.get("summary"),
                        },
                    )
        conn.commit()

    log.info(
        "ingest_day_done",
        day=str(day),
        calls=len(rows),
        transcribed=n_transcribed,
        in_scope=n_in_scope,
    )
    return {"calls": len(rows), "transcribed": n_transcribed, "in_scope": n_in_scope}


def recompute_fresh_followup(analytics_pool: ConnectionPool, dest_numbers: list[str]) -> None:
    """Deterministically label Fresh/Followup for the given numbers.

    The earliest call to a number (across everything ingested) is Fresh; the rest
    are Followup. Recomputed each run -> order-independent and idempotent.
    """
    nums = [n for n in dest_numbers if n]
    if not nums:
        return
    with analytics_pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            UPDATE calls c SET fresh_or_followup = sub.ff FROM (
                SELECT call_id,
                       CASE WHEN row_number() OVER (
                           PARTITION BY dest_number ORDER BY started_at, call_id) = 1
                            THEN 'fresh' ELSE 'followup' END AS ff
                FROM calls WHERE dest_number = ANY(%s)
            ) sub
            WHERE c.call_id = sub.call_id AND c.fresh_or_followup IS DISTINCT FROM sub.ff
            """,
            (nums,),
        )
        conn.commit()


def ingest_day_api(
    client: ThreeCXClient,
    analytics_pool: ConnectionPool,
    settings: Settings,
    day: date,
) -> dict:
    """Ingest one day of outbound calls + transcripts from the 3CX API. Idempotent."""
    roster = _load_roster(analytics_pool)
    rows = fetch_outbound_calls_for_day(client, day, settings.tz)
    if not rows:
        log.info("ingest_api_empty", day=str(day))
        return {"calls": 0, "transcribed": 0, "in_scope": 0}

    n_transcribed = n_in_scope = 0
    numbers: set[str] = set()

    with analytics_pool.connection() as conn:
        with conn.cursor() as cur:
            for r in rows:
                ext = r["bde_extension"]
                dest = r["dest_number"]
                if dest:
                    numbers.add(dest)
                agent = roster.get(ext or "")
                in_scope = bool(agent and agent["in_scope"] and agent["active"])
                bde_name = agent["bde_name"] if agent else None
                if in_scope:
                    n_in_scope += 1
                has_t = r["has_transcript"]
                if has_t:
                    n_transcribed += 1

                cur.execute(
                    """
                    INSERT INTO calls (
                        call_id, bde_extension, bde_name, direction, dest_number,
                        started_at, ring_seconds, talk_seconds, answered, is_voicemail,
                        call_type, recording_present, has_transcript, fresh_or_followup,
                        in_scope, lead_id)
                    VALUES (
                        %(call_id)s, %(ext)s, %(bde_name)s, %(direction)s, %(dest)s,
                        %(started)s, %(ring)s, %(talk)s, %(answered)s, %(voicemail)s,
                        %(call_type)s, %(rec)s, %(has_t)s, 'fresh', %(in_scope)s, NULL)
                    ON CONFLICT (call_id) DO UPDATE SET
                        bde_extension = EXCLUDED.bde_extension, bde_name = EXCLUDED.bde_name,
                        direction = EXCLUDED.direction, dest_number = EXCLUDED.dest_number,
                        started_at = EXCLUDED.started_at, ring_seconds = EXCLUDED.ring_seconds,
                        talk_seconds = EXCLUDED.talk_seconds, answered = EXCLUDED.answered,
                        is_voicemail = EXCLUDED.is_voicemail, call_type = EXCLUDED.call_type,
                        recording_present = EXCLUDED.recording_present,
                        has_transcript = EXCLUDED.has_transcript, in_scope = EXCLUDED.in_scope
                    """,
                    {
                        "call_id": r["call_id"], "ext": ext, "bde_name": bde_name,
                        "direction": r["direction"], "dest": dest, "started": r["started_at"],
                        "ring": r["ring_seconds"], "talk": r["talk_seconds"],
                        "answered": r["answered"], "voicemail": r["is_voicemail"],
                        "call_type": r["disposition"], "has_t": has_t, "in_scope": in_scope,
                        "rec": r.get("recording_present", False),
                    },
                )
                if has_t and r["transcript"]:
                    t = r["transcript"]
                    cur.execute(
                        """
                        INSERT INTO transcripts (call_id, source, diarized, text, sentiment, summary)
                        VALUES (%(cid)s, '3cx', %(d)s, %(text)s, %(sent)s, %(sum)s)
                        ON CONFLICT (call_id) DO UPDATE SET
                            diarized = EXCLUDED.diarized, text = EXCLUDED.text,
                            sentiment = EXCLUDED.sentiment, summary = EXCLUDED.summary
                        """,
                        {"cid": r["call_id"], "d": t["diarized"], "text": t["text"],
                         "sent": t["sentiment"], "sum": t["summary"]},
                    )
        conn.commit()

    recompute_fresh_followup(analytics_pool, list(numbers))
    log.info("ingest_api_done", day=str(day), calls=len(rows),
             transcribed=n_transcribed, in_scope=n_in_scope)
    return {"calls": len(rows), "transcribed": n_transcribed, "in_scope": n_in_scope}
