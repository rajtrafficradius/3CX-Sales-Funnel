"""Read-only transcript reads from the 3CX database.

The transcript table/columns are discovered in Phase B and supplied via
`TranscriptSchema` (config). 3CX publishes no REST endpoint for transcript text,
so this reads the DB directly. If the schema is not yet configured, callers get
an empty result and the call is simply treated as having no transcript.
"""

from __future__ import annotations

from psycopg import sql
from psycopg_pool import ConnectionPool

from ..config import TranscriptSchema


def _build_select(ts: TranscriptSchema) -> sql.Composed:
    cols = [
        sql.SQL("{} AS call_id").format(sql.Identifier(ts.col_call_id)),
        sql.SQL("{} AS text").format(sql.Identifier(ts.col_text)),
    ]
    # Optional columns — only select them if Phase B found them.
    cols.append(
        sql.SQL("{} AS sentiment").format(sql.Identifier(ts.col_sentiment))
        if ts.col_sentiment
        else sql.SQL("NULL AS sentiment")
    )
    cols.append(
        sql.SQL("{} AS summary").format(sql.Identifier(ts.col_summary))
        if ts.col_summary
        else sql.SQL("NULL AS summary")
    )
    cols.append(
        sql.SQL("{} AS diarized").format(sql.Identifier(ts.col_diarized))
        if ts.col_diarized
        else sql.SQL("NULL AS diarized")
    )
    return sql.SQL("SELECT {cols} FROM {table}").format(
        cols=sql.SQL(", ").join(cols), table=sql.Identifier(ts.table)
    )


def fetch_transcript(pool: ConnectionPool, ts: TranscriptSchema, call_id: str) -> dict | None:
    """Return {call_id, text, sentiment, summary, diarized} or None if absent."""
    if not ts.configured:
        return None
    q = _build_select(ts) + sql.SQL(" WHERE {key} = %(cid)s LIMIT 1").format(
        key=sql.Identifier(ts.col_call_id)
    )
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(q, {"cid": call_id})
        row = cur.fetchone()
    if not row or not (row.get("text") or "").strip():
        return None
    return row


def fetch_transcripts_for_calls(
    pool: ConnectionPool, ts: TranscriptSchema, call_ids: list[str]
) -> dict[str, dict]:
    """Batch-fetch transcripts for many call ids -> {call_id: row}."""
    if not ts.configured or not call_ids:
        return {}
    q = _build_select(ts) + sql.SQL(" WHERE {key} = ANY(%(ids)s)").format(
        key=sql.Identifier(ts.col_call_id)
    )
    out: dict[str, dict] = {}
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(q, {"ids": call_ids})
        for row in cur.fetchall():
            if (row.get("text") or "").strip():
                out[str(row["call_id"])] = row
    return out
