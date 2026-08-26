"""G11 / G12 — the single outbound-send sanitizer (SMS + email).

Runs at the SEND chokepoint (off the dial loop). Two jobs:

  G11  scrub BANNED PHRASES from the message body. The operator's firm, repeated rule: Lisa must
       NEVER mention or offer a "video link" — only the meeting invite. The scrub is PURE CPU (regex)
       and always runs, even when no pool is available, so it catches code-composed bodies AND
       LLM-composed inbound-SMS replies regardless of the prompt.
  G12  LINK HYGIENE — force every raw URL through the branded shortlink (``shortlink.short_url``) so
       what goes out is a tidy ``/s/xxxxxx`` link, never a long raw URL. Needs the pool; best-effort
       and fully guarded (a hygiene failure never blocks the send). Already-short ``/s/`` links are
       left alone.

The phrase scrub is the safety guarantee; the URL hygiene is a polish. Everything here returns the
input body unchanged on error and never raises into the send path.
"""

from __future__ import annotations

import re

from ..logging import get_logger

log = get_logger(__name__)

# Banned-phrase substitutions. "video link" (and close variants) -> the allowed term "meeting invite".
_PHRASE_SUBS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\bvideo[\s\-]*call[\s\-]*links?\b", re.IGNORECASE), "meeting invite"),
    (re.compile(r"\bvideo[\s\-]*links?\b", re.IGNORECASE), "meeting invite"),
    (re.compile(r"\bzoom[\s\-]*links?\b", re.IGNORECASE), "meeting invite"),
    (re.compile(r"\bmeet(?:ing)?[\s\-]*video[\s\-]*links?\b", re.IGNORECASE), "meeting invite"),
]

_URL_RX = re.compile(r"https?://[^\s<>()]+", re.IGNORECASE)
# trailing punctuation that shouldn't be swallowed into the URL
_URL_TRAIL = ".,;:!?)\"'"


def scrub_phrases(body: str) -> tuple[str, list[str]]:
    """Pure banned-phrase scrub. Returns (clean_body, list_of_patterns_hit). Never raises."""
    try:
        out = body or ""
        hits: list[str] = []
        for rx, repl in _PHRASE_SUBS:
            if rx.search(out):
                out = rx.sub(repl, out)
                hits.append(rx.pattern)  # record the pattern that fired
        if hits:
            out = re.sub(r"[ \t]{2,}", " ", out).strip()
        return out, hits
    except Exception:
        return body, []


def _shorten_urls(pool, body: str) -> tuple[str, list[str]]:
    """Replace every raw URL with its branded short form. Already-short ``/s/`` links are skipped.
    Best-effort per URL; a failure leaves that URL untouched. Never raises."""
    changed: list[str] = []

    def _repl(m: re.Match) -> str:
        raw = m.group(0)
        trail = ""
        while raw and raw[-1] in _URL_TRAIL:
            trail = raw[-1] + trail
            raw = raw[:-1]
        if not raw or "/s/" in raw:        # already a shortlink — leave it
            return m.group(0)
        try:
            from .. import shortlink as _sl
            short = _sl.short_url(pool, raw)
            if short and short != raw:
                changed.append(raw)
                return short + trail
        except Exception:
            pass
        return m.group(0)

    try:
        return _URL_RX.sub(_repl, body), changed
    except Exception:
        return body, changed


def sanitize_outbound(body: str, pool=None, *, call_id: str | None = None,
                      agent: str | None = None) -> str:
    """The single send-chokepoint sanitizer. Always scrubs banned phrases; when ``pool`` is provided
    also applies link hygiene and records what changed to ``qa_audit``. Returns the cleaned body.
    Never raises — on any error the original body is returned so the send still goes out."""
    try:
        if not body:
            return body
        cleaned, phrase_hits = scrub_phrases(body)
        url_hits: list[str] = []
        if pool is not None:
            cleaned, url_hits = _shorten_urls(pool, cleaned)
            if phrase_hits or url_hits:
                try:
                    from . import audit as _audit
                    _audit.log_event(pool, gate="G11/G12", kind="outbound_sanitized",
                                     call_id=call_id, agent=agent,
                                     detail={"phrases_scrubbed": phrase_hits,
                                             "urls_shortened": url_hits})
                except Exception:
                    pass
        return cleaned
    except Exception as exc:
        log.warning("qa_sanitize_outbound_failed", error=str(exc)[:120])
        return body
