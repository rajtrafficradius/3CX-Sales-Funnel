"""OLD-vs-NEW website comparison report.

Screenshots the prospect's CURRENT live site and the NEW site we built (both via headless Chromium in the
image), and renders a clean DE-branded before/after doc, stored like the other autopilot docs (kind
'comparison', linked on booked_crm.comparison_token). Fully guarded: if Chromium is missing, a screenshot
fails, or the old site is unreachable, it degrades gracefully (skips that side / returns None) and NEVER
raises into the caller.
"""
import os
import io
import re as _re
import html as _h
import shutil
import tempfile
import subprocess

_BASE = "https://www.trmatrix.com.au"


def _chromium_bin():
    for c in (os.environ.get("CHROMIUM_BIN"), "/usr/bin/chromium", "/usr/bin/chromium-browser",
              "/usr/bin/google-chrome", shutil.which("chromium"), shutil.which("chromium-browser")):
        if c and os.path.exists(c):
            return c
    return None


def _shoot(url: str, width: int = 1366, height: int = 900, timeout: int = 55) -> bytes | None:
    """Headless-Chromium screenshot of a URL → JPEG (or PNG) bytes, or None on any failure."""
    binp = _chromium_bin()
    if not binp or not url:
        return None
    with tempfile.TemporaryDirectory() as td:
        out = os.path.join(td, "shot.png")
        try:
            subprocess.run(
                [binp, "--headless=new", "--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage",
                 "--hide-scrollbars", "--force-color-profile=srgb", f"--window-size={width},{height}",
                 "--virtual-time-budget=9000", "--run-all-compositor-stages-before-draw",
                 f"--screenshot={out}", url],
                timeout=timeout, capture_output=True)
        except Exception:
            return None
        if not os.path.exists(out) or os.path.getsize(out) < 800:
            return None
        raw = open(out, "rb").read()
        try:   # compress to JPEG if Pillow is available (keeps the doc small); else embed the PNG
            from PIL import Image
            im = Image.open(io.BytesIO(raw)).convert("RGB")
            im.thumbnail((1100, 1100))
            buf = io.BytesIO(); im.save(buf, "JPEG", quality=82, optimize=True)
            return b"JPEG" + buf.getvalue()
        except Exception:
            return b"PNG" + raw


def _data_uri(tagged: bytes | None) -> str | None:
    if not tagged:
        return None
    import base64
    if tagged[:4] == b"JPEG":
        return "data:image/jpeg;base64," + base64.b64encode(tagged[4:]).decode()
    if tagged[:3] == b"PNG":
        return "data:image/png;base64," + base64.b64encode(tagged[3:]).decode()
    return "data:image/png;base64," + base64.b64encode(tagged).decode()


def gen_comparison_html(company: str, domain: str, old_uri: str | None, new_uri: str | None) -> str:
    c = _h.escape(company or "your business"); d = _h.escape(domain or "")
    def panel(kind, label, uri, empty):
        cls = "old" if kind == "old" else "new"
        inner = (f'<img src="{uri}" alt="{label}"/>' if uri
                 else f'<div class="empty">{empty}</div>')
        return (f'<div class="panel {cls}"><div class="phead"><span class="tag {cls}">{label}</span></div>'
                f'<div class="shot">{inner}</div></div>')
    old_empty = ("We couldn't load your current site for this snapshot — often a sign it's slow or hard for "
                 "Google to read." if d else "You don't have a website today.")
    return f'''<!doctype html><html lang="en"><head><meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/><title>Old vs New — {c}</title>
<style>
 :root{{--ink:#0b1524;--ink2:#28364c;--muted:#5b6d85;--faint:#8595a9;--bg:#fff;--bg2:#f4f8fd;--line:#e4ebf4;
   --navy:#0a1930;--blue:#1f5fd0;--green:#0f9d58;--red:#e0533d;--grad:linear-gradient(115deg,#1f5fd0,#17a8e6);
   --sh:0 2px 8px rgba(11,21,36,.06),0 22px 48px -30px rgba(11,21,36,.32);--sh-sm:0 1px 3px rgba(11,21,36,.08)}}
 *{{box-sizing:border-box}} body{{margin:0;background:var(--bg2);color:var(--ink);font:16px/1.65 ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,Arial;-webkit-font-smoothing:antialiased}}
 .wrap{{max-width:1000px;margin:0 auto;padding:0 22px}}
 .top{{background:linear-gradient(150deg,var(--navy),#12294a 60%,#0c1c38);color:#fff;padding:36px 0 40px;position:relative;overflow:hidden}}
 .top::after{{content:"";position:absolute;inset:0;background:radial-gradient(46% 120% at 88% 0,rgba(23,168,230,.28),transparent 60%);pointer-events:none}}
 .brandrow{{display:flex;align-items:center;gap:11px;position:relative}}
 .mark{{width:32px;height:32px;border-radius:8px;background:var(--grad);display:flex;align-items:flex-end;justify-content:center;gap:2.3px;padding:6px}}
 .mark i{{width:3.6px;border-radius:2px;background:#fff;display:block}} .mark i:nth-child(1){{height:6px;opacity:.6}} .mark i:nth-child(2){{height:11px;opacity:.82}} .mark i:nth-child(3){{height:16px}}
 .brandrow b{{font-size:18px;font-weight:800}} .brandrow small{{display:block;font-size:9px;font-weight:800;letter-spacing:2px;color:#9fb2cc;text-transform:uppercase;margin-top:-2px}}
 .badge{{margin-left:auto;font-size:10px;font-weight:800;letter-spacing:1.2px;text-transform:uppercase;color:#bfe0ff;border:1px solid rgba(191,224,255,.5);border-radius:999px;padding:5px 12px}}
 .top h1{{position:relative;font-size:clamp(23px,3.6vw,32px);font-weight:850;letter-spacing:-.02em;margin:18px 0 5px;text-wrap:balance}}
 .top p{{position:relative;color:#c3d2e6;font-size:15px;margin:0;max-width:66ch}}
 .grid{{display:grid;grid-template-columns:1fr 1fr;gap:18px;margin:-24px 0 20px;position:relative;z-index:2}}
 @media(max-width:720px){{.grid{{grid-template-columns:1fr}}}}
 .panel{{background:var(--bg);border:1px solid var(--line);border-radius:16px;box-shadow:var(--sh);overflow:hidden}}
 .phead{{padding:14px 16px 0}} .tag{{font-size:11px;font-weight:800;letter-spacing:.06em;text-transform:uppercase;border-radius:999px;padding:5px 12px}}
 .tag.old{{background:#fdeceb;color:var(--red)}} .tag.new{{background:#e7f6ee;color:var(--green)}}
 .shot{{padding:14px 16px 16px}} .shot img{{width:100%;display:block;border:1px solid var(--line);border-radius:10px}}
 .panel.new{{border-color:rgba(15,157,88,.4);box-shadow:0 2px 8px rgba(11,21,36,.06),0 26px 54px -30px rgba(15,157,88,.5)}}
 .empty{{aspect-ratio:1366/900;display:flex;align-items:center;justify-content:center;text-align:center;padding:26px;color:var(--muted);font-size:14.5px;background:var(--bg2);border:1px dashed var(--line);border-radius:10px}}
 .diff{{background:var(--bg);border:1px solid var(--line);border-radius:16px;box-shadow:var(--sh-sm);padding:24px 26px;margin:0 0 16px}}
 .diff h2{{font-size:19px;font-weight:850;letter-spacing:-.015em;margin:0 0 14px}}
 .rows{{display:grid;grid-template-columns:1fr 1fr;gap:12px 26px}} @media(max-width:640px){{.rows{{grid-template-columns:1fr}}}}
 .row{{display:flex;gap:11px;align-items:flex-start;font-size:14.5px;color:var(--ink2)}}
 .row .ic{{flex:none;width:22px;height:22px;border-radius:7px;background:#e7f6ee;color:var(--green);display:flex;align-items:center;justify-content:center;font-weight:800;font-size:13px;margin-top:1px}}
 .row b{{color:var(--ink);font-weight:750}}
 .cta{{background:linear-gradient(140deg,var(--navy),#12294a);color:#fff;border-radius:16px;padding:28px;margin:6px 0 26px;text-align:center}}
 .cta h3{{color:#fff;font-size:20px;margin:0 0 8px}} .cta p{{color:#c9d8ec;margin:0 auto 16px;max-width:54ch}}
 .cta a{{display:inline-block;background:var(--grad);color:#fff;font-weight:800;text-decoration:none;padding:13px 26px;border-radius:11px;font-size:15px}}
 .foot{{text-align:center;color:var(--faint);font-size:12.5px;padding:2px 20px 34px}}
</style></head><body>
 <div class="top"><div class="wrap">
   <div class="brandrow"><span class="mark"><i></i><i></i><i></i></span><span><b>Digital&nbsp;Expo</b><small>DE Group</small></span><span class="badge">Old vs New</span></div>
   <h1>{c} — your current site vs the new one we built</h1>
   <p>A side-by-side look at where your website is today and the modern, mobile-first, conversion-focused site we've built for you{(' — '+d) if d else ''}.</p>
 </div></div>
 <div class="wrap">
   <div class="grid">
     {panel("old","Your site today", old_uri, old_empty)}
     {panel("new","The new site we built", new_uri, "The new site is ready to view.")}
   </div>
   <div class="diff"><h2>What's better on the new site</h2><div class="rows">
     <div class="row"><span class="ic">✓</span><div><b>Modern, professional design</b> that builds instant trust with a new visitor.</div></div>
     <div class="row"><span class="ic">✓</span><div><b>Built for mobile first</b> — most of your customers are on a phone.</div></div>
     <div class="row"><span class="ic">✓</span><div><b>Fast to load</b>, so visitors don't bounce before they see you.</div></div>
     <div class="row"><span class="ic">✓</span><div><b>Clear calls to action</b> and a working enquiry form that lands straight in your inbox.</div></div>
     <div class="row"><span class="ic">✓</span><div><b>Set up for Google</b> from day one, so you're easier to find.</div></div>
     <div class="row"><span class="ic">✓</span><div><b>Room to grow</b> — dedicated pages for your services and your best work.</div></div>
   </div></div>
   <div class="cta"><h3>Ready to make it yours?</h3><p>We'll walk you through the new site and how it wins you more enquiries — then get it live under your name.</p><a href="tel:0370209196">📞 Book a 15-minute call · (03) 7020 9196</a></div>
 </div>
 <div class="foot"><b>Digital Expo · DE Group</b> — Google Partner digital marketing agency · digitalexpo.com.au</div>
</body></html>'''


def ensure_comparison(pool, settings, dest9: str, force: bool = False):
    """Create-if-missing OLD-vs-NEW comparison for a booked prospect with a built site. Returns token/None.
    Guarded — never raises. Needs Chromium in the image (Dockerfile); returns None otherwise."""
    import secrets, random
    from . import lisa as _l, crm as _crm
    d9 = _re.sub(r"[^0-9]", "", dest9 or "")[-9:]
    if not d9:
        return None
    try:
        _crm.ensure_crm_tables(pool)
        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT comparison_token FROM booked_crm WHERE dest9=%s", (d9,))
            r = cur.fetchone()
            if r and (r.get("comparison_token") or "") and not force:
                return r["comparison_token"]
        site = _l._fetch(pool, "SELECT share_token, company, domain FROM lisa4_sites WHERE dest9=%s AND status='built' "
                         "AND COALESCE(kind,'reveal')='reveal' AND share_token IS NOT NULL ORDER BY built_at DESC LIMIT 1", (d9,))
        if not site:
            return None
        token = site[0]["share_token"]; company = site[0].get("company")
        domain = (site[0].get("domain") or "")
        if not domain:
            dm = _l._fetch(pool, "SELECT domain FROM lisa4_pool WHERE dest9=%s", (d9,))
            domain = (dm[0].get("domain") if dm else "") or ""
        new_url = f"{_BASE}/api/lisa4/site/public/{token}"
        old_url = ("https://" + domain.replace("https://", "").replace("http://", "").strip("/")) if domain else None
        new_img = _data_uri(_shoot(new_url))
        old_img = _data_uri(_shoot(old_url)) if old_url else None
        if not new_img:
            return None   # no point in a comparison without at least the new site
        html = gen_comparison_html(company or "your business", domain, old_img, new_img)
        slug = _re.sub(r"[^a-z0-9]+", "-", (company or "site").lower()).strip("-")[:24] or "site"
        tok = f"{slug}-compare-" + secrets.token_urlsafe(8)
        synth = "9" + "".join(str(random.randint(0, 9)) for _ in range(8))
        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute("INSERT INTO lisa4_sites (dest9,company,domain,html,status,share_token,kind,built_at) "
                        "VALUES (%s,%s,%s,%s,'built',%s,'comparison',now())", (synth, company, domain, html, tok))
            cur.execute("INSERT INTO booked_crm (dest9,comparison_token,updated_by,updated_at) "
                        "VALUES (%s,%s,'autopilot',now()) ON CONFLICT (dest9) DO UPDATE SET "
                        "comparison_token=EXCLUDED.comparison_token, updated_at=now()", (d9, tok))
            conn.commit()
        return tok
    except Exception:
        return None
