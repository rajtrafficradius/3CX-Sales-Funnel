"""Map Aircall call objects to the canonical call shape used by ingestion.

raw_digits is the EXTERNAL party (the prospect) on both directions, so it maps to
dest_number directly (the last-9-digit normalisation downstream reconciles +61/0 with
3CX numbers). Timestamps are Unix seconds; ring/talk are derived from them.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo


def sniff_audio(data: bytes) -> tuple[str, str]:
    """Guess (extension, mime) from an audio file's magic bytes. Aircall recordings come
    back as either WAV or MP3, so we detect rather than assume — for correct playback,
    download filename, and the STT upload name (OpenAI keys off the extension)."""
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WAVE":
        return "wav", "audio/wav"
    if data[:3] == b"ID3" or (len(data) >= 2 and data[0] == 0xFF and (data[1] & 0xE0) == 0xE0):
        return "mp3", "audio/mpeg"
    if len(data) >= 12 and data[4:8] == b"ftyp":
        return "m4a", "audio/mp4"
    if data[:4] == b"OggS":
        return "ogg", "audio/ogg"
    return "mp3", "audio/mpeg"  # safe default; browsers + OpenAI sniff content anyway


def normalize_number(raw: str | None) -> str | None:
    if not raw:
        return None
    s = re.sub(r"\s+", "", str(raw))  # Aircall returns "+61 8 9374 6072"
    return s or None


def day_window_unix(day: date, tz: str) -> tuple[int, int]:
    """[local-midnight, next local-midnight) as Unix seconds — matches 3CX day buckets."""
    zone = ZoneInfo(tz)
    start = datetime(day.year, day.month, day.day, tzinfo=zone)
    return int(start.timestamp()), int((start + timedelta(days=1)).timestamp())


def to_canonical(call: dict) -> dict:
    """Map one Aircall call to the canonical dict (call_id prefixed to avoid 3CX collisions)."""
    st = call.get("started_at")
    an = call.get("answered_at")
    en = call.get("ended_at")
    started = datetime.fromtimestamp(int(st), timezone.utc) if st else None
    answered = an is not None
    ring = (int(an) - int(st)) if (an and st) else ((int(en) - int(st)) if (en and st) else 0)
    talk = (int(en) - int(an)) if (an and en) else 0
    rec = call.get("recording")
    aid = call.get("id")
    return {
        "aircall_id": aid,
        "user": call.get("user") or {},
        "call_id": f"aircall:{aid}",
        "direction": "Outbound" if call.get("direction") == "outbound" else "Inbound",
        "dest_number": normalize_number(call.get("raw_digits")),
        "started_at": started,
        "ring_seconds": max(0, int(ring)),
        "talk_seconds": max(0, int(talk)),
        "answered": answered,
        "is_voicemail": bool(call.get("voicemail")) and not answered,
        "disposition": call.get("status") or call.get("missed_call_reason"),
        "recording_present": bool(rec),
        # store the Aircall call id; download re-fetches a fresh signed URL each time.
        "recording_id": str(aid) if rec else None,
    }
