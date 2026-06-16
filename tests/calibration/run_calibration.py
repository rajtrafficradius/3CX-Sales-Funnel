"""Calibration: agreement % per funnel stage vs a manager's golden set.

Reads a CSV of human labels (call_id,human_rpc,human_pitch,human_lead,human_qualified),
pulls each call's transcript from the analytics DB, runs the live classifier, and
prints per-stage agreement against the targets:

    RPC >= 95%, Full Pitch >= 85%, Lead >= 85%, Qualified >= 80%

Usage:
    python tests/calibration/run_calibration.py tests/calibration/golden_set.csv

Requires ANALYTICS_DB_DSN + LLM_* env (this DOES spend tokens — it calls the model).
"""

from __future__ import annotations

import csv
import sys

TARGETS = {"rpc": 0.95, "pitch": 0.85, "lead": 0.85, "qualified": 0.80}


def _load_golden(path: str) -> list[dict]:
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 2
    path = argv[1]
    rows = _load_golden(path)
    if not rows:
        print("golden set is empty")
        return 1

    from funnel_agent.classify.classifier import Classifier
    from funnel_agent.config import get_settings
    from funnel_agent.db.analytics import make_analytics_pool

    settings = get_settings()
    pool = make_analytics_pool(settings.analytics_db_dsn)
    pool.open()
    clf = Classifier(settings)

    agree = {k: 0 for k in TARGETS}
    counted = {k: 0 for k in TARGETS}
    missing = 0

    try:
        for row in rows:
            call_id = row["call_id"]
            with pool.connection() as conn, conn.cursor() as cur:
                cur.execute(
                    "SELECT c.answered, c.is_voicemail, t.text, t.sentiment, t.summary "
                    "FROM calls c JOIN transcripts t ON t.call_id=c.call_id "
                    "WHERE c.call_id=%s",
                    (call_id,),
                )
                r = cur.fetchone()
            if not r or not (r["text"] or "").strip():
                missing += 1
                continue

            rec = clf.classify_one(
                {"call_id": call_id, "answered": r["answered"], "is_voicemail": r["is_voicemail"]},
                {"text": r["text"], "sentiment": r["sentiment"], "summary": r["summary"]},
            )
            pairs = {
                "rpc": (rec["rpc_connect"], row["human_rpc"]),
                "pitch": (rec["full_pitch"], row["human_pitch"]),
                "lead": (rec["is_lead"], row["human_lead"]),
                "qualified": (rec["qualified"], row["human_qualified"]),
            }
            for stage, (pred, human) in pairs.items():
                counted[stage] += 1
                if bool(pred) == bool(int(human)):
                    agree[stage] += 1
    finally:
        pool.close()

    print(f"\nCalibration over {len(rows)} labelled calls ({missing} missing transcript)\n")
    print(f"{'stage':<12}{'agreement':>12}{'target':>10}{'status':>10}")
    all_pass = True
    for stage, target in TARGETS.items():
        n = counted[stage]
        pct = agree[stage] / n if n else 0.0
        ok = pct >= target
        all_pass &= ok
        print(f"{stage:<12}{pct:>11.0%}{target:>10.0%}{'PASS' if ok else 'BELOW':>10}")
    return 0 if all_pass else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
