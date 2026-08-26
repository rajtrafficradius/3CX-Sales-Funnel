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


def _contact_name(c: dict) -> str:
    """'First Last' from an Aircall contact object (either half may be missing)."""
    fn = (c.get("first_name") or "").strip()
    ln = (c.get("last_name") or "").strip()
    return (fn + " " + ln).strip()


def call_meta(call: dict) -> dict:
    """Extract Alfred's ACTION on the call — his typed comments, the tags he set, and the linked contact.

    These are the IMMEDIATE closer signal (no STT needed): comments are Alfred's own written summary, tags
    are his disposition, and the contact carries the prospect's name/email/company. GET /calls and
    GET /calls/{id} both return `comments` (array of {content, posted_by, posted_at}), `tags` (array of
    {name,…}), and a `contact` object. Fully defensive: any missing / oddly-shaped field degrades to empty
    and never raises. Returns a dict ready to persist into aircall_call_meta."""
    comments: list[dict] = []
    for cm in (call.get("comments") or []):
        try:
            if not isinstance(cm, dict):
                continue
            pb = cm.get("posted_by") or {}
            by = (pb.get("name") if isinstance(pb, dict) else str(pb)) or ""
            content = (cm.get("content") or "").strip()
            if content:
                comments.append({"content": content, "by": by, "at": cm.get("posted_at")})
        except Exception:
            continue
    tags: list[str] = []
    for tg in (call.get("tags") or []):
        try:
            nm = (tg.get("name") if isinstance(tg, dict) else str(tg)) or ""
            nm = nm.strip()
            if nm:
                tags.append(nm)
        except Exception:
            continue
    contact = call.get("contact") or {}
    if not isinstance(contact, dict):
        contact = {}
    email = ""
    for ev in (contact.get("emails") or []):
        val = (ev.get("value") if isinstance(ev, dict) else str(ev)) or ""
        if val.strip():
            email = val.strip()
            break
    return {
        "comments": comments,                                   # list[{content, by, at}]
        "tags": tags,                                           # list[str]
        "contact_name": _contact_name(contact),
        "contact_email": email,
        "contact_company": (contact.get("company_name") or "").strip(),
    }


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
