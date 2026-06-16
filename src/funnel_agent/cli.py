"""Typer CLI — the operator surface for every phase.

  healthcheck      Phase A connectivity + read-only proof
  discover-schema  Phase B 3CX DB discovery -> SCHEMA_NOTES.md
  init-db          Phase C apply analytics schema (idempotent)
  roster-sync      Phase D sync /xapi/v1/Users -> bde_agents
  ingest           Phase E ingest a day (or range)
  classify         Phase F classify a day
  aggregate        Phase G recompute daily_funnel for a day
  backfill         Phase H one-time all-history run (resumable)
  daily            Phase H nightly run (previous day + look-back sweep)
  report           Phase I render the funnel report
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import typer

from .config import Settings, get_settings
from .logging import configure_logging, get_logger

app = typer.Typer(add_completion=False, help="3CX Sales-Funnel AI Reporting Agent")
log = get_logger(__name__)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _settings() -> Settings:
    configure_logging()
    return get_settings()


@contextmanager
def _source_pool(settings: Settings):
    from .db.source import make_source_pool

    pool = make_source_pool(settings.source_db_dsn)
    pool.open()
    try:
        yield pool
    finally:
        pool.close()


@contextmanager
def _analytics_pool(settings: Settings):
    from .db.analytics import make_analytics_pool

    pool = make_analytics_pool(settings.analytics_db_dsn)
    pool.open()
    try:
        yield pool
    finally:
        pool.close()


def _parse_date(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def _yesterday(settings: Settings) -> date:
    now = datetime.now(ZoneInfo(settings.tz))
    return (now - timedelta(days=1)).date()


# --------------------------------------------------------------------------- #
# Phase A — healthcheck
# --------------------------------------------------------------------------- #
@app.command()
def healthcheck() -> None:
    """Verify 3CX API, source DB (read-only), and analytics DB connectivity."""
    settings = _settings()
    ok = True

    # 1. 3CX Configuration API + version.
    try:
        from .threecx.api import ThreeCXClient

        with ThreeCXClient(settings) as client:
            version = client.get_version()
        typer.echo(f"[ok] 3CX Configuration API reachable — version {version}")
    except Exception as exc:
        ok = False
        typer.echo(f"[FAIL] 3CX Configuration API: {exc}")

    # 2. Source DB: a SELECT must work; a write must be rejected.
    try:
        with _source_pool(settings) as pool, pool.connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT 1 AS ok")
            cur.fetchone()
            typer.echo("[ok] Source 3CX DB: SELECT succeeded")
            try:
                cur.execute("CREATE TEMP TABLE _ro_probe (x int)")
                conn.rollback()
                ok = False
                typer.echo("[FAIL] Source DB accepted a write — it is NOT read-only!")
            except Exception:
                conn.rollback()
                typer.echo("[ok] Source 3CX DB rejects writes (read-only enforced)")
    except Exception as exc:
        ok = False
        typer.echo(f"[FAIL] Source 3CX DB: {exc}")

    # 3. Analytics DB.
    try:
        with _analytics_pool(settings) as pool, pool.connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT 1 AS ok")
            cur.fetchone()
            typer.echo("[ok] Analytics DB: connection succeeded")
    except Exception as exc:
        ok = False
        typer.echo(f"[FAIL] Analytics DB: {exc}")

    if not ok:
        raise typer.Exit(code=1)
    typer.echo("healthcheck: ALL OK")


# --------------------------------------------------------------------------- #
# Phase B — discover-schema
# --------------------------------------------------------------------------- #
@app.command(name="discover-schema")
def discover_schema(
    out: str = typer.Option("SCHEMA_NOTES.md", help="Where to write findings"),
) -> None:
    """Probe the 3CX DB (read-only) and write SCHEMA_NOTES.md."""
    settings = _settings()
    from .threecx.discover import discover, render_schema_notes

    with _source_pool(settings) as pool:
        findings = discover(pool, settings.cdr.table)
    notes = render_schema_notes(findings)
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(notes)
    typer.echo(f"Wrote {out} ({len(findings.get('cdr_columns', []))} CDR columns, "
               f"{len(findings.get('candidate_tables', []))} candidate transcript tables)")


# --------------------------------------------------------------------------- #
# Phase C — init-db
# --------------------------------------------------------------------------- #
@app.command(name="init-db")
def init_db() -> None:
    """Apply the analytics schema (idempotent)."""
    settings = _settings()
    from .db.migrate import apply_schema

    with _analytics_pool(settings) as pool:
        apply_schema(pool)
    typer.echo("init-db: schema applied")


# --------------------------------------------------------------------------- #
# Phase D — roster-sync
# --------------------------------------------------------------------------- #
@app.command(name="roster-sync")
def roster_sync() -> None:
    """Sync the BDE roster from 3CX /xapi/v1/Users into bde_agents."""
    settings = _settings()
    from .roster import sync_roster
    from .threecx.api import ThreeCXClient

    with _analytics_pool(settings) as pool, ThreeCXClient(settings) as client:
        stats = sync_roster(pool, client, settings)
    typer.echo(f"roster-sync: {stats}")


# --------------------------------------------------------------------------- #
# Phase E/F/G — single-day commands (useful for debugging / re-runs)
# --------------------------------------------------------------------------- #
@app.command()
def ingest(
    date_: str = typer.Option(None, "--date", help="YYYY-MM-DD (single day)"),
    start: str = typer.Option(None, help="range start YYYY-MM-DD"),
    end: str = typer.Option(None, help="range end YYYY-MM-DD"),
) -> None:
    """Ingest CDR + transcripts for a day (or an inclusive range)."""
    settings = _settings()
    from .ingest import ingest_day

    days = _resolve_days(date_, start, end)
    with _source_pool(settings) as src, _analytics_pool(settings) as ana:
        for d in days:
            typer.echo(f"ingest {d}: {ingest_day(src, ana, settings, d)}")


@app.command()
def classify(date_: str = typer.Option(..., "--date", help="YYYY-MM-DD")) -> None:
    """Classify transcribed in-scope calls for a day."""
    settings = _settings()
    from .classify.classifier import classify_day

    with _analytics_pool(settings) as ana:
        typer.echo(f"classify {date_}: {classify_day(ana, settings, _parse_date(date_))}")


@app.command()
def aggregate(date_: str = typer.Option(..., "--date", help="YYYY-MM-DD")) -> None:
    """Recompute daily_funnel for a day."""
    settings = _settings()
    from .aggregate import aggregate_day

    with _analytics_pool(settings) as ana:
        typer.echo(f"aggregate {date_}: {aggregate_day(ana, settings, _parse_date(date_))}")


def _resolve_days(date_: str | None, start: str | None, end: str | None) -> list[date]:
    if date_:
        return [_parse_date(date_)]
    if start and end:
        s, e = _parse_date(start), _parse_date(end)
        return [s + timedelta(days=i) for i in range((e - s).days + 1)]
    raise typer.BadParameter("provide --date OR (--start and --end)")


# --------------------------------------------------------------------------- #
# Phase H — backfill / daily
# --------------------------------------------------------------------------- #
@app.command()
def backfill(
    start: str = typer.Option(None, help="override BACKFILL_START (YYYY-MM-DD)"),
    end: str = typer.Option(None, help="override end (default: yesterday)"),
) -> None:
    """One-time backfill over all history (classification-only). Resumable."""
    settings = _settings()
    from .threecx.cdr import min_call_date
    from .pipeline import classify_window, get_state, set_state

    with _source_pool(settings) as src, _analytics_pool(settings) as ana:
        # Resolve the window start.
        if start:
            start_d = _parse_date(start)
        elif settings.backfill_start:
            start_d = _parse_date(settings.backfill_start)
        else:
            start_d = min_call_date(src, settings.cdr)
            if start_d is None:
                typer.echo("backfill: no calls found in CDR — nothing to do")
                return
        end_d = _parse_date(end) if end else _yesterday(settings)

        # Resume: continue from just-before the watermark if a prior run was interrupted.
        state = get_state(ana)
        resume_end = end_d
        if state and not state["backfill_complete"] and state["last_processed_date"]:
            resume_end = min(end_d, state["last_processed_date"] - timedelta(days=1))

        if resume_end < start_d:
            set_state(ana, start_d, backfill_complete=True)
            typer.echo(f"backfill: already complete through {start_d}")
            return

        typer.echo(f"backfill: {start_d} .. {resume_end} (newest-first, resumable)")
        totals = classify_window(src, ana, settings, start_d, resume_end, order="newest_first")
        set_state(ana, start_d, backfill_complete=True)
        typer.echo(f"backfill done: {totals}")


@app.command()
def daily(
    report_out: str = typer.Option("reports", help="dir for the rendered report"),
    email: bool = typer.Option(False, help="email the report if SMTP is configured"),
) -> None:
    """Nightly run: previous full day + look-back sweep, then render the report."""
    settings = _settings()
    from .pipeline import classify_window

    end_d = _yesterday(settings)
    start_d = end_d - timedelta(days=settings.daily_lookback_days)

    with _source_pool(settings) as src, _analytics_pool(settings) as ana:
        totals = classify_window(src, ana, settings, start_d, end_d, order="asc")
        typer.echo(f"daily done: {totals}")
        _emit_report(ana, settings, end_d, report_out, only_bde=None, only_all=False, email=email)


# --------------------------------------------------------------------------- #
# Phase I — report
# --------------------------------------------------------------------------- #
@app.command()
def report(
    date_: str = typer.Option(..., "--date", help="YYYY-MM-DD"),
    bde: str = typer.Option(None, help="restrict to one BDE (extension or name)"),
    all_: bool = typer.Option(False, "--all", help="overall (ALL) only"),
    out: str = typer.Option("reports", help="output directory"),
    email: bool = typer.Option(False, help="email if SMTP configured"),
) -> None:
    """Render the per-BDE + overall funnel report for a date."""
    settings = _settings()
    with _analytics_pool(settings) as ana:
        _emit_report(ana, settings, _parse_date(date_), out, only_bde=bde, only_all=all_, email=email)


def _emit_report(ana, settings, day, out, *, only_bde, only_all, email) -> None:
    from .report import build_json, build_markdown, send_email, write_report_files

    md = build_markdown(ana, day, only_bde=only_bde, only_all=only_all)
    payload = build_json(ana, day)
    md_path, json_path = write_report_files(md, payload, out, day)
    typer.echo(md)
    typer.echo(f"\n[wrote {md_path} and {json_path}]")
    if email and send_email(settings, f"3CX Funnel Report — {day}", md):
        typer.echo("[emailed]")


if __name__ == "__main__":
    app()
