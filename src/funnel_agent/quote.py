"""Reusable DE Group website-quote generator.
Total price drives the breakdown (fixed % split, rounded to $5, last item balances to the exact total).
Produces a self-contained, DE-Group-branded quote page (approved Foremore structure, rebranded — NO Traffic Radius).
Usage: gen_quote_html(company, domain, total) -> html string.
"""
import html as _h

# breakdown weights (sum to 1.0) — the approved Foremore split
ITEMS = [
    ("UX/UI Design & Art Direction", 0.22, "Bespoke identity, layout system, type scale & colour — unique to your brand, not a theme."),
    ("Front-End Engineering", 0.30, "A modern, component-based build — fully responsive, fast, with smooth routing across every page."),
    ("Copywriting & Content Structuring", 0.17, "Professional copy and section architecture across all pages — rich and persuasive, never thin."),
    ("Motion & Interaction Design", 0.14, "Scroll reveals, kinetic type, parallax, page transitions & ambient graphics that make it feel alive."),
    ("Brand Asset Integration", 0.07, "Your logo, embedded premium fonts & optimised imagery — fully self-contained and on-brand."),
    ("Testing, QA & Launch", 0.10, "Cross-device QA, accessibility & performance pass, domain connection and go-live."),
]

def _breakdown(total: int):
    """Return list of (name, price, desc) with prices summing EXACTLY to total; rounded to nearest $5."""
    raw = [(n, round((w * total) / 5) * 5, d) for (n, w, d) in ITEMS]
    diff = total - sum(p for _, p, _ in raw)
    # balance the difference onto the largest line so the column sums to the exact total
    if diff:
        i = max(range(len(raw)), key=lambda k: raw[k][1])
        raw[i] = (raw[i][0], raw[i][1] + diff, raw[i][2])
    return raw

def money(n): return f"A${n:,.0f}"

def gen_quote_html(company: str, domain: str, total: int = 1000, hosting_mo: int = 49) -> str:
    c = _h.escape(company); d = _h.escape(domain or "")
    rows = _breakdown(int(total))
    host_yr = hosting_mo * 10  # two months free
    items_html = "".join(
        f'''<div class="li"><div class="li-n">{i+1:02d}</div><div class="li-b"><div class="li-t">{_h.escape(n)}</div>
        <div class="li-d">{_h.escape(desc)}</div></div><div class="li-p">{money(p)}</div></div>'''
        for i,(n,p,desc) in enumerate(rows))
    return f'''<!doctype html><html lang="en"><head><meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>Website Quotation — {c}</title>
<style>
 :root{{--ink:#0b1524;--ink2:#28364c;--muted:#5b6d85;--faint:#8595a9;--bg:#ffffff;--bg2:#f5f8fc;--line:#e2e9f3;--line2:#d0dbea;
  --navy:#0a1930;--blue:#1f5fd0;--cyan:#17a8e6;--blue-l:#e9f1fd;--green:#10a35d;--gold:#f5b638;
  --grad:linear-gradient(115deg,#1f5fd0,#17a8e6);--sh:0 2px 8px rgba(11,21,36,.06),0 22px 48px -30px rgba(11,21,36,.32)}}
 *{{box-sizing:border-box}} body{{margin:0;background:var(--bg2);color:var(--ink);font:16px/1.6 ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,Arial;-webkit-font-smoothing:antialiased;font-variant-numeric:tabular-nums}}
 .wrap{{max-width:820px;margin:0 auto;padding:0 20px}} a{{color:var(--blue);text-decoration:none}}
 .doc{{background:var(--bg);margin:26px auto 40px;border:1px solid var(--line);border-radius:18px;box-shadow:var(--sh);overflow:hidden}}
 .top{{background:linear-gradient(140deg,var(--navy),#122645);color:#fff;padding:30px 34px;position:relative;overflow:hidden}}
 .top::after{{content:"";position:absolute;inset:0;background:radial-gradient(50% 130% at 100% 0,rgba(23,168,230,.28),transparent 60%);pointer-events:none}}
 .brandrow{{display:flex;align-items:center;gap:11px;position:relative}}
 .mark{{width:34px;height:34px;border-radius:9px;background:var(--grad);display:flex;align-items:flex-end;justify-content:center;gap:2.4px;padding:7px}}
 .mark i{{width:4px;border-radius:2px;background:#fff;display:block}} .mark i:nth-child(1){{height:7px;opacity:.6}} .mark i:nth-child(2){{height:12px;opacity:.82}} .mark i:nth-child(3){{height:18px}}
 .brandrow b{{font-size:19px;font-weight:900;letter-spacing:-.02em}} .brandrow small{{display:block;font-size:9.5px;font-weight:800;letter-spacing:2px;color:#9fb2cc;text-transform:uppercase;margin-top:-2px}}
 .top .for{{margin-left:auto;text-align:right;font-size:12px;color:#a7bad3;position:relative}} .top .for b{{color:#fff;font-size:14px;display:block}}
 .top h1{{position:relative;font-size:clamp(22px,3.6vw,30px);font-weight:850;letter-spacing:-.02em;margin:20px 0 6px;max-width:20ch}}
 .top p{{position:relative;color:#c3d2e6;font-size:14.5px;margin:0;max-width:60ch;line-height:1.6}}
 .sec{{padding:26px 34px;border-top:1px solid var(--line)}}
 .eyebrow{{font-size:11px;font-weight:800;letter-spacing:1.4px;text-transform:uppercase;color:var(--blue);display:flex;gap:9px;align-items:center}}
 .eyebrow .k{{font-weight:900;color:var(--faint)}}
 .sec h2{{font-size:19px;font-weight:850;letter-spacing:-.01em;margin:6px 0 16px}}
 .li{{display:flex;align-items:flex-start;gap:14px;padding:13px 0;border-bottom:1px solid var(--line)}}
 .li-n{{flex:none;width:28px;height:28px;border-radius:8px;background:var(--blue-l);color:var(--blue);font-weight:900;font-size:12px;display:flex;align-items:center;justify-content:center}}
 .li-b{{flex:1;min-width:0}} .li-t{{font-weight:800;font-size:15px}} .li-d{{font-size:13px;color:var(--muted);margin-top:2px;line-height:1.5}}
 .li-p{{flex:none;font-weight:850;font-size:15px;font-variant-numeric:tabular-nums}}
 .total{{display:flex;align-items:center;justify-content:space-between;gap:14px;background:var(--navy);color:#fff;border-radius:13px;padding:18px 20px;margin-top:18px;position:relative;overflow:hidden}}
 .total::after{{content:"";position:absolute;inset:0;background:radial-gradient(60% 130% at 100% 0,rgba(23,168,230,.26),transparent 60%);pointer-events:none}}
 .total .l{{position:relative}} .total .l b{{font-size:16px}} .total .l span{{display:block;font-size:12px;color:#a7bad3;margin-top:2px}}
 .total .amt{{position:relative;font-size:clamp(26px,4vw,34px);font-weight:900;letter-spacing:-.02em}}
 .gst{{font-size:12px;color:var(--faint);margin-top:9px;text-align:right}}
 .stack{{display:flex;flex-wrap:wrap;gap:6px;margin-top:6px}} .chip{{font-size:11.5px;font-weight:700;color:var(--ink2);background:var(--bg2);border:1px solid var(--line);border-radius:999px;padding:4px 11px}}
 .perf{{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin-top:16px}} @media(max-width:560px){{.perf{{grid-template-columns:1fr 1fr}}}}
 .perf .p{{background:var(--bg2);border:1px solid var(--line);border-radius:11px;padding:12px}} .perf .n{{font-size:19px;font-weight:900}} .perf .t{{font-size:11px;color:var(--muted);margin-top:2px}}
 .inc{{list-style:none;margin:0;padding:0;display:grid;grid-template-columns:1fr 1fr;gap:9px}} @media(max-width:560px){{.inc{{grid-template-columns:1fr}}}}
 .inc li{{display:flex;gap:9px;font-size:13.5px;color:var(--ink2);line-height:1.5}} .inc li::before{{content:"✓";color:var(--green);font-weight:900;flex:none}}
 .host{{display:flex;align-items:center;justify-content:space-between;gap:14px;background:var(--bg2);border:1px solid var(--line);border-radius:13px;padding:16px 18px;margin-top:6px;flex-wrap:wrap}}
 .host .price{{font-weight:900;font-size:18px}} .host .price span{{font-size:13px;color:var(--muted);font-weight:600}}
 .det{{display:grid;grid-template-columns:120px 1fr;gap:8px 16px;font-size:13.5px}} .det .k{{color:var(--faint);font-weight:800;font-size:11px;text-transform:uppercase;letter-spacing:.5px;padding-top:2px}} .det .v{{color:var(--ink2);line-height:1.55}}
 .cta{{padding:24px 34px;background:linear-gradient(140deg,var(--navy),#122645);color:#fff;text-align:center;position:relative;overflow:hidden}}
 .cta::after{{content:"";position:absolute;inset:0;background:radial-gradient(50% 130% at 50% 0,rgba(23,168,230,.3),transparent 60%);pointer-events:none}}
 .cta p{{position:relative;margin:0 0 14px;color:#c3d2e6;font-size:14px}} .cta .btns{{position:relative;display:flex;gap:10px;justify-content:center;flex-wrap:wrap}}
 .btn{{display:inline-flex;align-items:center;gap:7px;padding:12px 20px;border-radius:11px;font-weight:800;font-size:14.5px}} .btn.p{{background:#fff;color:var(--navy)}} .btn.g{{background:rgba(255,255,255,.12);color:#fff;border:1px solid rgba(255,255,255,.3)}}
 .foot{{text-align:center;color:var(--faint);font-size:12.5px;padding:18px 20px 30px;line-height:1.7}} .foot b{{color:var(--ink2)}}
</style></head><body>
<div class="doc">
 <div class="top">
   <div class="brandrow"><span class="mark"><i></i><i></i><i></i></span><span><b>Digital&nbsp;Expo</b><small>DE Group</small></span>
     <span class="for">Prepared for<b>{c}</b>{('· '+d) if d else ''}</span></div>
   <h1>A website built to earn its place — not a template.</h1>
   <p>A bespoke, hand-engineered site with its own identity, deep content on every page, and motion that makes it feel alive — built on modern web technology to load fast and convert the people who land on it.</p>
 </div>

 <div class="sec">
   <div class="eyebrow"><span class="k">01</span> Investment breakdown</div>
   <h2>What your build includes</h2>
   {items_html}
   <div class="total"><div class="l"><b>Total build — one-off</b><span>Complete website, designed, engineered &amp; launched</span></div><div class="amt">{money(total)}</div></div>
   <div class="gst">Price shown in AUD. GST included where applicable.</div>
 </div>

 <div class="sec">
   <div class="eyebrow"><span class="k">02</span> Engineering &amp; technology</div>
   <h2>Modern, fast, edge-deployed</h2>
   <p style="color:var(--muted);font-size:14px;margin:0 0 4px;line-height:1.6">Engineered with a modern, component-based framework and deployed to edge infrastructure — optimised for Core Web Vitals, smooth 60fps animation, with SEO and analytics built in. A fast, maintainable, future-proof codebase, not a page-builder template.</p>
   <div class="stack"><span class="chip">React</span><span class="chip">Component architecture</span><span class="chip">Responsive · mobile-first</span><span class="chip">Edge deploy</span><span class="chip">SSL / HTTPS</span><span class="chip">Global CDN</span><span class="chip">SEO optimised</span><span class="chip">Accessibility</span></div>
   <div class="perf"><div class="p"><div class="n">60fps</div><div class="t">Fluid animation</div></div><div class="p"><div class="n">90+</div><div class="t">Lighthouse performance</div></div><div class="p"><div class="n">A+</div><div class="t">SSL / security grade</div></div><div class="p"><div class="n">100%</div><div class="t">Responsive · mobile to 4K</div></div></div>
 </div>

 <div class="sec">
   <div class="eyebrow"><span class="k">03</span> Included — no extra cost</div>
   <h2>Everything to go live &amp; convert</h2>
   <ul class="inc">
     <li>Working contact form — enquiries delivered straight to your inbox.</li>
     <li>On-page SEO + Google Analytics — meta, titles &amp; visitor tracking set up.</li>
     <li>Domain connection &amp; SSL — we point {d or 'your domain'} to the site &amp; secure it.</li>
     <li>Two rounds of revisions — refinements after your first review, included.</li>
   </ul>
 </div>

 <div class="sec">
   <div class="eyebrow"><span class="k">04</span> Domain, server &amp; hosting</div>
   <h2>Keeping your site live</h2>
   <p style="color:var(--muted);font-size:14px;margin:0 0 6px;line-height:1.6">Your domain is already yours — no domain charge; we connect it (DNS) and secure it with SSL. What's ongoing is the managed edge server that keeps your website online 24/7, backed up and maintained.</p>
   <div class="host"><div><b>Server &amp; hosting · ongoing</b><div style="font-size:12.5px;color:var(--muted);margin-top:2px">Managed edge server · SSL &amp; CDN · daily backups · 99.9% uptime · security patching · up to 30 min of edits / month</div></div><div class="price">{money(hosting_mo)}<span>/month</span> &nbsp;·&nbsp; {money(host_yr)}<span>/year — two months free</span></div></div>
 </div>

 <div class="sec">
   <div class="eyebrow"><span class="k">05</span> The details</div>
   <h2>Timeline &amp; terms</h2>
   <div class="det">
     <div class="k">Timeline</div><div class="v">Your site is already built. Live on your domain within 24–48 hours of go-ahead.</div>
     <div class="k">Deliverables</div><div class="v">Live website, all source files, and full ownership transferred on final payment.</div>
     <div class="k">Terms</div><div class="v">{money(total)} build due on go-live; server &amp; hosting billed monthly or annually from launch.</div>
   </div>
 </div>

 <div class="cta">
   <p>Happy to go ahead, or have a question? We're one call away.</p>
   <div class="btns"><a class="btn p" href="tel:0370209196">📞 (03) 7020 9196</a><a class="btn g" href="mailto:hello@digitalexpo.com.au">hello@digitalexpo.com.au</a></div>
 </div>
</div>
<div class="foot"><b>Digital Expo · DE Group</b> — Web Design &amp; Digital Marketing · Melbourne, Australia<br>(03) 7020 9196 · hello@digitalexpo.com.au · digitalexpo.com.au</div>
</body></html>'''

if __name__ == "__main__":
    import sys
    company=sys.argv[1]; domain=sys.argv[2]; total=int(sys.argv[3]) if len(sys.argv)>3 else 1000
    out=sys.argv[4] if len(sys.argv)>4 else "quote_out.html"
    open(out,"w",encoding="utf-8").write(gen_quote_html(company,domain,total))
    print("wrote",out,"total",total)
    # sanity: breakdown sums to total
    print("breakdown:", [(n,p) for n,p,_ in _breakdown(total)], "sum=", sum(p for _,p,_ in _breakdown(total)))
