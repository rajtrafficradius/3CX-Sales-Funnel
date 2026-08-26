"""FIREFLIES meeting-watch — capture Alfred's (and the reps') recorded calls/meetings, match them to a
prospect, and AUTO-DETECT callbacks (e.g. "call back in September") so nothing slips.

Fully ISOLATED + guarded: its own table (no FK to calls, so it can never break call ingestion), a watermark
in crm_config, and it never touches the dialer. Matching is by the PHONE NUMBERS in the Fireflies title
(e.g. "Alfred Marion [+61 3 7058 1028] <> +61 403 728 261"), since prospect emails aren't captured. Fireflies
already returns a summary + action_items; a small Opus call normalises any callback timing to a real date.
"""
import re as _re
import json as _json
import urllib.request

_FF_URL = "https://api.fireflies.ai/graphql"


def _cfg(pool, key, default=None):
    try:
        from . import lisa as _l
        r = _l._fetch(pool, "SELECT v FROM crm_config WHERE k=%s", (key,))
        if r and (r[0].get("v") or "").strip():
            return r[0]["v"].strip()
    except Exception:
        pass
    return default


def _set_cfg(pool, key, val):
    try:
        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute("INSERT INTO crm_config (k,v) VALUES (%s,%s) ON CONFLICT (k) DO UPDATE SET v=EXCLUDED.v", (key, str(val)))
            conn.commit()
    except Exception:
        pass


def _ff(key: str, query: str, variables: dict | None = None, timeout: int = 30) -> dict:
    body = _json.dumps({"query": query, "variables": variables or {}}).encode()
    req = urllib.request.Request(_FF_URL, data=body, method="POST",
                                 headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    r = _json.loads(urllib.request.urlopen(req, timeout=timeout).read())
    if r.get("errors"):
        raise RuntimeError(str(r["errors"])[:200])
    return r.get("data") or {}


def ensure_fireflies_tables(pool) -> None:
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute("CREATE TABLE IF NOT EXISTS fireflies_meetings ("
                    "  id text PRIMARY KEY, dest9 text, title text, meeting_date timestamptz, duration_min numeric,"
                    "  matched_company text, matched_by text, summary text, action_items text,"
                    "  callback_requested boolean, callback_when text, callback_date date, outcome text,"
                    "  transcript text, created_at timestamptz DEFAULT now())")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_ff_meetings_dest9 ON fireflies_meetings(dest9)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_ff_meetings_date ON fireflies_meetings(meeting_date)")
        conn.commit()


def _phones_from_title(title: str) -> list[str]:
    """Pull the 9-digit AU keys from the phone numbers in a Fireflies title."""
    out = []
    for m in _re.findall(r"\+?61[\s\d]{7,}|\b0[\s\d]{8,}", title or ""):
        d = _re.sub(r"[^0-9]", "", m)
        if d.startswith("61"):
            d = d[2:]
        d = d[-9:]
        if len(d) == 9 and d not in out:
            out.append(d)
    return out


def _caller_phone_from_title(title: str) -> str | None:
    """The Fireflies account-holder's OWN line is the number in [brackets] (e.g.
    'Alfred Marion [+61 468 011 718] <> +61 432 374 361'). That is the REP/closer line, NEVER the prospect —
    return its 9-digit key so the matcher can exclude it. This bracketed number appears FIRST in the title and
    used to win the match falsely (it exists in companies/calls), filing Alfred's confirmation transcript under
    Alfred instead of the prospect he called — the root of the 'details never update after Alfred's call' bug."""
    m = _re.search(r"\[([^\]]*)\]", title or "")
    if not m:
        return None
    d = _re.sub(r"[^0-9]", "", m.group(1))
    if d.startswith("61"):
        d = d[2:]
    d = d[-9:]
    return d if len(d) == 9 else None


def _match_prospect(pool, dest9s: list[str], caller_d9: str | None = None):
    """Among the phones in the title, find the PROSPECT (has a Lisa call / booking / pool row). Picks the
    STRONGEST prospect signal across ALL title phones — not merely the first one that matches — and NEVER the
    rep's own bracketed line (caller_d9). A real booked prospect scores has_booking+has_lisa; the rep's line
    scores at most has_call/has_co, so it can no longer hijack the match. Returns (dest9, company, matched_by)
    or (None, None, None)."""
    from . import lisa as _l
    best = None  # (score, d9, company, by)
    for d9 in dest9s:
        if caller_d9 and d9 == caller_d9:
            continue  # the rep/closer's own line is never the prospect
        r = _l._fetch(pool,
            "SELECT "
            "  (SELECT company_name FROM lisa_calls WHERE dest9=%s AND company_name IS NOT NULL ORDER BY started_at DESC LIMIT 1) c1, "
            "  (SELECT company FROM lisa4_pool WHERE dest9=%s LIMIT 1) c2, "
            "  (SELECT company_name FROM companies WHERE right(regexp_replace(COALESCE(phone,''),'[^0-9]','','g'),9)=%s ORDER BY (source='gmaps') DESC LIMIT 1) c3, "
            "  EXISTS(SELECT 1 FROM lisa_calls WHERE dest9=%s) has_lisa, "
            "  EXISTS(SELECT 1 FROM booked_crm WHERE dest9=%s) has_booking, "
            "  EXISTS(SELECT 1 FROM calls WHERE right(regexp_replace(COALESCE(dest_number,''),'[^0-9]','','g'),9)=%s) has_call, "
            "  EXISTS(SELECT 1 FROM companies WHERE right(regexp_replace(COALESCE(phone,''),'[^0-9]','','g'),9)=%s) has_co",
            (d9, d9, d9, d9, d9, d9, d9))
        if not r:
            continue
        x = r[0]
        score = ((8 if x.get("has_booking") else 0) + (4 if x.get("has_lisa") else 0)
                 + (2 if x.get("has_call") else 0) + (1 if x.get("has_co") else 0))
        if score <= 0:
            continue
        company = x.get("c1") or x.get("c2") or x.get("c3")
        by = ("booking" if x.get("has_booking") else "lisa_call" if x.get("has_lisa")
              else "aircall" if x.get("has_call") else "company")
        if best is None or score > best[0]:
            best = (score, d9, company, by)
    if best:
        return best[1], best[2], best[3]
    return None, None, None


def _classify_callback(pool, settings, title, summary, action_items, transcript_excerpt):
    """Small Opus call: normalise the meeting into a structured callback/outcome. Returns a dict or {}.
    Uses the crm_config funded key. Guarded."""
    key = _cfg(pool, "anthropic_api_key") or getattr(settings, "anthropic_api_key", "") or ""
    model = _cfg(pool, "lisa4_designer_model", "claude-opus-5")
    if not key:
        return {}
    from .reveal_guide import _claude_text
    sysp = ("You read a sales-call transcript/summary — OUR rep (e.g. Alfred/Emma) calling a prospect who has "
            "already booked a website-reveal/meeting — and extract a strict JSON object. Fields: "
            "contact_name (the PROSPECT's own name as spoken, NEVER our rep's name; '' if unclear), "
            "email (the prospect's email ONLY if clearly spelled out/stated on the call, copied EXACTLY, never "
            "invented or completed; else ''), "
            "meeting_when (the agreed reveal day/time in their words, e.g. 'tomorrow 10:30', 'Tuesday 2pm'; else ''), "
            "website_status (one of: 'no_website' if the prospect says they do NOT have a website / not yet; "
            "'wrong_website' if they say the website/site shown to them is NOT theirs / 'that's not my site'; "
            "'has_website' if they confirm a working website that is genuinely theirs; else ''), "
            "callback_requested (true if the prospect asked to be contacted later / not now / at a future "
            "time), callback_when (the phrase they used, e.g. 'in September', 'in a couple of months', or ''), "
            "callback_date (best YYYY-MM-DD if a concrete month/'2 months'/'next week' can be resolved from "
            "today, else ''), outcome (one of: interested, callback, not_interested, already_has_vendor, "
            "no_answer, other), one_line (a short plain summary). Use '' (not a guess) for anything not clearly "
            "stated. Output ONLY the JSON object, nothing else.")
    today = _cfg(pool, "_today_hint", "")  # optional; model resolves relative dates from 'today' if given
    usr = (f"Title: {title}\nToday: {today or 'unknown (leave callback_date empty if you cannot resolve it)'}\n"
           f"Fireflies summary: {summary or ''}\nAction items: {action_items or ''}\n"
           f"Transcript excerpt:\n{(transcript_excerpt or '')[:2500]}")
    try:
        txt = _claude_text(key, model, sysp, usr, max_tokens=400)
        txt = txt.strip()
        if txt.startswith("```"):
            txt = txt.split("\n", 1)[1] if "\n" in txt else txt[3:]
            if txt.rstrip().endswith("```"):
                txt = txt.rstrip()[:-3]
        i, j = txt.find("{"), txt.rfind("}")
        return _json.loads(txt[i:j + 1]) if i >= 0 and j > i else {}
    except Exception:
        return {}


_EMAIL_RE = _re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_REP_NAMES = {"alfred", "emma", "lisa", "manoj", "ben", "raven", "raj", "vidhun", "xavier"}


def _ff_warn(event: str, **kw) -> None:
    try:
        from .lisa import log as _l
        _l.warning(event, **{k: (str(v)[:140] if isinstance(v, str) else v) for k, v in kw.items()})
    except Exception:
        pass


def _capture_confirmation(pool, dest9: str, cls: dict, title: str, settings=None, ref_dt=None) -> dict:
    """Push a matched confirmation transcript onto booked_crm — the piece that was missing, so Alfred's calls
    finally UPDATE the CRM. Blank-fills the prospect's name/email (NEVER overwrites a human-entered value),
    FIRMS the reveal time (a vague 'tomorrow morning' → a concrete next_action_at, fill-only) from the time
    the prospect agreed on the confirmation call, and ACTS on a website correction the prospect stated on the
    call: 'I have no website' / 'that site isn't mine' → strip the mis-attributed domain, flag the row
    no_website (so the reveal rebuilds fresh, never scraping a namesake), and post a prominent one-time CRM
    alert. Fully guarded; the domain-strip + alert fire at most once per prospect (guarded on an existing
    'website_alert' marker); every write is fill-blank so a human/closer edit is never overwritten."""
    out = {"name": False, "email": False, "website": None, "time": False}
    try:
        name = (cls.get("contact_name") or "").strip()
        if name.lower() in _REP_NAMES:
            name = ""
        email = (cls.get("email") or "").strip()
        if email and not _EMAIL_RE.match(email):
            email = ""
        ws = (cls.get("website_status") or "").strip().lower()
        with pool.connection() as conn, conn.cursor() as cur:
            if name:
                cur.execute(
                    "UPDATE booked_crm SET contact_name=%s, updated_at=now() WHERE dest9=%s AND "
                    "(contact_name IS NULL OR btrim(lower(contact_name)) IN ('','there','(unknown)','unknown','sir','madam','mate'))",
                    (name[:120], dest9))
                out["name"] = cur.rowcount > 0
            if email:
                cur.execute(
                    "UPDATE booked_crm SET contact_email=%s, updated_at=now() WHERE dest9=%s AND "
                    "(contact_email IS NULL OR btrim(contact_email)='')", (email[:160], dest9))
                out["email"] = cur.rowcount > 0
            # FIRM THE REVEAL TIME: a vague booking ('tomorrow morning') gets a concrete slot from the time the
            # prospect agreed on Alfred's confirmation call. Fill-ONLY (never overrides a set time), future-only.
            mw = (cls.get("meeting_when") or "").strip()
            if mw and settings is not None and ref_dt is not None:
                try:
                    from .emma import parse_agreed_time
                    from datetime import datetime as _dt
                    tz = getattr(settings, "tz", "Australia/Melbourne")
                    when = parse_agreed_time(mw, ref_dt, tz)
                    now = (_dt.now(when.tzinfo) if (when and when.tzinfo) else None)
                    if when and (now is None or when > now):
                        cur.execute("UPDATE booked_crm SET next_action_at=%s, updated_at=now() WHERE dest9=%s "
                                    "AND next_action_at IS NULL", (when, dest9))
                        out["time"] = cur.rowcount > 0
                except Exception:
                    pass
            if ws in ("no_website", "wrong_website"):
                out["website"] = ws
                already = cur.execute(
                    "SELECT 1 FROM crm_activity WHERE dest9=%s AND kind='website_alert' LIMIT 1", (dest9,)).fetchone()
                if not already:
                    cur.execute("UPDATE lisa4_pool SET domain=NULL, bucket='no_website', issue='no website' WHERE dest9=%s", (dest9,))
                    msg = ("⚠ CONFIRMATION CALL: prospect said " +
                           ("they do NOT have a website yet" if ws == "no_website" else "the website shown is NOT theirs") +
                           " — the mis-attributed site link was removed; the reveal must be a fresh no-website build.")
                    cur.execute("INSERT INTO crm_activity (dest9,kind,body,author) VALUES (%s,'website_alert',%s,'Fireflies')",
                                (dest9, msg))
            conn.commit()
    except Exception as exc:
        _ff_warn("ff_capture_confirmation_failed", dest9=dest9, error=str(exc))
    return out


def run_fireflies_watch(pool, settings, limit: int = 15) -> dict:
    """Poll recent Fireflies transcripts, match to prospects, detect callbacks, store, and surface on the CRM.
    Watermarked so each meeting is processed once. Guarded — never raises into the loop."""
    key = _cfg(pool, "fireflies_api") or getattr(settings, "fireflies_api", "") or ""
    if not key:
        return {"skipped": "no_key"}
    try:
        ensure_fireflies_tables(pool)
        # self-throttle: the loop calls this every cycle, but hit Fireflies at most every ~10 min. Atomic
        # check-and-set — RETURNING yields a row only when we actually claim this run.
        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute("INSERT INTO crm_config (k,v) VALUES ('fireflies_last_run', now()::text) "
                        "ON CONFLICT (k) DO UPDATE SET v=now()::text "
                        "WHERE crm_config.v IS NULL OR crm_config.v::timestamptz < now() - interval '10 minutes' "
                        "RETURNING v")
            claimed = cur.fetchone()
            conn.commit()
        if not claimed:
            return {"skipped": "throttled"}
        data = _ff(key, "query($l:Int){ transcripts(limit:$l){ id title date duration } }", {"l": limit})
        items = data.get("transcripts") or []
        from . import lisa as _l
        processed = matched = callbacks = 0
        for t in items:
            tid = t.get("id")
            if not tid:
                continue
            # already processed?
            seen = _l._fetch(pool, "SELECT 1 FROM fireflies_meetings WHERE id=%s", (tid,))
            if seen:
                continue
            title = t.get("title") or ""
            d9s = _phones_from_title(title)
            dest9, company, matched_by = _match_prospect(pool, d9s, _caller_phone_from_title(title))
            # detail (sentences + summary)
            detail = {}
            try:
                dd = _ff(key, "query($id:String!){ transcript(id:$id){ summary{ overview action_items short_summary } "
                         "sentences{ text speaker_name } } }", {"id": tid})
                detail = dd.get("transcript") or {}
            except Exception:
                detail = {}
            sm = detail.get("summary") or {}
            summary = sm.get("short_summary") or sm.get("overview") or ""
            action_items = sm.get("action_items") or ""
            sents = detail.get("sentences") or []
            transcript = "\n".join(f"{s.get('speaker_name') or '?'}: {s.get('text') or ''}" for s in sents)
            cls = _classify_callback(pool, settings, title, summary, action_items, transcript) if (summary or transcript) else {}
            cb = bool(cls.get("callback_requested"))
            cb_when = (cls.get("callback_when") or "")[:120]
            cb_date = cls.get("callback_date") or None
            try:
                cb_date = cb_date if (cb_date and _re.match(r"^\d{4}-\d{2}-\d{2}$", str(cb_date))) else None
            except Exception:
                cb_date = None
            mdate = None
            try:
                if t.get("date"):
                    mdate = int(t["date"]) / 1000.0
            except Exception:
                mdate = None
            with pool.connection() as conn, conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO fireflies_meetings (id,dest9,title,meeting_date,duration_min,matched_company,"
                    "matched_by,summary,action_items,callback_requested,callback_when,callback_date,outcome,transcript) "
                    "VALUES (%s,%s,%s, to_timestamp(%s), %s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (id) DO NOTHING",
                    (tid, dest9, title[:300], mdate, round(float(t.get("duration") or 0), 2), company, matched_by,
                     summary[:4000], action_items[:2000], cb, cb_when, cb_date, (cls.get("outcome") or "")[:40],
                     transcript[:60000]))
                # surface a detected callback on the matched prospect's CRM
                if dest9 and cb:
                    note = "Fireflies: prospect asked to be contacted" + (f" {cb_when}" if cb_when else " later") + \
                           (f" (meeting: {title[:60]})" if title else "")
                    cur.execute("INSERT INTO crm_activity (dest9,kind,body,author) VALUES (%s,'system',%s,'Fireflies')", (dest9, note))
                    if cb_date:
                        cur.execute("INSERT INTO booked_crm (dest9,next_action,next_action_at,updated_by,updated_at) "
                                    "VALUES (%s,%s,%s,'Fireflies',now()) ON CONFLICT (dest9) DO UPDATE SET "
                                    "next_action=EXCLUDED.next_action, next_action_at=EXCLUDED.next_action_at, updated_at=now()",
                                    (dest9, ("Call back " + (cb_when or "")).strip()[:200], cb_date))
                conn.commit()
            # NEW: actually update the CRM from the (correctly-matched) confirmation transcript —
            # blank-fill name/email, firm the reveal time, act on a stated no/wrong-website. Guarded.
            if dest9:
                _ref = None
                try:
                    if mdate:
                        from datetime import datetime as _dtm, timezone as _tz
                        _ref = _dtm.fromtimestamp(mdate, _tz.utc)
                except Exception:
                    _ref = None
                _capture_confirmation(pool, dest9, cls, title, settings=settings, ref_dt=_ref)
            processed += 1
            matched += 1 if dest9 else 0
            callbacks += 1 if cb else 0
        return {"processed": processed, "matched": matched, "callbacks": callbacks, "seen": len(items)}
    except Exception as exc:
        try:
            from .lisa import log as _log
            _log.warning("fireflies_watch_failed", error=str(exc)[:160])
        except Exception:
            pass
        return {"error": "guarded"}
