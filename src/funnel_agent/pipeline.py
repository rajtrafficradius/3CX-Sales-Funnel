"""Phase H — the shared core: `classify_window` + watermark management.

Per day: ingest (CDR + transcripts) -> classify (transcribed in-scope only) ->
aggregate (per BDE + ALL) -> advance the watermark. Both run modes call this:
  backfill -> classify_window(earliest, yesterday, order='newest_first')
  daily    -> classify_window(yesterday - lookback, yesterday, order='asc')
Everything is idempotent, so re-running a window never changes counts.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from psycopg_pool import ConnectionPool

from .aggregate import aggregate_day
from .classify.classifier import classify_day
from .config import Settings
from .enrich import enrich_day
from .logging import get_logger
from .sources import Source

log = get_logger(__name__)

JOB = "funnel_agent"


# --------------------------------------------------------------------------- #
# Watermark (processing_state)
# --------------------------------------------------------------------------- #
def get_state(pool: ConnectionPool) -> dict | None:
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT job, last_processed_date, backfill_complete, updated_at "
            "FROM processing_state WHERE job = %s",
            (JOB,),
        )
        return cur.fetchone()


def set_state(
    pool: ConnectionPool,
    last_processed_date: date,
    *,
    backfill_complete: bool | None = None,
) -> None:
    now = datetime.now(timezone.utc)
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO processing_state (job, last_processed_date, backfill_complete, updated_at)
            VALUES (%(job)s, %(d)s, COALESCE(%(bc)s, false), %(now)s)
            ON CONFLICT (job) DO UPDATE SET
                last_processed_date = EXCLUDED.last_processed_date,
                backfill_complete = COALESCE(%(bc)s, processing_state.backfill_complete),
                updated_at = EXCLUDED.updated_at
            """,
            {"job": JOB, "d": last_processed_date, "bc": backfill_complete, "now": now},
        )
        conn.commit()


# --------------------------------------------------------------------------- #
# Day iteration
# --------------------------------------------------------------------------- #
def _days(start: date, end: date, order: str) -> list[date]:
    if start > end:
        return []
    days = [start + timedelta(days=i) for i in range((end - start).days + 1)]
    if order == "newest_first":
        days.reverse()
    return days


def classify_window(
    source: Source,
    analytics_pool: ConnectionPool,
    settings: Settings,
    start: date,
    end: date,
    order: str = "asc",
) -> dict:
    """Process every day in [start, end] in `order`, advancing the watermark."""
    days = _days(start, end, order)
    totals = {"days": 0, "calls": 0, "transcribed": 0, "stt_transcribed": 0,
              "classified": 0, "needs_review": 0, "enriched": 0}
    log.info("window_start", start=str(start), end=str(end), order=order, days=len(days))

    for day in days:
        ing = source.ingest_day(analytics_pool, settings, day)
        stt = source.transcribe_missing(analytics_pool, settings, day)  # fill 3CX gaps
        cls = classify_day(analytics_pool, settings, day)
        # Marketing enrichment needs the AI-extracted website, so it runs AFTER
        # classify and only fetches new/stale domains (cheap; never spends Apollo credits).
        enr = {"enriched": 0}
        if settings.enrich_missing:
            try:
                enr = enrich_day(analytics_pool, settings, day)
            except Exception as exc:  # enrichment must never block the funnel
                log.warning("enrich_day_failed", day=str(day), error=str(exc)[:160])
        try:  # auto-assign interested-prospect callbacks to the BDE calendar
            from .calendar import sync_callbacks_for_day
            sync_callbacks_for_day(analytics_pool, day)
        except Exception as exc:
            log.warning("callbacks_failed", day=str(day), error=str(exc)[:160])
        try:  # RPC "Next Move": per-day upsert only; global resolve+schedule runs once after loop
            from .rpc import analyze_rpc_actions
            analyze_rpc_actions(analytics_pool, settings, day, run_global=False)
        except Exception as exc:
            log.warning("rpc_actions_failed", error=str(exc)[:160])
        aggregate_day(analytics_pool, settings, day)
        set_state(analytics_pool, day)

        totals["days"] += 1
        totals["calls"] += ing["calls"]
        totals["transcribed"] += ing["transcribed"]
        totals["stt_transcribed"] += stt["transcribed"]
        totals["classified"] += cls["classified"]
        totals["needs_review"] += cls["needs_review"]
        totals["enriched"] += enr.get("enriched", 0)
        log.info("window_day_done", day=str(day), calls=ing["calls"],
                 transcribed=ing["transcribed"], stt=stt["transcribed"],
                 classified=cls["classified"], needs_review=cls["needs_review"])

    try:  # RPC global passes ONCE for the whole window (resolve connected/DND + schedule retries)
        from .rpc import finalize_rpc_actions
        finalize_rpc_actions(analytics_pool, settings)
    except Exception as exc:
        log.warning("rpc_finalize_failed", error=str(exc)[:160])

    log.info("window_done", **totals)
    return totals
