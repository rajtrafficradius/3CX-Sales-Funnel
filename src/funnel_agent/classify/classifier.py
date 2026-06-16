"""The classification engine.

One structured-output LLM call per transcribed in-scope call. Cheap model first;
escalate low-confidence calls to the strong model. Deterministic CDR pre-filter
skips the LLM for unanswered/voicemail. Post-hoc guardrails enforce funnel
monotonicity. Results upsert into `classifications` (PK call_id) -> idempotent.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone

from psycopg.types.json import Json
from psycopg_pool import ConnectionPool

from ..config import Settings
from ..logging import get_logger
from .prompt import SYSTEM_PROMPT, build_user_message
from .schema import CallClassification, StageVerdict

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
    """True if the funnel ordering is violated (Lead w/o RPC, or Qualified w/o Lead)."""
    if v.is_lead.value and not v.rpc_connect.value:
        return True
    if v.qualified.value and not v.is_lead.value:
        return True
    return False


def min_quality_confidence(v: CallClassification) -> float:
    return min(getattr(v, stage).confidence for stage in QUALITY_STAGES)


def _all_false_verdict(outcome: str, note: str) -> CallClassification:
    sv = lambda: StageVerdict(value=False, confidence=1.0, evidence=note)  # noqa: E731
    return CallClassification(
        rpc_connect=sv(),
        full_pitch=sv(),
        is_lead=sv(),
        qualified=sv(),
        meeting_booked=sv(),
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

    def classify_one(self, call_row: dict, transcript_row: dict) -> dict:
        """Classify a single transcribed call -> a `classifications` record dict."""
        call_id = str(call_row["call_id"])

        # CDR pre-filter: don't spend an LLM call on a dial that never connected.
        if not call_row.get("answered") or call_row.get("is_voicemail"):
            outcome = "voicemail" if call_row.get("is_voicemail") else "other"
            verdict = _all_false_verdict(outcome, "CDR pre-filter: not a live conversation")
            return self._record(call_id, verdict, model="cdr_prefilter", needs_review=False)

        text = (transcript_row.get("text") or "")[: self._s.llm_max_transcript_chars]
        user = build_user_message(text, transcript_row.get("sentiment"), transcript_row.get("summary"))
        verdict, model_used = self._run_with_escalation(user)

        needs_review = (
            min_quality_confidence(verdict) < self._s.confidence_threshold
            or monotonicity_violation(verdict)
        )
        return self._record(call_id, verdict, model=model_used, needs_review=needs_review)

    @staticmethod
    def _record(call_id: str, v: CallClassification, *, model: str, needs_review: bool) -> dict:
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
                call_outcome, evidence, model, classified_at, needs_human_review)
            VALUES (
                %(call_id)s, %(rpc_connect)s, %(rpc_confidence)s, %(full_pitch)s, %(pitch_confidence)s,
                %(is_lead)s, %(lead_confidence)s, %(qualified)s, %(qual_confidence)s, %(meeting_booked)s,
                %(call_outcome)s, %(evidence)s, %(model)s, %(classified_at)s, %(needs_human_review)s)
            ON CONFLICT (call_id) DO UPDATE SET
                rpc_connect = EXCLUDED.rpc_connect, rpc_confidence = EXCLUDED.rpc_confidence,
                full_pitch = EXCLUDED.full_pitch, pitch_confidence = EXCLUDED.pitch_confidence,
                is_lead = EXCLUDED.is_lead, lead_confidence = EXCLUDED.lead_confidence,
                qualified = EXCLUDED.qualified, qual_confidence = EXCLUDED.qual_confidence,
                meeting_booked = EXCLUDED.meeting_booked, call_outcome = EXCLUDED.call_outcome,
                evidence = EXCLUDED.evidence, model = EXCLUDED.model,
                classified_at = EXCLUDED.classified_at,
                needs_human_review = EXCLUDED.needs_human_review
            """,
            rec,
        )
        conn.commit()


def _pending_calls_for_day(pool: ConnectionPool, day: date) -> list[dict]:
    """In-scope, transcribed, not-yet-classified calls for one day, with transcript."""
    start = datetime.combine(day, time.min)
    end = start + timedelta(days=1)
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT c.call_id, c.answered, c.is_voicemail,
                   t.text, t.sentiment, t.summary, t.diarized
            FROM calls c
            JOIN transcripts t ON t.call_id = c.call_id
            LEFT JOIN classifications cl ON cl.call_id = c.call_id
            WHERE c.started_at >= %(start)s AND c.started_at < %(end)s
              AND c.has_transcript AND c.in_scope AND cl.call_id IS NULL
            """,
            {"start": start, "end": end},
        )
        return cur.fetchall()


def classify_day(pool: ConnectionPool, settings: Settings, day: date) -> dict:
    """Classify all pending transcribed in-scope calls for a day. Idempotent."""
    pending = _pending_calls_for_day(pool, day)
    if not pending:
        return {"classified": 0, "skipped": 0, "needs_review": 0}

    clf = Classifier(settings)
    classified = needs_review = 0
    for row in pending:
        transcript_row = {
            "text": row.get("text"),
            "sentiment": row.get("sentiment"),
            "summary": row.get("summary"),
        }
        rec = clf.classify_one(row, transcript_row)
        upsert_classification(pool, rec)
        classified += 1
        needs_review += int(rec["needs_human_review"])

    log.info("classify_day_done", day=str(day), classified=classified, needs_review=needs_review)
    return {"classified": classified, "skipped": 0, "needs_review": needs_review}
