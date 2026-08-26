"""TASKS / project-management board for the owners (Vysakh + Raj).

A simple tracked task list — the running record of what's been asked, its status, and notes — so the owners
can open it in a meeting and discuss. Access is restricted to an allow-list of owner emails (crm_config
'tasks_access_emails', comma-separated; defaults to the account owner). Seeded once from the work log.
"""
import re as _re

STATUSES = ["pending", "in_progress", "blocked", "done"]
_DEFAULT_ACCESS = "raj@trafficradius.com.au"


def ensure_tasks_table(pool) -> None:
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute("CREATE TABLE IF NOT EXISTS tasks ("
                    "  id bigserial PRIMARY KEY, title text NOT NULL, description text,"
                    "  status text DEFAULT 'pending', priority text DEFAULT 'normal', category text,"
                    "  link text, notes text, source text DEFAULT 'Vysakh', sort integer DEFAULT 0,"
                    "  created_at timestamptz DEFAULT now(), updated_at timestamptz DEFAULT now(),"
                    "  updated_by text, done_at timestamptz)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status)")
        conn.commit()


def _allowed(pool) -> list[str]:
    try:
        from . import lisa as _l
        r = _l._fetch(pool, "SELECT v FROM crm_config WHERE k='tasks_access_emails'")
        raw = (r[0].get("v") if r else "") or ""
    except Exception:
        raw = ""
    emails = [e.strip().lower() for e in _re.split(r"[,\s;]+", raw) if e.strip()]
    return emails or [_DEFAULT_ACCESS]


def has_access(pool, email: str) -> bool:
    return bool(email) and email.strip().lower() in _allowed(pool)


# ---- the seed: today's work log (2026-08-19). Inserted once, only if the table is empty. ----
_SEED = [
    ("Autopilot / Builds", "done", "high",
     "Fix the stalled website-build autopilot",
     "Root cause found: the cloud Anthropic account was OUT OF CREDIT (not a code bug). Pointed the cloud at "
     "the funded key via a safe DB override, and fixed real queue bugs (head-of-line deadlock, retries, "
     "not-in-pool retire). Queue drained — 43+ sites built on Opus 5.", ""),
    ("Autopilot / Builds", "done", "high",
     "Confirmed-stage build trigger",
     "From 20 Aug, a website/audit build starts only when the closer sets the CRM stage to 'confirmed'. Today "
     "everything built freely so Alfred can be trained on the trigger first.", ""),
    ("Autopilot / Builds", "done", "normal",
     "Real-time build status + live % on the booking page",
     "Booking detail page now shows website-build state (Queued / Building… X% / Live / Failed) with a live "
     "progress bar, plus the growth-audit state, auto-refreshing while a build runs. Added a Retry-build "
     "button; share_token now assigned on build so the site has a shareable link.", ""),
    ("Growth Audit", "in_progress", "high",
     "Rebuild the growth audit — clear, prospect-friendly, digital-marketing focused",
     "Rebuilt the generator to be structured + plain-language (visibility, money-searches, keyword clusters, "
     "competitors, ads, AI-readiness, 90-day plan) and to pull the FULL DataForSEO data first so it's never "
     "thin. OPEN DECISION: keep the premium 'Growth Intelligence' editorial look and just fix the confusing "
     "'Deep Findings' section, vs. use the new structured version. Awaiting Vysakh's direction.", ""),
    ("Brand & Docs", "done", "high",
     "Rebuild the DE Group brand-intro doc",
     "Digital-marketing positioning, real case studies + testimonials, correct ABN (90 134 920 228), more "
     "CTAs, sample-website portfolio (6 real client sites), and the 20 REAL Traffic Radius client logos "
     "(The Good Guys, Koala Living, Mars Campers, HUSET…) on white tiles. Lisa auto-shares it post-booking.", ""),
    ("Outreach Autopilot", "done", "high",
     "No-show recovery autopilot",
     "Re-engage prospects who agreed a meeting but didn't show: auto-SMS the finished site + brand intro "
     "(QA-gated, mobile-only, short links); the human-like responder re-books them. Reporting section on the "
     "Outbound Intelligence page. 15 no-show prospects identified; ENABLED after Vysakh approved the message.", ""),
    ("Outreach Autopilot", "done", "normal",
     "Built-site link-on-ask + QA pipelines + short links",
     "When a prospect asks for the link and a QA-passed built site exists, the responder sends site + brand "
     "intro (+ comparison) human-like. A built site passes 11+ QA checks before ANY link is sent. SMS uses "
     "clean short links (/s/…).", ""),
    ("Comparison", "done", "normal",
     "Old-vs-new website comparison — auto-generated",
     "Added headless Chromium to the cloud image (safe: a failed build keeps the old deploy live). Screenshots "
     "the prospect's current site next to the new one → clean DE-branded before/after doc. Auto-generates on "
     "each build; on-demand Generate button on the booking page. Verified (ATP Tax example).", ""),
    ("Meeting Intelligence", "done", "normal",
     "Fireflies meeting-watch + auto-detect callbacks",
     "Captures the reps' recorded calls, matches them to a prospect by the phone in the meeting title, and "
     "auto-detects callbacks (e.g. 'call back in September') via an Opus classifier → surfaced on the CRM.", ""),
    ("Domain", "in_progress", "high",
     "Migrate to trmatrix.com.au",
     "Delivered a Cloudflare DNS runbook with the exact Railway targets. www is provisioning (Railway routing "
     "it, certificate finishing). Bare trmatrix.com.au needs a redirect → www (Cloudflare root-flattening "
     "stops Railway serving the bare domain). Then switch the app's base URL + short links to trmatrix.", ""),
    ("Scheduler", "blocked", "normal",
     "Lisa on-demand Teams meeting scheduler",
     "Verified directly: the Teams onlineMeetings API is still 403. IT must grant the app "
     "OnlineMeetings.ReadWrite.All (admin consent) + a Teams Application Access Policy on lisa@digitalexpo.com.au. "
     "Calendar invites already work; the Teams join-link is the only blocked part.", ""),
    ("Data / Ops", "pending", "high",
     "Top up the Lisa-4 (website) calling pool",
     "Lisa-4 has only ~97 fresh (uncalled) prospects left — it will idle within a day. Lisa-5 has 13,000+. "
     "Refill Lisa-4's pool from the Google-Maps stock so it keeps dialling.", ""),
    ("System", "in_progress", "high",
     "Tasks / project-management page (this page)",
     "A tracked task board for Vysakh + Raj, in the side panel (owner-only), seeded from today's work, so it "
     "can be opened and discussed in meetings.", ""),
]


def seed_tasks(pool) -> int:
    ensure_tasks_table(pool)
    from . import lisa as _l
    n = _l._fetch(pool, "SELECT count(*) c FROM tasks")
    if n and (n[0].get("c") or 0) > 0:
        return 0
    with pool.connection() as conn, conn.cursor() as cur:
        for i, (cat, st, pr, title, desc, link) in enumerate(_SEED):
            cur.execute("INSERT INTO tasks (title, description, status, priority, category, link, source, sort, "
                        "done_at) VALUES (%s,%s,%s,%s,%s,%s,'Vysakh',%s,%s)",
                        (title, desc, st, pr, cat, link or None, i, ("now()" if st == "done" else None)))
        # fix done_at (the 'now()' string above won't cast); set it properly
        cur.execute("UPDATE tasks SET done_at=updated_at WHERE status='done' AND done_at IS NULL")
        conn.commit()
    return len(_SEED)


def list_tasks(pool) -> list[dict]:
    from . import lisa as _l
    ensure_tasks_table(pool)
    return _l._fetch(pool,
        "SELECT id, title, description, status, priority, category, link, notes, source, "
        "to_char(created_at,'Mon DD') created, to_char(updated_at,'Mon DD HH24:MI') updated, updated_by "
        "FROM tasks ORDER BY (status='done'), "
        "CASE priority WHEN 'high' THEN 0 WHEN 'normal' THEN 1 ELSE 2 END, sort, id") or []


def create_task(pool, *, title, description="", category="", priority="normal", status="pending", who="") -> int:
    ensure_tasks_table(pool)
    if not (title or "").strip():
        return 0
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute("INSERT INTO tasks (title, description, category, priority, status, source, updated_by) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id",
                    (title.strip()[:300], (description or "")[:4000], (category or "")[:60],
                     priority if priority in ("high", "normal", "low") else "normal",
                     status if status in STATUSES else "pending", who[:80] or "Vysakh", who[:80]))
        tid = cur.fetchone()["id"]
        conn.commit()
    return tid


_EDITABLE = {"title", "description", "status", "priority", "category", "link", "notes"}


def upsert_system_task(pool, title: str, *, status: str, description: str, priority: str = "normal",
                       category: str = "Daily readiness", source: str = "autopilot") -> None:
    """Idempotent upsert of an AUTOPILOT-maintained task, keyed by its (stable) title — so a recurring check
    updates its ONE row in place instead of piling up duplicates. The board is realtime, so the status
    change shows within seconds. Never raises."""
    ensure_tasks_table(pool)
    try:
        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT id FROM tasks WHERE title=%s ORDER BY id LIMIT 1", (title[:300],))
            row = cur.fetchone()
            if row:
                # pool uses dict_row, so fetchone() is {'id': N} — row[0] KeyError'd here and the
                # except-pass swallowed it, silently killing the readiness autopilot. Read by key.
                rid = row["id"] if isinstance(row, dict) else row[0]
                cur.execute("UPDATE tasks SET status=%s, description=%s, priority=%s, category=%s, source=%s, "
                            "updated_by='autopilot', updated_at=now(), "
                            "done_at=CASE WHEN %s='done' THEN COALESCE(done_at, now()) ELSE NULL END WHERE id=%s",
                            (status, description[:4000], priority, category, source, status, rid))
            else:
                cur.execute("INSERT INTO tasks (title, description, status, priority, category, source, updated_by, "
                            "done_at) VALUES (%s,%s,%s,%s,%s,%s,'autopilot',"
                            "CASE WHEN %s='done' THEN now() ELSE NULL END)",
                            (title[:300], description[:4000], status, priority, category, source, status))
            conn.commit()
    except Exception:
        pass


def run_daily_readiness(pool, settings) -> dict:
    """AUTOPILOT daily health — is each Lisa stocked with enough fresh data for tomorrow's calls? Writes/updates
    LIVE tasks on the board (Vysakh: "check both lisa is ready for tomorrow with enough data … update in
    realtime"). 'Fresh' = pool prospects not yet dialed. Guarded — never raises."""
    from . import lisa as _l
    out = {}
    try:
        def fresh(table):
            r = _l._fetch(pool, f"SELECT count(*) n FROM {table} p WHERE NOT EXISTS "
                          f"(SELECT 1 FROM lisa_calls lc WHERE lc.dest9=p.dest9)")
            return int(r[0]["n"]) if r else 0

        def autodial(table):
            try:
                r = _l._fetch(pool, f"SELECT autodial FROM {table} WHERE id=1")
                return bool(r[0]["autodial"]) if r else False
            except Exception:
                return False

        for key, name, table, ctrl, daily in (
            ("lisa4", "Lisa 4 (websites)", "lisa4_pool", "lisa4_control", 600),
            ("lisa5", "Lisa 5 (growth)", "lisa5_pool", "lisa5_control", 600)):
            f = fresh(table); on = autodial(ctrl); days = f / max(1, daily)
            if not on:
                status, pri = "blocked", "high"
                note = f"⚠ Dialer is OFF. {f:,} fresh prospects in the pool — turn autodial on to call tomorrow."
            elif f < daily:
                status, pri = "blocked", "high"
                note = (f"⚠ LOW — only {f:,} fresh prospects (~{days:.1f} day of calls). {name} will run dry "
                        f"tomorrow — refill the pool now.")
            elif f < 2 * daily:
                status, pri = "in_progress", "normal"
                note = f"Ready for tomorrow — {f:,} fresh (~{days:.1f} days), but top the pool up soon. Dialer on."
            else:
                status, pri = "done", "normal"
                note = f"Ready — {f:,} fresh prospects (~{days:.1f} days of calls) and the dialer is on."
            upsert_system_task(pool, f"{name} — ready for tomorrow's calls", status=status, priority=pri,
                               description=note)
            out[key] = {"fresh": f, "on": on, "status": status}
        return out
    except Exception as exc:
        return {"error": str(exc)[:120]}


def run_post_booking_readiness(pool, settings) -> dict:
    """AUTOPILOT post-booking tracker (Vysakh, repeated): for EVERY booked meeting, watch the closer's
    confirmation activity — Alfred's Aircall calls (did they answer / real conversation?) + the Fireflies
    transcript outcome — to KNOW which reveals are actually CONFIRMED vs not-reached / not-interested /
    callback, AND check the reveal asset (a built Lisa-4 website OR a Lisa-5 growth audit) is READY. Surfaces
    ONE live task on the owner /tasks board listing the exact gaps so nothing slips (a not-interested reveal
    still on the calendar, a booking with no asset, one the closer hasn't reached, or no firm time). Light
    (a few counts) + guarded; read-only except the board task; never raises, never touches the dialer."""
    from . import lisa as _l
    out = {"total": 0, "confirmed": 0, "unreached": 0, "not_interested": 0,
           "callback": 0, "asset_missing": 0, "no_time": 0}
    try:
        rows = _l._fetch(pool, """
          WITH booked AS (
            SELECT DISTINCT dest9 FROM lisa_calls
            WHERE COALESCE(meeting_agreed,false) AND dest9 IS NOT NULL
              AND started_at > now() - interval '10 days'),
          alf AS (
            SELECT right(regexp_replace(COALESCE(dest_number,''),'[^0-9]','','g'),9) AS d9,
                   max(talk_seconds) AS max_talk, bool_or(answered) AS answered, count(*) AS attempts
            FROM calls WHERE provider='aircall' AND started_at > now() - interval '12 days'
            GROUP BY 1)
          SELECT b.dest9,
                 COALESCE(NULLIF(bc.contact_name,''), NULLIF(lp.company,''), lc.company, b.dest9) AS who,
                 a.max_talk, a.answered, a.attempts, ff.outcome AS ff_outcome,
                 bc.next_action_at, bc.next_action, bc.stage, bc.audit_token, st.status AS site_status
          FROM booked b
          LEFT JOIN booked_crm bc ON bc.dest9=b.dest9
          LEFT JOIN lisa4_pool lp ON lp.dest9=b.dest9
          LEFT JOIN alf a ON a.d9=b.dest9
          LEFT JOIN LATERAL (SELECT company_name AS company FROM lisa_calls WHERE dest9=b.dest9
                             AND company_name IS NOT NULL ORDER BY started_at DESC LIMIT 1) lc ON true
          LEFT JOIN LATERAL (SELECT outcome FROM fireflies_meetings WHERE dest9=b.dest9
                             ORDER BY meeting_date DESC LIMIT 1) ff ON true
          LEFT JOIN LATERAL (SELECT status FROM lisa4_sites s WHERE s.dest9=b.dest9
                             ORDER BY (status='built') DESC, id DESC LIMIT 1) st ON true
          WHERE COALESCE(bc.stage,'') NOT IN ('lost','won','no_show')
        """)
        crit, warn, minor = [], [], []   # severity tiers so the board leads with what matters
        for r in rows:
            out["total"] += 1
            mt = r.get("max_talk") or 0
            attempts = r.get("attempts") or 0
            reached = bool(r.get("answered")) and mt >= 40
            ffo = (r.get("ff_outcome") or "").strip().lower()
            asset_ok = bool(r.get("audit_token")) or (r.get("site_status") == "built")
            who = str(r.get("who"))[:40]
            if not asset_ok:
                out["asset_missing"] += 1
                crit.append(f"NO ASSET (reveal not built): {who}")
            if ffo in ("not_interested", "already_has_vendor"):
                out["not_interested"] += 1
                crit.append(f"NOT-INTERESTED on confirm call — remove/rebook: {who}")
            elif ffo == "callback" or (not reached and attempts > 0):
                out["callback"] += 1
                warn.append(f"awaiting confirm ({'callback requested' if ffo == 'callback' else 'rang, no answer'}): {who}")
            elif not reached and attempts == 0:
                out["unreached"] += 1
                warn.append(f"closer has NOT called yet: {who}")
            else:
                out["confirmed"] += 1
            # AUTOPILOT RETRY next-action (standard CRM behaviour): until the closer actually REACHES an
            # unconfirmed booking, the CRM next-action must say "call again to confirm" — a voicemail must
            # never look done. Only writes when next_action is empty or already a retry line (never clobbers
            # a genuine reschedule/next-step), and clears itself once the prospect is reached.
            stage = (r.get("stage") or "").strip().lower()
            cur_next = (r.get("next_action") or "").strip()
            unconfirmed = stage in ("", "new", "confirming")
            is_retry_line = cur_next.startswith("Call again") or cur_next.startswith("Closer to call")
            retry_msg = None
            if unconfirmed and not reached and ffo not in ("confirmed", "reschedule", "callback"):
                retry_msg = (f"Call again to confirm — closer hasn't reached {who} yet "
                             f"(last attempt: voicemail/no answer; {attempts} tried)") if attempts > 0 \
                            else f"Closer to call {who} to confirm the reveal"
            try:
                if retry_msg and (not cur_next or is_retry_line):
                    with pool.connection() as _cn, _cn.cursor() as _cu:
                        _cu.execute(
                            "UPDATE booked_crm SET next_action=%s, "
                            "  next_action_at=COALESCE(next_action_at, now() + interval '2 hours'), "
                            "  updated_by='closer-retry', updated_at=now() "
                            "WHERE dest9=%s AND COALESCE(next_action,'') IS DISTINCT FROM %s",
                            (retry_msg, r["dest9"], retry_msg))
                        _cn.commit()
                elif reached and is_retry_line:          # reached now — drop the stale retry nag
                    with pool.connection() as _cn, _cn.cursor() as _cu:
                        _cu.execute("UPDATE booked_crm SET next_action=NULL, updated_at=now() "
                                    "WHERE dest9=%s AND next_action=%s", (r["dest9"], cur_next))
                        _cn.commit()
            except Exception:
                pass
            if not r.get("next_action_at"):
                out["no_time"] += 1
                minor.append(f"no firm reveal TIME set: {who}")
        head = (f"{out['confirmed']} confirmed · {out['not_interested']} not-interested · "
                f"{out['callback']} awaiting-confirm · {out['unreached']} not-yet-called · "
                f"{out['asset_missing']} missing-asset · {out['no_time']} no-time "
                f"(of {out['total']} active bookings)")
        # critical first, then awaiting-confirm, then no-time; de-dupe preserving that order
        seen = set(); uniq = [g for g in (crit + warn + minor) if not (g in seen or seen.add(g))]
        pri = "high" if (out["asset_missing"] or out["not_interested"]) else "normal"
        status = ("blocked" if out["asset_missing"]
                  else "in_progress" if (out["callback"] or out["unreached"] or out["not_interested"] or out["no_time"])
                  else "done")
        desc = head + (("\n\nATTENTION:\n- " + "\n- ".join(uniq[:50])) if uniq
                       else "\n\nAll booked reveals are confirmed and their assets are ready.")
        upsert_system_task(pool, "Post-booking confirmations & reveal assets",
                           status=status, priority=pri, description=desc)
        return out
    except Exception as exc:
        return {"error": str(exc)[:120]}


def update_task(pool, task_id: int, fields: dict, who: str) -> None:
    ensure_tasks_table(pool)
    sets = {k: v for k, v in (fields or {}).items() if k in _EDITABLE}
    if not task_id or not sets:
        return
    cols = ", ".join(f"{k}=%s" for k in sets)
    vals = list(sets.values())
    done_touch = ", done_at = CASE WHEN %s='done' THEN COALESCE(done_at, now()) ELSE NULL END" if "status" in sets else ""
    with pool.connection() as conn, conn.cursor() as cur:
        if done_touch:
            cur.execute(f"UPDATE tasks SET {cols}, updated_by=%s, updated_at=now(){done_touch} WHERE id=%s",
                        (*vals, who[:80], sets["status"], task_id))
        else:
            cur.execute(f"UPDATE tasks SET {cols}, updated_by=%s, updated_at=now() WHERE id=%s",
                        (*vals, who[:80], task_id))
        conn.commit()
