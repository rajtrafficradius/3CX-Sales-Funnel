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


def _dm_rank(title: str | None, seniority: str | None) -> int:
    """Decision-maker priority for a MARKETING/ADVERTISING pitch (lower = higher). The person who decides on
    Google Ads spend is the OWNER/Managing Director/CEO, or the MARKETING head (CMO / Marketing
    Director/Manager) — NOT a Sales/IT/Ops/Finance lead. Sales & other functional roles are deliberately
    demoted so we never reveal/target the wrong person. rank<=3 = a real target; rank 6 = do not reveal."""
    t = (title or "").lower(); s = (seniority or "").lower()
    # functional roles that do NOT own the marketing/advertising budget
    _NONMKT = ("sales", "account manager", "account executive", "account director", "business development",
               "it ", "information tech", "software", "engineer", "developer", "product", "operations",
               "finance", "accountant", "accounts", "hr", "human resources", "legal", "procurement",
               "logistics", "warehouse", "customer service", "customer support", "support", "administrat")
    _MKT = ("marketing", "cmo", "brand", "digital", "growth", "ecommerce", "e-commerce", "advertising",
            "demand gen", "comms", "communications")
    is_mkt = any(k in t for k in _MKT)
    # 0 — the OWNER / boss: controls all budget, including marketing
    if s in ("owner", "founder") or any(k in t for k in ("founder", "owner", "co-owner", "proprietor",
                                                          "principal", "managing director")):
        return 0
    # 1 — CEO / managing partner, OR a MARKETING leader (owns the advertising budget)
    if any(k in t for k in ("chief executive", "ceo", "managing partner")):
        return 1
    if is_mkt and any(h in t for h in ("chief", "cmo", "head", "director", "manager", "lead", "vp",
                                       "gm", "general manager", "officer")):
        return 1
    # 2 — a general Director / General Manager (usually the boss at an SMB), but NOT a sales/functional role
    if any(h in t for h in ("director", "general manager")) and not any(k in t for k in _NONMKT):
        return 2
    # 6 — sales-only, IT, ops, finance, HR, product, account management, etc. — do NOT reveal/target these
    return 6


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

    def search_decision_makers(self, domain: str, org_id: str | None = None, limit: int = 10,
                               broad: bool = False) -> list[dict]:
        """Decision-maker NAMES + TITLES for a company — FREE (no email/phone reveal).

        Returns name/title/seniority/department/linkedin only. Email & phone are
        intentionally NOT requested or read, so no Apollo credits are consumed. broad=True drops the
        seniority filter — a fallback for micro-companies whose sole owner isn't tagged senior."""
        # Fetch a WIDER candidate set (25) so the founder/director isn't missed when a company
        # has many managers, then rank by decision-maker priority and return the top `limit`.
        body = {"page": 1, "per_page": 25}
        if not broad:
            body["person_seniorities"] = _SENIORITIES
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
        for p in (data.get("people") or []):
            org = p.get("organization") or {}
            first = p.get("first_name") or ""
            # api_search returns the last name OBFUSCATED for free (full name needs credits)
            last = p.get("last_name") or p.get("last_name_obfuscated") or ""
            name = (first + " " + last).strip()
            title = p.get("title")
            out.append({
                "id": p.get("id"),                             # Apollo person id → precise paid reveal
                "name": name or None,
                "title": title,                                # the designation (free)
                "seniority": p.get("seniority"),
                "rank": _dm_rank(title, p.get("seniority")),   # 0=Founder/Owner … 4=other
                "departments": p.get("departments") or [],
                "linkedin_url": p.get("linkedin_url"),
                "company": org.get("name"),
                # flags only — tells us contact data EXISTS; revealing it would cost credits
                "has_email": bool(p.get("has_email")),
                "has_phone": str(p.get("has_direct_phone", "")).lower() in ("yes", "true"),
                # email/phone VALUES deliberately omitted — no reveal_* = no credits.
            })
        out = [p for p in out if p.get("name") or p.get("title")]
        # Founder/Owner → Directors → CEO/CFO → Marketing & Sales heads → others.
        out.sort(key=lambda p: p.get("rank", 4))
        return out[:limit]

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

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=8), reraise=True)
    def reveal_contact(self, first_name: str, last_name: str, *, apollo_id: str | None = None,
                       organization_name: str | None = None, domain: str | None = None,
                       webhook_url: str | None = None) -> dict:
        """PAID: reveal ONE decision-maker's email + phone. Email comes back SYNC; the mobile/direct phone
        is delivered ASYNC to webhook_url (Apollo verifies numbers in real time). Costs Apollo credits —
        callers MUST gate this behind settings.apollo_paid_reveal. Prefer matching by apollo_id (precise;
        the free search obfuscates last names, so name-matching misses). Returns {email, phone, apollo_id}."""
        body: dict = {"reveal_personal_emails": True}
        if apollo_id:                                   # precise match by Apollo person id
            body["id"] = apollo_id
        else:
            body["first_name"] = first_name or ""
            body["last_name"] = last_name or ""
        if organization_name:
            body["organization_name"] = organization_name
        if domain:
            body["domain"] = domain
        if webhook_url:                      # phone reveal is async — only requested when a callback exists
            body["reveal_phone_number"] = True
            body["webhook_url"] = webhook_url
        resp = self._client.post(
            "/api/v1/people/match", json=body,
            headers={"X-Api-Key": self._key, "Content-Type": "application/json", "Cache-Control": "no-cache"})
        resp.raise_for_status()
        person = (resp.json() or {}).get("person") or {}
        email = person.get("email") or ""
        if not email:
            for e in (person.get("personal_emails") or []):
                if e:
                    email = e; break
        # a direct/mobile phone occasionally comes back sync; usually it arrives later via the webhook
        phone = ""
        for pn in (person.get("phone_numbers") or []):
            num = (pn or {}).get("sanitized_number") or (pn or {}).get("raw_number")
            if num:
                phone = num
                if (pn or {}).get("type") in ("mobile", "work_mobile"):
                    break
        return {"email": email, "phone": phone, "apollo_id": person.get("id")}
