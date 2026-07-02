"""RPC Connect — "Next Move" feedback engine.

Mirrors calendar.sync_callbacks_for_day: a deterministic, idempotent per-day pass
that turns the day's un-connected outbound dials (the decision-maker was NOT reached)
into an actionable coaching + follow-up record in `rpc_actions`, and auto-schedules a
retry on the BDE's calendar.

For every in-scope OUTBOUND dial on `day` whose classification has rpc_connect=false,
we group by the dialled number (dest9 = trailing 9 significant digits), take the LATEST
un-connected attempt, read the AI's `rpc_next_move` (from classifications.evidence), run
a few DETERMINISTIC compliance checks (double-tap / voicemail-left / gatekeeper handling),
derive the scenario + prescribed next move, and upsert one row per number. Actions
auto-resolve when a later call to the same number connects (or the prospect goes DND).

Deliberately conservative: SMS can't be verified (the messages table is inbound-only),
so sms_sent stays NULL and never counts as a breach; wrong numbers, toll-free numbers,
and DND prospects are skipped; and companies are de-duplicated so one business yields
one action even across several numbers.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

from psycopg_pool import ConnectionPool

from .calendar import create_event, guess_when, update_event
from .config import Settings
from .logging import get_logger

log = get_logger(__name__)

# Action codes we know how to act on. A subset schedules a calendar retry.
_RETRY_ACTIONS = {"double_tap", "sms_vm", "retry_vary_time", "gatekeeper_getdm"}
# Default follow-up channel per action code (used when the AI didn't set one).
_DEFAULT_CHANNEL = {
    "double_tap": "call_then_sms_vm",
    "sms_vm": "call_then_sms_vm",
    "gatekeeper_getdm": "retry_call",
    "ask_for_dm": "retry_call",
    "retry_vary_time": "retry_call",
    "mark_dnd": "none",
    "none": "none",
}


# --------------------------------------------------------------------------- #
# Pure helpers
# --------------------------------------------------------------------------- #
def _channel(dest9: str | None, is_tollfree: bool) -> str:
    """Classify a number from its trailing-9 digits. AU mobiles are 04xxxxxxxx, so the
    trailing-9 form begins with 4; 1300/1800 are toll-free; everything else is landline."""
    if is_tollfree:
        return "tollfree"
    if dest9 and dest9.startswith("4"):
        return "mobile"
    return "landline"


def _company_key(prospect_company: str | None, dest9: str) -> str:
    """Company identity for dedup — a normalised business name, else the number itself
    (so numbers with no known company never collapse into one another)."""
    name = (prospect_company or "").strip().lower()
    return name or f"num:{dest9}"


def _derive_scenario(channel: str, reason: str, ev: dict, comp: dict) -> dict:
    """Map (reason, channel, compliance) -> scenario label, action_code, required_action
    and whether the BDE actually did the required action. SMS is unverifiable, so it can
    never on its own make did_required_action False."""
    asked_for_dm = bool(ev.get("asked_for_dm"))
    asked_cb = bool(ev.get("asked_callback_time"))
    got_direct = bool(ev.get("got_direct_contact"))
    attempts = comp["attempts"]
    if reason == "gatekeeper_block":
        return {
            "scenario": "Gatekeeper blocked the decision-maker",
            "action_code": "gatekeeper_getdm",
            "required_action": "Get past the gatekeeper: ask for the decision-maker by name and a firm callback time.",
            "did": bool(comp["gatekeeper_handled"]) and (asked_for_dm or asked_cb),
        }
    if reason == "not_decision_maker":
        return {
            "scenario": "Reached a non-decision-maker",
            "action_code": "ask_for_dm",
            "required_action": "Ask for the decision-maker by name and secure a direct line / callback time.",
            "did": asked_for_dm or got_direct,
        }
    if reason == "dm_unavailable":
        return {
            "scenario": "Decision-maker unavailable",
            "action_code": "retry_vary_time",
            "required_action": "Call back when the decision-maker is in; ask for them by name.",
            "did": asked_cb or bool(ev.get("dm_available_when")),
        }
    if reason == "voicemail":
        if channel == "mobile":
            return {
                "scenario": "Voicemail on mobile",
                "action_code": "sms_vm",
                "required_action": "Leave a voicemail and send an intro SMS.",
                "did": bool(comp["voicemail_left"]),
            }
        return {
            "scenario": "Voicemail",
            "action_code": "retry_vary_time",
            "required_action": "Retry at a different time of day.",
            "did": attempts >= 2,
        }
    if reason == "no_answer":
        if channel == "mobile":
            return {
                "scenario": "No answer on mobile",
                "action_code": "double_tap",
                "required_action": "Double-tap: re-dial the mobile shortly after, then leave a voicemail / SMS.",
                "did": bool(comp["double_tap_done"]),
            }
        return {
            "scenario": "No answer",
            "action_code": "retry_vary_time",
            "required_action": "Retry at a different time of day.",
            "did": attempts >= 2,
        }
    # "other" / unknown fallback — treat like an un-reached DM by the number's channel.
    if channel == "mobile":
        return {
            "scenario": "Decision-maker not reached",
            "action_code": "double_tap",
            "required_action": "Double-tap the mobile, then leave a voicemail / SMS.",
            "did": bool(comp["double_tap_done"]),
        }
    return {
        "scenario": "Decision-maker not reached",
        "action_code": "retry_vary_time",
        "required_action": "Retry at a different time of day.",
        "did": attempts >= 2,
    }


# --------------------------------------------------------------------------- #
# Main entry point
# --------------------------------------------------------------------------- #
def analyze_rpc_actions(pool: ConnectionPool, settings: Settings, day: date) -> dict:
    """Analyse `day`'s un-connected outbound dials, upsert `rpc_actions`, resolve
    already-connected ones, and schedule retries. Idempotent (safe to re-run)."""
    start = datetime.combine(day, time.min)
    end = start + timedelta(days=1)
    window_min = settings.rpc_double_tap_window_min
    vm_secs = settings.rpc_voicemail_left_seconds

    # DND numbers (Pipeline-2 do-not-call) — skip these entirely.
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT dest9 FROM prospect_pipeline WHERE dnd")
        dnd_numbers = {r["dest9"] for r in cur.fetchall()}

    # Every in-scope OUTBOUND dial on the day, with its classification.
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT c.call_id, c.dest_number, c.bde_name, c.started_at,
                   c.answered, c.is_voicemail, c.talk_seconds,
                   right(regexp_replace(c.dest_number, '[^0-9]', '', 'g'), 9) AS dest9,
                   (c.dest_number ~ '1(3|8)00') AS is_tollfree,
                   COALESCE(cl.rpc_connect, false) AS rpc_connect,
                   COALESCE(cl.do_not_contact, false) AS do_not_contact,
                   COALESCE(cl.gatekeeper_handled_well, false) AS gatekeeper_handled_well,
                   cl.call_outcome, cl.prospect_company,
                   cl.evidence -> 'rpc_next_move' AS rpc_next_move
            FROM calls c
            LEFT JOIN classifications cl ON cl.call_id = c.call_id
            WHERE c.in_scope AND c.direction = %(outbound)s
              AND c.started_at >= %(start)s AND c.started_at < %(end)s
            ORDER BY c.dest_number, c.started_at
            """,
            {"outbound": settings.cdr.outbound_value, "start": start, "end": end},
        )
        rows = cur.fetchall()

    # Group by number.
    groups: dict[str, list[dict]] = {}
    for r in rows:
        d9 = r["dest9"]
        if not d9:
            continue
        groups.setdefault(d9, []).append(r)

    # Build one candidate action per number (latest un-connected attempt).
    candidates: list[dict] = []
    for d9, grp in groups.items():
        unconnected = [g for g in grp if not g["rpc_connect"]]
        if not unconnected:
            continue  # DM was reached on every attempt today — nothing to action
        anchor = max(unconnected, key=lambda g: g["started_at"])
        is_tollfree = bool(anchor["is_tollfree"])
        channel = _channel(d9, is_tollfree)
        ev = anchor.get("rpc_next_move") or {}
        reason_code = (ev.get("rpc_unreached_reason") or "").strip()
        if not reason_code or reason_code == "reached_rpc":
            # Fall back to the CDR/classifier outcome when the AI field is absent/stale.
            outcome = (anchor.get("call_outcome") or "").strip()
            reason_code = {
                "voicemail": "voicemail", "gatekeeper": "gatekeeper_block",
                "wrong_number": "wrong_number",
            }.get(outcome, "no_answer" if anchor["is_voicemail"] is not True else "voicemail")

        # --- SKIP rules: wrong number, toll-free, DND ---
        if reason_code == "wrong_number" or (anchor.get("call_outcome") == "wrong_number"):
            continue
        if channel == "tollfree":
            continue
        if anchor["do_not_contact"] or d9 in dnd_numbers:
            continue

        # --- deterministic compliance ---
        mobile_misses = unconnected if channel == "mobile" else []
        double_tap_done = False
        if mobile_misses:
            first = min(mobile_misses, key=lambda g: g["started_at"])
            w_end = first["started_at"] + timedelta(minutes=window_min)
            same_bde_in_window = [
                g for g in grp
                if g["bde_name"] == first["bde_name"]
                and first["started_at"] <= g["started_at"] <= w_end
            ]
            double_tap_done = len(same_bde_in_window) >= 2
        voicemail_left = any(
            g["is_voicemail"] and (g["talk_seconds"] or 0) >= vm_secs for g in mobile_misses
        )

        comp = {
            "attempts": len(grp),
            "double_tap_done": double_tap_done,
            "voicemail_left": voicemail_left,
            "gatekeeper_handled": anchor["gatekeeper_handled_well"],
        }
        derived = _derive_scenario(channel, reason_code, ev, comp)
        action_code = derived["action_code"]
        next_move = (ev.get("next_move") or "").strip() or derived["required_action"]
        ai_channel = (ev.get("next_move_channel") or "").strip()
        next_move_channel = ai_channel if ai_channel and ai_channel != "none" else _DEFAULT_CHANNEL.get(action_code, "none")

        candidates.append({
            "dest9": d9,
            "dest_number": anchor["dest_number"],
            "channel": channel,
            "company_key": _company_key(anchor.get("prospect_company"), d9),
            "business_name": anchor.get("prospect_company") or None,
            "last_call_id": anchor["call_id"],
            "last_attempt_at": anchor["started_at"],
            "last_bde": anchor["bde_name"],
            "attempts": len(grp),
            "answered_attempts": sum(1 for g in grp if g["answered"]),
            "scenario": derived["scenario"],
            "reason": (ev.get("reason_unavailable") or "").strip() or derived["scenario"],
            "action_code": action_code,
            "required_action": derived["required_action"],
            "next_move": next_move,
            "next_move_channel": next_move_channel,
            "dm_name": (ev.get("dm_name") or "").strip() or None,
            "dm_available_when": (ev.get("dm_available_when") or "").strip() or None,
            "reason_unavailable": (ev.get("reason_unavailable") or "").strip() or None,
            "double_tap_done": double_tap_done,
            "voicemail_left": voicemail_left,
            "sms_sent": None,  # inbound-only messages table — unverifiable, never a breach
            "asked_for_dm": bool(ev.get("asked_for_dm")),
            "asked_callback_time": bool(ev.get("asked_callback_time")),
            "gatekeeper_handled": bool(anchor["gatekeeper_handled_well"]),
            "did_required_action": bool(derived["did"]),
        })

    # Company-level dedup: most-recent attempt wins.
    candidates.sort(key=lambda c: c["last_attempt_at"], reverse=True)
    seen: set[str] = set()
    deduped: list[dict] = []
    for c in candidates:
        if c["company_key"] in seen:
            continue
        seen.add(c["company_key"])
        deduped.append(c)

    # --- UPSERT ---
    analyzed = 0
    for c in deduped:
        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO rpc_actions (
                    dest9, dest_number, channel, company_key, business_name,
                    last_call_id, last_attempt_at, last_bde, attempts, answered_attempts,
                    scenario, reason, action_code, required_action, next_move, next_move_channel,
                    dm_name, dm_available_when, reason_unavailable,
                    double_tap_done, voicemail_left, sms_sent,
                    asked_for_dm, asked_callback_time, gatekeeper_handled, did_required_action,
                    status, updated_at)
                VALUES (
                    %(dest9)s, %(dest_number)s, %(channel)s, %(company_key)s, %(business_name)s,
                    %(last_call_id)s, %(last_attempt_at)s, %(last_bde)s, %(attempts)s, %(answered_attempts)s,
                    %(scenario)s, %(reason)s, %(action_code)s, %(required_action)s, %(next_move)s, %(next_move_channel)s,
                    %(dm_name)s, %(dm_available_when)s, %(reason_unavailable)s,
                    %(double_tap_done)s, %(voicemail_left)s, %(sms_sent)s,
                    %(asked_for_dm)s, %(asked_callback_time)s, %(gatekeeper_handled)s, %(did_required_action)s,
                    'open', now())
                ON CONFLICT (dest9) DO UPDATE SET
                    dest_number = EXCLUDED.dest_number, channel = EXCLUDED.channel,
                    company_key = EXCLUDED.company_key, business_name = EXCLUDED.business_name,
                    last_call_id = EXCLUDED.last_call_id, last_attempt_at = EXCLUDED.last_attempt_at,
                    last_bde = EXCLUDED.last_bde, attempts = EXCLUDED.attempts,
                    answered_attempts = EXCLUDED.answered_attempts,
                    scenario = EXCLUDED.scenario, reason = EXCLUDED.reason,
                    action_code = EXCLUDED.action_code, required_action = EXCLUDED.required_action,
                    next_move = EXCLUDED.next_move, next_move_channel = EXCLUDED.next_move_channel,
                    dm_name = EXCLUDED.dm_name, dm_available_when = EXCLUDED.dm_available_when,
                    reason_unavailable = EXCLUDED.reason_unavailable,
                    double_tap_done = EXCLUDED.double_tap_done, voicemail_left = EXCLUDED.voicemail_left,
                    sms_sent = EXCLUDED.sms_sent,
                    asked_for_dm = EXCLUDED.asked_for_dm, asked_callback_time = EXCLUDED.asked_callback_time,
                    gatekeeper_handled = EXCLUDED.gatekeeper_handled,
                    did_required_action = EXCLUDED.did_required_action,
                    status = 'open', resolved_at = NULL, resolved_call_id = NULL,
                    updated_at = now()
                """,
                c,
            )
            conn.commit()
        analyzed += 1

    resolved = _resolve_open(pool, settings)
    scheduled = _schedule_retries(pool, settings)

    # Count what remains open right now.
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM rpc_actions WHERE status = 'open'")
        open_now = cur.fetchone()["n"]

    result = {"analyzed": analyzed, "open": open_now, "scheduled": scheduled, "resolved": resolved}
    if analyzed or resolved or scheduled:
        log.info("rpc_actions_done", day=str(day), **result)
    return result


# --------------------------------------------------------------------------- #
# Resolution + scheduling
# --------------------------------------------------------------------------- #
def _resolve_open(pool: ConnectionPool, settings: Settings) -> int:
    """Close any open action whose number later connected with the decision-maker, or
    whose prospect has since gone DND. Cancels the scheduled retry event if present."""
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT dest9, dest_number, last_attempt_at, event_id FROM rpc_actions WHERE status = 'open'"
        )
        openrows = cur.fetchall()
        cur.execute("SELECT dest9 FROM prospect_pipeline WHERE dnd")
        dnd_numbers = {r["dest9"] for r in cur.fetchall()}

    resolved = 0
    for r in openrows:
        d9 = r["dest9"]
        resolved_call_id = None
        is_resolved = False
        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT c.call_id, COALESCE(cl.rpc_connect, false) AS rpc_connect,
                       COALESCE(cl.do_not_contact, false) AS do_not_contact
                FROM calls c LEFT JOIN classifications cl ON cl.call_id = c.call_id
                WHERE c.in_scope AND c.direction = %(outbound)s
                  AND right(regexp_replace(c.dest_number, '[^0-9]', '', 'g'), 9) = %(dest9)s
                  AND c.started_at > %(last)s
                  AND (COALESCE(cl.rpc_connect, false) OR COALESCE(cl.do_not_contact, false))
                ORDER BY c.started_at
                LIMIT 1
                """,
                {"outbound": settings.cdr.outbound_value, "dest9": d9, "last": r["last_attempt_at"]},
            )
            hit = cur.fetchone()
        if hit:
            is_resolved = True
            resolved_call_id = hit["call_id"]
        elif d9 in dnd_numbers:
            is_resolved = True

        if not is_resolved:
            continue

        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE rpc_actions SET status='resolved', resolved_at=now(), "
                "resolved_call_id=%(cid)s, updated_at=now() WHERE dest9=%(dest9)s",
                {"cid": resolved_call_id, "dest9": d9},
            )
            conn.commit()
        if r["event_id"]:
            try:
                update_event(pool, r["event_id"], {"status": "cancelled"})
            except Exception as exc:
                log.warning("rpc_cancel_event_failed", dest9=d9, error=str(exc)[:160])
        resolved += 1
    return resolved


def _schedule_retries(pool: ConnectionPool, settings: Settings) -> int:
    """Create ONE calendar retry per open action that needs a follow-up call and has no
    event yet. Mirrors sync_callbacks_for_day's per-row try/except so one bad row can't
    abort the batch."""
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT dest9, dest_number, last_bde, last_call_id, business_name, "
            "next_move, dm_available_when, last_attempt_at, action_code "
            "FROM rpc_actions WHERE status='open' AND event_id IS NULL AND action_code = ANY(%(codes)s)",
            {"codes": list(_RETRY_ACTIONS)},
        )
        todo = cur.fetchall()

    scheduled = 0
    for r in todo:
        try:
            base = (r["last_attempt_at"].date() if r["last_attempt_at"] else date.today())
            when = guess_when(r["dm_available_when"] or r["next_move"], base)
            title = f"🔁 RPC retry: {r['business_name'] or r['dest_number']}"
            eid: int | None = None
            try:
                eid = create_event(
                    pool, bde_name=r["last_bde"], type="rpc_retry", title=title,
                    start_at=when, notes=r["next_move"], call_id=r["last_call_id"],
                    dest_number=r["dest_number"], created_by="rpc_ai",
                )
            except Exception:
                # A retry event already exists for this number (partial unique index).
                # Reuse it so we still record the linkage and stop retrying to create.
                with pool.connection() as conn, conn.cursor() as cur:
                    cur.execute(
                        "SELECT id FROM calendar_events WHERE type='rpc_retry' AND dest_number=%s "
                        "ORDER BY id DESC LIMIT 1",
                        (r["dest_number"],),
                    )
                    existing = cur.fetchone()
                eid = existing["id"] if existing else None
            if eid is None:
                continue
            with pool.connection() as conn, conn.cursor() as cur:
                cur.execute(
                    "UPDATE rpc_actions SET event_id=%(eid)s, updated_at=now() WHERE dest9=%(dest9)s",
                    {"eid": eid, "dest9": r["dest9"]},
                )
                conn.commit()
            scheduled += 1
        except Exception as exc:
            log.warning("rpc_schedule_failed", dest9=r["dest9"], error=str(exc)[:160])
    return scheduled
