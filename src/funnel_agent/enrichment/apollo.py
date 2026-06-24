"""Apollo.io organization enrichment — FREE company data ONLY.

============================ HARD NO-CREDITS GUARDRAIL ============================
Apollo charges credits for *people* data (emails/phones) and for `reveal_*` flags.
Company (organization) enrichment is free. This client is built so it is STRUCTURALLY
IMPOSSIBLE to spend credits:

  * The ONLY endpoint it can reach is `/api/v1/organizations/enrich` (an allowlist;
    any other path raises).
  * It exposes ONLY `enrich_organization(domain)` — there is no people/match/search
    method to call by mistake.
  * It NEVER sends `reveal_personal_emails` / `reveal_phone_number` (the params that
    burn credits).

Decision-maker name/role come from the CALL itself (who_answered / RPC /
prospect_summary), never from a credit-consuming Apollo people lookup. The pipeline
adds further runtime guards (kill-switch + per-day cap).
==================================================================================
"""

from __future__ import annotations

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

from ..config import Settings
from ..logging import get_logger

log = get_logger(__name__)

_BASE = "https://api.apollo.io"
_ALLOWED_PATH = "/api/v1/organizations/enrich"  # the ONLY endpoint this client may hit


class ApolloClient:
    def __init__(self, settings: Settings):
        self._key = settings.apollo_api_key
        self._client = httpx.Client(timeout=30.0, base_url=_BASE)

    def __enter__(self) -> "ApolloClient":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    def close(self) -> None:
        try:
            self._client.close()
        except Exception:
            pass

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=8), reraise=True)
    def _get_org(self, domain: str) -> dict:
        # GUARDRAIL: hard-coded free endpoint, only the `domain` param, NO reveal_* flags.
        assert _ALLOWED_PATH == "/api/v1/organizations/enrich", "endpoint allowlist tampered"
        resp = self._client.get(
            _ALLOWED_PATH,
            params={"domain": domain},  # never any reveal_personal_emails / reveal_phone_number
            headers={
                "X-Api-Key": self._key,
                "Content-Type": "application/json",
                "Cache-Control": "no-cache",
            },
        )
        resp.raise_for_status()
        return resp.json()

    def enrich_organization(self, domain: str) -> dict:
        """Free company firmographics for a domain. No credits consumed."""
        org = (self._get_org(domain) or {}).get("organization") or {}
        if not org:
            return {"found": False}
        phone = org.get("phone")
        if not phone and isinstance(org.get("primary_phone"), dict):
            phone = org["primary_phone"].get("number")
        return {
            "found": True,
            "name": org.get("name"),
            "domain": org.get("primary_domain") or org.get("website_url"),
            "industry": org.get("industry"),
            "employees": org.get("estimated_num_employees"),
            "annual_revenue": org.get("annual_revenue") or org.get("organization_revenue"),
            "annual_revenue_printed": (org.get("annual_revenue_printed")
                                       or org.get("organization_revenue_printed")),
            "founded_year": org.get("founded_year"),
            "city": org.get("city"),
            "state": org.get("state"),
            "country": org.get("country"),
            "phone": phone,
            "linkedin_url": org.get("linkedin_url"),
            "facebook_url": org.get("facebook_url"),
            "twitter_url": org.get("twitter_url"),
            "website_url": org.get("website_url"),
            "description": org.get("short_description"),
            "keywords": (org.get("keywords") or [])[:12],
            "technologies": (org.get("technology_names") or [])[:20],
        }
