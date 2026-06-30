"""Ingest one day of Aircall calls for the in-scope BDE/BDM agents.

Only calls whose agent (call.user) matches a ROSTER_INSCOPE_NAMES entry are stored —
so non-BDE Aircall users are ignored. Each in-scope agent is registered in bde_agents
under a synthetic `aircall:<user_id>` extension with their canonical BDE name, so the
dashboard merges their Aircall activity with any 3CX lines of the same name. Idempotent.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

from psycopg_pool import ConnectionPool

from ..config import Settings
from ..logging import get_logger
from ..roster import canonical_bde_name
from .api import AircallClient
from .calls import day_window_unix, to_canonical

log = get_logger(__name__)

_AIRCALL_EXT_PREFIX = "aircall:"


def _inscope_agents(client: AircallClient, settings: Settings) -> dict[int, dict]:
    """Aircall user_id -> {name, email} for users whose name matches an in-scope BDE.
    Uses settings.aircall_agents (AIRCALL_AGENT_NAMES, falling back to ROSTER_INSCOPE_NAMES)
    so Aircall works even when 3CX is scoped by groups."""
    names = settings.aircall_agents
    if not names:
        return {}
    agents: dict[int, dict] = {}
    for u in client.list_users():
        canon = canonical_bde_name(u.get("name") or "", names)
        if canon and u.get("id") is not None:
            agents[u["id"]] = {"name": canon, "email": u.get("email")}
    return agents


def _register_agents(pool: ConnectionPool, agents: dict[int, dict]) -> None:
    """Upsert in-scope Aircall agents into bde_agents (additive; never disturbs 3CX rows)."""
    now = datetime.now(timezone.utc)
    with pool.connection() as conn, conn.cursor() as cur:
        for uid, info in agents.items():
            cur.execute(
                "INSERT INTO bde_agents (extension, bde_name, email, group_name, role_name, "
                "                        in_scope, active, synced_at) "
                "VALUES (%s, %s, %s, 'Aircall', NULL, true, true, %s) "
                "ON CONFLICT (extension) DO UPDATE SET "
                "    bde_name = EXCLUDED.bde_name, email = EXCLUDED.email, "
                "    in_scope = true, active = true, synced_at = EXCLUDED.synced_at",
                (f"{_AIRCALL_EXT_PREFIX}{uid}", info["name"], info.get("email"), now),
            )
        conn.commit()


def ingest_day_aircall(
    client: AircallClient,
    analytics_pool: ConnectionPool,
    settings: Settings,
    day: date,
) -> dict:
    """Ingest in-scope Aircall calls for `day`. Returns {calls, transcribed, in_scope}."""
    agents = _inscope_agents(client, settings)
    if not agents:
        log.info("aircall_no_inscope_agents", day=str(day))
        return {"calls": 0, "transcribed": 0, "in_scope": 0}
    _register_agents(analytics_pool, agents)

    frm, to = day_window_unix(day, settings.tz)
    n_calls = 0
    n_skipped = 0  # calls with no in-scope agent (other users / unanswered inbound, user=None)
    numbers: set[str] = set()

    with analytics_pool.connection() as conn, conn.cursor() as cur:
        for raw in client.iter_calls(frm, to):
            uid = (raw.get("user") or {}).get("id")
            if uid not in agents:          # skip non-BDE Aircall users entirely
                n_skipped += 1
                continue
            r = to_canonical(raw)
            if r["dest_number"]:
                numbers.add(r["dest_number"])
            n_calls += 1
            cur.execute(
                """
                INSERT INTO calls (
                    call_id, bde_extension, bde_name, direction, dest_number,
                    started_at, ring_seconds, talk_seconds, answered, is_voicemail,
                    call_type, recording_present, recording_id, has_transcript,
                    fresh_or_followup, in_scope, provider, lead_id)
                VALUES (
                    %(call_id)s, %(ext)s, %(name)s, %(dir)s, %(dest)s,
                    %(st)s, %(ring)s, %(talk)s, %(ans)s, %(vm)s,
                    %(disp)s, %(rec)s, %(rec_id)s, false,
                    'fresh', true, 'aircall', NULL)
                ON CONFLICT (call_id) DO UPDATE SET
                    bde_extension = EXCLUDED.bde_extension, bde_name = EXCLUDED.bde_name,
                    direction = EXCLUDED.direction, dest_number = EXCLUDED.dest_number,
                    started_at = EXCLUDED.started_at, ring_seconds = EXCLUDED.ring_seconds,
                    talk_seconds = EXCLUDED.talk_seconds, answered = EXCLUDED.answered,
                    is_voicemail = EXCLUDED.is_voicemail, call_type = EXCLUDED.call_type,
                    recording_present = EXCLUDED.recording_present,
                    recording_id = COALESCE(EXCLUDED.recording_id, calls.recording_id),
                    -- never downgrade a transcript we already produced via STT
                    in_scope = EXCLUDED.in_scope, provider = 'aircall'
                """,
                {
                    "call_id": r["call_id"], "ext": f"{_AIRCALL_EXT_PREFIX}{uid}",
                    "name": agents[uid]["name"], "dir": r["direction"], "dest": r["dest_number"],
                    "st": r["started_at"], "ring": r["ring_seconds"], "talk": r["talk_seconds"],
                    "ans": r["answered"], "vm": r["is_voicemail"], "disp": r["disposition"],
                    "rec": r["recording_present"], "rec_id": r["recording_id"],
                },
            )
        conn.commit()

    # Fresh/followup is computed across ALL providers for a number (3CX + Aircall),
    # so a prospect dialled on both systems is ordered correctly.
    from ..ingest import recompute_fresh_followup
    recompute_fresh_followup(analytics_pool, list(numbers))

    log.info("aircall_ingest_done", day=str(day), calls=n_calls,
             skipped_no_inscope_agent=n_skipped, agents=len(agents))
    return {"calls": n_calls, "transcribed": 0, "in_scope": n_calls}
