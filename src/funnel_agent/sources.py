"""Pluggable ingestion source: the 3CX API (default) or the 3CX database.

Both expose the same tiny interface so the rest of the pipeline doesn't care
where calls + transcripts come from.
"""

from __future__ import annotations

from datetime import date

from psycopg_pool import ConnectionPool

from .config import Settings
from .ingest import ingest_day, ingest_day_api
from .logging import get_logger

log = get_logger(__name__)


class Source:
    def ingest_day(self, analytics_pool: ConnectionPool, settings: Settings, day: date) -> dict:
        raise NotImplementedError

    def min_call_date(self, settings: Settings) -> date | None:
        raise NotImplementedError

    def transcribe_missing(self, analytics_pool, settings: Settings, day: date) -> dict:
        """Fill transcripts 3CX didn't produce (API source only). No-op by default."""
        return {"transcribed": 0, "skipped": 0, "errors": 0}

    def close(self) -> None:
        pass


class ApiSource(Source):
    """Reads CDR + transcripts from the 3CX Configuration API (no DB access)."""

    def __init__(self, settings: Settings):
        from .threecx.api import ThreeCXClient

        self.client = ThreeCXClient(settings)

    def ingest_day(self, analytics_pool, settings, day):
        return ingest_day_api(self.client, analytics_pool, settings, day)

    def min_call_date(self, settings):
        from .threecx.recordings import earliest_recording_date

        return earliest_recording_date(self.client, settings.tz)

    def transcribe_missing(self, analytics_pool, settings, day):
        if not settings.transcribe_missing:
            return {"transcribed": 0, "skipped": 0, "errors": 0}
        from .transcribe import transcribe_missing_for_day

        return transcribe_missing_for_day(self.client, analytics_pool, settings, day)

    def close(self):
        self.client.close()


class AircallSource(Source):
    """Reads calls + recordings for the in-scope Aircall agents (Alfred/Ben et al.)."""

    def __init__(self, settings: Settings):
        from .aircall.api import AircallClient

        self.client = AircallClient(settings)

    def ingest_day(self, analytics_pool, settings, day):
        from .aircall.ingest import ingest_day_aircall

        return ingest_day_aircall(self.client, analytics_pool, settings, day)

    def min_call_date(self, settings):
        return self.client.earliest_call_date(settings.tz)

    def transcribe_missing(self, analytics_pool, settings, day):
        if not settings.transcribe_missing:
            return {"transcribed": 0, "skipped": 0, "errors": 0}
        from .aircall.transcribe import transcribe_missing_aircall

        return transcribe_missing_aircall(self.client, analytics_pool, settings, day)

    def close(self):
        self.client.close()


class CompositeSource(Source):
    """Fans every pipeline step out across several sources (e.g. 3CX + Aircall) and
    sums their counts, so calls from both telephony systems land in the same tables."""

    def __init__(self, sources: list[Source]):
        self.sources = sources

    def _merge(self, method: str, *args) -> dict:
        totals: dict = {}
        for src in self.sources:
            # Isolate sources: an Aircall outage must NOT stop 3CX (the primary system)
            # from ingesting/aggregating, or the whole dashboard would freeze. A failed
            # day self-heals on the next refresh (idempotent + daily lookback window).
            try:
                res = getattr(src, method)(*args) or {}
            except Exception as exc:
                log.warning("source_step_failed", source=type(src).__name__,
                            method=method, error=str(exc)[:180])
                continue
            for k, v in res.items():
                if isinstance(v, (int, float)):
                    totals[k] = totals.get(k, 0) + v
        return totals

    def ingest_day(self, analytics_pool, settings, day):
        return self._merge("ingest_day", analytics_pool, settings, day)

    def transcribe_missing(self, analytics_pool, settings, day):
        return self._merge("transcribe_missing", analytics_pool, settings, day)

    def min_call_date(self, settings):
        dates = [d for d in (s.min_call_date(settings) for s in self.sources) if d is not None]
        return min(dates) if dates else None

    def close(self):
        for src in self.sources:
            try:
                src.close()
            except Exception:
                pass


class DbSource(Source):
    """Reads CDR + transcripts directly from the 3CX PostgreSQL (original design)."""

    def __init__(self, settings: Settings):
        from .db.source import make_source_pool

        self.pool = make_source_pool(settings.source_db_dsn)
        self.pool.open()

    def ingest_day(self, analytics_pool, settings, day):
        return ingest_day(self.pool, analytics_pool, settings, day)

    def min_call_date(self, settings):
        from .threecx.cdr import min_call_date

        return min_call_date(self.pool, settings.cdr)

    def close(self):
        self.pool.close()


def make_source(settings: Settings) -> Source:
    mode = settings.source_mode.lower().strip()
    log.info("source_mode", mode=mode, aircall=settings.aircall_enabled)
    base: Source = ApiSource(settings) if mode == "api" else DbSource(settings)
    # When Aircall is configured, run BOTH systems through the same pipeline.
    if settings.aircall_enabled:
        return CompositeSource([base, AircallSource(settings)])
    return base
