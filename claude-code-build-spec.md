# 3CX Sales-Funnel AI Agent — Claude Code Build Spec

This is the executable spec. The companion strategy doc (`3cx-funnel-ai-agent-build-plan.md`)
explains the *why*; this document is the *how to build*.

---

## 1. What you're building

A service that reads call data from a **self-hosted 3CX**, classifies each call against the BDE
sales funnel, and produces the funnel report **per individual BDE and as an overall total**. It
runs in two modes:

- **Backfill** (one-time): all available history, using transcripts that already exist.
- **Daily** (recurring): the previous day, every night.

The funnel: **Calls Made -> RPC Connect -> Full Pitch -> Lead -> Qualified Lead -> Meeting Done**,
split **Fresh / Followup**, per BDE and overall.

---

## 2. Locked decisions (do not change without asking)

1. **Source:** self-hosted 3CX at `https://dotmappers.3cx.in:5001`.
2. **BDE roster:** 3CX **Configuration API** — `GET /xapi/v1/Users` (Service Principal auth).
3. **Call records (CDR):** read **read-only** from the 3CX PostgreSQL database, table `cdr_output`
   (V20 U6+ unified table). Exact columns confirmed in Phase B.
4. **Transcripts:** read **read-only** from the 3CX PostgreSQL database. Table/field confirmed in Phase B.
5. **No audio transcription.** Use transcripts as-found. A call with no transcript still counts as
   **Calls Made** but is **skipped for the quality stages** (Pitch/Lead/Qualified). Never download
   or transcribe recording audio.
6. **Storage:** all analytics data lives in a **separate PostgreSQL** (Railway) — never write to the 3CX DB.
7. **Classification:** an LLM with **structured output**, cheap-model-first with escalation of
   low-confidence calls. Provider/model is a config value (OpenAI by default; Anthropic drop-in).
8. **Idempotent + resumable:** re-running any window must never change counts; a watermark tracks progress.
9. **Report granularity:** every output exists **per BDE** and as **overall (`ALL`)**, each split
   Fresh/Followup/Total, with transcript coverage shown.
10. **Orchestration:** a self-contained CLI service — **no orchestrator, no workflow tool, no
    n8n.** Scheduled externally by a system cron entry or a systemd timer (or Railway cron if
    deployed on Railway), running `python -m funnel_agent.cli daily` nightly after midnight
    Australia/Melbourne. Reporting email (if used) goes over SMTP.

---

## 3. Tech stack & project layout

**Stack:** Python 3.12 · `psycopg[binary]` · `httpx` · `openai`/`anthropic` · `pydantic` +
`pydantic-settings` · `typer` · `tenacity` · `structlog`. Deploy on Railway.

```
3cx-funnel-agent/
  pyproject.toml
  Dockerfile
  .env.example
  README.md
  SCHEMA_NOTES.md                # WRITTEN in Phase B (3CX DB findings)
  src/funnel_agent/
    config.py logging.py
    db/ source.py analytics.py schema.sql migrate.py
    threecx/ api.py cdr.py transcripts.py
    roster.py ingest.py
    classify/ schema.py prompt.py classifier.py
    aggregate.py report.py pipeline.py cli.py
  tests/ test_idempotency.py calibration/
```

---

## 5. Build phases (build, then verify)

- **A — Scaffold & connectivity:** config, logging, both DB pools, token flow. Verify:
  `healthcheck` prints 3CX version, SELECTs source DB, connects analytics DB, source pool rejects writes.
- **B — Schema discovery:** `discover-schema` lists `cdr_output` columns, finds recording/transcript
  tables, identifies join key + BDE-leg field. Verify: `SCHEMA_NOTES.md` written; nothing hardcoded.
- **C — Analytics schema:** `schema.sql` + `migrate.py`; `init-db` idempotent.
- **D — Roster sync:** paged `GET /xapi/v1/Users` -> upsert `bde_agents`; `in_scope` from config rule.
- **E — Ingestion:** date-bounded `cdr_output` read -> upsert `calls`; set `has_transcript`;
  compute fresh/followup; attribute to in-scope BDEs. Idempotent.
- **F — Classifier:** one structured-output call per transcribed call; cheap-first escalation;
  monotonicity guardrails; skip LLM for unanswered/voicemail. Upsert `classifications`.
- **G — Aggregation:** recompute `daily_funnel` per day, per BDE + ALL, fresh/followup/combined.
- **H — Run modes:** `pipeline.classify_window(start, end, order)`; `backfill` + `daily` CLI.
- **I — Reporting:** per-BDE + overall funnel as Markdown + JSON; optional email.
- **J — Tests:** idempotency (double-run identical) + calibration (agreement % vs golden set).
- **K — Deploy & schedule:** Dockerfile, Railway service, nightly cron.

See `README.md` for the full per-phase Verify checklist and the strategy doc for rationale.

---

## 6. Classifier schema + prompt — see `src/funnel_agent/classify/schema.py` and `prompt.py`.

Two team inputs fill the `{{...}}` blocks before Phase F is final:
1. The exact **"Full Pitch"** component list.
2. The **"Qualified"** bar (concrete criteria).
Sensible documented defaults ship in `prompt.py` and are clearly marked as PLACEHOLDERS.

---

## 7. Analytics DB DDL — see `src/funnel_agent/db/schema.sql`.

## 8. Idempotency
- `calls`, `transcripts`, `classifications` — upsert on PK `call_id`.
- `daily_funnel` — recompute per day (delete day's rows for scope, then insert). Never append.
- `processing_state.last_processed_date` advanced only after a day fully aggregates.
- The idempotency test is the gate: a double-run must not change any count.
