"""PLAIN-LANGUAGE growth audit for a non-technical Australian small-business owner.

Vysakh's brief: "the growth audit we generate is not good — a common person won't understand it." The fix
is NOT to rebuild the data engine (audit.assemble_audit already aggregates, debrands and dollarises every
DataForSEO/website signal) but to add a plain-English narration layer on top of it: take the rich audit
model, distil it to the few numbers an owner actually cares about, and have Opus-5 turn those into an
everyday-language report (no acronyms, every term defined once, each section led by a concrete outcome).

Generation is fully guarded — returns None on any failure so it can never break the caller (the build loop).
"""
import html as _h
from .reveal_guide import _claude_text   # non-streaming Anthropic Messages helper (text blocks only)


def _fmt_money(v) -> str:
    try:
        v = float(v)
    except Exception:
        return ""
    if v >= 1000:
        return "A$" + format(int(round(v)), ",")
    return "A$" + str(int(round(v)))


def _distill(m: dict, avg_ticket: float | None) -> dict:
    """Reduce the full audit model to the handful of plain facts an owner cares about — dropping raw jargon
    fields (CPC, ETV, difficulty, SoV internals) so the model narrates outcomes, not metrics."""
    opp = m.get("opportunity") or {}
    biz = m.get("business") or {}
    seo = m.get("seo") or {}
    rev = m.get("revenue") or {}
    geo = m.get("geo_aeo") or {}
    ads = m.get("ads") or {}

    def _kw(rows, n):
        out = []
        for r in (rows or [])[:n]:
            k = (r.get("keyword") or "").strip()
            if not k:
                continue
            vol = r.get("volume") or r.get("search_volume")
            out.append({"phrase": k, "monthly_searches": vol})
        return out

    comps = []
    for c in (m.get("competitors") or [])[:4]:
        nm = c.get("domain") or c.get("name")
        if nm:
            comps.append({"competitor": nm, "monthly_google_visitors": c.get("est_traffic")})

    # GROUNDED opportunity value only. We deliberately IGNORE revenue.monthly/annual — that figure is
    # traffic x 3% x 25% x ticket and inflates to absurd (multi-$M) totals that destroy credibility. The
    # engine's gap_capturable + quickwin_value are the realistically-capturable $ (capture-CTR x CPC), which
    # read as believable. A conservative "extra jobs" hint comes from that grounded value / ticket, halved.
    grounded_month = int((opp.get("gap_capturable") or 0) + (opp.get("quickwin_value") or 0))
    extra_jobs = round((grounded_month * 0.5) / avg_ticket, 1) if (avg_ticket and grounded_month) else None
    return {
        "name": m.get("name"),
        "industry": biz.get("industry"),
        "location": biz.get("location"),
        "website": biz.get("website"),
        "found_on_google": {
            "searches_they_show_for": opp.get("org_keywords"),
            "monthly_visitors_from_google": opp.get("est_org_traffic"),
            "searches_already_winning": _kw(seo.get("proof_winning"), 5),
        },
        "money_searches_missing": {
            "near_wins_page2": _kw(seo.get("money_keywords"), 8),      # already ranking p2, one push to p1
            "not_showing_competitors_win": _kw(m.get("keyword_gap"), 8),
        },
        "who_is_beating_them": comps,
        "opportunity_value_per_month": grounded_month or None,   # believable $ of missed searches / month
        "conservative_extra_jobs_per_month": extra_jobs,         # only if it reads modestly; else omit in copy
        "avg_sale_value": avg_ticket,
        "running_google_ads": bool(ads.get("running")),
        "ads_years": ads.get("years"),
        "ai_search_readiness_score": geo.get("score"),
        "fastest_fixes": [q.get("title") or q.get("what") or str(q)[:120]
                          for q in (m.get("quick_wins") or [])[:5] if q],
        "overall_grade": (m.get("health") or {}).get("grade"),
    }


def _shell(company: str, domain: str, inner: str, headline_stat: str = "") -> str:
    c = _h.escape(company or "your business"); d = _h.escape(domain or "")
    stat = f'<div class="hstat">{_h.escape(headline_stat)}</div>' if headline_stat else ""
    return f'''<!doctype html><html lang="en"><head><meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/><title>Growth Audit — {c}</title>
<style>
 :root{{--ink:#0b1524;--ink2:#28364c;--muted:#5b6d85;--faint:#8595a9;--bg:#fff;--bg2:#f4f8fd;--line:#e4ebf4;
   --navy:#0a1930;--blue:#1f5fd0;--cyan:#17a8e6;--green:#0f9d58;--amber:#f0a020;--red:#e0533d;
   --grad:linear-gradient(115deg,#1f5fd0,#17a8e6);
   --sh:0 2px 8px rgba(11,21,36,.06),0 22px 48px -30px rgba(11,21,36,.32)}}
 *{{box-sizing:border-box}} body{{margin:0;background:var(--bg2);color:var(--ink);font:16px/1.68 ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,Arial;-webkit-font-smoothing:antialiased}}
 .wrap{{max-width:800px;margin:0 auto;padding:0 22px}}
 .top{{background:linear-gradient(150deg,var(--navy),#12294a 60%,#0c1c38);color:#fff;padding:36px 0 40px;position:relative;overflow:hidden}}
 .top::after{{content:"";position:absolute;inset:0;background:radial-gradient(46% 120% at 88% 0,rgba(23,168,230,.30),transparent 60%);pointer-events:none}}
 .brandrow{{display:flex;align-items:center;gap:11px;position:relative}}
 .mark{{width:34px;height:34px;border-radius:9px;background:var(--grad);display:flex;align-items:flex-end;justify-content:center;gap:2.4px;padding:6px}}
 .mark i{{width:3.8px;border-radius:2px;background:#fff;display:block}} .mark i:nth-child(1){{height:6px;opacity:.6}} .mark i:nth-child(2){{height:12px;opacity:.82}} .mark i:nth-child(3){{height:17px}}
 .brandrow b{{font-size:19px;font-weight:800;letter-spacing:-.01em}} .brandrow small{{display:block;font-size:9px;font-weight:800;letter-spacing:2px;color:#9fb2cc;text-transform:uppercase;margin-top:-2px}}
 .badge{{margin-left:auto;font-size:10px;font-weight:800;letter-spacing:1.2px;text-transform:uppercase;color:#bfe0ff;border:1px solid rgba(191,224,255,.5);border-radius:999px;padding:5px 12px}}
 .top h1{{position:relative;font-size:clamp(24px,4vw,34px);font-weight:850;letter-spacing:-.025em;margin:20px 0 6px;text-wrap:balance}}
 .top p{{position:relative;color:#c3d2e6;font-size:15px;margin:0;max-width:60ch}}
 .hstat{{position:relative;display:inline-block;margin-top:20px;background:rgba(255,255,255,.09);border:1px solid rgba(255,255,255,.2);border-radius:13px;padding:13px 18px;font-size:15px;font-weight:700;color:#eaf3ff}}
 .doc{{background:var(--bg);border:1px solid var(--line);border-radius:16px;box-shadow:var(--sh);margin:24px 0 18px;padding:30px 34px}}
 .doc h2{{font-size:20px;font-weight:850;letter-spacing:-.015em;margin:34px 0 10px;padding-top:22px;border-top:1px solid var(--line);display:flex;align-items:flex-start;gap:11px;line-height:1.3}}
 .doc h2:first-of-type{{border-top:none;padding-top:0;margin-top:0}}
 .doc h2::before{{content:"";width:9px;height:9px;margin-top:9px;border-radius:50%;background:var(--grad);flex:none}}
 .doc h3{{font-size:15.5px;font-weight:800;margin:18px 0 6px;color:var(--ink)}}
 .doc p{{margin:0 0 12px;color:var(--ink2);font-size:15.5px}}
 .doc ul,.doc ol{{margin:0 0 14px;padding-left:22px}} .doc li{{margin:7px 0;color:var(--ink2)}}
 .doc strong,.doc b{{color:var(--ink);font-weight:750}}
 .doc em{{color:var(--blue);font-style:normal;font-weight:650}}
 .doc table{{width:100%;border-collapse:collapse;margin:6px 0 16px;font-size:14.5px}}
 .doc th,.doc td{{text-align:left;padding:9px 12px;border-bottom:1px solid var(--line)}}
 .doc th{{font-size:11px;font-weight:800;letter-spacing:.06em;text-transform:uppercase;color:var(--faint);background:var(--bg2)}}
 .doc td:last-child,.doc th:last-child{{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}}
 .callout{{background:linear-gradient(120deg,#eef5ff,#f5fbff);border:1px solid #d7e6fb;border-left:4px solid var(--blue);border-radius:12px;padding:16px 20px;margin:14px 0 18px}}
 .callout p{{margin:0}}
 .win{{border-left-color:var(--green);background:linear-gradient(120deg,#eefaf2,#f4fdf8);border-color:#c8ecd6}}
 .cta{{background:linear-gradient(140deg,var(--navy),#12294a);color:#fff;border-radius:16px;padding:30px 30px;margin:8px 0 26px;text-align:center}}
 .cta h3{{color:#fff;font-size:20px;margin:0 0 8px}} .cta p{{color:#c9d8ec;margin:0 auto 16px;max-width:52ch}}
 .cta a{{display:inline-block;background:var(--grad);color:#fff;font-weight:800;text-decoration:none;padding:13px 26px;border-radius:11px;font-size:15px;box-shadow:0 12px 26px -12px rgba(23,168,230,.7)}}
 .foot{{text-align:center;color:var(--faint);font-size:12.5px;padding:2px 20px 34px;line-height:1.7}}
 .foot b{{color:var(--muted)}}
</style></head><body>
 <div class="top"><div class="wrap">
   <div class="brandrow"><span class="mark"><i></i><i></i><i></i></span><span><b>Digital&nbsp;Expo</b><small>DE Group</small></span><span class="badge">Growth Audit</span></div>
   <h1>How {c} can win more customers from Google</h1>
   <p>A plain-English look at where your business shows up online today, where you're missing out, and the quickest ways to fix it{(' — '+d) if d else ''}.</p>
   {stat}
 </div></div>
 <div class="wrap"><div class="doc">{inner}</div>
   <div class="cta"><h3>Want us to fix this for you?</h3><p>We'll walk you through exactly what this means for {c} and how quickly we can turn it around — no jargon, no pressure.</p><a href="tel:0370209196">📞 Book a 15-minute call · (03) 7020 9196</a></div>
 </div>
 <div class="foot"><b>Digital Expo · DE Group</b> — Google Partner digital marketing agency · digitalexpo.com.au<br/>Figures are careful estimates based on live Google search data for your website and your competitors.</div>
</body></html>'''


_SYSTEM = (
    "You are a senior growth strategist at DE Group (Digital Expo), a Google Partner digital-marketing "
    "agency in Australia. Write a GROWTH AUDIT for a busy small-business owner who has ZERO marketing "
    "knowledge and no time. Absolute rules: (1) Plain everyday English — no acronyms or jargon; if you must "
    "use a term (SEO, ranking, keyword) define it once in plain words in brackets the first time. (2) Lead "
    "EVERY section with a concrete real-world outcome the owner feels, e.g. 'When someone in your area "
    "Googles \"emergency plumber\", your business doesn't show up — your competitor does, and they get the "
    "call.' (3) Turn every number into money or customers, never leave a raw metric. (4) Be encouraging and "
    "specific, never salesy or alarmist — you're showing them a real opportunity, not scaring them. "
    "(5) Output ONLY clean semantic HTML: <h2> per section, then <p>, <ul><li>, small <table> for search "
    "lists, and <div class=\"callout\"> (or class=\"callout win\" for a positive point) for the one key "
    "takeaway per section. Use <strong> for emphasis and <em> for the single most important sentence in a "
    "section. NO <html>/<head>/<body>, NO markdown fences, NO preamble or sign-off — the page wraps it."
)


import re as _re
_LEGAL = _re.compile(r"\b(the trustee for|pty\.?\s*ltd\.?|p/?l|ltd\.?|proprietary|unit trust|family trust|"
                     r"trust|t/?as|trading as|holdings|enterprises)\b", _re.I)


def _clean_name(name: str, company: str, domain: str) -> str:
    """Prefer the prospect's trading name (company); fall back to the audit's name, stripping obvious legal
    entity wording ('The Trustee for … Trust', 'Pty Ltd') that reads badly to an owner."""
    for cand in (company, name):
        c = (cand or "").strip()
        if c and not _LEGAL.search(c) and c.lower() not in ("none", ""):
            return c
    # last resort: title-case the domain's second-level label (imperfect but never a legal name)
    base = (domain or "").split("/")[0].split(".")[0].replace("-", " ").strip()
    return base.title() if base else (company or name or "your business")


def gen_growth_audit(key: str, model: str, audit_model: dict, avg_ticket: float | None = None,
                     company: str = "") -> str | None:
    """Turn an assemble_audit() model into a plain-language growth-audit HTML page with Opus 5. None on failure."""
    if not key or not audit_model:
        return None
    import json as _json
    brief = _distill(audit_model, avg_ticket)
    domain = audit_model.get("domain") or ""
    name = _clean_name(audit_model.get("name") or "", company, domain)
    brief["name"] = name
    user = (
        "Here is what our tools found about this business from live Google data (real search phrases, real "
        "competitors, real ad activity). Write their Growth Audit from it.\n\n"
        + _json.dumps(brief, indent=1, default=str)
        + "\n\nWrite these <h2> sections in order, ONLY using facts present above (skip a section gracefully if "
        "its data is empty — never invent numbers):\n"
        "1) 'The short version' — 3-4 sentence plain summary of where they stand and the single biggest "
        "opportunity.\n"
        "2) 'Are people finding you on Google?' — explain how visible they are now, and celebrate any searches "
        "they already win (proof they can rank).\n"
        "3) 'The searches you're missing' — the money phrases customers type where they DON'T show up but "
        "competitors do; put the phrases in a small 2-column table (Search / Times searched a month).\n"
        "4) 'Who's winning your customers' — name the competitors beating them and what that costs them, in "
        "plain terms.\n"
        + ("5) 'What this is worth to you' — use ONLY opportunity_value_per_month (the believable monthly "
           "value of customer searches currently going to competitors). Frame it as the size of the prize per "
           "month. You MAY add 'even winning back a share of that could mean a few extra jobs a month' using "
           "conservative_extra_jobs_per_month ONLY if it is a small, modest number. NEVER state a large annual "
           "revenue total or multiply it out into millions — keep it grounded and credible.\n"
           if brief.get("opportunity_value_per_month") else "")
        + "6) 'Are your ads working for you?' — only if there's ad data; otherwise skip.\n"
        "7) 'The 3 fastest fixes' — the quickest wins, each a plain sentence on what to do and why it helps.\n"
        "Keep the whole thing skimmable in 3-4 minutes. Australian spelling and tone."
    )
    try:
        inner = _claude_text(key, model, _SYSTEM, user, max_tokens=5000)
    except Exception:
        return None
    if not inner or "<h2" not in inner.lower():
        return None
    t = inner.strip()
    if t.startswith("```"):
        nl = t.find("\n"); t = t[nl + 1:] if nl != -1 else t[3:]
    if t.rstrip().endswith("```"):
        t = t.rstrip()[:-3]
    # a GROUNDED headline stat for the hero (never the inflated revenue.annual)
    hs = ""
    gm = brief.get("opportunity_value_per_month")
    if gm and gm >= 300:
        hs = f"About {_fmt_money(gm)}/month in customer searches are going to your competitors — that's the gap we can close"
    return _shell(name, domain, t.strip(), hs)
