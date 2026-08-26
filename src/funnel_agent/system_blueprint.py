"""SYSTEM BLUEPRINT — a connected NODES+EDGES map of the ENTIRE AI sales floor, every node coloured by a
REAL signal (last-activity timestamps, recent counts, error/backlog rates, live API balances, table
freshness). Same non-negotiable as system_status.py: a node NEVER shows a fake green — if a signal can't be
read it degrades to 'unknown' (amber), never crashes (Vysakh, 2026-08-20).

This EXTENDS system_status.compute(): the 14 engine tiles it already computes are reused verbatim (single
source of truth), then mapped onto the graph and joined by ~50 more discovered elements + the data-flow
edges between them.

blueprint(pool, settings) -> {generated_at, overall, counts, layers[], nodes[], edges[]}
  node  = {id, label, layer, state, detail, last, metric}
  edge  = {"from": id, "to": id, "label"?: str}
  state = 'ok' | 'warn' | 'down' | 'idle' | 'unknown'
"""
from __future__ import annotations
from datetime import datetime, timezone

from . import system_status as _ss
from .system_status import _one, _g, _mins_since, _fmt_ago, _au_calling_hours


# ------------------------------------------------------------------ layers (columns, left -> right)
LAYERS = [
    {"id": "ingest",       "label": "Ingest & Telephony"},
    {"id": "core",         "label": "Core / Loop"},
    {"id": "intelligence", "label": "Intelligence"},
    {"id": "builders",     "label": "Builders"},
    {"id": "autopilot",    "label": "Autopilot"},
    {"id": "crm",          "label": "CRM / Web"},
    {"id": "data",         "label": "Data Stores"},
    {"id": "external",     "label": "External / Billing"},
]

# where each REUSED system_status tile lands in the graph
_ENGINE_LAYER = {
    "dialer": "ingest", "retell_webhook": "ingest", "inbound": "ingest", "aircall": "ingest",
    "classifier": "intelligence", "fireflies": "intelligence",
    "site_builder": "builders", "audit_builder": "builders", "cmp_builder": "builders",
    "noshow": "autopilot", "brand_intro": "autopilot",
    "loop": "core",
    "twilio": "external", "dataforseo": "external",
}

# data-flow edges (only drawn when BOTH endpoints exist). Order = source -> target.
_EDGES = [
    # telephony in -> the call ledger
    ("retell", "dialer", ""), ("dialer", "t_lisa_calls", ""),
    ("retell", "retell_webhook", ""), ("retell_webhook", "t_lisa_calls", ""),
    ("inbound", "t_lisa_calls", ""), ("inbound", "t_lisa_sms", ""),
    ("threecx_ingest", "db", ""),
    ("aircall", "aircall_transcribe", ""), ("aircall_transcribe", "t_transcripts", ""),
    ("aircall", "db", ""),
    ("gmaps_sweep", "t_companies", ""),
    ("sms_engine", "t_lisa_sms", ""), ("sms_engine", "twilio", ""),
    # readiness / prep feeds the dialer
    ("t_companies", "daily_readiness", ""), ("daily_readiness", "dialer", "ready pool"),
    ("pool_prep", "dialer", ""),
    # intelligence
    ("classifier", "t_lisa_calls", ""), ("classifier", "openai_llm", ""),
    ("message_classifier", "t_messages", ""), ("message_classifier", "openai_llm", ""),
    ("fireflies", "openai_llm", ""), ("fireflies", "calendar_bookings", ""),
    ("bde_capture", "aircall_transcribe", "reads"), ("bde_capture", "t_booked_crm", ""),
    ("bde_capture", "openai_llm", ""),
    ("enrichment", "apollo", ""), ("enrichment", "dataforseo", ""), ("enrichment", "t_enrichment", ""),
    # a won call becomes a booking -> autopilot builds the reveal kit
    ("t_lisa_calls", "booking_docs", "meeting agreed"),
    ("booking_docs", "site_builder", ""), ("booking_docs", "reveal_guide", ""),
    ("booking_docs", "quote_builder", ""), ("booking_docs", "t_booked_crm", ""),
    ("site_builder", "t_lisa4_sites", ""), ("site_builder", "anthropic_llm", ""),
    ("site_builder", "engagement", "tracked"),
    ("audit_builder", "t_lisa4_sites", ""), ("audit_builder", "dataforseo", ""),
    ("cmp_builder", "t_lisa4_sites", ""),
    ("reveal_guide", "t_booked_crm", ""), ("quote_builder", "t_booked_crm", ""),
    # autopilot outreach
    ("brand_intro", "t_booked_crm", ""), ("brand_intro", "sms_engine", ""),
    ("noshow", "t_booked_crm", ""), ("noshow", "sms_engine", ""),
    ("emma", "t_calendar_events", ""), ("emma", "calendar_bookings", ""),
    # crm / web surface
    ("t_booked_crm", "crm_activity", ""), ("crm_activity", "web_app", ""),
    ("calendar_bookings", "t_calendar_events", ""),
    ("next_calls", "web_app", ""), ("engagement", "t_asset_events", ""),
    ("loop", "t_lisa_calls", ""), ("loop", "web_app", ""),
    ("railway", "web_app", "hosts"), ("web_app", "db", ""),
    ("daily_readiness", "t_tasks", ""),
    # every store lives in the one analytics DB
    ("t_lisa_calls", "db", ""), ("t_lisa4_sites", "db", ""), ("t_booked_crm", "db", ""),
    ("t_calendar_events", "db", ""), ("t_companies", "db", ""), ("t_transcripts", "db", ""),
    ("t_tasks", "db", ""), ("t_asset_events", "db", ""), ("t_enrichment", "db", ""),
    ("t_messages", "db", ""), ("t_lisa_sms", "db", ""),
]


def _node(nid, label, layer, state, detail, last=None, metric=None):
    return {"id": nid, "label": label, "layer": layer, "state": state,
            "detail": detail, "last": last, "metric": metric}


def _cfg_ts(pool, key):
    """A crm_config heartbeat stored as an ISO/text timestamp -> minutes-ago (or None)."""
    r = _one(pool, "SELECT v FROM crm_config WHERE k=%s", (key,))
    v = _g(r, "v")
    if not v:
        return None
    try:
        return _mins_since(datetime.fromisoformat(str(v)))
    except Exception:
        return None


def blueprint(pool, settings) -> dict:
    hrs = _au_calling_hours()
    nodes: list[dict] = []

    def guard(fn):
        try:
            return fn()
        except Exception as exc:
            return ("unknown", f"check failed: {str(exc)[:80]}", None, None)

    def add(nid, label, layer, fn):
        nodes.append(_node(nid, label, layer, *guard(fn)))

    # ---------------------------------------------------------------- 1) REUSE the 14 status tiles
    # single source of truth: same signals as /status, mapped onto the graph.
    try:
        base = _ss.compute(pool, settings)
        for t in (base.get("engines") or []):
            layer = _ENGINE_LAYER.get(t.get("key"), "core")
            nodes.append(_node(t["key"], t["name"], layer, t["state"], t["detail"], t.get("last"), t.get("metric")))
    except Exception as exc:
        nodes.append(_node("status_core", "Engine status core", "core", "unknown",
                           f"system_status.compute failed: {str(exc)[:80]}"))

    # ---------------------------------------------------------------- 2) INGEST (newly discovered)
    def _threecx():
        r = _one(pool, "SELECT max(started_at) last, count(*) FILTER (WHERE started_at>now()-interval '24 hours') n "
                       "FROM calls WHERE provider='3cx'")
        last, n = _mins_since(_g(r, "last")), _g(r, "n", 0)
        state = "ok" if (last is not None and last < 60 * 24) else "idle"
        return state, f"{n} 3CX calls ingested (24h)", _fmt_ago(last), n
    add("threecx_ingest", "3CX call ingest", "ingest", _threecx)

    def _actranscribe():
        r = _one(pool, "SELECT count(*) FILTER (WHERE t.call_id IS NOT NULL AND c.started_at>now()-interval '24 hours') done, "
                       "count(*) FILTER (WHERE t.call_id IS NULL AND c.recording_present AND c.started_at>now()-interval '24 hours') pend "
                       "FROM calls c LEFT JOIN transcripts t ON t.call_id=c.call_id WHERE c.provider='aircall'")
        done, pend = _g(r, "done", 0), _g(r, "pend", 0)
        if pend >= 10 and done == 0:
            state = "down"
        elif pend >= 5 and pend > done:
            state = "warn"
        else:
            state = "ok" if (done or not hrs) else "idle"
        return state, f"{done} transcribed / {pend} pending (24h)", None, pend
    add("aircall_transcribe", "Aircall transcription", "ingest", _actranscribe)

    def _gmaps():
        mins = _cfg_ts(pool, "gmaps_autosweep_at")
        r = _one(pool, "SELECT count(*) n FROM companies WHERE source='gmaps' AND created_at>now()-interval '24 hours'")
        n = _g(r, "n", 0)
        if mins is None:
            state, det = "unknown", "no sweep recorded"
        elif mins < 60 * 8:
            state, det = "ok", f"{n} gmaps prospects added (24h)"
        elif mins < 60 * 24:
            state, det = "warn", f"slow · {n} added (24h)"
        else:
            state, det = "idle", "sweep quiet (pool full)"
        return state, det, _fmt_ago(mins), n
    add("gmaps_sweep", "Google-Maps auto-sweep", "ingest", _gmaps)

    def _sms():
        r = _one(pool, "SELECT count(*) FILTER (WHERE direction='outbound' AND created_at>now()-interval '24 hours') o, "
                       "count(*) FILTER (WHERE direction='inbound' AND created_at>now()-interval '24 hours') i, "
                       "max(created_at) last FROM lisa_sms")
        o, i, last = _g(r, "o", 0), _g(r, "i", 0), _mins_since(_g(r, "last"))
        state = "ok" if (last is not None and last < 60 * 24) else "idle"
        return state, f"{o} sent / {i} replies (24h)", _fmt_ago(last), o + i
    add("sms_engine", "SMS engine (Twilio in/out)", "ingest", _sms)

    # ---------------------------------------------------------------- 3) CORE
    def _web():
        _one(pool, "SELECT 1 x")
        return "ok", "serving /api/blueprint · DB reachable", None, None
    add("web_app", "Web app + SSE", "core", _web)

    def _prep():
        r = _one(pool, "SELECT (SELECT max(reserved_at) FROM lisa4_pool) a, (SELECT max(reserved_at) FROM lisa5_pool) b")
        ma, mb = _mins_since(_g(r, "a")), _mins_since(_g(r, "b"))
        cands = [m for m in (ma, mb) if m is not None]
        last = min(cands) if cands else None
        if last is None:
            state = "unknown"
        elif last < 180:
            state = "ok"
        elif hrs:
            state = "warn"
        else:
            state = "idle"
        return state, "dial-loop prep / pool top-up", _fmt_ago(last), None
    add("pool_prep", "Dial-loop prep thread", "core", _prep)

    # ---------------------------------------------------------------- 4) INTELLIGENCE
    def _msgcls():
        r = _one(pool, "SELECT count(*) FILTER (WHERE classified AND created_at>now()-interval '24 hours') done, "
                       "count(*) FILTER (WHERE NOT classified) pend, max(created_at) last FROM messages")
        done, pend, last = _g(r, "done", 0), _g(r, "pend", 0), _mins_since(_g(r, "last"))
        if pend >= 10 and done == 0:
            state = "down"
        elif pend >= 5 and pend > done:
            state = "warn"
        else:
            state = "ok" if (done or last is not None) else "idle"
        return state, f"{done} classified / {pend} pending", _fmt_ago(last), pend
    add("message_classifier", "Message classifier (3CX SMS)", "intelligence", _msgcls)

    def _bcap():
        mins = _cfg_ts(pool, "bde_capture_last_run")
        r = _one(pool, "SELECT count(*) FILTER (WHERE captured_at>now()-interval '24 hours') n FROM bde_capture_seen")
        n = _g(r, "n", 0)
        if mins is None:
            state, det = "unknown", "no run recorded"
        elif mins < 30:
            state, det = "ok", f"polling · {n} calls captured (24h)"
        elif mins < 180:
            state, det = ("warn" if hrs else "idle"), f"{n} captured (24h)"
        else:
            state, det = ("warn" if hrs else "idle"), "quiet"
        return state, det, _fmt_ago(mins), n
    add("bde_capture", "BDE-call capture (Aircall→CRM)", "intelligence", _bcap)

    def _enr():
        r = _one(pool, "SELECT count(*) FILTER (WHERE fetched_at>now()-interval '24 hours') n, max(fetched_at) last "
                       "FROM enrichment")
        n, last = _g(r, "n", 0), _mins_since(_g(r, "last"))
        state = "ok" if (last is not None and last < 60 * 48) else "idle"
        return state, f"{n} domains enriched (24h)", _fmt_ago(last), n
    add("enrichment", "Enrichment engine (Apollo/DFS/site)", "intelligence", _enr)

    # ---------------------------------------------------------------- 5) BUILDERS (docs)
    def _guide():
        r = _one(pool, "SELECT count(*) tot, count(guideline_token) has, max(updated_at) last FROM booked_crm")
        tot, has, last = _g(r, "tot", 0), _g(r, "has", 0), _mins_since(_g(r, "last"))
        state = "ok" if has > 0 else "idle"
        return state, f"{has}/{tot} bookings have a reveal guide", _fmt_ago(last), has
    add("reveal_guide", "Reveal-guide builder", "builders", _guide)

    def _quote():
        r = _one(pool, "SELECT count(*) tot, count(quote_token) has, max(updated_at) last FROM booked_crm")
        tot, has, last = _g(r, "tot", 0), _g(r, "has", 0), _mins_since(_g(r, "last"))
        state = "ok" if has > 0 else "idle"
        return state, f"{has}/{tot} bookings have a quote", _fmt_ago(last), has
    add("quote_builder", "Quote builder", "builders", _quote)

    # ---------------------------------------------------------------- 6) AUTOPILOT (new)
    def _docs():
        r = _one(pool, "SELECT count(*) tot, "
                       "count(*) FILTER (WHERE guideline_token IS NOT NULL OR quote_token IS NOT NULL "
                       "  OR comparison_token IS NOT NULL OR audit_token IS NOT NULL) docd, "
                       "max(updated_at) last FROM booked_crm")
        tot, docd, last = _g(r, "tot", 0), _g(r, "docd", 0), _mins_since(_g(r, "last"))
        pct = int(100 * docd / tot) if tot else 0
        state = "ok" if docd > 0 else ("idle" if tot == 0 else "warn")
        return state, f"{docd}/{tot} bookings kitted ({pct}%)", _fmt_ago(last), pct
    add("booking_docs", "Booking-docs autopilot", "autopilot", _docs)

    def _emma():
        ctl = _one(pool, "SELECT max(updated_at) last FROM emma_control")
        r = _one(pool, "SELECT count(*) FILTER (WHERE status='draft') drafts, "
                       "count(*) FILTER (WHERE status IN ('scheduled','accepted','booked') "
                       "  AND updated_at>now()-interval '7 days') live7, max(updated_at) last FROM emma_meetings")
        cmin = _mins_since(_g(ctl, "last"))
        mmin = _mins_since(_g(r, "last"))
        drafts, live7 = _g(r, "drafts", 0), _g(r, "live7", 0)
        if cmin is None and mmin is None:
            return "idle", "not active / not configured", None, None
        last = min([m for m in (cmin, mmin) if m is not None] or [None])
        return "ok", f"{drafts} drafts · {live7} scheduled (7d)", _fmt_ago(last), drafts
    add("emma", "Emma (invite autopilot)", "autopilot", _emma)

    def _ready():
        r = _one(pool, "SELECT (SELECT count(*) FROM lisa4_pool lp WHERE NOT EXISTS "
                       "   (SELECT 1 FROM lisa_calls lc WHERE lc.dest9=lp.dest9)) l4, "
                       "(SELECT count(*) FROM lisa5_pool lp WHERE NOT EXISTS "
                       "   (SELECT 1 FROM lisa_calls lc WHERE lc.dest9=lp.dest9)) l5")
        l4, l5 = _g(r, "l4", 0), _g(r, "l5", 0)
        ready = l4 + l5
        state = "down" if ready < 20 else ("warn" if ready < 60 else "ok")
        return state, f"{l4}+{l5} fresh prospects ready to call", None, ready
    add("daily_readiness", "Daily-readiness (pool stock)", "autopilot", _ready)

    # ---------------------------------------------------------------- 7) CRM / WEB
    def _crmact():
        r = _one(pool, "SELECT count(*) FILTER (WHERE created_at>now()-interval '24 hours') n, max(created_at) last "
                       "FROM crm_activity")
        n, last = _g(r, "n", 0), _mins_since(_g(r, "last"))
        state = "ok" if (last is not None and last < 60 * 48) else "idle"
        return state, f"{n} CRM timeline updates (24h)", _fmt_ago(last), n
    add("crm_activity", "Booked-CRM activity", "crm", _crmact)

    def _calbk():
        r = _one(pool, "SELECT count(*) FILTER (WHERE start_at>now() AND status='pending' AND type IN ('reveal','meeting')) up, "
                       "max(created_at) last FROM calendar_events")
        up, last = _g(r, "up", 0), _mins_since(_g(r, "last"))
        state = "ok" if (up > 0 or (last is not None and last < 60 * 24 * 7)) else "idle"
        return state, f"{up} upcoming reveals/meetings", _fmt_ago(last), up
    add("calendar_bookings", "Calendar / bookings", "crm", _calbk)

    def _next():
        r = _one(pool, "SELECT count(*) FILTER (WHERE status='open') o, max(synced_at) last FROM next_call_queue")
        o, last = _g(r, "o", 0), _mins_since(_g(r, "last"))
        state = "ok" if o > 0 else "idle"
        return state, f"{o} open in next-call queue", _fmt_ago(last), o
    add("next_calls", "Next-call queue", "crm", _next)

    def _eng():
        r = _one(pool, "SELECT count(*) FILTER (WHERE created_at>now()-interval '24 hours') n, "
                       "count(DISTINCT dest9) FILTER (WHERE created_at>now()-interval '7 days') p7, "
                       "max(created_at) last FROM asset_events")
        n, p7, last = _g(r, "n", 0), _g(r, "p7", 0), _mins_since(_g(r, "last"))
        state = "ok" if (last is not None and last < 60 * 24 * 7) else "idle"
        return state, f"{n} engagement events (24h) · {p7} prospects (7d)", _fmt_ago(last), n
    add("engagement", "Engagement tracking (pixels)", "crm", _eng)

    # ---------------------------------------------------------------- 8) DATA STORES (freshness)
    def _db():
        r = _one(pool, "SELECT count(*) n FROM crm_config")
        return "ok", f"analytics DB reachable · {_g(r, 'n', 0)} config keys", None, None
    add("db", "Analytics DB", "data", _db)

    def _fresh(table, ts_col, ok_h, warn_h, static=False, est=False):
        def fn():
            if est:
                r = _one(pool, "SELECT reltuples::bigint n FROM pg_class WHERE relname=%s", (table,))
                n = _g(r, "n", 0) or 0
            else:
                r = _one(pool, f"SELECT count(*) n FROM {table}")
                n = _g(r, "n", 0)
            if static or ts_col is None:
                state = "ok" if n and n > 0 else "idle"
                return state, f"~{n:,} rows" if est else f"{n:,} rows", None, n
            rt = _one(pool, f"SELECT max({ts_col}) last FROM {table}")
            last = _mins_since(_g(rt, "last"))
            if last is None:
                state = "idle" if n else "idle"
            elif last < ok_h * 60:
                state = "ok"
            elif last < warn_h * 60:
                state = "warn"
            else:
                state = "idle"
            return state, f"{n:,} rows", _fmt_ago(last), n
        return fn

    add("t_lisa_calls",      "lisa_calls",      "data", _fresh("lisa_calls",      "updated_at", 6, 48))
    add("t_lisa4_sites",     "lisa4_sites",     "data", _fresh("lisa4_sites",     "created_at", 72, 168))
    add("t_booked_crm",      "booked_crm",      "data", _fresh("booked_crm",      "updated_at", 72, 336))
    add("t_calendar_events", "calendar_events", "data", _fresh("calendar_events", "created_at", 72, 336))
    add("t_companies",       "companies",       "data", _fresh("companies",       None,        0, 0, static=True, est=True))
    add("t_transcripts",     "transcripts",     "data", _fresh("transcripts",     None,        0, 0, static=True))
    add("t_tasks",           "tasks",           "data", _fresh("tasks",           "updated_at", 336, 720, static=True))
    add("t_asset_events",    "asset_events",    "data", _fresh("asset_events",    "created_at", 168, 336))
    add("t_enrichment",      "enrichment",      "data", _fresh("enrichment",      "fetched_at", 72, 336))
    add("t_messages",        "messages",        "data", _fresh("messages",        "created_at", 72, 336))
    add("t_lisa_sms",        "lisa_sms",        "data", _fresh("lisa_sms",        "created_at", 72, 336))

    # ---------------------------------------------------------------- 9) EXTERNAL / BILLING
    def _retell():
        r = _one(pool, "SELECT count(*) FILTER (WHERE created_at>now()-interval '24 hours') n, max(created_at) last "
                       "FROM lisa_calls")
        n, last = _g(r, "n", 0), _mins_since(_g(r, "last"))
        state = "ok" if (last is not None and last < 60 * 24) else "idle"
        return state, f"{n} voice calls placed via Retell (24h)", _fmt_ago(last), n
    add("retell", "Retell (voice AI)", "external", _retell)

    def _apollo():
        r = _one(pool, "SELECT count(*) FILTER (WHERE apollo IS NOT NULL AND fetched_at>now()-interval '7 days') n, "
                       "max(fetched_at) FILTER (WHERE apollo IS NOT NULL) last FROM enrichment")
        n, last = _g(r, "n", 0), _mins_since(_g(r, "last"))
        state = "ok" if (last is not None and last < 60 * 24 * 7) else "idle"
        return state, f"{n} Apollo lookups (7d)", _fmt_ago(last), n
    add("apollo", "Apollo (contact data)", "external", _apollo)

    def _openai():
        r = _one(pool, "SELECT count(*) FILTER (WHERE created_at>now()-interval '24 hours') n, "
                       "COALESCE(sum(cost_cents) FILTER (WHERE created_at>now()-interval '24 hours'),0) c, "
                       "max(created_at) last FROM lisa_llm_usage")
        n, c, last = _g(r, "n", 0), float(_g(r, "c", 0) or 0), _mins_since(_g(r, "last"))
        state = "ok" if (last is not None and last < 60 * 24) else "idle"
        return state, f"{n} LLM tasks (24h) · ${c/100:.2f}", _fmt_ago(last), n
    add("openai_llm", "OpenAI (classify/SMS/STT)", "external", _openai)

    def _anthropic():
        key = _one(pool, "SELECT v FROM crm_config WHERE k='anthropic_api_key'")
        has_key = bool(_g(key, "v"))
        r = _one(pool, "SELECT count(*) FILTER (WHERE status='built' AND built_at>now()-interval '48 hours') built, "
                       "count(*) FILTER (WHERE status='error' AND COALESCE(built_at,created_at)>now()-interval '48 hours') err, "
                       "max(built_at) last FROM lisa4_sites")
        built, err, last = _g(r, "built", 0), _g(r, "err", 0), _mins_since(_g(r, "last"))
        if built > 0:
            state = "ok"
        elif err > 0:
            state = "warn"
        else:
            state = "idle"
        det = f"{built} Opus site builds (48h)" + ("" if has_key else " · key override unset")
        return state, det, _fmt_ago(last), built
    add("anthropic_llm", "Anthropic (site designer)", "external", _anthropic)

    def _railway():
        _one(pool, "SELECT 1 x")
        return "ok", "container serving", None, None
    add("railway", "Railway (hosting)", "external", _railway)

    # ---------------------------------------------------------------- assemble
    edges = []
    ids = {n["id"] for n in nodes}
    for a, b, lbl in _EDGES:
        if a in ids and b in ids:
            e = {"from": a, "to": b}
            if lbl:
                e["label"] = lbl
            edges.append(e)

    counts = {"down": 0, "warn": 0, "unknown": 0, "idle": 0, "ok": 0}
    for n in nodes:
        counts[n["state"]] = counts.get(n["state"], 0) + 1
    overall = "down" if counts.get("down") else ("warn" if (counts.get("warn") or counts.get("unknown")) else "ok")

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "overall": overall,
        "counts": counts,
        "layers": LAYERS,
        "nodes": nodes,
        "edges": edges,
    }
