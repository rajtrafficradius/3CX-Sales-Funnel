"""Opus-5-generated WEBSITE REVEAL PLAYBOOK for the closer (Alfred) — tailored per prospect: what to show,
what to say, how to handle objections, how to close. Wrapped in a clean DE-branded internal document.
Self-contained (no heavy deps). Generation is guarded — returns None on any failure.
"""
import json, urllib.request, html as _h

ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"


def _claude_text(key: str, model: str, system: str, user: str, max_tokens: int = 4000) -> str:
    """Non-streaming Anthropic call; concatenates TEXT blocks only (ignores Opus-5 thinking blocks)."""
    body = {"model": model, "max_tokens": max_tokens, "system": system,
            "messages": [{"role": "user", "content": user}]}
    req = urllib.request.Request(ANTHROPIC_URL, data=json.dumps(body).encode(), method="POST",
                                 headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                                          "content-type": "application/json"})
    r = json.loads(urllib.request.urlopen(req, timeout=150).read())
    return "".join(b.get("text", "") for b in (r.get("content") or []) if b.get("type") == "text").strip()


def _shell(company: str, domain: str, inner: str) -> str:
    c = _h.escape(company or "the prospect"); d = _h.escape(domain or "")
    return f'''<!doctype html><html lang="en"><head><meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/><title>Reveal Playbook — {c}</title>
<style>
 :root{{--ink:#0b1524;--ink2:#28364c;--muted:#5b6d85;--faint:#8595a9;--bg:#fff;--bg2:#f5f8fc;--line:#e2e9f3;
   --navy:#0a1930;--blue:#1f5fd0;--cyan:#17a8e6;--green:#10a35d;--gold:#f5b638;--grad:linear-gradient(115deg,#1f5fd0,#17a8e6);
   --sh:0 2px 8px rgba(11,21,36,.06),0 22px 48px -30px rgba(11,21,36,.3)}}
 *{{box-sizing:border-box}} body{{margin:0;background:var(--bg2);color:var(--ink);font:15.5px/1.65 ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,Arial;-webkit-font-smoothing:antialiased}}
 .wrap{{max-width:760px;margin:0 auto;padding:0 20px}}
 .top{{background:linear-gradient(140deg,var(--navy),#122645);color:#fff;padding:28px 0 30px;position:relative;overflow:hidden}}
 .top::after{{content:"";position:absolute;inset:0;background:radial-gradient(44% 130% at 90% 0,rgba(23,168,230,.28),transparent 60%);pointer-events:none}}
 .brandrow{{display:flex;align-items:center;gap:11px;position:relative}}
 .mark{{width:32px;height:32px;border-radius:8px;background:var(--grad);display:flex;align-items:flex-end;justify-content:center;gap:2.3px;padding:6px}}
 .mark i{{width:3.6px;border-radius:2px;background:#fff;display:block}} .mark i:nth-child(1){{height:6px;opacity:.6}} .mark i:nth-child(2){{height:11px;opacity:.82}} .mark i:nth-child(3){{height:16px}}
 .brandrow b{{font-size:18px;font-weight:800;letter-spacing:-.01em}} .brandrow small{{display:block;font-size:9px;font-weight:800;letter-spacing:2px;color:#9fb2cc;text-transform:uppercase;margin-top:-2px}}
 .badge{{margin-left:auto;font-size:10px;font-weight:800;letter-spacing:1.2px;text-transform:uppercase;color:var(--gold);border:1px solid var(--gold);border-radius:999px;padding:5px 12px}}
 .top h1{{position:relative;font-size:clamp(22px,3.6vw,28px);font-weight:850;letter-spacing:-.02em;margin:16px 0 4px}}
 .top p{{position:relative;color:#c3d2e6;font-size:14px;margin:0}}
 .doc{{background:var(--bg);border:1px solid var(--line);border-radius:14px;box-shadow:var(--sh);margin:22px 0 30px;padding:26px 28px}}
 .doc h3{{font-size:16.5px;font-weight:850;letter-spacing:-.01em;margin:26px 0 8px;padding-top:16px;border-top:1px solid var(--line);color:var(--ink);display:flex;align-items:center;gap:9px}}
 .doc h3:first-of-type{{border-top:none;padding-top:0;margin-top:0}}
 .doc h3::before{{content:"";width:8px;height:8px;border-radius:50%;background:var(--grad);flex:none}}
 .doc p{{margin:0 0 10px;color:var(--ink2);font-size:14.5px;line-height:1.62}}
 .doc ul,.doc ol{{margin:0 0 12px;padding-left:22px}} .doc li{{margin:5px 0;color:var(--ink2);font-size:14.5px;line-height:1.55}}
 .doc strong,.doc b{{color:var(--ink);font-weight:750}}
 .doc em{{color:var(--blue);font-style:normal;font-weight:600}}
 .foot{{text-align:center;color:var(--faint);font-size:12px;padding:2px 20px 30px;line-height:1.7}}
</style></head><body>
 <div class="top"><div class="wrap">
   <div class="brandrow"><span class="mark"><i></i><i></i><i></i></span><span><b>Digital&nbsp;Expo</b><small>DE Group</small></span><span class="badge">Internal · for Alfred</span></div>
   <h1>Reveal playbook — {c}</h1>
   <p>How to present the new website{(' for '+d) if d else ''} and close the deal.</p>
 </div></div>
 <div class="wrap"><div class="doc">{inner}</div></div>
 <div class="foot"><b>Digital Expo · DE Group</b> — internal sales playbook · not for the client</div>
</body></html>'''


def gen_reveal_guide(key: str, model: str, company: str, domain: str, industry: str,
                     finding: str, quote_line: str) -> str | None:
    """Generate the reveal playbook HTML with Opus 5. Returns None on any failure."""
    if not key:
        return None
    system = ("You are an elite sales coach for a premium web design & digital marketing agency "
              "(DE Group / Digital Expo). Write a concise, tactical WEBSITE REVEAL PLAYBOOK the closer will "
              "use live on a screen-share to present a brand-new website we built for a prospect and convert "
              "the deal. Be specific — give example phrasing they can say almost verbatim. Output ONLY clean "
              "semantic HTML: <h3> for each section title, then <p>/<ul><li>. Use <strong> for emphasis and "
              "<em> for lines to say out loud. No <html>/<head>/<body>, no markdown fences, no preamble.")
    user = (f"Prospect: {company or 'the business'} — a {industry or 'local Australian'} business"
            f"{(' ('+domain+')') if domain else ''}.\n"
            f"Why they booked / the hook: {finding or 'we rebuilt their outdated website and invited them to see it.'}\n"
            "The new site is bespoke (not a template), premium, mobile-first, fast, with cinematic motion, "
            "clear calls to action, a working enquiry form, and SEO set up at launch.\n"
            f"The offer to close on: {quote_line}.\n\n"
            "Write the playbook with these <h3> sections in order: "
            "1) 30-second context (who they are, what they care about). "
            "2) Opening the screen-share (exact first lines to say). "
            "3) The 'wow' moment — what to show first and why. "
            "4) Section-by-section walkthrough (hero, services/work, about, contact) — for each, what to point "
            "out AND example phrasing to say. "
            "5) The value story — tie the site to more enquiries, trust, mobile customers and speed for THEIR business. "
            "6) Handling the 3 most likely objections (price, 'I'll think about it', 'can you change X') with exact responses. "
            "7) The close — how to transition to the quotation and ask for the go-ahead. "
            "8) Do's & don'ts (tone, pace, what not to say). "
            "Keep it tight and usable live on a call.")
    try:
        inner = _claude_text(key, model, system, user, max_tokens=4500)
    except Exception:
        return None
    if not inner or "<h3" not in inner.lower():
        return None
    # strip any stray code fences the model may add
    t = inner.strip()
    if t.startswith("```"):
        nl = t.find("\n"); t = t[nl + 1:] if nl != -1 else t[3:]
    if t.rstrip().endswith("```"):
        t = t.rstrip()[:-3]
    return _shell(company, domain, t.strip())
