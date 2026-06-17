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
    rpc_connect: StageVerdict
    full_pitch: StageVerdict
    is_lead: StageVerdict
    qualified: StageVerdict
    meeting_booked: StageVerdict
    call_outcome: Literal["voicemail", "gatekeeper", "wrong_number", "conversation", "other"]
    overall_notes: str
