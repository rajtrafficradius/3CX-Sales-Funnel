"""NO-SHOW RECOVERY autopilot.

Re-engage prospects who AGREED a meeting on the Lisa call but never showed for the reveal. We send them the
finished website we built + the DE Group brand intro (NOT the quotation), and invite a new time. The existing
human-like SMS responder handles their reply and steers to re-booking.

Design guarantees (this touches REAL prospects, so it is deliberately conservative):
  • mobile-only (AU 04… → 9-digit starting '4'); never a landline.
  • ONCE per prospect (recovery_sent_at guard); never re-sends.
  • skips anyone already revealed/won/lost, already re-engaged (stage 'confirmed'), or opted out.
  • only fires when the meeting has plausibly passed (booking call older than a configurable age).
  • gated behind crm_config 'noshow_recovery_enabled' (default OFF) so nothing goes out until it's turned on.
  • fully guarded — never raises into the loop; never touches the dialer.
"""
import re as _re


def ensure_recovery_columns(pool) -> None:
    from . import crm as _crm
    _crm.ensure_crm_tables(pool)
    with pool.connection() as conn, conn.cursor() as cur:
        for col, typ in (("recovery_sent_at", "timestamptz"), ("recovery_channel", "text"),
                         ("recovery_reply_at", "timestamptz"), ("recovery_rebooked_at", "timestamptz"),
                         ("recovery_status", "text")):
            cur.execute(f"ALTER TABLE booked_crm ADD COLUMN IF NOT EXISTS {col} {typ}")
        conn.commit()


def _cfg(pool, key, default):
    try:
        from . import lisa as _l
        r = _l._fetch(pool, "SELECT v FROM crm_config WHERE k=%s", (key,))
        if r and (r[0].get("v") or "").strip():
            return r[0]["v"].strip()
    except Exception:
        pass
    return default


def enabled(pool) -> bool:
    return str(_cfg(pool, "noshow_recovery_enabled", "0")).lower() in ("1", "true", "on", "yes")


BRAND_INTRO_TOKEN = "de-group-reveal-aUGgB40aZsd9yQ"
_BASE = "https://3cx-sales-funnel-production.up.railway.app"


def _targets(pool, limit: int = 50):
    """The no-show recovery subset: agreed a meeting on a Lisa call, has a finished + shareable reveal site,
    is a mobile, hasn't shown/closed/re-engaged, hasn't opted out, hasn't already been recovered, and the
    meeting has plausibly passed (booking call older than recovery_min_age_hours). One row per prospect."""
    from . import lisa as _l
    ensure_recovery_columns(pool)
    min_age = int(float(_cfg(pool, "noshow_recovery_min_age_hours", "48") or 48))
    sql = (
        "SELECT lc.dest9, max(lc.company_name) company, max(lc.prospect_name) prospect_name, "
        "  min(lc.started_at) first_call, "
        "  (SELECT s.share_token FROM lisa4_sites s WHERE s.dest9=lc.dest9 AND s.status='built' "
        "     AND COALESCE(s.kind,'reveal')='reveal' AND s.share_token IS NOT NULL "
        "     ORDER BY s.built_at DESC LIMIT 1) reveal_token, "
        "  (SELECT b.recovery_sent_at FROM booked_crm b WHERE b.dest9=lc.dest9) recovery_sent_at "
        "FROM lisa_calls lc "
        "WHERE COALESCE(lc.meeting_agreed,false) AND left(lc.dest9,1)='4' "
        "  AND NOT EXISTS (SELECT 1 FROM booked_crm b WHERE b.dest9=lc.dest9 "
        "        AND (lower(COALESCE(b.stage,'')) IN ('revealed','won','lost','confirmed') "
        "             OR b.recovery_sent_at IS NOT NULL)) "
        "  AND NOT EXISTS (SELECT 1 FROM lisa_sms s WHERE s.dest9=lc.dest9 AND s.direction='inbound' "
        "        AND lower(s.body) ~ 'stop|unsubscribe|remove|opt out|wrong number') "
        "GROUP BY lc.dest9 "
        "HAVING min(lc.started_at) < now() - make_interval(hours => %s) "
        "   AND (SELECT s.share_token FROM lisa4_sites s WHERE s.dest9=lc.dest9 AND s.status='built' "
        "        AND COALESCE(s.kind,'reveal')='reveal' AND s.share_token IS NOT NULL LIMIT 1) IS NOT NULL "
        "ORDER BY min(lc.started_at) DESC LIMIT %s"
    )
    return _l._fetch(pool, sql, (min_age, limit)) or []


def _clean_co(company: str) -> str:
    """Human-friendly business name for an SMS: drop legal suffixes, de-shout ALL-CAPS."""
    c = (company or "").strip()
    if not c:
        return "your business"
    c = _re.sub(r"\s*(pty\.?\s*ltd\.?|p/?l|ltd\.?|proprietary|the trustee for|t/?as .*)$", "", c, flags=_re.I).strip(" .,-")
    if c and (c.isupper() or c.islower()):
        c = c.title()
    return c or "your business"


def _message(first_name: str, company: str, reveal_url: str, brand_url: str) -> str:
    nm = (" " + first_name.lower()) if first_name else ""
    co = _clean_co(company)
    return (f"hi{nm}, it's lisa from de group — sorry we missed you for the website reveal. we actually went "
            f"ahead and built the new site for {co} anyway, so you can see it for yourself: {reveal_url} — and "
            f"here's a quick intro to who we are: {brand_url}. would love to find a new time to walk you through "
            f"it, whenever suits you?")


def preview(pool, limit: int = 50) -> dict:
    """Dry-run: who WOULD be messaged + the exact text — for review before enabling. Sends nothing."""
    from . import lisa as _l
    out = []
    for r in _targets(pool, limit):
        d9 = r["dest9"]
        nm = _l._first_name((r.get("prospect_name") or "") or "")
        reveal = f"{_BASE}/api/lisa4/site/public/{r['reveal_token']}"
        brand = f"{_BASE}/api/lisa4/site/public/{BRAND_INTRO_TOKEN}"
        out.append({"dest9": d9, "company": r.get("company"), "mobile": "+61" + d9,
                    "reveal_token": r.get("reveal_token"),
                    "message": _message(nm, r.get("company"), reveal, brand)})
    return {"enabled": enabled(pool), "count": len(out), "targets": out}


def send_recovery(pool, settings, dest9: str, by: str = "Lisa") -> bool:
    """Send ONE recovery SMS (reveal + brand intro). Mobile-only, once, opt-out-aware. Returns True if sent."""
    from . import lisa as _l
    d9 = _re.sub(r"[^0-9]", "", dest9 or "")[-9:]
    if not d9 or not d9.startswith("4"):
        return False
    try:
        ensure_recovery_columns(pool)
        chk = _l._fetch(pool, "SELECT (SELECT recovery_sent_at FROM booked_crm WHERE dest9=%s) sent, "
                        "EXISTS(SELECT 1 FROM lisa_sms WHERE dest9=%s AND direction='inbound' "
                        "  AND lower(body) ~ 'stop|unsubscribe|remove|opt out|wrong number') optout", (d9, d9))
        if chk and (chk[0].get("sent") or chk[0].get("optout")):
            return False
        rt = _l._fetch(pool, "SELECT s.share_token, s.company FROM lisa4_sites s WHERE s.dest9=%s AND s.status='built' "
                       "AND COALESCE(s.kind,'reveal')='reveal' AND s.share_token IS NOT NULL ORDER BY s.built_at DESC LIMIT 1", (d9,))
        if not rt:
            return False
        token = rt[0]["share_token"]; company = rt[0].get("company")
        info = _l._fetch(pool, "SELECT prospect_name FROM lisa_calls WHERE dest9=%s AND COALESCE(meeting_agreed,false) "
                         "ORDER BY started_at DESC LIMIT 1", (d9,))
        nm = _l._first_name((info[0].get("prospect_name") if info else "") or "")
        reveal = f"{_BASE}/api/lisa4/site/public/{token}"
        brand = f"{_BASE}/api/lisa4/site/public/{BRAND_INTRO_TOKEN}"
        body = _message(nm, company, reveal, brand)
        frm = _l._e164_au((list(getattr(settings, "lisa4_numbers", []) or []) or [""])[0])
        if not (_l._twilio_ready(settings) and frm and _l._send_sms_twilio(settings, "+61" + d9, body, frm)):
            return False
        _l._log_sms(pool, "outbound", frm, "+61" + d9, body, d9)
        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute("INSERT INTO booked_crm (dest9,recovery_sent_at,recovery_channel,recovery_status,updated_at) "
                        "VALUES (%s,now(),'sms','sent',now()) ON CONFLICT (dest9) DO UPDATE SET "
                        "recovery_sent_at=now(), recovery_channel='sms', recovery_status='sent', updated_at=now()", (d9,))
            cur.execute("INSERT INTO crm_activity (dest9,kind,body,author) VALUES (%s,'system',%s,%s)",
                        (d9, "No-show recovery: sent website + brand intro via SMS", by))
            conn.commit()
        return True
    except Exception:
        return False


def run_noshow_recovery(pool, settings, limit: int = 3) -> dict:
    """Autopilot pass: if enabled, send the recovery to a few no-show targets. Guarded; never raises."""
    try:
        if not enabled(pool):
            return {"skipped": "disabled"}
        sent = 0
        for r in _targets(pool, limit):
            if send_recovery(pool, settings, r["dest9"]):
                sent += 1
        return {"sent": sent}
    except Exception as exc:
        try:
            from .lisa import log as _log
            _log.warning("noshow_recovery_failed", error=str(exc)[:140])
        except Exception:
            pass
        return {"error": "guarded"}


def mark_reply(pool, dest9: str) -> None:
    """Record that a recovered prospect replied (called by the SMS responder)."""
    d9 = _re.sub(r"[^0-9]", "", dest9 or "")[-9:]
    if not d9:
        return
    try:
        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute("UPDATE booked_crm SET recovery_reply_at=COALESCE(recovery_reply_at,now()), "
                        "recovery_status=CASE WHEN recovery_status='sent' THEN 'replied' ELSE recovery_status END "
                        "WHERE dest9=%s AND recovery_sent_at IS NOT NULL", (d9,))
            conn.commit()
    except Exception:
        pass


def report(pool) -> dict:
    """Campaign report for the Outbound Intelligence page."""
    from . import lisa as _l
    ensure_recovery_columns(pool)
    try:
        pending = len(_targets(pool, 500))
    except Exception:
        pending = None
    rows = _l._fetch(pool,
        "SELECT count(*) FILTER (WHERE recovery_sent_at IS NOT NULL) sent, "
        "       count(*) FILTER (WHERE recovery_reply_at IS NOT NULL) replied, "
        "       count(*) FILTER (WHERE recovery_rebooked_at IS NOT NULL) rebooked "
        "FROM booked_crm") or [{}]
    r = rows[0]
    recent = _l._fetch(pool,
        "SELECT b.dest9, b.company, b.recovery_status, "
        "  to_char(b.recovery_sent_at,'Mon DD HH24:MI') sent_at, "
        "  (b.recovery_reply_at IS NOT NULL) replied, (b.recovery_rebooked_at IS NOT NULL) rebooked "
        "FROM booked_crm b WHERE b.recovery_sent_at IS NOT NULL "
        "ORDER BY b.recovery_sent_at DESC LIMIT 50") or []
    return {"enabled": enabled(pool), "pending": pending,
            "sent": r.get("sent") or 0, "replied": r.get("replied") or 0,
            "rebooked": r.get("rebooked") or 0, "recent": recent}
