"""Read outbound calls + transcripts from the 3CX Configuration API.

The `Recordings` entity is a single queryable source that carries everything the
funnel needs per recorded outbound call: FromDn (BDE extension), the dialled
number, timing, the transcript text, an AI summary, and a sentiment score — no
database access and no joins required.

A recorded outbound call = one funnel call. (Unanswered dials that produce no
recording are not counted here; "Calls Made" therefore means recorded outbound
calls. If full dial attempts are needed later, CallHistoryView can augment this.)
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from .api import ThreeCXClient

_OUTBOUND_FILTER = "CallType eq 'OutboundExternal'"
_SELECT = (
    "Id,StartTime,EndTime,CallType,FromDn,ToDn,ToCallerNumber,ToDidNumber,"
    "IsTranscribed,Transcription,Summary,SentimentScore,RecordingUrl"
)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _duration_seconds(start: str | None, end: str | None) -> int:
    a, b = _parse_dt(start), _parse_dt(end)
    return int((b - a).total_seconds()) if a and b and b >= a else 0


def to_canonical(rec: dict) -> dict:
    """Map a 3CX Recording row to the canonical call shape used by ingestion."""
    start = rec.get("StartTime")
    talk = _duration_seconds(start, rec.get("EndTime"))
    text = (rec.get("Transcription") or "").strip()
    dest = str(rec.get("ToCallerNumber") or rec.get("ToDn") or rec.get("ToDidNumber") or "")
    return {
        "call_id": str(rec.get("Id")),
        "bde_extension": str(rec.get("FromDn") or "") or None,
        "direction": "Outbound",
        "dest_number": dest or None,
        "started_at": _parse_dt(start),
        "ring_seconds": 0,
        "talk_seconds": talk,
        "answered": True,          # a recording exists -> the call connected
        "is_voicemail": False,
        "disposition": rec.get("CallType"),
        "has_transcript": bool(rec.get("IsTranscribed") and text),
        "transcript": {
            "text": text,
            "sentiment": (str(rec["SentimentScore"]) if rec.get("SentimentScore") is not None else None),
            "summary": rec.get("Summary"),
            "diarized": False,
        } if (rec.get("IsTranscribed") and text) else None,
    }


def fetch_outbound_calls_for_day(client: ThreeCXClient, day: date) -> list[dict]:
    """Return canonical outbound-call dicts for one day from the Recordings API."""
    start = datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc)
    end = start + timedelta(days=1)
    flt = f"{_OUTBOUND_FILTER} and StartTime ge {_iso(start)} and StartTime lt {_iso(end)}"
    rows = client.iter_query(
        "/xapi/v1/Recordings",
        {"$filter": flt, "$select": _SELECT, "$orderby": "StartTime"},
    )
    return [to_canonical(r) for r in rows]


def earliest_recording_date(client: ThreeCXClient) -> date | None:
    """Earliest outbound recording date — used to auto-detect the backfill start."""
    resp = client._get(  # noqa: SLF001 (intentional internal use)
        "/xapi/v1/Recordings",
        params={"$filter": _OUTBOUND_FILTER, "$orderby": "StartTime", "$top": 1,
                "$select": "Id,StartTime"},
    )
    val = resp.json().get("value", [])
    dt = _parse_dt(val[0].get("StartTime")) if val else None
    return dt.date() if dt else None
