"""Shared pytest fixtures."""

from __future__ import annotations

import os

import pytest

ANALYTICS_DSN = os.environ.get("ANALYTICS_DB_DSN", "")


@pytest.fixture(scope="session")
def analytics_pool():
    """A live analytics Postgres pool, or skip the whole test if none is configured."""
    if not ANALYTICS_DSN:
        pytest.skip("ANALYTICS_DB_DSN not set — DB-backed test skipped")
    from funnel_agent.db.analytics import make_analytics_pool
    from funnel_agent.db.migrate import apply_schema

    pool = make_analytics_pool(ANALYTICS_DSN)
    pool.open()
    apply_schema(pool)
    yield pool
    pool.close()
