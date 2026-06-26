"""Apollo.io enrichment — FREE fields ONLY (no Apollo credits spent).

============================ NO-CREDITS GUARDRAIL ============================
Apollo burns credits ONLY when you REVEAL emails/phones (`reveal_personal_emails`
/ `reveal_phone_number`). Reading company firmographics and the people *list*
(names, titles, seniority, department, LinkedIn) does NOT consume those credits.

This client is allow-listed to two endpoints and NEVER sends a reveal_* flag:
  * `/api/v1/organizations/enrich`  — free company firmographics.
  * `/api/v1/mixed_people/search`   — decision-maker NAMES + TITLES only; we read
    name/title/seniority/department/linkedin and DELIBERATELY ignore email/phone
    (they come back masked anyway without a reveal flag).

So no email/mobile credits are ever spent. (Verify once on your plan with a small
batch before a full run, since record-view quotas can vary by Apollo plan.)
==================================================================================
"""

from __future__ import annotations

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

from ..config import Settings
from ..logging import get_logger

log = get_logger(__name__)

_BASE = "https://api.apollo.io"
_ALLOWED_PATH = "/api/v1/organizations/enrich"   # free company firmographics
_PEOPLE_PATH = "/api/v1/mixed_people/api_search"  # decision-maker names/titles (no reveal_* = no credits)
# Senior, decision-making roles we want for a B2B prospect.
_SENIORITIES = ["owner", "founder", "c_suite", "partner", "vp", "head", "director", "manager"]


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

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=8), reraise=True)
    def _search_people(self, body: dict) -> dict:
        # GUARDRAIL: people SEARCH only; NEVER reveal_personal_emails/reveal_phone_number.
        assert "reveal_personal_emails" not in body and "reveal_phone_number" not in body
        resp = self._client.post(
            _PEOPLE_PATH, json=body,
            headers={"X-Api-Key": self._key, "Content-Type": "application/json", "Cache-Control": "no-cache"},
        )
        resp.raise_for_status()
        return resp.json()

    def search_decision_makers(self, domain: str, org_id: str | None = None, limit: int = 10) -> list[dict]:
        """Decision-maker NAMES + TITLES for a company — FREE (no email/phone reveal).

        Returns name/title/seniority/department/linkedin only. Email & phone are
        intentionally NOT requested or read, so no Apollo credits are consumed."""
        body = {"page": 1, "per_page": max(1, min(limit, 25)), "person_seniorities": _SENIORITIES}
        if org_id:
            body["organization_ids"] = [org_id]
        else:
            body["q_organization_domains_list"] = [domain]
        try:
            data = self._search_people(body)
        except Exception as exc:
            log.warning("apollo_people_failed", domain=domain, error=str(exc)[:160])
            return []
        out = []
        for p in (data.get("people") or [])[:limit]:
            org = p.get("organization") or {}
            first = p.get("first_name") or ""
            # api_search returns the last name OBFUSCATED for free (full name needs credits)
            last = p.get("last_name") or p.get("last_name_obfuscated") or ""
            name = (first + " " + last).strip()
            out.append({
                "name": name or None,
                "title": p.get("title"),                       # the designation (free)
                "seniority": p.get("seniority"),
                "departments": p.get("departments") or [],
                "linkedin_url": p.get("linkedin_url"),
                "company": org.get("name"),
                # flags only — tells us contact data EXISTS; revealing it would cost credits
                "has_email": bool(p.get("has_email")),
                "has_phone": str(p.get("has_direct_phone", "")).lower() in ("yes", "true"),
                # email/phone VALUES deliberately omitted — no reveal_* = no credits.
            })
        return [p for p in out if p.get("name") or p.get("title")]

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
            "id": org.get("id"),
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
