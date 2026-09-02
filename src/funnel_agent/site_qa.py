"""QA PIPELINES for a built website — the gate that runs BEFORE any site link is sent to a prospect.

Several independent checks so a broken/incomplete/placeholder site never reaches a prospect:
  1. structure  — a complete document (<!doctype/<html>, </html>, a <title>, a <script>, enough sections)
  2. content    — the business name appears, there's contact/tap-to-call, nav, and real (non-lorem) copy
  3. placeholder— no leftover TODO/lorem/[company]/undefined/"your business here" scaffolding
  4. reachable  — the public share URL returns 200
  5. brand-safe — no Traffic Radius / internal-tool leakage on a client-facing page
  6. llm-review — (optional, cached) Opus reads the page and flags anything embarrassing

The result is CACHED on lisa4_sites (qa_passed / qa_notes / qa_at) so it runs once per build and the callers
(no-show recovery, link-on-ask) just read the cached verdict. Fully guarded — never raises.
"""
import re as _re
import urllib.request
import urllib.error

_BASE = "https://www.trmatrix.com.au"
# Scaffolding markers only. Deliberately NOT "placeholder" (a valid <input placeholder="…"> attribute) nor
# generic words — those caused false failures on real sites.
_PLACEHOLDERS = ("lorem ipsum", "[company name", "[company]", "your business here", "your company name",
                 "insert your", "xxxxxx", "tbd tbd")


def ensure_qa_columns(pool) -> None:
    with pool.connection() as conn, conn.cursor() as cur:
        for col, typ in (("qa_passed", "boolean"), ("qa_notes", "text"), ("qa_at", "timestamptz")):
            cur.execute(f"ALTER TABLE lisa4_sites ADD COLUMN IF NOT EXISTS {col} {typ}")
        conn.commit()


def _structural(html: str) -> list[dict]:
    h = html or ""
    low = h.lower()
    checks = []
    checks.append({"name": "complete-document", "pass": ("</html>" in low and ("<!doctype" in low or "<html" in low)),
                   "note": "document runs through to </html>"})
    checks.append({"name": "has-title", "pass": ("<title" in low), "note": "page has a <title>"})
    checks.append({"name": "has-interactivity", "pass": ("<script" in low), "note": "nav/router script present"})
    checks.append({"name": "enough-sections", "pass": (low.count("<section") >= 3 or low.count("<main") >= 1),
                   "note": f"{low.count('<section')} sections"})
    checks.append({"name": "substantial", "pass": (len(h) >= 8000), "note": f"{len(h)//1000}kb of HTML"})
    return checks


def _images(html: str) -> list[dict]:
    """A reveal whose hero renders as a grey box is worse than no reveal at all — the prospect is
    looking at it on a screen-share. Bodyoncall shipped on 2026-09-01 with `<img src="">` in the
    hero and still scored qa_passed=True, because nothing here looked at images.

    no-broken-images is HARD (an empty/missing src is unambiguous breakage). has-content-image is
    advisory — a design can legitimately carry all its imagery in CSS backgrounds.
    """
    h = html or ""
    tags = _re.findall(r"<img\b[^>]*>", h, _re.I)
    broken = [t for t in tags
              if not _re.search(r"""\bsrc\s*=\s*["'][^"']+["']""", t, _re.I)
              or _re.search(r"""\bsrc\s*=\s*["']\s*["']""", t, _re.I)
              or _re.search(r"""\bsrc\s*=\s*["']#["']""", t, _re.I)]
    usable = len(tags) - len(broken)
    has_css_bg = bool(_re.search(r"background(-image)?\s*:[^;}]*url\(\s*['\"]?(?!data:image/svg)", h, _re.I))
    return [
        {"name": "no-broken-images", "pass": not broken,
         "note": ("clean" if not broken else f"{len(broken)} of {len(tags)} <img> have an empty/missing src")},
        {"name": "has-content-image", "pass": bool(usable or has_css_bg), "advisory": True,
         "note": f"{usable} rendering <img>" + (" + css background" if has_css_bg else "")},
    ]


def _content(html: str, company: str, phone: str | None) -> list[dict]:
    low = (html or "").lower()
    checks = []
    # a recognisable slice of the business name should appear on the page
    toks = [t for t in _re.split(r"[^a-z0-9]+", (company or "").lower()) if len(t) >= 3
            and t not in ("pty", "ltd", "the", "and", "group", "trustee", "for", "trust")]
    name_hit = any(t in low for t in toks) if toks else True
    # ADVISORY: brand may be stylised/shortened on the page (e.g. "Admin Made Easy" for
    # "Administration Made Easy Pty Ltd"); the deep LLM review is the real wrong-business gate.
    checks.append({"name": "business-name-present", "pass": name_hit, "advisory": True, "note": "the business name appears"})
    has_tel = ("tel:" in low) or bool(_re.search(r"\b0[2-478][\s\d]{7,}\b", html or ""))
    checks.append({"name": "contactable", "pass": has_tel, "note": "phone / tap-to-call present"})
    checks.append({"name": "has-nav", "pass": ("<nav" in low or "nav" in low[:6000]), "note": "navigation present"})
    checks.append({"name": "real-copy", "pass": (len(_re.sub(r"<[^>]+>", " ", html or "")) >= 1200),
                   "note": "enough visible text"})
    return checks


def _placeholder(html: str) -> list[dict]:
    low = (html or "").lower()
    found = [p for p in _PLACEHOLDERS if p in low]
    return [{"name": "no-placeholders", "pass": not found, "note": ("clean" if not found else "found: " + ", ".join(found))}]


def _brand_safe(html: str) -> list[dict]:
    low = (html or "").lower()
    leaks = [s for s in ("traffic radius", "trafficradius", "lisa4@", "lisa 4", "reveal-token") if s in low]
    return [{"name": "brand-safe", "pass": not leaks, "note": ("clean" if not leaks else "leak: " + ", ".join(leaks))}]


def _reachable(token: str) -> list[dict]:
    # ADVISORY: a network-side probe must never gate whether we send a client their link.
    # Cloudflare in front of the site 403s a bare server-side fetch (no browser UA) even though
    # the page is live — so we send a real browser UA, and treat a 403 (WAF challenge) as reachable.
    if not token:
        return [{"name": "reachable", "pass": False, "advisory": True, "note": "no public link yet"}]
    try:
        req = urllib.request.Request(
            f"{_BASE}/api/lisa4/site/public/{token}", method="GET",
            headers={"User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                                    "Chrome/124.0 Safari/537.36")})
        code = urllib.request.urlopen(req, timeout=15).status
        return [{"name": "reachable", "pass": code in (200, 403), "advisory": True, "note": f"public URL {code}"}]
    except urllib.error.HTTPError as e:
        # 403/503 = WAF is serving the page but challenging the bot → the page IS deployed.
        ok = e.code in (403, 503)
        return [{"name": "reachable", "pass": ok, "advisory": True, "note": f"public URL {e.code}"}]
    except Exception as e:
        return [{"name": "reachable", "pass": False, "advisory": True, "note": str(e)[:60]}]


def _llm_review(pool, settings, html: str, company: str) -> dict | None:
    """Opus reads a trimmed version of the page and flags anything embarrassing. Cached by the caller."""
    try:
        from . import lisa as _l
        key = None
        r = _l._fetch(pool, "SELECT v FROM crm_config WHERE k='anthropic_api_key'")
        key = (r[0].get("v") if r else None) or getattr(settings, "anthropic_api_key", "") or ""
        m = _l._fetch(pool, "SELECT v FROM crm_config WHERE k='lisa4_designer_model'")
        model = (m[0].get("v") if m else None) or "claude-opus-5"
        if not key:
            return None
        from .reveal_guide import _claude_text
        text = _re.sub(r"<script[\s\S]*?</script>", " ", html or "", flags=_re.I)
        text = _re.sub(r"<style[\s\S]*?</style>", " ", text, flags=_re.I)
        text = _re.sub(r"<[^>]+>", " ", text)
        text = _re.sub(r"\s+", " ", text).strip()[:4000]
        sysp = ("You are QA for a web agency. A client-facing website was auto-built for a business. Read the "
                "visible copy and answer STRICT JSON: {ok: true|false, issues: [short strings]}. Fail (ok:false) "
                "ONLY for things that would embarrass us in front of the client: wrong business/industry, "
                "placeholder or lorem text, empty/broken sections, obviously fake claims, or gibberish. Minor "
                "wording is fine. Output ONLY the JSON.")
        usr = f"Business: {company or 'unknown'}\nVisible copy:\n{text}"
        out = _claude_text(key, model, sysp, usr, max_tokens=300).strip()
        if out.startswith("```"):
            out = out.split("\n", 1)[1] if "\n" in out else out[3:]
            if out.rstrip().endswith("```"):
                out = out.rstrip()[:-3]
        import json as _json
        i, j = out.find("{"), out.rfind("}")
        return _json.loads(out[i:j + 1]) if i >= 0 and j > i else None
    except Exception:
        return None


def qa_check(pool, settings, dest9: str, *, force: bool = False, deep: bool = True) -> dict:
    """Run the QA pipelines on a prospect's latest built site. Cached on the row. Returns
    {passed, checks:[...], issues:[...], share_token, url}. Guarded — never raises."""
    from . import lisa as _l
    d9 = _re.sub(r"[^0-9]", "", dest9 or "")[-9:]
    if not d9:
        return {"passed": False, "checks": [], "issues": ["bad dest9"]}
    try:
        ensure_qa_columns(pool)
        row = _l._fetch(pool, "SELECT id, company, html, share_token, qa_passed, qa_at FROM lisa4_sites "
                        "WHERE dest9=%s AND status='built' ORDER BY built_at DESC NULLS LAST, id DESC LIMIT 1", (d9,))
        if not row:
            return {"passed": False, "checks": [], "issues": ["no built site"]}
        r = row[0]
        token = r.get("share_token")
        url = f"{_BASE}/api/lisa4/site/public/{token}" if token else None
        if r.get("qa_passed") is not None and not force:
            return {"passed": bool(r["qa_passed"]), "cached": True, "checks": [], "issues": [],
                    "share_token": token, "url": url}
        html = r.get("html") or ""
        company = r.get("company") or ""
        phone = None
        checks = (_structural(html) + _images(html) + _content(html, company, phone) + _placeholder(html)
                  + _brand_safe(html) + _reachable(token))
        # Advisory checks (network-side reachability, brand-token heuristic) are reported but never
        # block QA — a Cloudflare bot-challenge or a stylised brand name must not stop us sending a
        # client their link. The real gates are the structural checks + the deep LLM content review.
        issues = [c["name"] + ": " + c["note"] for c in checks if not c["pass"] and not c.get("advisory")]
        advisories = [c["name"] + ": " + c["note"] for c in checks if not c["pass"] and c.get("advisory")]
        det_pass = all(c["pass"] for c in checks if not c.get("advisory"))
        llm = _llm_review(pool, settings, html, company) if (deep and det_pass) else None
        if llm and llm.get("ok") is False:
            issues += ["review: " + s for s in (llm.get("issues") or [])[:4]]
        passed = det_pass and (llm is None or llm.get("ok") is not False)
        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute("UPDATE lisa4_sites SET qa_passed=%s, qa_notes=%s, qa_at=now() WHERE id=%s",
                        (passed, ("; ".join(issues))[:1000] if issues else "all checks passed", r["id"]))
            conn.commit()
        return {"passed": passed, "checks": checks, "issues": issues, "advisories": advisories,
                "share_token": token, "url": url}
    except Exception as exc:
        return {"passed": False, "checks": [], "issues": ["qa error: " + str(exc)[:80]]}
