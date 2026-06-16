"""Pure-logic unit tests — no DB, no network, no LLM.

These cover the deterministic core: funnel guardrails, escalation thresholds,
the in-scope rule, conversion math, and day iteration.
"""

from __future__ import annotations

from datetime import date

from funnel_agent.classify.classifier import (
    QUALITY_STAGES,
    min_quality_confidence,
    monotonicity_violation,
)
from funnel_agent.classify.schema import CallClassification, StageVerdict
from funnel_agent.config import Settings
from funnel_agent.pipeline import _days
from funnel_agent.report import _pct
from funnel_agent.roster import decide_in_scope


def _verdict(rpc=True, pitch=True, lead=True, qual=False, confs=(0.9, 0.9, 0.9, 0.9)):
    rc, pc, lc, qc = confs
    return CallClassification(
        rpc_connect=StageVerdict(value=rpc, confidence=rc, evidence="x"),
        full_pitch=StageVerdict(value=pitch, confidence=pc, evidence="x"),
        is_lead=StageVerdict(value=lead, confidence=lc, evidence="x"),
        qualified=StageVerdict(value=qual, confidence=qc, evidence="x"),
        meeting_booked=StageVerdict(value=False, confidence=0.9, evidence="x"),
        call_outcome="conversation",
        overall_notes="x",
    )


# --- guardrails ------------------------------------------------------------- #
def test_monotonicity_ok():
    assert monotonicity_violation(_verdict(rpc=True, lead=True)) is False


def test_monotonicity_lead_without_rpc():
    assert monotonicity_violation(_verdict(rpc=False, lead=True)) is True


def test_monotonicity_qualified_without_lead():
    assert monotonicity_violation(_verdict(rpc=True, lead=False, qual=True)) is True


def test_min_quality_confidence_excludes_rpc():
    # rpc confidence is low but is NOT a quality stage -> ignored by escalation trigger.
    v = _verdict(confs=(0.1, 0.8, 0.75, 0.95))  # (rpc, pitch, lead, qual)
    assert min_quality_confidence(v) == 0.75
    assert "rpc_connect" not in QUALITY_STAGES


# --- in-scope rule ---------------------------------------------------------- #
def _user(ext, groups):
    return {"Number": ext, "Groups": [{"Name": g} for g in groups]}


def test_inscope_by_extension_allowlist():
    s = Settings(roster_inscope_extensions="101,105")
    assert decide_in_scope(_user("105", ["Admin"]), s) is True
    assert decide_in_scope(_user("110", ["Sales"]), s) is False


def test_inscope_by_group():
    s = Settings(roster_inscope_extensions="", roster_inscope_groups="Sales,BDE")
    assert decide_in_scope(_user("110", ["BDE"]), s) is True
    assert decide_in_scope(_user("111", ["Support"]), s) is False


def test_inscope_default_false():
    s = Settings(roster_inscope_extensions="", roster_inscope_groups="")
    assert decide_in_scope(_user("110", ["Sales"]), s) is False


# --- conversion math -------------------------------------------------------- #
def test_pct():
    assert _pct(68, 73) == "93%"
    assert _pct(0, 0) == "—"
    assert _pct(1, 2) == "50%"


# --- day iteration ---------------------------------------------------------- #
def test_days_ascending():
    assert _days(date(2026, 1, 1), date(2026, 1, 3), "asc") == [
        date(2026, 1, 1), date(2026, 1, 2), date(2026, 1, 3)
    ]


def test_days_newest_first():
    assert _days(date(2026, 1, 1), date(2026, 1, 3), "newest_first") == [
        date(2026, 1, 3), date(2026, 1, 2), date(2026, 1, 1)
    ]


def test_days_empty_when_start_after_end():
    assert _days(date(2026, 1, 5), date(2026, 1, 1), "asc") == []
