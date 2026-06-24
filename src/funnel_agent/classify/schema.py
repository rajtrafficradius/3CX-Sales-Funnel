"""Pydantic verdict models — the strict structured-output contract."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class StageVerdict(BaseModel):
    value: bool
    confidence: float = Field(ge=0, le=1)
    evidence: str  # short quote/paraphrase + timestamp if available


class Objection(BaseModel):
    objection: str          # what the prospect pushed back on
    response: str           # how the BDE handled it
    handled: bool           # did the BDE address it convincingly?


class Scorecard(BaseModel):
    # 0 (poor / absent) .. 5 (excellent). A coaching aid for managers.
    opening: int = Field(ge=0, le=5)
    discovery: int = Field(ge=0, le=5)
    pitch: int = Field(ge=0, le=5)
    objection_handling: int = Field(ge=0, le=5)
    close: int = Field(ge=0, le=5)


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
    # Firmness of any booking attempt on THIS call. meeting_booked must only be TRUE
    # when booking_status == "firm" (a specific day/time mutually agreed and NOT
    # retracted/cancelled later in the same call). "tentative" = vague/half-agreed or
    # left unresolved; "cancelled_or_declined" = the prospect cancelled, backed out,
    # or the arrangement fell apart; "none" = no booking discussed.
    booking_status: Literal["firm", "tentative", "cancelled_or_declined", "none"]
    meeting_datetime: str  # the agreed day/time in the prospect's words, "" if none
    # TRUE only when this call merely RE-CONFIRMS a meeting already booked on an
    # EARLIER call and NO new date was set here (no new booking happened). A
    # reschedule (a NEW date set this call) is NOT confirmation-only.
    meeting_confirmation_only: StageVerdict
    # TRUE when this call RE-BOOKS / moves a previously-agreed meeting to a NEW
    # date/time (the prospect missed or changed the original). Flagged so the call
    # page can label it "rescheduled booking"; meeting_confirmation_only must be FALSE
    # here. (Reschedules + confirmations are NOT counted again in the funnel — the
    # booking was already counted on the day it was first made.)
    meeting_rescheduled: StageVerdict

    # ---- Lead qualification: BANT + AO ----
    # Budget, Authority, Need(Problem), Timeline(Urgency) + Aspiration, Open-to-listening.
    # Each is judged from what the PROSPECT actually revealed on THIS call.
    # Temperature (Warm/Hot/Super-hot) is DERIVED from these in Python, not here.
    budget: StageVerdict      # signals they can/will spend on marketing
    authority: StageVerdict   # the person is a decision-maker (can say yes)
    problem: StageVerdict     # a real DIGITAL-MARKETING problem/need we can solve
    urgency: StageVerdict     # time pressure / wants to act soon
    aspiration: StageVerdict  # wants to grow/improve — ambition for the business (even if no acute "problem")
    open_to_listening: StageVerdict  # willing to hear us out / engage further, not a flat brush-off

    # ---- Auto-validation signals (inferred from behaviour, not explicit budget talk) ----
    # If the prospect ALREADY runs paid ads or ALREADY has a marketing agency, they are
    # demonstrably investing in marketing -> budget + aspiration can be inferred TRUE even
    # if never stated. Captured here so Python can apply that rule deterministically.
    runs_paid_ads: bool          # currently runs Google/Meta/any paid advertising
    has_marketing_agency: bool   # currently has an agency/freelancer doing their marketing
    # DND signal: TRUE if the prospect clearly does NOT want future contact — rude/
    # hostile, asked to be removed/not called again, OR firmly not interested and wants
    # us to stop. FALSE for a soft "not now / happy with our agency for now" (those stay
    # in Pipeline 2 for a later call). Drives auto-DND for Pipeline 2.
    do_not_contact: bool
    # ---- Right-Party-Connect: gatekeeper handling (#5) ----
    # Only meaningful when who_answered is a gatekeeper/receptionist. Captures how the
    # call was handled so the AI can coach the BDE toward reaching the decision-maker.
    gatekeeper_handled_well: bool   # did the BDE handle the gatekeeper well (ask for the DM by name, get a callback time, stay professional)?
    gatekeeper_notes: str           # what the gatekeeper asked / info they requested / helped vs blocked + what the BDE could do better; "" if not a gatekeeper call
    # The SPECIFIC digital-marketing problem in the prospect's OWN words ("" if none stated
    # by the prospect). Do NOT fill this from the BDE's pitch claims — only the prospect's.
    problem_summary: str

    # Pipeline routing:
    #   pipeline1_interested      = interested / agreed to a callback or next step
    #   pipeline2_existing_agency = NOT interested because already with an agency
    #   none                      = neither (voicemail, gatekeeper, flat no, wrong number, etc.)
    pipeline: Literal["pipeline1_interested", "pipeline2_existing_agency", "none"]

    # ---- Business identification (only what is actually stated/derivable) ----
    prospect_company: str   # business name, "" if unknown
    prospect_website: str   # bare domain e.g. "acme.com.au", "" if not stated/derivable
    prospect_industry: str  # e.g. "plumbing", "dental clinic", "" if unknown
    # ---- Contact details stated ON the call — capture EVERY one, never invent ----
    prospect_contact_name: str  # the person's name as stated (e.g. "Jean"), "" if unknown
    prospect_mobile: str        # a mobile number the prospect gave, digits as heard, "" if none
    prospect_email: str         # an email the prospect gave/spelled, "" if none
    # Every OTHER valuable fact the prospect stated that isn't already a field above —
    # e.g. revenue/turnover, # of staff, locations/branches, named competitors, current
    # tools/spend, timelines, who does their marketing. Short phrases. [] if none.
    key_facts: list[str]

    # ---- Callback request (feeds the BDE calendar in Phase 2) ----
    callback_requested: StageVerdict  # prospect asked to be called back
    callback_when: str                # free text e.g. "next Tuesday afternoon", "" if none

    # ---- Pipeline 2 (already-with-agency) cadence intelligence ----
    # Only meaningful when pipeline == pipeline2_existing_agency. The prospect can't
    # switch until their current agency contract ends, and over-calling makes them
    # rude — so capture any contract-end signal and recommend WHEN to call next.
    contract_end: str                 # what they said re: contract end (e.g. "ends March", "6 months left", "just signed 12mo"), "" if none
    recommended_cadence_days: int = Field(ge=0)  # AI's days-until-next-call for P2 (0 = unknown → use default monthly)

    # ---- Conversation intelligence (coaching + insight) ----
    objections: list[Objection]
    buying_signals: list[str]
    engagement: Literal["dismissive", "neutral", "engaged", "keen"]
    prospect_sentiment: Literal["negative", "neutral", "positive"]
    next_step: str            # the concrete agreed next step, "" if none
    coaching_tips: list[str]  # what the BDE could do better next time
    scorecard: Scorecard

    call_outcome: Literal["voicemail", "gatekeeper", "wrong_number", "conversation", "other"]
    overall_notes: str
