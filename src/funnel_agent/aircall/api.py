"""Thin client for the Aircall REST API (https://api.aircall.io/v1).

Basic auth (API ID : API token). Only the few endpoints we need: list users, list
calls in a time window (paginated), fetch one call, and download its recording. The
recording field on a call is a short-lived signed S3 URL, so download_recording always
re-fetches the call to get a fresh URL, then GETs it WITHOUT our auth header (S3 signed
URLs reject an extra Authorization header). Never used for writes.
"""

from __future__ import annotations

import base64
import time
from datetime import datetime
from typing import Iterator
from zoneinfo import ZoneInfo

import httpx

from ..config import Settings
from ..logging import get_logger

log = get_logger(__name__)


class AircallClient:
    def __init__(self, settings: Settings):
        self.base = settings.aircall_base.rstrip("/")
        self.page_size = max(1, min(int(settings.aircall_page_size or 50), 50))
        token = base64.b64encode(
            f"{settings.aircall_app_id}:{settings.aircall_api_key}".encode()
        ).decode()
        self._headers = {"Authorization": f"Basic {token}", "Accept": "application/json"}
        self._client = httpx.Client(timeout=30.0, headers=self._headers, follow_redirects=True)

    def close(self) -> None:
        try:
            self._client.close()
        except Exception:
            pass

    def _get(self, path: str, params: dict | None = None) -> dict:
        url = path if path.startswith("http") else f"{self.base}{path}"
        last: Exception | None = None
        for attempt in range(4):
            try:
                r = self._client.get(url, params=params)
            except Exception as exc:  # transient network — brief backoff then retry
                last = exc
                time.sleep(1.5 * (attempt + 1))
                continue
            if r.status_code == 429:  # Aircall rate limit (60 req/min) — back off
                time.sleep(2.0 * (attempt + 1))
                continue
            r.raise_for_status()
            return r.json()
        if last:
            raise last
        raise RuntimeError(f"aircall GET failed: {url}")

    def list_users(self) -> list[dict]:
        out: list[dict] = []
        page = 1
        while True:
            d = self._get("/users", {"per_page": self.page_size, "page": page})
            out.extend(d.get("users") or [])
            if not (d.get("meta") or {}).get("next_page_link"):
                break
            page += 1
        return out

    def iter_calls(self, frm: int, to: int) -> Iterator[dict]:
        """Yield every call with started_at in [frm, to] (Unix seconds), oldest first."""
        page = 1
        while True:
            d = self._get("/calls", {"from": frm, "to": to, "per_page": self.page_size,
                                     "page": page, "order": "asc"})
            calls = d.get("calls") or []
            for c in calls:
                yield c
            if not (d.get("meta") or {}).get("next_page_link"):
                break
            page += 1

    def get_call(self, call_id: int | str) -> dict:
        return (self._get(f"/calls/{call_id}") or {}).get("call") or {}

    def download_recording(self, call_id: int | str) -> bytes:
        """Fetch a fresh signed recording URL for the call and download the audio bytes."""
        call = self.get_call(call_id)
        url = call.get("recording")
        if not url:
            return b""
        # Signed S3 URL: must NOT carry our Basic auth header, so use a bare client.
        r = httpx.get(url, timeout=60.0, follow_redirects=True)
        r.raise_for_status()
        return r.content

    def earliest_call_date(self, tz: str = "UTC"):
        """Earliest call date in Aircall — used to bound a backfill window."""
        d = self._get("/calls", {"per_page": 1, "page": 1, "order": "asc"})
        calls = d.get("calls") or []
        ts = calls[0].get("started_at") if calls else None
        if not ts:
            return None
        return datetime.fromtimestamp(int(ts), ZoneInfo(tz)).date()
