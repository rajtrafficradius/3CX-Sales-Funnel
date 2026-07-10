"""Pipeline 2 assignment engine — rotate a DIFFERENT BDE onto each attempt.

Pipeline 2 = the prospect is already with a marketing agency. We can't win them
until their contract is up, and calling the same person repeatedly makes them
rude — so we (a) rotate a *different* BDE onto each attempt, and (b) call on a
cadence: around a mentioned contract-end date if known, else roughly monthly to
keep fishing for that date. The BDM (Raven/Kiran) can manually override any
assignment; a manual override then sticks (auto-rotation yields to it).

Keyed by `dest9` (trailing 9 significant digits of the dialled number) — the same
distinct-prospect key the pipeline tables count by — so it works whether or not
the prospect is in the master DB. No LLM needed: cadence defaults to ~monthly
until the contract-aware AI fields are added (separate, approved re-classify).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from psycopg_pool import ConnectionPool

from .db import fetch_all, fetch_one
from .logging import get_logger
from .prospects import gads_dnb_gate

log = get_logger(__name__)

_P2 = "pipeline2_existing_agency"
_DEST9 = "right(regexp_replace(c.dest_number, '[^0-9]', '', 'g'), 9)"


def active_bdes(pool: ConnectionPool) -> list[str]:
    """Active, in-scope BDE names (the rotation pool)."""
    return [r["n"] for r in fetch_all(
        pool,
        "SELECT DISTINCT COALESCE(bde_name, extension) AS n FROM bde_agents "
        "WHERE in_scope AND active ORDER BY 1",
    )]


def calling_bdes(pool: ConnectionPool, settings=None) -> list[str]:
    """Active BDEs eligible for a COLD-CALLING worklist — active_bdes minus non-calling names (the BDM,
    e.g. Ben, who only does verification/oversight). Used by every calling allocator so a BDM is never
    auto-assigned fresh/retry/reached calls. (They still show in analysis/reports.)"""
    exclude = set()
    raw = getattr(settings, "non_calling_names", "") if settings is not None else ""
    for n in (raw or "").split(","):
        n = n.strip().lower()
        if n:
            exclude.add(n)
    return [b for b in active_bdes(pool) if (b or "").strip().lower() not in exclude]


def _pick_next_bde(roster: list[str], last_bde: str | None, counts: dict[str, int],
                   load: dict[str, int] | None = None) -> str | None:
    """Choose the next BDE to call. Priorities:
       1. never the most recent caller (rotate to someone else),
       2. whoever has called THIS prospect the fewest times (try a fresh face),
       3. the least globally-loaded BDE this run (spread the work evenly),
       4. alphabetical for determinism.
    """
    if not roster:
        return None
    load = load or {}
    pool_ = [b for b in roster if b != last_bde] or list(roster)
    return sorted(pool_, key=lambda b: (counts.get(b, 0), load.get(b, 0), b))[0]


def sync_pipeline2(pool: ConnectionPool, default_cadence_days: int = 30) -> dict:
    """Rebuild Pipeline-2 assignment/cadence state from the calls. Idempotent.

    Honors existing BDM overrides (override_by set ⇒ assigned_bde/next_action_at
    are left alone; only call stats refresh). Logs every assignment change.
    """
    roster = active_bdes(pool)
    # One row per distinct prospect with at least one Pipeline-2 call.
    prospects = fetch_all(
        pool,
        f"SELECT {_DEST9} AS dest9, max(c.dest_number) AS dest_number, "
        "       max(c.started_at) AS last_call_at, count(*) AS attempts, "
        "       max(cl.prospect_company) AS business_name, "
        "       bool_or(COALESCE(cl.do_not_contact, false)) AS dnc, "
        # most-recent non-null AI cadence / contract-end signal across this prospect's calls
        "       (array_agg(cl.recommended_cadence_days ORDER BY c.started_at DESC) "
        "          FILTER (WHERE COALESCE(cl.recommended_cadence_days,0) > 0))[1] AS rec_cadence, "
        "       (array_agg(cl.contract_end ORDER BY c.started_at DESC) "
        "          FILTER (WHERE cl.contract_end IS NOT NULL AND cl.contract_end <> ''))[1] AS contract_end "
        "FROM calls c JOIN classifications cl ON cl.call_id=c.call_id "
        f"WHERE c.in_scope AND cl.pipeline=%s AND c.dest_number IS NOT NULL AND c.dest_number<>'' "
        f"GROUP BY {_DEST9} ORDER BY {_DEST9}",
        (_P2,),
    )
    # Per-prospect, per-BDE call counts + last-contact (across ALL their calls to
    # that number, so rotation knows who has already worked it and how recently).
    per_bde = fetch_all(
        pool,
        f"SELECT {_DEST9} AS dest9, COALESCE(c.bde_name, c.bde_extension) AS bde, "
        "       count(*) AS n, max(c.started_at) AS last_at "
        "FROM calls c "
        f"WHERE c.in_scope AND c.dest_number IS NOT NULL AND c.dest_number<>'' "
        f"  AND {_DEST9} IN (SELECT {_DEST9} FROM calls c JOIN classifications cl "
        f"      ON cl.call_id=c.call_id WHERE cl.pipeline=%s AND c.in_scope) "
        f"GROUP BY {_DEST9}, COALESCE(c.bde_name, c.bde_extension)",
        (_P2,),
    )
    by_dest: dict[str, dict] = {}
    last_by_dest: dict[str, tuple[str, datetime]] = {}
    for r in per_bde:
        d = r["dest9"]
        by_dest.setdefault(d, {})[r["bde"]] = int(r["n"] or 0)
        prev = last_by_dest.get(d)
        if r["last_at"] and (prev is None or r["last_at"] > prev[1]):
            last_by_dest[d] = (r["bde"], r["last_at"])

    created = updated = reassigned = 0
    load: dict[str, int] = {}  # assignments made this run, for even spread
    current = {p["dest9"] for p in prospects if p["dest9"]}
    with pool.connection() as conn, conn.cursor() as cur:
        # Prune prospects that are no longer Pipeline 2 (e.g. re-classified out),
        # except ones a BDM manually assigned (override_by set) — those persist.
        pruned = 0
        if current:
            cur.execute(
                "DELETE FROM prospect_pipeline WHERE override_by IS NULL "
                "AND NOT (dest9 = ANY(%s))",
                (list(current),),
            )
            pruned = cur.rowcount
        for p in prospects:
            dest9 = p["dest9"]
            if not dest9:
                continue
            counts = by_dest.get(dest9, {})
            last_bde = last_by_dest.get(dest9, (None, None))[0]
            last_call = p["last_call_at"]
            # link to master prospect (if any)
            pid_row = fetch_one(pool, "SELECT id, business_name FROM prospects WHERE %s = ANY(phones_norm) LIMIT 1", (dest9,))
            prospect_id = pid_row["id"] if pid_row else None
            biz = p["business_name"] or (pid_row["business_name"] if pid_row else None)

            existing = fetch_one(pool, "SELECT * FROM prospect_pipeline WHERE dest9=%s", (dest9,))
            overridden = bool(existing and existing.get("override_by"))
            # Cadence: when the prospect gave a real CONTRACT-END signal, honour the
            # AI's contract-aware cadence (don't pester someone locked into a long deal).
            # Otherwise rotate on the configured default (WEEKLY) — the AI's generic
            # "≈monthly" guess for no-signal prospects is overridden by the weekly policy.
            contract_end = p.get("contract_end")
            ai_cadence = int(p["rec_cadence"]) if p.get("rec_cadence") else None
            cadence = ai_cadence if (ai_cadence and contract_end) else default_cadence_days
            contract_end = p.get("contract_end")

            # DND (#5): a manual BDM DND (dnd_by != 'ai') always persists; the AI signal
            # (do_not_contact on any call) auto-activates DND. A DND prospect is NEVER
            # scheduled for a call (next_action_at = NULL) and is not rotated.
            manual_dnd = bool(existing and existing.get("dnd") and (existing.get("dnd_by") or "ai") != "ai")
            ai_dnd = bool(p.get("dnc"))
            dnd = manual_dnd or ai_dnd
            if manual_dnd:
                dnd_reason, dnd_by = existing.get("dnd_reason"), existing.get("dnd_by")
                dnd_at = existing.get("dnd_at")
            elif ai_dnd:
                dnd_reason = "AI: prospect was rude or asked to be removed from the list"
                dnd_by = "ai"
                dnd_at = existing.get("dnd_at") if (existing and existing.get("dnd")) else datetime.now(timezone.utc)
            else:
                dnd_reason = dnd_by = dnd_at = None

            if overridden:
                assigned = existing["assigned_bde"]
                next_at = existing["next_action_at"]
            else:
                assigned = _pick_next_bde(roster, last_bde, counts, load)
                base = last_call or datetime.now(timezone.utc)
                next_at = base + timedelta(days=cadence)
            if dnd:
                next_at = None  # never schedule a do-not-call prospect
            elif assigned:
                load[assigned] = load.get(assigned, 0) + 1

            if existing:
                cur.execute(
                    "UPDATE prospect_pipeline SET dest_number=%s, prospect_id=%s, pipeline=%s, "
                    "business_name=%s, assigned_bde=%s, last_bde=%s, next_action_at=%s, "
                    "cadence_days=%s, contract_end=%s, attempts=%s, last_call_at=%s, "
                    "dnd=%s, dnd_reason=%s, dnd_at=%s, dnd_by=%s, updated_at=now() WHERE dest9=%s",
                    (p["dest_number"], prospect_id, _P2, biz, assigned, last_bde, next_at,
                     cadence, contract_end, int(p["attempts"] or 0), last_call,
                     dnd, dnd_reason, dnd_at, dnd_by, dest9),
                )
                updated += 1
                if not overridden and not dnd and existing.get("assigned_bde") != assigned:
                    reassigned += 1
                    cur.execute(
                        "INSERT INTO prospect_assignments (dest9, bde_name, assigned_by, reason) "
                        "VALUES (%s,%s,'auto_rotate',%s)",
                        (dest9, assigned, f"rotated off {last_bde or 'n/a'}"),
                    )
            else:
                cur.execute(
                    "INSERT INTO prospect_pipeline (dest9, dest_number, prospect_id, pipeline, "
                    "business_name, assigned_bde, last_bde, next_action_at, cadence_days, contract_end, "
                    "attempts, last_call_at, dnd, dnd_reason, dnd_at, dnd_by) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    (dest9, p["dest_number"], prospect_id, _P2, biz, assigned, last_bde, next_at,
                     cadence, contract_end, int(p["attempts"] or 0), last_call,
                     dnd, dnd_reason, dnd_at, dnd_by),
                )
                created += 1
                if not dnd:
                    cur.execute(
                        "INSERT INTO prospect_assignments (dest9, bde_name, assigned_by, reason) "
                        "VALUES (%s,%s,'auto_rotate',%s)",
                        (dest9, assigned, f"first rotation off {last_bde or 'n/a'}"),
                    )
        # Stamp the master prospects table so the DB browser shows pipeline + owner
        # (pipeline = each prospect's most-recent classified pipeline; owner from P2 board).
        cur.execute(
            "UPDATE prospects pr SET pipeline = sub.pl FROM ("
            "  SELECT DISTINCT ON (d9) d9, pl FROM ("
            "    SELECT right(regexp_replace(c.dest_number,'[^0-9]','','g'),9) AS d9, "
            "           cl.pipeline AS pl, c.started_at "
            "    FROM calls c JOIN classifications cl ON cl.call_id=c.call_id "
            "    WHERE c.in_scope AND c.dest_number<>'' "
            "      AND cl.pipeline IN ('pipeline1_interested','pipeline2_existing_agency')) t "
            "  ORDER BY d9, started_at DESC) sub "
            "WHERE sub.d9 = ANY(pr.phones_norm)"
        )
        cur.execute(
            "UPDATE prospects pr SET assigned_bde = pp.assigned_bde "
            "FROM prospect_pipeline pp WHERE pp.prospect_id = pr.id"
        )
        conn.commit()
    dnd_n = fetch_one(pool, "SELECT count(*) AS n FROM prospect_pipeline WHERE dnd")["n"]
    stats = {"prospects": len(prospects), "created": created, "updated": updated,
             "reassigned": reassigned, "pruned": pruned, "dnd": dnd_n}
    log.info("pipeline2_sync", **stats)
    return stats


def assign_prospect(pool: ConnectionPool, dest9: str, bde: str, *, by: str,
                    next_action_at: str | None = None, notes: str | None = None) -> bool:
    """BDM/admin manual override. Sticks (auto-rotation yields to override_by)."""
    sets = ["assigned_bde=%s", "override_by=%s", "updated_at=now()"]
    params: list = [bde, by]
    if next_action_at:
        sets.append("next_action_at=%s")
        params.append(next_action_at)
    if notes is not None:
        sets.append("notes=%s")
        params.append(notes)
    params.append(dest9)
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(f"UPDATE prospect_pipeline SET {', '.join(sets)} WHERE dest9=%s", params)
        ok = cur.rowcount
        cur.execute(
            "INSERT INTO prospect_assignments (dest9, bde_name, assigned_by, reason) VALUES (%s,%s,%s,%s)",
            (dest9, bde, by, notes or "manual override"),
        )
        conn.commit()
    return bool(ok)


# Existing-agency prospect is in the confirmed-Google-Ads calling pool when its number (last-9) matches
# a GAds business's phone, or its domain is a GAds domain. Keeps the board in step with the pipeline
# board's GAds tab count (the calling layer only works the confirmed-ads pool).
_GADS_P2_FILTER = (
    "(pp.dest9 IN (SELECT co.phone_norm "
    "   FROM enrichment ge JOIN companies co ON co.domain=ge.domain "
    "   WHERE (ge.dataforseo->>'running_google_ads')='true' AND COALESCE(co.phone_norm,'') <> ''"
    + gads_dnb_gate("ge") + ") "
    " OR lower(COALESCE(pr.domain,'')) IN (SELECT e.domain FROM enrichment e "
    "   WHERE (e.dataforseo->>'running_google_ads')='true'" + gads_dnb_gate("e") + "))")


# A prospect that has since BOOKED a meeting (any p5 classification wins over "existing agency")
# is dropped from the agency list so it shows in ONE place only (the Booked pipeline).
_NOT_BOOKED = (
    "pp.dest9 NOT IN (SELECT right(regexp_replace(c.dest_number,'[^0-9]','','g'),9) "
    "  FROM calls c JOIN classifications cl ON cl.call_id=c.call_id "
    "  WHERE c.in_scope AND c.dest_number <> '' AND cl.pipeline_stage='p5')")


def list_pipeline2(pool: ConnectionPool, *, bde: str | None = None, due_only: bool = False,
                   limit: int = 500, gads_only: bool = False, since=None) -> list[dict]:
    """Pipeline-2 assignment board. `bde` filters to one owner; `due_only` to
    prospects whose next_action_at has arrived (the actionable call list).
    `gads_only` scopes to the confirmed-Google-Ads pool so the board matches the
    pipeline board's Existing-agency tab count (the calling layer only works that pool).
    `since` (a date) keeps only prospects last called on/after it — the window filter."""
    where = ["pp.pipeline=%(p)s", _NOT_BOOKED]
    params: dict = {"p": _P2, "lim": limit}
    if bde and bde != "ALL":
        where.append("pp.assigned_bde=%(bde)s")
        params["bde"] = bde
    if gads_only:
        where.append(_GADS_P2_FILTER)
    if since is not None:
        where.append("pp.last_call_at::date >= %(since)s")
        params["since"] = since
    if due_only:
        # DND prospects are NEVER due (they're excluded from the call list entirely).
        where.append("NOT COALESCE(pp.dnd, false) AND pp.next_action_at <= now()")
    rows = fetch_all(
        pool,
        "SELECT pp.dest9, pp.dest_number, pp.business_name, pp.assigned_bde, pp.last_bde, "
        "       pp.next_action_at, pp.cadence_days, pp.contract_end, pp.attempts, "
        "       pp.last_call_at, pp.override_by, pp.notes, pp.dnd, pp.dnd_reason, pp.dnd_by, pr.domain "
        "FROM prospect_pipeline pp LEFT JOIN prospects pr ON pp.prospect_id=pr.id "
        f"WHERE {' AND '.join(where)} "
        "ORDER BY COALESCE(pp.dnd,false) ASC, (pp.next_action_at IS NULL) DESC, "
        "pp.next_action_at ASC NULLS FIRST LIMIT %(lim)s",
        params,
    )
    return rows
