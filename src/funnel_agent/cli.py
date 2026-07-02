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

    # Bucket days in the PBX's local timezone so day boundaries match 3CX reports.
    pool = make_analytics_pool(settings.analytics_db_dsn, session_timezone=settings.tz)
    pool.open()
    try:
        yield pool
    finally:
        pool.close()


@contextmanager
def _source(settings: Settings):
    """Yield the ingestion source (3CX API or DB) per SOURCE_MODE."""
    from .sources import make_source

    src = make_source(settings)
    try:
        yield src
    finally:
        src.close()


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

    # 2. Source of CDR + transcripts.
    if settings.source_mode.lower() == "db":
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
    else:
        try:
            with _source(settings) as src:
                d = src.min_call_date(settings)  # exercises the Recordings API read
            typer.echo(f"[ok] Source = 3CX API (Recordings); earliest outbound recording: {d}")
        except Exception as exc:
            ok = False
            typer.echo(f"[FAIL] Source 3CX API (Recordings): {exc}")

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


@app.command(name="seed-prospects")
def seed_prospects(
    csv_path: str = typer.Argument(..., help="Path to DATA_MASTER_FILE CSV"),
) -> None:
    """Seed/refresh the master prospect database from the master CSV (idempotent)."""
    settings = _settings()
    from .prospects import seed_prospects_from_csv

    with _analytics_pool(settings) as pool:
        stats = seed_prospects_from_csv(pool, csv_path)
    typer.echo(f"seed-prospects: {stats}")


@app.command(name="load-companies")
def load_companies(
    csv_path: str = typer.Argument(..., help="Path to the given business data CSV (e.g. D&B export)"),
    source: str = typer.Option("given_data", help="tag for this upload (re-running replaces this source's rows)"),
) -> None:
    """Load ALL given business records into `companies` (every business kept; multi-per-domain OK)."""
    settings = _settings()
    from .companies import load_companies_from_csv

    with _analytics_pool(settings) as pool:
        stats = load_companies_from_csv(pool, csv_path, source=source)
    typer.echo(f"load-companies: {stats}")


@app.command(name="enrich-websites")
def enrich_websites_cmd(
    limit: int = typer.Option(500, help="how many not-yet-scanned domains to scan this run"),
    workers: int = typer.Option(8, help="parallel website fetches (sync mode)"),
    all_: bool = typer.Option(False, "--all", help="keep scanning in batches until none remain"),
    use_async: bool = typer.Option(False, "--async", help="high-throughput async scanner (for 100k–1M scale)"),
    concurrency: int = typer.Option(300, help="async: simultaneous homepage fetches"),
    batch: int = typer.Option(1000, help="async: domains scanned + bulk-written per batch"),
    max_kb: int = typer.Option(2000, help="async: cap homepage download at N KB (2 MB keeps detection parity)"),
    timeout: float = typer.Option(8.0, help="async: per-request read timeout (s) — fail fast on the slow tail"),
) -> None:
    """FREE tracking-pixel scan (GTM/GA4/Google Ads/Meta/etc.) across ALL domains to flag
    paid-ads activity. Idempotent; safe to re-run. Runs automatically in the refresh loop too.

    Use --async (hundreds of concurrent fetches + range-capped downloads) for large runs;
    the default sync mode (8 threads) is fine for the steady refresh-loop trickle."""
    settings = _settings()

    with _analytics_pool(settings) as pool:
        if use_async:
            from .enrich import enrich_websites_async
            stats = enrich_websites_async(pool, limit=limit, concurrency=concurrency, batch=batch,
                                          scan_all=all_, per_timeout=timeout, max_bytes=max_kb * 1000)
            typer.echo(f"enrich-websites (async): {stats}")
            return
        from .enrich import enrich_websites
        while True:
            stats = enrich_websites(pool, limit=limit, workers=workers)
            typer.echo(f"enrich-websites: {stats}")
            if not all_ or stats["scanned"] == 0 or stats["remaining"] == 0:
                break


@app.command(name="enrich-deep")
def enrich_deep_cmd(
    limit: int = typer.Option(1000, help="max paid-ads prospects to deep-enrich this run"),
    workers: int = typer.Option(6, help="parallel workers"),
    whois: bool = typer.Option(True, help="also run free WHOIS (off for bulk .au — auDA throttles)"),
    gated: bool = typer.Option(True, help="--no-gated = enrich EVERY $1-10M domain, not just paid-ads"),
) -> None:
    """Deep-enrich the PAID-ADS prospects in the $1-10M band: Apollo company + decision-maker
    names/titles (FREE, no credits) + website business intel (products/services/USPs/ICP).
    Each source stored separately. Idempotent — re-run to continue where it left off.
    Pass --no-gated to cover the WHOLE $1-10M segment (so no core prospect shows empty Apollo)."""
    settings = _settings()
    from .enrich import enrich_prospects_deep

    with _analytics_pool(settings) as pool:
        stats = enrich_prospects_deep(pool, settings, limit=limit, workers=workers,
                                      with_whois=whois, gated=gated)
    typer.echo(f"enrich-deep: {stats}")


@app.command(name="messages")
def messages_cmd() -> None:
    """Ingest inbound 3CX SMS/chat, classify intent, and firm up bookings confirmed by text.
    Same step the refresh loop runs each cycle — run manually to backfill / test."""
    settings = _settings()
    from .threecx.api import ThreeCXClient
    from .messages import process_messages

    with _analytics_pool(settings) as pool, ThreeCXClient(settings) as tcx:
        stats = process_messages(tcx, pool, settings)
    typer.echo(f"messages: {stats}")


@app.command(name="enrich-calls")
def enrich_calls_cmd(
    limit: int = typer.Option(100000, help="max call-prospect domains to backfill"),
    workers: int = typer.Option(8, help="parallel workers"),
    dataforseo: bool = typer.Option(False, help="also run DataForSEO (PAID ~$0.012/domain)"),
    whois: bool = typer.Option(False, help="also run WHOIS now (off = lazy on-demand; auDA throttles bulk .au)"),
) -> None:
    """Backfill EVERY already-called prospect (3CX/Aircall) that has an identified domain with
    its full FREE enrichment: website + SEMrush + Apollo company/people + business intel. WHOIS
    stays lazy and DataForSEO is paid/off by default — the daily refresh loop runs both forward
    for new prospects. Idempotent — re-run to continue."""
    settings = _settings()
    from .enrich import enrich_call_prospects

    with _analytics_pool(settings) as pool:
        stats = enrich_call_prospects(pool, settings, limit=limit, workers=workers,
                                      with_dataforseo=dataforseo, with_whois=whois)
    typer.echo(f"enrich-calls: {stats}")


@app.command(name="enrich-dataforseo")
def enrich_dataforseo_cmd(
    limit: int = typer.Option(1000, help="max Raghav $1-10M gated domains to DataForSEO-enrich"),
    workers: int = typer.Option(8, help="parallel workers"),
) -> None:
    """DataForSEO enrichment (PAID ~$0.012/domain) for Raghav $1-10M paid-ads-gated domains:
    Google Ads Transparency Center creatives + SEO/paid metrics. Idempotent (skips done)."""
    settings = _settings()
    from .enrich import enrich_dataforseo_batch

    with _analytics_pool(settings) as pool:
        stats = enrich_dataforseo_batch(pool, settings, limit=limit, workers=workers)
    typer.echo(f"enrich-dataforseo: {stats}")


@app.command(name="whatsapp")
def whatsapp_run() -> None:
    """Schedule WhatsApp nurturing for new bookings + send any due messages (#13).
    DRY-RUN until WHATSAPP_ENABLED + credentials are set (then it sends live)."""
    settings = _settings()
    from .whatsapp import schedule_due_bookings, process_due

    with _analytics_pool(settings) as pool:
        sched = schedule_due_bookings(pool, settings, lookback_days=settings.daily_lookback_days)
        proc = process_due(pool, settings)
    typer.echo(f"whatsapp: scheduled={sched} processed={proc}")


@app.command(name="enrich-prospects")
def enrich_prospects_cmd(
    limit: int = typer.Option(10, help="max prospect domains to enrich"),
    pipeline: str = typer.Option(None, help="restrict to a pipeline, e.g. pipeline1_interested"),
    dry_run: bool = typer.Option(False, "--dry-run", help="plan only; calls NO API (no credits)"),
) -> None:
    """Gap-aware enrichment of master-matched prospects (SEMrush only-missing, Apollo free)."""
    settings = _settings()
    from .enrich import enrich_prospects

    with _analytics_pool(settings) as pool:
        stats = enrich_prospects(pool, settings, limit=limit, pipeline=pipeline, dry_run=dry_run)
    typer.echo(f"enrich-prospects{' (dry-run)' if dry_run else ''}: {stats}")


@app.command(name="capture-prospects")
def capture_prospects_cmd() -> None:
    """Add master-DB records for called numbers not yet on file (from AI-extracted name/website)."""
    settings = _settings()
    from .prospects import capture_called_prospects

    with _analytics_pool(settings) as pool:
        stats = capture_called_prospects(pool)
    typer.echo(f"capture-prospects: {stats}")


@app.command(name="pipeline2-sync")
def pipeline2_sync(
    cadence_days: int = typer.Option(None, help="default cadence days when none is known (default: weekly, from config)"),
) -> None:
    """Rebuild Pipeline-2 assignments (rotate a different BDE; weekly cadence by default)."""
    settings = _settings()
    from .pipeline2 import sync_pipeline2

    cad = cadence_days if cadence_days is not None else settings.pipeline2_default_cadence_days
    with _analytics_pool(settings) as pool:
        stats = sync_pipeline2(pool, default_cadence_days=cad)
    typer.echo(f"pipeline2-sync (cadence={cad}d): {stats}")


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

    days = _resolve_days(date_, start, end)
    with _source(settings) as src, _analytics_pool(settings) as ana:
        for d in days:
            typer.echo(f"ingest {d}: {src.ingest_day(ana, settings, d)}")


@app.command()
def classify(
    date_: str = typer.Option(None, "--date", help="single day YYYY-MM-DD"),
    start: str = typer.Option(None, help="range start YYYY-MM-DD"),
    end: str = typer.Option(None, help="range end YYYY-MM-DD"),
    limit: int = typer.Option(None, help="cap calls per day (cost/time control)"),
    workers: int = typer.Option(None, help="parallel LLM workers (default from config)"),
    force: bool = typer.Option(False, "--force", help="re-classify already-classified calls (after a rubric change)"),
) -> None:
    """Classify transcribed in-scope calls for a day or an inclusive range."""
    settings = _settings()
    from .classify.classifier import classify_day

    days = _resolve_days(date_, start, end)
    with _analytics_pool(settings) as ana:
        for d in days:
            typer.echo(f"classify {d}: {classify_day(ana, settings, d, limit, workers, force)}")


@app.command(name="transcribe-missing")
def transcribe_missing_cmd(
    date_: str = typer.Option(None, "--date", help="single day YYYY-MM-DD"),
    start: str = typer.Option(None, help="range start YYYY-MM-DD"),
    end: str = typer.Option(None, help="range end YYYY-MM-DD"),
    limit: int = typer.Option(None, help="cap recordings per day"),
    workers: int = typer.Option(None, help="parallel STT workers"),
) -> None:
    """Download + transcribe in-scope recordings that 3CX left untranscribed."""
    settings = _settings()
    from .transcribe import transcribe_missing_for_day

    days = _resolve_days(date_, start, end)
    with _source(settings) as src, _analytics_pool(settings) as ana:
        if not hasattr(src, "client"):
            typer.echo("transcribe-missing needs SOURCE_MODE=api")
            raise typer.Exit(1)
        for d in days:
            res = transcribe_missing_for_day(src.client, ana, settings, d, limit, workers)
            typer.echo(f"transcribe-missing {d}: {res}")


@app.command()
def aggregate(date_: str = typer.Option(..., "--date", help="YYYY-MM-DD")) -> None:
    """Recompute daily_funnel for a day."""
    settings = _settings()
    from .aggregate import aggregate_day

    with _analytics_pool(settings) as ana:
        typer.echo(f"aggregate {date_}: {aggregate_day(ana, settings, _parse_date(date_))}")


@app.command()
def enrich(
    date_: str = typer.Option(None, "--date", help="single day YYYY-MM-DD"),
    start: str = typer.Option(None, help="range start YYYY-MM-DD"),
    end: str = typer.Option(None, help="range end YYYY-MM-DD"),
    limit: int = typer.Option(None, help="cap domains per day"),
    workers: int = typer.Option(None, help="parallel domain lookups"),
) -> None:
    """Fetch SEMrush + Apollo (free) marketing data per call website (cached per domain)."""
    settings = _settings()
    from .enrich import enrich_day

    days = _resolve_days(date_, start, end)
    with _analytics_pool(settings) as ana:
        for d in days:
            typer.echo(f"enrich {d}: {enrich_day(ana, settings, d, limit, workers)}")


@app.command(name="sync-callbacks")
def sync_callbacks_cmd(
    date_: str = typer.Option(None, "--date", help="single day YYYY-MM-DD"),
    start: str = typer.Option(None, help="range start YYYY-MM-DD"),
    end: str = typer.Option(None, help="range end YYYY-MM-DD"),
) -> None:
    """Auto-create BDE calendar callbacks from interested-prospect callback requests."""
    settings = _settings()
    from .calendar import sync_callbacks_for_day

    days = _resolve_days(date_, start, end)
    with _analytics_pool(settings) as ana:
        for d in days:
            typer.echo(f"sync-callbacks {d}: {sync_callbacks_for_day(ana, d)}")


@app.command(name="users-sync")
def users_sync(
    manager: str = typer.Option(None, help="manager email(s) CSV; default from MANAGER_EMAILS"),
) -> None:
    """Create login accounts: one per in-scope BDE + manager(s). Idempotent.
    Prints one-time temp passwords for newly-created accounts."""
    settings = _settings()
    from .auth import seed_users

    mgrs = [m.strip() for m in (manager.split(",") if manager else settings.manager_email_list) if m.strip()]
    with _analytics_pool(settings) as ana:
        created = seed_users(ana, mgrs)
    if not created:
        typer.echo("users-sync: no new accounts (all already exist).")
        return
    typer.echo(f"users-sync: created {len(created)} account(s). TEMP PASSWORDS (share once):")
    for u in created:
        typer.echo(f"  {u['role']:<8} {u['email']:<42} {u['temp_password']}")


@app.command(name="set-password")
def set_password_cmd(
    email: str = typer.Option(..., help="user email"),
    password: str = typer.Option(..., help="new password"),
) -> None:
    """Set / reset a user's password."""
    settings = _settings()
    from .auth import set_password

    with _analytics_pool(settings) as ana:
        ok = set_password(ana, email, password, must_change=False)
    typer.echo("password updated" if ok else "user not found")


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
    from .pipeline import classify_window, get_state, set_state

    with _source(settings) as src, _analytics_pool(settings) as ana:
        # Resolve the window start.
        if start:
            start_d = _parse_date(start)
        elif settings.backfill_start:
            start_d = _parse_date(settings.backfill_start)
        else:
            start_d = src.min_call_date(settings)
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

    with _source(settings) as src, _analytics_pool(settings) as ana:
        totals = classify_window(src, ana, settings, start_d, end_d, order="asc")
        typer.echo(f"daily done: {totals}")
        _emit_report(ana, settings, end_d, report_out, only_bde=None, only_all=False, email=email)


# --------------------------------------------------------------------------- #
# Phase I — report
# --------------------------------------------------------------------------- #
@app.command()
def refresh(
    days: int = typer.Option(2, help="days back through today to refresh"),
) -> None:
    """Intraday refresh: ingest -> transcribe -> classify -> aggregate from
    (today - days + 1) through TODAY. Run on a 5-minute schedule for a live dashboard."""
    settings = _settings()
    from .pipeline import classify_window
    from .pipeline2 import sync_pipeline2
    from .prospects import capture_called_prospects

    today = datetime.now(ZoneInfo(settings.tz)).date()
    start = today - timedelta(days=max(1, days) - 1)
    with _source(settings) as src, _analytics_pool(settings) as ana:
        totals = classify_window(src, ana, settings, start, today, order="asc")
        # Grow the master DB from live calls (numbers not on file → captured from the
        # AI-extracted business name/website), then keep Pipeline-2 assignments current.
        cap = capture_called_prospects(ana)
        # Mirror Raven (master) + 3CX-captured prospects into `companies` so the Database
        # browser shows all data sources (with the source filter) and stays current.
        from .companies import sync_company_sources_from_prospects
        sync_company_sources_from_prospects(ana)
        p2 = sync_pipeline2(ana, default_cadence_days=settings.pipeline2_default_cadence_days)
        # FREE tracking-pixel scan across the DB (paid-ads detection), a batch per cycle.
        ws = {"scanned": 0}
        if settings.website_scan_per_cycle > 0:
            from .enrich import enrich_websites
            ws = enrich_websites(ana, limit=settings.website_scan_per_cycle,
                                 workers=settings.website_scan_workers)
        # Paced WHOIS fill for running-ads prospects (auDA rate-limits per IP, so a few/cycle).
        wh = {"checked": 0, "found": 0}
        if settings.whois_trickle_per_cycle > 0:
            from .enrich import enrich_whois_trickle
            try:
                wh = enrich_whois_trickle(ana, settings, limit=settings.whois_trickle_per_cycle)
            except Exception as exc:
                log.warning("whois_trickle_failed", error=str(exc)[:160])
        # Paced Apollo decision-maker fill for running-ads prospects missing/stale DMs (Apollo
        # rate-limits bursts, so a few/cycle from the always-on loop).
        ap = {"checked": 0, "updated": 0}
        if settings.apollo_people_trickle_per_cycle > 0:
            from .enrich import enrich_apollo_people_trickle
            try:
                ap = enrich_apollo_people_trickle(ana, settings, limit=settings.apollo_people_trickle_per_cycle)
            except Exception as exc:
                log.warning("apollo_people_trickle_failed", error=str(exc)[:160])
        # Inbound SMS/chat: capture meeting confirmations a prospect TEXTS to a 3CX number and
        # firm up their prior tentative booking (an SMS-only confirmation the calls pipeline misses).
        # Gated off by default until validated end-to-end (set MESSAGES_ENABLED=true to activate).
        msg = {"skipped": True}
        if settings.messages_enabled:
            msg = {"new": 0, "classified": 0, "bookings": 0}
            try:
                from .threecx.api import ThreeCXClient
                from .messages import process_messages
                with ThreeCXClient(settings) as _tcx:
                    msg = process_messages(_tcx, ana, settings)
            except Exception as exc:
                log.warning("messages_failed", error=str(exc)[:160])
        # WhatsApp nurturing: schedule sequences for new bookings + send due messages
        # (dry-run until WHATSAPP_ENABLED + credentials are configured).
        from .whatsapp import schedule_due_bookings, process_due
        schedule_due_bookings(ana, settings, lookback_days=settings.daily_lookback_days)
        wa = process_due(ana, settings)
    typer.echo(f"refresh {start}..{today}: {totals} | captured: {cap} | pipeline2: {p2} | websites: {ws} | whois: {wh} | apollo_dm: {ap} | messages: {msg} | whatsapp: {wa}")


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


# --------------------------------------------------------------------------- #
# Human-review queue (low-confidence / guardrail-flagged calls)
# --------------------------------------------------------------------------- #
@app.command(name="review-queue")
def review_queue(limit: int = typer.Option(50, help="max rows")) -> None:
    """List calls the classifier flagged for human review (low confidence or
    a funnel-monotonicity violation). Manager corrections become calibration data."""
    settings = _settings()
    with _analytics_pool(settings) as ana, ana.connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT cl.call_id, c.bde_name, c.dest_number, c.started_at, cl.call_outcome,
                   LEAST(cl.pitch_confidence, cl.lead_confidence, cl.qual_confidence) AS min_conf,
                   cl.model
            FROM classifications cl JOIN calls c ON c.call_id = cl.call_id
            WHERE cl.needs_human_review
            ORDER BY min_conf ASC NULLS FIRST LIMIT %s
            """,
            (limit,),
        )
        rows = cur.fetchall()
    if not rows:
        typer.echo("review-queue: empty — nothing flagged.")
        return
    typer.echo(f"{'call_id':<22}{'BDE':<16}{'number':<16}{'min_conf':>9}  outcome")
    for r in rows:
        mc = f"{float(r['min_conf']):.2f}" if r["min_conf"] is not None else "—"
        typer.echo(
            f"{str(r['call_id'])[:21]:<22}{(r['bde_name'] or '?')[:15]:<16}"
            f"{(r['dest_number'] or '')[:15]:<16}{mc:>9}  {r['call_outcome'] or ''}"
        )
    typer.echo(f"\n{len(rows)} call(s) awaiting review. Open them in the dashboard for transcript + evidence.")


# --------------------------------------------------------------------------- #
# Dashboard (live web UI)
# --------------------------------------------------------------------------- #
@app.command()
def dashboard(
    host: str = typer.Option("127.0.0.1", help="bind host (0.0.0.0 to expose)"),
    port: int = typer.Option(8080, help="bind port"),
) -> None:
    """Serve the live funnel dashboard (FastAPI + ECharts) over the analytics DB."""
    import uvicorn

    from .dashboard.app import create_app

    settings = _settings()
    typer.echo(f"dashboard: http://{host}:{port}")
    uvicorn.run(create_app(settings), host=host, port=port, log_level="info")


if __name__ == "__main__":
    app()
