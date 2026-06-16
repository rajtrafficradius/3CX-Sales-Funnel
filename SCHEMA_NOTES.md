# SCHEMA_NOTES.md — placeholder (Phase B output goes here)

This file is **generated** by:

```bash
funnel-agent discover-schema
```

…run read-only against the live 3CX Postgres at `dotmappers.3cx.in`. It will
list the real `cdr_output` columns, surface candidate transcript/recording
tables, and tell you exactly which `CDR_*` / `TRANSCRIPT_*` values to set in `.env`.

**Do not hardcode 3CX table/column names anywhere in the code.** All schema
details are config (`CdrSchema` / `TranscriptSchema` in `src/funnel_agent/config.py`),
filled from what this discovery confirms.

## To complete Phase B

1. Get a read-only DB user on the 3CX box with SELECT on the CDR + transcript tables.
2. Set `SOURCE_DB_DSN` in `.env`.
3. Run `funnel-agent discover-schema` (this overwrites this file).
4. Confirm the mappings and set `CDR_COL_*`, `CDR_OUTBOUND_VALUE`, and
   `TRANSCRIPT_TABLE` / `TRANSCRIPT_COL_*` in `.env`.

## Operational checks still open (from strategy doc §15)

- [ ] OS / PostgreSQL reachable on the box; read-only user provisioned.
- [ ] 3CX edition supports transcription (ENT / AI+).
- [ ] Transcript-coverage start date (how far back transcripts exist vs recordings).
- [ ] In-scope sales BDE extensions identified (`ROSTER_INSCOPE_*`).
