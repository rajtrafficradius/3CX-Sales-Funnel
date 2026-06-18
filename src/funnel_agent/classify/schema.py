"""Pydantic verdict models — the strict structured-output contract."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class StageVerdict(BaseModel):
    value: bool
    confidence: float = Field(ge=0, le=1)
    evidence: str  # short quote/paraphrase + timestamp if available


class CallClassification(BaseModel):
    # `analysis` is generated FIRST so the model reasons through the transcript
    # (who answered, was it the decision-maker, what was pitched, any commitment)
    # before committing to the per-stage verdicts below. This materially improves
    # accuracy on the judgement stages.
    analysis: str
    who_answered: str  # e.g. "owner/decision-maker", "gatekeeper/receptionist", "voicemail", "wrong number"
    prospect_summary: str  # what the PROSPECT said: their business, needs, objections, commitments
    bde_summary: str       # what the BDE (our agent) said/did: intro, pitch, offer, next step secured
    rpc_connect: StageVerdict
    full_pitch: StageVerdict
    is_lead: StageVerdict
    qualified: StageVerdict
    meeting_booked: StageVerdict
    # TRUE only when this call merely CONFIRMS / reschedules a meeting that was
    # already booked on an EARLIER call (no NEW booking happened here). A genuine
    # new booking made during this call is FALSE. Used to keep confirmation calls
    # out of the "Meeting Booked" funnel count (they would double-count a booking).
    meeting_confirmation_only: StageVerdict
    call_outcome: Literal["voicemail", "gatekeeper", "wrong_number", "conversation", "other"]
    overall_notes: str
