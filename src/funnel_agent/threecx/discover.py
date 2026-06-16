"""Phase B — schema discovery against the live 3CX Postgres (READ-ONLY).

Produces the facts needed to fill the CDR_* / TRANSCRIPT_* config and writes
`SCHEMA_NOTES.md`. Nothing downstream hardcodes 3CX schema; it all reads the
config values that this discovery helps you set.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

from psycopg_pool import ConnectionPool

from ..logging import get_logger

log = get_logger(__name__)

# Terms that hint a column/table is about transcripts, recordings, or text.
_HINT_TERMS = [
    "transcript",
    "recording",
    "summary",
    "sentiment",
    "segment",
    "diariz",
    "speaker",
    "text",
    "audio",
]
_PII_COL_HINT = re.compile(r"(number|caller|callee|cid|did|phone|name|text|transcript)", re.I)


def _redact(col: str, value: object) -> object:
    """Mask likely-PII values for safe display in SCHEMA_NOTES.md."""
    if value is None:
        return None
    if _PII_COL_HINT.search(col):
        s = str(value)
        if len(s) <= 4:
            return "***"
        return s[:3] + "***" + s[-2:] if s[:3].isdigit() else s[:8] + "…[redacted]"
    s = str(value)
    return s if len(s) <= 60 else s[:60] + "…"


def list_columns(pool: ConnectionPool, table: str) -> list[dict]:
    sql = """
        SELECT column_name, data_type, is_nullable
        FROM information_schema.columns
        WHERE table_name = %s
        ORDER BY ordinal_position
    """
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(sql, (table,))
        return cur.fetchall()


def sample_rows(pool: ConnectionPool, table: str, limit: int = 3) -> list[dict]:
    # table is from trusted config / discovery; still keep it parameter-free & quoted.
    from psycopg import sql as _sql

    q = _sql.SQL("SELECT * FROM {} LIMIT %s").format(_sql.Identifier(table))
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(q, (limit,))
        rows = cur.fetchall()
    return [{k: _redact(k, v) for k, v in row.items()} for row in rows]


def find_candidate_tables(pool: ConnectionPool) -> list[dict]:
    """Find tables whose name or columns hint at transcripts/recordings/text."""
    like = " OR ".join(["c.column_name ILIKE %s"] * len(_HINT_TERMS))
    like_tbl = " OR ".join(["t.table_name ILIKE %s"] * len(_HINT_TERMS))
    sql = f"""
        SELECT t.table_name,
               array_agg(DISTINCT c.column_name) FILTER (WHERE {like}) AS hint_columns
        FROM information_schema.tables t
        JOIN information_schema.columns c ON c.table_name = t.table_name
        WHERE t.table_schema = 'public'
          AND (({like}) OR ({like_tbl}))
        GROUP BY t.table_name
        ORDER BY t.table_name
    """
    params = (
        [f"%{w}%" for w in _HINT_TERMS]  # FILTER
        + [f"%{w}%" for w in _HINT_TERMS]  # column WHERE
        + [f"%{w}%" for w in _HINT_TERMS]  # table WHERE
    )
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def discover(pool: ConnectionPool, cdr_table: str = "cdr_output") -> dict:
    """Run all discovery probes and return a findings dict."""
    findings: dict = {"cdr_table": cdr_table}
    try:
        findings["cdr_columns"] = list_columns(pool, cdr_table)
        findings["cdr_sample"] = sample_rows(pool, cdr_table, 3)
    except Exception as exc:  # table name may differ on this instance
        findings["cdr_error"] = str(exc)
        findings["cdr_columns"] = []
        findings["cdr_sample"] = []
    findings["candidate_tables"] = find_candidate_tables(pool)
    return findings


def render_schema_notes(findings: dict) -> str:
    """Render findings to the SCHEMA_NOTES.md document the operator confirms."""
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    lines: list[str] = [
        "# SCHEMA_NOTES.md — 3CX database findings (Phase B)",
        "",
        f"_Generated {now} by `funnel-agent discover-schema` (read-only)._",
        "",
        "> Confirm these against the live DB and then set the matching `CDR_*` /",
        "> `TRANSCRIPT_*` values in `.env`. Downstream code reads ONLY those config",
        "> values — it never hardcodes 3CX schema.",
        "",
        f"## CDR table: `{findings.get('cdr_table')}`",
        "",
    ]
    if findings.get("cdr_error"):
        lines += [f"**Could not read this table:** `{findings['cdr_error']}`", ""]
    if findings.get("cdr_columns"):
        lines += ["| column | type | nullable |", "|---|---|---|"]
        for c in findings["cdr_columns"]:
            lines.append(
                f"| `{c['column_name']}` | {c['data_type']} | {c['is_nullable']} |"
            )
        lines.append("")
        lines += [
            "**Map these to config (`CDR_COL_*`):**",
            "",
            "- call id -> `CDR_COL_CALL_ID`",
            "- BDE/agent extension -> `CDR_COL_EXTENSION`",
            "- direction (inbound/outbound) -> `CDR_COL_DIRECTION` (+ `CDR_OUTBOUND_VALUE`)",
            "- dialled number -> `CDR_COL_DEST_NUMBER`",
            "- start time -> `CDR_COL_STARTED_AT`",
            "- ring seconds -> `CDR_COL_RING_SECONDS`",
            "- talk seconds -> `CDR_COL_TALK_SECONDS`",
            "- disposition / termination reason -> `CDR_COL_DISPOSITION`",
            "",
        ]
    if findings.get("cdr_sample"):
        lines += ["**Sample rows (PII redacted):**", "", "```"]
        for row in findings["cdr_sample"]:
            lines.append(str(row))
        lines += ["```", ""]

    lines += [
        "## Candidate transcript / recording tables",
        "",
        "Tables whose name or columns hint at transcripts, recordings, summaries,",
        "sentiment, or diarization. Identify which holds transcript **text** and the",
        "**join key** back to the CDR call id.",
        "",
    ]
    cands = findings.get("candidate_tables", [])
    if not cands:
        lines.append("_None found via keyword scan — inspect the schema manually._")
    else:
        lines += ["| table | hint columns |", "|---|---|"]
        for t in cands:
            cols = ", ".join(f"`{c}`" for c in (t.get("hint_columns") or []))
            lines.append(f"| `{t['table_name']}` | {cols} |")
    lines += [
        "",
        "**Set transcript config once confirmed:**",
        "",
        "- `TRANSCRIPT_TABLE`, `TRANSCRIPT_COL_CALL_ID` (join key to CDR),",
        "  `TRANSCRIPT_COL_TEXT`, and optionally `TRANSCRIPT_COL_SENTIMENT`,",
        "  `TRANSCRIPT_COL_SUMMARY`, `TRANSCRIPT_COL_DIARIZED`.",
        "",
        "## Open operational checks",
        "",
        "- [ ] 3CX edition supports transcription (ENT / AI+).",
        "- [ ] Transcript coverage start date (how far back transcripts exist vs recordings).",
        "- [ ] Read-only DB user has SELECT on the CDR + transcript tables.",
        "- [ ] Which extensions are in-scope sales BDEs (Gate Zero #2 / `ROSTER_INSCOPE_*`).",
        "",
    ]
    return "\n".join(lines)
