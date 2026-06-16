# 3CX Sales-Funnel AI Reporting Agent

Reads call data from a **self-hosted 3CX**, classifies each call against the BDE
sales funnel with an LLM, and produces the funnel report **per individual BDE and
as an overall total** — split Fresh / Followup / Total, with honest transcript
coverage.

Funnel: **Calls Made → RPC Connect → Full Pitch → Lead → Qualified Lead → Meeting Done**

Two run modes:

- **Backfill** (one-time): all available history, using transcripts that already exist.
- **Daily** (nightly): the previous full day + a look-back sweep for late transcripts.

> Built from `claude-code-build-spec.md` (the *how*) and `3cx-funnel-ai-agent-build-plan.md` (the *why*).

---

## Locked decisions (see spec §2)

| # | Decision |
|---|---|
| Source | Self-hosted 3CX at `https://dotmappers.3cx.in:5001` |
| Roster | 3CX Configuration API `GET /xapi/v1/Users` (Service Principal auth) |
| CDR | **Read-only** from the 3CX Postgres, table `cdr_output` |
| Transcripts | **Read-only** from the 3CX Postgres (no audio transcription, ever) |
| Storage | A **separate** analytics Postgres — never write to the 3CX DB |
| Classifier | LLM, structured output, cheap-model-first with escalation (OpenAI default, Anthropic drop-in) |
| Idempotency | Re-running any window never changes counts; a watermark tracks progress |
| Granularity | Every output exists per BDE **and** as overall (`ALL`) |

**No-transcript calls still count as Calls Made** but are skipped for the quality
stages (Pitch/Lead/Qualified). The report always shows `transcribed: N of Calls Made`
so partial historical coverage is transparent.

---

## Architecture

```
3CX PBX ──CDR (cdr_output)────────▶ ingest ─▶┐
        ──transcripts (as-found)──▶          │      ┌─────────────┐
        ──/xapi/v1/Users──────────▶ roster ─▶├────▶ │ analytics   │
                                              │      │ Postgres    │
   has transcript? ── no ─▶ Calls Made only   │      └──────┬──────┘
                  └─ yes ─▶ AI classifier ────┘             ▼
                                                    aggregate ▶ daily_funnel
                                                    (per BDE + ALL, fresh/followup)
                                                            ▼
                                              report (markdown + json, email) / dashboard
```

---

## Quick start

```bash
# 1. Install (Python 3.12+)
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

# 2. Configure
cp .env.example .env        # fill in 3CX + DB + LLM credentials

# 3. Phase A — connectivity
funnel-agent healthcheck

# 4. Phase B — discover the 3CX DB schema, then fill CDR_*/TRANSCRIPT_* in .env
funnel-agent discover-schema          # writes SCHEMA_NOTES.md

# 5. Phase C/D — analytics schema + roster
funnel-agent init-db
funnel-agent roster-sync              # review bde_agents.in_scope after first run

# 6. Phase H — backfill all history (resumable), then nightly
funnel-agent backfill
funnel-agent daily

# Reports (Phase I)
funnel-agent report --date 2026-06-15            # per BDE + overall
funnel-agent report --date 2026-06-15 --all      # overall only
funnel-agent report --date 2026-06-15 --bde 105  # one BDE
```

---

## CLI commands

| Command | Phase | What |
|---|---|---|
| `healthcheck` | A | 3CX API version + source DB SELECT + analytics DB + read-only proof |
| `discover-schema` | B | Probe 3CX DB (read-only) → `SCHEMA_NOTES.md` |
| `init-db` | C | Apply analytics schema (idempotent) |
| `roster-sync` | D | `/xapi/v1/Users` → `bde_agents` |
| `ingest --date / --start --end` | E | CDR + transcripts → analytics |
| `classify --date` | F | Classify transcribed in-scope calls |
| `aggregate --date` | G | Recompute `daily_funnel` |
| `backfill` | H | One-time all-history run (resumable from watermark) |
| `daily` | H | Previous day + look-back sweep + report |
| `report --date [--bde EXT \| --all]` | I | Render the funnel report |

---

## Per-phase Verify checklist (from the spec)

- **A** `healthcheck` prints the 3CX version, SELECTs the source DB, connects analytics, and the source pool **rejects writes**.
- **B** `SCHEMA_NOTES.md` written with `cdr_output` columns + candidate transcript tables. Nothing hardcoded — code reads `CDR_*`/`TRANSCRIPT_*` config.
- **C** all tables exist; re-running `init-db` is a no-op.
- **D** `roster-sync` populates `bde_agents`; re-running updates `synced_at` without dupes; departed users get `active=false`.
- **E** one day's row counts match a manual `cdr_output` query; `has_transcript` matches reality; re-ingest changes nothing.
- **F** JSON parses every time; monotonicity holds; escalation fires on low-confidence; re-classify overwrites, never duplicates.
- **G** `ALL` totals equal the sum across in-scope BDEs; fresh + followup = combined for additive stages; re-run stable.
- **H** backfill resumes after interruption; daily run twice leaves counts unchanged.
- **I** output matches `daily_funnel`; coverage line present; overall sums BDEs.
- **J** `pytest` idempotency passes; `run_calibration.py` reports agreement per stage.

---

## Configuration

All settings load from env / `.env` (see `.env.example`). Highlights:

- **`CDR_*` / `TRANSCRIPT_*`** — the 3CX schema mapping. **Set these from Phase B
  discovery.** Defaults are the documented V20 U6+ shape and are almost certainly
  wrong for transcripts until you confirm them.
- **`ROSTER_INSCOPE_GROUPS` / `ROSTER_INSCOPE_EXTENSIONS`** — which extensions
  count as sales BDEs. If both blank, everyone syncs with `in_scope=false` until reviewed.
- **`LLM_MODEL_CHEAP` / `LLM_MODEL_STRONG`** — model strings are **not hardcoded**;
  set the current ones. Cheap runs first; low-confidence quality stages escalate to strong.
- **`CONFIDENCE_THRESHOLD`** (default 0.7) — below this on Pitch/Lead/Qualified →
  `needs_human_review` + escalation.

---

## Two team inputs still needed (before Phase F is final)

The classifier rubric **is** the product's accuracy. Fill these in
`src/funnel_agent/classify/prompt.py` (clearly marked PLACEHOLDER blocks):

1. The exact **"Full Pitch"** component list.
2. The **"Qualified"** bar (concrete criteria).

Sensible defaults from the strategy doc ship in place so the pipeline runs, but
tune them with sales leadership and validate against the golden set (Phase J).

---

## Idempotency & resumability

- `calls`, `transcripts`, `classifications` — upsert on PK `call_id`.
- `daily_funnel` — recomputed per day (delete the day's rows, then insert). Never append.
- `processing_state.last_processed_date` advances only after a day fully aggregates.
- No-transcript calls are recorded once (Calls Made) and never re-sent to the LLM.
- The idempotency test (`tests/test_idempotency.py`) is the gate: a double-run must
  not change any count.

---

## Tests

```bash
pytest tests/test_unit.py                 # pure logic — no DB, no network, always runs
ANALYTICS_DB_DSN=postgresql://... pytest  # includes the DB-backed idempotency test
python tests/calibration/run_calibration.py tests/calibration/golden_set.csv  # spends LLM tokens
```

`tests/test_unit.py` runs with no external services. `tests/test_idempotency.py`
skips automatically unless `ANALYTICS_DB_DSN` is set; it monkeypatches the 3CX
source + LLM, so it needs only a Postgres analytics DB and spends no tokens.

---

## Deploy & schedule (Phase K)

This is a **self-contained Python CLI — no orchestrator, no workflow tool, no n8n.**
Scheduling is plain and external: a system cron entry, a systemd timer, or Railway
cron — each just runs `python -m funnel_agent.cli daily` nightly. Reporting email
(if used) goes over SMTP.

**Setup (anywhere):**
1. Provision the analytics Postgres → set `ANALYTICS_DB_DSN`.
2. Set the 3CX read-only DB DSN, Configuration API creds, and LLM key
   (`SOURCE_DB_DSN`, `THREECX_*`, `LLM_*`), plus the `CDR_*`/`TRANSCRIPT_*` values
   discovered in Phase B. Never bake secrets into the image — read them from env.
3. One-time: `python -m funnel_agent.cli backfill`.

**Schedule the nightly run** (pick one; runs after midnight Australia/Melbourne):

```cron
# crontab — Australia/Melbourne is UTC+10/+11; 18:00 UTC ≈ 04:00–05:00 local
0 18 * * *  cd /opt/funnel-agent && /opt/funnel-agent/.venv/bin/python -m funnel_agent.cli daily --email
```

```ini
# systemd timer — /etc/systemd/system/funnel-agent.timer
[Unit]
Description=Nightly 3CX funnel run
[Timer]
OnCalendar=*-*-* 04:30 Australia/Melbourne
Persistent=true
[Install]
WantedBy=timers.target

# /etc/systemd/system/funnel-agent.service
[Unit]
Description=3CX funnel daily run
[Service]
Type=oneshot
EnvironmentFile=/opt/funnel-agent/.env
WorkingDirectory=/opt/funnel-agent
ExecStart=/opt/funnel-agent/.venv/bin/python -m funnel_agent.cli daily --email
```

**Railway** (if deployed there): deploy from the `Dockerfile`, run `backfill` once as
a one-off job, then add a Railway cron service whose command is
`python -m funnel_agent.cli daily` (secrets from Railway env).

---

## Compliance note

Transcripts are PII. The Australian Privacy Principles and state call-recording
consent rules apply: confirm outbound-call disclosure, restrict transcript access,
and set a retention policy. This service only ever **reads** the 3CX DB and stores
analytics in your own Postgres.
