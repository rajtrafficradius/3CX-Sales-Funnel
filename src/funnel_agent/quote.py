"""Reusable DE Group website-quote generator — editorial 'paper' layout (the approved Foremore design),
rebranded to Digital Expo / DE Group (NO Traffic Radius). The TOTAL drives a 6-item breakdown; Australian
10% GST is added; ongoing web+email hosting shown separately. Self-contained, centered, print-clean.
Usage: gen_quote_html(company, domain, total) -> html string.
"""
import html as _h
from datetime import datetime, timedelta, timezone

ITEMS = [
    ("UX/UI Design & Art Direction", 0.22, "Bespoke identity, layout system, type scale & colour — unique to your brand, not a theme."),
    ("Front-End Engineering", 0.30, "A modern, component-based build — fully responsive, fast, with smooth routing across every page."),
    ("Copywriting & Content Structuring", 0.17, "Professional copy and section architecture across all pages — rich and persuasive, never thin."),
    ("Motion & Interaction Design", 0.14, "Scroll reveals, kinetic type, parallax, page transitions & ambient graphics that make it feel alive."),
    ("Brand Asset Integration", 0.07, "Your logo, embedded premium fonts & optimised imagery — fully self-contained and on-brand."),
    ("Testing, QA & Launch", 0.10, "Cross-device QA, accessibility & performance pass, domain connection and go-live."),
]

def _breakdown(total: int):
    raw = [(n, round((w * total) / 5) * 5, d) for (n, w, d) in ITEMS]
    diff = total - sum(p for _, p, _ in raw)
    if diff:
        i = max(range(len(raw)), key=lambda k: raw[k][1])
        raw[i] = (raw[i][0], raw[i][1] + diff, raw[i][2])
    return raw


def normalise_items(items) -> list[tuple[str, int, str]]:
    """Coerce a closer-edited line-item list into the (name, amount, description) rows the quote
    renders. Alfred prices per prospect, so his numbers are AUTHORITATIVE — they are never rescaled
    to some expected total, and the quote total becomes whatever his lines add up to.

    Accepts [{name, amount, desc}] or [[name, amount, desc]]. Rows with no name AND no amount are
    dropped (the UI ships one empty row for adding). Returns [] when nothing usable is left, so the
    caller can fall back to the weighted default breakdown.
    """
    out: list[tuple[str, int, str]] = []
    for it in (items or []):
        if isinstance(it, dict):
            name, amount, desc = it.get("name"), it.get("amount"), it.get("desc")
        elif isinstance(it, (list, tuple)) and len(it) >= 2:
            name, amount, desc = it[0], it[1], (it[2] if len(it) > 2 else "")
        else:
            continue
        name = str(name or "").strip()[:120]
        desc = str(desc or "").strip()[:400]
        try:
            amount = int(round(float(amount or 0)))
        except (TypeError, ValueError):
            amount = 0
        if not name and amount == 0:
            continue
        out.append((name or "Line item", max(0, amount), desc))
    return out

def _money(n): return f"A${n:,.0f}"

def gen_quote_html(company: str, domain: str, total: int = 1000, hosting_mo: int = 100,
                   attn: str = "", issued=None, items=None) -> str:
    """items: optional closer-edited line items (see normalise_items). When supplied they REPLACE the
    weighted default breakdown and their sum becomes the total — Alfred prices each prospect himself,
    so his figures win over the ratio model."""
    c = _h.escape(company or "your business"); d = _h.escape(domain or ""); at = _h.escape(attn or "")
    custom = normalise_items(items)
    if custom:
        rows = custom
        total = sum(p for _, p, _ in rows)
    else:
        total = int(total)
        rows = _breakdown(total)
    gst = round(total * 0.10); grand = total + gst
    host_yr = hosting_mo * 10  # two months free
    try:
        today = issued or datetime.now(timezone.utc).astimezone().date()
    except Exception:
        today = datetime.now().date()
    issued_s = today.strftime("%d %b %Y")
    valid_s = (today + timedelta(days=30)).strftime("%d %b %Y")
    qno = "DE-" + today.strftime("%Y%m%d")
    items_html = "".join(
        f'''<div class="li"><div class="li-n">{i+1:02d}</div><div class="li-b"><div class="li-t">{_h.escape(n)}</div>
        <div class="li-d">{_h.escape(desc)}</div></div><div class="li-p">{_money(p)}</div></div>'''
        for i, (n, p, desc) in enumerate(rows))
    return f'''<!doctype html><html lang="en"><head><meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/><title>Quotation — {c}</title>
<style>
 :root{{--paper:#e9e6dd;--sheet:#faf8f2;--ink:#17150f;--ink2:#3b382f;--muted:#6f6a5d;--faint:#9a9384;--line:#ddd8cb;--line2:#c9c3b3;
   --accent:#dc4632;--good:#2f7d4f;--serif:Georgia,"Times New Roman",Cambria,serif;--sans:ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,Helvetica,Arial}}
 *{{box-sizing:border-box}} body{{margin:0;background:var(--paper);color:var(--ink);font:15.5px/1.62 var(--sans);-webkit-font-smoothing:antialiased;
   font-variant-numeric:tabular-nums;padding:clamp(14px,4vw,52px) clamp(10px,4vw,40px)}}
 a{{color:var(--accent);text-decoration:none}}
 .sheet{{max-width:880px;margin:0 auto;background:var(--sheet);border:1px solid var(--line);border-radius:5px;
   box-shadow:0 1px 0 rgba(255,255,255,.6) inset,0 30px 80px -44px rgba(20,19,14,.42);overflow:hidden}}
 .pad{{padding:clamp(26px,5vw,54px)}}
 .eyebrow{{font-size:10.5px;font-weight:800;letter-spacing:1.6px;text-transform:uppercase;color:var(--accent)}}
 /* header */
 .hd{{display:flex;align-items:flex-start;gap:18px;flex-wrap:wrap}}
 .logo{{display:flex;align-items:center;gap:10px}}
 .mark{{width:30px;height:30px;border-radius:7px;background:linear-gradient(120deg,#1f5fd0,#17a8e6);display:flex;align-items:flex-end;justify-content:center;gap:2.3px;padding:6px}}
 .mark i{{width:3.4px;border-radius:2px;background:#fff;display:block}} .mark i:nth-child(1){{height:6px;opacity:.6}} .mark i:nth-child(2){{height:11px;opacity:.82}} .mark i:nth-child(3){{height:16px}}
 .logo b{{font-size:19px;font-weight:800;letter-spacing:-.01em}} .logo b span{{color:var(--muted);font-weight:600}}
 .qpill{{margin-left:auto;border:1px solid var(--accent);color:var(--accent);font-weight:800;font-size:11px;letter-spacing:1.5px;text-transform:uppercase;border-radius:999px;padding:6px 15px}}
 .subline{{font-size:10.5px;font-weight:800;letter-spacing:1.3px;text-transform:uppercase;color:var(--muted);margin-top:10px}} .subline b{{color:var(--accent)}}
 .meta{{margin-top:12px;font-size:12.5px;color:var(--muted);text-align:right;line-height:1.9}} .meta b{{color:var(--ink)}}
 .rule{{height:1px;background:var(--line);margin:26px 0}}
 /* prepared for + project */
 .two{{display:grid;grid-template-columns:1fr 1fr;gap:30px}} @media(max-width:640px){{.two{{grid-template-columns:1fr;gap:18px}}}}
 .two .co{{font-family:var(--serif);font-size:24px;font-weight:700;letter-spacing:-.01em;margin:6px 0 4px}}
 .two .sub{{font-size:13.5px;color:var(--ink2);line-height:1.6}}
 .two p{{font-size:14px;color:var(--ink2);line-height:1.62;margin:6px 0 0}}
 /* hero headline */
 .hl{{font-family:var(--serif);font-weight:700;font-size:clamp(34px,6vw,54px);line-height:1.02;letter-spacing:-.015em;margin:34px 0 14px;max-width:14ch}}
 .hl em{{font-style:normal;color:var(--accent)}}
 .intro{{font-size:15px;color:var(--ink2);line-height:1.7;max-width:62ch}}
 /* section head */
 .sh{{display:flex;align-items:baseline;gap:12px;margin:40px 0 4px}} .sh .n{{font-family:var(--serif);font-weight:700;color:var(--accent);font-size:20px}}
 .sh h2{{font-family:var(--serif);font-weight:700;font-size:23px;letter-spacing:-.01em;margin:0}}
 /* line items */
 .items{{border-top:2px solid var(--ink);margin-top:14px}}
 .li{{display:flex;align-items:flex-start;gap:16px;padding:15px 2px;border-bottom:1px solid var(--line)}}
 .li-n{{font-family:var(--serif);font-weight:700;color:var(--accent);font-size:14px;width:26px;flex:none;padding-top:2px}}
 .li-b{{flex:1;min-width:0}} .li-t{{font-weight:800;font-size:15.5px}} .li-d{{font-size:13px;color:var(--muted);margin-top:2px;line-height:1.5;max-width:60ch}}
 .li-p{{font-weight:800;font-size:15.5px;flex:none;white-space:nowrap}}
 .subrow{{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:11px 2px;border-bottom:1px solid var(--line);font-size:14.5px;color:var(--ink2)}} .subrow b{{font-weight:800;color:var(--ink)}}
 .totalrow{{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:16px 2px;border-bottom:2px solid var(--ink);margin-top:2px}}
 .totalrow .l{{font-family:var(--serif);font-weight:700;font-size:19px}} .totalrow .l span{{display:block;font-family:var(--sans);font-size:12px;color:var(--muted);font-weight:600;margin-top:2px}}
 .totalrow .amt{{font-family:var(--serif);font-weight:700;font-size:clamp(26px,4vw,34px);letter-spacing:-.01em}}
 .note{{font-size:12px;color:var(--faint);margin-top:9px;text-align:right}}
 /* included */
 .inc{{list-style:none;margin:14px 0 0;padding:0;display:grid;grid-template-columns:1fr 1fr;gap:10px}} @media(max-width:560px){{.inc{{grid-template-columns:1fr}}}}
 .inc li{{display:flex;gap:9px;font-size:14px;color:var(--ink2);line-height:1.5}} .inc li::before{{content:"✓";color:var(--good);font-weight:900;flex:none}}
 /* hosting */
 .host{{display:flex;align-items:center;justify-content:space-between;gap:16px;border:1px solid var(--line2);border-radius:8px;padding:18px 20px;margin-top:14px;flex-wrap:wrap;background:#fff}}
 .host .d{{font-size:12.5px;color:var(--muted);margin-top:3px;max-width:52ch;line-height:1.5}}
 .host .price{{font-family:var(--serif);font-weight:700;font-size:20px;white-space:nowrap}} .host .price span{{font-family:var(--sans);font-size:12.5px;color:var(--muted);font-weight:600}}
 /* details */
 .det{{display:grid;grid-template-columns:130px 1fr;gap:10px 18px;font-size:14px;margin-top:14px}} .det .k{{color:var(--accent);font-weight:800;font-size:10.5px;text-transform:uppercase;letter-spacing:.6px;padding-top:3px}} .det .v{{color:var(--ink2);line-height:1.6}}
 .foot{{border-top:1px solid var(--line);margin-top:36px;padding-top:20px;text-align:center;color:var(--muted);font-size:12.5px;line-height:1.8}} .foot b{{color:var(--ink)}}
</style></head><body>
 <div class="sheet"><div class="pad">
   <div class="hd">
     <div class="logo"><span class="mark"><i></i><i></i><i></i></span><b>Digital&nbsp;Expo <span>· DE Group</span></b></div>
     <span class="qpill">Quotation</span>
   </div>
   <div style="display:flex;justify-content:space-between;gap:20px;flex-wrap:wrap;align-items:flex-end">
     <div class="subline"><b>DE Group</b> · Web Design · Digital Marketing · Melbourne</div>
     <div class="meta">Quote No. <b>{qno}</b><br>Date issued <b>{issued_s}</b><br>Valid until <b>{valid_s}</b></div>
   </div>
   <div class="rule"></div>

   <div class="two">
     <div><div class="eyebrow">Prepared for</div><div class="co">{c}</div>
       <div class="sub">{('Attn: '+at+' · ') if at else ''}{d or ''}</div></div>
     <div><div class="eyebrow">The project</div>
       <p>A bespoke marketing website — designed, engineered and launched with original art direction, cinematic motion and a full responsive build, delivered live on managed hosting.</p></div>
   </div>

   <h1 class="hl">A website built to <em>earn its place.</em></h1>
   <p class="intro">Not a template. A bespoke, hand-engineered site with its own identity, deep content on every page, and motion that makes it feel alive — built on modern web technology to load fast and convert the people who land on it.</p>

   <div class="sh"><span class="n">01</span><h2>Investment breakdown</h2></div>
   <div class="items">
     {items_html}
     <div class="subrow"><span>Subtotal — website build (one-off)</span><b>{_money(total)}</b></div>
     <div class="subrow"><span>GST (10%)</span><b>{_money(gst)}</b></div>
     <div class="totalrow"><div class="l">Total — inc GST<span>Complete website, designed, engineered &amp; launched</span></div><div class="amt">{_money(grand)}</div></div>
   </div>
   <div class="note">All prices in AUD, inclusive of 10% GST.</div>

   <div class="sh"><span class="n">02</span><h2>Included — no extra cost</h2></div>
   <ul class="inc">
     <li>Working contact form — enquiries delivered straight to your inbox.</li>
     <li>On-page SEO + Google Analytics — meta, titles &amp; visitor tracking set up.</li>
     <li>Domain connection &amp; SSL — we point {d or 'your domain'} to the site &amp; secure it.</li>
     <li>Two rounds of revisions — refinements after your first review, included.</li>
   </ul>

   <div class="sh"><span class="n">03</span><h2>Web + email hosting &amp; server</h2></div>
   <div class="host"><div><b>Ongoing hosting</b><div class="d">Managed website hosting · business email hosting · managed server · SSL &amp; CDN · daily backups · 99.9% uptime · security patching · up to 30 min of edits / month</div></div>
     <div class="price">{_money(hosting_mo)}<span>/month + GST</span><br><span style="font-size:12px">or {_money(host_yr)}/year — two months free</span></div></div>

   <div class="sh"><span class="n">04</span><h2>The details</h2></div>
   <div class="det">
     <div class="k">Timeline</div><div class="v">Your site is already built. Live on your domain within 24–48 hours of go-ahead.</div>
     <div class="k">Deliverables</div><div class="v">Live website, all source files, and full ownership transferred on final payment.</div>
     <div class="k">Terms</div><div class="v">{_money(grand)} (inc GST) build due on go-live; web + email hosting &amp; server {_money(hosting_mo)}/month + GST, billed monthly or annually from launch.</div>
     <div class="k">Questions</div><div class="v">Call us on (03) 7020 9196 or email hello@digitalexpo.com.au — happy to walk you through it.</div>
   </div>

   <div class="foot"><b>Digital Expo · DE Group</b> — Web Design &amp; Digital Marketing · Melbourne, Australia<br>
     (03) 7020 9196 · hello@digitalexpo.com.au · digitalexpo.com.au · ABN 90 134 920 228</div>
 </div></div>
</body></html>'''

if __name__ == "__main__":
    import sys
    company = sys.argv[1] if len(sys.argv) > 1 else "DeltaCert"
    domain = sys.argv[2] if len(sys.argv) > 2 else "deltacert.com.au"
    total = int(sys.argv[3]) if len(sys.argv) > 3 else 900
    out = sys.argv[4] if len(sys.argv) > 4 else "quote_out.html"
    open(out, "w", encoding="utf-8").write(gen_quote_html(company, domain, total))
    print("wrote", out, "| breakdown:", [(n, p) for n, p, _ in _breakdown(total)], "sum", sum(p for _, p, _ in _breakdown(total)))
