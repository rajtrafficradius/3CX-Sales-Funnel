"""PLAIN-LANGUAGE, PREMIUM growth audit for a non-technical Australian small-business owner.

Vysakh's brief: the audit must be prospect-friendly (a normal owner understands it), feel valuable and
premium, and carry ALL the substance our full audit engine has. So this is a HYBRID:

  • the health SCORECARD + the key-numbers strip are rendered DETERMINISTICALLY here (always accurate, always
    premium — never left to the model), straight from audit.assemble_audit()'s numbers;
  • the plain-English NARRATIVE for every section (found-on-Google, the buying journey, the searches you're
    missing, who's winning, topics you haven't covered, your ads, Google/AI-search readiness, what it's worth,
    and a 90-day plan) is written by Opus-5 from a distilled, jargon-free brief.

Grounded $ only (gap_capturable + quick-win value) — never the inflated traffic×conversion revenue. Fully
guarded: returns None on any failure so it can never break the caller.
"""
import html as _h
import re as _re
from .reveal_guide import _claude_text   # non-streaming Anthropic Messages helper (text blocks only)


def _n(v) -> str:
    try:
        return format(int(round(float(v))), ",")
    except Exception:
        return "0"


def _fmt_money(v) -> str:
    try:
        return "A$" + format(int(round(float(v))), ",")
    except Exception:
        return ""


def _band(score):
    """Positive, non-threatening band for a 0-100 score (an owner should feel opportunity, not judgement)."""
    try:
        s = float(score)
    except Exception:
        s = 0
    if s >= 80:
        return ("Excellent", "#0f9d58")
    if s >= 60:
        return ("Strong", "#1f9d55")
    if s >= 40:
        return ("Developing", "#f0a020")
    return ("Emerging", "#e0774d")


_LEGAL = _re.compile(r"\b(the trustee for|pty\.?\s*ltd\.?|p/?l|ltd\.?|proprietary|unit trust|family trust|"
                     r"trust|t/?as|trading as|holdings|enterprises)\b", _re.I)


def _clean_name(name: str, company: str, domain: str) -> str:
    for cand in (company, name):
        c = (cand or "").strip()
        if c and not _LEGAL.search(c) and c.lower() not in ("none", ""):
            return c
    base = (domain or "").split("/")[0].split(".")[0].replace("-", " ").strip()
    return base.title() if base else (company or name or "your business")


def _kw(rows, n):
    out = []
    for r in (rows or [])[:n]:
        k = (r.get("keyword") or "").strip()
        if not k:
            continue
        out.append({"phrase": k, "monthly_searches": r.get("volume") or r.get("search_volume")})
    return out


def _distill(m: dict, name: str, avg_ticket) -> dict:
    """Everything the model knows, reduced to plain facts an owner cares about — no CPC/ETV/difficulty/SoV."""
    opp = m.get("opportunity") or {}
    biz = m.get("business") or {}
    seo = m.get("seo") or {}
    ads = m.get("ads") or {}
    geo = m.get("geo_aeo") or {}
    fn = (m.get("universe") or {}).get("funnel") or {}
    rec = m.get("recommendation") or {}

    def fstage(key):
        s = fn.get(key) or {}
        return {"searches_a_month": s.get("volume"), "you_get_now": s.get("traffic")}

    grounded = int((opp.get("gap_capturable") or 0) + (opp.get("quickwin_value") or 0))
    comps = [{"competitor": c.get("domain") or c.get("name"),
              "monthly_google_visitors": c.get("est_traffic")}
             for c in (m.get("competitors") or [])[:4] if (c.get("domain") or c.get("name"))]
    content = [{"topic": c.get("topic"), "total_monthly_searches": c.get("total_volume"),
                "a_competitor_covering_it": c.get("domain"),
                "example_searches": (c.get("example_keywords") or [])[:3]}
               for c in (m.get("content_gap") or [])[:5] if c.get("topic")]
    ads_formats = ads.get("formats") or {}
    geo_missing = [c.get("note") for c in (geo.get("checks") or []) if not c.get("pass")][:6]
    geo_have = [c.get("name") for c in (geo.get("checks") or []) if c.get("pass")][:5]
    plan = []
    for st in (rec.get("steps") or [])[:5]:
        if isinstance(st, (list, tuple)) and len(st) >= 2:
            plan.append({"move": st[0], "why": st[1]})
        elif isinstance(st, dict):
            plan.append({"move": st.get("title"), "why": st.get("detail")})
    quick = [{"do": q.get("title"), "why": q.get("detail")} for q in (m.get("quick_wins") or [])[:4] if q.get("title")]

    return {
        "business": name,
        "industry": biz.get("industry"),
        "location": biz.get("location"),
        "found_on_google": {
            "searches_you_show_for": opp.get("org_keywords") or (m.get("universe") or {}).get("totals", {}).get("ranked"),
            "visitors_from_google_a_month": opp.get("est_org_traffic"),
            "searches_you_already_win": _kw(seo.get("proof_winning"), 5),
        },
        "buying_journey": {
            "just_researching": fstage("TOFU"),
            "comparing_options": fstage("MOFU"),
            "ready_to_buy_now": fstage("BOFU"),
        },
        "searches_you_are_missing": {
            "one_step_from_page_one": _kw(seo.get("money_keywords"), 8),
            "not_showing_at_all_competitors_win": _kw(m.get("keyword_gap"), 8),
        },
        "who_is_beating_you": comps,
        "topics_you_have_not_covered": content,
        "your_google_ads": ({
            "running_now": bool(ads.get("running")),
            "years_running": ads.get("years_active") or ads.get("years"),
            "live_ads_count": ads.get("count"),
            "ad_types": ads_formats,
        } if (ads.get("running") or ads.get("count")) else None),
        "google_and_ai_search_readiness": {
            "score_out_of_100": geo.get("score"),
            "you_already_have": geo_have,
            "you_are_missing": geo_missing,
        },
        "opportunity_value_per_month": grounded or None,
        "conservative_extra_jobs_per_month": (round((grounded * 0.5) / avg_ticket, 1)
                                              if (avg_ticket and grounded) else None),
        "avg_sale_value": avg_ticket,
        "ninety_day_plan": plan,
        "quick_wins": quick,
    }


def _scorecard(m: dict) -> str:
    """DETERMINISTIC premium block: overall score dial + 4 plain-language dimension bars + a key-numbers strip.
    Rendered here (not by the model) so it is always accurate and always looks the same."""
    h = m.get("health") or {}
    opp = m.get("opportunity") or {}
    overall = int(h.get("overall") or 0)
    oband, ocol = _band(overall)
    dims = [
        ("Getting found on Google", h.get("seo"), "how visible you are in normal (unpaid) search"),
        ("Google Ads", h.get("ads"), "how well your paid ads are working"),
        ("Standing out from rivals", h.get("competitive"), "your share next to competitors"),
        ("Website &amp; tech health", h.get("technical"), "speed, mobile &amp; being readable by Google/AI"),
    ]
    bars = ""
    for label, sc, note in dims:
        sc = int(sc or 0)
        _, col = _band(sc)
        bars += (f'<div class="dim"><div class="dim-t"><span>{label}</span><b style="color:{col}">{sc}<i>/100</i></b></div>'
                 f'<div class="bar"><span style="width:{max(3,min(100,sc))}%;background:{col}"></span></div>'
                 f'<div class="dim-n">{note}</div></div>')
    grounded = int((opp.get("gap_capturable") or 0) + (opp.get("quickwin_value") or 0))
    miss = 0
    seo = m.get("seo") or {}
    for r in (seo.get("money_keywords") or []) + (m.get("keyword_gap") or []):
        miss += int(r.get("volume") or r.get("search_volume") or 0)
    stats = []
    if grounded:
        stats.append((_fmt_money(grounded), "of customer searches going to rivals, a month"))
    if miss:
        stats.append((_n(miss) + "+", "monthly searches you could be showing up for"))
    ncomp = len(m.get("competitors") or [])
    if ncomp:
        stats.append((str(ncomp), "competitors currently ahead of you online"))
    stat_html = "".join(f'<div class="stat"><b>{v}</b><span>{lab}</span></div>' for v, lab in stats[:3])
    deg = max(0, min(360, round(overall * 3.6)))
    return (
        '<div class="scorecard">'
        '<div class="sc-head">'
        f'<div class="dial" style="background:conic-gradient({ocol} {deg}deg,#e7edf5 {deg}deg)">'
        f'<div class="dial-in"><b>{overall}</b><span>/100</span></div></div>'
        f'<div class="sc-lead"><div class="sc-band" style="color:{ocol}">{oband}</div>'
        '<h3>Your online health score</h3>'
        '<p>A quick read on how your business shows up online today. The lower the score, the bigger the '
        'opportunity — here is exactly where the room to grow is.</p></div></div>'
        f'<div class="dims">{bars}</div>'
        + (f'<div class="statstrip">{stat_html}</div>' if stat_html else "")
        + '</div>'
    )


def _shell(company: str, domain: str, scorecard: str, inner: str, headline_stat: str = "") -> str:
    c = _h.escape(company or "your business"); d = _h.escape(domain or "")
    stat = f'<div class="hstat">{_h.escape(headline_stat)}</div>' if headline_stat else ""
    return f'''<!doctype html><html lang="en"><head><meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/><title>Growth Audit — {c}</title>
<style>
 :root{{--ink:#0b1524;--ink2:#28364c;--muted:#5b6d85;--faint:#8595a9;--bg:#fff;--bg2:#f4f8fd;--line:#e4ebf4;
   --navy:#0a1930;--blue:#1f5fd0;--cyan:#17a8e6;--green:#0f9d58;--amber:#f0a020;--red:#e0533d;
   --grad:linear-gradient(115deg,#1f5fd0,#17a8e6);
   --sh:0 2px 8px rgba(11,21,36,.06),0 22px 48px -30px rgba(11,21,36,.32);--sh-sm:0 1px 3px rgba(11,21,36,.08)}}
 *{{box-sizing:border-box}} body{{margin:0;background:var(--bg2);color:var(--ink);font:16px/1.68 ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,Arial;-webkit-font-smoothing:antialiased}}
 .wrap{{max-width:820px;margin:0 auto;padding:0 22px}}
 .top{{background:linear-gradient(150deg,var(--navy),#12294a 60%,#0c1c38);color:#fff;padding:38px 0 44px;position:relative;overflow:hidden}}
 .top::after{{content:"";position:absolute;inset:0;background:radial-gradient(46% 120% at 88% 0,rgba(23,168,230,.30),transparent 60%);pointer-events:none}}
 .brandrow{{display:flex;align-items:center;gap:11px;position:relative}}
 .mark{{width:34px;height:34px;border-radius:9px;background:var(--grad);display:flex;align-items:flex-end;justify-content:center;gap:2.4px;padding:6px}}
 .mark i{{width:3.8px;border-radius:2px;background:#fff;display:block}} .mark i:nth-child(1){{height:6px;opacity:.6}} .mark i:nth-child(2){{height:12px;opacity:.82}} .mark i:nth-child(3){{height:17px}}
 .brandrow b{{font-size:19px;font-weight:800;letter-spacing:-.01em}} .brandrow small{{display:block;font-size:9px;font-weight:800;letter-spacing:2px;color:#9fb2cc;text-transform:uppercase;margin-top:-2px}}
 .badge{{margin-left:auto;font-size:10px;font-weight:800;letter-spacing:1.2px;text-transform:uppercase;color:#bfe0ff;border:1px solid rgba(191,224,255,.5);border-radius:999px;padding:5px 12px}}
 .top h1{{position:relative;font-size:clamp(25px,4vw,35px);font-weight:850;letter-spacing:-.025em;margin:20px 0 6px;text-wrap:balance}}
 .top p{{position:relative;color:#c3d2e6;font-size:15px;margin:0;max-width:60ch}}
 .hstat{{position:relative;display:inline-block;margin-top:20px;background:rgba(255,255,255,.09);border:1px solid rgba(255,255,255,.2);border-radius:13px;padding:13px 18px;font-size:15px;font-weight:700;color:#eaf3ff}}
 /* scorecard (deterministic) */
 .scorecard{{background:var(--bg);border:1px solid var(--line);border-radius:18px;box-shadow:var(--sh);margin:-26px 0 22px;padding:26px 28px;position:relative;z-index:2}}
 .sc-head{{display:flex;gap:22px;align-items:center;flex-wrap:wrap}}
 .dial{{width:104px;height:104px;border-radius:50%;flex:none;display:flex;align-items:center;justify-content:center}}
 .dial-in{{width:78px;height:78px;border-radius:50%;background:var(--bg);display:flex;flex-direction:column;align-items:center;justify-content:center;box-shadow:inset 0 0 0 1px var(--line)}}
 .dial-in b{{font-size:30px;font-weight:850;line-height:1;letter-spacing:-.02em}} .dial-in span{{font-size:12px;color:var(--faint);font-weight:700}}
 .sc-lead{{flex:1;min-width:230px}} .sc-band{{font-size:12px;font-weight:850;letter-spacing:1.6px;text-transform:uppercase}}
 .sc-lead h3{{font-size:20px;font-weight:850;letter-spacing:-.015em;margin:2px 0 6px}} .sc-lead p{{margin:0;color:var(--ink2);font-size:14.5px}}
 .dims{{display:grid;grid-template-columns:1fr 1fr;gap:16px 26px;margin-top:22px;padding-top:22px;border-top:1px solid var(--line)}}
 @media(max-width:600px){{.dims{{grid-template-columns:1fr}}}}
 .dim-t{{display:flex;justify-content:space-between;align-items:baseline;font-size:14px;font-weight:700}}
 .dim-t b{{font-weight:850;font-size:15px}} .dim-t b i{{font-style:normal;font-size:11px;color:var(--faint);font-weight:700}}
 .bar{{height:8px;border-radius:6px;background:#eef2f8;margin:6px 0 4px;overflow:hidden}}
 .bar span{{display:block;height:100%;border-radius:6px}}
 .dim-n{{font-size:12px;color:var(--muted)}}
 .statstrip{{display:grid;grid-template-columns:repeat(3,1fr);gap:14px;margin-top:22px}}
 @media(max-width:600px){{.statstrip{{grid-template-columns:1fr}}}}
 .stat{{background:var(--bg2);border:1px solid var(--line);border-radius:13px;padding:15px 16px}}
 .stat b{{display:block;font-size:23px;font-weight:850;letter-spacing:-.02em;color:var(--blue)}}
 .stat span{{font-size:12.5px;color:var(--muted);line-height:1.4;display:block;margin-top:3px}}
 /* narrative doc */
 .doc{{background:var(--bg);border:1px solid var(--line);border-radius:16px;box-shadow:var(--sh);margin:0 0 18px;padding:30px 34px}}
 .doc h2{{font-size:20px;font-weight:850;letter-spacing:-.015em;margin:34px 0 10px;padding-top:22px;border-top:1px solid var(--line);display:flex;align-items:flex-start;gap:11px;line-height:1.3}}
 .doc h2:first-of-type{{border-top:none;padding-top:0;margin-top:0}}
 .doc h2::before{{content:"";width:9px;height:9px;margin-top:9px;border-radius:50%;background:var(--grad);flex:none}}
 .doc h3{{font-size:15.5px;font-weight:800;margin:18px 0 6px;color:var(--ink)}}
 .doc p{{margin:0 0 12px;color:var(--ink2);font-size:15.5px}}
 .doc ul,.doc ol{{margin:0 0 14px;padding-left:22px}} .doc li{{margin:7px 0;color:var(--ink2)}}
 .doc strong,.doc b{{color:var(--ink);font-weight:750}} .doc em{{color:var(--blue);font-style:normal;font-weight:650}}
 .doc table{{width:100%;border-collapse:collapse;margin:6px 0 16px;font-size:14.5px}}
 .doc th,.doc td{{text-align:left;padding:9px 12px;border-bottom:1px solid var(--line)}}
 .doc th{{font-size:11px;font-weight:800;letter-spacing:.06em;text-transform:uppercase;color:var(--faint);background:var(--bg2)}}
 .doc td:last-child,.doc th:last-child{{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}}
 .callout{{background:linear-gradient(120deg,#eef5ff,#f5fbff);border:1px solid #d7e6fb;border-left:4px solid var(--blue);border-radius:12px;padding:16px 20px;margin:14px 0 18px}}
 .callout p{{margin:0}} .callout.win{{border-left-color:var(--green);background:linear-gradient(120deg,#eefaf2,#f4fdf8);border-color:#c8ecd6}}
 .cta{{background:linear-gradient(140deg,var(--navy),#12294a);color:#fff;border-radius:16px;padding:30px 30px;margin:8px 0 26px;text-align:center}}
 .cta h3{{color:#fff;font-size:20px;margin:0 0 8px}} .cta p{{color:#c9d8ec;margin:0 auto 16px;max-width:52ch}}
 .cta a{{display:inline-block;background:var(--grad);color:#fff;font-weight:800;text-decoration:none;padding:13px 26px;border-radius:11px;font-size:15px;box-shadow:0 12px 26px -12px rgba(23,168,230,.7)}}
 .foot{{text-align:center;color:var(--faint);font-size:12.5px;padding:2px 20px 34px;line-height:1.7}} .foot b{{color:var(--muted)}}
</style></head><body>
 <div class="top"><div class="wrap">
   <div class="brandrow"><span class="mark"><i></i><i></i><i></i></span><span><b>Digital&nbsp;Expo</b><small>DE Group</small></span><span class="badge">Growth Audit</span></div>
   <h1>How {c} can win more customers from Google</h1>
   <p>A plain-English look at where your business shows up online today, where you're missing out, and the fastest ways to fix it{(' — '+d) if d else ''}.</p>
   {stat}
 </div></div>
 <div class="wrap">{scorecard}<div class="doc">{inner}</div>
   <div class="cta"><h3>Want us to turn this into customers?</h3><p>We'll walk you through what this means for {c} and how quickly we can turn it around — in plain English, no pressure.</p><a href="tel:0370209196">📞 Book a 15-minute call · (03) 7020 9196</a></div>
 </div>
 <div class="foot"><b>Digital Expo · DE Group</b> — Google Partner digital marketing agency · digitalexpo.com.au<br/>Figures are careful estimates based on live Google search data for your website and your competitors.</div>
</body></html>'''


_SYSTEM = (
    "You are a senior growth strategist at DE Group (Digital Expo), a Google Partner digital-marketing agency "
    "in Australia, writing a premium GROWTH AUDIT a business owner will read and feel is genuinely valuable. "
    "The reader has ZERO marketing knowledge and no time. Rules: (1) Plain everyday English — no acronyms or "
    "jargon; if a term is unavoidable, define it once in brackets. (2) Lead EVERY section with a concrete "
    "real-world outcome the owner feels ('When someone near you Googles \"emergency plumber\", your business "
    "doesn't show up — your competitor does, and they get the call.'). (3) Turn every number into money, "
    "customers or plain meaning — never a bare metric. (4) Warm, confident, encouraging — you're showing a "
    "real opportunity, never scaring or shaming them. (5) Premium and skimmable: short paragraphs, a small "
    "<table> for any list of searches (2 columns: Search / Times searched a month), and exactly ONE "
    "<div class=\"callout\"> (or class=\"callout win\" for a positive point) per section as the key takeaway. "
    "Use <strong> for emphasis and <em> for the single most important sentence in a section. Output ONLY clean "
    "semantic HTML: <h2> per section then <p>/<ul>/<table>/<div class=callout>. NO <html>/<head>/<body>, NO "
    "markdown fences, NO preamble or sign-off. Do NOT restate the numeric health scores — those are shown "
    "separately above your text."
)


def gen_growth_audit(key: str, model: str, audit_model: dict, avg_ticket: float | None = None,
                     company: str = "") -> str | None:
    """Turn an assemble_audit() model into a premium, plain-language growth audit (deterministic scorecard +
    Opus-5 narrative). Returns None on any failure."""
    if not key or not audit_model:
        return None
    import json as _json
    domain = audit_model.get("domain") or ""
    name = _clean_name(audit_model.get("name") or "", company, domain)
    brief = _distill(audit_model, name, avg_ticket)
    user = (
        "Here is what our tools found about this business from live Google data (real search phrases, real "
        "competitors, real ad activity). Write their Growth Audit narrative from it.\n\n"
        + _json.dumps(brief, indent=1, default=str)
        + "\n\nWrite these <h2> sections IN THIS ORDER, using ONLY facts present above (skip a section "
        "gracefully if its data is empty — never invent numbers):\n"
        "1) 'The short version' — 3-4 sentences: where they stand and the single biggest opportunity.\n"
        "2) 'What your customers are searching for' — explain the buying journey in plain terms using "
        "buying_journey (just researching → comparing options → ready to buy now), with the monthly search "
        "sizes, and where they're already capturing vs missing that demand.\n"
        "3) 'Are people finding you on Google?' — how visible they are now; celebrate any searches they "
        "already win (searches_you_already_win) as proof they can rank.\n"
        "4) 'The searches you're missing' — the money searches where they don't show but competitors do; put "
        "the phrases in a small 2-column table. Cover both one_step_from_page_one and not_showing_at_all.\n"
        "5) 'Who's winning your customers' — name the competitors in who_is_beating_you and what that costs "
        "them in plain terms.\n"
        + ("6) 'The topics you haven't covered yet' — from topics_you_have_not_covered, explain the subject "
           "areas competitors have content for and they don't, and why filling them wins customers.\n" if brief.get("topics_you_have_not_covered") else "")
        + ("7) 'Your Google Ads' — from your_google_ads, plainly explain what they're running (how long, how "
           "many live ads, the mix of search/display/video) and whether it's working for them.\n" if brief.get("your_google_ads") else "")
        + "8) 'Are you ready for Google and AI search?' — using google_and_ai_search_readiness, explain in "
        "plain words what helps Google and AI tools (like ChatGPT) recommend a business, what they already "
        "have, and the simple things they're missing.\n"
        + ("9) 'What this is worth to you' — use ONLY opportunity_value_per_month (believable monthly value of "
           "missed searches). Frame it as the size of the prize per month; you MAY mention "
           "conservative_extra_jobs_per_month only if it's a small modest number. NEVER a large annual total.\n" if brief.get("opportunity_value_per_month") else "")
        + "10) 'Your 90-day plan' — turn ninety_day_plan and quick_wins into a clear, phased, plain-English "
        "action plan (what we'd do first, next, then) framed as how we'd grow their business.\n"
        "Australian spelling and tone. Keep the whole thing readable in 4-5 minutes."
    )
    try:
        inner = _claude_text(key, model, _SYSTEM, user, max_tokens=6000)
    except Exception:
        return None
    if not inner or "<h2" not in inner.lower():
        return None
    t = inner.strip()
    if t.startswith("```"):
        nl = t.find("\n"); t = t[nl + 1:] if nl != -1 else t[3:]
    if t.rstrip().endswith("```"):
        t = t.rstrip()[:-3]
    scorecard = _scorecard(audit_model)
    opp = audit_model.get("opportunity") or {}
    gm = int((opp.get("gap_capturable") or 0) + (opp.get("quickwin_value") or 0))
    hs = (f"About {_fmt_money(gm)}/month in customer searches are going to your competitors — that's the gap we can close"
          if gm >= 300 else "")
    return _shell(name, domain, scorecard, t.strip(), hs)
