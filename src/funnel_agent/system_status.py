"""Real-time SYSTEM/ENGINE status — every tile is derived from a REAL signal (last-activity
timestamps, recent counts, error rates, live API balances), never a hardcoded 'OK'. If a signal
can't be read, the tile says 'unknown' (amber) rather than a false green — a status page that lies
is worse than none (Vysakh, 2026-08-20).

Each check returns: {key, name, group, state, detail, last, metric}
  state: 'ok' | 'warn' | 'down' | 'idle' | 'unknown'
Fully guarded — a failing check degrades to 'unknown', never raises."""
from __future__ import annotations
from datetime import datetime, timezone
from zoneinfo import ZoneInfo


def _one(pool, sql, args=()):
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(sql, args)
        r = cur.fetchone()
        return r


def _g(r, k, default=None):
    if r is None:
        return default
    try:
        return r[k]
    except Exception:
        try:
            return r.get(k, default)
        except Exception:
            return default


def _mins_since(ts) -> float | None:
    if not ts:
        return None
    try:
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - ts).total_seconds() / 60.0
    except Exception:
        return None


def _fmt_ago(mins) -> str:
    if mins is None:
        return "never"
    if mins < 1:
        return "just now"
    if mins < 90:
        return f"{int(mins)}m ago"
    if mins < 60 * 48:
        return f"{int(mins/60)}h ago"
    return f"{int(mins/1440)}d ago"


def _au_calling_hours() -> bool:
    """AU calling window Mon–Sat 09:00–19:00 Australia/Sydney (when engines SHOULD be active)."""
    now = datetime.now(ZoneInfo("Australia/Sydney"))
    return now.weekday() < 6 and 9 <= now.hour < 19


# Lisa outbound lines (authoritative, per Retell) — used to detect real dial activity.
_L4 = ("468030256", "489266405", "495044526", "468091513")
_L5 = ("468096730", "468008827")


def _tile(key, name, group, state, detail, last=None, metric=None):
    return {"key": key, "name": name, "group": group, "state": state,
            "detail": detail, "last": last, "metric": metric}


def compute(pool, settings) -> dict:
    tiles: list[dict] = []
    hrs = _au_calling_hours()

    def guard(fn):
        try:
            return fn()
        except Exception as exc:
            return ("unknown", f"check failed: {str(exc)[:80]}", None, None)

    # ---------- OUTBOUND DIALER (Lisa 4 + 5) ----------
    def _dialer():
        r = _one(pool, "SELECT count(*) FILTER (WHERE created_at>now()-interval '15 min') n15, "
                       "count(*) FILTER (WHERE created_at>now()-interval '60 min') n60, "
                       "max(created_at) last FROM lisa_calls")
        n15, n60, last = _g(r, "n15", 0), _g(r, "n60", 0), _g(r, "last")
        mins = _mins_since(last)
        if hrs:
            state = "ok" if n15 > 0 else ("warn" if n60 > 0 else "down")
        else:
            state = "idle"  # off-hours: not expected to dial
        det = f"{n15} calls last 15m · {n60}/hr" + ("" if hrs else " · off-hours (paused)")
        return state, det, _fmt_ago(mins), n60
    tiles.append(_tile("dialer", "Outbound dialer (Lisa 4+5)", "Calling", *guard(_dialer)))

    # ---------- RETELL WEBHOOKS (call completion) ----------
    def _webhooks():
        r = _one(pool, "SELECT count(*) FILTER (WHERE status='analyzed' AND created_at>now()-interval '60 min') ok60, "
                       "count(*) FILTER (WHERE status='ongoing' AND created_at<now()-interval '15 min') stale FROM lisa_calls")
        ok60, stale = _g(r, "ok60", 0), _g(r, "stale", 0)
        # stale 'ongoing' backlog = the classic webhook-jam symptom
        if stale >= 3:
            state = "down"
        elif hrs and ok60 == 0:
            state = "warn"
        else:
            state = "ok"
        return state, f"{ok60} completed last hr · {stale} stuck 'ongoing'", None, stale
    tiles.append(_tile("retell_webhook", "Retell webhooks (completion)", "Calling", *guard(_webhooks)))

    # ---------- INBOUND ENGINE ----------
    def _inbound():
        # inbound = a call whose from_number is NOT one of our outbound lines (prospect called us)
        r = _one(pool, "SELECT max(created_at) last, count(*) FILTER (WHERE created_at>now()-interval '24 hours') n24 "
                       "FROM lisa_calls WHERE right(regexp_replace(COALESCE(from_number,''),'[^0-9]','','g'),9) = dest9")
        last, n24 = _g(r, "last"), _g(r, "n24", 0)
        mins = _mins_since(last)
        # inbound is event-driven; 'ok' if we've seen inbound in the last day, else idle (not necessarily broken)
        state = "ok" if (mins is not None and mins < 60 * 24) else "idle"
        return state, f"{n24} inbound last 24h", _fmt_ago(mins), n24
    tiles.append(_tile("inbound", "Inbound call+SMS engine", "Calling", *guard(_inbound)))

    # ---------- CLASSIFIER (call outcome tagging) ----------
    def _classifier():
        r = _one(pool, "SELECT count(*) FILTER (WHERE status='analyzed' AND call_outcome IS NOT NULL "
                       "AND updated_at>now()-interval '2 hours') tagged, "
                       "count(*) FILTER (WHERE status='analyzed' AND call_outcome IS NULL "
                       "AND created_at>now()-interval '2 hours') untagged FROM lisa_calls")
        tagged, untagged = _g(r, "tagged", 0), _g(r, "untagged", 0)
        if tagged == 0 and untagged >= 10:
            state = "down"          # analyzed calls piling up unclassified
        elif untagged > tagged and untagged >= 5:
            state = "warn"
        else:
            state = "ok" if (tagged or not hrs) else "idle"
        return state, f"{tagged} tagged / {untagged} pending (2h)", None, untagged
    tiles.append(_tile("classifier", "Call classifier", "Intelligence", *guard(_classifier)))

    # ---------- WEBSITE BUILDER (Lisa 4 reveals) ----------
    def _site_builder():
        r = _one(pool, "SELECT count(*) FILTER (WHERE status='built' AND built_at>now()-interval '24 hours') built, "
                       "count(*) FILTER (WHERE status='error' AND COALESCE(built_at,created_at)>now()-interval '24 hours') err, "
                       "count(*) FILTER (WHERE status='building' AND building_at<now()-interval '40 min') stuck, "
                       "max(built_at) last FROM lisa4_sites WHERE COALESCE(kind,'reveal')='reveal'")
        built, err, stuck = _g(r, "built", 0), _g(r, "err", 0), _g(r, "stuck", 0)
        last = _mins_since(_g(r, "last"))
        if stuck >= 1:
            state = "warn"          # a build hung past the reaper window
        elif err > 0 and built == 0:
            state = "down"
        elif err > 0:
            state = "warn"
        else:
            state = "ok"
        return state, f"{built} built / {err} error (24h)" + (f" · {stuck} stuck" if stuck else ""), _fmt_ago(last), err
    tiles.append(_tile("site_builder", "Website builder", "Builders", *guard(_site_builder)))

    # ---------- AUDIT BUILDER (growth audits) ----------
    def _audit_builder():
        r = _one(pool, "SELECT count(*) FILTER (WHERE built_at>now()-interval '48 hours') n, max(built_at) last "
                       "FROM lisa4_sites WHERE kind='audit'")
        n, last = _g(r, "n", 0), _mins_since(_g(r, "last"))
        state = "ok" if (last is not None and last < 60 * 72) else "idle"
        return state, f"{n} audits built (48h)", _fmt_ago(last), n
    tiles.append(_tile("audit_builder", "Growth-audit builder", "Builders", *guard(_audit_builder)))

    # ---------- COMPARISON BUILDER ----------
    def _cmp_builder():
        r = _one(pool, "SELECT count(*) FILTER (WHERE built_at>now()-interval '48 hours') n, max(built_at) last "
                       "FROM lisa4_sites WHERE kind='comparison'")
        n, last = _g(r, "n", 0), _mins_since(_g(r, "last"))
        state = "ok" if (last is not None and last < 60 * 72) else "idle"
        return state, f"{n} comparisons (48h)", _fmt_ago(last), n
    tiles.append(_tile("cmp_builder", "Old-vs-new comparison builder", "Builders", *guard(_cmp_builder)))

    # ---------- NO-SHOW RECOVERY ----------
    def _noshow():
        from . import noshow_recovery as _nsr
        enabled = True
        try:
            r = _one(pool, "SELECT v FROM crm_config WHERE k='noshow_recovery_enabled'")
            if r is not None and str(_g(r, "v", "")).lower() in ("false", "0", "off", "no"):
                enabled = False
        except Exception:
            pass
        win = _nsr.send_window_open()
        r = _one(pool, "SELECT count(*) FILTER (WHERE recovery_sent_at>now()-interval '24 hours') n, "
                       "max(recovery_sent_at) last FROM booked_crm")
        n, last = _g(r, "n", 0), _mins_since(_g(r, "last"))
        if not enabled:
            state = "warn"
            det = "DISABLED"
        else:
            state = "ok"
            det = f"{n} recoveries sent (24h) · window {'open' if win else 'closed'}"
        return state, det, _fmt_ago(last), n
    tiles.append(_tile("noshow", "No-show recovery", "Autopilot", *guard(_noshow)))

    # ---------- BRAND-INTRO AUTOPILOT ----------
    def _brand():
        r = _one(pool, "WITH b AS (SELECT DISTINCT dest9 FROM lisa_calls WHERE COALESCE(meeting_agreed,false) "
                       "AND created_at>now()-interval '7 days') "
                       "SELECT count(*) total, count(bc.brand_intro_sent_at) sent, "
                       "max(bc.brand_intro_sent_at) last "
                       "FROM b LEFT JOIN booked_crm bc ON bc.dest9=b.dest9")
        total, sent = _g(r, "total", 0), _g(r, "sent", 0)
        last = _mins_since(_g(r, "last"))
        pct = int(100 * sent / total) if total else 0
        state = "ok" if pct >= 80 else ("warn" if pct >= 40 else ("down" if total else "idle"))
        return state, f"{sent}/{total} bookings covered ({pct}%)", _fmt_ago(last), pct
    tiles.append(_tile("brand_intro", "Brand-intro autopilot", "Autopilot", *guard(_brand)))

    # ---------- FIRELIES WATCH ----------
    def _fireflies():
        r = _one(pool, "SELECT v FROM crm_config WHERE k='fireflies_last_run'")
        v = _g(r, "v")
        last = None
        try:
            last = _mins_since(datetime.fromisoformat(str(v))) if v else None
        except Exception:
            last = None
        if last is None:
            state, det = "unknown", "no run recorded"
        elif last < 30:
            state, det = "ok", "polling"
        elif last < 120:
            state, det = "warn", "slow"
        else:
            state, det = "down", "stalled"
        return state, det, _fmt_ago(last), None
    tiles.append(_tile("fireflies", "Fireflies meeting-watch", "Intelligence", *guard(_fireflies)))

    # ---------- AIRCALL INGEST ----------
    def _aircall():
        try:
            r = _one(pool, "SELECT max(started_at) last, count(*) FILTER (WHERE started_at>now()-interval '24 hours') n "
                           "FROM calls WHERE provider='aircall'")
        except Exception:
            return "unknown", "calls table unavailable", None, None
        last, n = _mins_since(_g(r, "last")), _g(r, "n", 0)
        state = "ok" if (last is not None and last < 60 * 24) else "idle"
        return state, f"{n} Aircall calls ingested (24h)", _fmt_ago(last), n
    tiles.append(_tile("aircall", "Aircall ingest", "Calling", *guard(_aircall)))

    # ---------- REFRESH-LOOP HEARTBEAT ----------
    def _loop():
        r = _one(pool, "SELECT max(updated_at) last FROM lisa_calls")
        last = _mins_since(_g(r, "last"))
        if last is None:
            state = "unknown"
        elif last < 20:
            state = "ok"
        elif last < 90:
            state = "warn"
        else:
            state = "down" if hrs else "idle"
        return state, "engine loop writing", _fmt_ago(last), None
    tiles.append(_tile("loop", "Refresh loop heartbeat", "Core", *guard(_loop)))

    # ---------- TWILIO BALANCE ----------
    def _twilio():
        from . import cost as _cost
        ts = _cost.twilio_status(settings, days=1) or {}
        bal = ts.get("balance")
        if bal is None:
            return "unknown", "balance API unavailable", None, None
        # twilio_status() returns only the raw balance — derive LOW here from the configured threshold
        # (the old ts.get('low') was never set, so an $11 balance showed 'ok': 2026-08-30 bug).
        thr = float((_cost._prices(pool) or {}).get("twilio_low_balance") or 50.0)
        low = bal < thr
        state = "down" if bal < 20 else ("warn" if low else "ok")
        return state, f"${bal:.2f} balance" + (f" · LOW (<${thr:.0f}) — TOP UP" if low else ""), None, round(bal, 2)
    tiles.append(_tile("twilio", "Twilio balance (SMS/voice)", "Billing", *guard(_twilio)))

    # ---------- DATAFORSEO BALANCE ----------
    def _dfs():
        if not getattr(settings, "dataforseo_enabled", False):
            return "idle", "not configured", None, None
        from .enrichment.dataforseo import DataForSEOClient
        c = DataForSEOClient(settings)
        try:
            b = c.balance() or {}
        finally:
            try:
                c.close()
            except Exception:
                pass
        bal = b.get("balance")
        if bal is None:
            return "unknown", "balance API unavailable", None, None
        state = "down" if bal < 5 else ("warn" if bal < 15 else "ok")
        return state, f"${bal:.2f} balance", None, round(bal, 2)
    tiles.append(_tile("dataforseo", "DataForSEO balance (audits)", "Billing", *guard(_dfs)))

    # ---------- LISA 4 DIALER (own toggle + line set + pool) ----------
    def _l4_dialer():
        from . import lisa4 as _l4
        on = False
        try:
            on = _l4.get_lisa4_autodial(pool, settings)
        except Exception:
            pass
        r = _one(pool, f"SELECT count(*) FILTER (WHERE {_l4._L4_PRED} AND created_at>now()-interval '60 min' "
                       f"AND from_number ~ %s) n60, "
                       "count(*) n FROM lisa_calls WHERE created_at>now()-interval '24 hours'",
                 (_l4.L4_LINE_RX, _l4.L4_LINE_RX, _l4.L4_LINE_RX))
        n60 = _g(r, "n60", 0)
        pl = _one(pool, "SELECT count(*) n FROM lisa4_pool lp WHERE NOT EXISTS "
                        "(SELECT 1 FROM lisa_calls lc WHERE lc.dest9=lp.dest9)")
        depth = _g(pl, "n", 0)
        if not on:
            state = "warn"
            det = "toggle OFF"
        elif hrs:
            state = "ok" if n60 > 0 else "warn"
            det = f"ON · {n60} dials/hr"
        else:
            state = "idle"
            det = "ON · off-hours"
        det += f" · pool {depth:,} unworked"
        if on and depth < 300:
            state = "warn" if state in ("ok", "idle") else state
            det += " · LOW"
        return state, det, None, depth
    tiles.append(_tile("lisa4_dialer", "Lisa 4 dialer (websites)", "Calling", *guard(_l4_dialer)))

    # ---------- LISA 5 DIALER ----------
    def _l5_dialer():
        from . import lisa4 as _l4
        from . import lisa5 as _l5
        on = False
        try:
            on = _l5.get_lisa5_autodial(pool, settings)
        except Exception:
            pass
        r = _one(pool, f"SELECT count(*) FILTER (WHERE {_l4._L5_PRED} AND created_at>now()-interval '60 min') n60 "
                       "FROM lisa_calls WHERE created_at>now()-interval '24 hours'",
                 (_l4.L5_LINE_RX, _l4.L5_LINE_RX))
        n60 = _g(r, "n60", 0)
        pl = _one(pool, "SELECT count(*) n FROM lisa5_pool lp WHERE NOT EXISTS "
                        "(SELECT 1 FROM lisa_calls lc WHERE lc.dest9=lp.dest9)")
        depth = _g(pl, "n", 0)
        if not on:
            state, det = "warn", "toggle OFF"
        elif hrs:
            state = "ok" if n60 > 0 else "warn"
            det = f"ON · {n60} dials/hr"
        else:
            state, det = "idle", "ON · off-hours"
        det += f" · pool {depth:,} unworked"
        return state, det, None, depth
    tiles.append(_tile("lisa5_dialer", "Lisa 5 dialer (D&B growth)", "Calling", *guard(_l5_dialer)))

    # ---------- EMMA SCHEDULER ----------
    def _emma():
        q = _one(pool, "SELECT count(*) FILTER (WHERE status='draft') draft, "
                       "count(*) FILTER (WHERE status='needs-info') ni, "
                       "count(*) FILTER (WHERE status='scheduled') sched, "
                       "max(staff_notified_at) last_alert FROM emma_meetings "
                       "WHERE booked_at > now()-interval '7 days'")
        draft, ni = _g(q, "draft", 0), _g(q, "ni", 0)
        last_alert = _mins_since(_g(q, "last_alert"))
        gc = bool(getattr(settings, "graph_configured", False))
        state = "ok" if gc else "warn"
        det = f"{draft} drafts · {ni} needs-info (7d)" + ("" if gc else " · Graph creds missing")
        return state, det, _fmt_ago(last_alert), draft + ni
    tiles.append(_tile("emma", "Emma scheduler + staff alerts", "Autopilot", *guard(_emma)))

    # ---------- SMS ENGINE ----------
    def _sms():
        r = _one(pool, "SELECT count(*) FILTER (WHERE direction='outbound' AND created_at>now()-interval '24 hours') o24, "
                       "count(*) FILTER (WHERE direction='inbound' AND created_at>now()-interval '24 hours') i24, "
                       "max(created_at) last FROM lisa_sms")
        o24, i24 = _g(r, "o24", 0), _g(r, "i24", 0)
        last = _mins_since(_g(r, "last"))
        state = "ok" if (last is not None and last < 60 * 24) else "idle"
        return state, f"{o24} out / {i24} in (24h)", _fmt_ago(last), o24
    tiles.append(_tile("sms", "SMS engine (Twilio 2-way)", "Calling", *guard(_sms)))

    # ---------- BOOKING PIPELINE TODAY ----------
    def _bookings():
        r = _one(pool, "SELECT count(*) FILTER (WHERE meeting_agreed AND (started_at AT TIME ZONE "
                       "'Australia/Melbourne')::date=(now() AT TIME ZONE 'Australia/Melbourne')::date) today, "
                       "count(*) FILTER (WHERE meeting_agreed AND started_at>now()-interval '7 days') week "
                       "FROM lisa_calls")
        today, week = _g(r, "today", 0), _g(r, "week", 0)
        state = "ok" if week else "idle"
        return state, f"{today} booked today · {week} this week", None, today
    tiles.append(_tile("bookings", "Booking capture (G1-gated)", "Intelligence", *guard(_bookings)))

    # ---------- DATABASE ----------
    def _db():
        r = _one(pool, "SELECT pg_database_size(current_database()) sz, "
                       "(SELECT count(*) FROM pg_stat_activity WHERE state='active') act")
        sz = _g(r, "sz", 0)
        act = _g(r, "act", 0)
        gb = sz / 1e9
        state = "ok" if gb < 8 else "warn"
        return state, f"{gb:.1f} GB · {act} active queries", None, round(gb, 2)
    tiles.append(_tile("db", "Postgres (Railway)", "Core", *guard(_db)))

    order = {"down": 0, "warn": 1, "unknown": 2, "idle": 3, "ok": 4}
    summary = {"down": 0, "warn": 1, "unknown": 2, "idle": 3, "ok": 4}
    counts = {s: 0 for s in summary}
    for t in tiles:
        counts[t["state"]] = counts.get(t["state"], 0) + 1
    overall = "down" if counts.get("down") else ("warn" if counts.get("warn") or counts.get("unknown") else "ok")
    tiles.sort(key=lambda t: order.get(t["state"], 5))
    # ---------- 24h PULSE (hourly activity for the engine-room chart) ----------
    pulse = []
    try:
        rows = _one_all(pool, "SELECT date_trunc('hour', created_at) h, "
                              "count(*) FILTER (WHERE right(regexp_replace(COALESCE(from_number,''),'[^0-9]','','g'),9) <> dest9) dials, "
                              "count(*) FILTER (WHERE right(regexp_replace(COALESCE(from_number,''),'[^0-9]','','g'),9) = dest9) inbound "
                              "FROM lisa_calls WHERE created_at > now()-interval '24 hours' GROUP BY 1 ORDER BY 1")
        smsrows = {str(r["h"]): r for r in _one_all(pool, "SELECT date_trunc('hour', created_at) h, count(*) n "
                                                          "FROM lisa_sms WHERE created_at > now()-interval '24 hours' GROUP BY 1")}
        for r in rows:
            k = str(r["h"])
            pulse.append({"hour": k, "dials": int(r.get("dials") or 0), "inbound": int(r.get("inbound") or 0),
                          "sms": int((smsrows.get(k) or {}).get("n") or 0)})
    except Exception:
        pulse = []
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "overall": overall,
        "counts": counts,
        "engines": tiles,
        "pulse": pulse,
        "calling_hours": hrs,
    }


def _one_all(pool, sql, args=()):
    from . import lisa as _l
    return _l._fetch(pool, sql, args) or []


# ------------------------------------------------------------------ codebase inventory (the real engines)
# Curated role/subsystem for every meaningful module in src/funnel_agent — the .py files ARE the engines.
# LOC is measured live at call time so the numbers are always the truth of the deployed code.
_MODULE_REGISTRY = {
    # file (relative to funnel_agent)        (subsystem, role, live-tile key or None)
    "lisa.py":                  ("Calling", "Lisa core: dial machinery, briefs, SMS, classifier, postcall", "dialer"),
    "lisa4.py":                 ("Calling", "Lisa 4: website-selling dialer, pool, AI site designer", "lisa4_dialer"),
    "lisa5.py":                 ("Calling", "Lisa 5: D&B growth-audit dialer", "lisa5_dialer"),
    "ingest.py":                ("Calling", "3CX call ledger ingest", None),
    "aircall/ingest.py":        ("Calling", "Aircall call ingest", "aircall"),
    "aircall/transcribe.py":    ("Calling", "Aircall recording transcription", None),
    "aircall/calls.py":         ("Calling", "Aircall API — call fetch", None),
    "aircall/api.py":           ("Calling", "Aircall API client", None),
    "threecx/api.py":           ("Calling", "3CX API client", None),
    "threecx/cdr.py":           ("Calling", "3CX CDR reader", None),
    "threecx/recordings.py":    ("Calling", "3CX recording fetcher", None),
    "threecx/discover.py":      ("Calling", "3CX endpoint discovery", None),
    "transcribe.py":            ("Intelligence", "Whisper STT for human-BDE recordings", None),
    "classify/classifier.py":   ("Intelligence", "Call-outcome classifier (BANT, bookings)", "classifier"),
    "classify/prompt.py":       ("Intelligence", "Classifier prompt + rules", None),
    "classify/schema.py":       ("Intelligence", "Classification schema", None),
    "classify/memory.py":       ("Intelligence", "Company booking memory (dedupe)", None),
    "qa/gates.py":              ("Intelligence", "QA gates: G1 booking verdict", "bookings"),
    "qa/outbound.py":           ("Intelligence", "Outbound QA checks", None),
    "qa/dynvars.py":            ("Intelligence", "Dynamic-variable safety (G8-G10)", None),
    "bde_capture.py":           ("Intelligence", "Alfred call capture → CRM (Aircall+Fireflies)", None),
    "fireflies.py":             ("Intelligence", "Fireflies meeting watch", "fireflies"),
    "audit.py":                 ("Intelligence", "SEO/competitor audit model + relevance gate", None),
    "audit_signals.py":         ("Intelligence", "PageSpeed/CWV + technical signals (DataForSEO)", None),
    "competitor.py":            ("Intelligence", "Competitor discovery + share-of-voice", None),
    "enrich.py":                ("Enrichment", "Enrichment orchestrator (per-domain full)", None),
    "enrichment/website.py":    ("Enrichment", "Website scrape + media/logo extraction", None),
    "enrichment/dataforseo.py": ("Enrichment", "DataForSEO client (SERP/Labs/keywords)", "dataforseo"),
    "enrichment/apollo.py":     ("Enrichment", "Apollo decision-maker lookup", None),
    "enrichment/whois_lookup.py": ("Enrichment", "WHOIS domain intel", None),
    "enrichment/semrush.py":    ("Enrichment", "SEMrush metrics (gap-fill)", None),
    "enrichment/business_intel.py": ("Enrichment", "Business intel scrape", None),
    "gmaps.py":                 ("Enrichment", "Google Places sweeps + GBP photos", None),
    "website_finder.py":        ("Enrichment", "Website finder for no-domain prospects", None),
    "tracking.py":              ("Enrichment", "Ad/pixel tracking detection", None),
    "growth_audit.py":          ("Builders", "Growth-audit report writer (Opus)", "audit_builder"),
    "comparison.py":            ("Builders", "Old-vs-new site comparison builder", "cmp_builder"),
    "quote.py":                 ("Builders", "Quote document builder", None),
    "reveal_guide.py":          ("Builders", "Reveal meeting guide builder", None),
    "site_qa.py":               ("Builders", "Built-site QA checks", None),
    "crm.py":                   ("Autopilot", "Booked CRM + booking-docs autopilot", None),
    "emma.py":                  ("Autopilot", "Emma: invites, reminders, staff alerts", "emma"),
    "noshow_recovery.py":       ("Autopilot", "No-show recovery SMS campaign", "noshow"),
    "whatsapp.py":              ("Autopilot", "WhatsApp nurture (dry-run)", None),
    "messages.py":              ("Autopilot", "Inbound SMS classifier + booking firmer", "sms"),
    "recalls.py":               ("Autopilot", "Callback / recall scheduling", None),
    "retry.py":                 ("Autopilot", "Retry ladder for unanswered calls", None),
    "fresh_alloc.py":           ("Autopilot", "GAds fresh-prospect calendar allocator", None),
    "tasks.py":                 ("Autopilot", "Daily readiness + post-booking tracker", None),
    "push.py":                  ("Autopilot", "Chrome push alerts (new bookings)", None),
    "emailer.py":               ("Autopilot", "Report emailer", None),
    "aggregate.py":             ("Core", "Funnel aggregation (booked/qualified)", "loop"),
    "pipeline.py":              ("Core", "Classification pipeline window", None),
    "pipeline2.py":             ("Core", "Pipeline v2 (4-pipeline design)", None),
    "cli.py":                   ("Core", "Process entrypoints: loops, dial, refresh", None),
    "config.py":                ("Core", "Settings (env + defaults)", None),
    "auth.py":                  ("Core", "Login, roles, page access", None),
    "roster.py":                ("Core", "BDE roster sync", None),
    "sources.py":               ("Core", "Call-source composition (3CX+Aircall)", None),
    "db/analytics.py":          ("Core", "Analytics DB access", "db"),
    "db/migrate.py":            ("Core", "Schema migration", None),
    "db/source.py":             ("Core", "Source DB access", None),
    "__main__.py":              ("Core", "Package entrypoint (python -m funnel_agent)", None),
    "logging.py":               ("Core", "Structured logging setup", None),
    "shortlink.py":             ("Autopilot", "Short links for SMS'd audit/site URLs (/s/)", None),
    "qa/audit.py":              ("Intelligence", "QA event audit trail (gate decisions)", None),
    "threecx/transcripts.py":   ("Calling", "3CX transcript fetcher", None),
    "dashboard/app.py":         ("Surfaces", "FastAPI web app — every console + API", None),
    "system_status.py":         ("Surfaces", "Engine Room signals (this page)", None),
    "system_blueprint.py":      ("Surfaces", "System map nodes+edges", None),
    "cost.py":                  ("Surfaces", "Cost Intelligence engine", None),
    "report.py":                ("Surfaces", "Daily report generator", None),
    "next_call.py":             ("Surfaces", "Next-call coaching intelligence", None),
    "rpc.py":                   ("Surfaces", "RPC-connect intelligence", None),
    "calendar.py":              ("Surfaces", "Calendar engine", None),
    "prospects.py":             ("Surfaces", "Prospect DB pages", None),
    "companies.py":             ("Surfaces", "Companies table loader", None),
}


# Every operator console (dashboard/static/*.html) — the SURFACES of the system.
_PAGE_REGISTRY = {
    "index.html": "Main funnel dashboard (React) — live floor, funnel, leaderboards",
    "tv.html": "TV wall mode — the floor on the big screen",
    "lisa.html": "Outbound Intelligence console — Lisa floor cards + controls",
    "lisa-crm.html": "Booked CRM — every Lisa booking through to close",
    "crm-record.html": "Single-prospect CRM record",
    "lisa-data.html": "Lisa data browser",
    "meetings.html": "Meetings board",
    "next-calls.html": "Next-call coaching queue",
    "calendar.html": "Calendar — bookings + dial schedule",
    "pipeline.html": "Pipeline v1", "pipeline2.html": "Pipeline v2 (4-pipeline design)",
    "prospect.html": "One-prospect intelligence page",
    "database.html": "Prospect database explorer",
    "coaching.html": "Coaching intelligence",
    "agency-rpc.html": "Agency & RPC intelligence",
    "call.html": "Single-call drilldown",
    "cost.html": "Cost Intelligence (this suite)",
    "status.html": "Engine Room (this page)",
    "blueprint.html": "System blueprint graph",
    "tasks.html": "Tasks / readiness board",
    "admin.html": "Admin — users, toggles, config",
    "login.html": "Sign-in",
}

# Long-running PROCESSES (from docker-entrypoint.sh) + external SERVICES the engines drive.
_PROCESSES = [
    {"name": "Web app (uvicorn)", "role": "Serves every console + API + webhooks", "loop": "always-on"},
    {"name": "Refresh loop", "role": "Ingest → transcribe → classify → aggregate → capture, every 60s", "loop": "60s"},
    {"name": "Lisa dial loop", "role": "Lisa-1 dial cadence (gated off)", "loop": "25s"},
    {"name": "Lisa 4+5 autopilot loop", "role": "Pool prep + builds + ONE dial per agent per tick, self-healing reapers", "loop": "25s"},
    {"name": "init-db migrator", "role": "Schema migration on every deploy (idempotent)", "loop": "on deploy"},
]
_EXTERNALS = [
    {"name": "Retell AI", "role": "Voice AI — Lisa's calls (agents, webhooks)"},
    {"name": "Twilio", "role": "Telephony trunk + 2-way SMS + numbers"},
    {"name": "OpenAI", "role": "Classifier (GPT-4o-mini) + Whisper STT"},
    {"name": "Anthropic Claude", "role": "Site / audit / comparison builders + seeds"},
    {"name": "DataForSEO", "role": "SERP, keywords, Lighthouse, ads transparency"},
    {"name": "Google Places", "role": "Business sweeps + GBP photos"},
    {"name": "Apollo", "role": "Decision-maker enrichment"},
    {"name": "Aircall", "role": "Human-BDE telephony (Alfred, Ben)"},
    {"name": "3CX", "role": "Human-BDE PBX + CDR + recordings"},
    {"name": "Fireflies", "role": "Meeting recording + transcripts"},
    {"name": "Microsoft Graph", "role": "Emma's mailbox + calendar invites"},
    {"name": "Railway", "role": "Cloud host + Postgres"},
]


def module_inventory() -> dict:
    """Walk the deployed source tree and return the REAL engine inventory: every .py module with live
    line-counts, mapped to subsystem + role from the curated registry (unknown files still listed).
    Guarded — {} on any error."""
    import os as _os
    import re as _re
    try:
        base = _os.path.dirname(_os.path.abspath(__file__))
        mods = []
        total_loc = 0
        for root, dirs, files in _os.walk(base):
            dirs[:] = [d for d in dirs if d not in ("__pycache__",)]
            for fn in files:
                if not fn.endswith(".py") or fn == "__init__.py":
                    continue
                p = _os.path.join(root, fn)
                rel = _os.path.relpath(p, base).replace("\\", "/")
                try:
                    with open(p, "rb") as fh:
                        loc = sum(1 for _ in fh)
                except Exception:
                    loc = 0
                total_loc += loc
                grp, role, tile = _MODULE_REGISTRY.get(rel, ("Other", "", None))
                mods.append({"file": rel, "loc": loc, "group": grp, "role": role, "tile": tile})
        mods.sort(key=lambda m: -m["loc"])
        pages = []
        try:
            sd = _os.path.join(base, "dashboard", "static")
            for f in sorted(_os.listdir(sd)):
                if f.endswith(".html"):
                    try:
                        with open(_os.path.join(sd, f), "rb") as fh:
                            ploc = sum(1 for _ in fh)
                    except Exception:
                        ploc = 0
                    pages.append({"file": f, "loc": ploc, "role": _PAGE_REGISTRY.get(f, "")})
        except Exception:
            pass
        # schema: table count from schema.sql (the data layer is part of the engine too)
        tables = 0
        try:
            with open(_os.path.join(base, "db", "schema.sql")) as fh:
                tables = len(_re.findall(r"CREATE TABLE", fh.read(), _re.I))
        except Exception:
            pass
        return {"modules": mods, "pages": pages, "processes": _PROCESSES, "externals": _EXTERNALS,
                "totals": {"files": len(mods), "loc": total_loc, "pages": len(pages),
                           "page_loc": sum(p["loc"] for p in pages), "tables": tables,
                           "processes": len(_PROCESSES), "externals": len(_EXTERNALS)}}
    except Exception:
        return {}
