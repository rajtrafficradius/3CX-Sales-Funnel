"""GROWTH AUDIT — a focused, prospect-friendly DIGITAL-MARKETING audit.

Vysakh's brief (repeated): a normal business owner must understand it, it must FEEL premium, and it must be
FOCUSED on the digital-marketing opportunity — where you show up on Google, the SEO/Google-Ads gaps, and
closing them to win more customers — NOT rambling general business advice.

So this is rendered as clean, SCANNABLE, mostly-DETERMINISTIC sections (a health scorecard, visibility,
money-searches tables, product-area clusters, competitors, Google Ads, Google/AI readiness, a 90-day plan) —
exactly the structure of our full audit engine, but with plain-English labels and one short tailored intro
line per section (a single constrained Opus call, so it can't ramble). Grounded $ only (gap_capturable +
quick-win), never the inflated traffic×conversion revenue. Fully guarded: returns None on any failure.
"""
import html as _h
import re as _re
import json as _json
from .reveal_guide import _claude_text


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


def _e(s):
    return _h.escape(str(s if s is not None else ""))


# --------------------------------------------------------------------------- distil ---

def _distill(m: dict, name: str, avg_ticket) -> dict:
    opp = m.get("opportunity") or {}
    biz = m.get("business") or {}
    seo = m.get("seo") or {}
    ads = m.get("ads") or {}
    geo = m.get("geo_aeo") or {}
    uni = m.get("universe") or {}
    rec = m.get("recommendation") or {}

    def kw(rows, n):
        out = []
        for r in (rows or [])[:n]:
            k = (r.get("keyword") or "").strip()
            if k:
                out.append({"phrase": k, "vol": r.get("volume") or r.get("search_volume"), "pos": r.get("position")})
        return out

    clusters = []
    for c in (uni.get("clusters") or [])[:8]:
        top = c.get("top") or []
        theme = (top[0].get("keyword") if top else None)
        if theme and c.get("volume"):
            clusters.append({"area": theme, "vol": c.get("volume"), "ranked": c.get("ranked") or 0,
                             "missing": c.get("gap") or 0,
                             "examples": [t.get("keyword") for t in top[:3] if t.get("keyword")]})
    comps = []
    for c in (m.get("competitors") or [])[:5]:
        nm = c.get("domain") or c.get("name")
        if nm:
            comps.append({"name": nm, "traffic": c.get("est_traffic") or 0})
    grounded = int((opp.get("gap_capturable") or 0) + (opp.get("quickwin_value") or 0))
    return {
        "business": name, "industry": biz.get("industry"), "location": biz.get("location"),
        "grounded_value_month": grounded or None,
        "visitors_from_google_month": opp.get("est_org_traffic"),
        "searches_you_rank_for": opp.get("org_keywords") or (uni.get("totals") or {}).get("ranked"),
        "top_rankings": kw(seo.get("proof_winning"), 6),
        "near_page1": kw(seo.get("money_keywords"), 8),
        "missing_competitors_win": kw(m.get("keyword_gap"), 8),
        "clusters": clusters,
        "competitors": comps,
        "ads": ({"running": bool(ads.get("running")), "live_ads": ads.get("count"),
                 "years": ads.get("years_active") or ads.get("years"),
                 "formats": ads.get("formats") or {}} if (ads.get("running") or ads.get("count")) else None),
        "ai_score": geo.get("score"),
        "ai_missing": [c.get("note") for c in (geo.get("checks") or []) if not c.get("pass")][:6],
        "ai_have": [c.get("name") for c in (geo.get("checks") or []) if c.get("pass")][:5],
        "plan": [{"move": (s[0] if isinstance(s, (list, tuple)) else s.get("title")),
                  "why": (s[1] if isinstance(s, (list, tuple)) else s.get("detail"))}
                 for s in (rec.get("steps") or [])[:5]
                 if (isinstance(s, (list, tuple)) and len(s) >= 2) or (isinstance(s, dict) and s.get("title"))],
    }


# --------------------------------------------------------------------------- intros (one constrained call) ---

_INTRO_SYS = (
    "You write ONE short plain-English intro line for each section of a digital-marketing audit that a "
    "non-technical Australian business owner will read. Rules: each intro is 1–2 short sentences; plain "
    "everyday words, NO jargon or acronyms; focused ONLY on getting more customers from Google (search "
    "visibility, the searches they're missing, competitors, Google Ads, being found by Google/AI); NEVER give "
    "generic business advice, NEVER mention software/tools, NEVER invent numbers. Lead with the real-world "
    "outcome. Output ONLY a JSON object with these exact keys (string values): "
    "bottom_line, visibility, money_searches, areas, competitors, ads, ai, plan. Nothing else."
)


def _intros(key, model, brief) -> dict:
    if not key:
        return {}
    try:
        txt = _claude_text(key, model, _INTRO_SYS,
                           "Business: " + _json.dumps(brief, default=str)[:3500], max_tokens=700).strip()
        if txt.startswith("```"):
            txt = txt.split("\n", 1)[1] if "\n" in txt else txt[3:]
            if txt.rstrip().endswith("```"):
                txt = txt.rstrip()[:-3]
        i, j = txt.find("{"), txt.rfind("}")
        return _json.loads(txt[i:j + 1]) if i >= 0 and j > i else {}
    except Exception:
        return {}


# --------------------------------------------------------------------------- deterministic renderers ---

def _sec(title, intro, body, sub=""):
    ih = f'<p class="intro">{_e(intro)}</p>' if intro else ""
    sh = f'<div class="subttl">{sub}</div>' if sub else ""
    return f'<section class="sec"><h2>{_e(title)}</h2>{ih}{sh}{body}</section>'


def _kw_table(rows, head="Search", posc=False):
    if not rows:
        return ""
    trs = ""
    for r in rows:
        pos = f'<td class="num">#{_e(r.get("pos"))}</td>' if (posc and r.get("pos")) else ("<td class=\"num\">—</td>" if posc else "")
        trs += f'<tr><td>{_e(r.get("phrase"))}</td>{pos}<td class="num">{_n(r.get("vol")) if r.get("vol") else "—"}</td></tr>'
    posh = "<th>Where you rank</th>" if posc else ""
    return (f'<div class="tbl"><table><thead><tr><th>{_e(head)}</th>{posh}'
            f'<th class="num">Searches / month</th></tr></thead><tbody>{trs}</tbody></table></div>')


def _cluster_table(clusters):
    if not clusters:
        return ""
    trs = ""
    for c in clusters:
        ex = ", ".join(c.get("examples") or [])
        trs += (f'<tr><td><b>{_e(c.get("area"))}</b>{("<span class=ex>"+_e(ex)+"</span>") if ex else ""}</td>'
                f'<td class="num">{_n(c.get("vol"))}</td>'
                f'<td class="num"><span class="ok">{_e(c.get("ranked"))}</span></td>'
                f'<td class="num"><span class="miss">{_e(c.get("missing"))}</span></td></tr>')
    return ('<div class="tbl"><table><thead><tr><th>Product / service area</th><th class="num">Searches/mo</th>'
            '<th class="num">You rank for</th><th class="num">You\'re missing</th></tr></thead>'
            f'<tbody>{trs}</tbody></table></div>')


def _competitors(comps):
    if not comps:
        return ""
    mx = max((c.get("traffic") or 0) for c in comps) or 1
    rows = ""
    for c in comps:
        t = c.get("traffic") or 0
        w = max(6, round(100 * t / mx))
        rows += (f'<div class="crow"><div class="cname">{_e(c.get("name"))}</div>'
                 f'<div class="cbar"><span style="width:{w}%"></span></div>'
                 f'<div class="cval">{_n(t)}/mo</div></div>')
    return f'<div class="comps">{rows}</div>'


def _ads_block(ads):
    if not ads:
        return ('<div class="note note-b"><b>You\'re not running Google Ads right now.</b> That\'s the fastest '
                'way to appear at the very top for the "ready to buy" searches while your unpaid rankings grow.</div>')
    chips = []
    if ads.get("years"):
        chips.append(("Running for", f"{ads['years']} yrs"))
    if ads.get("live_ads"):
        chips.append(("Live ads now", _n(ads["live_ads"])))
    for k, v in (ads.get("formats") or {}).items():
        chips.append((k, _n(v)))
    ch = "".join(f'<div class="chip"><b>{_e(v)}</b><span>{_e(k)}</span></div>' for k, v in chips)
    return f'<div class="chips">{ch}</div>'


def _geo(score, have, missing):
    bar = ""
    if score is not None:
        _, col = _band(score)
        bar = (f'<div class="gscore"><div class="gbar"><span style="width:{max(4,min(100,int(score)))}%;background:{col}"></span></div>'
               f'<b style="color:{col}">{int(score)}/100</b></div>')
    mi = "".join(f'<li class="x">{_e(x)}</li>' for x in (missing or []))
    hv = "".join(f'<li class="c">{_e(x)}</li>' for x in (have or []))
    cols = ""
    if hv:
        cols += f'<div><div class="glab">Already in place</div><ul class="glist">{hv}</ul></div>'
    if mi:
        cols += f'<div><div class="glab">Simple things you\'re missing</div><ul class="glist">{mi}</ul></div>'
    return bar + (f'<div class="gcols">{cols}</div>' if cols else "")


def _plan(steps):
    if not steps:
        return ""
    items = ""
    for i, s in enumerate(steps, 1):
        items += (f'<div class="pstep"><span class="pn">{i}</span><div><b>{_e(s.get("move"))}</b>'
                  f'{("<p>"+_e(s.get("why"))+"</p>") if s.get("why") else ""}</div></div>')
    return f'<div class="plan">{items}</div>'


def _scorecard(m: dict) -> str:
    h = m.get("health") or {}
    opp = m.get("opportunity") or {}
    overall = int(h.get("overall") or 0)
    oband, ocol = _band(overall)
    dims = [("Getting found on Google", h.get("seo"), "your visibility in normal (unpaid) search"),
            ("Google Ads", h.get("ads"), "how well your paid ads work"),
            ("Standing out from rivals", h.get("competitive"), "your share next to competitors"),
            ("Website &amp; tech health", h.get("technical"), "speed, mobile &amp; being readable by Google/AI")]
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
        stats.append((_n(miss) + "+", "monthly searches you could show up for"))
    if len(m.get("competitors") or []):
        stats.append((str(len(m["competitors"])), "competitors currently ahead of you online"))
    stat_html = "".join(f'<div class="stat"><b>{v}</b><span>{lab}</span></div>' for v, lab in stats[:3])
    deg = max(0, min(360, round(overall * 3.6)))
    return (
        '<div class="scorecard"><div class="sc-head">'
        f'<div class="dial" style="background:conic-gradient({ocol} {deg}deg,#e7edf5 {deg}deg)">'
        f'<div class="dial-in"><b>{overall}</b><span>/100</span></div></div>'
        f'<div class="sc-lead"><div class="sc-band" style="color:{ocol}">{oband}</div>'
        '<h3>Your online health score</h3>'
        '<p>A quick read on how your business shows up online today. The lower the score, the bigger the room '
        'to grow — the sections below show exactly where.</p></div></div>'
        f'<div class="dims">{bars}</div>'
        + (f'<div class="statstrip">{stat_html}</div>' if stat_html else "") + '</div>')


def _shell(company, domain, scorecard, sections, headline) -> str:
    c = _e(company); d = _e(domain)
    hs = f'<div class="hstat">{_e(headline)}</div>' if headline else ""
    return f'''<!doctype html><html lang="en"><head><meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/><title>Growth Audit — {c}</title>
<style>
 :root{{--ink:#0b1524;--ink2:#28364c;--muted:#5b6d85;--faint:#8595a9;--bg:#fff;--bg2:#f4f8fd;--line:#e4ebf4;
   --navy:#0a1930;--blue:#1f5fd0;--cyan:#17a8e6;--green:#0f9d58;--amber:#f0a020;--red:#e0533d;
   --grad:linear-gradient(115deg,#1f5fd0,#17a8e6);--sh:0 2px 8px rgba(11,21,36,.06),0 22px 48px -30px rgba(11,21,36,.32);--sh-sm:0 1px 3px rgba(11,21,36,.08)}}
 *{{box-sizing:border-box}} body{{margin:0;background:var(--bg2);color:var(--ink);font:16px/1.65 ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,Arial;-webkit-font-smoothing:antialiased}}
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
 .scorecard{{background:var(--bg);border:1px solid var(--line);border-radius:18px;box-shadow:var(--sh);margin:-26px 0 20px;padding:26px 28px;position:relative;z-index:2}}
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
 .bar{{height:8px;border-radius:6px;background:#eef2f8;margin:6px 0 4px;overflow:hidden}} .bar span{{display:block;height:100%;border-radius:6px}}
 .dim-n{{font-size:12px;color:var(--muted)}}
 .statstrip{{display:grid;grid-template-columns:repeat(3,1fr);gap:14px;margin-top:22px}}
 @media(max-width:600px){{.statstrip{{grid-template-columns:1fr}}}}
 .stat{{background:var(--bg2);border:1px solid var(--line);border-radius:13px;padding:15px 16px}}
 .stat b{{display:block;font-size:23px;font-weight:850;letter-spacing:-.02em;color:var(--blue)}} .stat span{{font-size:12.5px;color:var(--muted);line-height:1.4;display:block;margin-top:3px}}
 /* sections */
 .sec{{background:var(--bg);border:1px solid var(--line);border-radius:16px;box-shadow:var(--sh-sm);padding:24px 26px;margin:0 0 16px}}
 .sec h2{{font-size:19px;font-weight:850;letter-spacing:-.015em;margin:0 0 6px;display:flex;align-items:center;gap:10px}}
 .sec h2::before{{content:"";width:9px;height:9px;border-radius:50%;background:var(--grad);flex:none}}
 .intro{{margin:0 0 14px;color:var(--ink2);font-size:15px;line-height:1.6}}
 .subttl{{font-size:12px;font-weight:800;letter-spacing:.05em;text-transform:uppercase;color:var(--faint);margin:14px 0 6px}}
 .tbl{{overflow-x:auto;border:1px solid var(--line);border-radius:11px;margin:4px 0 6px}}
 table{{width:100%;border-collapse:collapse;font-size:14px}}
 th{{text-align:left;font-size:11px;font-weight:800;letter-spacing:.05em;text-transform:uppercase;color:var(--faint);background:var(--bg2);padding:9px 13px;border-bottom:1px solid var(--line)}}
 td{{padding:10px 13px;border-bottom:1px solid var(--line);color:var(--ink2)}} tr:last-child td{{border-bottom:none}}
 td.num,th.num{{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}}
 .ex{{display:block;font-size:12px;color:var(--faint);margin-top:2px}}
 .ok{{color:var(--green);font-weight:800}} .miss{{color:var(--red);font-weight:800}}
 .comps{{display:flex;flex-direction:column;gap:10px}}
 .crow{{display:grid;grid-template-columns:1fr 2fr auto;align-items:center;gap:12px}}
 @media(max-width:520px){{.crow{{grid-template-columns:1fr auto}}.cbar{{display:none}}}}
 .cname{{font-weight:700;font-size:14px;word-break:break-all}}
 .cbar{{height:12px;background:#eef2f8;border-radius:7px;overflow:hidden}} .cbar span{{display:block;height:100%;background:var(--grad);border-radius:7px}}
 .cval{{font-size:12.5px;color:var(--muted);font-variant-numeric:tabular-nums;white-space:nowrap}}
 .chips{{display:flex;flex-wrap:wrap;gap:12px}}
 .chip{{background:var(--bg2);border:1px solid var(--line);border-radius:12px;padding:12px 16px;min-width:96px}}
 .chip b{{display:block;font-size:19px;font-weight:850;color:var(--blue)}} .chip span{{font-size:12px;color:var(--muted)}}
 .gscore{{display:flex;align-items:center;gap:12px;margin:2px 0 14px}} .gbar{{flex:1;height:9px;border-radius:6px;background:#eef2f8;overflow:hidden}} .gbar span{{display:block;height:100%;border-radius:6px}}
 .gcols{{display:grid;grid-template-columns:1fr 1fr;gap:18px}} @media(max-width:560px){{.gcols{{grid-template-columns:1fr}}}}
 .glab{{font-size:12px;font-weight:800;letter-spacing:.04em;text-transform:uppercase;color:var(--faint);margin-bottom:6px}}
 .glist{{list-style:none;margin:0;padding:0}} .glist li{{position:relative;padding:5px 0 5px 24px;font-size:13.5px;color:var(--ink2)}}
 .glist li::before{{position:absolute;left:0;top:5px;font-weight:800}} .glist li.c::before{{content:"✓";color:var(--green)}} .glist li.x::before{{content:"→";color:var(--amber)}}
 .plan{{display:flex;flex-direction:column;gap:12px}}
 .pstep{{display:flex;gap:13px;align-items:flex-start}} .pn{{flex:none;width:28px;height:28px;border-radius:8px;background:var(--grad);color:#fff;font-weight:850;font-size:14px;display:flex;align-items:center;justify-content:center}}
 .pstep b{{font-size:15px;font-weight:750}} .pstep p{{margin:3px 0 0;color:var(--ink2);font-size:14px}}
 .note-b{{background:var(--bg2);border:1px solid var(--line);border-left:3px solid var(--blue);border-radius:10px;padding:13px 16px;font-size:14.5px;color:var(--ink2)}}
 .cta{{background:linear-gradient(140deg,var(--navy),#12294a);color:#fff;border-radius:16px;padding:30px;margin:6px 0 26px;text-align:center}}
 .cta h3{{color:#fff;font-size:20px;margin:0 0 8px}} .cta p{{color:#c9d8ec;margin:0 auto 16px;max-width:52ch}}
 .cta a{{display:inline-block;background:var(--grad);color:#fff;font-weight:800;text-decoration:none;padding:13px 26px;border-radius:11px;font-size:15px}}
 .foot{{text-align:center;color:var(--faint);font-size:12.5px;padding:2px 20px 34px;line-height:1.7}} .foot b{{color:var(--muted)}}
</style></head><body>
 <div class="top"><div class="wrap">
   <div class="brandrow"><span class="mark"><i></i><i></i><i></i></span><span><b>Digital&nbsp;Expo</b><small>DE Group</small></span><span class="badge">Growth Audit</span></div>
   <h1>How {c} can win more customers from Google</h1>
   <p>A plain-English look at where your business shows up on Google today, the searches you're missing, and how we'd close the gap{(' — '+d) if d else ''}.</p>
   {hs}
 </div></div>
 <div class="wrap">{scorecard}{sections}
   <div class="cta"><h3>Want us to close these gaps for you?</h3><p>We'll walk you through exactly what this means for {c} and how quickly we can turn missed searches into customers — in plain English.</p><a href="tel:0370209196">📞 Book a 15-minute call · (03) 7020 9196</a></div>
 </div>
 <div class="foot"><b>Digital Expo · DE Group</b> — Google Partner digital marketing agency · digitalexpo.com.au<br/>Figures are careful estimates based on live Google search data for your website and your competitors.</div>
</body></html>'''


def gen_growth_audit(key: str, model: str, audit_model: dict, avg_ticket: float | None = None,
                     company: str = "") -> str | None:
    if not key or not audit_model:
        return None
    domain = audit_model.get("domain") or ""
    name = _clean_name(audit_model.get("name") or "", company, domain)
    b = _distill(audit_model, name, avg_ticket)
    intros = _intros(key, model, b) or {}
    secs = []
    # bottom line
    bl = intros.get("bottom_line") or (
        f"Right now you're winning a handful of searches but missing the everyday ones your customers type most "
        f"— that's where the growth is.")
    secs.append(_sec("The bottom line", bl,
                     (f'<div class="note-b">The realistic value of the customer searches currently going to your '
                      f'competitors is about <b>{_fmt_money(b["grounded_value_month"])} a month</b> — that\'s the gap '
                      f'we\'d work to close.</div>') if b.get("grounded_value_month") else ""))
    # visibility
    vis_body = ""
    if b.get("visitors_from_google_month") or b.get("searches_you_rank_for"):
        vis_body += (f'<div class="chips"><div class="chip"><b>{_n(b.get("visitors_from_google_month") or 0)}</b>'
                     f'<span>visitors/mo from Google (unpaid)</span></div>'
                     f'<div class="chip"><b>{_n(b.get("searches_you_rank_for") or 0)}</b>'
                     f'<span>searches you currently show for</span></div></div>')
    if b.get("top_rankings"):
        vis_body += '<div class="subttl">Searches you already rank near the top for</div>' + _kw_table(b["top_rankings"], "Search", posc=True)
    if vis_body:
        secs.append(_sec("Where you show up on Google today", intros.get("visibility"), vis_body))
    # money searches missing
    ms_body = ""
    if b.get("near_page1"):
        ms_body += '<div class="subttl">One push from page one (you\'re already on page 2)</div>' + _kw_table(b["near_page1"], "Search")
    if b.get("missing_competitors_win"):
        ms_body += '<div class="subttl">You don\'t show at all — competitors do</div>' + _kw_table(b["missing_competitors_win"], "Search")
    if ms_body:
        secs.append(_sec("The money searches you're missing", intros.get("money_searches"), ms_body))
    # clusters
    if b.get("clusters"):
        secs.append(_sec("Your customers' search areas", intros.get("areas"), _cluster_table(b["clusters"])))
    # competitors
    if b.get("competitors"):
        secs.append(_sec("Who's ahead of you online", intros.get("competitors"), _competitors(b["competitors"])))
    # ads
    secs.append(_sec("Your Google Ads", intros.get("ads"), _ads_block(b.get("ads"))))
    # ai readiness
    if b.get("ai_score") is not None or b.get("ai_missing"):
        secs.append(_sec("Getting found by Google & AI", intros.get("ai"), _geo(b.get("ai_score"), b.get("ai_have"), b.get("ai_missing"))))
    # plan
    if b.get("plan"):
        secs.append(_sec("Your 90-day growth plan", intros.get("plan"), _plan(b["plan"])))

    scorecard = _scorecard(audit_model)
    gm = b.get("grounded_value_month")
    hs = (f"About {_fmt_money(gm)}/month in customer searches are going to your competitors — that's the gap we can close"
          if (gm and gm >= 300) else "")
    return _shell(name, domain, scorecard, "".join(secs), hs)
