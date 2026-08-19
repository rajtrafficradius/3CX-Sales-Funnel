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
