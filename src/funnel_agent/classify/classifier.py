"""The classification engine.

One structured-output LLM call per transcribed in-scope call. Cheap model first;
escalate low-confidence calls to the strong model. Deterministic CDR pre-filter
skips the LLM for unanswered/voicemail. Post-hoc guardrails enforce funnel
monotonicity. Results upsert into `classifications` (PK call_id) -> idempotent.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, time, timedelta, timezone

from psycopg.types.json import Json
from psycopg_pool import ConnectionPool

from ..config import Settings
from ..logging import get_logger
from .prompt import SYSTEM_PROMPT, build_user_message
from .schema import CallClassification, RpcNextMove, Scorecard, StageVerdict

log = get_logger(__name__)

# Stages whose confidence drives escalation + human review.
QUALITY_STAGES = ("full_pitch", "is_lead", "qualified")


# --------------------------------------------------------------------------- #
# LLM backends (SDKs imported lazily so the rest of the package needs neither)
# --------------------------------------------------------------------------- #
class LLMBackend:
    def classify(self, system: str, user: str, model: str) -> CallClassification:
        raise NotImplementedError


class OpenAIBackend(LLMBackend):
    def __init__(self, api_key: str):
        from openai import OpenAI

        self._client = OpenAI(api_key=api_key)

    def classify(self, system: str, user: str, model: str) -> CallClassification:
        completion = self._client.beta.chat.completions.parse(
            model=model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            response_format=CallClassification,
            temperature=0,
        )
        msg = completion.choices[0].message
        if getattr(msg, "refusal", None):
            raise RuntimeError(f"model refused: {msg.refusal}")
        return msg.parsed


class AnthropicBackend(LLMBackend):
    _TOOL = "record_call_classification"

    def __init__(self, api_key: str):
        from anthropic import Anthropic

        self._client = Anthropic(api_key=api_key)

    def classify(self, system: str, user: str, model: str) -> CallClassification:
        schema = CallClassification.model_json_schema()
        msg = self._client.messages.create(
            model=model,
            max_tokens=1024,
            temperature=0,
            system=system,
            messages=[{"role": "user", "content": user}],
            tools=[
                {
                    "name": self._TOOL,
                    "description": "Record the structured funnel classification for this call.",
                    "input_schema": schema,
                }
            ],
            tool_choice={"type": "tool", "name": self._TOOL},
        )
        for block in msg.content:
            if block.type == "tool_use" and block.name == self._TOOL:
                return CallClassification.model_validate(block.input)
        raise RuntimeError("Anthropic response contained no tool_use block")


def make_backend(settings: Settings) -> LLMBackend:
    if not settings.llm_api_key:
        raise RuntimeError("LLM_API_KEY is not set")
    if settings.llm_provider == "openai":
        return OpenAIBackend(settings.llm_api_key)
    return AnthropicBackend(settings.llm_api_key)


# --------------------------------------------------------------------------- #
# Guardrails
# --------------------------------------------------------------------------- #
def monotonicity_violation(v: CallClassification) -> bool:
    """True if the funnel ordering is violated — a self-contradictory flag combo.

    Each downstream stage requires reaching the decision-maker (RPC). A booking can
    legitimately happen on a warm callback without a fresh full pitch, so we do NOT
    require full_pitch for meeting_booked — only RPC.
    """
    if v.full_pitch.value and not v.rpc_connect.value:
        return True
    if v.is_lead.value and not v.rpc_connect.value:
        return True
    if v.meeting_booked.value and not v.rpc_connect.value:
        return True
    if v.qualified.value and not v.is_lead.value:
        return True
    if v.meeting_confirmation_only.value and not v.meeting_booked.value:
        return True
    if v.meeting_rescheduled.value and not v.meeting_booked.value:
        return True
    if v.meeting_confirmation_only.value and v.meeting_rescheduled.value:
        return True
    return False


def min_quality_confidence(v: CallClassification) -> float:
    return min(getattr(v, stage).confidence for stage in QUALITY_STAGES)


def normalize_domain(website: str | None) -> str | None:
    """Reduce an LLM-extracted website to a bare, lower-case domain for use as the
    enrichment cache key (and a clean display value). Returns None when there's no
    usable domain. Shared by the classifier and the enrichment step so cache keys match.
    """
    import re

    s = (website or "").strip().lower()
    if not s:
        return None
    s = re.sub(r"^[a-z]+://", "", s)          # strip scheme
    s = s.split("/")[0].split("?")[0]          # drop path/query
    s = s.split("@")[-1]                        # drop any user@ prefix
    if s.startswith("www."):
        s = s[4:]
    s = s.strip().strip(".")
    # must look like a domain (has a dot, no spaces)
    if "." not in s or " " in s or len(s) < 4:
        return None
    return s


def derive_temperature(v: CallClassification) -> str:
    """Lead temperature from BAPU flags — only for the interested pipeline.

    warm = Authority+Problem; hot = +Urgency; super_hot = +Budget (all four).
    Pipeline 2 (already with an agency) and everything else get no temperature.
    """
    if v.pipeline != "pipeline1_interested":
        return "none"
    a, p, u, b = v.authority.value, v.problem.value, v.urgency.value, v.budget.value
    if a and p and u and b:
        return "super_hot"
    if a and p and u:
        return "hot"
    if a and p:
        return "warm"
    return "none"


import re as _re

# A tentative booking counts as a real Meeting Booked ONLY when a SPECIFIC time was agreed —
# a clock time (10:00 / 3:30), an am/pm time (10am, 2 pm), or noon/midday. A vague day or range
# ("next Thursday or Friday", "Monday", "next week") is a soft follow-up, not a booking. Firm
# bookings (meeting_booked=True) always count regardless of the datetime text. This mirrors the
# SQL regex used in aggregate.py / app.py / cli.py so the count and the pipeline routing agree.
_MEETING_TIME_RE = _re.compile(r"[0-9]:[0-9]|[0-9]\s*[ap]\.?m|noon|midday", _re.IGNORECASE)


def _has_meeting_time(dt: str | None) -> bool:
    return bool(dt) and bool(_MEETING_TIME_RE.search(dt))


def derive_pipeline_stage(v: CallClassification) -> str:
    """Batch D — deterministic 5-pipeline routing bucket for THIS call.

    Precedence (first match wins); P4 (fresh worklist) is DB-derived and is NEVER a
    call outcome, so it is not returned here:
      1. 'p5'  — a genuinely NEW meeting was booked on this call (firm OR tentative-with-a-
                 time), and it is not a confirmation / reschedule / already-existing booking.
                 P5 wins over everything so a booked prospect never lands in P1-P4.
      2. 'p2'  — the LLM routed the call to the existing-agency pipeline. Kept ABOVE p1 so an
                 agency prospect who also asks for a callback stays in the P2 rotation.
      3. 'p1'  — the decision-maker was REACHED (rpc_connect) AND explicitly asked for a
                 callback (strict — narrower than the legacy "interested" pipeline1).
      4. 'p3'  — the decision-maker was NOT reached but a gatekeeper/receptionist arranged
                 a callback.
      5. 'none' — voicemail / no-answer / wrong number / flat-no / reached-but-no-callback.
    The first-booking-per-company dedup and BDM booking/qualification overrides are applied
    at COUNT time (aggregate.py / _STAGE_COND), NOT here, so the stored value is a pure
    per-call signal that mirrors aggregate.py's "new booking" flag.
    """
    # A tentative-with-a-time counts as a NEW booking ONLY if it isn't really a callback: a prospect
    # who says "call me back tomorrow at 11" to continue a cold call is a CALLBACK (p1), not a booked
    # meeting — the classifier often tags that as booking_status='tentative' with a time, so exclude it.
    new_booking = (
        (v.meeting_booked.value
         or (v.booking_status == "tentative" and _has_meeting_time(v.meeting_datetime)
             and not v.callback_requested.value))
        and not v.meeting_confirmation_only.value
        and not v.meeting_rescheduled.value
        and not v.booking_already_exists.value
    )
    if new_booking:
        return "p5"
    if v.pipeline == "pipeline2_existing_agency":
        return "p2"
    if v.rpc_connect.value and v.callback_requested.value:
        return "p1"
    who = (v.who_answered or "").lower()
    is_gatekeeper = (v.call_outcome == "gatekeeper") or ("gatekeeper" in who) or ("recept" in who)
    # A callback must actually have been ARRANGED — an explicit callback request/time, the BDE
    # asking when to call back, or the gatekeeper stating when the DM is available. "Handled
    # well" is call quality, NOT a callback, so it does not qualify a prospect for P3.
    gk_callback = (
        v.callback_requested.value
        or bool((v.callback_when or "").strip())
        or v.rpc_next_move.asked_callback_time
        or bool((v.rpc_next_move.dm_available_when or "").strip())
    )
    if (not v.rpc_connect.value) and is_gatekeeper and gk_callback:
        return "p3"
    return "none"


def apply_auto_validation(v: CallClassification) -> None:
    """Auto-validate budget + aspiration from BEHAVIOUR (#6).

    If the prospect ALREADY runs paid ads or ALREADY has a marketing agency, they are
    demonstrably investing in marketing — so budget and aspiration are TRUE even if they
    never stated a figure. The prompt is told to do this, but we also enforce it here so
    the structured flags are deterministic (the prompt sometimes leaves budget=false).
    Mutates v.
    """
    if v.runs_paid_ads or v.has_marketing_agency:
        why = "already running paid ads" if v.runs_paid_ads else "already has a marketing agency"
        if not v.budget.value:
            v.budget = StageVerdict(value=True, confidence=max(v.budget.confidence, 0.7),
                                    evidence=f"Auto-validated: {why} — demonstrably investing in marketing.")
        if not v.aspiration.value:
            v.aspiration = StageVerdict(value=True, confidence=max(v.aspiration.confidence, 0.6),
                                        evidence=f"Auto-validated: {why} — clearly wants marketing results.")


def enforce_booking_firmness(v: CallClassification) -> None:
    """A booking only counts when it is FIRM (#7a — the Jesudas case).

    The model sets booking_status; if it is not 'firm' (tentative, cancelled/declined, or
    none) then meeting_booked must be FALSE — a half-agreed or collapsed appointment is not
    a booking. Reschedule/confirmation flags also require a firm booking. Mutates v.
    """
    if v.meeting_booked.value and v.booking_status != "firm":
        reason = {
            "tentative": "Not booked — only a tentative/half-agreement, no firm date/time held.",
            "cancelled_or_declined": "Not booked — the prospect cancelled / backed out; the appointment collapsed.",
            "none": "Not booked — no appointment was actually scheduled.",
        }.get(v.booking_status, "Not booked — booking not firm.")
        v.meeting_booked = StageVerdict(value=False, confidence=max(v.meeting_booked.confidence, 0.8),
                                        evidence=reason)
    if not v.meeting_booked.value:
        if v.meeting_confirmation_only.value:
            v.meeting_confirmation_only = StageVerdict(value=False, confidence=v.meeting_confirmation_only.confidence,
                                                       evidence="No firm booking on this call.")
        if v.meeting_rescheduled.value:
            v.meeting_rescheduled = StageVerdict(value=False, confidence=v.meeting_rescheduled.confidence,
                                                 evidence="No firm booking on this call.")


def enforce_decision_maker_gate(v: CallClassification) -> None:
    """Qualification rule (#7a) — DEFINITIVE, BANT-based. Mutates v.

    A lead / booking is QUALIFIED iff ALL THREE hold:
      (1) Authority — the person has buying authority (is the decision-maker). A non-decision-
          maker is NEVER qualified, no matter how strong the other signals.
      (2) Problem (Need) — a real marketing problem/need was established on the call. No need,
          not qualified — this is what separates a genuine opportunity from a friendly chat.
      (3) PLUS at least ONE more supporting signal: Budget, Urgency (Timeline), Aspiration, or
          Open-to-listening.
    Applied deterministically — it OVERRIDES the model's own guess (up OR down) so Qualified /
    Qualified Booked is a transparent, auditable count. The BDM keeps overriding authority on top.
    Run AFTER apply_auto_validation so a prospect already spending on ads/agency keeps budget.
    """
    extra = [v.budget.value, v.urgency.value, v.aspiration.value, v.open_to_listening.value]
    m = sum(1 for x in extra if x)
    q = bool(v.authority.value) and bool(v.problem.value) and m >= 1
    if q == v.qualified.value:
        return
    if q:
        ev = (f"Qualified — decision-maker with a real marketing Need (Problem) plus {m} more "
              "supporting signal(s) (Budget / Timeline / Aspiration / Open-to-listening).")
    elif not v.authority.value:
        ev = ("Not qualified — the person on the call is not the decision-maker (no buying "
              "authority); qualification cannot be established regardless of budget or need.")
    elif not v.problem.value:
        ev = ("Not qualified — decision-maker but no real marketing problem/need was established "
              "on the call (a genuine opportunity needs a Need).")
    else:
        ev = ("Not qualified — decision-maker with a Need but no other supporting signal "
              "(Budget / Timeline / Aspiration / Open-to-listening).")
    v.qualified = StageVerdict(value=q, confidence=max(v.qualified.confidence, 0.8), evidence=ev)


def _all_false_verdict(outcome: str, note: str) -> CallClassification:
    sv = lambda: StageVerdict(value=False, confidence=1.0, evidence=note)  # noqa: E731
    # No live conversation reached. `outcome` carries the only signal we have here
    # ("voicemail" when the CDR pre-filter saw a voicemail); everything else is a
    # non-answered dial -> "no_answer". Either way the move is to retry the call.
    is_voicemail = outcome == "voicemail"
    rpc_next_move = RpcNextMove(
        rpc_unreached_reason="voicemail" if is_voicemail else "no_answer",
        next_move="",  # leave blank — rpc.py derives the user-facing move; don't leak the CDR note
        next_move_channel="retry_call",
    )
    return CallClassification(
        analysis=note,
        who_answered="n/a",
        prospect_summary="",
        bde_summary="",
        rpc_connect=sv(),
        full_pitch=sv(),
        is_lead=sv(),
        qualified=sv(),
        meeting_booked=sv(),
        booking_status="none",
        meeting_datetime="",
        meeting_confirmation_only=sv(),
        meeting_rescheduled=sv(),
        booking_already_exists=sv(),
        budget=sv(),
        authority=sv(),
        problem=sv(),
        urgency=sv(),
        aspiration=sv(),
        open_to_listening=sv(),
        runs_paid_ads=False,
        has_marketing_agency=False,
        do_not_contact=False,
        gatekeeper_handled_well=False,
        gatekeeper_notes="",
        rpc_next_move=rpc_next_move,
        problem_summary="",
        pipeline="none",
        prospect_company="",
        prospect_website="",
        prospect_industry="",
        prospect_contact_name="",
        prospect_mobile="",
        prospect_email="",
        key_facts=[],
        callback_requested=sv(),
        callback_when="",
        contract_end="",
        recommended_cadence_days=0,
        objections=[],
        buying_signals=[],
        engagement="neutral",
        prospect_sentiment="neutral",
        next_step="",
        coaching_tips=[],
        scorecard=Scorecard(opening=0, discovery=0, pitch=0, objection_handling=0, close=0),
        call_outcome=outcome,  # type: ignore[arg-type]
        overall_notes=note,
    )


# --------------------------------------------------------------------------- #
# Classifier
# --------------------------------------------------------------------------- #
class Classifier:
    def __init__(self, settings: Settings, backend: LLMBackend | None = None):
        self._s = settings
        self._backend = backend  # lazily created on first LLM call

    def _llm(self) -> LLMBackend:
        if self._backend is None:
            self._backend = make_backend(self._s)
        return self._backend

    def _run_with_escalation(self, user: str) -> tuple[CallClassification, str]:
        """Cheap model first; escalate to the strong model if low-confidence."""
        cheap = self._s.llm_model_cheap
        strong = self._s.llm_model_strong
        verdict = self._llm().classify(SYSTEM_PROMPT, user, cheap)
        model_used = cheap
        if min_quality_confidence(verdict) < self._s.confidence_threshold and strong and strong != cheap:
            verdict = self._llm().classify(SYSTEM_PROMPT, user, strong)
            model_used = strong
        return verdict, model_used

    def classify_one(self, call_row: dict, transcript_row: dict, memory: str | None = None,
                     master_domain: str | None = None) -> dict:
        """Classify a single transcribed call -> a `classifications` record dict.

        `memory` is an optional [COMPANY MEMORY] block injected so the model knows a meeting
        was already booked for this company (prevents double-counting referral hand-offs).
        `master_domain` (resolved from the master prospects table by phone) stabilises the
        company_key when the AI doesn't extract a website.
        """
        call_id = str(call_row["call_id"])
        dest_number = call_row.get("dest_number")

        # CDR pre-filter: don't spend an LLM call on a dial that never connected.
        if not call_row.get("answered") or call_row.get("is_voicemail"):
            outcome = "voicemail" if call_row.get("is_voicemail") else "other"
            verdict = _all_false_verdict(outcome, "CDR pre-filter: not a live conversation")
            return self._record(call_id, verdict, model="cdr_prefilter", needs_review=False,
                                dest_number=dest_number, master_domain=master_domain)

        text = (transcript_row.get("text") or "")[: self._s.llm_max_transcript_chars]
        user = build_user_message(text, transcript_row.get("sentiment"),
                                  transcript_row.get("summary"), memory=memory or None)
        verdict, model_used = self._run_with_escalation(user)
        apply_auto_validation(verdict)      # budget/aspiration from running ads / having an agency
        enforce_booking_firmness(verdict)   # a non-firm/cancelled booking does not count
        enforce_decision_maker_gate(verdict)  # authority necessary AND not-authority-alone

        needs_review = (
            min_quality_confidence(verdict) < self._s.confidence_threshold
            or monotonicity_violation(verdict)
        )
        return self._record(call_id, verdict, model=model_used, needs_review=needs_review,
                            dest_number=dest_number, master_domain=master_domain)

    @staticmethod
    def _record(call_id: str, v: CallClassification, *, model: str, needs_review: bool,
                dest_number: str | None = None, master_domain: str | None = None) -> dict:
        from .memory import company_key
        # a booking that already existed for the company is not a NEW booking (only meaningful
        # when a booking was actually detected on this call)
        already_exists = bool(v.booking_already_exists.value and v.meeting_booked.value)
        # domain identity: AI-extracted website, else the master-prospects domain for the phone
        _domain = v.prospect_website or master_domain
        return {
            "call_id": call_id,
            "rpc_connect": v.rpc_connect.value,
            "rpc_confidence": v.rpc_connect.confidence,
            "full_pitch": v.full_pitch.value,
            "pitch_confidence": v.full_pitch.confidence,
            "is_lead": v.is_lead.value,
            "lead_confidence": v.is_lead.confidence,
            "qualified": v.qualified.value,
            "qual_confidence": v.qualified.confidence,
            "meeting_booked": v.meeting_booked.value,
            "booking_status": v.booking_status,
            "meeting_datetime": v.meeting_datetime or None,
            "meeting_confirmation": v.meeting_confirmation_only.value,
            "meeting_rescheduled": v.meeting_rescheduled.value,
            "booking_already_exists": already_exists,
            "company_key": company_key(_domain, v.prospect_company, dest_number),
            # lead qualification (BANT + AO)
            "budget": v.budget.value,
            "authority": v.authority.value,
            "problem": v.problem.value,
            "urgency": v.urgency.value,
            "aspiration": v.aspiration.value,
            "open_to_listening": v.open_to_listening.value,
            "runs_paid_ads": v.runs_paid_ads,
            "has_marketing_agency": v.has_marketing_agency,
            "do_not_contact": v.do_not_contact,
            "gatekeeper_handled_well": v.gatekeeper_handled_well,
            "gatekeeper_notes": v.gatekeeper_notes or None,
            "problem_summary": v.problem_summary or None,
            "lead_temperature": derive_temperature(v),
            "pipeline": v.pipeline,
            "pipeline_stage": derive_pipeline_stage(v),  # Batch D 5-pipeline routing bucket
            "callback_requested": v.callback_requested.value,
            "callback_when": v.callback_when or None,
            # pipeline 2 cadence intelligence
            "contract_end": v.contract_end or None,
            "recommended_cadence_days": (v.recommended_cadence_days or None),
            # business identification + contact details extracted from the call
            "prospect_company": v.prospect_company or None,
            "prospect_website": normalize_domain(v.prospect_website),
            "prospect_industry": v.prospect_industry or None,
            "prospect_contact_name": v.prospect_contact_name or None,
            "prospect_mobile": v.prospect_mobile or None,
            "prospect_email": v.prospect_email or None,
            "call_outcome": v.call_outcome,
            "evidence": v.model_dump(),
            "model": model,
            "needs_human_review": needs_review,
        }


# --------------------------------------------------------------------------- #
# Persistence + day driver
# --------------------------------------------------------------------------- #
def upsert_classification(pool: ConnectionPool, rec: dict) -> None:
    rec = {**rec, "classified_at": datetime.now(timezone.utc), "evidence": Json(rec["evidence"])}
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO classifications (
                call_id, rpc_connect, rpc_confidence, full_pitch, pitch_confidence,
                is_lead, lead_confidence, qualified, qual_confidence, meeting_booked,
                booking_status, meeting_datetime,
                meeting_confirmation, meeting_rescheduled, booking_already_exists, company_key,
                budget, authority, problem, urgency,
                aspiration, open_to_listening, runs_paid_ads, has_marketing_agency, do_not_contact,
                gatekeeper_handled_well, gatekeeper_notes, problem_summary,
                lead_temperature,
                pipeline, pipeline_stage, callback_requested, callback_when, contract_end, recommended_cadence_days,
                prospect_company, prospect_website, prospect_industry,
                prospect_contact_name, prospect_mobile, prospect_email,
                call_outcome, evidence, model, classified_at, needs_human_review)
            VALUES (
                %(call_id)s, %(rpc_connect)s, %(rpc_confidence)s, %(full_pitch)s, %(pitch_confidence)s,
                %(is_lead)s, %(lead_confidence)s, %(qualified)s, %(qual_confidence)s, %(meeting_booked)s,
                %(booking_status)s, %(meeting_datetime)s,
                %(meeting_confirmation)s, %(meeting_rescheduled)s, %(booking_already_exists)s, %(company_key)s,
                %(budget)s, %(authority)s, %(problem)s, %(urgency)s,
                %(aspiration)s, %(open_to_listening)s, %(runs_paid_ads)s, %(has_marketing_agency)s, %(do_not_contact)s,
                %(gatekeeper_handled_well)s, %(gatekeeper_notes)s, %(problem_summary)s,
                %(lead_temperature)s,
                %(pipeline)s, %(pipeline_stage)s, %(callback_requested)s, %(callback_when)s, %(contract_end)s, %(recommended_cadence_days)s,
                %(prospect_company)s, %(prospect_website)s, %(prospect_industry)s,
                %(prospect_contact_name)s, %(prospect_mobile)s, %(prospect_email)s,
                %(call_outcome)s, %(evidence)s, %(model)s, %(classified_at)s, %(needs_human_review)s)
            ON CONFLICT (call_id) DO UPDATE SET
                rpc_connect = EXCLUDED.rpc_connect, rpc_confidence = EXCLUDED.rpc_confidence,
                full_pitch = EXCLUDED.full_pitch, pitch_confidence = EXCLUDED.pitch_confidence,
                is_lead = EXCLUDED.is_lead, lead_confidence = EXCLUDED.lead_confidence,
                qualified = EXCLUDED.qualified, qual_confidence = EXCLUDED.qual_confidence,
                meeting_booked = EXCLUDED.meeting_booked,
                booking_status = EXCLUDED.booking_status, meeting_datetime = EXCLUDED.meeting_datetime,
                meeting_confirmation = EXCLUDED.meeting_confirmation,
                meeting_rescheduled = EXCLUDED.meeting_rescheduled,
                booking_already_exists = EXCLUDED.booking_already_exists,
                company_key = EXCLUDED.company_key,
                budget = EXCLUDED.budget, authority = EXCLUDED.authority,
                problem = EXCLUDED.problem, urgency = EXCLUDED.urgency,
                aspiration = EXCLUDED.aspiration, open_to_listening = EXCLUDED.open_to_listening,
                runs_paid_ads = EXCLUDED.runs_paid_ads, has_marketing_agency = EXCLUDED.has_marketing_agency,
                do_not_contact = EXCLUDED.do_not_contact,
                gatekeeper_handled_well = EXCLUDED.gatekeeper_handled_well,
                gatekeeper_notes = EXCLUDED.gatekeeper_notes,
                problem_summary = EXCLUDED.problem_summary,
                lead_temperature = EXCLUDED.lead_temperature, pipeline = EXCLUDED.pipeline,
                pipeline_stage = EXCLUDED.pipeline_stage,
                callback_requested = EXCLUDED.callback_requested, callback_when = EXCLUDED.callback_when,
                contract_end = EXCLUDED.contract_end,
                recommended_cadence_days = EXCLUDED.recommended_cadence_days,
                prospect_company = EXCLUDED.prospect_company, prospect_website = EXCLUDED.prospect_website,
                prospect_industry = EXCLUDED.prospect_industry,
                prospect_contact_name = EXCLUDED.prospect_contact_name,
                prospect_mobile = EXCLUDED.prospect_mobile, prospect_email = EXCLUDED.prospect_email,
                call_outcome = EXCLUDED.call_outcome,
                evidence = EXCLUDED.evidence, model = EXCLUDED.model,
                classified_at = EXCLUDED.classified_at,
                needs_human_review = EXCLUDED.needs_human_review
            """,
            rec,
        )
        conn.commit()


def _pending_calls_for_day(
    pool: ConnectionPool, day: date, limit: int | None = None, force: bool = False
) -> list[dict]:
    """In-scope, transcribed calls for one day, with transcript.

    By default only NOT-yet-classified calls (idempotent). With force=True, ALL
    such calls are returned so they get re-classified under the current rubric —
    use this after a prompt / schema change (e.g. new verdict fields).
    """
    start = datetime.combine(day, time.min)
    end = start + timedelta(days=1)
    not_classified = "" if force else "AND cl.call_id IS NULL"
    sql = f"""
        SELECT c.call_id, c.answered, c.is_voicemail, c.dest_number,
               t.text, t.sentiment, t.summary, t.diarized
        FROM calls c
        JOIN transcripts t ON t.call_id = c.call_id
        LEFT JOIN classifications cl ON cl.call_id = c.call_id
        WHERE c.started_at >= %(start)s AND c.started_at < %(end)s
          AND c.has_transcript AND c.in_scope {not_classified}
        ORDER BY c.started_at
    """
    params: dict = {"start": start, "end": end}
    if limit:
        sql += " LIMIT %(limit)s"
        params["limit"] = limit
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def classify_day(
    pool: ConnectionPool,
    settings: Settings,
    day: date,
    limit: int | None = None,
    workers: int | None = None,
    force: bool = False,
) -> dict:
    """Classify pending transcribed in-scope calls for a day, in parallel. Idempotent.

    force=True re-classifies already-classified calls too (for rubric changes).
    """
    pending = _pending_calls_for_day(pool, day, limit, force)
    if not pending:
        return {"classified": 0, "errors": 0, "needs_review": 0}

    workers = workers or settings.classify_workers
    clf = Classifier(settings)
    clf._llm()  # warm the backend before fanning out across threads

    def work(row: dict) -> tuple[str, bool]:
        try:
            # Company memory: does this call's company already have a firm booking on
            # record? If so, tell the model so a referral/hand-off isn't double-counted.
            # Resolve the domain from the master prospects table (by phone) so calls to the
            # same company get a STABLE domain-based company_key even if the AI doesn't
            # extract a website (this is what cross-contact booking dedup keys on).
            from .memory import _master_domain, format_memory, lookup_booking_memory
            mdom = _master_domain(pool, row.get("dest_number"))
            mem = format_memory(lookup_booking_memory(
                pool, row.get("dest_number"), domain=mdom, before_call_id=str(row.get("call_id"))))
            rec = clf.classify_one(row, {
                "text": row.get("text"), "sentiment": row.get("sentiment"),
                "summary": row.get("summary"),
            }, memory=mem, master_domain=mdom)
            upsert_classification(pool, rec)
            return ("ok", bool(rec["needs_human_review"]))
        except Exception as exc:  # one bad call must not abort the batch
            log.warning("classify_call_failed", call_id=row.get("call_id"), error=str(exc)[:200])
            return ("err", False)

    classified = errors = needs_review = 0
    if workers > 1:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            outcomes = ex.map(work, pending)
    else:
        outcomes = (work(r) for r in pending)
    for status, nr in outcomes:
        if status == "ok":
            classified += 1
            needs_review += int(nr)
        else:
            errors += 1

    log.info("classify_day_done", day=str(day), classified=classified,
             errors=errors, needs_review=needs_review)
    return {"classified": classified, "errors": errors, "needs_review": needs_review}
