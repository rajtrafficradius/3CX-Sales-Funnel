"""Transcribe Aircall recordings that don't yet have a transcript.

Aircall has no inline transcript, so every recorded in-scope call is transcribed here
(download the recording -> OpenAI STT), mirroring the 3CX STT fallback. Idempotent:
a call with a transcript is skipped. Reuses transcribe_wav (Aircall recordings are
16 kHz PCM WAV, so chunked de-looping applies as for 3CX).
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, time, timedelta

from psycopg_pool import ConnectionPool

from ..config import Settings
from ..logging import get_logger
from ..transcribe import _MAX_BYTES, _openai, transcribe_wav
from .api import AircallClient
from .calls import sniff_audio

log = get_logger(__name__)


def _pending(pool: ConnectionPool, day: date, min_age_minutes: int, limit: int | None) -> list[dict]:
    start = datetime.combine(day, time.min)
    end = start + timedelta(days=1)
    sql = (
        "SELECT call_id, recording_id FROM calls "
        "WHERE provider = 'aircall' AND started_at >= %(s)s AND started_at < %(e)s "
        "AND in_scope AND recording_id IS NOT NULL AND NOT has_transcript "
        "AND started_at < now() - make_interval(mins => %(age)s) "
        "ORDER BY started_at"
    )
    params: dict = {"s": start, "e": end, "age": min_age_minutes}
    if limit:
        sql += " LIMIT %(lim)s"
        params["lim"] = limit
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def transcribe_missing_aircall(
    client: AircallClient,
    pool: ConnectionPool,
    settings: Settings,
    day: date,
    limit: int | None = None,
    workers: int | None = None,
) -> dict:
    """Download + transcribe Aircall recordings missing a transcript for a day."""
    pending = _pending(pool, day, settings.transcribe_min_age_minutes, limit)
    if not pending:
        return {"transcribed": 0, "skipped": 0, "errors": 0}

    workers = workers or settings.transcribe_workers
    oai = _openai(settings)

    def work(row: dict) -> str:
        try:
            audio = client.download_recording(row["recording_id"])
            if not audio or len(audio) > _MAX_BYTES:
                return "skip"
            ext, _ = sniff_audio(audio)
            text = transcribe_wav(oai, settings, audio, filename=f"recording.{ext}")
            if not text:
                return "skip"
            with pool.connection() as conn, conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO transcripts (call_id, source, diarized, text, sentiment, summary) "
                    "VALUES (%s, 'openai-stt', false, %s, NULL, NULL) "
                    "ON CONFLICT (call_id) DO UPDATE SET text = EXCLUDED.text, source = EXCLUDED.source",
                    (row["call_id"], text),
                )
                cur.execute("UPDATE calls SET has_transcript = true WHERE call_id = %s", (row["call_id"],))
                conn.commit()
            return "ok"
        except Exception as exc:
            log.warning("aircall_transcribe_failed", call_id=row.get("call_id"), error=str(exc)[:200])
            return "err"

    transcribed = skipped = errors = 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for status in ex.map(work, pending):
            if status == "ok":
                transcribed += 1
            elif status == "skip":
                skipped += 1
            else:
                errors += 1

    log.info("aircall_transcribe_done", day=str(day), transcribed=transcribed,
             skipped=skipped, errors=errors)
    return {"transcribed": transcribed, "skipped": skipped, "errors": errors}
