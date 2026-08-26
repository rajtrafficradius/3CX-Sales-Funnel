"""ASSET ENGAGEMENT TRACKING.

Every asset we send a prospect — the reveal website, the growth audit, the old-vs-new comparison, the DE Group
brand intro — plus the channels we send them on (SMS short-links, email) record engagement here, so the
closer can see on the booking page whether the prospect actually opened it, how far they scrolled, how long
they spent, and whether they clicked the SMS link / opened the email.

  • asset opened / scrolled / time-on-page  → a tiny beacon injected into the served HTML posts to /api/track
  • SMS link clicked                          → the /s/{code} short-link redirect records a 'click'
  • email opened                              → a 1x1 pixel in the email posts to /api/track/px

All keyed on the prospect's dest9 + the asset kind, so `report()` can render a per-asset engagement summary
and a recent timeline on the Booked-CRM booking-detail page. Public write path (assets are public) but
low-risk: it only ever INSERTs a capped event row. Fully guarded — never raises into a caller.
"""
import re as _re

BRAND_TOKEN = "de-group-reveal-aUGgB40aZsd9yQ"
_KINDS = ("site", "audit", "comparison", "brand", "quote", "email", "sms")
_EVENTS = ("open", "scroll", "time", "click")


def ensure_tracking(pool) -> None:
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute("CREATE TABLE IF NOT EXISTS asset_events ("
                    "  id bigserial PRIMARY KEY, dest9 text, token text, kind text, event text,"
                    "  value integer DEFAULT 0, ua text, ip text, created_at timestamptz DEFAULT now())")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_asset_events_dest9 ON asset_events(dest9, created_at DESC)")
        conn.commit()


def kind_for_token(pool, token: str) -> tuple[str | None, str]:
    """Resolve a public asset token to (dest9, kind). Per-prospect tokens (site/audit/comparison/quote) map to
    the real prospect dest9; the shared brand doc maps to (None,'brand')."""
    from . import lisa as _l
    if not token:
        return (None, "")
    if token == BRAND_TOKEN:
        return (None, "brand")
    try:
        r = _l._fetch(pool,
            "SELECT dest9, CASE WHEN audit_token=%s THEN 'audit' WHEN comparison_token=%s THEN 'comparison' "
            "  WHEN quote_token=%s THEN 'quote' END AS kind FROM booked_crm "
            "WHERE audit_token=%s OR comparison_token=%s OR quote_token=%s LIMIT 1",
            (token, token, token, token, token, token))
        if r:
            return (r[0]["dest9"], r[0]["kind"] or "asset")
        r = _l._fetch(pool, "SELECT dest9, COALESCE(kind,'reveal') k FROM lisa4_sites WHERE share_token=%s LIMIT 1", (token,))
        if r:
            k = r[0]["k"]
            return (r[0]["dest9"], "site" if k == "reveal" else k)
    except Exception:
        pass
    return (None, "")


def record(pool, *, dest9=None, token=None, kind="", event="", value=0, ua="", ip="") -> bool:
    """Insert one engagement event. Resolves dest9 from token when not given. Guarded — never raises."""
    try:
        ensure_tracking(pool)
        d9 = _re.sub(r"[^0-9]", "", dest9 or "")[-9:] if dest9 else None
        k = kind
        if (not d9 or not k) and token:
            rd9, rk = kind_for_token(pool, token)
            d9 = d9 or rd9
            k = k or rk
        if not d9 or event not in _EVENTS:
            return False
        try:
            v = max(0, min(100000, int(value or 0)))
        except Exception:
            v = 0
        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute("INSERT INTO asset_events (dest9, token, kind, event, value, ua, ip) "
                        "VALUES (%s,%s,%s,%s,%s,%s,%s)", (d9, token, (k or "asset")[:20], event, v, (ua or "")[:200], (ip or "")[:60]))
            conn.commit()
        return True
    except Exception:
        return False


# 1x1 transparent GIF (email open pixel)
PIXEL_GIF = (b"GIF89a\x01\x00\x01\x00\x80\x00\x00\xff\xff\xff\x00\x00\x00!\xf9\x04\x01\x00\x00\x00\x00"
             b",\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02D\x01\x00;")


def beacon_js(token: str, kind: str, base: str) -> str:
    """A tiny self-contained script injected into a served asset: posts 'open' on load, tracks max scroll
    depth, and posts time-on-page + final scroll on hide/unload (sendBeacon so it survives the tab closing)."""
    t = (token or "").replace("\\", "").replace('"', "")
    k = (kind or "asset").replace('"', "")
    b = base.rstrip("/")
    return (
        "<script>(function(){try{var T=\"" + t + "\",K=\"" + k + "\",B=\"" + b + "\",mx=0,t0=Date.now(),done=false;"
        "function P(ev,val){try{var d=JSON.stringify({token:T,kind:K,event:ev,value:val||0});"
        "if(navigator.sendBeacon){navigator.sendBeacon(B+\"/api/track\",new Blob([d],{type:\"text/plain\"}));}"
        "else{fetch(B+\"/api/track\",{method:\"POST\",headers:{\"content-type\":\"text/plain\"},body:d,keepalive:true});}}catch(e){}}"
        "P(\"open\",0);function D(){var h=document.documentElement,b=document.body||{},st=h.scrollTop||b.scrollTop||0,"
        "sh=(h.scrollHeight||0)-(h.clientHeight||0),p=sh>0?Math.round(st/sh*100):100;if(p>mx)mx=p;}"
        "window.addEventListener(\"scroll\",D,{passive:true});D();"
        "function F(){if(done)return;done=true;P(\"time\",Math.round((Date.now()-t0)/1000));P(\"scroll\",mx);}"
        "document.addEventListener(\"visibilitychange\",function(){if(document.hidden)F();});"
        "window.addEventListener(\"pagehide\",F);window.addEventListener(\"beforeunload\",F);}catch(e){}})();</script>")


def inject_beacon(html: str, token: str, kind: str, base: str) -> str:
    """Insert the beacon just before </body> (or append). Only for client-facing prospect assets."""
    try:
        snip = beacon_js(token, kind, base)
        low = (html or "").lower()
        i = low.rfind("</body>")
        if i >= 0:
            return html[:i] + snip + html[i:]
        return (html or "") + snip
    except Exception:
        return html


def email_pixel(dest9: str, base: str) -> str:
    d9 = _re.sub(r"[^0-9]", "", dest9 or "")[-9:]
    if not d9:
        return ""
    return (f'<img src="{base.rstrip("/")}/api/track/px?d9={d9}&k=email" width="1" height="1" '
            'alt="" style="display:none" />')


# ---- report for the booking page ----
_LABELS = {"site": "Website", "audit": "Growth audit", "comparison": "Comparison",
           "brand": "Brand intro", "quote": "Quote", "email": "Email", "sms": "SMS link"}


def report(pool, dest9: str) -> dict:
    """Per-asset engagement summary + recent timeline for one booking. Guarded."""
    from . import lisa as _l
    d9 = _re.sub(r"[^0-9]", "", dest9 or "")[-9:]
    if not d9:
        return {"assets": [], "timeline": []}
    try:
        ensure_tracking(pool)
        rows = _l._fetch(pool,
            "SELECT kind, "
            "  count(*) FILTER (WHERE event='open') opens, "
            "  count(*) FILTER (WHERE event='click') clicks, "
            "  COALESCE(max(value) FILTER (WHERE event='scroll'),0) max_scroll, "
            "  COALESCE(sum(value) FILTER (WHERE event='time'),0) total_secs, "
            "  max(created_at) last_at "
            "FROM asset_events WHERE dest9=%s GROUP BY kind", (d9,)) or []
        assets = []
        for r in rows:
            opened = (r["opens"] or 0) > 0 or (r["clicks"] or 0) > 0
            assets.append({
                "kind": r["kind"], "label": _LABELS.get(r["kind"], r["kind"].title()),
                "opened": opened, "opens": r["opens"] or 0, "clicks": r["clicks"] or 0,
                "max_scroll": int(r["max_scroll"] or 0), "total_secs": int(r["total_secs"] or 0),
                "last_at": r["last_at"].isoformat() if r.get("last_at") else None,
            })
        order = {"site": 0, "audit": 1, "comparison": 2, "brand": 3, "quote": 4, "email": 5, "sms": 6}
        assets.sort(key=lambda a: order.get(a["kind"], 9))
        tl = _l._fetch(pool,
            "SELECT kind, event, value, to_char(created_at,'Mon DD HH24:MI') at, created_at "
            "FROM asset_events WHERE dest9=%s ORDER BY created_at DESC LIMIT 40", (d9,)) or []
        timeline = [{"kind": t["kind"], "label": _LABELS.get(t["kind"], t["kind"].title()),
                     "event": t["event"], "value": t["value"], "at": t["at"]} for t in tl]
        engaged = any(a["opened"] for a in assets)
        return {"assets": assets, "timeline": timeline, "engaged": engaged}
    except Exception:
        return {"assets": [], "timeline": [], "engaged": False}
