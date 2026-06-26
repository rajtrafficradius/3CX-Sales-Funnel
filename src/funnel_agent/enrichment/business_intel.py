"""FREE website business-intelligence extraction.

Fetch a prospect's homepage (+ an About/Services page if linked) and use the LLM to
pull structured business info: what they sell, their USPs, who they target, and their
ideal-customer profile. No paid API — just httpx + the OpenAI model we already use.
Robust: any fetch/parse error returns {found: False} rather than raising.
"""

from __future__ import annotations

import json
import re

import httpx

from ..logging import get_logger

log = get_logger(__name__)

_UA = {"User-Agent": "Mozilla/5.0 (compatible; TrafficRadiusBot/1.0; +https://trafficradius.com.au)"}
_TAG_RE = re.compile(r"(?is)<(script|style|noscript|svg)[^>]*>.*?</\1>")
_HTML_RE = re.compile(r"(?s)<[^>]+>")
_WS_RE = re.compile(r"\s+")
_ABOUT_RE = re.compile(r'href=["\']([^"\']*(?:about|services|what-we-do|solutions|products)[^"\']*)["\']', re.I)

_SCHEMA = {
    "type": "object",
    "properties": {
        "business_summary": {"type": "string", "description": "2-3 sentences on what the business does"},
        "products": {"type": "array", "items": {"type": "string"}},
        "services": {"type": "array", "items": {"type": "string"}},
        "usps": {"type": "array", "items": {"type": "string"}, "description": "unique selling points / differentiators"},
        "target_audience": {"type": "string", "description": "who they sell to"},
        "icp": {"type": "string", "description": "ideal customer profile in one line"},
        "industry": {"type": "string"},
    },
    "required": ["business_summary", "products", "services", "usps", "target_audience", "icp", "industry"],
    "additionalProperties": False,
}


def _visible_text(html: str, cap: int = 9000) -> str:
    html = _TAG_RE.sub(" ", html or "")
    text = _WS_RE.sub(" ", _HTML_RE.sub(" ", html)).strip()
    return text[:cap]


def _fetch(client: httpx.Client, url: str) -> str:
    try:
        r = client.get(url)
        return r.text or ""
    except Exception:
        return ""


def extract_business_intel(oai, settings, domain: str) -> dict:
    """Scrape homepage (+ one about/services page) and LLM-extract business info. Free."""
    if not domain:
        return {"found": False}
    homepage = ""
    with httpx.Client(timeout=12.0, follow_redirects=True, headers=_UA) as client:
        for scheme in ("https", "http"):
            homepage = _fetch(client, f"{scheme}://{domain}")
            if homepage:
                break
        if not homepage:
            return {"found": False, "status": "unreachable"}
        text = _visible_text(homepage)
        # follow one internal about/services page for richer context
        m = _ABOUT_RE.search(homepage)
        if m:
            href = m.group(1)
            if href.startswith("/"):
                href = f"https://{domain}{href}"
            if href.startswith("http") and domain in href:
                text += " \n\n" + _visible_text(_fetch(client, href), cap=5000)

    if len(text) < 80:
        return {"found": False, "status": "thin_content"}
    try:
        resp = oai.chat.completions.create(
            model=(getattr(settings, "llm_model_cheap", "") or "gpt-4o-mini"),
            messages=[
                {"role": "system", "content": "You are a B2B research analyst. From the website text, extract the company's "
                 "products, services, unique selling points, target audience and ideal customer profile (ICP). "
                 "Be concise and concrete; use the company's own wording. If something isn't stated, give your best "
                 "inference from the content. Return ONLY the JSON object."},
                {"role": "user", "content": f"Website: {domain}\n\n{text}"},
            ],
            response_format={"type": "json_schema",
                             "json_schema": {"name": "business_intel", "schema": _SCHEMA, "strict": True}},
            temperature=0,
        )
        data = json.loads(resp.choices[0].message.content)
        data["found"] = True
        return data
    except Exception as exc:
        log.warning("business_intel_failed", domain=domain, error=str(exc)[:160])
        return {"found": False, "status": "llm_error"}
