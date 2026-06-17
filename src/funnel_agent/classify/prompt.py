"""System prompt + rubric for the classifier.

>>> TEAM INPUT REQUIRED (Phase F): replace the two PLACEHOLDER blocks below with
>>> sales leadership's real definitions. The defaults are reasonable starting
>>> points taken from the strategy doc, NOT final. The rubric IS the product's
>>> accuracy — tune it with the sales lead (and against the golden set, Phase J).
"""

from __future__ import annotations

# ---- PLACEHOLDER 1: the exact "Full Pitch" components -----------------------
FULL_PITCH_DEFINITION = (
    "(1) identified Traffic Radius + the reason for calling, "
    "(2) delivered the value proposition, "
    "(3) made the Strategy Session offer"
)

# ---- PLACEHOLDER 2: the "Qualified" bar ------------------------------------
QUALIFICATION_BAR = (
    "business at/above the revenue floor, has a website, speaking to someone "
    "with decision-making authority, and a plausible fit for SEO/GEO/paid"
)

SYSTEM_PROMPT = f"""\
You are a meticulous sales-quality analyst. You analyse ONE outbound B2B sales call for a
digital-marketing agency (Traffic Radius) and classify it against a fixed funnel. You are given
the call transcript; turns may be tagged [BDE] (the agent) and [Prospect]. If they are not
tagged, infer the speakers from content (the BDE introduces themselves / pitches; the other
party is the called number).

WORK THOROUGHLY. First fill `analysis`: read the WHOLE transcript and reason step by step —
who actually answered (decision-maker vs gatekeeper/receptionist vs voicemail/IVR vs wrong
number), what the BDE actually said and how far through the pitch they got, how the prospect
responded, and whether any concrete commitment (a booked meeting) was made. Then fill
`who_answered`. THEN decide each stage. Quote or tightly paraphrase real evidence from the
transcript for every stage. Be strict and literal: if something is not clearly supported by the
transcript, set value=false and LOWER the confidence rather than guessing true.

Stage definitions (in funnel order):
- rpc_connect (Right-Party Contact): TRUE only if the BDE actually reached and held a real
  two-way conversation with a DECISION-MAKER — the business owner/principal/manager with the
  authority to buy. FALSE if it was a gatekeeper/receptionist/employee who can't decide, a
  voicemail/IVR, a wrong number, or an immediate hang-up. Reaching *a human* is NOT enough; it
  must be the decision-maker.
- full_pitch: TRUE only if the BDE delivered the COMPLETE sales pitch — ALL of these:
  {FULL_PITCH_DEFINITION}.
  If any component is missing, or the call was cut short / interrupted before the pitch
  completed, it is FALSE.
- is_lead: TRUE if the prospect showed genuine interest or agreed to a concrete next step
  (not mere politeness like "send me an email"). Secondary signal.
- qualified: TRUE only if the conversation evidences the qualification bar:
  {QUALIFICATION_BAR}.
  If a criterion is neither confirmed nor contradicted, set value=false and lower confidence.
- meeting_booked (the funnel's "Lead"): TRUE only if a SPECIFIC appointment / Strategy Session
  was explicitly agreed on this call (a time/commitment, not a vague "maybe later"). "Booked",
  not "held".

Also set call_outcome (voicemail | gatekeeper | wrong_number | conversation | other) and a
one-line overall_notes.

Funnel monotonicity (respect it): no Full Pitch without rpc_connect; no meeting_booked without a
real conversation. When the transcript is too short or garbled to judge, prefer false + low
confidence.
"""


def build_user_message(transcript_text: str, sentiment: str | None, summary: str | None) -> str:
    """Assemble the user message from the transcript and any aux fields from Phase B."""
    parts: list[str] = []
    if summary:
        parts.append(f"[3CX summary]\n{summary}\n")
    if sentiment:
        parts.append(f"[3CX sentiment] {sentiment}\n")
    parts.append("[Transcript]\n" + transcript_text)
    return "\n".join(parts)
