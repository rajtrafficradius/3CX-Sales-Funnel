"""G8 / G9 / G10 — brief dynamic-variable safety (PURE CPU, in-memory only).

These run while building the brief / just before the dynamic-variables dict is handed to Retell. They
are the ONLY QA code that sits near the dial path, so they are strictly pure — dict / string / regex
work with ZERO I/O — and each function returns its input unchanged on any error.

  G8  scrub_residual_placeholders — remove any raw ``{{ ... }}`` left inside a VALUE so Retell can
      never voice a literal placeholder. (Used together with lisa._fill_dynamic_var_defaults, which
      fills missing/blank KEYS; this scrubs residue inside the values themselves.)
  G9  enforce_no_website_invariant — a prospect with NO website must never carry a website URL /
      domain into the brief (so Lisa can never claim "we looked at yoursite.com" for a site-less
      prospect). Only enforced on a POSITIVE no-site signal (website_bucket == 'no_website').
  G10 clean_prospect_name — reduce a raw contact to a clean spoken FIRST name, or "" — never a
      placeholder ('the'/'owner'/'unknown'), a legal entity ('… PTY LTD'), an email, or digits.
"""

from __future__ import annotations

import re

# ------------------------------------------------------------------ G8: residual {{ }} scrub -------
_PLACEHOLDER_RX = re.compile(r"\{\{[^{}]*\}\}")


def scrub_residual_placeholders(dvars: dict | None) -> dict:
    """Strip any residual ``{{placeholder}}`` from every string VALUE (belt-and-suspenders on top of
    the defaults fill). Collapses the whitespace left behind. Never raises."""
    try:
        out: dict = {}
        for k, v in (dvars or {}).items():
            if isinstance(v, str) and "{{" in v:
                v = _PLACEHOLDER_RX.sub("", v)
                v = re.sub(r"\s{2,}", " ", v).strip()
            out[k] = v
        return out
    except Exception:
        return dvars or {}


# --------------------------------------------------------------- G9: no-website invariant ----------
def enforce_no_website_invariant(dvars: dict | None) -> dict:
    """When the brief carries a POSITIVE no-website signal (Lisa-4/5 ``website_bucket == 'no_website'``),
    force the website URL + bare domain fields blank so no downstream fill can re-introduce a
    "your website" placeholder or a stale URL for a site-less prospect. Present-but-empty variables
    render as empty (never as a raw ``{{...}}``). Lisa-1 (which has no website_bucket) is untouched —
    its generic "your website" fallback is intentional. Never raises."""
    try:
        out = dict(dvars or {})
        bucket = str(out.get("website_bucket", "") or "").strip().lower()
        if bucket == "no_website":
            out["prospect_website"] = ""
            out["company_domain"] = ""
            # a site-less prospect cannot be "running Google Ads to their site"
            if "runs_google_ads" in out:
                out["runs_google_ads"] = "false"
        return out
    except Exception:
        return dvars or {}


# -------------------------------------------------------------- G10: clean prospect name -----------
# tokens that are NOT a person's name even though they can arrive in a "name" slot
_NAME_STOP = frozenset({
    "the", "owner", "unknown", "there", "sir", "madam", "maam", "mister", "mr", "mrs", "ms",
    "team", "manager", "director", "customer", "client", "guest", "friend", "mate", "boss",
    "admin", "reception", "receptionist", "staff", "hello", "hi", "hey", "yes", "no", "na",
    "none", "null", "test", "business", "company", "info", "enquiries", "enquiry", "sales",
    "accounts", "principal", "proprietor", "contact", "person", "someone", "anybody", "n/a",
})
# legal / entity noise that means this string is a company, not a person
_LEGAL_RX = re.compile(
    r"\b(pty|ltd|limited|inc|incorporated|llc|trust|trustee|group|holdings|enterprises|"
    r"services|solutions|consulting|the\s+trustee)\b",
    re.IGNORECASE,
)
_NAME_TOKEN_RX = re.compile(r"[A-Za-z][A-Za-z'’\-]*")


def clean_prospect_name(raw: str | None) -> str:
    """Return a clean spoken FIRST name, or "" when the input isn't a usable person name. Pure."""
    try:
        s = (raw or "").strip()
        if not s:
            return ""
        if "@" in s or any(ch.isdigit() for ch in s):   # emails / anything with digits are not names
            return ""
        if _LEGAL_RX.search(s):                          # legal entity, not a person
            return ""
        first = re.split(r"[\s,/]+", s)[0].strip(".'’\"-")
        if len(first) < 2 or first.lower() in _NAME_STOP:
            return ""
        if not _NAME_TOKEN_RX.fullmatch(first):          # must be alphabetic (allow ' and -)
            return ""
        # normalise SHOUTING-CAPS or all-lower to Title case; preserve genuine mixed case (McDonald, O'Brien)
        if first.isupper() or first.islower():
            first = first[:1].upper() + first[1:].lower()
        return first
    except Exception:
        return raw or ""
