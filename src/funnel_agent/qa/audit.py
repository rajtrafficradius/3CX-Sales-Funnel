"""Phase 0 — the ``qa_audit`` marker table + a guarded event logger.

Every QA gate that changes (or would have changed) an outcome, scrubs a variable, or blocks a phrase
writes ONE row here so the divergence between Retell's analytics and our own verdicts is auditable.
The table self-creates (``CREATE TABLE IF NOT EXISTS``, idempotent) exactly like ``short_links`` /
``emma_events`` do, so it works whether or not ``init-db`` has run yet. Logging NEVER raises — a QA
audit failure must never break a call, a webhook, or a send.
"""

from __future__ import annotations

from psycopg.types.json import Json
from psycopg_pool import ConnectionPool

from ..logging import get_logger

log = get_logger(__name__)


def ensure_qa_audit(pool: ConnectionPool) -> None:
    """Create the qa_audit marker table + its indexes if absent. Idempotent; safe to call every write."""
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "CREATE TABLE IF NOT EXISTS qa_audit ("
            "  id         bigserial PRIMARY KEY,"
            "  gate       text,"          # 'G1', 'G8', 'G11/G12', ...
            "  agent      text,"          # 'Lisa', 'Lisa 4', 'Lisa 5', ...
            "  call_id    text,"
            "  kind       text,"          # 'unbooked' | 'outbound_sanitized' | 'scrub' | ...
            "  detail     jsonb,"
            "  created_at timestamptz DEFAULT now())")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_qa_audit_gate ON qa_audit (gate, created_at)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_qa_audit_call ON qa_audit (call_id)")
        conn.commit()


def log_event(pool: ConnectionPool, *, gate: str, kind: str, call_id: str | None = None,
              agent: str | None = None, detail: dict | None = None) -> None:
    """Record ONE QA event. Fully guarded — logs a warning and returns on any failure, never raises."""
    try:
        ensure_qa_audit(pool)
        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO qa_audit (gate, agent, call_id, kind, detail) VALUES (%s,%s,%s,%s,%s)",
                (gate, agent, call_id, kind, Json({k: v for k, v in (detail or {}).items() if v is not None})))
            conn.commit()
    except Exception as exc:
        log.warning("qa_audit_log_failed", gate=gate, kind=kind, error=str(exc)[:120])
