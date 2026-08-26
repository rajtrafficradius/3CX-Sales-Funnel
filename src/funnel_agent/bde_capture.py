"""REALTIME BDE-call capture — map Alfred's Aircall confirmation-call ACTION onto the booked CRM.

THE GAP THIS CLOSES: Alfred's Aircall confirmation calls ARE ingested into `calls` (provider='aircall',
matched to a prospect by the trailing-9 of dest_number), but the single most reliable + IMMEDIATE signal —
the NOTE/tags Alfred TYPES on the call in Aircall — was discarded, and the STT transcript LAGS (a recording
sits has_transcript=false for a while), so nothing reached `booked_crm` and the team re-keyed by hand. This
module now captures, per active booking, in PRIORITY order:
  (a) Alfred's Aircall COMMENT (his own written summary) + tags + the linked contact — immediate, no STT;
  (b) the STT transcript, as a fallback/enrichment.
Both FILL BLANKS ONLY (never overwriting a human's value; next_action appends), write via crm.crm_update,
APPEND Alfred's words verbatim to the timeline, and ADVANCE the pipeline stage without regressing (answered
call / confirm|booked tag → 'confirmed'; Fireflies reveal meeting → 'revealed'). To beat the STT lag, each
pass first RE-FETCHES recent closer calls from Aircall (fresh comments/tags/contact into aircall_call_meta)
and prompt-transcribes any booked-prospect recording still missing a transcript.

SAFETY: fully guarded (never raises into the loop), self-throttled via crm_config so the OpenAI/API cost
runs at most ~every 5 minutes, and it NEVER touches the dialer.

IDEMPOTENCY: the marker table `bde_capture_seen(call_id PK)` records every source item processed —
Aircall TRANSCRIPTS under the call_id as-is; Fireflies meetings under 'ff:'<id>; Alfred's NOTES under
'cmt:'<call_id>':'<sig>, where sig is an 8-hex hash of the comments+tags text. Keying the note marker on
the comment signature means a NEW/edited comment on an already-seen call is captured again (new sig → new
key), while the same note is classified (and paid for) exactly once.
"""
from __future__ import annotations

import hashlib
import json
import re


# --------------------------------------------------------------------------- #
# Extraction schema + prompt (mirrors messages._classify_text's strict-JSON pattern)
# --------------------------------------------------------------------------- #
_CAPTURE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "contact_name": {"type": ["string", "null"]},
        "company": {"type": ["string", "null"]},
        "email": {"type": ["string", "null"]},
        "next_action": {"type": ["string", "null"]},
        "meeting_datetime": {"type": ["string", "null"]},
        "meeting_when_text": {"type": ["string", "null"]},
        "outcome": {"type": ["string", "null"]},
        "summary": {"type": ["string", "null"]},
    },
    "required": ["contact_name", "company", "email", "next_action",
                 "meeting_datetime", "meeting_when_text", "outcome", "summary"],
}

_SYS = (
    "You read a TRANSCRIPT of a phone call OUR business-development rep made to a prospect who has "
    "already booked a meeting / website-reveal with us. Extract a strict JSON object of the facts the "
    "rep would log in the CRM. Fields:\n"
    "- contact_name: the PROSPECT's own name (the person the rep spoke to), or null. Never our rep's name.\n"
    "- company: the prospect's business name, or null.\n"
    "- email: the prospect's email address if it was spelled out / stated / confirmed on the call "
    "(e.g. 'name@company.com'), else null. Copy it EXACTLY — never invent or complete a partial address.\n"
    "- next_action: the agreed NEXT step in a few words (e.g. 'reveal Thu 2pm', 'call back Friday', "
    "'send proposal'), or null.\n"
    "- meeting_datetime: the day/time agreed for the meeting/reveal, in the speakers' own words, or null.\n"
    "- meeting_when_text: the EXACT spoken meeting day/time phrase, VERBATIM as said (e.g. 'Monday the "
    "24th at 12:30pm', 'next Tuesday 10am', 'tomorrow at 3'), or null. Copy the words as spoken — do NOT "
    "reformat, resolve a calendar date, or infer anything that was not said.\n"
    "- outcome: exactly one of confirmed, reschedule, callback, not_interested, no_answer, other.\n"
    "- summary: one plain sentence summarising the call.\n"
    "Use null (NOT a guess) for anything not clearly stated. Output ONLY the JSON object."
)


# --------------------------------------------------------------------------- #
# Small local helpers (kept lightweight; lisa.py imported lazily where needed)
# --------------------------------------------------------------------------- #
def _fetch(pool, sql, params=None):
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(sql, params if params is not None else ())
        return cur.fetchall()


def _clean(v) -> str:
    return v.strip() if isinstance(v, str) else ""


def _log_warn(event: str, **kw) -> None:
    try:
        from .lisa import log as _l
        _l.warning(event, **{k: (str(v)[:140] if isinstance(v, str) else v) for k, v in kw.items()})
    except Exception:
        pass


def ensure_bde_capture_tables(pool) -> None:
    """The idempotency marker — additive + isolated (NO FK to calls, so it can never break ingestion)."""
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute("CREATE TABLE IF NOT EXISTS bde_capture_seen ("
                    "  call_id text PRIMARY KEY, dest9 text, captured_at timestamptz DEFAULT now())")
        conn.commit()


def _closers(settings) -> list[str]:
    """The SAME closer list the CRM uses (settings.reveal_closers + Alfred), deduped — so we capture the
    calls that show up as `bde_attempts` on the booking page."""
    base = list(getattr(settings, "reveal_closers", []) or [])
    return list(dict.fromkeys(base + ["Alfred"])) or ["Alfred"]


def _throttle_claim(pool, minutes: int = 10) -> bool:
    """Atomic check-and-set (identical pattern to fireflies.run_fireflies_watch): claim a run only when the
    last one was >N min ago, so the OpenAI cost fires at most ~every N minutes even though the loop calls
    us every tick. RETURNING yields a row only when WE win the claim. Guarded → False on any error."""
    try:
        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute("INSERT INTO crm_config (k,v) VALUES ('bde_capture_last_run', now()::text) "
                        "ON CONFLICT (k) DO UPDATE SET v=now()::text "
                        "WHERE crm_config.v IS NULL "
                        "   OR crm_config.v::timestamptz < now() - make_interval(mins => %s) "
                        "RETURNING v", (int(minutes),))
            claimed = cur.fetchone()
            conn.commit()
        return bool(claimed)
    except Exception:
        return False


def _classify_transcript(oai, model: str, text: str) -> dict:
    """One cheap-model call → strict JSON. Same client + response_format pattern as messages._classify_text."""
    resp = oai.chat.completions.create(
        model=model,
        messages=[{"role": "system", "content": _SYS},
                  {"role": "user", "content": (text or "")[:9000]}],
        response_format={"type": "json_schema",
                         "json_schema": {"name": "bde_capture", "schema": _CAPTURE_SCHEMA, "strict": True}},
        temperature=0,
    )
    return json.loads(resp.choices[0].message.content)


def _mark_seen(pool, key: str, dest9: str | None) -> None:
    try:
        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute("INSERT INTO bde_capture_seen (call_id,dest9) VALUES (%s,%s) "
                        "ON CONFLICT (call_id) DO NOTHING", (key, dest9))
            conn.commit()
    except Exception:
        pass


def _merge_fields(res: dict, *, cur_name: str, cur_email: str, cur_company: str,
                  bc_next: str, bc_outcome: str, booked_at=None,
                  tz: str = "Australia/Melbourne", cur_next_at=None):
    """Build the fill-blank / append field set for crm.crm_update from one extraction.

    RULE (per spec): only pass a key when its current stored value is EMPTY (never overwrite a human's
    value); the sole exception is next_action, which APPENDS the newly-captured step to whatever's there.

    Two auto-captures the closer's confirmation call must reliably land (owner request):
      • the prospect's EMAIL — when a valid-looking address was stated and NO email is stored yet, fill BOTH
        booked_crm.contact_email (the CRM field the operator watches) and the lisa_calls identity email.
      • the concrete MEETING TIME — parse the raw spoken phrase (meeting_when_text / meeting_datetime) with
        emma.parse_agreed_time relative to the CALL time; if it resolves to a FUTURE datetime and the CRM has
        no scheduled time yet, fill booked_crm.next_action_at and append the readable phrase to next_action.
        Fully guarded: any import/parse failure just leaves next_action_at untouched (never raises).
    Returns (fields, outcome, meeting_when_text)."""
    fields: dict = {}
    name = _clean(res.get("contact_name"))
    company = _clean(res.get("company"))
    email = _clean(res.get("email"))
    if name and not cur_name:
        fields["prospect_name"] = name[:120]                       # flows to lisa_calls + mirrors booked_crm.contact_name
    if company and not cur_company:
        fields["company"] = company[:160]
    if email and re.search(r"[^@\s]+@[^@\s]+\.[^@\s]+", email) and not cur_email:
        fields["email"] = email[:160]                              # → lisa_calls.prospect_email (prospect identity)
        fields["contact_email"] = email[:160]                      # → booked_crm.contact_email (fill-blank only)
    outcome = _clean(res.get("outcome")).lower()
    if outcome and outcome != "other" and not _clean(bc_outcome):
        fields["outcome"] = outcome[:40]
    # The raw spoken meeting phrase — prefer the explicit verbatim field, fall back to the legacy one.
    mdt = _clean(res.get("meeting_when_text")) or _clean(res.get("meeting_datetime"))
    # Turn it into a CONCRETE datetime by REUSING emma's robust parser (never a new parser, never a guess).
    # Only meaningful relative to a known call time, and we keep only a FUTURE result — a phrase from an
    # already-past reveal must not schedule a bogus next action.
    next_at = None
    if mdt and booked_at:
        try:
            from .emma import parse_agreed_time
            from datetime import datetime as _dt, timezone as _tzc
            dt = parse_agreed_time(mdt, booked_at, tz or "Australia/Melbourne")
            if dt is not None and dt > (_dt.now(dt.tzinfo) if dt.tzinfo else _dt.now(_tzc.utc)):
                next_at = dt
        except Exception:
            next_at = None                                         # guarded: bad parse → leave next_action_at as-is
    if next_at is not None and not cur_next_at:                    # fill-blank: never overwrite a human's time
        fields["next_action_at"] = next_at
    # next_action: keep whatever's stored, then append (once, deduped) the agreed step and — only when we
    # actually filled next_action_at just now — a readable "meeting <spoken phrase>" alongside it.
    merged = _clean(bc_next)
    for step in (_clean(res.get("next_action")),
                 ("meeting " + mdt) if (fields.get("next_action_at") is not None and mdt) else ""):
        if step and step.lower() not in merged.lower():            # append once — don't duplicate the same step
            merged = (merged + " · " + step) if merged else step
    if merged and merged != _clean(bc_next):
        fields["next_action"] = merged[:200]
    return fields, outcome, mdt


def _append_note(pool, dest9: str, label: str, author: str, summary: str,
                 outcome: str, meeting_datetime: str, meta: dict) -> None:
    """Append-only timeline row — never overwrites; carries the captured summary + source call_id in meta."""
    note = label
    if summary:
        note += ": " + summary
    bits = []
    if outcome:
        bits.append("outcome " + outcome)
    if meeting_datetime:
        bits.append("meeting " + meeting_datetime)
    if bits:
        note += " (" + "; ".join(bits) + ")"
    try:
        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute("INSERT INTO crm_activity (dest9,kind,body,author,meta) "
                        "VALUES (%s,'system',%s,%s,%s)",
                        (dest9, note[:2000], author[:80], json.dumps(meta)))
            conn.commit()
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# RESCHEDULE FIX (recurring bug, Vysakh): a closer/confirmation call that RESCHEDULES a booked
# meeting must MOVE the actual meeting — not just append the next_action TEXT. The record page +
# calendar read the displayed time from the prospect's live calendar_events reveal/meeting row
# (see crm.LIST_SQL: type IN ('reveal','meeting'), pending-first then latest start), so unless
# that row moves the CRM keeps showing the OLD slot. These helpers re-time that row + re-point
# booked_crm.next_action_at whenever a reschedule/callback day-time is captured. Fully guarded,
# idempotent, and they touch ONLY calendar_events + booked_crm.next_action_at (never the dialer).
# --------------------------------------------------------------------------- #
def _fmt_dt(dt, tz: str) -> str:
    """Readable Melbourne wall-clock rendering for CRM notes. Never raises."""
    try:
        from zoneinfo import ZoneInfo
        return dt.astimezone(ZoneInfo(tz or "Australia/Melbourne")).strftime("%A %d %B %Y, %H:%M")
    except Exception:
        return str(dt)


def _active_meeting_row(pool, dest9: str) -> dict | None:
    """The prospect's LIVE calendar_events meeting/reveal row — the SAME row crm.LIST_SQL reads the
    displayed meeting time from (type IN ('reveal','meeting'), not cancelled, pending first then
    latest start). Returns {id,start_at,end_at,status,type} or None. Never raises."""
    try:
        rows = _fetch(pool,
                      "SELECT id, start_at, end_at, status, type FROM calendar_events"
                      " WHERE type IN ('reveal','meeting') AND status <> 'cancelled'"
                      "   AND right(regexp_replace(COALESCE(dest_number,''),'[^0-9]','','g'),9)=%s"
                      " ORDER BY (status='pending') DESC, start_at DESC NULLS LAST LIMIT 1", (dest9,))
        return rows[0] if rows else None
    except Exception as exc:
        _log_warn("bde_capture_meeting_row_failed", dest9=dest9, error=str(exc))
        return None


def _parse_reschedule_dt(phrase: str, booked_at, cur_start, tz: str):
    """Parse the rescheduled day/time into a CONCRETE FUTURE datetime, REUSING emma.parse_agreed_time
    (never a new parser). When the phrase names only a DAY ('call back Monday') the parser returns
    None; we then KEEP the existing meeting slot's time-of-day (append it and re-parse) so the DATE
    advances sensibly while the time stays put — exactly the manual MASPION fix. Only a future
    result is returned. Never raises."""
    if not phrase or not booked_at:
        return None
    try:
        from datetime import datetime as _dt
        from zoneinfo import ZoneInfo
        from .emma import parse_agreed_time
        tz = tz or "Australia/Melbourne"
        dt = parse_agreed_time(phrase, booked_at, tz)
        if dt is None and cur_start is not None:                # day-only phrase → reuse prior slot's time
            try:
                local = cur_start.astimezone(ZoneInfo(tz))
                hh, mm = local.hour, local.minute
                suffix = " %d:%02d%s" % (hh % 12 or 12, mm, "am" if hh < 12 else "pm")
                dt = parse_agreed_time(str(phrase) + suffix, booked_at, tz)
            except Exception:
                dt = None
        if dt is None:
            return None
        now = _dt.now(dt.tzinfo) if dt.tzinfo else _dt.now()
        return dt if dt > now else None
    except Exception:
        return None


def _apply_reschedule(pool, settings, dest9: str, *, outcome: str, phrase: str,
                      booked_at, who: str = "aircall-capture", out: dict | None = None) -> bool:
    """When a closer/confirmation call RESCHEDULES a booked meeting, MOVE the live calendar_events
    reveal/meeting row (the row the record page + calendar show the time from) to the new slot and
    re-point booked_crm.next_action_at — closing the recurring bug where only the next_action TEXT
    moved. Only fires for reschedule/callback outcomes with a parseable FUTURE time. Idempotent (a
    re-run that lands on the same slot is a no-op — the row already sits there and next_action_at is
    unchanged), fully guarded (never raises, never blocks capture), and it touches ONLY
    calendar_events + booked_crm.next_action_at — never the dialer / Lisa's Retell prompt. Returns
    True only when the calendar row was actually moved."""
    try:
        if (outcome or "").strip().lower() not in ("reschedule", "callback"):
            return False
        from datetime import timedelta
        row = _active_meeting_row(pool, dest9)
        cur_start = row.get("start_at") if row else None
        tz = getattr(settings, "tz", "Australia/Melbourne")
        new_dt = _parse_reschedule_dt(phrase, booked_at, cur_start, tz)
        if new_dt is None:
            return False
        # idempotent: the row already sits within a minute of the new slot → nothing to move.
        already = bool(cur_start) and abs((cur_start - new_dt).total_seconds()) <= 60
        # 1) MOVE the calendar row (never duplicate) — keep its original duration; no row ⇒ skip.
        moved = False
        if row and row.get("id") and not already:
            old_s, old_e = row.get("start_at"), row.get("end_at")
            dur = (old_e - old_s) if (old_s and old_e and old_e > old_s) else timedelta(minutes=15)
            try:
                with pool.connection() as conn, conn.cursor() as cur:
                    cur.execute(
                        "UPDATE calendar_events SET start_at=%s, end_at=%s, status='pending',"
                        " notes=left(COALESCE(notes,'')||' · Rescheduled to '||%s||' (closer confirmation call)',500)"
                        " WHERE id=%s AND status <> 'cancelled'",
                        (new_dt, new_dt + dur, _fmt_dt(new_dt, tz), row["id"]))
                    moved = cur.rowcount > 0
                    conn.commit()
            except Exception as exc:
                _log_warn("bde_capture_move_meeting_failed", dest9=dest9, error=str(exc))
        # 2) point booked_crm.next_action_at at the new slot — write ONLY when it actually changes.
        try:
            with pool.connection() as conn, conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO booked_crm (dest9, next_action_at, updated_by, updated_at)"
                    " VALUES (%s,%s,%s,now())"
                    " ON CONFLICT (dest9) DO UPDATE SET next_action_at=EXCLUDED.next_action_at,"
                    "   updated_by=EXCLUDED.updated_by, updated_at=now()"
                    " WHERE booked_crm.next_action_at IS DISTINCT FROM EXCLUDED.next_action_at",
                    (dest9, new_dt, (who or "aircall-capture")[:80]))
                conn.commit()
        except Exception as exc:
            _log_warn("bde_capture_next_at_failed", dest9=dest9, error=str(exc))
        if moved:
            _append_note(pool, dest9, "Meeting rescheduled", who or "aircall-capture",
                         _fmt_dt(new_dt, tz), (outcome or ""), (phrase or ""),
                         {"source": "bde_reschedule", "new_start_at": new_dt.isoformat()})
            if isinstance(out, dict):
                out["updated"] = out.get("updated", 0) + 1
        return moved
    except Exception as exc:
        _log_warn("bde_capture_reschedule_failed", dest9=dest9, error=str(exc))
        return False


# --------------------------------------------------------------------------- #
# Stage tracking + Aircall NOTES-FIRST capture (Alfred's typed action → CRM, no STT wait)
# --------------------------------------------------------------------------- #
# Linear pipeline rank (crm.STAGES minus the terminal branch) — used to advance a stage but NEVER regress it.
_STAGE_RANK = {"new": 0, "confirming": 1, "confirmed": 2, "revealed": 3, "won": 4}
_TERMINAL = {"won", "lost", "no_show"}          # a human decision — auto-capture never overrides these

# Aircall tag (substring, lower-cased) -> (stage_to_advance_to | None, crm_outcome | None). First match wins
# per field. Kept to distinctive substrings so we never false-match (e.g. we match 'voicemail', not 'vm').
_TAG_MAP: list[tuple[str, tuple[str | None, str | None]]] = [
    ("confirmed", ("confirmed", "confirmed")),
    ("confirm", ("confirmed", "confirmed")),
    ("booked", ("confirmed", "confirmed")),
    ("reschedul", (None, "reschedule")),
    ("callback", (None, "callback")),
    ("call back", (None, "callback")),
    ("call-back", (None, "callback")),
    ("no answer", (None, "no_answer")),
    ("no-answer", (None, "no_answer")),
    ("voicemail", (None, "no_answer")),
    ("not interest", (None, "not_interested")),
    ("not-interest", (None, "not_interested")),
    ("declin", (None, "not_interested")),
    ("revealed", ("revealed", None)),
    ("reveal done", ("revealed", None)),
    ("demo done", ("revealed", None)),
    ("closed won", ("won", None)),
    ("closed-won", ("won", None)),
]


def _parse_json_list(v) -> list:
    """Defensive JSON-array parse (comments / tags stored as JSON text). Never raises."""
    if isinstance(v, (list, dict)):
        return v if isinstance(v, list) else [v]
    if not v:
        return []
    try:
        d = json.loads(v)
        return d if isinstance(d, list) else [d]
    except Exception:
        return []


def _sig(comments_text: str | None, tags_text: str | None) -> str:
    """8-hex signature of the stored comments+tags text — MUST match the SQL
    left(md5(coalesce(comments,'')||'|'||coalesce(tags,'')),8) so idempotency can be pushed into the query."""
    raw = (comments_text or "") + "|" + (tags_text or "")
    return hashlib.md5(raw.encode("utf-8", "ignore")).hexdigest()[:8]


def _tag_signal(tags: list[str]) -> tuple[str | None, str | None]:
    """Map Alfred's Aircall tags -> (stage, outcome). First matching pattern wins per field."""
    stage = outcome = None
    for t in tags or []:
        k = (t or "").strip().lower()
        if not k:
            continue
        for pat, (sg, oc) in _TAG_MAP:
            if pat in k:
                stage = stage or sg
                outcome = outcome or oc
    return stage, outcome


def _advance_stage(pool, d9: str, target: str | None, who: str) -> bool:
    """Advance booked_crm.stage to `target` ONLY when it is strictly later than the current stage and the
    current stage isn't terminal — so a signal can move new->confirmed->revealed but NEVER regresses a later
    stage or overrides a won/lost/no_show decision. crm_update(stage='confirmed') also kicks off the website
    build (idempotent, per crm.py). Guarded → False on any error."""
    if not target:
        return False
    try:
        from .crm import crm_update
        rows = _fetch(pool, "SELECT stage FROM booked_crm WHERE dest9=%s", (d9,))
        cur_stage = ((rows[0].get("stage") if rows else "") or "").strip().lower()
        if cur_stage in _TERMINAL:
            return False
        if _STAGE_RANK.get(target, -1) <= _STAGE_RANK.get(cur_stage, -1):
            return False
        crm_update(pool, d9, {"stage": target}, who=who)
        return True
    except Exception as exc:
        _log_warn("bde_capture_stage_failed", dest9=d9, error=str(exc))
        return False


def _append_verbatim(pool, dest9: str, text: str, comments: list, cid: str) -> None:
    """Append Alfred's EXACT typed words to the timeline as a human 'note' (not a system line), attributed to
    whoever posted the comment, so his own summary shows up verbatim on the booking page. Never overwrites."""
    author = "Alfred"
    try:
        if comments and isinstance(comments[0], dict) and comments[0].get("by"):
            author = comments[0]["by"] or "Alfred"
    except Exception:
        pass
    try:
        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute("INSERT INTO crm_activity (dest9,kind,body,author,meta) VALUES (%s,'note',%s,%s,%s)",
                        (dest9, ("Aircall note — " + (text or "").strip())[:2000], (author or "Alfred")[:80],
                         json.dumps({"call_id": cid, "source": "aircall_comment"})))
            conn.commit()
    except Exception:
        pass


def _refresh_aircall_meta_for_bookings(pool, settings, closers: list[str], limit: int = 15) -> None:
    """LAG FIX: for each active booking, RE-FETCH its recent Aircall closer calls straight from the API so
    Alfred's just-typed comment/tags/contact land within the ~5-min throttle (not on the next daily ingest),
    AND promptly transcribe any recent recording that has no transcript yet so the transcript fallback isn't
    stuck waiting for the daily sweep. Bounded (limit calls/pass) + fully guarded. No-op without Aircall."""
    if not getattr(settings, "aircall_enabled", False):
        return
    rows = _fetch(pool, """
      WITH booked AS (
        SELECT DISTINCT dest9 FROM lisa_calls WHERE COALESCE(meeting_agreed,false) AND dest9 IS NOT NULL)
      SELECT c.call_id,
             right(regexp_replace(COALESCE(c.dest_number,''),'[^0-9]','','g'),9) AS d9,
             c.recording_present, c.has_transcript, c.started_at
      FROM calls c
      JOIN booked b ON b.dest9 = right(regexp_replace(COALESCE(c.dest_number,''),'[^0-9]','','g'),9)
      LEFT JOIN booked_crm bc ON bc.dest9 = b.dest9
      WHERE c.provider='aircall' AND c.bde_name = ANY(%(closers)s)
        AND c.started_at > now() - interval '10 days'
        AND COALESCE(bc.stage,'') NOT IN ('lost','won','no_show')
      ORDER BY c.started_at DESC
      LIMIT %(limit)s
    """, {"closers": closers or ["__none__"], "limit": int(limit)})
    if not rows:
        return
    from .aircall.api import AircallClient
    from .aircall.calls import call_meta
    from .aircall.ingest import ensure_aircall_meta_table, upsert_aircall_meta
    ensure_aircall_meta_table(pool)
    client = AircallClient(settings)
    to_transcribe: list[str] = []
    try:
        fetched: list[tuple] = []
        for r in rows:
            cid = r["call_id"]
            if r.get("recording_present") and not r.get("has_transcript"):
                to_transcribe.append(cid)
            try:
                aid = cid.split(":", 1)[1] if ":" in cid else cid
                call = client.get_call(aid)
                if call:
                    fetched.append((cid, r["d9"], call_meta(call)))
            except Exception as exc:
                _log_warn("bde_capture_getcall_failed", call_id=cid, error=str(exc))
        for cid, d9, meta in fetched:      # each in its own tiny txn so one bad row can't abort the batch
            try:
                with pool.connection() as conn, conn.cursor() as cur:
                    upsert_aircall_meta(cur, cid, d9, meta)
                    conn.commit()
            except Exception:
                pass
        if to_transcribe:
            try:
                from .aircall.transcribe import transcribe_calls
                transcribe_calls(client, pool, settings, to_transcribe, limit=3)
            except Exception as exc:
                _log_warn("bde_capture_prompt_transcribe_failed", error=str(exc))
    finally:
        try:
            client.close()
        except Exception:
            pass


def _capture_aircall_comments(pool, settings, oai, model: str, closers: list[str], limit: int, out: dict) -> None:
    """NOTES-FIRST capture: map Alfred's TYPED Aircall comment/tags/contact onto the booking IMMEDIATELY —
    no STT wait. Per active booking's most-recent uncaptured comment/tag/contact set: use the linked contact
    for identity (authoritative), classify the free-text note (if any) for next_action/meeting/summary, map
    tags -> outcome/stage, then FILL BLANKS via crm.crm_update, APPEND the note verbatim, and ADVANCE the
    stage (answered call OR a confirm/reveal/won tag) without regressing. Idempotent on the comment+tag
    signature (pushed into the query), so a NEW comment on an already-seen call is re-captured. Guarded."""
    from .crm import crm_update
    rows = _fetch(pool, """
      WITH booked AS (
        SELECT DISTINCT ON (dest9) dest9, company_name, prospect_name, prospect_email, created_at
        FROM lisa_calls WHERE COALESCE(meeting_agreed,false) AND dest9 IS NOT NULL
        ORDER BY dest9, created_at ASC),
      cand AS (
        SELECT b.dest9, b.company_name, b.prospect_name, b.prospect_email,
               m.call_id, m.comments, m.tags,
               m.contact_name AS m_name, m.contact_email AS m_email, m.contact_company AS m_company,
               c.started_at, c.answered,
               bc.contact_name AS bc_name, bc.contact_email AS bc_email,
               bc.next_action AS bc_next, bc.next_action_at AS bc_next_at, bc.outcome AS bc_outcome,
               row_number() OVER (PARTITION BY b.dest9 ORDER BY c.started_at DESC) AS rn
        FROM booked b
        JOIN aircall_call_meta m ON m.dest9 = b.dest9
        JOIN calls c ON c.call_id = m.call_id AND c.bde_name = ANY(%(closers)s)
        LEFT JOIN booked_crm bc ON bc.dest9 = b.dest9
        WHERE COALESCE(bc.stage,'') NOT IN ('lost','won','no_show')
          AND ( COALESCE(m.comments,'[]') NOT IN ('[]','')
             OR COALESCE(m.tags,'[]')     NOT IN ('[]','')
             OR COALESCE(m.contact_name,'')  <> ''
             OR COALESCE(m.contact_email,'') <> '' )
          AND NOT EXISTS (SELECT 1 FROM bde_capture_seen s
                          WHERE s.call_id = 'cmt:'||m.call_id||':'
                                || left(md5(COALESCE(m.comments,'')||'|'||COALESCE(m.tags,'')),8)))
      SELECT * FROM cand WHERE rn=1 ORDER BY started_at DESC LIMIT %(limit)s
    """, {"closers": closers or ["__none__"], "limit": int(limit)})
    for r in rows or []:
        out["seen"] += 1
        d9 = r["dest9"]
        cid = r["call_id"]
        comments = _parse_json_list(r.get("comments"))
        tags = [t for t in _parse_json_list(r.get("tags")) if isinstance(t, str)]
        comment_text = "\n".join(c.get("content", "") for c in comments
                                 if isinstance(c, dict) and c.get("content")).strip()
        key = "cmt:" + str(cid) + ":" + _sig(r.get("comments"), r.get("tags"))
        # Classify the free-text note only (cheap + fast); a tag/contact-only signal needs no OpenAI call.
        res: dict = {}
        if comment_text:
            try:
                res = _classify_transcript(oai, model, comment_text) or {}
            except Exception as exc:
                _log_warn("bde_capture_comment_classify_failed", call_id=cid, error=str(exc))
                res = {}
        res = dict(res)
        # The linked Aircall contact is authoritative identity — prefer it over anything parsed from free text.
        if _clean(r.get("m_name")):
            res["contact_name"] = r.get("m_name")
        if _clean(r.get("m_email")):
            res["email"] = r.get("m_email")
        if _clean(r.get("m_company")):
            res["company"] = r.get("m_company")
        tstage, toutcome = _tag_signal(tags)
        if toutcome and not _clean(res.get("outcome")):
            res["outcome"] = toutcome
        fields, outcome, mdt = _merge_fields(
            res,
            cur_name=_clean(r.get("bc_name")) or _clean(r.get("prospect_name")),
            cur_email=_clean(r.get("bc_email")) or _clean(r.get("prospect_email")),
            cur_company=_clean(r.get("company_name")),
            bc_next=r.get("bc_next"), bc_outcome=r.get("bc_outcome"),
            booked_at=r.get("started_at"), tz=getattr(settings, "tz", "Australia/Melbourne"),
            cur_next_at=r.get("bc_next_at"))
        if fields:
            try:
                crm_update(pool, d9, fields, who="aircall-capture")
                out["updated"] += 1
            except Exception as exc:
                _log_warn("bde_capture_comment_update_failed", call_id=cid, error=str(exc))
        # RESCHEDULE FIX: a reschedule/callback moves the ACTUAL meeting — re-time the live
        # calendar row + next_action_at, not just the next_action text (guarded, idempotent).
        _apply_reschedule(pool, settings, d9, outcome=outcome,
                          phrase=(mdt or _clean(res.get("next_action"))),
                          booked_at=r.get("started_at"), who="aircall-capture", out=out)
        # Alfred's exact words (or, if only tags were set, a tag line) into the timeline.
        if comment_text:
            _append_verbatim(pool, d9, comment_text, comments, cid)
        elif tags:
            _append_note(pool, d9, "Alfred tagged the call", "aircall-capture",
                         ", ".join(tags), outcome, mdt, {"call_id": cid, "tags": tags})
        # Stage: an ANSWERED confirmation call and/or a confirm/reveal/won tag — take the later of the two.
        target = "confirmed" if r.get("answered") else None
        if tstage and _STAGE_RANK.get(tstage, -1) > _STAGE_RANK.get(target or "", -1):
            target = tstage
        if _advance_stage(pool, d9, target, "aircall-capture"):
            out["updated"] += 1
        _mark_seen(pool, key, d9)
        out["captured"] += 1


# --------------------------------------------------------------------------- #
# Fireflies path — additive, reuses the ALREADY-matched fireflies_meetings rows
# --------------------------------------------------------------------------- #
def _capture_fireflies(pool, oai, model: str, limit: int, out: dict) -> None:
    """Map already-matched Fireflies meeting transcripts onto booked_crm with the SAME fill-blank/append
    merge. We READ the fireflies_meetings table (populated by the fully-isolated run_fireflies_watch — we
    never touch that watcher) and only WRITE fill-blank booked_crm fields + an append-only note. Fully
    guarded: a Fireflies hiccup can't affect the Aircall capture or the loop. No-ops if the table is absent."""
    try:
        reg = _fetch(pool, "SELECT to_regclass('public.fireflies_meetings') AS t")
        if not reg or not reg[0].get("t"):
            return
        from .crm import crm_update
        rows = _fetch(pool, """
          SELECT fm.id, fm.dest9, fm.title, fm.summary, fm.transcript,
                 bc.contact_name AS bc_name, bc.contact_email AS bc_email,
                 bc.next_action AS bc_next, bc.outcome AS bc_outcome,
                 (SELECT company_name FROM lisa_calls WHERE dest9=fm.dest9 AND company_name IS NOT NULL
                    ORDER BY created_at DESC LIMIT 1) AS company_name,
                 (SELECT prospect_name FROM lisa_calls WHERE dest9=fm.dest9 AND prospect_name IS NOT NULL
                    ORDER BY created_at DESC LIMIT 1) AS prospect_name,
                 (SELECT prospect_email FROM lisa_calls WHERE dest9=fm.dest9 AND prospect_email IS NOT NULL
                    ORDER BY created_at DESC LIMIT 1) AS prospect_email
          FROM fireflies_meetings fm
          LEFT JOIN booked_crm bc ON bc.dest9=fm.dest9
          WHERE fm.dest9 IS NOT NULL
            AND COALESCE(bc.stage,'') NOT IN ('lost','won','no_show')
            AND EXISTS (SELECT 1 FROM lisa_calls lc WHERE lc.dest9=fm.dest9 AND COALESCE(lc.meeting_agreed,false))
            AND NOT EXISTS (SELECT 1 FROM bde_capture_seen s WHERE s.call_id='ff:'||fm.id)
            AND length(COALESCE(fm.transcript,'') || COALESCE(fm.summary,'')) > 120
          ORDER BY fm.meeting_date DESC NULLS LAST
          LIMIT %(limit)s
        """, {"limit": int(limit)})
        for r in rows or []:
            d9 = r["dest9"]
            key = "ff:" + str(r["id"])
            src_text = ((r.get("transcript") or "") or (r.get("summary") or ""))
            try:
                res = _classify_transcript(oai, model, src_text)
            except Exception as exc:
                _log_warn("bde_capture_ff_classify_failed", id=str(r.get("id")), error=str(exc))
                _mark_seen(pool, key, d9)                          # don't re-pay for an unparseable transcript
                continue
            fields, outcome, mdt = _merge_fields(
                res,
                cur_name=_clean(r.get("bc_name")) or _clean(r.get("prospect_name")),
                cur_email=_clean(r.get("bc_email")) or _clean(r.get("prospect_email")),
                cur_company=_clean(r.get("company_name")),
                bc_next=r.get("bc_next"), bc_outcome=r.get("bc_outcome"))
            if fields:
                try:
                    crm_update(pool, d9, fields, who="fireflies-capture")
                    out["updated"] += 1
                except Exception as exc:
                    _log_warn("bde_capture_ff_update_failed", id=str(r.get("id")), error=str(exc))
            _append_note(pool, d9, "Fireflies meeting captured", "fireflies-capture",
                         _clean(res.get("summary")), outcome, mdt, {"fireflies_id": r.get("id")})
            # A recorded reveal MEETING happened → advance to 'revealed' (never regressing a later stage).
            if _advance_stage(pool, d9, "revealed", "fireflies-capture"):
                out["updated"] += 1
            _mark_seen(pool, key, d9)
            out["captured"] += 1
    except Exception as exc:
        _log_warn("bde_capture_fireflies_failed", error=str(exc))


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def run_bde_call_capture(pool, settings, limit: int = 6) -> dict:
    """Map each NEW closer confirmation-call ACTION onto its booked prospect's CRM — reliably and fast.

    Per ACTIVE booking (a dest9 with meeting_agreed, stage NOT in lost/won/no_show), in priority order:
      (a) Alfred's TYPED Aircall comment/tags + the linked contact — the IMMEDIATE signal (no STT wait);
      (b) the STT transcript of his call, as a fallback/enrichment.
    Both FILL BLANKS via crm.crm_update (identity flows to lisa_calls + booked_crm), APPEND to the timeline
    (his note verbatim), and ADVANCE the pipeline stage without regressing (answered call / confirm tag →
    'confirmed'; Fireflies reveal meeting → 'revealed'). To beat transcription lag, the pass first RE-FETCHES
    recent closer calls from Aircall (fresh comments) and prompt-transcribes any recording still missing a
    transcript. Self-throttled to ~5 min, fully guarded. Returns {seen, captured, updated}."""
    out = {"seen": 0, "captured": 0, "updated": 0}
    try:
        from .crm import ensure_crm_tables, crm_update
        ensure_crm_tables(pool)
        ensure_bde_capture_tables(pool)
        try:
            from .aircall.ingest import ensure_aircall_meta_table
            ensure_aircall_meta_table(pool)
        except Exception:
            pass
        # Cost/API gate: the loop calls us every tick, but we do real work at most ~every 5 min.
        if not _throttle_claim(pool, 5):
            out["skipped"] = "throttled"
            return out
        closers = _closers(settings)
        # 0) LAG FIX — refresh Alfred's Aircall notes/tags/contact for active bookings + prompt-transcribe any
        #    recent recording still missing a transcript, so his action reflects THIS pass (guarded).
        try:
            _refresh_aircall_meta_for_bookings(pool, settings, closers, limit=15)
        except Exception as exc:
            _log_warn("bde_capture_meta_refresh_failed", error=str(exc))
        # Shared cheap-model client for every path this pass.
        from .transcribe import _openai
        oai = _openai(settings)
        model = getattr(settings, "llm_model_cheap", "") or "gpt-4o-mini"
        # 1) NOTES-FIRST — Alfred's typed comment/tags/contact (immediate; no STT).
        try:
            _capture_aircall_comments(pool, settings, oai, model, closers, limit, out)
        except Exception as exc:
            _log_warn("bde_capture_comments_failed", error=str(exc))
        # 2) TRANSCRIPT FALLBACK/ENRICHMENT — most-recent UNCAPTURED aircall closer call with a transcript per
        #    active booking (rn=1 = newest not-yet-processed). Fills whatever the note left blank.
        rows = _fetch(pool, """
          WITH booked AS (
            SELECT DISTINCT ON (dest9) dest9, company_name, prospect_name, prospect_email, created_at
            FROM lisa_calls WHERE COALESCE(meeting_agreed,false) AND dest9 IS NOT NULL
            ORDER BY dest9, created_at ASC),
          cand AS (
            SELECT b.dest9, b.company_name, b.prospect_name, b.prospect_email,
                   c.call_id, c.started_at, c.bde_name, c.answered, t.text AS transcript,
                   bc.contact_name AS bc_name, bc.contact_email AS bc_email,
                   bc.next_action AS bc_next, bc.next_action_at AS bc_next_at, bc.outcome AS bc_outcome,
                   row_number() OVER (PARTITION BY b.dest9 ORDER BY c.started_at DESC) AS rn
            FROM booked b
            JOIN calls c ON c.provider='aircall' AND c.has_transcript
                 AND right(regexp_replace(COALESCE(c.dest_number,''),'[^0-9]','','g'),9)=b.dest9
                 AND c.bde_name = ANY(%(closers)s)
            JOIN transcripts t ON t.call_id=c.call_id AND length(COALESCE(t.text,'')) > 200
            LEFT JOIN booked_crm bc ON bc.dest9=b.dest9
            WHERE COALESCE(bc.stage,'') NOT IN ('lost','won','no_show')
              AND NOT EXISTS (SELECT 1 FROM bde_capture_seen s WHERE s.call_id=c.call_id))
          SELECT * FROM cand WHERE rn=1 ORDER BY started_at DESC LIMIT %(limit)s
        """, {"closers": closers, "limit": int(limit)})
        for r in rows or []:
            out["seen"] += 1
            d9 = r["dest9"]
            cid = r["call_id"]
            try:
                res = _classify_transcript(oai, model, r.get("transcript") or "")
            except Exception as exc:
                _log_warn("bde_capture_classify_failed", call_id=cid, error=str(exc))
                _mark_seen(pool, cid, d9)                       # don't re-pay for an unparseable transcript
                continue
            fields, outcome, mdt = _merge_fields(
                res,
                cur_name=_clean(r.get("bc_name")) or _clean(r.get("prospect_name")),
                cur_email=_clean(r.get("bc_email")) or _clean(r.get("prospect_email")),
                cur_company=_clean(r.get("company_name")),
                bc_next=r.get("bc_next"), bc_outcome=r.get("bc_outcome"),
                booked_at=r.get("started_at"), tz=getattr(settings, "tz", "Australia/Melbourne"),
                cur_next_at=r.get("bc_next_at"))
            if fields:
                try:
                    crm_update(pool, d9, fields, who="aircall-capture")
                    out["updated"] += 1
                except Exception as exc:
                    _log_warn("bde_capture_update_failed", call_id=cid, error=str(exc))
            # RESCHEDULE FIX: same as the notes path — a reschedule/callback moves the meeting, so
            # re-time the live calendar row + next_action_at (guarded, idempotent).
            _apply_reschedule(pool, settings, d9, outcome=outcome,
                              phrase=(mdt or _clean(res.get("next_action"))),
                              booked_at=r.get("started_at"), who="aircall-capture", out=out)
            _append_note(pool, d9, "Alfred call captured", "aircall-capture",
                         _clean(res.get("summary")), outcome, mdt, {"call_id": cid})
            # An ANSWERED closer call is a confirmation → advance to 'confirmed' (never regressing).
            if r.get("answered") and _advance_stage(pool, d9, "confirmed", "aircall-capture"):
                out["updated"] += 1
            _mark_seen(pool, cid, d9)                           # processed exactly once
            out["captured"] += 1
        # 3) FIREFLIES — matched reveal-meeting transcripts (same fill-blank/append + stage 'revealed').
        try:
            _capture_fireflies(pool, oai, model, max(1, min(3, limit)), out)
        except Exception as exc:
            _log_warn("bde_capture_fireflies_only_failed", error=str(exc))
        return out
    except Exception as exc:
        _log_warn("bde_capture_failed", error=str(exc))
        out["error"] = "guarded"
        return out
