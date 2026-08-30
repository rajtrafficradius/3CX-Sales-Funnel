"""Lisa 5 — a second isolated cold-caller subsystem, CLONED from Lisa-4's dialer.

This module is a faithful copy of Lisa-4's calling machinery (run_lisa4_autodial -> run_lisa5_autodial)
on its OWN pool (lisa5_pool), OWN Retell agent (lisa5_agent_id), OWN caller-ID rotation pool
(lisa5_from_numbers) and OWN calendar identity (bde_name='Lisa5' / extension 'LISA5'). It shares only the
generic helpers in lisa.py (E.164 normalisation, Retell client, Twilio SMS, funnel mirror) and the shared
lisa_calls / calendar_events tables — every read/write is filtered by Lisa-5's own numbers or bde_name, so
the two agents never touch each other's state.

What was intentionally DROPPED from the Lisa-4 clone (Lisa-5 is a plain dialer, not the website-selling
subsystem): the AI website designer (build_website), the lisa4_sites pipeline, the website-audit pool
sourcing (reserve_lisa4_pool). Lisa-5's pool is fed externally — this module only reads lisa5_pool.

Autodial is OFF by default (lisa5_autodial_enabled=False AND the DB toggle unset) — enable only after verify.
"""

from __future__ import annotations

import re as _re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from psycopg_pool import ConnectionPool

from .config import Settings
from .logging import get_logger
from .qa import dynvars as _qa_dyn   # G8/G9/G10 pure in-memory dynamic-variable safety

log = get_logger(__name__)

_BDE = "Lisa5"   # Lisa 5's calendar owner tag — never collides with Lisa-1's 'Lisa' or Lisa-4's 'Lisa4'

# LINE ATTRIBUTION (mirrors Lisa-4's L4_LINE_RX): a lisa_calls row belongs to Lisa-5 iff EITHER leg
# (from_number when she dials out, to_number when a prospect calls her back) contains a Lisa-5 line's
# digits. APPEND-ONLY registry — attribution over history must NOT depend on the current
# LISA5_FROM_NUMBERS rotation (a rested number's historical + inbound rows are still Lisa-5's). Add, never
# remove. With L4_LINE_RX these two registries + "everything else = Lisa-1" exactly partition lisa_calls,
# so floor_today = the exact sum of the three rep cards (no row double-counted or dropped).
L5_LINE_DIGITS_ALL = ("468096730", "468008827")
L5_LINE_RX = "(" + "|".join(L5_LINE_DIGITS_ALL) + ")"
_L5_PRED = ("(COALESCE(from_number,'') ~ %s "
            "OR COALESCE(to_number,'') ~ %s)")


def _fetch(pool: ConnectionPool, sql: str, params=None) -> list[dict]:
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(sql, params or ())
        return cur.fetchall()


# lisa5_pool.issue doubles as an internal state/sourcing marker on some rows ('dead number' /
# 'wrong number - unreachable' / 'opt-out (SMS)' from post-call cleanup). Those are bookkeeping, not a
# speakable finding — if one leaks into the brief the agent reads it out VERBATIM. Blank them.
_INTERNAL_ISSUES = {"mobile-reachable owner", "dead number", "wrong number - unreachable",
                    "wrong number", "opt-out (sms)", "opt-out"}


def _speakable_issue(issue: str | None) -> str:
    s = (issue or "").strip()
    return "" if s.lower() in _INTERNAL_ISSUES else s


def ensure_lisa5_tables(pool: ConnectionPool) -> None:
    """Lisa-5's OWN prospect pool. Mirrors lisa4_pool's columns so build_brief_lisa5 works identically;
    fed externally (this module never sources it). Isolated from Lisa-1 / Lisa-4 tables."""
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "CREATE TABLE IF NOT EXISTS lisa5_pool ("
            "  dest9 text PRIMARY KEY, dest_number text, company text, domain text,"
            "  bucket text, issue text, title text, email text, priority integer DEFAULT 0,"
            "  sms_sent boolean DEFAULT false, reserved_at timestamptz DEFAULT now())")
        cur.execute("ALTER TABLE lisa5_pool ADD COLUMN IF NOT EXISTS title text")
        cur.execute("ALTER TABLE lisa5_pool ADD COLUMN IF NOT EXISTS email text")
        cur.execute("ALTER TABLE lisa5_pool ADD COLUMN IF NOT EXISTS sms_sent boolean DEFAULT false")
        conn.commit()


# --------------------------------------------------------------------------- #
# Autopilot — control toggle, dial path, scheduler, dial loop (all ISOLATED from Lisa-1 / Lisa-4)
# --------------------------------------------------------------------------- #
def ensure_lisa5_control(pool: ConnectionPool) -> None:
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute("CREATE TABLE IF NOT EXISTS lisa5_control ("
                    "  id integer PRIMARY KEY, autodial boolean, updated_at timestamptz DEFAULT now(),"
                    "  updated_by text, heavy_at timestamptz)")
        # Lisa 5 = a real BDE on the main funnel (calls.bde_extension FK + leaderboard identity).
        # Re-asserted each cycle so a 3CX roster-sync can't drop her (she isn't a 3CX agent).
        cur.execute(
            "INSERT INTO bde_agents (extension, bde_name, email, group_name, role_name, in_scope, active, "
            "  synced_at) VALUES ('LISA5','Lisa 5','lisa5@trafficradius.com.au','AI','AI BDE',true,true,now()) "
            "ON CONFLICT (extension) DO UPDATE SET bde_name='Lisa 5', in_scope=true, active=true, "
            "  role_name='AI BDE'")
        conn.commit()


def get_lisa5_autodial(pool: ConnectionPool, settings: Settings) -> bool:
    """DB toggle wins once set; else the env default (lisa5_autodial_enabled, default OFF)."""
    ensure_lisa5_control(pool)
    r = _fetch(pool, "SELECT autodial FROM lisa5_control WHERE id=1")
    if r and r[0].get("autodial") is not None:
        return bool(r[0]["autodial"])
    return bool(getattr(settings, "lisa5_autodial_enabled", False))


def set_lisa5_autodial(pool: ConnectionPool, on: bool, by: str = "console") -> None:
    ensure_lisa5_control(pool)
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute("INSERT INTO lisa5_control (id, autodial, updated_at, updated_by) VALUES (1,%s,now(),%s) "
                    "ON CONFLICT (id) DO UPDATE SET autodial=EXCLUDED.autodial, updated_at=now(), "
                    "updated_by=EXCLUDED.updated_by", (on, by))
        conn.commit()


def _rotate_from_number(candidates: list[str], today_counts: dict[str, int], cap: int) -> str | None:
    """Pick the next caller ID from the rotation pool: least-used today first (random tie-break), and
    NEVER a number already at its per-day cap. Pure function so the rotation is dry-run testable.
    Returns None when every number is maxed (the day is over for outbound on this pool)."""
    import random
    eligible = [n for n in candidates if today_counts.get(n, 0) < max(1, cap)]
    if not eligible:
        return None
    low = min(today_counts.get(n, 0) for n in eligible)
    return random.choice([n for n in eligible if today_counts.get(n, 0) == low])


def _pick_lisa5_from(pool: ConnectionPool, settings: Settings) -> tuple[str | None, str]:
    """Resolve today's outbound caller ID: normalise LISA5_FROM_NUMBERS, count today's lisa_calls per
    from_number (settings.tz day) and rotate via _rotate_from_number honoring the ~150/day cap.
    Returns (number, "") on success or (None, reason)."""
    from . import lisa as _L1
    candidates: list[str] = []
    for n in list(getattr(settings, "lisa5_numbers", []) or []):
        e = _L1._e164_au(n)
        if e and e not in _L1._BLOCKED_FROM and e not in candidates:
            candidates.append(e)
    if not candidates:
        return None, "no valid LISA5_FROM_NUMBERS"
    tz = settings.tz
    rows = _fetch(pool,
                  "SELECT from_number f, count(*) n FROM lisa_calls WHERE from_number = ANY(%s) "
                  "AND (created_at AT TIME ZONE %s)::date=(now() AT TIME ZONE %s)::date GROUP BY from_number",
                  (candidates, tz, tz))
    counts = {r["f"]: int(r["n"]) for r in rows}
    frm = _rotate_from_number(candidates, counts, int(getattr(settings, "lisa5_per_number_daily_cap", 150)))
    return (frm, "") if frm else (None, "all LISA5_FROM_NUMBERS at daily cap")


def build_brief_lisa5(pool: ConnectionPool, dest9: str) -> dict:
    """Dynamic variables for the Lisa 5 agent — the story she tells (company + bucket + issue). Uses the
    real trading name from lisa5_pool (never a legal/partnership name). Shares Lisa-1's lisa_dm store for a
    resolved decision-maker first name + direct email when available."""
    r = _fetch(pool, "SELECT company, domain, bucket, issue, title, email FROM lisa5_pool WHERE dest9=%s", (dest9,))
    d = dict(r[0]) if r else {}
    dm = {}
    if d.get("domain"):
        dr = _fetch(pool, "SELECT dm_first, dm_name, dm_email, dm_title, trading_name, apollo_extra "
                    "FROM lisa_dm WHERE domain=%s AND source IS DISTINCT FROM 'too_big'", (d.get("domain"),))
        dm = dict(dr[0]) if dr else {}
    pn = (dm.get("dm_first") or "").strip()
    ex = dm.get("apollo_extra") or {}
    # compact company context Lisa can weave in for credibility ("you've run [co], the [industry] business…")
    ctx = " · ".join(x for x in [ex.get("industry"),
                                 (f"{ex.get('employees')} staff" if ex.get("employees") else None),
                                 (f"since {ex.get('founded_year')}" if ex.get("founded_year") else None)] if x)
    return {
        "company_name": (dm.get("trading_name") or d.get("title") or d.get("company") or "").strip(),
        "company_domain": (d.get("domain") or "").strip().lower().removeprefix("www."),
        "website_bucket": d.get("bucket") or "",
        "website_issue": _speakable_issue(d.get("issue")),   # never an internal state marker on the call
        "prospect_email": dm.get("dm_email") or d.get("email") or "",  # she CONFIRMS it, never asks cold
        "prospect_name": _qa_dyn.clean_prospect_name(pn),   # G10: clean spoken first name, or "" for junk
        "prospect_role": (dm.get("dm_title") or ex.get("role") or "").strip(),  # e.g. "Managing Director"
        "company_context": ctx,                              # e.g. "retail · 8 staff · since 1999"
    }


def start_lisa5_call(pool: ConnectionPool, settings: Settings, *, to_number: str, dest9: str,
                     extra_vars: dict | None = None, allow_landline: bool = False) -> dict:
    """Place ONE Lisa-5 call: build the brief, create the Retell call on the Lisa-5 agent from Lisa-5's own
    number, log a pending lisa_calls row (from_number distinguishes it from Lisa-1 / Lisa-4). The caller ID
    ROTATES across the LISA5_FROM_NUMBERS pool with a per-number daily cap."""
    from . import lisa as _L1                      # reuse the shared helpers (never Lisa-1/4's dial state)
    if not getattr(settings, "lisa_enabled", False):
        return {"error": "lisa disabled"}
    frm, why = _pick_lisa5_from(pool, settings)
    if not frm:
        return {"error": why}
    to_number = _L1._e164_au(to_number)
    if len(_L1.re.sub(r"[^0-9]", "", to_number)) < 8:
        return {"error": f"invalid phone: {to_number}"}
    # MOBILE-FIRST HARD GATE: Lisa-5 dials AU mobiles ONLY. Final belt — whatever path reaches here
    # (pool / calendar event / DM override), a non-mobile is refused so a landline can never be dialed.
    if not allow_landline and _L1.re.sub(r"[^0-9]", "", to_number)[-9:][:1] != "4":
        return {"error": "skipped non-mobile (mobile-first)"}
    d9 = _L1._d9(dest9 or to_number)
    brief = build_brief_lisa5(pool, d9)
    dyn = {k: ("" if v is None else str(v)) for k, v in brief.items()}
    # she can't hear what she dialed — tell her, so she never asks "is this the best number?" on the very
    # number that just answered (a dead giveaway on a mobile)
    dyn["phone_is_mobile"] = "yes" if to_number.startswith("+614") else "no"
    for k, v in (extra_vars or {}).items():
        dyn[k] = "" if v is None else str(v)
    # final belt: an extra_vars/event override can re-introduce an internal state marker — never speakable
    dyn["website_issue"] = _speakable_issue(dyn.get("website_issue"))
    # QA G8/G9 (PURE in-memory — dict/regex only, ZERO I/O): fill any known blank/missing variable so
    # Retell can never voice a raw "{{placeholder}}", scrub any residual {{...}} inside a value, then
    # enforce the no-website invariant (a site-less prospect never carries a website URL/domain). This
    # only shapes the variables dict handed to Retell — it never changes how the call is placed/paced.
    dyn = _L1._fill_dynamic_var_defaults(dyn)
    dyn = _qa_dyn.scrub_residual_placeholders(dyn)
    dyn = _qa_dyn.enforce_no_website_invariant(dyn)
    body = {
        "from_number": frm, "to_number": to_number,
        "override_agent_id": getattr(settings, "lisa5_agent_id", "") or None,
        "retell_llm_dynamic_variables": dyn,
        "metadata": {"dest9": d9, "lisa5": "true", "bucket": brief.get("website_bucket", "")},
    }
    try:
        r = _L1._retell(settings, "POST", "v2/create-phone-call", body)
    except Exception as exc:
        log.warning("lisa5_start_call_failed", to=to_number, error=str(exc)[:200])
        return {"error": str(exc)[:200]}
    cid = r.get("call_id")
    if cid:
        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO lisa_calls (call_id, dest9, to_number, from_number, company_name, domain, "
                "  status, brief) VALUES (%s,%s,%s,%s,%s,%s,'ongoing',%s) ON CONFLICT (call_id) DO NOTHING",
                (cid, d9, to_number, frm, brief.get("company_name"), None, _L1.json.dumps(brief)))
            conn.commit()
    return r


def schedule_lisa5_fresh(pool: ConnectionPool, settings: Settings) -> dict:
    """Put reserved Lisa-5 prospects on the Lisa5 calendar (bde_name='Lisa5') as fresh_call events, filling
    up to lisa5_daily_target/day, staggered across the window. Idempotent. MOBILE-FIRST: only AU mobiles."""
    ensure_lisa5_tables(pool)
    tz = settings.tz
    cap = int(getattr(settings, "lisa5_daily_target", 200))
    wstart = int(getattr(settings, "lisa_call_window_start", 9))
    wsmin = int(getattr(settings, "lisa_call_window_start_min", 0))
    wend = int(getattr(settings, "lisa_call_window_end", 17))
    rows = _fetch(pool,
        "SELECT lp.dest9, lp.dest_number, lp.company, lp.domain FROM lisa5_pool lp "
        "WHERE NOT EXISTS (SELECT 1 FROM calendar_events e WHERE e.bde_name=%s AND e.status IN ('pending','done') "
        "   AND right(regexp_replace(COALESCE(e.dest_number,''),'[^0-9]','','g'),9)=lp.dest9) "
        "  AND NOT EXISTS (SELECT 1 FROM lisa_calls lc WHERE lc.dest9=lp.dest9) "
        # MOBILE-FIRST: only ever schedule AU mobiles (last-9 starts with 4).
        "  AND left(lp.dest9,1) = '4' "
        "ORDER BY COALESCE(lp.priority,0) DESC, (CASE WHEN lp.bucket='critical_issue' THEN 0 "
        "  WHEN lp.bucket='upgrade' THEN 1 ELSE 2 END), lp.reserved_at", (_BDE,))
    existing = {r["d"]: r["n"] for r in _fetch(pool,
        "SELECT (start_at AT TIME ZONE %s)::date d, count(*) n FROM calendar_events "
        "WHERE bde_name=%s AND status='pending' AND type='fresh_call' GROUP BY 1", (tz, _BDE))}
    now = datetime.now(ZoneInfo(tz))
    day = now.date() + (timedelta(days=1) if now.hour >= wend else timedelta(0))
    open_min = wstart * 60 + wsmin
    span_min = max(1, wend * 60 - open_min)
    to_insert = []
    for r in rows:
        while day.weekday() >= 5 or existing.get(day, 0) >= cap:
            day += timedelta(days=1)
        used = existing.get(day, 0)
        t = min(open_min + int(used * span_min / max(1, cap)), wend * 60 - 1)
        when = datetime(day.year, day.month, day.day, t // 60, t % 60, tzinfo=ZoneInfo(tz))
        existing[day] = used + 1
        to_insert.append((_BDE, "fresh_call", f"📞 Lisa 5: {r['company'] or r['dest_number']}",
                          when, when + timedelta(minutes=15), r["dest_number"]))
    scheduled = 0
    if to_insert:
        # ON CONFLICT on the GLOBAL partial-unique index (one pending fresh_call per dest9 across ALL
        # bde_names — Lisa-1 / Lisa-4 included), so one d9 collision can't abort the whole batch.
        _d9expr = "right(regexp_replace(COALESCE(dest_number,''),'[^0-9]','','g'),9)"
        with pool.connection() as conn, conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO calendar_events (bde_name, type, title, start_at, end_at, dest_number, "
                "  created_by, status) VALUES (%s,%s,%s,%s,%s,%s,'lisa5','pending') "
                "ON CONFLICT (" + _d9expr + ") WHERE type='fresh_call' AND status='pending' DO NOTHING",
                to_insert)
            scheduled = cur.rowcount
            conn.commit()
    return {"scheduled": scheduled, "candidates": len(rows)}


def run_lisa5_head(pool: ConnectionPool, settings: Settings) -> dict:
    """Schedule prep for Lisa 5 (throttled ~every 3 min). Isolated from Lisa-1/4. Lisa-5's pool is fed
    externally, so — unlike Lisa-4 — there is no reserve/website-audit step here, only scheduling."""
    ensure_lisa5_control(pool)
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT (heavy_at IS NULL OR heavy_at < now() - make_interval(secs => 180)) go "
                    "FROM lisa5_control WHERE id=1")
        row = cur.fetchone()
        do = bool(row["go"]) if row else True
        if do and row is not None:
            cur.execute("UPDATE lisa5_control SET heavy_at=now() WHERE id=1"); conn.commit()
    if not do:
        return {"skipped": "throttled"}
    out = {}
    try:
        out["scheduled"] = schedule_lisa5_fresh(pool, settings).get("scheduled", 0)
    except Exception as exc:
        log.warning("lisa5_schedule_failed", error=str(exc)[:140])
    return out


def run_lisa5_autodial(pool: ConnectionPool, settings: Settings) -> dict:
    """GATED Lisa-5 dialer — one call at a time, pipeline-paced, within the window. Mirrors Lisa-4's
    autodial but on the Lisa5 calendar + agent + number pool. No call fires unless the Lisa5 toggle is on."""
    from . import lisa as _L1
    ensure_lisa5_tables(pool)
    l5_numbers = list(getattr(settings, "lisa5_numbers", []) or [])
    # SCHEDULED SMS (type='sms' on the Lisa5 calendar): send `notes` to dest_number once start_at is due.
    # Runs ahead of the dialer gates so a queued text fires even if the dial toggle is off. Transient
    # failures retry each tick for 30 minutes past start_at, then the event is cancelled. Each event is
    # handled in its OWN try/except — one provider error must never block the rest of the queue.
    for s in _fetch(pool, "SELECT id, dest_number, notes, start_at, created_at FROM calendar_events "
                    "WHERE bde_name=%s AND status='pending' AND type='sms' AND start_at <= now() "
                    "LIMIT 3", (_BDE,)):
        try:
            # SELF-STOP: if the prospect texted or called us since this event was queued, cancel the
            # automated touch instead of talking over a live conversation.
            d9g = _re.sub(r"[^0-9]", "", s.get("dest_number") or "")[-9:]
            replied = _fetch(pool,
                "SELECT 1 FROM lisa_sms WHERE dest9=%s AND direction='inbound' AND created_at > %s "
                "UNION ALL "
                "SELECT 1 FROM lisa_calls WHERE dest9=%s AND from_number LIKE '%%'||%s AND created_at > %s "
                "LIMIT 1", (d9g, s["created_at"], d9g, d9g, s["created_at"]))
            if replied:
                with pool.connection() as conn, conn.cursor() as cur:
                    cur.execute("UPDATE calendar_events SET status='cancelled', "
                                "notes=COALESCE(notes,'')||' · auto-cancelled: prospect responded' WHERE id=%s", (s["id"],))
                    conn.commit()
                log.info("lisa5_scheduled_sms_skipped_responded", event_id=s["id"])
                continue
            frm = (l5_numbers or [""])[0]
            ok = bool(s.get("dest_number") and (s.get("notes") or "").strip() and frm
                      and _L1._twilio_ready(settings)
                      and _L1._send_sms_twilio(settings, s["dest_number"], s["notes"], frm))
        except Exception as exc:
            log.warning("lisa5_scheduled_sms_error", event_id=s.get("id"), error=str(exc)[:140])
            ok = False
        try:
            if ok:
                with pool.connection() as conn, conn.cursor() as cur:
                    cur.execute("UPDATE calendar_events SET status='done' WHERE id=%s", (s["id"],))
                    conn.commit()
                _L1._log_sms(pool, "outbound", frm, s["dest_number"], s["notes"],
                             _re.sub(r"[^0-9]", "", s["dest_number"] or "")[-9:])
                log.info("lisa5_scheduled_sms_sent", to=s["dest_number"], event_id=s["id"])
            else:
                expired = _fetch(pool, "SELECT (now() - start_at) > interval '30 minutes' e "
                                 "FROM calendar_events WHERE id=%s", (s["id"],))
                if expired and expired[0].get("e"):
                    with pool.connection() as conn, conn.cursor() as cur:
                        cur.execute("UPDATE calendar_events SET status='cancelled', "
                                    "notes=COALESCE(notes,'')||' · sms failed >30min' WHERE id=%s", (s["id"],))
                        conn.commit()
                log.warning("lisa5_scheduled_sms_not_sent", event_id=s["id"])
        except Exception as exc:
            log.warning("lisa5_scheduled_sms_error", event_id=s.get("id"), error=str(exc)[:140])
    if not get_lisa5_autodial(pool, settings):
        return {"skipped": "lisa5 autodial off"}
    if not getattr(settings, "lisa_enabled", False):
        return {"skipped": "lisa disabled"}
    # No caller ID yet → skip WITHOUT touching the due queue. (Without this, _pick_lisa5_from would return
    # a hard "no valid LISA5_FROM_NUMBERS" error that the permanent-failure path would treat as fatal and
    # CANCEL every due event — a footgun if the toggle is flipped on before LISA5_FROM_NUMBERS is set.)
    if not l5_numbers:
        return {"skipped": "no lisa5 numbers configured"}
    tz = settings.tz
    now = datetime.now(ZoneInfo(tz))
    wstart = int(getattr(settings, "lisa_call_window_start", 9))
    wsmin = int(getattr(settings, "lisa_call_window_start_min", 0))
    wend = int(getattr(settings, "lisa_call_window_end", 17))
    open_min = wstart * 60 + wsmin
    if now.weekday() >= 5 or not (open_min <= now.hour * 60 + now.minute < wend * 60):
        return {"skipped": "outside window"}
    placed = _fetch(pool, "SELECT count(*) n FROM lisa_calls WHERE from_number = ANY(%s) "
                    "AND (created_at AT TIME ZONE %s)::date=(now() AT TIME ZONE %s)::date",
                    (l5_numbers, tz, tz))[0]["n"]
    if placed >= int(getattr(settings, "lisa5_daily_target", 200)):
        return {"skipped": "daily target reached", "placed": placed}
    # pipeline pacing: only one Lisa5 call in flight at a time
    inflight = _fetch(pool, "SELECT count(*) n FROM lisa_calls WHERE status='ongoing' "
                      "AND from_number = ANY(%s) AND created_at > now() - interval '8 minutes'",
                      (l5_numbers,))[0]["n"]
    if inflight:
        return {"skipped": "call in-flight"}
    floor = int(getattr(settings, "lisa_min_call_gap_seconds", 0)) or 20
    since = _fetch(pool, "SELECT extract(epoch from (now() - max(created_at))) s FROM lisa_calls "
                   "WHERE from_number = ANY(%s) AND (created_at AT TIME ZONE %s)::date=(now() AT TIME ZONE %s)::date",
                   (l5_numbers, tz, tz))[0]["s"]
    if since is not None and since < floor:
        return {"skipped": "floor gap"}
    due = _fetch(pool,
        "SELECT id, dest_number, type, notes, right(regexp_replace(COALESCE(dest_number,''),'[^0-9]','','g'),9) d9 "
        "FROM calendar_events WHERE bde_name=%s AND status='pending' "
        "  AND type IN ('fresh_call','callback','retry') AND start_at <= now() "
        # SELF-DIAL GUARD: never dial a number ANY Lisa calls FROM (bot-to-bot busy-divert bridge)
        "  AND right(regexp_replace(COALESCE(dest_number,''),'[^0-9]','','g'),9) NOT IN "
        "    (SELECT DISTINCT right(regexp_replace(from_number,'[^0-9]','','g'),9) FROM lisa_calls "
        "     WHERE COALESCE(from_number,'')<>'' AND created_at > now() - interval '45 days') "
        # MOBILE-FIRST — drop legacy landline FRESH/retry events; AGREED CALLBACKS are a promise to a
        # prospect who already engaged, so honour them even on a landline.
        "  AND (type='callback' OR left(right(regexp_replace(COALESCE(dest_number,''),'[^0-9]','','g'),9),1) = '4') "
        "  ORDER BY CASE WHEN type='callback' THEN 0 WHEN type='retry' THEN 1 ELSE 2 END, start_at "
        "  LIMIT 6", (_BDE,))
    if not due:
        # SELF-FEEDING QUEUE: nothing due but we're inside the window and under target — promote the next
        # scheduled event instead of idling (the day-spread stagger otherwise starves the dialer).
        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE calendar_events SET start_at=now() WHERE id = ("
                "  SELECT id FROM calendar_events WHERE bde_name=%s AND status='pending' "
                "    AND type='fresh_call' AND start_at > now() ORDER BY start_at LIMIT 1)", (_BDE,))
            conn.commit()
        due = _fetch(pool,
            "SELECT id, dest_number, type, notes, right(regexp_replace(COALESCE(dest_number,''),'[^0-9]','','g'),9) d9 "
            "FROM calendar_events WHERE bde_name=%s AND status='pending' "
            "  AND type IN ('fresh_call','callback','retry') AND start_at <= now() "
            "  AND right(regexp_replace(COALESCE(dest_number,''),'[^0-9]','','g'),9) NOT IN "  # self-dial guard
            "    (SELECT DISTINCT right(regexp_replace(from_number,'[^0-9]','','g'),9) FROM lisa_calls "
            "     WHERE COALESCE(from_number,'')<>'' AND created_at > now() - interval '45 days') "
            "  AND (type='callback' OR left(right(regexp_replace(COALESCE(dest_number,''),'[^0-9]','','g'),9),1) = '4') "  # MOBILE-FIRST (agreed callbacks exempt)
            "  LIMIT 1", (_BDE,))
    for e in due:
        if not e.get("dest_number"):
            continue
        lh, _tz = _L1._prospect_local_hour(pool, e["d9"])
        if not (wstart <= lh < wend):
            continue
        # RPC lever: if Apollo resolved the owner's MOBILE for this prospect, dial that instead of the
        # switchboard (pool identity stays on the event's dest9). Only for FRESH calls; agreed callbacks
        # dial the number the prospect actually agreed on.
        to = e["dest_number"]
        if e.get("type") != "callback":
            dm = _fetch(pool, "SELECT d.dm_phone FROM lisa5_pool lp JOIN lisa_dm d ON d.domain=lp.domain "
                        "WHERE lp.dest9=%s AND COALESCE(d.dm_phone,'')<>'' AND d.dm_is_mobile "
                        "AND d.source IS DISTINCT FROM 'too_big'", (e["d9"],))
            if dm:
                to = dm[0]["dm_phone"]
        extra = {}
        if e.get("type") in ("callback", "retry") and e.get("notes"):
            extra["callback_context"] = str(e["notes"])[:180]
        r = start_lisa5_call(pool, settings, to_number=to, dest9=e["d9"], extra_vars=extra,
                             allow_landline=(e.get("type") == "callback"))
        if r.get("call_id"):
            with pool.connection() as conn, conn.cursor() as cur:
                cur.execute("UPDATE calendar_events SET status='done' WHERE id=%s", (e["id"],))
                conn.commit()
            return {"dialed": 1, "placed": placed + 1}
        # PERMANENT failure → cancel so it never wedges the due queue (transient API blips stay pending)
        err = (r.get("error") or "")
        transient = any(t in err.lower() for t in ("http 5", "timed out", "timeout", "concurrency", "temporarily"))
        if err and not transient:
            with pool.connection() as conn, conn.cursor() as cur:
                cur.execute("UPDATE calendar_events SET status='cancelled', "
                            "notes=COALESCE(notes,'')||' · auto-cancelled: '||%s WHERE id=%s", (err[:120], e["id"]))
                conn.commit()
            log.info("lisa5_autodial_event_cancelled", event_id=e["id"], reason=err[:80])
    return {"dialed": 0, "candidates": len(due)}


def handle_lisa5_postcall(pool: ConnectionPool, settings: Settings, payload: dict) -> dict:
    """Lisa-5's OWN post-call handler (separate webhook) — record the outcome on lisa_calls, mirror it into
    the main funnel as her own BDE (booking classification), and schedule her next move on a non-booked
    call. Kept isolated from Lisa-1/4. (No AI-designer site build — Lisa-5 is a plain dialer.)"""
    ensure_lisa5_tables(pool)
    ensure_lisa5_control(pool)                     # guarantee the LISA5 bde_agents FK before the funnel write
    from . import lisa as _L1
    call = payload.get("call") or payload
    cid = call.get("call_id")
    if not cid:
        return {"ok": False, "error": "no call_id"}
    if (payload.get("event") or "") != "call_analyzed":
        return {"ok": True, "event": payload.get("event")}
    meta = call.get("metadata") or {}
    inbound = (call.get("direction") or "").lower() == "inbound"
    # inbound = a prospect returning Lisa's missed call/SMS: THEY are from_number, our line is to_number
    d9 = _L1._d9(call.get("from_number") if inbound else (meta.get("dest9") or call.get("to_number")))
    if inbound and cid:
        # inbound caller has no brief → recover the company (+domain) from our own data by dest9 so the
        # booking is never a nameless '?' in the CRM / OTHER bucket.
        _co, _dom = _L1.inbound_company(pool, d9, (call.get("retell_llm_dynamic_variables") or {}).get("company_name"))
        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute("INSERT INTO lisa_calls (call_id, dest9, to_number, from_number, company_name, domain, "
                        "  status, brief) VALUES (%s,%s,%s,%s,%s,%s,'ongoing','{}') "
                        "ON CONFLICT (call_id) DO NOTHING",
                        (cid, d9, call.get("to_number"), call.get("from_number"), _co, _dom))
            conn.commit()
    # SOLE SOURCE OF TRUTH = OUR transcript classifier (never Retell's custom_analysis_data). It runs
    # post-call (off the dial loop) and is idempotent per call_id; {} when there is no transcript so the
    # never-connected telephony-only handling below still fires.
    dyn5 = call.get("retell_llm_dynamic_variables") or {}
    cad = _L1._lisa_postcall_cad(pool, settings, call, cid)
    outcome = (cad.get("call_outcome") or "").strip().lower()
    booked = bool(cad.get("meeting_agreed"))
    # QA G1 (webhook handler — off the dial loop): belt-and-suspenders on top of OUR classifier — honour a
    # "booked" only for a genuine two-party conversation (disconnect reason + duration) with a concrete agreed
    # time in the transcript. A gate-fail un-books so the lisa_calls row, the Lisa-5 calendar event, the
    # booking SMS and the funnel mirror all agree. Verdict derives ONLY from the transcript + telephony facts.
    if booked:
        from .qa import gates as _qg, audit as _qa
        _v = _qg.booking_verdict(
            transcript=call.get("transcript"), disconnect_reason=call.get("disconnection_reason"),
            duration_ms=call.get("duration_ms") or call.get("call_length_ms"),
            claimed_time=cad.get("agreed_day_time"))
        if not _v.ok:
            booked = False
            try:
                _qa.log_event(pool, gate="G1", kind="unbooked", call_id=cid, agent="Lisa 5",
                              detail={"reason": _v.reason, "classifier_meeting_agreed": True,
                                      "disconnect": call.get("disconnection_reason"),
                                      "duration_ms": call.get("duration_ms")})
            except Exception:
                pass
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute("UPDATE lisa_calls SET status='analyzed', call_outcome=%s, meeting_agreed=%s, "
                    "  agreed_day_time=%s, call_summary=%s, transcript=%s, recording_url=%s, "
                    "  duration_ms=%s, callback_when=%s, main_objection=%s, asked_if_ai=%s "
                    "WHERE call_id=%s",
                    (outcome or None, booked, cad.get("agreed_day_time"), cad.get("call_summary"),
                     call.get("transcript"), call.get("recording_url"),
                     call.get("duration_ms") or call.get("call_length_ms"),
                     cad.get("callback_when"), cad.get("main_objection"),
                     bool(cad.get("asked_if_ai")), cid))
        conn.commit()
    # Mirror into the MAIN funnel (calls + classifications) as her own BDE so the dashboard shows Lisa 5
    # alongside Lisa-1 / Lisa-4 and the humans.
    try:
        _L1._write_funnel_call(pool, cid, call.get("retell_llm_dynamic_variables") or {}, cad,
                               ({**call, "to_number": call.get("from_number")} if inbound else call),
                               bde_ext="LISA5", bde_name="Lisa 5", booked_override=booked)
    except Exception as exc:
        log.warning("lisa5_funnel_write_failed", error=str(exc)[:140])
    # BOOKED → put the meeting on the Lisa5 calendar (what the floor pipeline + a human closer see). Time
    # parsed from the prospect's spoken words. Generic 'meeting' type (Lisa-5 has no website-reveal flow).
    if booked and d9:
        # A confirmed booking must ALSO land in the booked CRM (not only the funnel + calendar). Contact/time
        # all come from OUR classifier / the dialed brief, never Retell. Idempotent + fill-blank.
        _L1._crm_mark_booked(pool, d9, contact_name=(dyn5.get("prospect_name") or cad.get("contact_name")),
                             contact_email=cad.get("confirmed_email"), agreed_day_time=cad.get("agreed_day_time"),
                             next_action_at=_L1._parse_when(cad.get("agreed_day_time")), agent="Lisa 5")
        try:
            when = _L1._parse_when(cad.get("agreed_day_time"))
            if when:
                who = (call.get("retell_llm_dynamic_variables") or {}).get("company_name") or d9
                ex_ev = _fetch(pool, "SELECT 1 FROM calendar_events WHERE bde_name=%s AND type='meeting' "
                               "AND status='pending' AND right(regexp_replace(COALESCE(dest_number,''),'[^0-9]','','g'),9)=%s LIMIT 1",
                               (_BDE, d9))
                if not ex_ev:
                    with pool.connection() as conn, conn.cursor() as cur:
                        cur.execute("INSERT INTO calendar_events (bde_name,type,title,start_at,end_at,notes,"
                                    "dest_number,created_by,status) VALUES (%s,'meeting',%s,%s,%s,%s,%s,'lisa5','pending')",
                                    (_BDE, f"📅 Lisa 5 meeting: {who}", when, when + timedelta(minutes=15),
                                     f"Prospect said: {cad.get('agreed_day_time') or ''}",
                                     (call.get("from_number") if inbound else call.get("to_number"))))
                        conn.commit()
                    log.info("lisa5_meeting_event_created", dest9=d9, when=str(when))
        except Exception as exc:
            log.warning("lisa5_meeting_event_failed", error=str(exc)[:140])
        # she promises "I'll text you the details" on every booking — keep that promise automatically.
        # NOTE: neutral wording (no offer specifics) — review before enabling Lisa-5 for a real campaign.
        try:
            pn = call.get("from_number") if inbound else call.get("to_number")
            if (pn or "").startswith("+614") and _L1._twilio_ready(settings):
                frm = (list(getattr(settings, "lisa5_numbers", []) or []) or [""])[0]
                when_txt = (cad.get("agreed_day_time") or "the time we agreed").strip()
                body = (f"Hey, it's Lisa - all locked in for {when_txt}. I'll send you the details before "
                        "we chat. Cheers!")
                if frm and _L1._send_sms_twilio(settings, pn, body, frm, pool):
                    _L1._log_sms(pool, "outbound", frm, pn, body, d9)
        except Exception as exc:
            log.warning("lisa5_booking_sms_failed", error=str(exc)[:120])
    # NEVER-CONNECTED calls carry no analysis outcome — classify them off the telephony reason instead,
    # or they'd be silently one-shot lost: dead numbers get purged, busy/no-pickup get a normal retry.
    if not outcome and d9:
        reason = (call.get("disconnection_reason") or "").strip().lower()
        if reason in ("invalid_destination", "no_valid_payment", "concurrency_limit_reached"):
            if reason == "invalid_destination":
                with pool.connection() as conn, conn.cursor() as cur:
                    cur.execute("UPDATE lisa5_pool SET bucket='ok', issue='dead number' WHERE dest9=%s", (d9,))
                    cur.execute("UPDATE calendar_events SET status='cancelled' WHERE bde_name=%s AND status='pending' "
                                "AND right(regexp_replace(COALESCE(dest_number,''),'[^0-9]','','g'),9)=%s", (_BDE, d9))
                    conn.commit()
            return {"ok": True, "booked": False, "outcome": reason}
        if reason in ("dial_busy", "dial_no_answer", "dial_failed"):
            outcome = "no_answer"
    # WRONG NUMBER → the pool number doesn't reach this business. Kill the pool row + pending events so she
    # never redials a random person.
    if outcome == "wrong_number" and d9:
        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute("UPDATE lisa5_pool SET bucket='ok', issue='wrong number - unreachable' WHERE dest9=%s", (d9,))
            cur.execute("UPDATE calendar_events SET status='cancelled' WHERE bde_name=%s AND status='pending' "
                        "AND right(regexp_replace(COALESCE(dest_number,''),'[^0-9]','','g'),9)=%s", (_BDE, d9))
            conn.commit()
        return {"ok": True, "booked": False, "outcome": outcome, "cleaned": "wrong_number"}
    # NOT booked → Lisa-5's own next move (callback at the promised time / paced retry).
    if not booked and d9:
        try:
            schedule_lisa5_followup(pool, settings, dest9=d9,
                                    dest_number=call.get("to_number") or "",
                                    outcome=outcome, cad=cad,
                                    dyn=call.get("retell_llm_dynamic_variables") or {})
        except Exception as exc:
            log.warning("lisa5_followup_failed", error=str(exc)[:140])
    return {"ok": True, "booked": booked, "outcome": outcome}


def handle_lisa5_inbound_sms(pool: ConnectionPool, settings: Settings, from_number: str, body: str,
                             to_number: str | None = None) -> dict:
    """A prospect TEXTED one of Lisa-5's numbers back. Capture it, then hand off to the SHARED inbound-SMS
    auto-responder on the Lisa-5 (growth-audit) line — curiosity + get-them-on-a-call; a texted time books a
    callback; NEVER reveals the purpose pre-booking. `to_number` = the exact Lisa-5 number they texted (from
    the Twilio webhook 'To'); it is threaded through so the reply comes from that same number. When absent we
    log + reply on Lisa-5's primary number."""
    from . import lisa as _L1
    ensure_lisa5_tables(pool)
    d9 = _L1._d9(from_number)
    l5 = [_L1._e164_au(n) for n in list(getattr(settings, "lisa5_numbers", []) or []) if n]
    texted = _L1._e164_au(to_number) if to_number else ""
    line = texted if texted in l5 else (l5[0] if l5 else "L5")   # exact Lisa-5 number, else primary, else token
    logged_to = line if line in l5 else (l5[0] if l5 else "")
    _L1._log_sms(pool, "inbound", from_number, logged_to, body, d9)
    return _L1.reply_to_inbound_sms(pool, settings, dest9=d9, from_line=line, inbound_text=body)


def schedule_lisa5_followup(pool: ConnectionPool, settings: Settings, *, dest9: str, dest_number: str,
                            outcome: str, cad: dict, dyn: dict) -> None:
    """Lisa-5's next move after a non-booked call, on the Lisa5 calendar (never Lisa-1/4's): a callback at
    the time the prospect asked for, or a paced retry for no-pickups. Mirrors Lisa-4's follow-up rules.
    Attempt-counting is by Lisa-5's CURRENT caller numbers; if the rotation pool ever changes, add an
    append-only line registry (as Lisa-4's L4_LINE_RX) so history isn't lost."""
    from .calendar import create_event
    from . import lisa as _L1
    tz = settings.tz
    l5_numbers = list(getattr(settings, "lisa5_numbers", []) or [])
    who = dyn.get("company_name") or dest_number
    if outcome == "callback_requested" or cad.get("callback_when"):
        when = _L1._parse_when(cad.get("callback_when")) or (
            datetime.now(ZoneInfo(tz)) + timedelta(days=1)).replace(hour=10, minute=0, second=0, microsecond=0)
        create_event(pool, bde_name=_BDE, type="callback", title=f"📞 Lisa5 callback: {who}",
                     start_at=when, end_at=when + timedelta(minutes=15),
                     notes=f"Prospect asked for a callback: {cad.get('callback_when') or ''}",
                     dest_number=dest_number, created_by="lisa5")
        return
    if outcome in ("no_answer", "voicemail", "gatekeeper_only"):
        # MOBILE-ONLY: never schedule a cold retry to a landline (pool is mobile-only — belt-and-suspenders;
        # requested callbacks are handled above and stay exempt).
        if (dest9 or "")[:1] != "4":
            return
        # MISSED-CALL SMS (once per prospect, mobiles only): ultra-minimal callback bait — NO pitch.
        if outcome in ("no_answer", "voicemail") and (dest_number or "").startswith("+614"):
            try:
                row = _fetch(pool, "SELECT sms_sent FROM lisa5_pool WHERE dest9=%s", (dest9,))
                if row and not row[0].get("sms_sent") and _L1._twilio_ready(settings):
                    frm = (l5_numbers or [""])[0]
                    body = ("Hey, it's Lisa — tried to give you a buzz. Could you call me back on this "
                            "number when you get a sec? Ta!")
                    if frm and _L1._send_sms_twilio(settings, dest_number, body, frm, pool):
                        _L1._log_sms(pool, "outbound", frm, dest_number, body, dest9)
                        with pool.connection() as conn, conn.cursor() as cur:
                            cur.execute("UPDATE lisa5_pool SET sms_sent=true WHERE dest9=%s", (dest9,))
                            conn.commit()
                        log.info("lisa5_miss_sms_sent", to=dest_number)
            except Exception as exc:
                log.warning("lisa5_miss_sms_failed", error=str(exc)[:120])
        attempts = _fetch(pool, "SELECT count(*) n FROM lisa_calls WHERE from_number = ANY(%s) AND dest9=%s",
                          (l5_numbers, dest9))[0]["n"]
        if attempts >= int(getattr(settings, "lisa_retry_max_attempts", 4)):
            return
        now_l = datetime.now(ZoneInfo(tz))
        wstart = int(getattr(settings, "lisa_call_window_start", 9))
        wend = int(getattr(settings, "lisa_call_window_end", 17))
        dt_h = int(getattr(settings, "lisa_double_tap_hours", 2))
        cand = now_l + timedelta(hours=dt_h)
        # VOICEMAIL LEVER: voicemail ALSO gets a same-day double-tap, then next-business-day retries at a
        # VARIED hour (owners who dodge a 9am unknown mobile often answer at 2pm).
        _vary = [9, 13, 15, 11]
        if outcome in ("no_answer", "voicemail") and attempts <= 1 and dt_h > 0 and cand.weekday() < 5 and wstart <= cand.hour < wend:
            when, label = cand, "double-tap"
        elif outcome in ("no_answer", "voicemail"):
            _hr = min(max(_vary[min(attempts, len(_vary) - 1)], wstart), wend - 1)
            when, label = _L1._future_biz(now_l + timedelta(days=1), _hr), "retry"
        else:
            when = _L1._future_biz(now_l + timedelta(days=int(getattr(settings, "lisa_retry_cadence_days", 3))), wstart)
            label = "retry"
        create_event(pool, bde_name=_BDE, type="retry", title=f"🔄 Lisa5 {label} ({attempts+1}): {who}",
                     start_at=when, end_at=when + timedelta(minutes=15),
                     notes="No pickup yet — Lisa5 retries at a different time.",
                     dest_number=dest_number, created_by="lisa5")
