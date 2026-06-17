"""Read outbound calls + transcripts from the 3CX Configuration API.

Source: the `ReportOutboundCalls` report function `Pbx.GetOutboundCalls`, invoked
via GET with a date range. It returns EVERY outbound call (answered AND
unanswered) in one row each, with timing, the transcript text, an AI summary, and
a sentiment score — so "Calls Made" matches 3CX's own Extension Statistics
(unlike the Recordings entity, which only covers connected/recorded calls).

Day windows are computed in the configured local timezone so per-day counts line
up with 3CX's "Yesterday"/calendar-day reports.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .api import ThreeCXClient

_REPORT = "/xapi/v1/ReportOutboundCalls/Pbx.GetOutboundCalls"
_DUR = re.compile(r"P(?:T(?:(\d+)H)?(?:(\d+)M)?(?:([\d.]+)S)?)?")


def _iso_utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _duration_seconds(value) -> int:
    """Parse an ISO-8601 duration ('PT13.46S') or a numeric value to whole seconds."""
    if value is None:
        return 0
    if isinstance(value, (int, float)):
        return int(value)
    m = _DUR.fullmatch(str(value).strip())
    if not m:
        return 0
    h, mi, s = m.groups()
    return int(int(h or 0) * 3600 + int(mi or 0) * 60 + float(s or 0))


def _day_window_utc(day: date, tz: str) -> tuple[str, str]:
    zone = ZoneInfo(tz)
    start = datetime(day.year, day.month, day.day, tzinfo=zone)
    return _iso_utc(start), _iso_utc(start + timedelta(days=1))


def to_canonical(row: dict) -> dict:
    """Map a GetOutboundCalls row to the canonical call shape used by ingestion."""
    text = (row.get("Transcription") or "").strip()
    answered = bool(row.get("Answered"))
    talk = _duration_seconds(row.get("TalkingDuration"))
    dest = str(row.get("DestinationCalleeId") or row.get("DestinationDn") or "")
    cid = row.get("CdrId") or row.get("CallHistoryId")
    return {
        "call_id": str(cid),
        "bde_extension": str(row.get("SourceDn") or "") or None,
        "direction": "Outbound",
        "dest_number": dest or None,
        "started_at": _parse_dt(row.get("StartTime")),
        "ring_seconds": _duration_seconds(row.get("RingingDuration")),
        "talk_seconds": talk,
        "answered": answered,
        "is_voicemail": False,  # refined by the classifier's call_outcome
        "disposition": row.get("Status"),
        "has_transcript": bool(text),
        "transcript": {
            "text": text,
            "sentiment": (str(row["SentimentScore"]) if row.get("SentimentScore") is not None else None),
            "summary": row.get("Summary"),
            "diarized": False,
        } if text else None,
    }


def fetch_outbound_calls_for_day(client: ThreeCXClient, day: date, tz: str = "UTC") -> list[dict]:
    """Return canonical outbound-call dicts for one local day (answered + unanswered)."""
    pf, pt = _day_window_utc(day, tz)
    path = (
        f"{_REPORT}(periodFrom={pf},periodTo={pt},trunkDns='',callsType=0)"
    )
    return [to_canonical(r) for r in client.get_value(path)]


def earliest_call_date(client: ThreeCXClient, tz: str = "UTC") -> date | None:
    """Earliest outbound call date — used to auto-detect the backfill start."""
    # Probe the first outbound recording (cheap, ordered) for a history floor.
    rows = client.get_value(
        "/xapi/v1/Recordings",
        {"$filter": "CallType eq 'OutboundExternal'", "$orderby": "StartTime",
         "$top": 1, "$select": "Id,StartTime"},
    )
    dt = _parse_dt(rows[0].get("StartTime")) if rows else None
    if not dt:
        return None
    return dt.astimezone(ZoneInfo(tz)).date()


# Back-compat alias (sources.py imports this name).
earliest_recording_date = earliest_call_date
