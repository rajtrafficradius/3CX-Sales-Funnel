"""Audio-transcription fallback.

When 3CX has a call recording but never produced a transcript, download the WAV
from 3CX (Pbx.DownloadRecording) and transcribe it ourselves with the configured
OpenAI speech-to-text model. This closes 3CX's transcription gaps so connected
calls get analysed. Cheap (a few tenths of a cent per minute) and idempotent —
once a call has a transcript it is skipped.
"""

from __future__ import annotations

import io
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, time, timedelta

from psycopg_pool import ConnectionPool

from .config import Settings
from .logging import get_logger
from .threecx.api import ThreeCXClient

log = get_logger(__name__)

_MAX_BYTES = 25 * 1024 * 1024  # OpenAI transcription upload limit


def _openai(settings: Settings):
    from openai import OpenAI

    return OpenAI(api_key=settings.llm_api_key)


def _deloop(text: str) -> str:
    """Collapse repeated on-hold / IVR loops in a Whisper transcript.

    Hold music + IVR jingles ("Please hold… shop online at…") repeat verbatim while a
    caller is on hold; Whisper transcribes each loop, which can drown out the real
    decision-maker conversation. Cap each DISTINCT sentence at 2 occurrences — this
    strips the 7×-looped jingle while NEVER dropping a unique sentence (so the actual
    conversation, incl. the booking, is always preserved).
    """
    counts: dict[str, int] = {}
    out: list[str] = []
    for s in re.split(r"(?<=[.?!])\s+", text):
        key = s.strip().lower()
        if not key:
            continue
        counts[key] = counts.get(key, 0) + 1
        if counts[key] <= 2:
            out.append(s)
    return " ".join(out)


def _wav_chunks(wav: bytes, chunk_seconds: int = 120) -> list[bytes] | None:
    """Split a PCM WAV into ~chunk_seconds pieces (no ffmpeg — stdlib `wave`).

    Returns None if the bytes aren't a parseable PCM WAV (caller falls back to a
    single-shot transcribe)."""
    import wave as wavemod
    try:
        w = wavemod.open(io.BytesIO(wav), "rb")
        fr, nf, ch, sw = w.getframerate(), w.getnframes(), w.getnchannels(), w.getsampwidth()
    except Exception:
        return None
    if fr <= 0 or nf <= 0:
        return None
    per = fr * chunk_seconds
    out: list[bytes] = []
    for i in range(0, nf, per):
        w.setpos(i)
        frames = w.readframes(min(per, nf - i))
        buf = io.BytesIO()
        ww = wavemod.open(buf, "wb")
        ww.setnchannels(ch); ww.setsampwidth(sw); ww.setframerate(fr)
        ww.writeframes(frames); ww.close()
        out.append(buf.getvalue())
    return out


def transcribe_wav(oai, settings: Settings, wav: bytes) -> str:
    """Transcribe a recording, robust to long mid-call hold music.

    Whisper falls into a repetition ("looping") failure mode on hold music: on a long
    call it transcribes the looped hold jingle and DROPS the real decision-maker
    conversation after it — making a booked call look gatekeeper-only. Two defences:
    (1) split calls >2min into ~2min chunks transcribed INDEPENDENTLY, so the post-hold
    conversation can't be swallowed by an earlier loop; (2) temperature=0 + an English
    hint reduce looping within a chunk. Finally de-loop to strip repeated hold jingles.
    """
    def _one(b: bytes) -> str:
        bio = io.BytesIO(b)
        bio.name = "recording.wav"
        resp = oai.audio.transcriptions.create(
            model=settings.transcribe_model, file=bio, temperature=0, language="en")
        return (resp.text or "").strip()

    chunks = _wav_chunks(wav, 120)
    text = " ".join(_one(b) for b in chunks if b) if (chunks and len(chunks) > 1) else _one(wav)
    return _deloop(text)


def _pending(pool: ConnectionPool, day: date, limit: int | None, min_age_minutes: int) -> list[dict]:
    """In-scope calls that have a recording but no transcript, for one day.

    Skips calls younger than `min_age_minutes` so 3CX gets a chance to transcribe
    recent calls for free (we only pay to fill genuine gaps / on intraday refresh).
    """
    start = datetime.combine(day, time.min)
    end = start + timedelta(days=1)
    sql = (
        "SELECT call_id, recording_id FROM calls "
        "WHERE started_at >= %(s)s AND started_at < %(e)s AND in_scope "
        "AND recording_id IS NOT NULL AND NOT has_transcript "
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


def transcribe_missing_for_day(
    client: ThreeCXClient,
    pool: ConnectionPool,
    settings: Settings,
    day: date,
    limit: int | None = None,
    workers: int | None = None,
) -> dict:
    """Download + transcribe recordings missing a transcript for a day. Idempotent."""
    pending = _pending(pool, day, limit, settings.transcribe_min_age_minutes)
    if not pending:
        return {"transcribed": 0, "skipped": 0, "errors": 0}

    workers = workers or settings.transcribe_workers
    oai = _openai(settings)
    client._auth_header()  # warm the token before fanning out across threads

    def work(row: dict) -> str:
        try:
            wav = client.download_recording(row["recording_id"])
            if not wav or len(wav) > _MAX_BYTES:
                return "skip"
            text = transcribe_wav(oai, settings, wav)
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
            log.warning("transcribe_failed", call_id=row.get("call_id"), error=str(exc)[:200])
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

    log.info("transcribe_day_done", day=str(day), transcribed=transcribed,
             skipped=skipped, errors=errors)
    return {"transcribed": transcribed, "skipped": skipped, "errors": errors}
