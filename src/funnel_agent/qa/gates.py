"""G1 — the booking gate (pure CPU, no I/O).

Operator directive: NEVER trust Retell's ``meeting_agreed``. A "booked" is only honoured when the
call was a GENUINE TWO-PARTY CONVERSATION (not a voicemail / machine / no-answer, per the telephony
disconnect reason + a real talk duration) AND the transcript actually contains a concrete agreed
time. Anything else is recorded as a conversation / callback instead, and the divergence from
Retell's flag is logged to ``qa_audit`` by the caller.

This module is deliberately pure: it takes the transcript text + hard telephony facts and returns a
verdict. It performs NO database or network I/O, so it is safe to call anywhere (including — though it
is not — on the dial path). Callers do the logging + the un-booking.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Telephony disconnect reasons that mean we NEVER reached a live human two-party conversation. Retell
# emits these on the `call.disconnection_reason` field. Anything here can never be a real booking.
_NON_CONVERSATION_REASONS = frozenset({
    "voicemail_reached", "voicemail", "machine_detected", "answering_machine",
    "dial_no_answer", "no_answer", "dial_busy", "dial_failed", "dial_no_pickup",
    "invalid_destination", "no_valid_payment", "concurrency_limit_reached",
    "registered_call_no_answer", "call_transfer_failed",
})

# A genuine, bookable conversation runs at least this long. Tunable. The operator specified "~45s":
# below this a "booking" is almost always a voicemail greeting or an instant hang-up mis-flagged by
# Retell. Kept as a named constant so it is easy to retune from one place.
_MIN_CONVERSATION_MS = 45_000

# A concrete agreed time leaves a fingerprint in the transcript: a weekday, a relative day, a
# part-of-day, or a clock time. Broad on purpose — a real booking almost always names a DAY, so the
# false-downgrade risk is low, while a voicemail / "I'll think about it" transcript names no time at
# all. Word boundaries keep it from matching inside other words.
_TIME_RX = re.compile(
    r"\b(?:"
    r"\d{1,2}\s*:\s*\d{2}"                                     # 14:30 / 2:30
    r"|\d{1,2}\s*(?:a\.?m\.?|p\.?m\.?)"                        # 2pm / 2 p.m.
    r"|\d{1,2}\s*o'?\s*clock"                                  # 3 o'clock
    r"|(?:half|quarter)\s+(?:past|to)"                         # half past / quarter to
    r"|monday|tuesday|wednesday|thursday|friday|saturday|sunday"
    r"|tomorrow|today|tonight"
    r"|next\s+week|this\s+(?:week|arvo|afternoon|morning|evening)"
    r"|morning|afternoon|evening|midday|noon|lunchtime|midnight"
    r")\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class BookingVerdict:
    """Result of the G1 gate. ``ok`` True => honour the booking. When False, ``downgraded_outcome`` is
    the outcome the caller should record instead ('conversation' or 'callback'), and ``reason`` is a
    short machine-readable explanation for the qa_audit log."""
    ok: bool
    reason: str
    downgraded_outcome: str = "conversation"


def _genuine_two_party(disconnect_reason: str | None, duration_ms) -> bool:
    """True only if the telephony facts are consistent with a live two-way call of real length."""
    r = (disconnect_reason or "").strip().lower()
    if r in _NON_CONVERSATION_REASONS:
        return False
    try:
        return int(duration_ms or 0) >= _MIN_CONVERSATION_MS
    except (TypeError, ValueError):
        return False


def has_concrete_time(text: str | None) -> bool:
    """True if the text contains a concrete day / time reference. Transcript-only (never Retell's cad)."""
    return bool(text and _TIME_RX.search(str(text)))


def booking_verdict(*, transcript: str | None = None, disconnect_reason: str | None = None,
                    duration_ms=None, claimed_time: str | None = None) -> BookingVerdict:
    """Decide whether a Retell "booked" should be honoured.

    ``claimed_time`` (Retell's agreed_day_time) is accepted for context/logging but — per the operator
    directive — is NOT used in the decision; only the transcript + telephony facts are.

    Rules:
      1. Must be a genuine two-party conversation (disconnect reason not a machine/no-answer AND
         duration >= ~45s). Fail => downgrade to 'callback'.
      2. If we have a transcript to inspect, it must contain a concrete agreed time. Fail =>
         downgrade to 'conversation'. (When no transcript is available we do NOT downgrade on this
         basis — we rely on rule 1 only, to avoid false un-bookings from missing data.)
    """
    if not _genuine_two_party(disconnect_reason, duration_ms):
        r = (disconnect_reason or "").strip().lower()
        try:
            dur = int(duration_ms or 0)
        except (TypeError, ValueError):
            dur = 0
        if r in _NON_CONVERSATION_REASONS:
            reason = f"not a live two-party conversation (disconnect={r})"
        elif dur < _MIN_CONVERSATION_MS:
            reason = f"call too short to be a real booking (dur_ms={dur} < {_MIN_CONVERSATION_MS})"
        else:
            reason = f"not a live two-party conversation (disconnect={r or 'unknown'})"
        return BookingVerdict(False, reason, "callback")

    tx = transcript or ""
    if tx and not has_concrete_time(tx):
        return BookingVerdict(False, "genuine conversation but no concrete agreed time in transcript",
                              "conversation")
    return BookingVerdict(True, "genuine two-party conversation with a concrete agreed time")
