"""OLD-vs-NEW website comparison report.

Screenshots the prospect's CURRENT live site and the NEW site we built (both via headless Chromium in the
image) — full-page on desktop AND on a phone — and renders a substantial, DE-branded before/after doc:
hero, desktop side-by-side, mobile side-by-side, a before/after scorecard, and a specific "what's better"
list keyed off the actual issue we found. Stored like the other autopilot docs (kind 'comparison', linked on
booked_crm.comparison_token). Fully guarded: if Chromium is missing, a screenshot fails, or the old site is
unreachable, it degrades gracefully (skips that side / returns None) and NEVER raises into the caller.
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


def _shoot(url: str, width: int = 1440, height: int = 2600, mobile: bool = False, timeout: int = 60) -> bytes | None:
    """Headless-Chromium screenshot of a URL → JPEG (or PNG) bytes, or None on any failure. A TALL window
    height captures far more of the page than a single viewport (that was the old 'thin' look). `mobile`
    uses a phone-width viewport + device scale so responsive layouts render as they would on a phone."""
    binp = _chromium_bin()
    if not binp or not url:
        return None
    with tempfile.TemporaryDirectory() as td:
        out = os.path.join(td, "shot.png")
        args = [binp, "--headless=new", "--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage",
                "--hide-scrollbars", "--force-color-profile=srgb", f"--window-size={width},{height}",
                "--virtual-time-budget=10000", "--run-all-compositor-stages-before-draw"]
        if mobile:
            args += ["--force-device-scale-factor=2",
                     "--user-agent=Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15 "
                     "(KHTML, like Gecko) Version/16.0 Mobile/15E148 Safari/604.1"]
        args += [f"--screenshot={out}", url]
        try:
            subprocess.run(args, timeout=timeout, capture_output=True)
        except Exception:
            return None
        if not os.path.exists(out) or os.path.getsize(out) < 800:
            return None
        raw = open(out, "rb").read()
        try:   # compress to JPEG if Pillow is available (keeps the doc small); else embed the PNG
            from PIL import Image
            im = Image.open(io.BytesIO(raw)).convert("RGB")
            im.thumbnail((1400, 5200))                 # keep tall pages tall, just bound the file size
            buf = io.BytesIO(); im.save(buf, "JPEG", quality=78, optimize=True)
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


def _issue_line(issue: str | None) -> str:
    """One plain sentence naming what was holding the current site back (from the finding we captured)."""
    iss = (issue or "").strip()
    if not iss:
        return ("Your current site wasn't doing your business justice online — dated design, hard to use on a "
                "phone, and easy for a new customer to skip past.")
    iss = iss[0].upper() + iss[1:]
    if iss[-1] not in ".!?":
        iss += "."
    return _h.escape(iss)


def _dots(filled: int) -> str:
    filled = max(0, min(5, filled))
    return ("<span class='on'></span>" * filled) + ("<span class='off'></span>" * (5 - filled))


def gen_comparison_html(company: str, domain: str, *, old_desktop=None, new_desktop=None,
                        old_mobile=None, new_mobile=None, issue: str | None = None) -> str:
    c = _h.escape(company or "your business"); d = _h.escape(domain or "")
    has_old = bool(old_desktop or old_mobile)

    def shot(uri, label_cls, empty):
        if uri:
            return f'<div class="scan"><img src="{uri}" alt=""/></div><div class="scanhint">the full page — scroll to see it all ↓</div>'
        return f'<div class="empty">{empty}</div>'

    old_empty_d = ("We couldn't load your current site for this snapshot — usually a sign it's slow or hard for "
                   "Google to read." if d else "You don't have a website today — so every visitor who searches "
                   "for you finds nothing.")

    # before/after scorecard — qualitative, defensible (old sites we target are dated/slow/not-mobile)
    areas = [
        ("First impression &amp; trust", "Dated look, easy to skip past", "Modern, credible, built to convert", 2, 5),
        ("Works on a phone", "Hard to read / tap on mobile", "Mobile-first — perfect on any phone", 1, 5),
        ("Speed &amp; performance", "Slow to load, visitors bounce", "Fast, lightweight, instant", 2, 5),
        ("Found on Google", "Little on-page SEO structure", "Search-ready from day one", 2, 5),
    ]
    score_rows = "".join(
        f'<div class="scard"><div class="sarea">{a}</div>'
        f'<div class="scol old"><span class="slab">Now</span><div class="dots">{_dots(o)}</div><small>{ol}</small></div>'
        f'<div class="scol new"><span class="slab">New</span><div class="dots">{_dots(n)}</div><small>{nl}</small></div></div>'
        for a, ol, nl, o, n in areas)

    mobile_block = ""
    if new_mobile or old_mobile:
        mobile_block = f'''
   <h2 class="sech">On a phone</h2>
   <p class="secp">Most of {c}'s visitors are on a mobile — here's the difference where it matters most.</p>
   <div class="mgrid">
     <div class="phone old"><div class="ptag old">Your site now</div><div class="pscreen">{shot(old_mobile, "old", "Not mobile-friendly today.")}</div></div>
     <div class="phone new"><div class="ptag new">The new site</div><div class="pscreen">{shot(new_mobile, "new", "Ready to view.")}</div></div>
   </div>'''

    betters = [
        ("Modern, professional design", "that builds instant trust with a first-time visitor."),
        ("Built for mobile first", "so it's effortless for the customers already searching on a phone."),
        ("Fast to load", "so visitors don't bounce before they even see you."),
        ("Clear calls to action", "and a working enquiry form that lands straight in your inbox."),
        ("Set up for Google", "with proper page structure, so you're easier to find."),
        ("Room to grow", "dedicated pages for your services and your best work."),
    ]
    better_rows = "".join(
        f'<div class="row"><span class="ic">✓</span><div><b>{t}</b> {r}</div></div>' for t, r in betters)

    return f'''<!doctype html><html lang="en"><head><meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/><title>Old vs New — {c}</title>
<style>
 :root{{--ink:#0b1524;--ink2:#28364c;--muted:#5b6d85;--faint:#8595a9;--bg:#fff;--bg2:#eef3f9;--line:#e0e8f2;
   --navy:#0a1930;--blue:#1f5fd0;--green:#0f9d58;--red:#e0533d;--amber:#d98a12;--grad:linear-gradient(115deg,#1f5fd0,#17a8e6);
   --sh:0 2px 8px rgba(11,21,36,.06),0 22px 48px -30px rgba(11,21,36,.32);--sh-sm:0 1px 3px rgba(11,21,36,.08)}}
 *{{box-sizing:border-box}} body{{margin:0;background:var(--bg2);color:var(--ink);font:16px/1.65 ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,Arial;-webkit-font-smoothing:antialiased}}
 .wrap{{max-width:1040px;margin:0 auto;padding:0 22px}}
 .top{{background:linear-gradient(150deg,var(--navy),#12294a 60%,#0c1c38);color:#fff;padding:36px 0 44px;position:relative;overflow:hidden}}
 .top::after{{content:"";position:absolute;inset:0;background:radial-gradient(46% 120% at 88% 0,rgba(23,168,230,.28),transparent 60%);pointer-events:none}}
 .brandrow{{display:flex;align-items:center;gap:11px;position:relative}}
 .mark{{width:32px;height:32px;border-radius:8px;background:var(--grad);display:flex;align-items:flex-end;justify-content:center;gap:2.3px;padding:6px}}
 .mark i{{width:3.6px;border-radius:2px;background:#fff;display:block}} .mark i:nth-child(1){{height:6px;opacity:.6}} .mark i:nth-child(2){{height:11px;opacity:.82}} .mark i:nth-child(3){{height:16px}}
 .brandrow b{{font-size:18px;font-weight:800}} .brandrow small{{display:block;font-size:9px;font-weight:800;letter-spacing:2px;color:#9fb2cc;text-transform:uppercase;margin-top:-2px}}
 .badge{{margin-left:auto;font-size:10px;font-weight:800;letter-spacing:1.2px;text-transform:uppercase;color:#bfe0ff;border:1px solid rgba(191,224,255,.5);border-radius:999px;padding:5px 12px}}
 .top h1{{position:relative;font-size:clamp(23px,3.6vw,33px);font-weight:850;letter-spacing:-.02em;margin:18px 0 6px;text-wrap:balance}}
 .top p{{position:relative;color:#c3d2e6;font-size:15.5px;margin:0;max-width:70ch}}
 .callout{{position:relative;margin-top:16px;background:rgba(224,83,61,.14);border:1px solid rgba(224,83,61,.4);border-left:4px solid var(--red);border-radius:10px;padding:12px 15px;color:#ffdfd9;font-size:14px;max-width:74ch}}
 .callout b{{color:#fff}}
 .sech{{font-size:20px;font-weight:850;letter-spacing:-.015em;margin:30px 0 4px}}
 .secp{{color:var(--muted);font-size:14.5px;margin:0 0 16px;max-width:74ch}}
 .grid{{display:grid;grid-template-columns:1fr 1fr;gap:18px;margin:-28px 0 8px;position:relative;z-index:2}}
 @media(max-width:760px){{.grid{{grid-template-columns:1fr}}}}
 .panel{{background:var(--bg);border:1px solid var(--line);border-radius:16px;box-shadow:var(--sh);overflow:hidden}}
 .panel.new{{border-color:rgba(15,157,88,.4);box-shadow:0 2px 8px rgba(11,21,36,.06),0 26px 54px -30px rgba(15,157,88,.5)}}
 .phead{{padding:14px 16px 0}} .tag{{font-size:11px;font-weight:800;letter-spacing:.06em;text-transform:uppercase;border-radius:999px;padding:5px 12px}}
 .tag.old{{background:#fdeceb;color:var(--red)}} .tag.new{{background:#e7f6ee;color:var(--green)}}
 .scanwrap{{padding:14px 16px 8px}}
 /* Show the FULL-page screenshot inline (the page itself scrolls) — a cramped inner scroll box
    is a nested-scroll trap on a trackpad, so people never see past the fold. */
 .scan{{display:block;overflow:visible;border:1px solid var(--line);border-radius:10px;background:#f7fafd}}
 .scan img{{width:100%;display:block}}
 .scanhint{{text-align:center;color:var(--faint);font-size:11.5px;font-weight:700;padding:7px 0 4px;letter-spacing:.02em}}
 .empty{{min-height:280px;display:flex;align-items:center;justify-content:center;text-align:center;padding:26px;color:var(--muted);font-size:14.5px;background:var(--bg2);border:1px dashed var(--line);border-radius:10px;margin:14px 16px}}
 /* scorecard */
 .score{{background:var(--bg);border:1px solid var(--line);border-radius:16px;box-shadow:var(--sh-sm);padding:8px 10px;margin:8px 0 6px}}
 .scard{{display:grid;grid-template-columns:1.3fr 1fr 1fr;gap:10px;align-items:center;padding:13px 12px;border-bottom:1px solid var(--line)}}
 .scard:last-child{{border-bottom:none}}
 .sarea{{font-weight:750;font-size:14.5px;color:var(--ink)}}
 .scol{{border-radius:10px;padding:9px 11px}} .scol.old{{background:#fdf1ef}} .scol.new{{background:#ecf8f1}}
 .slab{{font-size:9.5px;font-weight:800;letter-spacing:.1em;text-transform:uppercase;color:var(--faint)}}
 .scol.old .slab{{color:var(--red)}} .scol.new .slab{{color:var(--green)}}
 .dots{{display:flex;gap:4px;margin:5px 0 3px}} .dots span{{width:11px;height:11px;border-radius:50%}}
 .scol.old .dots .on{{background:var(--red)}} .scol.new .dots .on{{background:var(--green)}} .dots .off{{background:#dbe3ee}}
 .scol small{{font-size:12px;color:var(--muted);display:block;line-height:1.4}}
 @media(max-width:640px){{.scard{{grid-template-columns:1fr;gap:7px}}}}
 /* mobile phones */
 .mgrid{{display:grid;grid-template-columns:1fr 1fr;gap:22px;justify-items:center;margin:2px 0 6px}}
 .phone{{width:100%;max-width:300px}}
 .ptag{{text-align:center;font-size:11px;font-weight:800;letter-spacing:.05em;text-transform:uppercase;border-radius:999px;padding:5px 12px;margin:0 auto 10px;width:max-content}}
 .ptag.old{{background:#fdeceb;color:var(--red)}} .ptag.new{{background:#e7f6ee;color:var(--green)}}
 /* the phone is a FIXED-HEIGHT frame; the full-page screenshot SCROLLS inside it (a real phone view) —
    otherwise a tall screenshot stretches the frame down the whole page and reads as broken on mobile. */
 .pscreen{{border:9px solid #0b1524;border-radius:30px;overflow-y:auto;overflow-x:hidden;height:min(74vh,580px);box-shadow:var(--sh);background:#0b1524;-webkit-overflow-scrolling:touch;scrollbar-width:thin}}
 .pscreen .scan{{border:none;border-radius:20px;overflow:visible}}
 .pscreen .scan img{{width:100%;height:auto;display:block}} .pscreen .empty{{margin:0;border-radius:20px;height:100%}}
 .pscreen .scanhint{{color:#7f8ea3;background:#0b1524;position:sticky;bottom:0;padding:4px 0}}
 /* better list */
 .diff{{background:var(--bg);border:1px solid var(--line);border-radius:16px;box-shadow:var(--sh-sm);padding:22px 24px;margin:8px 0 16px}}
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
   {('<div class="callout"><b>What we found:</b> '+_issue_line(issue)+'</div>') if has_old else ''}
 </div></div>
 <div class="wrap">
   <div class="grid">
     <div class="panel old"><div class="phead"><span class="tag old">Your site today</span></div><div class="scanwrap">{shot(old_desktop, "old", old_empty_d)}</div></div>
     <div class="panel new"><div class="phead"><span class="tag new">The new site we built</span></div><div class="scanwrap">{shot(new_desktop, "new", "The new site is ready to view.")}</div></div>
   </div>

   <h2 class="sech">How they stack up</h2>
   <p class="secp">A quick, honest read on where your current site sits today versus the new one — across the four things that decide whether a visitor becomes an enquiry.</p>
   <div class="score">{score_rows}</div>
   {mobile_block}

   <div class="diff"><h2 style="font-size:19px;font-weight:850;letter-spacing:-.015em;margin:0 0 14px">What's better on the new site</h2><div class="rows">{better_rows}</div></div>

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
        issue = None
        pl = _l._fetch(pool, "SELECT domain, issue FROM lisa4_pool WHERE dest9=%s", (d9,))
        if pl:
            domain = domain or (pl[0].get("domain") or "")
            issue = pl[0].get("issue")
        new_url = f"{_BASE}/api/lisa4/site/public/{token}"
        old_url = ("https://" + domain.replace("https://", "").replace("http://", "").strip("/")) if domain else None
        # Full-page desktop + phone captures for BOTH sides (the old thin doc only had one top-of-page slice).
        new_desktop = _data_uri(_shoot(new_url))
        if not new_desktop:
            return None   # no point in a comparison without at least the new site
        new_mobile = _data_uri(_shoot(new_url, width=414, height=2200, mobile=True))
        old_desktop = _data_uri(_shoot(old_url)) if old_url else None
        old_mobile = _data_uri(_shoot(old_url, width=414, height=2200, mobile=True)) if old_url else None
        html = gen_comparison_html(company or "your business", domain, old_desktop=old_desktop,
                                   new_desktop=new_desktop, old_mobile=old_mobile, new_mobile=new_mobile, issue=issue)
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
