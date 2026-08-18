"""Chrome/web-push notifications for the booked-CRM (high-priority alert to Alfred's laptop on new bookings).

Self-contained Web Push: VAPID keypair is generated ONCE and persisted in the DB (crm_config), so no
env-var change is needed (env changes trigger GitHub redeploys on this service). Subscriptions live in
`push_subscriptions` (created by crm.ensure_crm_tables). Sending uses pywebpush.

Requires `pywebpush` (+ its `cryptography` dep) — see requirements. Everything degrades gracefully to a
no-op if the lib is missing, so the app never breaks.
"""
from __future__ import annotations
import base64, json
import structlog

log = structlog.get_logger()


def _ensure_cfg(pool) -> None:
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute("CREATE TABLE IF NOT EXISTS crm_config (k text PRIMARY KEY, v text)")
        conn.commit()


def ensure_vapid(pool) -> tuple[str, str] | tuple[None, None]:
    """Return (public_b64url, private_b64url_raw), generating + persisting once. None,None if crypto absent."""
    _ensure_cfg(pool)
    with pool.connection() as conn, conn.cursor() as cur:
        rows = {r["k"]: r["v"] for r in cur.execute("SELECT k, v FROM crm_config WHERE k IN ('vapid_public','vapid_private')").fetchall()}
        if rows.get("vapid_public") and rows.get("vapid_private"):
            return rows["vapid_public"], rows["vapid_private"]
    try:
        from cryptography.hazmat.primitives.asymmetric import ec
    except Exception:
        log.warning("push_vapid_no_crypto")
        return None, None
    priv = ec.generate_private_key(ec.SECP256R1())
    pub_pt = priv.public_key().public_bytes(
        __import__("cryptography").hazmat.primitives.serialization.Encoding.X962,
        __import__("cryptography").hazmat.primitives.serialization.PublicFormat.UncompressedPoint)
    pub_b64 = base64.urlsafe_b64encode(pub_pt).rstrip(b"=").decode()
    priv_raw = priv.private_numbers().private_value.to_bytes(32, "big")
    priv_b64 = base64.urlsafe_b64encode(priv_raw).rstrip(b"=").decode()
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute("INSERT INTO crm_config (k,v) VALUES ('vapid_public',%s),('vapid_private',%s) "
                    "ON CONFLICT (k) DO UPDATE SET v=EXCLUDED.v", (pub_b64, priv_b64))
        conn.commit()
    log.info("push_vapid_generated")
    return pub_b64, priv_b64


def public_key(pool) -> str | None:
    pub, _ = ensure_vapid(pool)
    return pub


def save_subscription(pool, user_email: str, sub: dict, ua: str = "") -> None:
    ep = (sub or {}).get("endpoint")
    keys = (sub or {}).get("keys") or {}
    if not ep or not keys.get("p256dh") or not keys.get("auth"):
        return
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO push_subscriptions (user_email, endpoint, p256dh, auth, ua) VALUES (%s,%s,%s,%s,%s) "
            "ON CONFLICT (endpoint) DO UPDATE SET user_email=EXCLUDED.user_email, p256dh=EXCLUDED.p256dh, "
            "auth=EXCLUDED.auth, ua=EXCLUDED.ua",
            (user_email[:120], ep, keys["p256dh"], keys["auth"], (ua or "")[:200]))
        conn.commit()


def send_to_all(pool, title: str, body: str, url: str = "/lisa-crm", tag: str = "booking") -> dict:
    """Fire a push to every stored subscription. Prunes dead endpoints. No-op if pywebpush/VAPID absent."""
    pub, priv = ensure_vapid(pool)
    if not priv:
        return {"sent": 0, "skipped": "no_vapid"}
    try:
        from pywebpush import webpush, WebPushException
    except Exception:
        return {"sent": 0, "skipped": "no_pywebpush"}
    payload = json.dumps({"title": title, "body": body, "url": url, "tag": tag})
    claims = {"sub": "mailto:hello@trafficradius.com.au"}
    sent = 0; dead: list[str] = []
    with pool.connection() as conn, conn.cursor() as cur:
        subs = cur.execute("SELECT endpoint, p256dh, auth FROM push_subscriptions").fetchall()
    for s in subs:
        info = {"endpoint": s["endpoint"], "keys": {"p256dh": s["p256dh"], "auth": s["auth"]}}
        try:
            webpush(subscription_info=info, data=payload, vapid_private_key=priv, vapid_claims=dict(claims),
                    ttl=3600, headers={"Urgency": "high"})
            sent += 1
        except WebPushException as e:
            code = getattr(getattr(e, "response", None), "status_code", 0)
            if code in (404, 410):
                dead.append(s["endpoint"])
        except Exception:
            pass
    if dead:
        with pool.connection() as conn, conn.cursor() as cur:
            cur.executemany("DELETE FROM push_subscriptions WHERE endpoint=%s", [(e,) for e in dead])
            conn.commit()
    log.info("push_sent", sent=sent, pruned=len(dead))
    return {"sent": sent, "pruned": len(dead)}


def push_new_bookings(pool, settings=None) -> None:
    """Fire a Chrome push for each NEW booking since the last watermark. Runs in the dial loop.
    Watermark in crm_config; on first run it's set to now() so we never blast the historical backlog.
    Fully guarded — never raises into the loop."""
    try:
        _ensure_cfg(pool)
        with pool.connection() as conn, conn.cursor() as cur:
            wm = cur.execute("SELECT v FROM crm_config WHERE k='last_push_booking_at'").fetchone()
            if not wm:
                cur.execute("INSERT INTO crm_config (k,v) VALUES ('last_push_booking_at', now()::text) "
                            "ON CONFLICT (k) DO UPDATE SET v=EXCLUDED.v")
                conn.commit()
                return
            rows = cur.execute(
                "SELECT DISTINCT ON (dest9) dest9, company_name, prospect_name, agreed_day_time, created_at "
                "FROM lisa_calls WHERE COALESCE(meeting_agreed,false) AND dest9 IS NOT NULL "
                "AND created_at > %s::timestamptz ORDER BY dest9, created_at DESC LIMIT 25", (wm["v"],)).fetchall()
        if not rows:
            return
        for r in rows:
            company = r.get("company_name") or "New prospect"
            who = (r.get("prospect_name") or "").strip()
            when = (r.get("agreed_day_time") or "").strip()
            body = " · ".join(x for x in [who, (f"reveal {when}" if when else "")] if x) or "New Lisa booking — tap to open"
            send_to_all(pool, f"🎯 New booking: {company}", body, f"/lisa-crm/{r['dest9']}")
        newest = max(r["created_at"] for r in rows)
        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute("INSERT INTO crm_config (k,v) VALUES ('last_push_booking_at', %s) "
                        "ON CONFLICT (k) DO UPDATE SET v=EXCLUDED.v", (newest.isoformat(),))
            conn.commit()
    except Exception as exc:
        log.warning("push_new_bookings_failed", error=str(exc)[:160])
