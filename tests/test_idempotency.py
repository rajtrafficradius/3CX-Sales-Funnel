"""Phase J — the idempotency gate.

Ingest + classify + aggregate a fixed day TWICE and assert `calls`,
`classifications`, and `daily_funnel` are byte-identical the second time, and
that `ALL` totals equal the sum across in-scope BDEs.

The 3CX source reads and the LLM backend are monkeypatched with fixtures, so
this needs only a Postgres analytics DB (set ANALYTICS_DB_DSN). It runs no
network calls and spends no LLM tokens.
"""

from __future__ import annotations

from datetime import date, datetime

import pytest

from funnel_agent.classify.schema import CallClassification, StageVerdict
from funnel_agent.config import get_settings

DAY = date(2026, 1, 15)
EXT = "101"


# --- fixture data the fake 3CX returns -------------------------------------- #
def _fake_calls():
    base = datetime(2026, 1, 15, 9, 0, 0)
    return [
        {"call_id": "c-fresh-conv", "bde_extension": EXT, "direction": "Outbound",
         "dest_number": "0400000001", "started_at": base, "ring_seconds": 5,
         "talk_seconds": 120, "disposition": "Answered"},
        {"call_id": "c-follow-conv", "bde_extension": EXT, "direction": "Outbound",
         "dest_number": "0400000001", "started_at": datetime(2026, 1, 15, 10, 0, 0),
         "ring_seconds": 4, "talk_seconds": 90, "disposition": "Answered"},
        {"call_id": "c-voicemail", "bde_extension": EXT, "direction": "Outbound",
         "dest_number": "0400000002", "started_at": datetime(2026, 1, 15, 11, 0, 0),
         "ring_seconds": 20, "talk_seconds": 8, "disposition": "Voicemail"},
        {"call_id": "c-no-transcript", "bde_extension": EXT, "direction": "Outbound",
         "dest_number": "0400000003", "started_at": datetime(2026, 1, 15, 12, 0, 0),
         "ring_seconds": 30, "talk_seconds": 0, "disposition": "No Answer"},
    ]


def _fake_earliest(_pool, _cdr, _nums):
    # First call to 0400000001 is c-fresh-conv at 09:00 -> that one is Fresh.
    return {"0400000001": datetime(2026, 1, 15, 9, 0, 0),
            "0400000002": datetime(2026, 1, 15, 11, 0, 0),
            "0400000003": datetime(2026, 1, 15, 12, 0, 0)}


def _fake_transcripts(_pool, _ts, call_ids):
    have = {"c-fresh-conv", "c-follow-conv", "c-voicemail"}
    return {
        cid: {"text": f"[BDE] hello [Prospect] yes interested ({cid})",
              "sentiment": "positive", "summary": "demo", "diarized": True}
        for cid in call_ids if cid in have
    }


class _FakeBackend:
    """Deterministic classifier: stable verdict per call so re-runs match."""

    def classify(self, system, user, model):
        is_fresh = "c-fresh-conv" in user
        return CallClassification(
            rpc_connect=StageVerdict(value=True, confidence=0.98, evidence="two-way convo"),
            full_pitch=StageVerdict(value=True, confidence=0.9, evidence="all components"),
            is_lead=StageVerdict(value=is_fresh, confidence=0.88, evidence="interest"),
            qualified=StageVerdict(value=False, confidence=0.82, evidence="revenue unknown"),
            meeting_booked=StageVerdict(value=False, confidence=0.95, evidence="none"),
            call_outcome="conversation",
            overall_notes="fixture",
        )


@pytest.fixture
def wired(monkeypatch, analytics_pool):
    import funnel_agent.classify.classifier as clf
    import funnel_agent.ingest as ingest

    monkeypatch.setattr(ingest, "fetch_calls_for_day", lambda *a, **k: _fake_calls())
    monkeypatch.setattr(ingest, "earliest_outbound_times", _fake_earliest)
    monkeypatch.setattr(ingest, "fetch_transcripts_for_calls", _fake_transcripts)
    monkeypatch.setattr(clf, "make_backend", lambda settings: _FakeBackend())

    # Clean slate + an in-scope BDE.
    with analytics_pool.connection() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM daily_funnel WHERE report_date = %s", (DAY,))
        cur.execute("DELETE FROM classifications")
        cur.execute("DELETE FROM transcripts")
        cur.execute("DELETE FROM calls")
        cur.execute("DELETE FROM bde_agents")
        cur.execute(
            "INSERT INTO bde_agents (extension, bde_name, in_scope, active) "
            "VALUES (%s, 'Test BDE', true, true)",
            (EXT,),
        )
        conn.commit()
    return analytics_pool


def _snapshot(pool):
    out = {}
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT * FROM calls ORDER BY call_id")
        out["calls"] = cur.fetchall()
        cur.execute("SELECT * FROM classifications ORDER BY call_id")
        out["classifications"] = cur.fetchall()
        cur.execute("SELECT * FROM daily_funnel ORDER BY bde_name, track")
        out["daily_funnel"] = cur.fetchall()
    return out


def _run_once(pool):
    from funnel_agent.aggregate import aggregate_day
    from funnel_agent.classify.classifier import classify_day
    from funnel_agent.ingest import ingest_day

    settings = get_settings()
    ingest_day(None, pool, settings, DAY)       # source_pool unused (reads monkeypatched)
    classify_day(pool, settings, DAY)
    aggregate_day(pool, settings, DAY)


def test_double_run_is_identical(wired):
    pool = wired
    _run_once(pool)
    snap1 = _snapshot(pool)
    _run_once(pool)
    snap2 = _snapshot(pool)
    assert snap1 == snap2, "second run changed the data — not idempotent"

    # Sanity on the fixture: 4 calls, 3 transcribed, 1 fresh lead.
    assert len(snap1["calls"]) == 4
    assert sum(1 for c in snap1["calls"] if c["has_transcript"]) == 3


def test_all_equals_sum_of_bdes(wired):
    pool = wired
    _run_once(pool)
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT calls_made, rpc_connect, leads FROM daily_funnel "
            "WHERE report_date=%s AND bde_name='ALL' AND track='combined'",
            (DAY,),
        )
        all_row = cur.fetchone()
        cur.execute(
            "SELECT COALESCE(SUM(calls_made),0) AS cm, COALESCE(SUM(rpc_connect),0) AS rpc, "
            "COALESCE(SUM(leads),0) AS l FROM daily_funnel "
            "WHERE report_date=%s AND bde_name<>'ALL' AND track='combined'",
            (DAY,),
        )
        sum_row = cur.fetchone()
    assert all_row["calls_made"] == sum_row["cm"]
    assert all_row["rpc_connect"] == sum_row["rpc"]
    assert all_row["leads"] == sum_row["l"]
