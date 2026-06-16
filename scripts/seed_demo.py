"""DEV-ONLY: seed the analytics DB with realistic synthetic data.

Lets you run the dashboard / report live without a 3CX DB connection or an LLM
key. If a live roster has already been synced (bde_agents has in-scope reps),
it attributes the sample calls to your REAL BDE names; otherwise it falls back
to fictional names. It writes calls, transcripts, classifications (some flagged
for review), then recomputes daily_funnel via the real aggregation.

NOT for production — these are synthetic numbers for visual/demo purposes only.

    ANALYTICS_DB_DSN=postgresql://... python scripts/seed_demo.py
"""

from __future__ import annotations

import random
from datetime import date, datetime, time, timedelta

from psycopg.types.json import Json

from funnel_agent.aggregate import aggregate_day
from funnel_agent.config import get_settings
from funnel_agent.db.analytics import make_analytics_pool
from funnel_agent.db.migrate import apply_schema

RNG = random.Random(42)
DAYS = 21
FALLBACK_BDES = [
    ("101", "Priya Sharma"),
    ("105", "Daniel Cohen"),
    ("108", "Aisha Khan"),
    ("112", "Marcus Lee"),
]
OUTCOMES = ["conversation", "voicemail", "gatekeeper", "wrong_number"]


def _verdict(stage_val, conf, ev):
    return {"value": stage_val, "confidence": round(conf, 2), "evidence": ev}


def _evidence(rpc, pitch, lead, qual, booked, outcome):
    return {
        "rpc_connect": _verdict(rpc, 0.97 if rpc else 0.9, "Reached the owner directly." if rpc else "Voicemail greeting detected."),
        "full_pitch": _verdict(pitch, RNG.uniform(0.55, 0.95), "Intro + value prop + Strategy Session offer." if pitch else "Pitch interrupted before the offer."),
        "is_lead": _verdict(lead, RNG.uniform(0.5, 0.95), "Asked about pricing and timelines." if lead else "Polite but not interested."),
        "qualified": _verdict(qual, RNG.uniform(0.45, 0.9), "Has a website, decision-maker, plausible fit." if qual else "Revenue not confirmed."),
        "meeting_booked": _verdict(booked, 0.92, "Strategy Session set for next week." if booked else "No appointment agreed."),
        "call_outcome": outcome,
        "overall_notes": "Synthetic demo record.",
    }


def _load_inscope_bdes(pool) -> list[tuple[str, str]]:
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT extension, COALESCE(bde_name, extension) AS name FROM bde_agents "
            "WHERE in_scope AND active ORDER BY extension"
        )
        return [(str(r["extension"]), str(r["name"])) for r in cur.fetchall()]


def main() -> int:
    settings = get_settings()
    if not settings.analytics_db_dsn:
        print("Set ANALYTICS_DB_DSN first.")
        return 2
    pool = make_analytics_pool(settings.analytics_db_dsn)
    pool.open()
    apply_schema(pool)

    real = _load_inscope_bdes(pool)
    bdes = real or FALLBACK_BDES
    using_real = bool(real)

    threshold = settings.confidence_threshold
    today = date(2026, 6, 16)  # fixed for reproducibility
    start = today - timedelta(days=DAYS)

    with pool.connection() as conn:
        with conn.cursor() as cur:
            # Clear demo facts. Keep a live roster if one exists.
            for t in ("daily_funnel", "classifications", "transcripts", "calls"):
                cur.execute(f"DELETE FROM {t}")
            if not using_real:
                cur.execute("DELETE FROM bde_agents")
                for ext, name in bdes:
                    cur.execute(
                        "INSERT INTO bde_agents (extension, bde_name, group_name, role_name, "
                        "in_scope, active, synced_at) VALUES (%s,%s,'Sales','Agent',true,true,%s)",
                        (ext, name, datetime.now()),
                    )
                cur.execute(
                    "INSERT INTO bde_agents (extension, bde_name, group_name, in_scope, active, synced_at)"
                    " VALUES ('900','Reception','Admin',false,true,%s)", (datetime.now(),))

            call_seq = 0
            seen_numbers: dict[str, datetime] = {}
            for d in range(DAYS):
                day = start + timedelta(days=d)
                for ext, name in bdes:
                    n_calls = RNG.randint(25, 55)
                    for _ in range(n_calls):
                        call_seq += 1
                        cid = f"demo-{call_seq:06d}"
                        hour = RNG.randint(9, 17)
                        started = datetime.combine(day, time(hour, RNG.randint(0, 59), RNG.randint(0, 59)))
                        number = f"04{RNG.randint(10000000, 99999999)}"
                        if seen_numbers and RNG.random() < 0.25:
                            number = RNG.choice(list(seen_numbers.keys()))
                        first = seen_numbers.setdefault(number, started)
                        fresh = "fresh" if started <= first else "followup"

                        connected = RNG.random() < 0.62
                        talk = RNG.randint(30, 400) if connected else RNG.randint(0, 12)
                        is_vm = (not connected) and RNG.random() < 0.5
                        has_tx = RNG.random() < 0.85
                        outcome = "conversation" if connected else RNG.choice(OUTCOMES[1:])

                        cur.execute(
                            "INSERT INTO calls (call_id,bde_extension,bde_name,direction,dest_number,"
                            "started_at,ring_seconds,talk_seconds,answered,is_voicemail,call_type,"
                            "recording_present,has_transcript,fresh_or_followup,in_scope,lead_id) "
                            "VALUES (%s,%s,%s,'Outbound',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,true,NULL)",
                            (cid, ext, name, number, started, RNG.randint(2, 25), talk,
                             connected, is_vm, outcome, has_tx, has_tx, fresh),
                        )
                        if not has_tx:
                            continue
                        cur.execute(
                            "INSERT INTO transcripts (call_id,source,diarized,text,sentiment,summary) "
                            "VALUES (%s,'3cx',true,%s,%s,%s)",
                            (cid,
                             f"[BDE] Hi, this is {name} from Traffic Radius...\n[Prospect] "
                             + ("Sure, tell me more about pricing." if connected else "Leave a message.")
                             + f"\n(demo {cid})",
                             RNG.choice(["positive", "neutral", "negative"]),
                             "Outbound prospecting call."),
                        )
                        rpc = connected and not is_vm
                        pitch = rpc and RNG.random() < 0.45
                        lead = pitch and RNG.random() < 0.35
                        qual = lead and RNG.random() < 0.45
                        booked = qual and RNG.random() < 0.5
                        ev = _evidence(rpc, pitch, lead, qual, booked, outcome)
                        min_q = min(ev["full_pitch"]["confidence"], ev["is_lead"]["confidence"],
                                    ev["qualified"]["confidence"])
                        needs_review = min_q < threshold
                        cur.execute(
                            "INSERT INTO classifications (call_id,rpc_connect,rpc_confidence,"
                            "full_pitch,pitch_confidence,is_lead,lead_confidence,qualified,"
                            "qual_confidence,meeting_booked,call_outcome,evidence,model,"
                            "classified_at,needs_human_review) VALUES "
                            "(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                            (cid, rpc, ev["rpc_connect"]["confidence"], pitch,
                             ev["full_pitch"]["confidence"], lead, ev["is_lead"]["confidence"],
                             qual, ev["qualified"]["confidence"], booked, outcome, Json(ev),
                             "demo-cheap", started + timedelta(minutes=20), needs_review),
                        )
        conn.commit()

    for d in range(DAYS):
        aggregate_day(pool, settings, start + timedelta(days=d))

    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM calls")
        calls = cur.fetchone()["n"]
        cur.execute("SELECT count(*) AS n FROM classifications WHERE needs_human_review")
        review = cur.fetchone()["n"]
    pool.close()
    src = "your live in-scope roster" if using_real else "fictional sample names"
    print(f"Seeded {calls} synthetic calls across {DAYS} days for {len(bdes)} BDEs "
          f"({src}); {review} flagged for review. Open the dashboard to view.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
