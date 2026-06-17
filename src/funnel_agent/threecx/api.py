"""3CX Configuration API client.

Handles Service-Principal auth (`POST /connect/token`, client_credentials -> a
~60-minute Bearer token) and the provisioning reads we use:
  - `GET /xapi/v1/Defs`  -> server version (via the `X-3CX-Version` header)
  - `GET /xapi/v1/Users` -> the BDE roster (paged via OData $top/$skip)

This is a *provisioning* API: it has no call-history or transcript endpoints
(those come from the database). We only use it for the roster + a version ping.
"""

from __future__ import annotations

import time
from collections.abc import Iterator

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

from ..config import Settings
from ..logging import get_logger

log = get_logger(__name__)

_USER_SELECT = "Id,FirstName,LastName,Number,EmailAddress"
_PAGE_SIZE = 100


class ThreeCXAPIError(RuntimeError):
    pass


class ThreeCXClient:
    """Thin client over the 3CX Configuration API."""

    def __init__(self, settings: Settings):
        self._s = settings
        self._token: str | None = None
        self._token_expiry: float = 0.0
        self._client = httpx.Client(
            base_url=settings.threecx_api_base.rstrip("/"),
            verify=settings.threecx_verify_tls,
            timeout=httpx.Timeout(30.0),
        )

    # -- lifecycle -----------------------------------------------------------
    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> ThreeCXClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- auth ----------------------------------------------------------------
    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=10), reraise=True)
    def _fetch_token(self) -> None:
        if not self._s.threecx_client_id or not self._s.threecx_client_secret:
            raise ThreeCXAPIError(
                "THREECX_CLIENT_ID / THREECX_CLIENT_SECRET are not set "
                "(register a Service Principal in 3CX)."
            )
        resp = self._client.post(
            "/connect/token",
            data={
                "grant_type": "client_credentials",
                "client_id": self._s.threecx_client_id,
                "client_secret": self._s.threecx_client_secret,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        if resp.status_code != 200:
            raise ThreeCXAPIError(
                f"token request failed: {resp.status_code} {resp.text[:300]}"
            )
        payload = resp.json()
        self._token = payload["access_token"]
        # Refresh slightly early. Buffer is proportional so short-lived tokens
        # (3CX issues ~60s) don't force a re-auth on every single request.
        expires_in = int(payload.get("expires_in", 3600))
        self._token_expiry = time.monotonic() + expires_in - min(30, expires_in * 0.2)
        log.info("threecx_token_acquired", expires_in=expires_in)

    def _auth_header(self) -> dict[str, str]:
        if self._token is None or time.monotonic() >= self._token_expiry:
            self._fetch_token()
        return {"Authorization": f"Bearer {self._token}"}

    # -- requests ------------------------------------------------------------
    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=10), reraise=True)
    def _get(self, path: str, params: dict | None = None) -> httpx.Response:
        resp = self._client.get(path, params=params, headers=self._auth_header())
        if resp.status_code == 401:
            # token may have been revoked; force one refresh + retry
            self._token = None
            resp = self._client.get(path, params=params, headers=self._auth_header())
        resp.raise_for_status()
        return resp

    # -- public API ----------------------------------------------------------
    def get_version(self) -> str:
        """Return the 3CX server version from the `X-3CX-Version` response header."""
        resp = self._get("/xapi/v1/Defs")
        return resp.headers.get("X-3CX-Version", "unknown")

    def get_value(self, path: str, params: dict | None = None) -> list[dict]:
        """GET an OData collection (or report function) and return its `value` list."""
        return self._get(path, params=params).json().get("value", [])

    def iter_query(self, path: str, params: dict, page_size: int = 100) -> Iterator[dict]:
        """Page through any OData collection (`$top`/`$skip`), robust to server caps.

        Advances by the actual returned count and keeps going while the server
        signals more (`@odata.nextLink`) or returns a full page.
        """
        skip = 0
        while True:
            body = self._get(path, params={**params, "$top": page_size, "$skip": skip}).json()
            page = body.get("value", [])
            if not page:
                break
            yield from page
            if body.get("@odata.nextLink") or len(page) == page_size:
                skip += len(page)
                continue
            break

    def iter_users(self) -> Iterator[dict]:
        """Yield every user from `/xapi/v1/Users`, paging through with $top/$skip."""
        skip = 0
        while True:
            resp = self._get(
                "/xapi/v1/Users",
                params={
                    "$select": _USER_SELECT,
                    "$expand": "Groups",
                    "$top": _PAGE_SIZE,
                    "$skip": skip,
                },
            )
            page = resp.json().get("value", [])
            if not page:
                break
            yield from page
            if len(page) < _PAGE_SIZE:
                break
            skip += _PAGE_SIZE
