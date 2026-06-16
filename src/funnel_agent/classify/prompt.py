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
You classify a single outbound B2B sales call for a digital-marketing agency (Traffic Radius)
against a fixed sales funnel. You are given a call transcript; turns may be tagged [BDE] (the
agent) and [Prospect]. If speakers are not tagged, infer them from content.

Return ONLY the structured object. For each stage give value (bool), confidence (0..1), and a
short evidence string. Be strict; when unsure, lower the confidence rather than guessing true.

Stage definitions:
- rpc_connect: TRUE only if the BDE reached the intended decision-maker and had a real two-way
  conversation. FALSE for voicemail, IVR, gatekeeper-only, wrong number, or immediate hang-up.
- full_pitch: TRUE only if the BDE delivered ALL core pitch components:
  {FULL_PITCH_DEFINITION}.
  A partial or interrupted pitch is FALSE.
- is_lead: TRUE if the prospect showed genuine interest or agreed to a next step (not mere politeness).
- qualified: TRUE only if the conversation evidences the qualification bar:
  {QUALIFICATION_BAR}.
  If a criterion is neither confirmed nor contradicted, set value=false and lower confidence.
- meeting_booked: TRUE only if a specific appointment/Strategy Session was explicitly agreed on
  the call. This is "booked", not "held".

Also set call_outcome (one of: voicemail, gatekeeper, wrong_number, conversation, other) and a
one-line overall_notes.

Funnel monotonicity (the reviewer enforces this, but respect it): there is no Lead without an RPC
connect, and no Qualified Lead without a Lead.
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
