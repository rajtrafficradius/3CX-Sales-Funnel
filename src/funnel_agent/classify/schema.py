"""Pydantic verdict models — the strict structured-output contract."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class StageVerdict(BaseModel):
    value: bool
    confidence: float = Field(ge=0, le=1)
    evidence: str  # short quote/paraphrase + timestamp if available


class CallClassification(BaseModel):
    rpc_connect: StageVerdict
    full_pitch: StageVerdict
    is_lead: StageVerdict
    qualified: StageVerdict
    meeting_booked: StageVerdict
    call_outcome: Literal["voicemail", "gatekeeper", "wrong_number", "conversation", "other"]
    overall_notes: str
