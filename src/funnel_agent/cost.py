"""COST INTELLIGENCE — what it actually costs to run the outbound-intelligence system, and whether it pays.

Combines REAL usage from the database (AI call minutes, SMS sent, sites/audits/comparisons built, transcripts
classified, bookings, wins) with a unit-price table (researched defaults, every price OVERRIDABLE from
crm_config so the operator can correct it) to produce:
  • a per-tool monthly cost breakdown (Retell voice, Twilio SMS + numbers, Anthropic/OpenAI, DataForSEO,
    Google Places, Apollo, Aircall, Railway, Fireflies),
  • the headline unit economics — cost per lead (booking), cost per acquisition (won), and
  • a revenue vs cost read so we can see if the system is net-generating.

Everything is a careful ESTIMATE where exact token/API counts aren't logged (clearly flagged); the operator
tunes any figure and the whole report recomputes. Fully guarded.
"""
import json as _json
import re as _re


# --- default unit prices (USD). Researched; overridable via crm_config key 'cost_prices' (JSON) or per-key. ---
_DEFAULT_PRICES = {
    # Researched Aug 2026 (retellai/twilio AU/Claude API/DataForSEO/Google/Apollo/Aircall/Railway/Fireflies).
    "retell_per_min": 0.115,           # Retell voice-infra + LLM + TTS per connected min. TELEPHONY is EXCLUDED
                                       # here because the AI voice runs on a Twilio trunk already counted in the
                                       # live Twilio line (avoids double-counting). All-in w/ Retell telephony ≈ $0.13.
    "twilio_sms_au": 0.0515,           # outbound SMS per segment to an AU mobile (official Twilio AU)
    "twilio_number_month": 6.50,       # AU MOBILE number monthly rental (Lisa dials from +614… numbers)
    "opus_in_per_mtok": 5.0,           # Claude Opus 5 input, $/1M tokens (the model the builders use)
    "opus_out_per_mtok": 25.0,         # Claude Opus 5 output, $/1M tokens
    "gpt4omini_in_per_mtok": 0.15,
    "gpt4omini_out_per_mtok": 0.60,
    "whisper_per_min": 0.006,           # OpenAI Whisper STT — transcribing human-BDE (3CX/Aircall) recordings
    "dataforseo_audit": 0.10,          # per full SEO/competitor audit (aggregate of ~10-20 SERP+Labs calls)
    "places_per_1k": 32.0,             # Google Places API (New) Pro SKU per 1000 requests
    "apollo_month": 49.0,              # Apollo Basic per user/mo
    "aircall_seat_month": 30.0,        # Aircall Essentials per seat/mo (3-seat minimum)
    "railway_month": 25.0,
    "fireflies_month": 10.0,           # Fireflies Pro per user/mo
    "threecx_month": 0.0,              # 3CX human-dialer subscription (set your plan; 0 = not counted)
    "twilio_low_balance": 50.0,        # alert on the cost page when the live Twilio balance drops below this
    # per-asset token ESTIMATES (exact tokens aren't logged) — tune to taste
    "opus_tokens_site_out": 60000, "opus_tokens_site_in": 6000,
    "opus_tokens_audit_out": 6000, "opus_tokens_audit_in": 8000,
    "opus_tokens_comparison_out": 1500, "opus_tokens_comparison_in": 4000,
    "gpt_tokens_classify_out": 500, "gpt_tokens_classify_in": 3000,
    # fixed monthly infra estimates the operator sets
    "places_requests_month": 20000,    # est. Google Places sweep volume / mo
    "aircall_seats": 3,                # Aircall 3-seat minimum
    "caller_numbers": 6,               # Twilio AU numbers rented (Lisa 4 + Lisa 5 pools)
    "avg_deal_value": 3000.0,          # avg $ per WON deal (for revenue vs cost)
    "fx_usd_aud": 1.52,                # USD→AUD rate — unit prices are USD; the page shows AUD-first so the
                                       # finance view lives in ONE currency (tune when the rate moves)
}


def _prices(pool) -> dict:
    p = dict(_DEFAULT_PRICES)
    try:
        from . import lisa as _l
        r = _l._fetch(pool, "SELECT v FROM crm_config WHERE k='cost_prices'")
        if r and (r[0].get("v") or "").strip():
            over = _json.loads(r[0]["v"])
            for k, v in (over or {}).items():
                if k in p and isinstance(v, (int, float)):
                    p[k] = v
    except Exception:
        pass
    return p


def save_prices(pool, overrides: dict) -> None:
    """Persist operator price overrides (merged) to crm_config so the report recomputes. Guarded."""
    try:
        cur = _prices(pool)
        for k, v in (overrides or {}).items():
            if k in _DEFAULT_PRICES:
                try:
                    cur[k] = float(v)
                except Exception:
                    pass
        diff = {k: cur[k] for k in _DEFAULT_PRICES if cur[k] != _DEFAULT_PRICES[k]}
        with pool.connection() as conn, conn.cursor() as c:
            c.execute("INSERT INTO crm_config (k,v) VALUES ('cost_prices',%s) ON CONFLICT (k) DO UPDATE SET v=EXCLUDED.v",
                      (_json.dumps(diff),))
            conn.commit()
    except Exception:
        pass


def usage(pool, days: int = 30) -> dict:
    """Real usage counts over the window. Guarded — missing tables degrade to 0."""
    from . import lisa as _l
    u = {}

    def one(sql):
        try:
            r = _l._fetch(pool, sql)
            return dict(r[0]) if r else {}
        except Exception:
            return {}
    win = f"now() - interval '{int(days)} days'"
    c = one(f"SELECT count(*) calls, count(*) FILTER (WHERE meeting_agreed) booked, "
            f"COALESCE(sum(duration_ms),0)/60000.0 minutes, count(*) FILTER (WHERE status='analyzed') analyzed "
            f"FROM lisa_calls WHERE created_at > {win}")
    u["ai_calls"] = int(c.get("calls") or 0)
    u["ai_minutes"] = float(c.get("minutes") or 0)
    u["ai_booked"] = int(c.get("booked") or 0)
    u["classified"] = int(c.get("analyzed") or 0)
    # real CONVERSATIONS (>=20s talk or booked) — the funnel stage between dials and bookings, so the page
    # can price every stage of the funnel (cost/call → /convo → /booking → /held → /won).
    cv = one(f"SELECT count(*) n FROM lisa_calls WHERE created_at > {win} "
             f"AND (COALESCE(duration_ms,0)/1000 >= 20 OR meeting_agreed)")
    u["ai_convos"] = int(cv.get("n") or 0)
    s = one(f"SELECT count(*) FILTER (WHERE direction='outbound') out FROM lisa_sms WHERE created_at > {win}")
    u["sms_out"] = int(s.get("out") or 0)
    # HUMAN-BDE (3CX + Aircall) analytics workload — the STT transcription minutes + call-classifications that
    # power the human-BDE funnel/dashboard. Tracked SEPARATELY from the Lisa AI outbound-intelligence system.
    h = one("SELECT COALESCE(sum(talk_seconds) FILTER (WHERE has_transcript),0)/60.0 tx_min, "
            "count(*) FILTER (WHERE has_transcript) tx_calls, "
            "count(*) FILTER (WHERE EXISTS(SELECT 1 FROM classifications cl WHERE cl.call_id=calls.call_id)) cls, "
            "count(*) bde_calls "
            f"FROM calls WHERE provider IN ('3cx','aircall') AND started_at > {win}")
    u["bde_tx_minutes"] = float(h.get("tx_min") or 0)
    u["bde_transcribed"] = int(h.get("tx_calls") or 0)
    u["bde_classified"] = int(h.get("cls") or 0)
    u["bde_calls"] = int(h.get("bde_calls") or 0)
    a = one(f"SELECT count(*) FILTER (WHERE kind='reveal') sites, count(*) FILTER (WHERE kind='audit') audits, "
            f"count(*) FILTER (WHERE kind='comparison') comps FROM lisa4_sites WHERE status='built' AND built_at > {win}")
    u["sites"] = int(a.get("sites") or 0)
    u["audits"] = int(a.get("audits") or 0)
    u["comparisons"] = int(a.get("comps") or 0)
    b = one("SELECT count(DISTINCT dest9) leads FROM lisa_calls WHERE COALESCE(meeting_agreed,false) "
            f"AND created_at > {win}")
    u["leads"] = int(b.get("leads") or 0)
    w = one("SELECT count(*) FILTER (WHERE stage='won') won, count(*) FILTER (WHERE stage='revealed') revealed "
            "FROM booked_crm")
    u["won"] = int(w.get("won") or 0)
    u["revealed"] = int(w.get("revealed") or 0)
    # SEEN MEETINGS — reveals that ACTUALLY HAPPENED, within the window (DISTINCT prospects).
    # The reveal-tracking flips a booking to 'revealed' from BOTH legs of the Aircall + Fireflies pipeline:
    #   • a Fireflies reveal transcript / held reveal → booked_crm.stage='revealed' (bde_capture), and
    #   • an answered >=2min Aircall reveal call by a closer → booked_crm.status='revealed' (sync_reveal_calls).
    # Both writers stamp updated_at=now(), so updated_at is the window anchor. We also UNION any Fireflies-
    # recorded reveal matched to a booking (fireflies_meetings.meeting_date — a precise meeting timestamp) when
    # that table exists. Counting DISTINCT trailing-9 identity across all signals never double-counts a prospect
    # seen via more than one leg. Guarded → degrades to 0 (via one()) if a signal/table is unavailable.
    # SEEN = a BOOKED prospect whose reveal ACTUALLY HAPPENED in the window. The authoritative flag is
    # booked_crm.stage/status='revealed' (set by BOTH legs — the Aircall closer reveal-call AND the
    # Fireflies reveal pipeline). A bare fireflies_meetings row is ANY meeting the team recorded (internal
    # calls, unrelated prospects) — it is NOT a reveal, so the Fireflies leg is RESTRICTED to meetings that
    # MATCH an actual booking (dest9 ∈ booked_crm). Without the match the raw table over-counts reveals
    # ~6x. DISTINCT trailing-9 identity never double-counts a prospect seen on more than one leg.
    _n9 = "right(regexp_replace(COALESCE(dest9,''),'[^0-9]','','g'),9)"
    _bk9 = "SELECT right(regexp_replace(COALESCE(dest9,''),'[^0-9]','','g'),9) FROM booked_crm"
    seen_parts = [f"SELECT {_n9} d9 FROM booked_crm "
                  f"WHERE (stage='revealed' OR status='revealed') AND updated_at > {win}"]
    try:
        reg = _l._fetch(pool, "SELECT to_regclass('public.fireflies_meetings') AS t")
        if reg and (reg[0].get("t")):
            seen_parts.append(f"SELECT {_n9} d9 FROM fireflies_meetings "
                              f"WHERE meeting_date > {win} AND {_n9} IN ({_bk9})")
    except Exception:
        pass
    sm = one("SELECT count(DISTINCT d9) seen FROM (" + " UNION ALL ".join(seen_parts) + ") q WHERE d9 <> ''")
    u["seen_meetings"] = int(sm.get("seen") or 0)
    u["days"] = int(days)
    return u


def twilio_status(settings, days: int = 30) -> dict | None:
    """LIVE Twilio balance + ACTUAL spend over the window, straight from Twilio (not an estimate). Returns
    {balance, currency, window_spend, low, threshold} or None if unavailable. Guarded, short timeout."""
    import urllib.request, base64, json as _j
    from datetime import date, timedelta
    sid = getattr(settings, "twilio_account_sid", "") or ""
    sk = getattr(settings, "twilio_api_key_sid", "") or ""
    sec = getattr(settings, "twilio_api_key_secret", "") or ""
    tok = getattr(settings, "twilio_auth_token", "") or ""
    if not sid:
        return None
    user, pw = (sk, sec) if (sk and sec) else (sid, tok)
    if not pw:
        return None
    auth = base64.b64encode(f"{user}:{pw}".encode()).decode()
    base = f"https://api.twilio.com/2010-04-01/Accounts/{sid}"

    def g(url):
        req = urllib.request.Request(url, headers={"Authorization": "Basic " + auth})
        return _j.loads(urllib.request.urlopen(req, timeout=12).read())
    out = {}
    try:
        b = g(base + "/Balance.json")
        out["balance"] = round(float(b.get("balance") or 0), 2)
        out["currency"] = b.get("currency", "USD")
    except Exception:
        return None
    try:
        end = date.today()
        start = end - timedelta(days=int(days))
        u = g(f"{base}/Usage/Records.json?Category=totalprice&StartDate={start}&EndDate={end}&PageSize=1")
        recs = u.get("usage_records") or []
        out["window_spend"] = round(float(recs[0]["price"]), 2) if recs else None
    except Exception:
        out["window_spend"] = None
    return out


def _mtok(n):
    return (n or 0) / 1_000_000.0


def compute(pool, settings=None, days: int = 30) -> dict:
    """The full cost report: per-tool monthly lines + unit economics + revenue read. Uses LIVE Twilio
    balance/usage when available (else an estimate). Guarded."""
    p = _prices(pool)
    u = usage(pool, days)
    mo = days / 30.0                    # window as a fraction of a month — prorates fixed monthly costs
    pw = f' (prorated to {days}d)' if abs(days - 30) > 0.5 else ''
    lines = []

    def line(tool, detail, amount, kind="usage", group="outbound"):
        # group: 'outbound' = Lisa AI outbound-intelligence system; 'human_bde' = human-BDE (3CX/Aircall)
        # analytics; 'shared' = infra used by both. Kept separate so daily/weekly/monthly cost is trackable
        # per system without ever mixing the two.
        lines.append({"tool": tool, "detail": detail, "usd": round(float(amount or 0), 2), "kind": kind, "group": group})

    # --- usage-priced ---
    line("Retell AI voice", f'{int(u["ai_minutes"]):,} connected minutes × ${p["retell_per_min"]:.3f}',
         u["ai_minutes"] * p["retell_per_min"])
    # Twilio: prefer the LIVE, ACTUAL spend from the Twilio API (real segments + numbers + voice); fall back
    # to an estimate only if the API is unreachable.
    tw = twilio_status(settings, days) if settings is not None else None
    if tw and tw.get("window_spend") is not None:
        line("Twilio (live)", f'actual Twilio spend, last {days}d (SMS segments + numbers + voice)',
             tw["window_spend"], kind="live")
    else:
        line("Twilio — SMS", f'{u["sms_out"]:,} outbound SMS × ${p["twilio_sms_au"]:.4f} (est.)',
             u["sms_out"] * p["twilio_sms_au"], kind="estimate")
        line("Twilio — numbers", f'{int(p["caller_numbers"])} AU numbers × ${p["twilio_number_month"]:.2f}/mo{pw}',
             p["caller_numbers"] * p["twilio_number_month"] * mo, kind="fixed")
    # Anthropic Opus — site/audit/comparison builds (token estimates)
    opus_out = (u["sites"] * p["opus_tokens_site_out"] + u["audits"] * p["opus_tokens_audit_out"]
                + u["comparisons"] * p["opus_tokens_comparison_out"])
    opus_in = (u["sites"] * p["opus_tokens_site_in"] + u["audits"] * p["opus_tokens_audit_in"]
               + u["comparisons"] * p["opus_tokens_comparison_in"])
    opus_cost = _mtok(opus_in) * p["opus_in_per_mtok"] + _mtok(opus_out) * p["opus_out_per_mtok"]
    line("Anthropic Opus", f'{u["sites"]} sites + {u["audits"]} audits + {u["comparisons"]} comparisons (est. tokens)',
         opus_cost, kind="estimate")
    # OpenAI classifier
    gpt_out = u["classified"] * p["gpt_tokens_classify_out"]
    gpt_in = u["classified"] * p["gpt_tokens_classify_in"]
    gpt_cost = _mtok(gpt_in) * p["gpt4omini_in_per_mtok"] + _mtok(gpt_out) * p["gpt4omini_out_per_mtok"]
    line("OpenAI classifier (Lisa)", f'{u["classified"]:,} Lisa transcripts classified (est. tokens)', gpt_cost, kind="estimate")
    # --- HUMAN-BDE (3CX/Aircall) analytics — separate system, own cost bucket ---
    line("BDE call transcription", f'{int(u["bde_tx_minutes"]):,} min of 3CX/Aircall calls × ${p["whisper_per_min"]:.4f} (Whisper STT)',
         u["bde_tx_minutes"] * p["whisper_per_min"], kind="estimate", group="human_bde")
    _bde_gpt = (_mtok(u["bde_classified"] * p["gpt_tokens_classify_in"]) * p["gpt4omini_in_per_mtok"]
                + _mtok(u["bde_classified"] * p["gpt_tokens_classify_out"]) * p["gpt4omini_out_per_mtok"])
    line("BDE call classification", f'{u["bde_classified"]:,} human-BDE calls classified (est. tokens)',
         _bde_gpt, kind="estimate", group="human_bde")
    # DataForSEO
    line("DataForSEO", f'{u["audits"]} audit pulls × ${p["dataforseo_audit"]:.2f}',
         u["audits"] * p["dataforseo_audit"])
    # Google Places (monthly volume estimate, prorated to the window)
    line("Google Places", f'~{int(p["places_requests_month"]):,} requests/mo × ${p["places_per_1k"]:.2f}/1k (est.){pw}',
         (p["places_requests_month"] / 1000.0) * p["places_per_1k"] * mo, kind="estimate")
    # --- fixed subscriptions (monthly, prorated to the window) ---
    line("Apollo", f'prospecting plan{pw}', p["apollo_month"] * mo, kind="fixed")
    line("Aircall", f'{int(p["aircall_seats"])} seats × ${p["aircall_seat_month"]:.0f}/mo{pw}',
         p["aircall_seats"] * p["aircall_seat_month"] * mo, kind="fixed", group="human_bde")
    if p.get("threecx_month"):
        line("3CX", f'human-dialer subscription{pw}', p["threecx_month"] * mo, kind="fixed", group="human_bde")
    line("Railway", f'cloud hosting + Postgres{pw}', p["railway_month"] * mo, kind="fixed", group="shared")
    line("Fireflies", f'meeting capture{pw}', p["fireflies_month"] * mo, kind="fixed", group="shared")

    total = round(sum(l["usd"] for l in lines), 2)
    groups = {g: round(sum(l["usd"] for l in lines if l.get("group") == g), 2)
              for g in ("outbound", "human_bde", "shared")}
    leads = u["leads"] or u["ai_booked"]
    # Cost per MEETING must be attributed to the AI OUTBOUND system + its shared infra ONLY — NOT the whole
    # company. The human-BDE floor (Aircall/3CX) books its OWN meetings in the human funnel, so folding its
    # cost into the AI's per-meeting figure overstated it. Two honest views, both on the SAME AI-system cost:
    #   • per BOOKING  — every meeting Lisa agreed (ai_booked)
    #   • per HELD reveal — meetings that actually happened (seen_meetings)
    ai_sys_cost = round(groups.get("outbound", 0.0) + groups.get("shared", 0.0), 2)
    seen_meetings = int(u.get("seen_meetings") or 0)
    cost_per_booking = round(ai_sys_cost / u["ai_booked"], 2) if u.get("ai_booked") else None
    cost_per_seen_meeting = round(ai_sys_cost / seen_meetings, 2) if seen_meetings else None
    # cost-per-lead / acquisition keep the whole-company basis (they're company-wide funnel metrics).
    cpl = round(total / leads, 2) if leads else None
    cpa = round(total / u["won"], 2) if u["won"] else None
    revenue = round(u["won"] * p["avg_deal_value"], 2)
    net = round(revenue - total, 2)
    # projected: pipeline value if bookings close at a modest rate
    proj_rev = round(leads * 0.30 * p["avg_deal_value"], 2)
    lines.sort(key=lambda l: -l["usd"])
    twilio = None
    if tw:
        low = tw.get("balance") is not None and tw["balance"] < p["twilio_low_balance"]
        twilio = {"balance": tw.get("balance"), "currency": tw.get("currency", "USD"),
                  "window_spend": tw.get("window_spend"), "low": bool(low),
                  "threshold": p["twilio_low_balance"]}
    return {
        "days": days, "usage": u, "prices": p, "lines": lines, "total": total, "groups": groups,
        "cost_per_lead": cpl, "cost_per_acquisition": cpa, "leads": leads, "won": u["won"],
        "cost_per_seen_meeting": cost_per_seen_meeting, "seen_meetings": seen_meetings,
        "cost_per_booking": cost_per_booking, "ai_meeting_cost": ai_sys_cost, "ai_booked": u["ai_booked"],
        "revenue": revenue, "net": net, "projected_revenue": proj_rev,
        "avg_deal_value": p["avg_deal_value"], "twilio": twilio,
    }


def daily_series(pool, days: int = 30) -> list:
    """Per-DAY cost series for the spend chart, split by system (outbound / human_bde / shared). Built from
    real per-day usage × unit prices in a handful of grouped queries (NOT 30 × compute()). Fixed monthly
    subscriptions are spread evenly per day. Guarded → [] on any error."""
    from datetime import date, timedelta
    from . import lisa as _l
    try:
        p = _prices(pool)
        days = max(1, min(120, int(days)))
        win = f"now() - interval '{days} days'"
        TZ = "Australia/Melbourne"

        def per_day(sql):
            try:
                return {str(r["d"]): r for r in _l._fetch(pool, sql)}
            except Exception:
                return {}
        ai = per_day(f"SELECT (created_at AT TIME ZONE '{TZ}')::date d, COALESCE(sum(duration_ms),0)/60000.0 minutes, "
                     f"count(*) FILTER (WHERE status='analyzed') classified FROM lisa_calls "
                     f"WHERE created_at > {win} GROUP BY 1")
        sms = per_day(f"SELECT (created_at AT TIME ZONE '{TZ}')::date d, count(*) n FROM lisa_sms "
                      f"WHERE direction='outbound' AND created_at > {win} GROUP BY 1")
        assets = per_day(f"SELECT (built_at AT TIME ZONE '{TZ}')::date d, "
                         f"count(*) FILTER (WHERE kind='reveal') sites, count(*) FILTER (WHERE kind='audit') audits, "
                         f"count(*) FILTER (WHERE kind='comparison') comps FROM lisa4_sites "
                         f"WHERE status='built' AND built_at > {win} GROUP BY 1")
        bde = per_day(f"SELECT (started_at AT TIME ZONE '{TZ}')::date d, "
                      f"COALESCE(sum(talk_seconds) FILTER (WHERE has_transcript),0)/60.0 tx_min, "
                      f"count(*) FILTER (WHERE EXISTS(SELECT 1 FROM classifications cl WHERE cl.call_id=calls.call_id)) cls "
                      f"FROM calls WHERE provider IN ('3cx','aircall') AND started_at > {win} GROUP BY 1")
        # fixed subscriptions spread per day
        fx_out = (p["caller_numbers"] * p["twilio_number_month"] + (p["places_requests_month"] / 1000.0) * p["places_per_1k"]
                  + p["apollo_month"]) / 30.0
        fx_bde = (p["aircall_seats"] * p["aircall_seat_month"] + (p.get("threecx_month") or 0)) / 30.0
        fx_sh = (p["railway_month"] + p["fireflies_month"]) / 30.0
        out = []
        today = date.today()
        for i in range(days - 1, -1, -1):
            d = str(today - timedelta(days=i))
            a = ai.get(d) or {}
            s = sms.get(d) or {}
            asr = assets.get(d) or {}
            b = bde.get(d) or {}
            opus = (_mtok((asr.get("sites") or 0) * p["opus_tokens_site_out"] + (asr.get("audits") or 0) * p["opus_tokens_audit_out"]
                          + (asr.get("comps") or 0) * p["opus_tokens_comparison_out"]) * p["opus_out_per_mtok"]
                    + _mtok((asr.get("sites") or 0) * p["opus_tokens_site_in"] + (asr.get("audits") or 0) * p["opus_tokens_audit_in"]
                            + (asr.get("comps") or 0) * p["opus_tokens_comparison_in"]) * p["opus_in_per_mtok"])
            gpt = (_mtok((a.get("classified") or 0) * p["gpt_tokens_classify_in"]) * p["gpt4omini_in_per_mtok"]
                   + _mtok((a.get("classified") or 0) * p["gpt_tokens_classify_out"]) * p["gpt4omini_out_per_mtok"])
            outbound = ((a.get("minutes") or 0) * p["retell_per_min"] + (s.get("n") or 0) * p["twilio_sms_au"]
                        + opus + gpt + (asr.get("audits") or 0) * p["dataforseo_audit"] + fx_out)
            human = ((b.get("tx_min") or 0) * p["whisper_per_min"]
                     + _mtok((b.get("cls") or 0) * p["gpt_tokens_classify_in"]) * p["gpt4omini_in_per_mtok"]
                     + _mtok((b.get("cls") or 0) * p["gpt_tokens_classify_out"]) * p["gpt4omini_out_per_mtok"] + fx_bde)
            out.append({"date": d, "outbound": round(outbound, 2), "human_bde": round(human, 2),
                        "shared": round(fx_sh, 2), "total": round(outbound + human + fx_sh, 2)})
        return out
    except Exception:
        return []


def cost_periods(pool, settings=None) -> dict:
    """Per-SYSTEM run-cost for the last 1 / 7 / 30 days — Human-BDE analytics vs Lisa Outbound-Intelligence
    (+ shared infra) — so the operator can track daily / weekly / monthly cost of each system separately.
    Auto-updates from real usage every load. Twilio uses the estimate here (skip 3 live API calls) — the main
    report still shows live Twilio for the selected window. Guarded → zeros on any error."""
    out = {}
    for label, d in (("daily", 1), ("weekly", 7), ("monthly", 30)):
        try:
            r = compute(pool, None, days=d)
            g = r.get("groups") or {}
            out[label] = {"human_bde": g.get("human_bde", 0.0), "outbound": g.get("outbound", 0.0),
                          "shared": g.get("shared", 0.0), "total": r.get("total", 0.0)}
        except Exception:
            out[label] = {"human_bde": 0.0, "outbound": 0.0, "shared": 0.0, "total": 0.0}
    return out
