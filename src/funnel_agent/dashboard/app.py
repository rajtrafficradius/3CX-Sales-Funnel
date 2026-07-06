"""FastAPI app for the funnel dashboard."""

from __future__ import annotations

import asyncio
import re
import threading
from contextlib import asynccontextmanager
from datetime import date as _date, datetime as _dt, time as _time, timedelta, timezone as _tz
from zoneinfo import ZoneInfo
from importlib import resources

from fastapi import FastAPI, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.encoders import jsonable_encoder
from psycopg.types.json import Json
from fastapi.responses import (HTMLResponse, JSONResponse, RedirectResponse,
                               Response, StreamingResponse)

from ..auth import (can_manage_pipeline, create_session, create_user,
                    delete_session, delete_user, get_user_by_email, is_admin,
                    list_users, set_password_by_id, update_user,
                    user_for_session, verify_password)
from ..config import Settings, get_settings
from ..db.analytics import make_analytics_pool

# Funnel stages in order, with the daily_funnel column each maps to.
STAGES = [
    ("Calls Made", "calls_made"),
    ("Connected", "connected"),
    ("Right Party Contact", "rpc_connect"),
    ("Full Pitch", "full_pitch"),
    ("Meeting Booked", "meetings_booked"),          # any new booking the BDE made
    ("Qualified Booked Meetings", "qualified_booked"),  # strict subset: also qualified (BAPU)
]


# ---- Performance benchmarks (#4) ----
# Each conversion stage's expected ratio of the PRIOR stage. Targets CASCADE from the
# Calls-Made target down the funnel (calls_target → ×0.40 → ×0.40 → ×0.30 → ×0.25), so a
# stage's target is what it SHOULD be if the calls target were hit — not a ratio of the
# actual (possibly short) prior stage. Shown in the funnel Total column as "N short".
#   {stage_key: (ratio, prior_stage_key)}
BENCHMARK_RATIOS = {
    "connected":       (0.40, "calls_made"),   # 40% of calls made
    "rpc_connect":     (0.40, "connected"),    # 40% of connected
    "full_pitch":      (0.30, "rpc_connect"),  # 30% of RPC
    "meetings_booked": (0.25, "full_pitch"),   # 25% of full pitch
}
# Calls-Made benchmark is a TEAM target: the whole BDE team should make CALLS_PER_30MIN
# dials per 30-min block, sustained over a standard WORKDAY_HALF_HOURS-block (8h) day.
#   => team daily calls target = 75 x 16 = 1200 calls/day.
# A single-BDE view shows that BDE's equal share (team target / # in-scope BDEs).
CALLS_PER_30MIN = 75     # team dials per 30-min block
WORKDAY_START_HOUR = 9   # BDEs start dialling at 9:00 AM Melbourne
CALLING_HOURS = 8        # 8 hours of CALLING time/day => 75/30min x 16 = 1200 calls/day
DAY_SPAN_HOURS = 9       # 9-hour day; the extra ~1h is flexible lunch/break BDEs self-manage
                         # (we don't model a fixed lunch window — just cap calling time at 8h)


def _pct(num: int, den: int) -> float | None:
    return round(100 * num / den, 1) if den else None


def _index_html() -> str:
    return resources.files("funnel_agent.dashboard").joinpath("static/index.html").read_text(
        encoding="utf-8"
    )


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    pool = make_analytics_pool(settings.analytics_db_dsn, session_timezone=settings.tz)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        pool.open()
        yield
        pool.close()

    app = FastAPI(title="3CX Sales-Funnel Dashboard", lifespan=lifespan)

    def q(sql: str, params=None) -> list[dict]:
        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall()

    def _static(name: str) -> str:
        return resources.files("funnel_agent.dashboard").joinpath(
            f"static/{name}").read_text(encoding="utf-8")

    # ---- auth: per-person login + role-based scoping --------------------- #
    _PUBLIC = {"/login", "/logout", "/healthz", "/readyz", "/logo.png"}

    @app.middleware("http")
    async def auth_mw(request: Request, call_next):
        path = request.url.path
        user = None
        # Kiosk: an office TV may pass ?token=<KIOSK_TOKEN> once (then a cookie) for
        # a read-only ALL view without a personal login.
        # A REAL login always wins — check the session FIRST so an admin/BDE who once
        # opened TV mode (and got the kiosk cookie) isn't downgraded to read-only kiosk
        # on the normal dashboard.
        if token := request.cookies.get("fa_session"):
            user = await run_in_threadpool(user_for_session, pool, token)
        # TV mode is PUBLIC: anyone NOT logged in with the link (?tv=1 / /tv / kiosk token
        # / kiosk cookie) gets a READ-ONLY kiosk view. A cookie keeps the TV page's /api
        # calls authed for that display. (Skipped entirely when a real session exists.)
        ktok = settings.kiosk_token
        via_query_token = bool(ktok) and request.query_params.get("token") == ktok
        via_tv = request.query_params.get("tv") == "1" or path == "/tv"
        kiosk_cookie = request.cookies.get("fa_kiosk")
        # The kiosk COOKIE only keeps the TV display's /api/* calls authed. It must NOT
        # turn the normal dashboard HTML into a read-only kiosk view for someone who once
        # opened TV mode — that silently hides the Database/Admin links and forces
        # read-only. HTML page routes require a real login (or an explicit ?tv=1 / kiosk
        # token); only /api requests accept the bare cookie.
        via_cookie = (bool(kiosk_cookie) and (kiosk_cookie == ktok or kiosk_cookie == "tv")
                      and path.startswith("/api"))
        if user is None and (via_query_token or via_tv or via_cookie):
            user = {"role": "kiosk", "bde_name": None, "email": "kiosk", "name": "Display"}
        request.state.user = user

        if path not in _PUBLIC and user is None:
            if path.startswith("/api"):
                return JSONResponse({"error": "auth required"}, status_code=401)
            return RedirectResponse("/login", status_code=302)

        resp = await call_next(request)
        # Only persist the kiosk cookie for an ACTUAL kiosk view (not a logged-in user
        # who happened to open a TV link) — so logins are never downgraded to kiosk.
        if (via_query_token or via_tv) and (request.state.user or {}).get("role") == "kiosk":
            resp.set_cookie("fa_kiosk", ktok or "tv", max_age=31536000, httponly=True, samesite="lax")
        return resp

    def _scoped_bde(request: Request, requested: str | None) -> str:
        """BDEs are forced to their own data; managers/kiosk see what they ask for."""
        u = getattr(request.state, "user", None) or {}
        if u.get("role") == "bde":
            return u.get("bde_name") or "__none__"
        return requested or "ALL"

    def _is_bde(request: Request) -> bool:
        return (getattr(request.state, "user", None) or {}).get("role") == "bde"

    @app.get("/login", response_class=HTMLResponse)
    def login_page() -> str:
        return _static("login.html")

    @app.post("/login")
    async def login_post(request: Request):
        # Parse the urlencoded form manually (avoids the python-multipart dependency).
        from urllib.parse import parse_qs
        body = (await request.body()).decode("utf-8")
        data = {k: v[0] for k, v in parse_qs(body).items()}
        email = (data.get("email") or "").strip()
        pw = data.get("password") or ""
        u = await run_in_threadpool(get_user_by_email, pool, email)
        if not u or not u.get("active") or not verify_password(pw, u["password_hash"]):
            return RedirectResponse("/login?e=1", status_code=302)
        token, _exp = await run_in_threadpool(create_session, pool, u["id"])
        resp = RedirectResponse("/", status_code=302)
        resp.set_cookie("fa_session", token, max_age=2592000, httponly=True, samesite="lax")
        return resp

    @app.get("/logout")
    async def logout(request: Request):
        await run_in_threadpool(delete_session, pool, request.cookies.get("fa_session"))
        resp = RedirectResponse("/login", status_code=302)
        resp.delete_cookie("fa_session")
        return resp

    @app.get("/api/me")
    def me(request: Request) -> JSONResponse:
        u = getattr(request.state, "user", None) or {}
        return JSONResponse({"email": u.get("email"), "name": u.get("name"),
                             "role": u.get("role"), "bde_name": u.get("bde_name"),
                             "is_admin": is_admin(u)})

    @app.get("/api/recent-bookings")
    def recent_bookings(request: Request) -> JSONResponse:
        """Recent NEW meetings booked (counted, last 6h) for admin/BDM browser notifications.
        Excludes confirmations, reschedules and already-booked hand-offs (not new bookings)."""
        u = getattr(request.state, "user", None) or {}
        if not (is_admin(u) or u.get("role") == "bdm"):
            raise HTTPException(status_code=403, detail="admin / BDM only")
        rows = q(
            "SELECT call_id, started_at, bde, prospect_company, dest_number, meeting_datetime, source FROM ( "
            # new bookings made on a call in the last 6h
            "  SELECT c.call_id, c.started_at, COALESCE(c.bde_name, c.bde_extension) AS bde, "
            "         cl.prospect_company, c.dest_number, cl.meeting_datetime, 'call' AS source "
            "  FROM calls c JOIN classifications cl ON cl.call_id = c.call_id "
            "  WHERE c.in_scope AND cl.meeting_booked = true "
            "    AND NOT COALESCE(cl.meeting_confirmation, false) "
            "    AND NOT COALESCE(cl.meeting_rescheduled, false) "
            "    AND NOT COALESCE(cl.booking_already_exists, false) "
            "    AND c.started_at > now() - interval '6 hours' "
            "  UNION ALL "
            # bookings a prospect CONFIRMED BY SMS in the last 6h (firmed a prior call)
            "  SELECT m.applied_call_id AS call_id, m.time_sent AS started_at, "
            "         COALESCE(m.bde_name, c2.bde_name, c2.bde_extension) AS bde, "
            "         cl2.prospect_company, m.sender_phone AS dest_number, m.meeting_datetime, 'sms' AS source "
            "  FROM messages m JOIN calls c2 ON c2.call_id = m.applied_call_id "
            "  LEFT JOIN classifications cl2 ON cl2.call_id = m.applied_call_id "
            "  WHERE m.is_booking_confirmation AND m.applied_call_id IS NOT NULL "
            "    AND m.time_sent > now() - interval '6 hours' "
            ") t ORDER BY started_at DESC LIMIT 30"
        )
        for r in rows:
            r["started_at"] = str(r["started_at"]) if r.get("started_at") else None
        return JSONResponse(jsonable_encoder({"bookings": rows}))

    # ---- admin: user management (admin role only) ----------------------- #
    def _require_admin(request: Request) -> dict:
        u = getattr(request.state, "user", None) or {}
        if not is_admin(u):
            raise HTTPException(status_code=403, detail="admin access required")
        return u

    @app.get("/admin", response_class=HTMLResponse)
    def admin_page(request: Request):
        u = getattr(request.state, "user", None) or {}
        if not is_admin(u):
            # Non-admins are bounced back to the dashboard rather than shown the panel.
            return RedirectResponse("/", status_code=302)
        return _static("admin.html")

    @app.get("/api/admin/users")
    def admin_list_users(request: Request) -> JSONResponse:
        _require_admin(request)
        rows = list_users(pool)
        for r in rows:
            r["created_at"] = str(r["created_at"]) if r.get("created_at") else None
        return JSONResponse({"users": jsonable_encoder(rows)})

    async def _form(request: Request) -> dict:
        """Parse a urlencoded or JSON body (no python-multipart dependency)."""
        ctype = request.headers.get("content-type", "")
        raw = (await request.body()).decode("utf-8")
        if "application/json" in ctype:
            import json
            return json.loads(raw or "{}")
        from urllib.parse import parse_qs
        return {k: v[0] for k, v in parse_qs(raw).items()}

    @app.post("/api/admin/users")
    async def admin_create_user(request: Request) -> JSONResponse:
        _require_admin(request)
        d = await _form(request)
        try:
            row = await run_in_threadpool(
                create_user, pool,
                email=d.get("email", ""), name=d.get("name", ""),
                role=d.get("role", "bde"), bde_name=d.get("bde_name"),
                password=d.get("password", ""),
                must_change=str(d.get("must_change", "true")).lower() in ("1", "true", "yes", "on"),
            )
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        return JSONResponse({"ok": True, "user": jsonable_encoder(row)})

    @app.post("/api/admin/users/{user_id}")
    async def admin_update_user(user_id: int, request: Request) -> JSONResponse:
        _require_admin(request)
        d = await _form(request)
        active = d.get("active")
        active_val = None if active is None else str(active).lower() in ("1", "true", "yes", "on")
        try:
            ok = await run_in_threadpool(
                update_user, pool, user_id,
                name=d.get("name"), role=d.get("role"),
                bde_name=d.get("bde_name"), active=active_val,
            )
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        return JSONResponse({"ok": ok})

    @app.post("/api/admin/users/{user_id}/password")
    async def admin_set_password(user_id: int, request: Request) -> JSONResponse:
        _require_admin(request)
        d = await _form(request)
        pw = d.get("password") or ""
        if len(pw) < 6:
            return JSONResponse({"error": "password must be at least 6 characters"}, status_code=400)
        must = str(d.get("must_change", "false")).lower() in ("1", "true", "yes", "on")
        ok = await run_in_threadpool(set_password_by_id, pool, user_id, pw, must_change=must)
        return JSONResponse({"ok": ok})

    @app.post("/api/admin/users/{user_id}/delete")
    async def admin_delete_user(user_id: int, request: Request) -> JSONResponse:
        admin = _require_admin(request)
        if admin.get("id") == user_id:
            return JSONResponse({"error": "you cannot delete your own account"}, status_code=400)
        ok = await run_in_threadpool(delete_user, pool, user_id)
        return JSONResponse({"ok": ok})

    # ---- pages ---------------------------------------------------------- #
    # Dashboard pages embed their JS/CSS inline, so a stale cached page = stale UI.
    # Serve the app HTML with no-cache so a refresh always picks up new code.
    _NOCACHE = {"Cache-Control": "no-cache, no-store, must-revalidate", "Pragma": "no-cache"}

    @app.get("/", response_class=HTMLResponse)
    def index() -> HTMLResponse:
        return HTMLResponse(_index_html(), headers=_NOCACHE)

    @app.get("/tv", response_class=HTMLResponse)
    def tv_page() -> str:
        return resources.files("funnel_agent.dashboard").joinpath(
            "static/tv.html").read_text(encoding="utf-8")

    @app.get("/logo.png")
    def logo() -> Response:
        data = resources.files("funnel_agent.dashboard").joinpath(
            "static/logo.png").read_bytes()
        return Response(content=data, media_type="image/png",
                        headers={"Cache-Control": "public, max-age=86400"})

    @app.get("/healthz")
    def healthz() -> dict:
        # Liveness only (no DB) so the Railway healthcheck passes as soon as the
        # web process is up, decoupled from database availability.
        return {"ok": True}

    @app.get("/readyz")
    def readyz() -> JSONResponse:
        # Readiness: confirms the analytics DB is reachable.
        try:
            q("SELECT 1")
            return JSONResponse({"ready": True})
        except Exception as exc:
            return JSONResponse({"ready": False, "error": str(exc)[:200]}, status_code=503)

    # ---- realtime push (Server-Sent Events) ----------------------------- #
    def _freshness_token() -> str:
        """A cheap fingerprint of the data. Changes whenever calls are ingested
        or (re)classified — i.e. whenever any dashboard number could change."""
        r = q(
            "SELECT (SELECT max(classified_at) FROM classifications) AS t, "
            "(SELECT count(*) FROM classifications) AS nc, "
            "(SELECT count(*) FROM calls) AS ncalls"
        )[0]
        return f"{r['t']}|{r['nc']}|{r['ncalls']}"

    @app.get("/api/events")
    async def events() -> StreamingResponse:
        """Push a `refresh` event the moment the underlying data changes, so the
        dashboard updates in near-real-time instead of waiting on a poll timer.
        Detects change via a tiny DB fingerprint (works across the separate
        refresh process). Auto-reconnects from the browser's EventSource."""
        async def gen():
            last = None
            beats = 0
            while True:
                try:
                    tok = await run_in_threadpool(_freshness_token)
                except Exception:
                    tok = last  # transient DB hiccup: keep the stream alive
                if tok != last:
                    last = tok
                    yield f"event: refresh\ndata: {tok}\n\n"
                else:
                    beats += 1
                    if beats >= 7:  # ~20s heartbeat keeps the connection open
                        beats = 0
                        yield ": keepalive\n\n"
                await asyncio.sleep(3)

        return StreamingResponse(
            gen(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "Connection": "keep-alive",
                     "X-Accel-Buffering": "no"},
        )

    # ---- api ------------------------------------------------------------ #
    @app.get("/api/classification-progress")
    def classification_progress() -> JSONResponse:
        rows = q(
            "SELECT count(*) AS total, count(cl.call_id) AS done "
            "FROM calls c LEFT JOIN classifications cl ON cl.call_id = c.call_id "
            "WHERE c.in_scope AND c.has_transcript"
        )
        total = int(rows[0]["total"] or 0)
        done = int(rows[0]["done"] or 0)
        return JSONResponse({
            "total": total, "done": done, "pending": total - done,
            "pct": round(100 * done / total, 1) if total else 100.0,
        })

    @app.get("/api/summary")
    def summary(request: Request) -> JSONResponse:
        # Only days with actual activity — so "today" anchors to the last working day
        # and the picker never offers empty weekend/future rows.
        dates = [str(r["report_date"]) for r in q(
            "SELECT report_date FROM daily_funnel WHERE bde_name='ALL' AND track='combined' "
            "AND COALESCE(calls_made,0) > 0 ORDER BY report_date DESC")]
        if _is_bde(request):  # a BDE only ever sees themselves
            own = _scoped_bde(request, None)
            bdes = [own]
        else:
            bdes = ["ALL", *[r["bde_name"] for r in q(
                "SELECT DISTINCT bde_name FROM daily_funnel WHERE bde_name <> 'ALL' ORDER BY bde_name")]]
        review = q("SELECT count(*) AS n FROM classifications WHERE needs_human_review")
        u = getattr(request.state, "user", None) or {}
        return JSONResponse({
            "dates": dates,
            "bdes": bdes,
            "review_count": review[0]["n"] if review else 0,
            "stages": [s[0] for s in STAGES],
            "user": {"name": u.get("name"), "role": u.get("role"), "bde_name": u.get("bde_name")},
        })

    # Columns we sum over a date range (daily_funnel is purely additive per day).
    _SUM_COLS = ("calls_made", "connected", "transcribed", "rpc_connect", "full_pitch",
                 "leads", "qualified", "meetings_booked", "qualified_booked", "meetings_done",
                 "warm", "hot", "super_hot", "pipeline1", "pipeline2")
    _LEAD_COLS = ("warm", "hot", "super_hot", "pipeline1", "pipeline2")

    def _latest_date() -> str | None:
        # Last day with actual activity (ignores empty future/weekend rows).
        r = q("SELECT max(report_date) AS d FROM daily_funnel WHERE bde_name='ALL' "
              "AND track='combined' AND COALESCE(calls_made,0) > 0")
        if r and r[0]["d"]:
            return str(r[0]["d"])
        r = q("SELECT max(report_date) AS d FROM daily_funnel")
        return str(r[0]["d"]) if r and r[0]["d"] else None

    def _resolve_window(date: str | None, start: str | None, end: str | None):
        """A single `date` OR a `start`+`end` range OR (default) the latest day."""
        if start and end:
            return start, end
        if date:
            return date, date
        d = _latest_date()
        return d, d

    def _sums_by_track(bde: str, start: str, end: str) -> dict:
        cols = ", ".join(f"COALESCE(SUM({c}),0) AS {c}" for c in _SUM_COLS)
        rows = q(
            f"SELECT track, {cols} FROM daily_funnel "
            "WHERE bde_name=%s AND report_date BETWEEN %s AND %s GROUP BY track",
            (bde, start, end),
        )
        return {r["track"]: r for r in rows}

    def _prev_window(start: str, end: str):
        """The previous equal-length period that actually has activity — ends on the
        last working day BEFORE `start` (skips weekends/empty days), so a Monday is
        compared to the previous Friday rather than to a dead Sunday."""
        s, e = _date.fromisoformat(start), _date.fromisoformat(end)
        length = (e - s).days + 1
        r = q("SELECT max(report_date) AS d FROM daily_funnel WHERE bde_name='ALL' "
              "AND track='combined' AND COALESCE(calls_made,0) > 0 AND report_date < %s", (start,))
        pe = r[0]["d"] if r and r[0]["d"] else (s - timedelta(days=1))
        ps = pe - timedelta(days=length - 1)
        return ps.isoformat(), pe.isoformat()

    def _calls_made_target(bde: str, start: str, end: str):
        """Calls-Made benchmark as a TEAM target, LIVE and time-elapsed-aware.

        The team should dial CALLS_PER_30MIN per 30-min block. Each working day's clock
        starts at the day's FIRST call but never before WORKDAY_START_HOUR (9:00 AM
        Melbourne, so a stray pre-9 call doesn't move it). Calling time is the wall-clock
        elapsed since then, CAPPED at CALLING_HOURS (8h) — we don't model a fixed lunch
        window; the extra ~1h break is flexible and self-managed. The expected ("target")
        count is 75 x the calling time elapsed so far — small early in the day, growing to
        the full day's 1200 by close. A single-BDE view shows the equal share (team / N).
        Returns (target_count, elapsed_half_hours, rate_target)."""
        tz = ZoneInfo(settings.tz)
        now = _dt.now(tz)
        cap_min = CALLING_HOURS * 60
        rows = q("SELECT min(started_at) AS fc FROM calls "
                 "WHERE in_scope AND started_at >= %s::date AND started_at < (%s::date + 1) "
                 "GROUP BY (started_at AT TIME ZONE %s)::date",
                 (start, end, settings.tz))
        total_min = 0.0
        for r in rows:
            if not r["fc"]:
                continue
            fc = r["fc"].astimezone(tz)
            nine = _dt.combine(fc.date(), _time(WORKDAY_START_HOUR, 0), tzinfo=tz)
            anchor = max(fc, nine)                       # floor the start at 9:00 AM
            end_pt = min(now, anchor + timedelta(hours=DAY_SPAN_HOURS))
            elapsed = (end_pt - anchor).total_seconds() / 60.0
            total_min += min(max(0.0, elapsed), cap_min)  # cap calling time at 8h/day
        half_hours = total_min / 30.0
        if bde and bde != "ALL":
            rc = q("SELECT count(DISTINCT COALESCE(bde_name, extension)) AS n "
                   "FROM bde_agents WHERE in_scope AND active")
            n = max(1, int(rc[0]["n"]) if rc and rc[0]["n"] else 1)
            return int(round(CALLS_PER_30MIN * half_hours / n)), half_hours, round(CALLS_PER_30MIN / n, 1)
        return int(round(CALLS_PER_30MIN * half_hours)), half_hours, float(CALLS_PER_30MIN)

    def _workday_progress() -> dict:
        """TODAY's calling-time progress vs the 8-hour calling target (9am Melbourne start,
        wall-clock elapsed capped at 8h — no fixed lunch window). For the header timer."""
        tz = ZoneInfo(settings.tz)
        now = _dt.now(tz)
        today = now.date()
        start_dt = _dt.combine(today, _time(WORKDAY_START_HOUR, 0), tzinfo=tz)
        end_dt = start_dt + timedelta(hours=DAY_SPAN_HOURS)           # 6:00 PM
        total_min = CALLING_HOURS * 60                               # 480 = 8h
        elapsed = 0.0 if now <= start_dt else (min(now, end_dt) - start_dt).total_seconds() / 60.0
        elapsed = max(0.0, min(elapsed, total_min))                  # cap at 8h calling
        return {
            "elapsed_min": int(round(elapsed)), "total_min": total_min,
            "pct": int(round(100 * elapsed / total_min)) if total_min else 0,
            "ended": now >= end_dt, "not_started": now < start_dt,
        }

    @app.get("/api/funnel")
    def funnel(request: Request, date: str | None = None, start: str | None = None,
               end: str | None = None, bde: str = "ALL", compare: str = "") -> JSONResponse:
        bde = _scoped_bde(request, bde)
        start, end = _resolve_window(date, start, end)
        if not start:
            return JSONResponse({"found": False, "stages": [], "conversion": {}})
        tracks = _sums_by_track(bde, start, end)
        if not tracks:
            return JSONResponse({"found": False, "stages": [], "conversion": {},
                                 "window": {"start": start, "end": end}})

        def col(t: str, key: str) -> int:
            return int((tracks.get(t) or {}).get(key) or 0)

        # Each stage carries its conversion from the PREVIOUS stage (funnel drop-off).
        stage_rows = []
        prev = None
        for label, key in STAGES:
            tot = col("combined", key)
            stage_rows.append({
                "stage": label, "key": key, "fresh": col("fresh", key),
                "followup": col("followup", key), "total": tot,
                "conv": (_pct(tot, prev) if prev else None),
            })
            prev = tot
        cm = col("combined", "calls_made")
        conv = {
            "connect": _pct(col("combined", "connected"), cm),
            "rpc": _pct(col("combined", "rpc_connect"), col("combined", "connected")),
            "pitch": _pct(col("combined", "full_pitch"), col("combined", "rpc_connect")),
            "booked": _pct(col("combined", "meetings_booked"), col("combined", "full_pitch")),
        }
        # ---- Benchmark targets per stage (#4): flag stages below the standard. ----
        totals = {key: col("combined", key) for _, key in STAGES}
        bench: dict = {}
        cm_target, half_hours, cm_rate_target = _calls_made_target(bde, start, end)
        if cm_target:
            # Achieved dials per 30-min block over the standard workday (team basis).
            rate = round(totals["calls_made"] / half_hours, 1) if half_hours else None
            scope = "team" if (not bde or bde == "ALL") else "per-BDE share"
            bench["calls_made"] = {
                "target": cm_target, "actual": totals["calls_made"],
                "met": totals["calls_made"] >= cm_target,
                "short": max(0, cm_target - totals["calls_made"]),
                "rate": rate, "target_rate": cm_rate_target,
                "basis": f"{scope} target: {cm_rate_target} dials/30min from 9am, 8 calling hrs/day — {cm_target} expected so far",
            }
        # Cascade each stage's target from the TIME-PRORATED Calls-Made target down the funnel using
        # the standard conversion ratios (40% connected → 40% RPC → 30% pitch → 25% booked). The base
        # is the calls ACHIEVABLE by the calling-time elapsed so far (75/30min from 9am), so the stage
        # targets grow through the day: at 4h the connected target is ~240, reaching the full 480 only
        # once all 8 calling hours are done — showing a fair "how far short right now" at any hour.
        target_chain = {"calls_made": cm_target}
        for key, (ratio, prior) in BENCHMARK_RATIOS.items():
            target_chain[key] = int(round(ratio * target_chain.get(prior, 0)))
        for key, (ratio, prior) in BENCHMARK_RATIOS.items():
            target = target_chain[key]
            actual = totals.get(key, 0)
            bench[key] = {
                "target": target, "actual": actual, "ratio": int(ratio * 100),
                "prior": prior, "met": actual >= target, "short": max(0, target - actual),
            }
        out = {
            "found": True, "window": {"start": start, "end": end},
            "stages": stage_rows, "conversion": conv, "benchmark": bench,
            "coverage": _pct(col("combined", "transcribed"), cm),
            "calls_made": cm, "transcribed": col("combined", "transcribed"),
            "lead": {k: col("combined", k) for k in _LEAD_COLS},
            "workday": _workday_progress(),
        }
        if compare == "prev":
            ps, pe = _prev_window(start, end)
            pt = _sums_by_track(bde, ps, pe).get("combined") or {}
            out["compare"] = {
                "window": {"start": ps, "end": pe},
                "stages": {key: int(pt.get(key) or 0) for _, key in STAGES},
                "calls_made": int(pt.get("calls_made") or 0),
                "lead": {k: int(pt.get(k) or 0) for k in _LEAD_COLS},
            }
        return JSONResponse(out)

    @app.get("/coaching", response_class=HTMLResponse)
    def coaching_page() -> HTMLResponse:
        return HTMLResponse(_static("coaching.html"), headers=_NOCACHE)

    @app.get("/next-calls", response_class=HTMLResponse)
    def next_calls_page() -> HTMLResponse:
        """Ranked 'who to call next' board (A) — reads /api/next-calls."""
        return HTMLResponse(_static("next-calls.html"), headers=_NOCACHE)

    @app.get("/api/rpc-monitor")
    def rpc_monitor(request: Request, bde: str = "ALL", limit: int = 300) -> JSONResponse:
        """#10b — RPC follow-up worklist + enforcement. For every dialled number NOT yet
        connected to the decision-maker, show the attempt sequence and the REQUIRED next
        action so the BDE keeps working it: mobile 1st no-answer -> call again (double-tap);
        mobile 2+ no-answer -> SMS + voicemail; gatekeeper reached -> get DM name + callback.
        Persists per number until RPC is reached."""
        scope = _scoped_bde(request, bde) if _is_bde(request) else (bde or "ALL")
        params: dict = {"thr": settings.rpc_min_talk_seconds, "lim": limit}
        bde_filter = ""
        if scope and scope != "ALL":
            bde_filter = "AND COALESCE(c.bde_name,c.bde_extension) = %(bde)s"
            params["bde"] = scope
        conn = "c.answered AND c.talk_seconds >= %(thr)s AND COALESCE(cl.call_outcome,'')<>'voicemail'"
        digits = "right(regexp_replace(c.dest_number,'[^0-9]','','g'),9)"
        chan = (f"CASE WHEN regexp_replace(c.dest_number,'[^0-9]','','g') ~ '1(3|8)00' THEN 'tollfree' "
                f"WHEN {digits} ~ '^4' THEN 'mobile' ELSE 'landline' END")
        rows = q(
            f"SELECT {digits} AS dest9, max(c.dest_number) AS dest_number, "
            f"max({chan}) AS channel, count(*) AS attempts, "
            f"count(*) FILTER (WHERE c.answered) AS answered, "
            f"bool_or({conn} AND cl.rpc_connect) AS ever_rpc, "
            f"bool_or(cl.call_outcome='gatekeeper' OR cl.evidence->>'who_answered' ILIKE '%%gatekeeper%%') AS hit_gatekeeper, "
            "bool_or(COALESCE(cl.gatekeeper_handled_well,false)) AS gk_handled_well, "
            "max(c.started_at) AS last_attempt, "
            "(array_agg(COALESCE(c.bde_name,c.bde_extension) ORDER BY c.started_at DESC))[1] AS last_bde, "
            "(array_remove(array_agg(NULLIF(cl.prospect_company,'') ORDER BY c.started_at DESC), NULL))[1] AS business_name, "
            "(array_remove(array_agg(NULLIF(cl.gatekeeper_notes,'') ORDER BY c.started_at DESC), NULL))[1] AS gatekeeper_notes, "
            "(array_remove(array_agg(NULLIF(cl.callback_when,'') ORDER BY c.started_at DESC), NULL))[1] AS callback_when "
            "FROM calls c LEFT JOIN classifications cl ON cl.call_id=c.call_id "
            f"WHERE c.in_scope AND c.dest_number IS NOT NULL AND c.dest_number<>'' {bde_filter} "
            f"GROUP BY {digits} "
            # only numbers NOT yet connected to the decision-maker (the worklist)
            f"HAVING NOT bool_or({conn} AND cl.rpc_connect) "
            "ORDER BY max(c.started_at) DESC LIMIT %(lim)s",
            params,
        )
        out = []
        for r in rows:
            ch, att, ans = r["channel"], int(r["attempts"] or 0), int(r["answered"] or 0)
            if r["hit_gatekeeper"]:
                action, urg = "Gatekeeper reached — get the decision-maker's name & best callback time, then retry", "high"
            elif ch == "mobile" and ans == 0 and att <= 1:
                action, urg = "Double-tap: call again now (mobile, 1 missed)", "high"
            elif ch == "mobile" and ans == 0 and att >= 2:
                action, urg = "Send a text + leave a voicemail, then schedule a retry", "high"
            elif ans > 0:
                action, urg = "Answered but not the decision-maker — ask for the owner / decision-maker", "med"
            else:
                action, urg = "Keep trying — vary the call time until you reach the decision-maker", "med"
            r["last_attempt"] = str(r["last_attempt"]) if r.get("last_attempt") else None
            # BDE compliance: a mobile that went unanswered should have had a 2nd attempt.
            double_tap_done = ch == "mobile" and att >= 2
            mobile_missed = ch == "mobile" and ans == 0
            out.append({**r, "required_action": action, "urgency": urg,
                        "double_tap_done": double_tap_done, "mobile_missed": mobile_missed})
        # Compliance: of mobile-missed numbers, how many got the required 2nd attempt.
        mobile_missed_total = sum(1 for x in out if x["mobile_missed"])
        complied = sum(1 for x in out if x["mobile_missed"] and x["double_tap_done"])
        summary = {
            "open": len(out),
            "mobile_double_tap_needed": sum(1 for x in out if "Double-tap" in x["required_action"]),
            "needs_sms_vm": sum(1 for x in out if "text" in x["required_action"]),
            "gatekeeper_blocked": sum(1 for x in out if x["hit_gatekeeper"]),
            "gatekeeper_mishandled": sum(1 for x in out if x["hit_gatekeeper"] and not x["gk_handled_well"]),
            "double_tap_compliance_pct": (round(100 * complied / mobile_missed_total) if mobile_missed_total else None),
        }
        return JSONResponse(jsonable_encoder({"summary": summary, "rows": out}))

    @app.get("/api/coaching")
    def coaching(request: Request, bde: str, date: str | None = None, start: str | None = None,
                 end: str | None = None) -> JSONResponse:
        """#10a — feedback / coaching intelligence for one BDE: a training & development
        view built from the AI's per-call analysis. Shows scorecard averages, the trend
        vs the earlier half of the period, what they do DIFFERENTLY on calls they book
        (the winning behaviours), recurring coaching tips, and unhandled objections."""
        start, end = _resolve_window(date, start, end)
        if _is_bde(request):
            bde = _scoped_bde(request, None)  # a BDE only sees their own coaching
        if not bde or bde == "ALL":
            return JSONResponse({"error": "pick a BDE"}, status_code=400)
        base = ("FROM calls c JOIN classifications cl ON cl.call_id=c.call_id "
                "WHERE c.in_scope AND COALESCE(c.bde_name,c.bde_extension)=%(bde)s "
                "AND c.started_at >= %(s)s::date AND c.started_at < (%(e)s::date + 1) AND cl.rpc_connect")
        p = {"bde": bde, "s": start, "e": end}
        dims = ["opening", "discovery", "pitch", "objection_handling", "close"]
        avg = ", ".join(f"round(avg((cl.evidence->'scorecard'->>'{d}')::numeric),2) AS {d}" for d in dims)
        overall = q(f"SELECT count(*) AS pitched, "
                    f"count(*) FILTER (WHERE cl.meeting_booked AND NOT COALESCE(cl.meeting_confirmation,false) "
                    f"AND NOT COALESCE(cl.meeting_rescheduled,false)) AS booked, {avg} {base} "
                    "AND cl.evidence ? 'scorecard'", p)
        ov = overall[0] if overall else {}
        # Booked vs non-booked behaviour: what they do differently when they win.
        def _avg_for(cond):
            r = q(f"SELECT {avg} {base} AND cl.evidence ? 'scorecard' AND {cond}", p)
            return r[0] if r else {}
        booked_cond = "cl.meeting_booked AND NOT COALESCE(cl.meeting_confirmation,false) AND NOT COALESCE(cl.meeting_rescheduled,false)"
        won = _avg_for(booked_cond)
        lost = _avg_for(f"NOT ({booked_cond})")
        # Trend: recent half vs earlier half of the window.
        mid = q("SELECT (%(s)s::date + ((%(e)s::date - %(s)s::date)/2)) AS m", p)[0]["m"]
        p2 = {**p, "m": str(mid)}
        recent = q(f"SELECT {avg} {base} AND cl.evidence ? 'scorecard' AND c.started_at >= %(m)s::date", p2)
        earlier = q(f"SELECT {avg} {base} AND cl.evidence ? 'scorecard' AND c.started_at < %(m)s::date", p2)
        # Recurring coaching tips + unhandled objections (most frequent).
        tips = q("SELECT lower(trim(t.tip)) AS tip, count(*) AS n FROM calls c "
                 "JOIN classifications cl ON cl.call_id=c.call_id, "
                 "jsonb_array_elements_text(COALESCE(cl.evidence->'coaching_tips','[]'::jsonb)) AS t(tip) "
                 "WHERE c.in_scope AND COALESCE(c.bde_name,c.bde_extension)=%(bde)s "
                 "AND c.started_at >= %(s)s::date AND c.started_at < (%(e)s::date + 1) "
                 "GROUP BY 1 ORDER BY n DESC LIMIT 8", p)
        objs = q("SELECT lower(trim(o.obj->>'objection')) AS objection, count(*) AS n FROM calls c "
                 "JOIN classifications cl ON cl.call_id=c.call_id, "
                 "jsonb_array_elements(COALESCE(cl.evidence->'objections','[]'::jsonb)) AS o(obj) "
                 "WHERE c.in_scope AND COALESCE(c.bde_name,c.bde_extension)=%(bde)s "
                 "AND c.started_at >= %(s)s::date AND c.started_at < (%(e)s::date + 1) "
                 "AND COALESCE((o.obj->>'handled')::boolean, false) = false "
                 "GROUP BY 1 ORDER BY n DESC LIMIT 8", p)
        # ---- Feedback intelligence: period-over-period funnel + a generated narrative
        # (what changed, why, and the next action) — #6. ----
        cur_t = _sums_by_track(bde, start, end).get("combined") or {}
        ps, pe = _prev_window(start, end)
        prev_t = _sums_by_track(bde, ps, pe).get("combined") or {}
        def cv(t, n, d): return _pct(int(t.get(n) or 0), int(t.get(d) or 0))
        funnel = {
            "connect": [cv(cur_t, "connected", "calls_made"), cv(prev_t, "connected", "calls_made")],
            "rpc": [cv(cur_t, "rpc_connect", "connected"), cv(prev_t, "rpc_connect", "connected")],
            "pitch": [cv(cur_t, "full_pitch", "rpc_connect"), cv(prev_t, "full_pitch", "rpc_connect")],
            "booked": [cv(cur_t, "meetings_booked", "full_pitch"), cv(prev_t, "meetings_booked", "full_pitch")],
        }
        insights = []
        STAGE_LABEL = {"connect": "Connect rate", "rpc": "Right-Party-Contact rate",
                       "pitch": "Pitch rate", "booked": "Booking rate"}
        STAGE_FIX = {"connect": "vary call times and double-tap missed mobiles",
                     "rpc": "improve gatekeeper handling — ask for the decision-maker by name",
                     "pitch": "complete the full pitch every time you reach a decision-maker",
                     "booked": "always ask for the meeting and handle the final objection"}
        for k in ("connect", "rpc", "pitch", "booked"):
            now_v, prev_v = funnel[k]
            if now_v is None or prev_v is None:
                continue
            delta = round(now_v - prev_v, 1)
            if delta <= -5:
                insights.append({"kind": "down", "text": f"{STAGE_LABEL[k]} dropped {abs(delta)} pts vs the previous period ({prev_v}% → {now_v}%). Fix: {STAGE_FIX[k]}."})
            elif delta >= 5:
                insights.append({"kind": "up", "text": f"{STAGE_LABEL[k]} improved {delta} pts vs the previous period ({prev_v}% → {now_v}%). Keep doing what changed here."})
        # Winning behaviours: biggest booked-vs-not gap → replicate it.
        gaps = []
        for k in dims:
            w_ = won.get(k); l_ = lost.get(k)
            if w_ is not None and l_ is not None:
                gaps.append((k, round(float(w_) - float(l_), 1)))
        gaps.sort(key=lambda x: x[1], reverse=True)
        if gaps and gaps[0][1] >= 1:
            g = gaps[0]
            insights.append({"kind": "tip", "text": f"On calls you booked, your {g[0].replace('_',' ')} scored {g[1]} pts higher than on calls you didn't — replicate that on every pitch."})
        # Weakest skill overall.
        weak = sorted([(k, ov.get(k)) for k in dims if ov.get(k) is not None], key=lambda x: float(x[1]))
        if weak and float(weak[0][1]) < 2.5:
            insights.append({"kind": "down", "text": f"Weakest skill this period: {weak[0][0].replace('_',' ')} ({weak[0][1]}/5) — focus coaching here."})
        if objs:
            insights.append({"kind": "tip", "text": f"Most common unhandled objection: “{objs[0]['objection']}” (×{objs[0]['n']}) — prepare a confident response."})
        return JSONResponse(jsonable_encoder({
            "bde": bde, "window": {"start": start, "end": end},
            "overall": ov, "booked": won, "not_booked": lost,
            "trend": {"recent": recent[0] if recent else {}, "earlier": earlier[0] if earlier else {}},
            "dims": dims, "funnel": funnel, "prev_window": {"start": ps, "end": pe}, "insights": insights,
            "recurring_tips": [t for t in tips if t["tip"]],
            "unhandled_objections": [o for o in objs if o["objection"]],
        }))

    @app.get("/api/pitch-quality")
    def pitch_quality(request: Request, date: str | None = None, start: str | None = None,
                      end: str | None = None) -> JSONResponse:
        """#12 — per-BDE pitch-quality intelligence from the AI scorecard (0-5 on opening,
        discovery, pitch, objection handling, close). Flags weak pitches: an RPC-connected
        call where the pitch score <= 2. Averages over calls that reached a decision-maker."""
        start, end = _resolve_window(date, start, end)
        if not start:
            return JSONResponse({"rows": []})
        bde = _scoped_bde(request, "ALL") if _is_bde(request) else "ALL"
        where = ["c.in_scope", "c.started_at >= %(s)s::date", "c.started_at < (%(e)s::date + 1)",
                 "cl.rpc_connect", "cl.evidence ? 'scorecard'"]
        params: dict = {"s": start, "e": end}
        if bde and bde != "ALL":
            where.append("COALESCE(c.bde_name, c.bde_extension) = %(bde)s")
            params["bde"] = bde
        sc = lambda k: f"(cl.evidence->'scorecard'->>'{k}')::numeric"
        rows = q(
            f"SELECT COALESCE(c.bde_name,c.bde_extension) AS bde, count(*) AS pitched, "
            f"round(avg({sc('opening')}),1) AS opening, round(avg({sc('discovery')}),1) AS discovery, "
            f"round(avg({sc('pitch')}),1) AS pitch, round(avg({sc('objection_handling')}),1) AS objection, "
            f"round(avg({sc('close')}),1) AS close, "
            f"count(*) FILTER (WHERE {sc('pitch')} <= 2) AS weak_pitches "
            "FROM calls c JOIN classifications cl ON cl.call_id=c.call_id "
            f"WHERE {' AND '.join(where)} GROUP BY 1 ORDER BY pitch ASC NULLS FIRST",
            params,
        )
        return JSONResponse(jsonable_encoder({"window": {"start": start, "end": end}, "rows": rows}))

    @app.get("/api/channel-report")
    def channel_report(request: Request, date: str | None = None, start: str | None = None,
                       end: str | None = None) -> JSONResponse:
        """#11 — per-BDE outcomes split by the DIALLED number type: mobile (04x) vs
        landline (02/03/07/08) vs tollfree (1300/1800). Shows where bookings come from."""
        start, end = _resolve_window(date, start, end)
        if not start:
            return JSONResponse({"rows": []})
        bde = _scoped_bde(request, "ALL") if _is_bde(request) else "ALL"
        where = ["c.in_scope", "c.started_at >= %(s)s::date", "c.started_at < (%(e)s::date + 1)"]
        params: dict = {"s": start, "e": end, "thr": settings.rpc_min_talk_seconds}
        if bde and bde != "ALL":
            where.append("COALESCE(c.bde_name, c.bde_extension) = %(bde)s")
            params["bde"] = bde
        digits = "regexp_replace(c.dest_number,'[^0-9]','','g')"
        channel = (f"CASE WHEN {digits} ~ '1(3|8)00' THEN 'tollfree' "
                   f"WHEN right({digits},9) ~ '^4' THEN 'mobile' ELSE 'landline' END")
        booked = ("c.answered AND c.talk_seconds >= %(thr)s AND COALESCE(cl.call_outcome,'')<>'voicemail' "
                  "AND cl.meeting_booked AND NOT COALESCE(cl.meeting_confirmation,false) "
                  "AND NOT COALESCE(cl.meeting_rescheduled,false) "
                  "AND NOT EXISTS (SELECT 1 FROM calls pc JOIN classifications pcl ON pcl.call_id=pc.call_id "
                  "WHERE pc.in_scope AND right(regexp_replace(pc.dest_number,'[^0-9]','','g'),9)=right(regexp_replace(c.dest_number,'[^0-9]','','g'),9) AND pc.started_at<c.started_at "
                  "AND pc.answered AND pc.talk_seconds>=%(thr)s AND COALESCE(pcl.call_outcome,'')<>'voicemail' "
                  "AND pcl.meeting_booked AND NOT COALESCE(pcl.meeting_confirmation,false) AND NOT COALESCE(pcl.meeting_rescheduled,false))")
        conn = "c.answered AND c.talk_seconds >= %(thr)s AND COALESCE(cl.call_outcome,'')<>'voicemail'"
        rows = q(
            f"SELECT COALESCE(c.bde_name,c.bde_extension) AS bde, {channel} AS channel, "
            f"count(*) AS calls, count(*) FILTER (WHERE {conn}) AS connected, "
            f"count(*) FILTER (WHERE {booked}) AS booked "
            "FROM calls c LEFT JOIN classifications cl ON cl.call_id=c.call_id "
            f"WHERE {' AND '.join(where)} GROUP BY 1,2 ORDER BY 1,2",
            params,
        )
        # pivot per BDE → {mobile:{...}, landline:{...}, tollfree:{...}}
        bdes: dict = {}
        for r in rows:
            b = bdes.setdefault(r["bde"], {"bde": r["bde"], "mobile": {}, "landline": {}, "tollfree": {}})
            b[r["channel"]] = {"calls": r["calls"], "connected": r["connected"], "booked": r["booked"]}
        return JSONResponse(jsonable_encoder({"window": {"start": start, "end": end},
                                              "rows": sorted(bdes.values(), key=lambda x: x["bde"])}))

    @app.get("/api/leaderboard")
    def leaderboard(request: Request, date: str | None = None, start: str | None = None,
                    end: str | None = None) -> JSONResponse:
        start, end = _resolve_window(date, start, end)
        if not start:
            return JSONResponse({"rows": []})
        lb_cols = ("calls_made", "connected", "transcribed", "rpc_connect", "full_pitch",
                   "leads", "qualified", "meetings_booked", "qualified_booked", *_LEAD_COLS)
        cols = ", ".join(f"COALESCE(SUM({c}),0) AS {c}" for c in lb_cols)
        where = ["track='combined'", "bde_name<>'ALL'", "report_date BETWEEN %s AND %s"]
        params: list = [start, end]
        if _is_bde(request):  # a BDE sees only their own row
            where.append("bde_name = %s")
            params.append(_scoped_bde(request, None))
        rows = q(
            f"SELECT bde_name, {cols} FROM daily_funnel WHERE {' AND '.join(where)} "
            "GROUP BY bde_name ORDER BY SUM(meetings_booked) DESC, SUM(calls_made) DESC",
            tuple(params),
        )
        # Rescheduled/confirmed bookings per BDE (reference only — NOT counted in the
        # funnel). Computed from calls/classifications since it isn't a daily_funnel column.
        ab_where = ["c.in_scope", "c.started_at >= %s::date", "c.started_at < (%s::date + 1)",
                    "c.answered", "c.talk_seconds >= %s", "COALESCE(cl.call_outcome,'') <> 'voicemail'",
                    "cl.meeting_booked",
                    "(COALESCE(cl.meeting_confirmation,false) OR COALESCE(cl.meeting_rescheduled,false))"]
        ab_params: list = [start, end, settings.rpc_min_talk_seconds]
        if _is_bde(request):
            ab_where.append("COALESCE(c.bde_name, c.bde_extension) = %s")
            ab_params.append(_scoped_bde(request, None))
        ab_rows = q(
            "SELECT COALESCE(c.bde_name, c.bde_extension) AS bde, count(*) AS n "
            "FROM calls c JOIN classifications cl ON cl.call_id=c.call_id "
            f"WHERE {' AND '.join(ab_where)} GROUP BY 1",
            tuple(ab_params),
        )
        ab = {r["bde"]: int(r["n"] or 0) for r in ab_rows}
        out = []
        for r in rows:
            out.append({
                **{k: int(r[k] or 0) for k in lb_cols},
                "bde_name": r["bde_name"],
                "already_booked": ab.get(r["bde_name"], 0),
                "conv_connect": _pct(r["connected"] or 0, r["calls_made"] or 0),
                "conv_rpc": _pct(r["rpc_connect"] or 0, r["connected"] or 0),
                "conv_pitch": _pct(r["full_pitch"] or 0, r["rpc_connect"] or 0),
                "conv_booked": _pct(r["meetings_booked"] or 0, r["full_pitch"] or 0),
            })
        return JSONResponse({"rows": out, "window": {"start": start, "end": end}})

    @app.get("/api/trend")
    def trend(request: Request, bde: str = "ALL", days: int = 30) -> JSONResponse:
        bde = _scoped_bde(request, bde)
        rows = q(
            "SELECT report_date, calls_made, transcribed, rpc_connect, full_pitch, "
            "leads, qualified, meetings_booked FROM daily_funnel "
            "WHERE bde_name=%s AND track='combined' "
            "ORDER BY report_date DESC LIMIT %s",
            (bde, days),
        )
        rows = list(reversed(rows))
        return JSONResponse({
            "dates": [str(r["report_date"]) for r in rows],
            "series": {
                key: [int(r[key] or 0) for r in rows]
                for key in ("calls_made", "transcribed", "rpc_connect",
                            "full_pitch", "leads", "qualified", "meetings_booked")
            },
        })

    # ---- pipeline tables (distinct prospects per window) ---------------- #
    _PIPE_WINDOWS = [("today", "Today", 0), ("d3", "Last 3 days", 2),
                     ("d7", "Last 7 days", 6), ("d14", "Last 14 days", 13),
                     ("d30", "Last 30 days", 29)]
    _PIPE_ROWS = [("pipeline1_interested", "Pipeline 1 · Interested (callback)"),
                  ("pipeline2_existing_agency", "Pipeline 2 · Already with an agency")]
    # Batch D 5-pipeline board. Prospect-level precedence: a prospect is shown under its
    # HIGHEST-precedence stage in each window (p5 > p2 > p1 > p3) so a booked prospect
    # appears only under P5, never P1-P4. P4 (fresh worklist) is a separate standing pool.
    _PIPE5_ROWS = [("p5", "P5 · Meeting booked"),
                   ("p2", "P2 · Already with an agency"),
                   ("p1", "P1 · RPC callback requested"),
                   ("p3", "P3 · Gatekeeper callback")]
    _STAGE_RANK = {"p5": 1, "p2": 2, "p1": 3, "p3": 4}
    _P4_SUBS = ["fresh_ads", "fresh_unscanned", "captured_3cx", "captured_aircall", "attempted"]
    # last 9 significant digits of the dialled number = the distinct-prospect key
    _DEST9_SQL = "right(regexp_replace(c.dest_number, '[^0-9]', '', 'g'), 9)"

    def _pipe_anchor() -> str | None:
        r = q("SELECT max(started_at::date) AS d FROM calls WHERE in_scope")
        return str(r[0]["d"]) if r and r[0]["d"] else None

    @app.get("/api/pipelines")
    def pipelines(request: Request, bde: str = "ALL") -> JSONResponse:
        """P1/P2 rows × today/3d/7d/14d/30d, each cell = DISTINCT prospects."""
        bde = _scoped_bde(request, bde)
        today = _pipe_anchor()
        if not today:
            return JSONResponse({"found": False, "rows": []})
        where = ["c.in_scope", "c.dest_number IS NOT NULL", "c.dest_number <> ''",
                 "cl.pipeline IN ('pipeline1_interested','pipeline2_existing_agency')",
                 "c.started_at >= (%(today)s::date - 29)", "c.started_at < (%(today)s::date + 1)"]
        params: dict = {"today": today}
        if bde and bde != "ALL":
            where.append("COALESCE(c.bde_name, c.bde_extension) = %(bde)s")
            params["bde"] = bde
        filt = ", ".join(
            f"count(DISTINCT dest9) FILTER (WHERE d >= %(today)s::date - {off}) AS {k}"
            for k, _lbl, off in _PIPE_WINDOWS
        )
        rows = q(
            f"WITH p AS (SELECT cl.pipeline AS pl, {_DEST9_SQL} AS dest9, c.started_at::date AS d "
            "FROM calls c JOIN classifications cl ON cl.call_id=c.call_id "
            f"WHERE {' AND '.join(where)}) "
            f"SELECT pl, {filt} FROM p GROUP BY pl",
            params,
        )
        bypl = {r["pl"]: r for r in rows}
        out_rows = []
        for pl, label in _PIPE_ROWS:
            r = bypl.get(pl) or {}
            out_rows.append({"pipeline": pl, "label": label,
                             "counts": {k: int(r.get(k) or 0) for k, _l, _o in _PIPE_WINDOWS}})
        return JSONResponse({"found": True, "today": today,
                             "windows": [[k, lbl] for k, lbl, _o in _PIPE_WINDOWS],
                             "rows": out_rows})

    @app.get("/api/pipeline-prospects")
    def pipeline_prospects(request: Request, pipeline: str, window: str = "d30",
                           bde: str = "ALL") -> JSONResponse:
        """The distinct prospects behind a pipeline-table cell (drill-down)."""
        bde = _scoped_bde(request, bde)
        today = _pipe_anchor()
        off = dict((k, o) for k, _l, o in _PIPE_WINDOWS).get(window, 29)
        if not today or pipeline not in dict(_PIPE_ROWS):
            return JSONResponse({"rows": []})
        where = ["c.in_scope", "c.dest_number IS NOT NULL", "c.dest_number <> ''",
                 "cl.pipeline = %(pl)s",
                 "c.started_at >= (%(today)s::date - %(off)s)", "c.started_at < (%(today)s::date + 1)"]
        params: dict = {"pl": pipeline, "today": today, "off": off}
        if bde and bde != "ALL":
            where.append("COALESCE(c.bde_name, c.bde_extension) = %(bde)s")
            params["bde"] = bde
        rows = q(
            f"WITH p AS (SELECT {_DEST9_SQL} AS dest9, c.dest_number, c.started_at, "
            "  COALESCE(c.bde_name, c.bde_extension) AS bde, cl.prospect_company, cl.lead_temperature, "
            f"  row_number() OVER (PARTITION BY {_DEST9_SQL} ORDER BY c.started_at DESC) AS rn, "
            f"  count(*) OVER (PARTITION BY {_DEST9_SQL}) AS ncalls "
            "  FROM calls c JOIN classifications cl ON cl.call_id=c.call_id "
            f"  WHERE {' AND '.join(where)}) "
            "SELECT p.dest_number, p.started_at, p.bde, p.prospect_company, p.lead_temperature, p.ncalls, "
            "       pr.business_name, pr.domain "
            "FROM p LEFT JOIN prospects pr ON p.dest9 = ANY(pr.phones_norm) "
            "WHERE p.rn = 1 ORDER BY p.started_at DESC",
            params,
        )
        for r in rows:
            r["started_at"] = str(r["started_at"]) if r["started_at"] else None
        return JSONResponse({"pipeline": pipeline, "window": window,
                             "count": len(rows), "rows": jsonable_encoder(rows)})

    # ---- Batch D: 5-pipeline board (P1/P2/P3/P5 flow + P4 standing pool) --- #
    _P5_RANK_CASE = ("CASE cl.pipeline_stage WHEN 'p5' THEN 1 WHEN 'p2' THEN 2 "
                     "WHEN 'p1' THEN 3 WHEN 'p3' THEN 4 END")

    @app.get("/api/pipelines5")
    def pipelines5(request: Request, bde: str = "ALL") -> JSONResponse:
        """P1/P2/P3/P5 × today/3d/7d/14d/30d (distinct prospects, prospect-level precedence)
        plus the P4 standing worklist pool. The 5-model view; the legacy /api/pipelines is
        left untouched."""
        bde = _scoped_bde(request, bde)
        today = _pipe_anchor()
        if not today:
            return JSONResponse({"found": False, "rows": [], "pools": {}})
        where = ["c.in_scope", "c.dest_number IS NOT NULL", "c.dest_number <> ''",
                 "cl.pipeline_stage IN ('p1','p2','p3','p5')",
                 "c.started_at >= (%(today)s::date - 29)", "c.started_at < (%(today)s::date + 1)"]
        params: dict = {"today": today}
        if bde and bde != "ALL":
            where.append("COALESCE(c.bde_name, c.bde_extension) = %(bde)s")
            params["bde"] = bde
        # Per prospect, the winning (min) stage-rank within each nested window; then count
        # distinct prospects whose winning rank is this pipeline's rank.
        win_mins = ", ".join(
            f"min(rnk) FILTER (WHERE d >= %(today)s::date - {off}) AS w_{k}"
            for k, _lbl, off in _PIPE_WINDOWS)
        cells = ", ".join(
            f"count(*) FILTER (WHERE w_{k} = {r}) AS c_{st}_{k}"
            for k, _lbl, _off in _PIPE_WINDOWS for st, r in _STAGE_RANK.items())
        rows = q(
            f"WITH base AS (SELECT {_DEST9_SQL} AS dest9, c.started_at::date AS d, {_P5_RANK_CASE} AS rnk "
            f"  FROM calls c JOIN classifications cl ON cl.call_id=c.call_id WHERE {' AND '.join(where)}), "
            f"perdest AS (SELECT dest9, {win_mins} FROM base GROUP BY dest9) "
            f"SELECT {cells} FROM perdest",
            params,
        )
        agg = rows[0] if rows else {}
        out_rows = [{"pipeline": st, "label": label,
                     "counts": {k: int(agg.get(f"c_{st}_{k}") or 0) for k, _l, _o in _PIPE_WINDOWS}}
                    for st, label in _PIPE5_ROWS]
        # P4 standing pool — the DB worklist (uncalled ads + captured + attempted), not per-day.
        pool = q(
            "SELECT "
            + ", ".join(f"count(*) FILTER (WHERE p4_subpipeline='{s}') AS {s}" for s in _P4_SUBS)
            + ", count(*) FILTER (WHERE pipeline_stage='p4' AND COALESCE(p4_subpipeline,'') "
              "NOT IN ('dead','')) AS total FROM prospects"
        )[0]
        pools = {s: int(pool.get(s) or 0) for s in _P4_SUBS}
        pools["total"] = int(pool.get("total") or 0)
        return JSONResponse({"found": True, "today": today,
                             "windows": [[k, lbl] for k, lbl, _o in _PIPE_WINDOWS],
                             "rows": out_rows, "pools": pools})

    @app.get("/api/pipeline5-prospects")
    def pipeline5_prospects(request: Request, pipeline: str, window: str = "d30",
                            bde: str = "ALL") -> JSONResponse:
        """Distinct prospects behind a P1/P2/P3/P5 board cell (prospect-level precedence)."""
        bde = _scoped_bde(request, bde)
        today = _pipe_anchor()
        off = dict((k, o) for k, _l, o in _PIPE_WINDOWS).get(window, 29)
        if not today or pipeline not in _STAGE_RANK:
            return JSONResponse({"rows": []})
        where = ["c.in_scope", "c.dest_number IS NOT NULL", "c.dest_number <> ''",
                 "cl.pipeline_stage IN ('p1','p2','p3','p5')",
                 "c.started_at >= (%(today)s::date - %(off)s)", "c.started_at < (%(today)s::date + 1)"]
        params: dict = {"today": today, "off": off, "want": _STAGE_RANK[pipeline]}
        if bde and bde != "ALL":
            where.append("COALESCE(c.bde_name, c.bde_extension) = %(bde)s")
            params["bde"] = bde
        rows = q(
            f"WITH base AS (SELECT {_DEST9_SQL} AS dest9, c.dest_number, c.started_at, "
            f"  COALESCE(c.bde_name, c.bde_extension) AS bde, cl.prospect_company, cl.lead_temperature, "
            f"  {_P5_RANK_CASE} AS rnk "
            f"  FROM calls c JOIN classifications cl ON cl.call_id=c.call_id WHERE {' AND '.join(where)}), "
            "win AS (SELECT dest9, min(rnk) AS wr, count(*) AS ncalls FROM base GROUP BY dest9), "
            "latest AS (SELECT DISTINCT ON (dest9) dest9, dest_number, started_at, bde, "
            "  prospect_company, lead_temperature FROM base ORDER BY dest9, started_at DESC) "
            "SELECT l.dest_number, l.started_at, l.bde, l.prospect_company, l.lead_temperature, "
            "  w.ncalls, pr.business_name, pr.domain "
            "FROM win w JOIN latest l ON l.dest9 = w.dest9 "
            "LEFT JOIN prospects pr ON w.dest9 = ANY(pr.phones_norm) "
            "WHERE w.wr = %(want)s ORDER BY l.started_at DESC",
            params,
        )
        for r in rows:
            r["started_at"] = str(r["started_at"]) if r["started_at"] else None
        return JSONResponse({"pipeline": pipeline, "window": window,
                             "count": len(rows), "rows": jsonable_encoder(rows)})

    @app.get("/api/review-queue")
    def review_queue(request: Request, limit: int = 50) -> JSONResponse:
        where = ["cl.needs_human_review"]
        params: list = []
        if _is_bde(request):
            where.append("COALESCE(c.bde_name, c.bde_extension) = %s")
            params.append(_scoped_bde(request, None))
        params.append(limit)
        rows = q(
            "SELECT cl.call_id, c.bde_name, c.dest_number, c.started_at, cl.call_outcome, "
            "cl.full_pitch, cl.is_lead, cl.qualified, cl.meeting_booked, "
            "LEAST(cl.pitch_confidence, cl.lead_confidence, cl.qual_confidence) AS min_conf, "
            "cl.model "
            "FROM classifications cl JOIN calls c ON c.call_id = cl.call_id "
            f"WHERE {' AND '.join(where)} ORDER BY min_conf ASC NULLS FIRST LIMIT %s",
            tuple(params),
        )
        for r in rows:
            r["started_at"] = str(r["started_at"]) if r["started_at"] else None
            r["min_conf"] = float(r["min_conf"]) if r["min_conf"] is not None else None
        return JSONResponse({"rows": rows})

    # Stage filters mirror the STRICTLY-NESTED aggregation so drill-down counts
    # equal the funnel counts: Connected = answered + real talk + NOT a voicemail.
    _CONN = "c.answered AND c.talk_seconds >= %(thr)s AND COALESCE(cl.call_outcome, '') <> 'voicemail'"
    # A booking counts once per PROSPECT — only the FIRST booked call to a number; a later
    # booked call to the same prospect is a duplicate/re-touch (don't double-count the lead).
    _FIRST_BOOKING = ("NOT EXISTS (SELECT 1 FROM calls pc JOIN classifications pcl ON pcl.call_id=pc.call_id "
                      "WHERE pc.in_scope AND ("
                      "right(regexp_replace(pc.dest_number,'[^0-9]','','g'),9)=right(regexp_replace(c.dest_number,'[^0-9]','','g'),9) "
                      "OR (cl.company_key IS NOT NULL AND pcl.company_key IS NOT NULL AND pcl.company_key = cl.company_key)) AND pc.started_at < c.started_at "
                      "AND pc.answered AND pc.talk_seconds >= %(thr)s AND COALESCE(pcl.call_outcome,'')<>'voicemail' "
                      "AND (pcl.meeting_booked OR (pcl.booking_status='tentative' AND pcl.meeting_datetime ~* '[0-9]:[0-9]|[0-9][[:space:]]*[ap][.]?m|noon|midday')) "
                      "AND NOT COALESCE(pcl.meeting_confirmation,false) "
                      "AND NOT COALESCE(pcl.meeting_rescheduled,false) AND NOT COALESCE(pcl.booking_already_exists,false))")
    # A booking = firm OR a TENTATIVE meeting WITH a proposed date/time (rule B), honouring a BDM
    # booking-outcome override (counts/not_booking). MUST match aggregate.py's eff_booked so the
    # drill-down list equals the KPI count.
    _BO = "(SELECT qo.booking_outcome FROM qualification_overrides qo WHERE qo.call_id=c.call_id)"
    _BOOKED = (f"(CASE WHEN {_BO}='counts' THEN true WHEN {_BO}='not_booking' THEN false "
               "ELSE (cl.meeting_booked OR (cl.booking_status='tentative' AND cl.meeting_datetime ~* '[0-9]:[0-9]|[0-9][[:space:]]*[ap][.]?m|noon|midday')) END)")
    # Meeting Booked = ANY genuinely NEW booking the BDE made (no decision-maker/qualified
    # gate). EXCLUDES confirmation-only, reschedules, AND duplicate bookings of the same
    # prospect (only the first booked call per number counts). Batch D's P5 pipeline REUSES
    # this exact predicate, so P5 == Meeting Booked with zero drift.
    _MEETINGS_BOOKED = (f"{_CONN} AND {_BOOKED} "
                        "AND NOT COALESCE(cl.meeting_confirmation, false) "
                        "AND NOT COALESCE(cl.meeting_rescheduled, false) "
                        "AND NOT COALESCE(cl.booking_already_exists, false) "
                        f"AND {_FIRST_BOOKING}")
    _STAGE_COND = {
        "calls_made": "TRUE",
        "connected": _CONN,
        "unconnected": f"NOT ({_CONN})",
        "rpc_connect": f"{_CONN} AND cl.rpc_connect",
        "full_pitch": f"{_CONN} AND cl.rpc_connect AND cl.full_pitch",
        "meetings_booked": _MEETINGS_BOOKED,
        # Qualified Booked = strict subset (also qualified, BAPU). Qualification is a
        # PROSPECT-level fact: this call qualified OR any in-scope call to the same number.
        "qualified_booked": f"{_CONN} AND {_BOOKED} AND NOT COALESCE(cl.meeting_confirmation, false) "
                            f"AND NOT COALESCE(cl.meeting_rescheduled, false) AND NOT COALESCE(cl.booking_already_exists, false) AND {_FIRST_BOOKING} "
                            "AND (COALESCE((SELECT qo.qualified FROM qualification_overrides qo WHERE qo.call_id=c.call_id), cl.qualified) "
                            "OR EXISTS (SELECT 1 FROM calls c2 JOIN classifications cl2 ON cl2.call_id=c2.call_id "
                            "LEFT JOIN qualification_overrides qo2 ON qo2.call_id=c2.call_id "
                            "WHERE right(regexp_replace(c2.dest_number,'[^0-9]','','g'),9)=right(regexp_replace(c.dest_number,'[^0-9]','','g'),9) AND c2.in_scope AND COALESCE(qo2.qualified, cl2.qualified)))",
        # Booked but NOT qualified = the EXACT complement of qualified_booked within new
        # bookings, so qualified_booked + booked_unqualified = meetings_booked. (Same
        # first-booking dedup + override + prospect-level qualification, negated.)
        "booked_unqualified": f"{_CONN} AND {_BOOKED} AND NOT COALESCE(cl.meeting_confirmation, false) "
                              f"AND NOT COALESCE(cl.meeting_rescheduled, false) AND NOT COALESCE(cl.booking_already_exists, false) AND {_FIRST_BOOKING} "
                              "AND NOT COALESCE((SELECT qo.qualified FROM qualification_overrides qo WHERE qo.call_id=c.call_id), cl.qualified, false) "
                              "AND NOT EXISTS (SELECT 1 FROM calls c2 JOIN classifications cl2 ON cl2.call_id=c2.call_id "
                              "LEFT JOIN qualification_overrides qo2 ON qo2.call_id=c2.call_id "
                              "WHERE right(regexp_replace(c2.dest_number,'[^0-9]','','g'),9)=right(regexp_replace(c.dest_number,'[^0-9]','','g'),9) AND c2.in_scope AND COALESCE(qo2.qualified, cl2.qualified))",
        # Reference-only drill-downs (NOT counted in the funnel). The sidebar "Rescheduled /
        # confirmed" link uses `already_booked`; the two split views are also available.
        "already_booked": f"{_CONN} AND {_BOOKED} AND "
                          "(COALESCE(cl.meeting_confirmation, false) OR COALESCE(cl.meeting_rescheduled, false) "
                          "OR COALESCE(cl.booking_already_exists, false))",
        "meeting_confirmation": f"{_CONN} AND {_BOOKED} "
                                "AND COALESCE(cl.meeting_confirmation, false)",
        "meeting_rescheduled": f"{_CONN} AND {_BOOKED} "
                               "AND COALESCE(cl.meeting_rescheduled, false)",
        "booking_unqualified": f"{_CONN} AND {_BOOKED} "
                               "AND NOT COALESCE(cl.meeting_confirmation, false) "
                               "AND NOT COALESCE(cl.meeting_rescheduled, false) AND NOT COALESCE(cl.qualified, false)",
        "lead": f"{_CONN} AND cl.rpc_connect AND cl.is_lead",
        "qualified": f"{_CONN} AND cl.rpc_connect AND cl.is_lead AND cl.qualified",
        # Disqualified = a lead that reached a decision-maker but did NOT pass the
        # qualification gate (BANT/BAPU). The clean complement of `qualified` within
        # leads, so qualified + disqualified = leads. Honours a BDM/admin override.
        "disqualified": f"{_CONN} AND cl.rpc_connect AND cl.is_lead AND NOT COALESCE("
                        "(SELECT qo.qualified FROM qualification_overrides qo WHERE qo.call_id=c.call_id), "
                        "cl.qualified, false)",
        # Lead quality + pipeline routing (interested pipeline only carries temperature).
        "warm": f"{_CONN} AND cl.lead_temperature = 'warm'",
        "hot": f"{_CONN} AND cl.lead_temperature = 'hot'",
        "super_hot": f"{_CONN} AND cl.lead_temperature = 'super_hot'",
        "pipeline1": f"{_CONN} AND cl.pipeline = 'pipeline1_interested'",
        "pipeline2": f"{_CONN} AND cl.pipeline = 'pipeline2_existing_agency'",
        # Batch D 5-pipeline drill-downs (per-call routing bucket). p5_booked mirrors
        # meetings_booked exactly (same predicate) so the KPI and pipeline agree.
        "p1_callback": f"{_CONN} AND cl.pipeline_stage = 'p1'",
        "p2_agency": f"{_CONN} AND cl.pipeline_stage = 'p2'",
        "p3_gk_callback": f"{_CONN} AND cl.pipeline_stage = 'p3'",
        "p5_booked": _MEETINGS_BOOKED,
    }

    @app.get("/api/stage-calls")
    def stage_calls(request: Request, stage: str, date: str | None = None,
                    start: str | None = None, end: str | None = None, bde: str = "ALL",
                    track: str = "combined", limit: int = 100000) -> JSONResponse:
        """List the individual calls behind a funnel-stage count (for validation).
        Supports a single `date` or a `start`+`end` range."""
        bde = _scoped_bde(request, bde)
        start, end = _resolve_window(date, start, end)
        cond = _STAGE_COND.get(stage, "TRUE")
        where = ["c.in_scope", "c.started_at >= %(s)s::date",
                 "c.started_at < (%(e)s::date + 1)", f"({cond})"]
        params: dict = {"s": start, "e": end, "thr": settings.rpc_min_talk_seconds, "lim": limit}
        if bde and bde != "ALL":
            where.append("COALESCE(c.bde_name, c.bde_extension) = %(bde)s")
            params["bde"] = bde
        if track in ("fresh", "followup"):
            where.append("c.fresh_or_followup = %(track)s")
            params["track"] = track
        whereclause = " AND ".join(where)
        # True total (uncapped) so the drawer header shows the real count.
        total = q("SELECT count(*) AS n FROM calls c "
                  "LEFT JOIN classifications cl ON cl.call_id = c.call_id "
                  "WHERE " + whereclause, params)[0]["n"]
        rows = q(
            "SELECT c.call_id, c.bde_name, c.dest_number, c.started_at, c.talk_seconds, "
            "c.answered, c.has_transcript, cl.call_outcome, cl.rpc_connect, cl.full_pitch, "
            "cl.meeting_booked, cl.evidence->>'who_answered' AS who_answered "
            "FROM calls c LEFT JOIN classifications cl ON cl.call_id = c.call_id "
            "WHERE " + whereclause + " ORDER BY c.started_at DESC LIMIT %(lim)s",
            params,
        )
        for r in rows:
            r["started_at"] = str(r["started_at"]) if r["started_at"] else None
        return JSONResponse({"stage": stage, "count": int(total), "shown": len(rows), "rows": rows})

    @app.get("/api/rpc-actions")
    def rpc_actions_report(request: Request, bde: str = "ALL", date: str | None = None,
                           start: str | None = None, end: str | None = None) -> JSONResponse:
        """RPC Connect — Next Move accountability. Per BDE, over the window: how many
        dialled numbers still haven't reached the decision-maker, how many REQUIRED
        follow-up actions were skipped (did_required_action=false), the open next-moves,
        overdue retries, and a compliance %. Mirrors the RPC worklist scoping — a BDE
        sees only their own rows; a BDM/admin sees the team. Reads the persisted
        rpc_actions rows (status='open') the classifier/pipeline maintains."""
        scope = _scoped_bde(request, bde) if _is_bde(request) else (bde or "ALL")
        start, end = _resolve_window(date, start, end)
        team0 = {"unconnected": 0, "missed_double_tap": 0, "missed_sms_vm": 0, "sms_nv": 0,
                 "gatekeeper_unhandled": 0, "open_actions": 0, "overdue": 0, "compliance_pct": None}
        empty = {"window": {"start": start, "end": end}, "scope": scope, "team": team0, "rows": []}
        if not start:
            return JSONResponse(empty)
        params: dict = {"s": start, "e": end}
        bde_filter = ""
        if scope and scope != "ALL":
            bde_filter = "AND ra.last_bde = %(bde)s"
            params["bde"] = scope
        # A linked rpc_retry calendar event whose start time has passed (and is still
        # pending) means the scheduled retry is overdue.
        overdue_expr = ("ce.id IS NOT NULL AND ce.start_at < now() "
                        "AND COALESCE(ce.status,'pending') = 'pending'")
        # did_required_action already reflects only VERIFIABLE signals (for sms_vm it is the
        # voicemail-left check; SMS itself is never counted as a breach). So all action codes
        # count toward compliance; sms_nv is an informational "SMS unverifiable" tally only.
        req = "ra.action_code IN ('double_tap','sms_vm','gatekeeper_getdm','retry_vary_time')"
        undone = "COALESCE(ra.did_required_action,false)=false"
        try:
            rows = q(
                f"SELECT COALESCE(ra.last_bde,'—') AS bde, "
                f"count(*) AS unconnected, "
                f"count(*) FILTER (WHERE ra.action_code='double_tap' AND {undone}) AS missed_double_tap, "
                f"count(*) FILTER (WHERE ra.action_code='sms_vm' AND {undone}) AS missed_sms_vm, "
                f"count(*) FILTER (WHERE ra.action_code='sms_vm' AND ra.sms_sent IS NULL) AS sms_nv, "
                f"count(*) FILTER (WHERE ra.action_code='gatekeeper_getdm' AND {undone}) AS gatekeeper_unhandled, "
                f"count(*) FILTER (WHERE {undone} AND ra.action_code IS NOT NULL AND ra.action_code <> 'none') AS open_actions, "
                f"count(*) FILTER (WHERE ra.action_code='double_tap') AS b_double_tap, "
                f"count(*) FILTER (WHERE ra.action_code='sms_vm') AS b_sms_vm, "
                f"count(*) FILTER (WHERE ra.action_code='gatekeeper_getdm') AS b_gatekeeper_getdm, "
                f"count(*) FILTER (WHERE ra.action_code='retry_vary_time') AS b_retry_vary_time, "
                f"count(*) FILTER (WHERE {overdue_expr}) AS overdue, "
                f"count(*) FILTER (WHERE {req}) AS req_total, "
                f"count(*) FILTER (WHERE {req} AND COALESCE(ra.did_required_action,false)=true) AS req_done, "
                f"(array_remove(array_agg(NULLIF(ra.next_move,'') ORDER BY ra.last_attempt_at DESC), NULL))[1] AS next_move, "
                f"(array_remove(array_agg(ra.last_call_id ORDER BY ra.last_attempt_at DESC), NULL))[1] AS top_call_id "
                f"FROM rpc_actions ra LEFT JOIN calendar_events ce ON ce.id = ra.event_id "
                f"WHERE ra.status='open' "
                f"AND ra.last_attempt_at >= %(s)s::date AND ra.last_attempt_at < (%(e)s::date + 1) {bde_filter} "
                f"GROUP BY COALESCE(ra.last_bde,'—') "
                f"ORDER BY open_actions DESC, unconnected DESC",
                params,
            )
        except Exception:
            return JSONResponse(empty)
        out, team_req_total, team_req_done = [], 0, 0
        team = dict(team0)
        for r in rows:
            req_total = int(r.get("req_total") or 0)
            req_done = int(r.get("req_done") or 0)
            team_req_total += req_total
            team_req_done += req_done
            row = {
                "bde": r["bde"],
                "unconnected": int(r.get("unconnected") or 0),
                "missed_double_tap": int(r.get("missed_double_tap") or 0),
                "missed_sms_vm": int(r.get("missed_sms_vm") or 0),
                "sms_nv": int(r.get("sms_nv") or 0),
                "gatekeeper_unhandled": int(r.get("gatekeeper_unhandled") or 0),
                "open_actions": int(r.get("open_actions") or 0),
                "overdue": int(r.get("overdue") or 0),
                "compliance_pct": (round(100 * req_done / req_total) if req_total else None),
                "next_move": r.get("next_move"),
                "top_call_id": r.get("top_call_id"),
                "buckets": {
                    "double_tap": int(r.get("b_double_tap") or 0),
                    "sms_vm": int(r.get("b_sms_vm") or 0),
                    "gatekeeper_getdm": int(r.get("b_gatekeeper_getdm") or 0),
                    "retry_vary_time": int(r.get("b_retry_vary_time") or 0),
                },
            }
            for k in ("unconnected", "missed_double_tap", "missed_sms_vm", "sms_nv",
                      "gatekeeper_unhandled", "open_actions", "overdue"):
                team[k] += row[k]
            out.append(row)
        team["compliance_pct"] = (round(100 * team_req_done / team_req_total)
                                  if team_req_total else None)
        return JSONResponse(jsonable_encoder(
            {"window": {"start": start, "end": end}, "scope": scope, "team": team, "rows": out}))

    @app.get("/api/rpc-undone")
    def rpc_undone(request: Request) -> JSONResponse:
        """BDM/admin real-time alert feed: open RPC next-moves the BDE has NOT done in
        the last 6 hours (did_required_action=false), so a manager can be pushed a
        notification the moment an action is skipped."""
        u = getattr(request.state, "user", None) or {}
        if not (is_admin(u) or u.get("role") == "bdm"):
            raise HTTPException(403, "BDM / admin access required")
        try:
            rows = q(
                "SELECT last_call_id AS call_id, last_bde AS bde, "
                "COALESCE(NULLIF(business_name,''), dest_number) AS company, dest_number, "
                "reason, required_action, action_code "
                "FROM rpc_actions WHERE status='open' "
                "AND COALESCE(did_required_action,false)=false "
                "AND action_code IS NOT NULL AND action_code <> 'none' "
                "AND last_attempt_at >= now() - interval '6 hours' "
                "ORDER BY last_attempt_at DESC",
                None,
            )
        except Exception:
            rows = []
        return JSONResponse(jsonable_encoder(rows))

    @app.get("/api/rpc-feedback")
    def rpc_feedback(request: Request, start: str | None = None, end: str | None = None,
                     bde: str = "ALL") -> JSONResponse:
        """RPC-connect feedback intelligence: over the window, how often each BDE reaches
        the decision-maker (Right-Party-Contact) vs stops at the gatekeeper, plus gatekeeper
        handling quality and a few example calls to learn from. Read-only. Mirrors the
        connected/rpc SQL conventions (see _CONN / _STAGE_COND / /api/rpc-monitor)."""
        start, end = _resolve_window(None, start, end)
        if not start:
            return JSONResponse({"window": {"start": None, "end": None}, "scope": "ALL",
                                 "team": {}, "rows": [], "tips": [],
                                 "examples": {"reached_dm": [], "stopped_at_gatekeeper": []}})
        # A BDE only ever sees themselves; a manager may focus on one BDE (or ALL).
        focus_bde = _scoped_bde(request, bde)
        conn = _CONN  # answered + real talk + not voicemail (same as the funnel)
        # Gatekeeper reached: AI outcome or who_answered says gatekeeper (matches /api/rpc-monitor).
        hit_gk = "(cl.call_outcome = 'gatekeeper' OR cl.evidence->>'who_answered' ILIKE '%%gatekeeper%%')"
        gk_ok = "COALESCE(cl.gatekeeper_handled_well, false)"
        no_rpc = "NOT COALESCE(cl.rpc_connect, false)"
        agg = (
            f"count(*) FILTER (WHERE {conn}) AS connected, "
            f"count(*) FILTER (WHERE {conn} AND cl.rpc_connect) AS rpc, "
            f"count(*) FILTER (WHERE {conn} AND {hit_gk}) AS gatekeeper_calls, "
            f"count(*) FILTER (WHERE {conn} AND {hit_gk} AND {gk_ok}) AS gk_handled_well, "
            f"count(*) FILTER (WHERE {conn} AND {hit_gk} AND {no_rpc}) AS stopped_at_gatekeeper, "
            f"count(*) FILTER (WHERE {conn} AND {hit_gk} AND {no_rpc} AND NOT {gk_ok}) AS stopped_poorly"
        )
        where = ["c.in_scope", "c.started_at >= %(s)s::date", "c.started_at < (%(e)s::date + 1)"]
        params: dict = {"s": start, "e": end, "thr": settings.rpc_min_talk_seconds}
        # Per-BDE table: a BDE sees only their own row; a manager sees the whole team.
        bde_filter = ""
        if _is_bde(request):
            bde_filter = "AND COALESCE(c.bde_name, c.bde_extension) = %(fb)s"
            params["fb"] = focus_bde
        rows_raw = q(
            f"SELECT COALESCE(c.bde_name, c.bde_extension) AS bde, {agg} "
            "FROM calls c LEFT JOIN classifications cl ON cl.call_id = c.call_id "
            f"WHERE {' AND '.join(where)} {bde_filter} "
            f"GROUP BY 1 HAVING count(*) FILTER (WHERE {conn}) > 0 "
            "ORDER BY 1",
            params,
        )
        # Team benchmark: the whole team over the window (never bde-scoped).
        team_raw = q(
            f"SELECT {agg} FROM calls c LEFT JOIN classifications cl ON cl.call_id = c.call_id "
            f"WHERE {' AND '.join(where)}",
            params,
        )

        def _rates(r: dict) -> dict:
            connected = int(r.get("connected") or 0)
            rpc = int(r.get("rpc") or 0)
            gk = int(r.get("gatekeeper_calls") or 0)
            gk_ok_n = int(r.get("gk_handled_well") or 0)
            stopped = int(r.get("stopped_at_gatekeeper") or 0)
            return {
                "connected": connected, "rpc": rpc, "reached_dm": rpc,
                "rpc_rate": _pct(rpc, connected),
                "gatekeeper_calls": gk, "gk_handled_well": gk_ok_n,
                "gk_handled_rate": _pct(gk_ok_n, gk),
                "stopped_at_gatekeeper": stopped,
                "stopped_poorly": int(r.get("stopped_poorly") or 0),
                "stopped_pct": _pct(stopped, connected),
            }

        rows = [{"bde": r["bde"], **_rates(r)} for r in rows_raw]
        team = _rates(team_raw[0] if team_raw else {})

        # Example calls for the focused BDE (or the whole team when ALL).
        ex_where = list(where)
        ex_params = dict(params)
        if focus_bde and focus_bde != "ALL":
            ex_where.append("COALESCE(c.bde_name, c.bde_extension) = %(fb)s")
            ex_params["fb"] = focus_bde
        ex_base = "FROM calls c LEFT JOIN classifications cl ON cl.call_id = c.call_id WHERE "
        reached = q(
            "SELECT c.call_id, c.dest_number, c.started_at, "
            "COALESCE(c.bde_name, c.bde_extension) AS bde, NULLIF(cl.prospect_company,'') AS company, "
            "cl.evidence->>'who_answered' AS who_answered "
            + ex_base + " AND ".join(ex_where) + f" AND {conn} AND cl.rpc_connect "
            "ORDER BY c.started_at DESC LIMIT 5",
            ex_params,
        )
        stopped = q(
            "SELECT c.call_id, c.dest_number, c.started_at, "
            "COALESCE(c.bde_name, c.bde_extension) AS bde, NULLIF(cl.prospect_company,'') AS company, "
            "cl.evidence->>'who_answered' AS who_answered, NULLIF(cl.gatekeeper_notes,'') AS gatekeeper_notes "
            + ex_base + " AND ".join(ex_where) + f" AND {conn} AND {hit_gk} AND {no_rpc} AND NOT {gk_ok} "
            "ORDER BY c.started_at DESC LIMIT 5",
            ex_params,
        )
        for r in (*reached, *stopped):
            r["started_at"] = str(r["started_at"]) if r.get("started_at") else None

        # Actionable tips for the focused context (own row, else the team benchmark).
        fs = next((r for r in rows if r["bde"] == focus_bde), None) if focus_bde != "ALL" else None
        fs = fs or team
        who = "your" if focus_bde != "ALL" else "the team's"
        tips: list[str] = []
        if fs.get("stopped_pct") is not None and fs["stopped_pct"] >= 15:
            tips.append(f"{fs['stopped_pct']}% of {who} connected calls stopped at the gatekeeper — "
                        "ask for the owner / decision-maker by name and lock in a direct callback time.")
        if fs.get("gatekeeper_calls") and fs.get("gk_handled_rate") is not None and fs["gk_handled_rate"] < 60:
            tips.append(f"Gatekeeper handled well only {fs['gk_handled_rate']}% of the time — stay confident, "
                        "give a clear reason for the call, and request the decision-maker directly.")
        if (focus_bde != "ALL" and fs.get("rpc_rate") is not None and team.get("rpc_rate") is not None
                and fs["rpc_rate"] < team["rpc_rate"] - 5):
            tips.append(f"Your Right-Party-Contact rate ({fs['rpc_rate']}%) is below the team ({team['rpc_rate']}%) — "
                        "focus on getting past the gatekeeper to the decision-maker.")
        if not tips and fs.get("rpc_rate") is not None:
            tips.append(f"Right-Party-Contact rate {fs['rpc_rate']}% — keep asking for the decision-maker "
                        "by name to push it higher.")

        return JSONResponse(jsonable_encoder({
            "window": {"start": start, "end": end}, "scope": focus_bde,
            "team": team, "rows": rows, "tips": tips,
            "examples": {"reached_dm": reached, "stopped_at_gatekeeper": stopped},
        }))

    @app.get("/call/{call_id}", response_class=HTMLResponse)
    def call_page(call_id: str) -> str:
        return resources.files("funnel_agent.dashboard").joinpath(
            "static/call.html").read_text(encoding="utf-8")

    @app.get("/api/call/{call_id}")
    def call_detail(request: Request, call_id: str) -> JSONResponse:
        calls = q("SELECT * FROM calls WHERE call_id=%s", (call_id,))
        if not calls:
            raise HTTPException(404, "call not found")
        call = calls[0]
        if _is_bde(request):  # a BDE may only open their own calls
            own = _scoped_bde(request, None)
            if (call.get("bde_name") or call.get("bde_extension")) != own:
                raise HTTPException(403, "not your call")
        call["started_at"] = str(call["started_at"]) if call["started_at"] else None
        tr = q("SELECT text, sentiment, summary, diarized FROM transcripts WHERE call_id=%s",
               (call_id,))
        cl = q("SELECT * FROM classifications WHERE call_id=%s", (call_id,))
        # Resolve the prospect by phone → master DB (reliable), falling back to the
        # AI-extracted website. This makes the business/marketing card CONSISTENT on
        # every matched call (master SEO metrics always present) instead of only the
        # rare calls where the prospect stated their website.
        from ..prospects import match_prospect_by_phone
        master = match_prospect_by_phone(pool, call.get("dest_number"))
        domain = (cl[0].get("prospect_website") if cl else None) or (master.get("domain") if master else None)
        if not domain and call.get("dest_number"):
            # Resilient fallback: if THIS call has no website (e.g. a re-classification didn't
            # re-extract it), use the most common website the AI extracted across the prospect's
            # OTHER calls to the same number — so the domain/marketing card never blanks out.
            sib = q("SELECT cl2.prospect_website AS d, count(*) n FROM calls c2 "
                    "JOIN classifications cl2 ON cl2.call_id=c2.call_id "
                    "WHERE c2.in_scope AND cl2.prospect_website IS NOT NULL AND cl2.prospect_website<>'' "
                    "AND right(regexp_replace(c2.dest_number,'[^0-9]','','g'),9)="
                    "    right(regexp_replace(%s,'[^0-9]','','g'),9) "
                    "GROUP BY 1 ORDER BY n DESC LIMIT 1", (call.get("dest_number"),))
            if sib:
                domain = sib[0]["d"]
        enr = None
        if domain:
            rows = q("SELECT domain, semrush, apollo, website, business_intel, whois, dataforseo, status, fetched_at FROM enrichment WHERE domain=%s",
                     (domain,))
            enr = rows[0] if rows else None
        ovr = q("SELECT qualified, reason, override_by, created_at, booking_outcome FROM qualification_overrides WHERE call_id=%s",
                (call_id,))
        # Does THIS booking actually count, or is the company already booked earlier? Mirrors
        # the funnel dedup (first genuine booking per number OR company counts; later ones don't).
        booking_counts = None
        c0 = cl[0] if cl else None
        # A booking = firm OR tentative-with-a-time (rule B), matching aggregate.py + _STAGE_COND.
        _is_booked = bool(c0) and (c0.get("meeting_booked")
            or (c0.get("booking_status") == "tentative" and c0.get("meeting_datetime")))
        if _is_booked:
            if c0.get("meeting_confirmation") or c0.get("meeting_rescheduled") or c0.get("booking_already_exists"):
                booking_counts = False
            else:
                prior = q(
                    "SELECT 1 FROM calls pc JOIN classifications pcl ON pcl.call_id=pc.call_id, "
                    "(SELECT started_at, dest_number FROM calls WHERE call_id=%(cid)s) cur "
                    "WHERE pc.in_scope AND pc.call_id <> %(cid)s AND pc.started_at < cur.started_at "
                    "AND (right(regexp_replace(pc.dest_number,'[^0-9]','','g'),9)=right(regexp_replace(cur.dest_number,'[^0-9]','','g'),9) "
                    "     OR (%(ck)s::text IS NOT NULL AND pcl.company_key = %(ck)s::text)) "
                    "AND (pcl.meeting_booked OR (pcl.booking_status='tentative' AND pcl.meeting_datetime ~* '[0-9]:[0-9]|[0-9][[:space:]]*[ap][.]?m|noon|midday')) "
                    "AND NOT COALESCE(pcl.meeting_confirmation,false) "
                    "AND NOT COALESCE(pcl.meeting_rescheduled,false) AND NOT COALESCE(pcl.booking_already_exists,false) LIMIT 1",
                    {"cid": call_id, "ck": c0.get("company_key")})
                booking_counts = not prior
        # Prospect-level qualification (mirrors the funnel's qualified_booked): the booking counts
        # as Qualified Booked if THIS call is (effectively) qualified OR any in-scope call to the
        # same number/company is. Surfaced so the call page's Qualified-Booked verdict matches the
        # funnel — a booking can be qualified via an EARLIER call on the same prospect.
        prospect_qualified = None
        qualified_via = None  # {bde, date} of the earlier call that qualified the prospect
        if c0:
            eff_this = (ovr[0]["qualified"] if ovr else None)
            if eff_this is None:
                eff_this = c0.get("qualified")
            if eff_this:
                prospect_qualified = True
            else:
                sib_q = q(
                    "SELECT COALESCE(c2.bde_name, c2.bde_extension) AS bde, c2.started_at::date AS d "
                    "FROM calls c2 JOIN classifications cl2 ON cl2.call_id=c2.call_id "
                    "LEFT JOIN qualification_overrides qo2 ON qo2.call_id=c2.call_id, "
                    "(SELECT dest_number FROM calls WHERE call_id=%(cid)s) cur "
                    "WHERE c2.in_scope AND c2.call_id <> %(cid)s "
                    "AND (right(regexp_replace(c2.dest_number,'[^0-9]','','g'),9)=right(regexp_replace(cur.dest_number,'[^0-9]','','g'),9) "
                    "     OR (%(ck)s::text IS NOT NULL AND cl2.company_key = %(ck)s::text)) "
                    "AND COALESCE(qo2.qualified, cl2.qualified) ORDER BY c2.started_at LIMIT 1",
                    {"cid": call_id, "ck": c0.get("company_key")})
                prospect_qualified = bool(sib_q)
                if sib_q:
                    qualified_via = {"bde": sib_q[0]["bde"], "date": str(sib_q[0]["d"])}
        # Inbound SMS/chat from this prospect (same number) + flag any that firmed THIS booking.
        _d9 = re.sub(r"\D", "", call.get("dest_number") or "")[-9:]
        messages = q(
            "SELECT message_id, sender_phone, sender_name, body, time_sent, intent, "
            "       is_booking_confirmation, meeting_datetime, applied_call_id "
            "FROM messages WHERE dest9 = %s ORDER BY time_sent DESC LIMIT 30",
            (_d9,)) if _d9 else []
        for m in messages:
            m["time_sent"] = str(m["time_sent"]) if m.get("time_sent") else None
        sms_confirmed = any(m.get("applied_call_id") == call_id and m.get("is_booking_confirmation") for m in messages)

        # RPC Connect — Next Move: the deterministic ledger row for this number (reuse _d9), with
        # the AI-only next-move fields (evidence->'rpc_next_move') merged in where the row is null.
        # Defensive: rpc_actions may not exist yet on a deployment that hasn't run the migration.
        rpc_action = None
        if _d9:
            try:
                _ra = q("SELECT ra.*, (ce.id IS NOT NULL AND ce.start_at < now() "
                        "AND COALESCE(ce.status,'pending')='pending') AS overdue "
                        "FROM rpc_actions ra LEFT JOIN calendar_events ce ON ce.id = ra.event_id "
                        "WHERE ra.dest9=%s", (_d9,))
                rpc_action = _ra[0] if _ra else None
            except Exception:
                rpc_action = None
        _ev = (cl[0].get("evidence") if cl else None) or {}
        _ai_nm = _ev.get("rpc_next_move") if isinstance(_ev, dict) else None
        if isinstance(_ai_nm, dict) and _ai_nm:
            if rpc_action is None:
                rpc_action = dict(_ai_nm)
            else:
                for _k, _v in _ai_nm.items():
                    if rpc_action.get(_k) is None:
                        rpc_action[_k] = _v

        u = getattr(request.state, "user", None) or {}
        # jsonable_encoder converts numeric->float and datetime->iso so confidences serialize.
        return JSONResponse(jsonable_encoder({
            "call": call,
            "transcript": tr[0] if tr else None,
            "classification": cl[0] if cl else None,
            "master": master,
            "domain": domain,
            "enrichment": enr,
            "qual_override": ovr[0] if ovr else None,
            "booking_counts": booking_counts,  # None=n/a, True=counts, False=company already booked
            "prospect_qualified": prospect_qualified,  # prospect-level qualified (this OR a sibling call)
            "qualified_via": qualified_via,  # {bde,date} of the earlier call that qualified, if not this one
            "can_override": can_manage_pipeline(u),  # BDM/admin only
            "messages": messages,
            "sms_confirmed": sms_confirmed,  # booking firmed by a prospect SMS
            "rpc_action": rpc_action,  # RPC Connect — Next Move (ledger + AI next-move)
        }))

    # One cached 3CX client (reuses its OAuth token) for streaming recordings on demand.
    _tcx: dict = {}

    def _threecx():
        cli = _tcx.get("c")
        if cli is None:
            from ..threecx.api import ThreeCXClient
            cli = ThreeCXClient(settings)
            _tcx["c"] = cli
        return cli

    # One cached Aircall client for streaming Aircall recordings on demand.
    _acx: dict = {}

    def _aircall():
        cli = _acx.get("c")
        if cli is None:
            from ..aircall.api import AircallClient
            cli = AircallClient(settings)
            _acx["c"] = cli
        return cli

    @app.get("/api/call/{call_id}/recording")
    def call_recording(request: Request, call_id: str, download: int = 0):
        """Stream a call's recording audio from 3CX on demand. PLAY is available to any
        logged-in staff user; DOWNLOAD (attachment) is restricted to BDM / admin."""
        u = getattr(request.state, "user", None) or {}
        if u.get("role") == "kiosk":               # the public TV display can't pull audio
            raise HTTPException(403, "login required")
        rows = q("SELECT bde_name, bde_extension, recording_id, provider FROM calls WHERE call_id=%s", (call_id,))
        if not rows:
            raise HTTPException(404, "call not found")
        call = rows[0]
        if _is_bde(request):                        # a BDE may only hear their own calls
            own = _scoped_bde(request, None)
            if (call.get("bde_name") or call.get("bde_extension")) != own:
                raise HTTPException(403, "not your call")
        rec_id = call.get("recording_id")
        if not rec_id:
            raise HTTPException(404, "no recording for this call")
        if download and not can_manage_pipeline(u):
            raise HTTPException(403, "download is restricted to BDM / admin")
        ext, media_type = "wav", "audio/wav"
        if call.get("provider") == "aircall":       # Aircall recording (signed S3 URL)
            if not settings.aircall_enabled:
                raise HTTPException(503, "Aircall recordings unavailable: credentials not configured")
            try:
                wav = _aircall().download_recording(rec_id)
            except Exception:
                _acx.pop("c", None)
                wav = _aircall().download_recording(rec_id)
            from ..aircall.calls import sniff_audio  # Aircall serves WAV or MP3 — detect
            ext, media_type = sniff_audio(wav)
        else:
            try:
                wav = _threecx().download_recording(rec_id)
            except Exception:
                _tcx.pop("c", None)                 # token may be stale — rebuild + retry once
                wav = _threecx().download_recording(rec_id)
        safe_id = re.sub(r"[^A-Za-z0-9_.-]", "_", call_id)
        disp = "attachment" if download else "inline"
        return Response(content=wav, media_type=media_type, headers={
            "Content-Disposition": f'{disp}; filename="call_{safe_id}.{ext}"',
            "Accept-Ranges": "bytes", "Cache-Control": "private, max-age=3600",
        })

    def _reaggregate_for_call(call_id: str) -> None:
        """Re-aggregate EVERY day that has an in-scope call to the same NUMBER or COMPANY as
        `call_id`. A qualification override is a prospect/company-level fact, and the company's
        COUNTED booking may sit on a different (earlier) day than the overridden call — so we must
        recompute all related days, not just the overridden call's day, for the funnel to update."""
        from ..aggregate import aggregate_day
        days = q(
            "WITH cur AS (SELECT right(regexp_replace(c.dest_number,'[^0-9]','','g'),9) AS d9, "
            "                    cl.company_key AS ck "
            "             FROM calls c LEFT JOIN classifications cl ON cl.call_id=c.call_id "
            "             WHERE c.call_id=%(cid)s) "
            "SELECT DISTINCT c.started_at::date AS d FROM calls c "
            "LEFT JOIN classifications cl ON cl.call_id=c.call_id, cur "
            "WHERE c.in_scope AND c.started_at IS NOT NULL AND ("
            "  right(regexp_replace(c.dest_number,'[^0-9]','','g'),9)=cur.d9 "
            "  OR (cur.ck IS NOT NULL AND cl.company_key=cur.ck))",
            {"cid": call_id})
        for r in days:
            if r.get("d"):
                aggregate_day(pool, settings, r["d"])

    @app.post("/api/call/{call_id}/qualify-override")
    async def call_qualify_override(request: Request, call_id: str) -> JSONResponse:
        """#4b — BDM/admin re-qualifies a booked meeting with a MANDATORY reason. The
        funnel's Qualified Booked count then honours this over the AI verdict."""
        u = getattr(request.state, "user", None) or {}
        if not can_manage_pipeline(u):
            raise HTTPException(403, "BDM / admin access required")
        d = await _form(request)
        reason = (d.get("reason") or "").strip()
        # Optional booking-outcome override (#4c): the BDM sets what the call actually is when
        # the AI misreads tentative/firm/reschedule/confirmation. NULL = qualified-flag only.
        booking_outcome = (d.get("booking_outcome") or "").strip().lower() or None
        _ALLOWED_OUTCOMES = {"counts", "tentative", "not_booking", "rescheduled", "confirmation"}
        if booking_outcome and booking_outcome not in _ALLOWED_OUTCOMES:
            return JSONResponse({"error": "invalid booking_outcome"}, status_code=400)
        qualified = d.get("qualified")
        qualified = qualified in (True, "true", "True", "1", 1, "yes")
        if booking_outcome == "counts":
            qualified = True  # "counts as a qualified booking" implies qualified
        if not reason:
            return JSONResponse({"error": "a reason is required"}, status_code=400)
        if not q("SELECT 1 FROM calls WHERE call_id=%s", (call_id,)):
            raise HTTPException(404, "call not found")

        def _do():
            with pool.connection() as conn, conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO qualification_overrides (call_id, qualified, reason, override_by, booking_outcome, created_at) "
                    "VALUES (%s,%s,%s,%s,%s, now()) ON CONFLICT (call_id) DO UPDATE SET "
                    "qualified=EXCLUDED.qualified, reason=EXCLUDED.reason, override_by=EXCLUDED.override_by, "
                    "booking_outcome=EXCLUDED.booking_outcome, created_at=now()",
                    (call_id, qualified, reason, u.get("email") or u.get("name"), booking_outcome))
                conn.commit()
            _reaggregate_for_call(call_id)
        await run_in_threadpool(_do)
        return JSONResponse({"ok": True, "qualified": qualified, "booking_outcome": booking_outcome})

    @app.post("/api/call/{call_id}/qualify-override/clear")
    async def call_qualify_override_clear(request: Request, call_id: str) -> JSONResponse:
        """Remove a BDM override → revert to the AI verdict (re-aggregates the day)."""
        u = getattr(request.state, "user", None) or {}
        if not can_manage_pipeline(u):
            raise HTTPException(403, "BDM / admin access required")

        def _do():
            with pool.connection() as conn, conn.cursor() as cur:
                cur.execute("DELETE FROM qualification_overrides WHERE call_id=%s", (call_id,))
                conn.commit()
            _reaggregate_for_call(call_id)
        await run_in_threadpool(_do)
        return JSONResponse({"ok": True})

    # ---- consolidated prospect page (one prospect = one page) ----------- #
    # A prospect is keyed by the dialled number (all calls to it collapse onto one
    # page) or by website domain. We surface the master-DB firmographics, every
    # call by every BDE in a timeline, the conversation intelligence, and the
    # per-domain marketing enrichment — so the next BDE to call sees everything.
    from ..prospects import clean_domain as _clean_domain
    from ..prospects import normalize_au_phone as _norm_phone

    # last 9 significant digits of a dialled number, computed in SQL (matches
    # normalize_au_phone's canonical form for +61/0/bare AU numbers).
    _DEST9 = "right(regexp_replace(c.dest_number, '[^0-9]', '', 'g'), 9)"

    _PCALL_COLS = (
        "c.call_id, c.bde_name, c.bde_extension, c.dest_number, c.started_at, "
        "c.talk_seconds, c.answered, c.fresh_or_followup, c.direction, c.has_transcript, "
        "c.recording_present, c.recording_id, "
        "cl.call_outcome, cl.rpc_connect, cl.full_pitch, cl.is_lead, cl.qualified, "
        "cl.meeting_booked, cl.meeting_confirmation, cl.meeting_rescheduled, "
        "cl.budget, cl.authority, cl.problem, cl.urgency, "
        "cl.lead_temperature, cl.pipeline, cl.callback_requested, cl.callback_when, "
        "cl.prospect_company, cl.prospect_website, cl.prospect_industry, "
        "cl.prospect_contact_name, cl.prospect_mobile, cl.prospect_email, "
        "cl.runs_paid_ads, cl.has_marketing_agency, cl.problem_summary, "
        "cl.evidence, cl.model, "
        "tr.summary AS transcript_summary, tr.sentiment AS transcript_sentiment"
    )

    def _resolve_prospect(key: str):
        """Return (master_row|None, domain|None, norm9|None) for a phone/domain key."""
        key = (key or "").strip()
        # A domain always contains letters (the TLD); a dialled number never does.
        looks_domain = bool(re.search(r"[a-zA-Z]", key))
        if looks_domain:
            domain = _clean_domain(key)
            m = q("SELECT * FROM prospects WHERE domain=%s", (domain,))
            master = m[0] if m else None
            return master, (master["domain"] if master else domain), None
        norm = _norm_phone(key)
        master = None
        if norm:
            m = q("SELECT * FROM prospects WHERE %s = ANY(phones_norm) LIMIT 1", (norm,))
            master = m[0] if m else None
        return master, (master["domain"] if master else None), norm

    _whois_inflight: set = set()
    _whois_lock = threading.Lock()

    def _fire_bg_whois(domain: str) -> None:
        """Fetch + cache a domain's WHOIS in a daemon thread so it NEVER blocks the prospect
        page (a failing .au lookup can take tens of seconds). De-dupes concurrent lookups."""
        with _whois_lock:
            if domain in _whois_inflight:
                return
            _whois_inflight.add(domain)

        def run():
            try:
                from ..enrichment.whois_lookup import lookup_whois
                w = lookup_whois(domain)
                with pool.connection() as conn, conn.cursor() as cur:
                    cur.execute(
                        "INSERT INTO enrichment (domain, whois, fetched_at) VALUES (%s,%s,now()) "
                        "ON CONFLICT (domain) DO UPDATE SET whois = EXCLUDED.whois, fetched_at = now()",
                        (domain, Json(w)))
                    conn.commit()
            except Exception:
                pass
            finally:
                with _whois_lock:
                    _whois_inflight.discard(domain)
        threading.Thread(target=run, daemon=True).start()

    # Paid-ad pixels actually placed on a site are remarketing/conversion tags that are
    # only added when running campaigns; analytics tags (GA4/GTM) just measure traffic.
    _PAID_TRACKERS = {
        "google_ads": "Google Ads tag (conversion/remarketing)",
        "meta_pixel": "Meta/Facebook Pixel", "facebook": "Meta/Facebook Pixel",
        "facebook_pixel": "Meta/Facebook Pixel", "linkedin": "LinkedIn Insight Tag",
        "linkedin_insight": "LinkedIn Insight Tag", "tiktok": "TikTok Pixel",
        "bing_uet": "Microsoft/Bing UET", "twitter": "X/Twitter Pixel",
        "pinterest": "Pinterest Tag",
    }
    _ANALYTICS_TRACKERS = {"ga4": "GA4", "ua": "Universal Analytics", "gtag": "gtag.js",
                           "gtm": "Google Tag Manager", "hotjar": "Hotjar"}

    def _paid_ads_intel(enr: dict | None, calls: list) -> dict:
        """A reasoned 'is this prospect running paid ads?' verdict that COMBINES two
        independent intelligence sources — the free website tracking-code scan and the
        AI's analysis of what the prospect said on calls — and shows the reasons behind
        the verdict (not just a yes/no flag)."""
        w = ((enr or {}).get("website")) or {}
        reasons: list[dict] = []
        score = 0
        platforms: list[str] = []
        # 1) Website tracking-code signals.
        trackers = w.get("trackers") or {}
        if w.get("found"):
            seen: set[str] = set()
            for k, lab in _PAID_TRACKERS.items():
                if k in trackers and lab not in seen:
                    platforms.append(lab); seen.add(lab)
            if not platforms:
                platforms = list(w.get("paid_ad_platforms") or [])
            if platforms:
                score += 3
                for lab in platforms:
                    reasons.append({"source": "website", "sign": "pos",
                                    "text": f"{lab} is installed on the website — a remarketing/conversion pixel that's typically only added when paid campaigns are running"})
            else:
                ana = [lab for k, lab in _ANALYTICS_TRACKERS.items() if k in trackers]
                if ana:
                    reasons.append({"source": "website", "sign": "weak",
                                    "text": f"Only analytics tags found ({', '.join(ana)}) — measures traffic but is not a paid-ad pixel"})
                reasons.append({"source": "website", "sign": "neg",
                                "text": "Website scanned — no Google Ads, Meta, LinkedIn, TikTok or Bing ad pixel detected"})
                score -= 1
        else:
            reasons.append({"source": "website", "sign": "none",
                            "text": "Website not scanned yet — a free scan reads the homepage for ad pixels"})
        # 2) Transcription-analysis signals (what the prospect actually said).
        if any(c.get("runs_paid_ads") for c in calls):
            score += 2
            reasons.append({"source": "call", "sign": "pos",
                            "text": "On a call, the prospect indicated they run paid advertising"})
        if any(c.get("has_marketing_agency") for c in calls):
            score += 2
            reasons.append({"source": "call", "sign": "pos",
                            "text": "Prospect works with a marketing agency — agencies typically run paid campaigns on their behalf"})
        if any(c.get("pipeline") == "pipeline2_existing_agency" for c in calls):
            score += 1
            reasons.append({"source": "call", "sign": "pos",
                            "text": "Classified into Pipeline 2 (existing agency) from the conversation"})
        assessed = [c for c in calls if c.get("runs_paid_ads") is not None]
        if assessed and not any(c.get("runs_paid_ads") for c in calls) \
                and not any(c.get("has_marketing_agency") for c in calls):
            score -= 1
            reasons.append({"source": "call", "sign": "neg",
                            "text": "On the call(s) assessed, the prospect did not indicate any paid advertising or agency"})
        # 3) Combined verdict + confidence.
        if score >= 3:
            verdict, label, conf = "running", "Running paid ads", "high"
        elif score >= 1:
            verdict, label, conf = "likely", "Likely running paid ads", "medium"
        elif score <= -2:
            verdict, label, conf = "not", "Likely NOT running paid ads", "medium"
        elif score <= -1:
            verdict, label, conf = "not", "Probably not running paid ads", "low"
        else:
            verdict, label, conf = "unknown", "Not enough signal yet", "low"
        return {"verdict": verdict, "label": label, "confidence": conf,
                "score": score, "platforms": platforms, "reasons": reasons}

    @app.get("/prospect/{key}", response_class=HTMLResponse)
    def prospect_page(key: str) -> str:
        return _static("prospect.html")

    @app.get("/api/prospect/{key}")
    def prospect_detail(request: Request, key: str) -> JSONResponse:
        master, domain, norm = _resolve_prospect(key)
        # Gather all in-scope calls that belong to this prospect: same dialled
        # number (normalized) OR same AI-extracted website.
        conds, params = [], {"thr": settings.rpc_min_talk_seconds}
        if norm:
            conds.append(f"{_DEST9} = %(norm)s")
            params["norm"] = norm
        # When resolved via the master DB, also gather every call to ANY of the
        # prospect's known numbers (so domain-keyed access still finds phone-linked calls).
        mphones = (master or {}).get("phones_norm") or []
        if mphones:
            conds.append(f"{_DEST9} = ANY(%(mphones)s)")
            params["mphones"] = mphones
        if domain:
            conds.append("cl.prospect_website = %(domain)s")
            params["domain"] = domain
        # Given business data (companies) for this prospect — fetched early so a DB-browser
        # click opens the page even when there are no calls yet (pure reference data).
        companies: list = []
        if domain:
            companies = q("SELECT * FROM companies WHERE domain=%s ORDER BY revenue_musd DESC NULLS LAST", (domain,))
        elif norm:
            companies = q("SELECT * FROM companies WHERE phone_norm=%s ORDER BY revenue_musd DESC NULLS LAST", (norm,))

        if not conds:
            return JSONResponse({"found": bool(companies), "companies": jsonable_encoder(companies)})
        where = "c.in_scope AND (" + " OR ".join(conds) + ")"
        calls = q(
            f"SELECT {_PCALL_COLS} FROM calls c "
            "LEFT JOIN classifications cl ON cl.call_id=c.call_id "
            "LEFT JOIN transcripts tr ON tr.call_id=c.call_id "
            f"WHERE {where} ORDER BY c.started_at ASC",
            params,
        )
        if not calls and not master and not companies:
            return JSONResponse({"found": False})

        # BDE scoping: a BDE may only open a prospect they've actually called
        # (then they DO see every BDE's calls to it — the rotation hand-off).
        if _is_bde(request):
            own = _scoped_bde(request, None)
            if not any((c.get("bde_name") or c.get("bde_extension")) == own for c in calls):
                raise HTTPException(403, "not your prospect")

        # Best domain for enrichment: master's, else the most common extracted site.
        if not domain:
            sites = [c["prospect_website"] for c in calls if c.get("prospect_website")]
            domain = max(set(sites), key=sites.count) if sites else None
        enr = None
        if domain:
            rows = q("SELECT domain, semrush, apollo, website, business_intel, whois, dataforseo, status, fetched_at FROM enrichment WHERE domain=%s",
                     (domain,))
            enr = rows[0] if rows else None

        # Lazy on-demand WHOIS. auDA throttles bulk .au and a FAILING lookup can take tens of
        # seconds (port-43 timeout + RDAP 429 backoff), so it must NEVER run synchronously on
        # the request path. Fire it in the BACKGROUND (non-blocking) and let it appear on the
        # next page load. Retry not-found/absent WHOIS too (single lookups succeed even when a
        # bulk run was throttled), but throttle retries to ~once/day via the row's fetched_at
        # so a permanently-failing domain isn't hammered on every open. Self-heals over time.
        _ew = enr.get("whois") if enr else None
        if domain and not (isinstance(_ew, dict) and _ew.get("found") is True):
            _fa = enr.get("fetched_at") if enr else None
            stale = True
            if _ew is not None and _fa is not None:
                try:
                    fa = _fa if isinstance(_fa, _dt) else _dt.fromisoformat(str(_fa))
                    if fa.tzinfo is None:
                        fa = fa.replace(tzinfo=ZoneInfo("UTC"))
                    stale = (_dt.now(ZoneInfo("UTC")) - fa).total_seconds() > 86400
                except Exception:
                    stale = True
            if stale:
                _fire_bg_whois(domain)

        # If the domain was only resolved from the calls above (phone access), fetch the
        # given business data now (companies were not found by phone earlier).
        if not companies and domain:
            companies = q("SELECT * FROM companies WHERE domain=%s ORDER BY revenue_musd DESC NULLS LAST", (domain,))
        company_summary = None
        if companies:
            revs = [float(c["revenue_musd"]) for c in companies if c.get("revenue_musd") is not None]
            company_summary = {
                "businesses": len(companies),
                "total_revenue_musd": round(sum(revs), 2) if revs else None,
                "industry": next((c.get("industry") for c in companies if c.get("industry")), None),
                "employees": max((c.get("employees") or 0) for c in companies) or None,
                "location": next((", ".join(x for x in (c.get("suburb"), c.get("state")) if x)
                                  for c in companies if c.get("suburb") or c.get("state")), None),
            }

        # Prospect-level rollup across all calls.
        def _truthy(c, k):
            return bool(c.get(k))
        temps = [c.get("lead_temperature") for c in calls if c.get("lead_temperature") in ("warm", "hot", "super_hot")]
        temp_rank = {"warm": 1, "hot": 2, "super_hot": 3}
        best_temp = max(temps, key=lambda t: temp_rank.get(t, 0)) if temps else None
        pipelines = [c.get("pipeline") for c in calls if c.get("pipeline") in ("pipeline1_interested", "pipeline2_existing_agency")]
        # latest pipeline wins (calls are ascending) else master's
        pipeline = pipelines[-1] if pipelines else (master.get("pipeline") if master else None)
        bdes = []
        for c in calls:
            nm = c.get("bde_name") or c.get("bde_extension")
            if nm and nm not in bdes:
                bdes.append(nm)
        rollup = {
            "calls": len(calls),
            "bdes": bdes,
            "first_call": str(calls[0]["started_at"]) if calls else None,
            "last_call": str(calls[-1]["started_at"]) if calls else None,
            "ever_booked": any(_truthy(c, "meeting_booked") and not c.get("meeting_confirmation") for c in calls),
            "ever_qualified": any(_truthy(c, "qualified") for c in calls),
            "ever_rpc": any(_truthy(c, "rpc_connect") for c in calls),
            "temperature": best_temp,
            "pipeline": pipeline,
            "callback_requested": any(_truthy(c, "callback_requested") for c in calls),
        }
        for c in calls:
            c["started_at"] = str(c["started_at"]) if c.get("started_at") else None

        # Inbound SMS/chat for this prospect (any of its numbers) — booking confirmations etc.
        d9s = set()
        if norm:
            d9s.add(norm)
        for p in (master or {}).get("phones_norm") or []:
            d9s.add(p)
        for c in calls:
            dn = re.sub(r"\D", "", c.get("dest_number") or "")
            if dn:
                d9s.add(dn[-9:])
        messages = q(
            "SELECT message_id, sender_phone, sender_name, body, time_sent, intent, "
            "       is_booking_confirmation, meeting_datetime, applied_call_id "
            "FROM messages WHERE dest9 = ANY(%s) ORDER BY time_sent DESC LIMIT 50",
            (list(d9s),)) if d9s else []
        for m in messages:
            m["time_sent"] = str(m["time_sent"]) if m.get("time_sent") else None

        # Smart next-call priority for this prospect (A) — defensive: table may not exist yet.
        priority = None
        try:
            pr_id = (master or {}).get("id")
            if pr_id:
                pr = q("SELECT prospect_id, score, tier, reason, next_best_time, assigned_bde, "
                       "override_by, source_signal FROM next_call_queue WHERE prospect_id=%s", (pr_id,))
            else:
                pr = q("SELECT prospect_id, score, tier, reason, next_best_time, assigned_bde, "
                       "override_by, source_signal FROM next_call_queue "
                       "WHERE (domain=%s AND %s<>'') OR dest9 = ANY(%s) ORDER BY score DESC LIMIT 1",
                       (domain, domain or "", list(d9s) if d9s else [])) if (domain or d9s) else []
            priority = pr[0] if pr else None
        except Exception:
            priority = None

        # Next scheduled action + Do-Not-Contact status — consolidate the three sources so EVERY
        # prospect page shows the next call (or clearly says why there isn't one). Sources, in
        # order of authority: a real dated calendar appointment (calendar_events) > the P2 agency
        # rotation board (prospect_pipeline, weekly cadence) > the priority queue (next_best_time).
        next_action = None
        dnd = None
        try:
            d9list = list(d9s) if d9s else []
            board = None
            if d9list:
                bd = q("SELECT pipeline, assigned_bde, next_action_at, cadence_days, attempts, "
                       "contract_end, dnd, dnd_reason, dnd_at FROM prospect_pipeline "
                       "WHERE dest9 = ANY(%s) ORDER BY updated_at DESC NULLS LAST LIMIT 1", (d9list,))
                board = bd[0] if bd else None
            if board and board.get("dnd"):
                dnd = {"reason": board.get("dnd_reason"), "at": board.get("dnd_at")}
            cal = None
            if d9list and not dnd:
                # An agency prospect's next call is owned by the contract-aware rotation, so ignore
                # a stray rpc_retry double-tap for them; otherwise take the soonest real appointment.
                skip_retry = bool(board and board.get("pipeline") == "pipeline2_existing_agency")
                # Pick the soonest UPCOMING event (never a past/stale one — a prospect can have
                # several piled-up pending events); only if none are upcoming fall back to the most
                # recent past one (an overdue to-do). Never show a 'next call' before today.
                _now = _dt.now(_tz.utc)
                ev = q("SELECT type, start_at, bde_name, status FROM calendar_events "
                       "WHERE right(regexp_replace(COALESCE(dest_number,''),'[^0-9]','','g'),9) = ANY(%(d9)s) "
                       "  AND status='pending' AND (NOT %(sr)s OR type <> 'rpc_retry') "
                       "ORDER BY (start_at >= %(now)s) DESC, "
                       "         CASE WHEN start_at >= %(now)s THEN start_at END ASC, start_at DESC "
                       "LIMIT 1", {"d9": d9list, "sr": skip_retry, "now": _now})
                cal = ev[0] if ev else None
            # A far-future agency next-action = 'parked' (e.g. locked into a renewed contract):
            # show 'Re-engage ~<date> · <why>', not a normal 'call this week'.
            horizon = _dt.now(_tz.utc) + timedelta(days=45)
            if dnd:
                next_action = None
            elif cal:
                next_action = {"when": cal["start_at"], "bde": cal.get("bde_name"),
                               "source": "calendar", "type": cal.get("type")}
            elif board and board.get("next_action_at"):
                na = board["next_action_at"]
                parked = bool(na and na > horizon)
                next_action = {"when": na, "bde": board.get("assigned_bde"),
                               "source": "pipeline2", "type": "agency_cadence",
                               "attempts": board.get("attempts"), "parked": parked,
                               "reason": board.get("contract_end")}
            elif priority and priority.get("next_best_time"):
                next_action = {"when": priority["next_best_time"], "bde": priority.get("assigned_bde"),
                               "source": "queue", "type": "priority"}
        except Exception:
            next_action = None
            dnd = None

        # Batch D 5-pipeline membership for this prospect — so the hero shows P4 · Fresh for an
        # uncalled, Google-ads-confirmed domain (like ourxplor.com) instead of "No pipeline".
        # Resolves p5>p2>p1>p3 by domain company_key OR any dialled number; else P4 (fresh/attempted).
        pipeline5 = None
        try:
            prow = q(
                "SELECT bool_or(cl.pipeline_stage='p5') p5, "
                "bool_or(cl.pipeline='pipeline2_existing_agency') p2, "
                "bool_or(cl.pipeline_stage='p1') p1, bool_or(cl.pipeline_stage='p3') p3, count(*)>0 called "
                "FROM classifications cl JOIN calls c ON c.call_id=cl.call_id "
                "WHERE c.in_scope AND ((%(dom)s<>'' AND cl.company_key='dom:'||%(dom)s) "
                "  OR right(regexp_replace(COALESCE(c.dest_number,''),'[^0-9]','','g'),9) = ANY(%(d9)s))",
                {"dom": domain or "", "d9": list(d9s) if d9s else []})
            r0 = prow[0] if prow else {}
            pl = ("p5" if r0.get("p5") else "p2" if r0.get("p2") else "p1" if r0.get("p1")
                  else "p3" if r0.get("p3") else "p4")
            sub = None
            if pl == "p4":
                # running_google_ads is stored as a JSON boolean (true), not a string — so
                # str().lower() handles both bool True and the legacy string "true".
                _ra = (enr.get("dataforseo") or {}).get("running_google_ads") if enr else None
                runs = str(_ra).strip().lower() == "true"
                sub = "attempted" if r0.get("called") else ("fresh_ads" if runs else "fresh_unscanned")
            pipeline5 = {"pipeline": pl, "p4_sub": sub}
        except Exception:
            pipeline5 = None

        return JSONResponse(jsonable_encoder({
            "found": True,
            "key": key,
            "domain": domain,
            "master": master,
            "enrichment": enr,
            "companies": companies,
            "company_summary": company_summary,
            "rollup": rollup,
            "paid_ads": _paid_ads_intel(enr, calls),
            "priority": priority,
            "next_action": next_action,
            "dnd": dnd,
            "pipeline5": pipeline5,
            "can_download": can_manage_pipeline(getattr(request.state, "user", None) or {}),
            "calls": calls,
            "messages": messages,
        }))

    @app.post("/api/prospect/{key}/add-phone")
    async def prospect_add_phone(request: Request, key: str) -> JSONResponse:
        """Any signed-in user (not kiosk) can add a phone to a prospect missing one."""
        u = getattr(request.state, "user", None) or {}
        if u.get("role") == "kiosk":
            raise HTTPException(403, "read-only")
        d = await _form(request)
        phone = (d.get("phone") or "").strip()
        norm = _norm_phone(phone)
        if not norm:
            return JSONResponse({"error": "enter a valid phone number"}, status_code=400)
        master, domain, _ = _resolve_prospect(key)
        if not master:
            return JSONResponse({"error": "no master prospect to attach this to"}, status_code=404)

        def _do():
            import json
            entry = {"name": d.get("name") or None, "title": d.get("title") or None,
                     "mobile": phone, "phone": None,
                     "added_by": u.get("email"), "added_norm": norm}
            with pool.connection() as conn, conn.cursor() as cur:
                cur.execute(
                    "UPDATE prospects SET "
                    "  phones_norm = (SELECT array_agg(DISTINCT x) FROM unnest(phones_norm || %s::text[]) x), "
                    "  extra_contacts = COALESCE(extra_contacts,'[]'::jsonb) || %s::jsonb, "
                    "  updated_at = now() "
                    "WHERE id = %s",
                    ([norm], json.dumps([entry]), master["id"]),
                )
                conn.commit()
        await run_in_threadpool(_do)
        return JSONResponse({"ok": True, "normalized": norm})

    @app.post("/api/prospect/{key}/enrich-website")
    async def prospect_enrich_website(request: Request, key: str) -> JSONResponse:
        """FREE 'Enrich now' — fill every FREE prospect tab in one go: website intelligence
        (tracking pixels / paid-ads activity / emails & socials), SEMrush metrics, Apollo
        company + decision-makers, website business intel, and Domain/WHOIS. No paid API
        (DataForSEO is the separate BDM-only paid button). Keyed by domain."""
        u = getattr(request.state, "user", None) or {}
        if u.get("role") == "kiosk":
            raise HTTPException(403, "read-only")
        _master, domain, _norm = _resolve_prospect(key)
        if not domain:
            return JSONResponse({"error": "no website on file for this prospect"}, status_code=400)

        def _do() -> dict:
            from ..enrich import enrich_domain_full
            res = enrich_domain_full(pool, settings, domain, with_dataforseo=False, force=True)
            with pool.connection() as conn, conn.cursor() as cur:
                cur.execute("SELECT website FROM enrichment WHERE domain=%s", (domain,))
                row = cur.fetchone() or {}
            return {"did": res.get("did"), "website": row.get("website")}
        out = await run_in_threadpool(_do)
        return JSONResponse(jsonable_encoder({"ok": True, "domain": domain, **out}))

    @app.post("/api/prospect/{key}/enrich-dataforseo")
    async def prospect_enrich_dataforseo(request: Request, key: str) -> JSONResponse:
        """PAID on-demand: DataForSEO SEO metrics + Google Ads Transparency Center for this
        prospect's domain. Restricted to BDM/admin (each run costs ~$0.012)."""
        u = getattr(request.state, "user", None) or {}
        if not can_manage_pipeline(u):
            raise HTTPException(403, "DataForSEO is paid — restricted to BDM / admin")
        if not settings.dataforseo_enabled:
            raise HTTPException(503, "DataForSEO not configured")
        _master, domain, _norm = _resolve_prospect(key)
        if not domain:
            return JSONResponse({"error": "no website on file for this prospect"}, status_code=400)

        def _do() -> dict:
            from ..enrich import enrich_dataforseo_one
            from ..enrichment.dataforseo import DataForSEOClient
            c = DataForSEOClient(settings)
            try:
                return enrich_dataforseo_one(pool, c, domain)
            finally:
                c.close()
        data = await run_in_threadpool(_do)
        return JSONResponse(jsonable_encoder({"ok": True, "domain": domain, "dataforseo": data}))

    @app.post("/api/prospect/{key}/seo-audit")
    async def prospect_seo_audit(request: Request, key: str) -> JSONResponse:
        """PAID on-demand: fetch the domain's ranked KEYWORDS (DataForSEO) and build a
        quick-wins -> growth SEO audit. Traffic is ESTIMATED from position x search volume (a
        labelled assumption), not bought. BDM / admin / manager only (each run costs a few cents)."""
        u = getattr(request.state, "user", None) or {}
        if not can_manage_pipeline(u):
            raise HTTPException(403, "SEO audit is paid — restricted to BDM / admin / manager")
        if not settings.dataforseo_enabled:
            raise HTTPException(503, "DataForSEO not configured")
        _master, domain, _norm = _resolve_prospect(key)
        if not domain:
            return JSONResponse({"error": "no website on file for this prospect"}, status_code=400)

        def _do() -> dict:
            from ..enrichment.dataforseo import DataForSEOClient, build_seo_audit, brand_tokens
            c = DataForSEOClient(settings)
            try:
                rk = c.ranked_keywords(domain, limit=100)   # 100 (was 200): cost scales with rows
            finally:
                c.close()
            kws = (rk.get("keywords") or [])[:100]
            brands = brand_tokens(domain, (_master or {}).get("company_name")
                                  or (_master or {}).get("name") or "")
            audit = build_seo_audit(kws, brands=brands)
            audit["keyword_count_total"] = rk.get("count")
            audit["fetched_at"] = _dt.now().isoformat()  # audit's own run time
            # Cache the audit AND the raw ranked keywords (ranked_kw) so the competitor audit can
            # reuse them WITHOUT a second (paid) ranked_keywords fetch. Merge via jsonb || so
            # ads/rank/running_google_ads siblings are kept.
            patch = {"audit": audit, "ranked_kw": kws}
            with pool.connection() as conn, conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO enrichment (domain, dataforseo, fetched_at) VALUES (%s,%s,now()) "
                    "ON CONFLICT (domain) DO UPDATE SET "
                    "dataforseo = COALESCE(enrichment.dataforseo,'{}'::jsonb) || %s::jsonb, fetched_at=now()",
                    (domain, Json(patch), Json(patch)))
                conn.commit()
            return audit
        try:
            audit = await run_in_threadpool(_do)
        except Exception as exc:
            raise HTTPException(502, f"SEO audit unavailable — DataForSEO error: {str(exc)[:120]}")
        return JSONResponse(jsonable_encoder({"ok": True, "domain": domain, "audit": audit}))

    @app.post("/api/prospect/{key}/competitor-audit")
    async def prospect_competitor_audit(request: Request, key: str) -> JSONResponse:
        """PAID on-demand (K): top-5 competitor gap audit — paid + organic competitors, keyword /
        content / backlink gap, GEO/AEO readiness, quick-wins -> growth. Cached in enrichment
        jsonb (re-open is free). BDM / admin / manager only; capped at ~6 DataForSEO calls."""
        u = getattr(request.state, "user", None) or {}
        if not can_manage_pipeline(u):
            raise HTTPException(403, "Competitor audit is paid — restricted to BDM / admin / manager")
        if not settings.dataforseo_enabled:
            raise HTTPException(503, "DataForSEO not configured")
        _master, domain, _norm = _resolve_prospect(key)
        if not domain:
            return JSONResponse({"error": "no website on file for this prospect"}, status_code=400)
        force = (await _form(request)).get("force") in ("1", "true", "on")
        from ..competitor import run_competitor_audit
        try:
            audit = await run_in_threadpool(lambda: run_competitor_audit(pool, settings, domain, force=force))
        except Exception as exc:
            raise HTTPException(502, f"Competitor audit unavailable — {str(exc)[:120]}")
        return JSONResponse(jsonable_encoder({"ok": True, "domain": domain, "audit": audit}))

    # ---- Smart next-call priority queue (A) ------------------------------ #
    @app.get("/api/next-calls")
    def next_calls(request: Request, bde: str = "ALL", due_only: bool = False,
                   tier: str = "", limit: int = 200) -> JSONResponse:
        """The ranked next-call queue (intent x attention x revenue). BDEs see only their own."""
        scope = _scoped_bde(request, bde) if _is_bde(request) else (bde or "ALL")
        from ..next_call import list_next_calls
        rows = list_next_calls(pool, bde=(None if scope == "ALL" else scope),
                               due_only=bool(due_only), tier=(tier or None), limit=min(500, max(1, limit)))
        u = getattr(request.state, "user", None) or {}
        roster = [r["name"] for r in q("SELECT DISTINCT COALESCE(bde_name, extension) AS name "
                                       "FROM bde_agents WHERE in_scope AND active ORDER BY 1")]
        return JSONResponse(jsonable_encoder({"rows": rows, "can_assign": can_manage_pipeline(u), "roster": roster}))

    @app.post("/api/next-calls/sync")
    async def next_calls_sync(request: Request) -> JSONResponse:
        u = getattr(request.state, "user", None) or {}
        if not can_manage_pipeline(u):
            raise HTTPException(403, "manager only")
        from ..next_call import sync_next_call_scores
        stats = await run_in_threadpool(lambda: sync_next_call_scores(pool, settings, min_interval_minutes=0))
        return JSONResponse(jsonable_encoder({"ok": True, "stats": stats}))

    @app.post("/api/next-calls/{key}/reassign")
    async def next_calls_reassign(request: Request, key: str) -> JSONResponse:
        u = getattr(request.state, "user", None) or {}
        if not can_manage_pipeline(u):
            raise HTTPException(403, "manager only")
        d = await _form(request)
        bde = (d.get("bde") or "").strip()
        if not bde:
            return JSONResponse({"error": "bde required"}, status_code=400)
        from ..next_call import reassign_next_call
        ok = await run_in_threadpool(lambda: reassign_next_call(pool, int(key), bde,
                                     by=(u.get("email") or "manager"), notes=d.get("notes")))
        return JSONResponse({"ok": bool(ok)})

    @app.post("/api/next-calls/{key}/schedule")
    async def next_calls_schedule(request: Request, key: str) -> JSONResponse:
        d = await _form(request)
        try:
            start_at = _dt.fromisoformat((d.get("start_at") or "").replace("Z", "+00:00"))
        except Exception:
            return JSONResponse({"error": "invalid start_at (ISO datetime required)"}, status_code=400)
        from ..next_call import set_next_best_time
        eid = await run_in_threadpool(lambda: set_next_best_time(pool, int(key), start_at))
        return JSONResponse({"ok": True, "event_id": eid})

    @app.post("/api/next-calls/{key}/done")
    async def next_calls_done(request: Request, key: str) -> JSONResponse:
        from ..next_call import mark_done
        ok = await run_in_threadpool(lambda: mark_done(pool, int(key)))
        return JSONResponse({"ok": bool(ok)})

    @app.post("/api/prospect/{key}/set-website")
    async def prospect_set_website(request: Request, key: str) -> JSONResponse:
        """Set a prospect's website (any signed-in user) → immediately enrich it.
        This is how the ~1,000 called-but-no-website prospects get marketing data."""
        u = getattr(request.state, "user", None) or {}
        if u.get("role") == "kiosk":
            raise HTTPException(403, "read-only")
        d = await _form(request)
        website = _clean_domain(d.get("website"))
        name = (d.get("business_name") or "").strip() or None
        if not website or "." not in website:
            return JSONResponse({"error": "enter a valid website, e.g. acme.com.au"}, status_code=400)
        master, _dom, norm = _resolve_prospect(key)

        def _do() -> dict:
            with pool.connection() as conn, conn.cursor() as cur:
                cur.execute("SELECT id FROM prospects WHERE domain=%s", (website,))
                existing = cur.fetchone()
                if master and existing and existing["id"] != master["id"]:
                    # this website already belongs to another prospect → attach the
                    # phone there and remove the now-redundant phone-only capture row.
                    if norm:
                        cur.execute(
                            "UPDATE prospects SET phones_norm = "
                            "(SELECT array_agg(DISTINCT x) FROM unnest(phones_norm || %s::text[]) x), "
                            "updated_at=now() WHERE id=%s", ([norm], existing["id"]))
                    if master.get("source") == "call_capture":
                        cur.execute("DELETE FROM prospects WHERE id=%s", (master["id"],))
                elif master:
                    cur.execute(
                        "UPDATE prospects SET domain=%s, business_name=COALESCE(business_name,%s), "
                        "updated_at=now() WHERE id=%s", (website, name, master["id"]))
                elif existing:
                    if norm:
                        cur.execute(
                            "UPDATE prospects SET phones_norm = "
                            "(SELECT array_agg(DISTINCT x) FROM unnest(phones_norm || %s::text[]) x), "
                            "updated_at=now() WHERE id=%s", ([norm], existing["id"]))
                else:
                    cur.execute(
                        "INSERT INTO prospects (domain, business_name, phones_norm, source, updated_at) "
                        "VALUES (%s,%s,%s,'manual',now())",
                        (website, name, [norm] if norm else []))
                conn.commit()
            return {}

        await run_in_threadpool(_do)
        from ..enrich import enrich_domain_full
        res = await run_in_threadpool(enrich_domain_full, pool, settings, website,
                                      with_dataforseo=False)
        return JSONResponse({"ok": True, "domain": website, "enriched": bool(res.get("ok"))})

    # ---- Master prospect database browser (admin + BDM only, realtime) -- #
    def _require_db_access(request: Request) -> dict:
        u = getattr(request.state, "user", None) or {}
        if not (is_admin(u) or u.get("role") == "bdm"):
            raise HTTPException(status_code=403, detail="admin / BDM access required")
        return u

    @app.get("/database", response_class=HTMLResponse)
    def database_page(request: Request):
        u = getattr(request.state, "user", None) or {}
        if not (is_admin(u) or u.get("role") == "bdm"):
            return RedirectResponse("/", status_code=302)
        return _static("database.html")

    # coverage facet -> `ge` boolean column. Multiple selected facets combine with AND.
    _COV = {"scanned": "ge.scanned", "gate": "ge.gate_pass", "apollo": "ge.has_apollo",
            "intel": "ge.has_intel", "whois": "ge.has_whois", "dataforseo": "ge.has_dataforseo",
            "transparency": "ge.dfs_checked"}

    # tracking-tag type -> `ge` boolean column (supportive signal). Multiple combine with OR.
    _TRACK = {"google_ads": "ge.has_google_ads_tag", "meta_pixel": "ge.has_meta_pixel",
              "gtm": "ge.has_gtm", "ga4": "ge.has_ga4", "bing": "ge.has_bing",
              "tiktok": "ge.has_tiktok", "linkedin": "ge.has_linkedin"}

    def _coverage_conds(coverage: str) -> list:
        """Parse a comma-separated coverage list into `ge` conditions (deduped, AND-ed)."""
        out = []
        for cov in (coverage or "").split(","):
            cond = _COV.get(cov.strip())
            if cond and cond not in out:
                out.append(cond)
        return out

    # Revenue range (per-domain SUMMED revenue, millions USD) -> `ge` condition. Whitelist
    # of preset bands; the strings are fixed (no user input inlined) so they're injection-safe.
    _REV = {"lt1": "ge.total_revenue < 1",
            "1-10": "ge.total_revenue >= 1 AND ge.total_revenue < 10",
            "10-50": "ge.total_revenue >= 10 AND ge.total_revenue < 50",
            "50-100": "ge.total_revenue >= 50 AND ge.total_revenue < 100",
            "100up": "ge.total_revenue >= 100"}  # avoid '+' (URL-decodes to space)

    def _db_cte(search: str, source: str, enriched: str, paid_ads: str, revenue: str = "",
                tracking: str = "", transparency: str = "", industry: str = "", state: str = "",
                website: str = "", multilocation: str = "", agency: str = "",
                pipeline: str = "", p4sub: str = "", rev_min: str = "", rev_max: str = ""):
        """Shared companies+enrichment CTE for the Database browser. Returns
        (cte_sql, params, base_conds) for the NON-coverage filters: search/source bake
        into the CTE; the rest come back as `ge` conditions. Coverage filters are added by
        the caller, so /stats can facet independent of them."""
        term = search.strip().lower()
        digits = re.sub(r"[^0-9]", "", search)
        params: dict = {}
        sd = sn = ""  # search filters for the domain / no-domain branches
        if term:
            params["like"] = f"%{term}%"
            sd = " AND (lower(co.company_name) LIKE %(like)s OR lower(co.domain) LIKE %(like)s"
            sn = " AND (lower(co.company_name) LIKE %(like)s"
            if digits:
                params["qd"] = f"%{digits}%"
                sd += " OR co.phone_norm LIKE %(qd)s"
                sn += " OR co.phone_norm LIKE %(qd)s"
            sd += ")"; sn += ")"
        src = ""
        if source in ("raghav", "raven", "3cx_calls"):
            src = " AND co.source = %(src)s"
            params["src"] = source
        conds = []
        if enriched == "yes":
            conds.append("ge.enriched")
        elif enriched == "no":
            conds.append("NOT ge.enriched")
        # Master "runs paid ads": Google = Transparency Center (PRIMARY); Meta = pixel
        # (supportive — no Meta ad-library source). Legacy yes/no map to either/none.
        if paid_ads == "google":
            conds.append("ge.runs_google_ads")
        elif paid_ads == "meta":
            conds.append("ge.has_meta_pixel")
        elif paid_ads == "both":
            conds.append("ge.runs_google_ads AND ge.has_meta_pixel")
        elif paid_ads in ("either", "yes"):
            conds.append("(ge.runs_google_ads OR ge.has_meta_pixel)")
        elif paid_ads in ("none", "no"):
            conds.append("ge.scanned AND NOT ge.runs_google_ads AND NOT ge.has_meta_pixel")
        elif paid_ads == "unscanned":
            conds.append("NOT ge.scanned")
        # Tracking-tag TYPE (supportive): OR of the selected tags present.
        tsel = [_TRACK[t.strip()] for t in (tracking or "").split(",") if t.strip() in _TRACK]
        if tsel:
            conds.append("(" + " OR ".join(dict.fromkeys(tsel)) + ")")
        # Transparency-check state.
        if transparency == "checked":
            conds.append("ge.dfs_checked")
        elif transparency == "running":
            conds.append("ge.runs_google_ads")
        elif transparency == "not_running":
            conds.append("ge.dfs_checked AND NOT ge.runs_google_ads")
        if industry.strip():
            conds.append("ge.industry ILIKE %(ind)s")
            params["ind"] = f"%{industry.strip()}%"
        if state.strip():
            conds.append("ge.location ILIKE %(st)s")
            params["st"] = f"%{state.strip()}%"
        if revenue in _REV:
            conds.append(_REV[revenue])
        # Custom revenue range (in $M USD) — min and/or max, combines with any bucket above.
        for _rk, _op, _pk in (("rev_min", ">=", "rmin"), ("rev_max", "<=", "rmax")):
            _rv = {"rev_min": rev_min, "rev_max": rev_max}[_rk]
            if str(_rv).strip() != "":
                try:
                    params[_pk] = float(_rv)
                    conds.append(f"ge.total_revenue {_op} %({_pk})s")
                except (ValueError, TypeError):
                    pass
        # Has-a-website filter: kind='domain' means the company has a domain on file.
        if website == "yes":
            conds.append("ge.kind = 'domain'")
        elif website == "no":
            conds.append("ge.kind = 'nodomain'")
        # Multi-location: >1 business record under the domain, or a branches count on file.
        if multilocation == "yes":
            conds.append("ge.multiloc")
        elif multilocation == "no":
            conds.append("NOT ge.multiloc")
        # Already working with an agency (from a BDE call classified pipeline2).
        if agency == "yes":
            conds.append("ge.has_agency")
        elif agency == "no":
            conds.append("NOT ge.has_agency")
        # Batch D: 5-pipeline membership + P4 sub-pipeline (both derived on `ge`, whitelisted).
        if pipeline in ("p1", "p2", "p3", "p4", "p5"):
            conds.append("ge.pipeline = %(pl)s")
            params["pl"] = pipeline
        if p4sub in ("fresh_ads", "fresh_unscanned", "captured_3cx", "captured_aircall", "attempted", "dead"):
            conds.append("ge.p4_sub = %(p4sub)s")
            params["p4sub"] = p4sub
        cte = f"""
        WITH g AS (
          SELECT co.domain AS domain, 'domain' AS kind, count(*) AS businesses,
                 sum(co.revenue_musd) AS total_revenue, max(co.employees) AS employees,
                 (array_agg(co.company_name ORDER BY co.revenue_musd DESC NULLS LAST))[1] AS name,
                 (array_agg(co.industry ORDER BY co.revenue_musd DESC NULLS LAST))[1] AS industry,
                 (array_agg(co.sub_industry ORDER BY co.revenue_musd DESC NULLS LAST))[1] AS sub_industry,
                 (array_agg(NULLIF(concat_ws(', ', co.suburb, co.state), '') ORDER BY co.revenue_musd DESC NULLS LAST))[1] AS location,
                 (array_remove(array_agg(co.phone_norm ORDER BY co.revenue_musd DESC NULLS LAST), NULL))[1] AS phone,
                 sum(jsonb_array_length(COALESCE(co.contacts, '[]'::jsonb))) AS contacts,
                 array_to_string(array_agg(DISTINCT co.source), ',') AS sources,
                 -- Multi-location = a branch count on file OR >1 business record under the SAME
                 -- domain (franchise/chain). The count>1 arm excludes generic/shared domains
                 -- (social, free hosts, gov portals, CDN junk) where many UNRELATED firms list
                 -- the same domain and would otherwise be falsely flagged multi-location.
                 (COALESCE(bool_or(co.branches ~ '[1-9]'), false)
                  OR (count(*) > 1 AND co.domain !~* '(^|\\.)(facebook|instagram|wix|wixsite|squarespace|godaddy|000webhost|weebly|blogspot|linktr|perfdrive|communityguide)\\.|\\.gov\\.au$')) AS multiloc
          FROM companies co WHERE co.domain IS NOT NULL {sd}{src}
          GROUP BY co.domain
          UNION ALL
          SELECT NULL, 'nodomain', 1, co.revenue_musd, co.employees, co.company_name, co.industry, co.sub_industry,
                 NULLIF(concat_ws(', ', co.suburb, co.state), ''), co.phone_norm,
                 jsonb_array_length(COALESCE(co.contacts, '[]'::jsonb)), co.source,
                 COALESCE(co.branches ~ '[1-9]', false)
          FROM companies co WHERE co.domain IS NULL {sn}{src}
        ), ge AS (
          SELECT g.*, (e.status IS NOT NULL) AS enriched,
                 (e.website IS NOT NULL) AS scanned,
                 ((e.website->>'runs_paid_ads') = 'true') AS runs_paid_ads,
                 (((e.website->>'runs_paid_ads')='true')
                   OR ((e.website->'trackers'->>'gtm') IS NOT NULL AND (e.website->>'uses_utm')='true')) AS gate_pass,
                 -- tracking-tag presence (supportive signals)
                 ((e.website->'trackers'->>'google_ads') IS NOT NULL) AS has_google_ads_tag,
                 ((e.website->'trackers'->>'meta_pixel') IS NOT NULL) AS has_meta_pixel,
                 ((e.website->'trackers'->>'gtm') IS NOT NULL) AS has_gtm,
                 ((e.website->'trackers'->>'ga4') IS NOT NULL) AS has_ga4,
                 ((e.website->'trackers'->>'bing_uet') IS NOT NULL) AS has_bing,
                 ((e.website->'trackers'->>'tiktok') IS NOT NULL) AS has_tiktok,
                 ((e.website->'trackers'->>'linkedin') IS NOT NULL) AS has_linkedin,
                 -- DataForSEO Transparency Center (PRIMARY running-ads signal)
                 (e.dataforseo IS NOT NULL) AS dfs_checked,
                 (e.dataforseo IS NOT NULL) AS has_dataforseo,
                 ((e.dataforseo->>'running_google_ads')='true') AS runs_google_ads,
                 (e.apollo IS NOT NULL AND (e.apollo->>'found')='true') AS has_apollo,
                 (e.business_intel IS NOT NULL AND (e.business_intel->>'found')='true') AS has_intel,
                 (e.whois IS NOT NULL AND (e.whois->>'found')='true') AS has_whois,
                 -- "already working with an agency" — a BDE call classified this prospect as
                 -- pipeline2 (existing agency / in-house team), matched by the domain company_key.
                 COALESCE(k.k_agency, false) AS has_agency,
                 -- Batch D 5-pipeline membership (query-time, domain-grained). Precedence
                 -- p5>p2>p1>p3; everything unresolved is the P4 fresh worklist. Booked/agency/
                 -- callback prospects are excluded from P4 by construction.
                 CASE WHEN COALESCE(k.k_booked, false) THEN 'p5'
                      WHEN COALESCE(k.k_agency, false) THEN 'p2'
                      WHEN COALESCE(k.k_p1, false)     THEN 'p1'
                      WHEN COALESCE(k.k_p3, false)     THEN 'p3'
                      ELSE 'p4' END AS pipeline,
                 CASE WHEN COALESCE(k.k_booked,false) OR COALESCE(k.k_agency,false)
                           OR COALESCE(k.k_p1,false) OR COALESCE(k.k_p3,false) THEN NULL
                      ELSE COALESCE(pr.p4_subpipeline,
                           CASE WHEN NOT (COALESCE(k.k_called,false) OR pr.last_called_at IS NOT NULL)
                                     AND ((e.dataforseo->>'running_google_ads')='true') THEN 'fresh_ads'
                                WHEN NOT (COALESCE(k.k_called,false) OR pr.last_called_at IS NOT NULL) THEN 'fresh_unscanned'
                                ELSE 'attempted' END)
                      END AS p4_sub
          FROM g
          LEFT JOIN enrichment e ON e.domain = g.domain
          LEFT JOIN prospects pr ON pr.domain = g.domain
          LEFT JOIN LATERAL (
            SELECT bool_or(cl.pipeline_stage = 'p5') AS k_booked,
                   bool_or(cl.pipeline = 'pipeline2_existing_agency') AS k_agency,
                   bool_or(cl.pipeline_stage = 'p1') AS k_p1,
                   bool_or(cl.pipeline_stage = 'p3') AS k_p3,
                   count(*) > 0 AS k_called
            FROM classifications cl
            WHERE g.domain IS NOT NULL AND cl.company_key = 'dom:' || g.domain
          ) k ON g.domain IS NOT NULL
        )
        """
        return cte, params, conds

    # Allow-listed sortable columns: query key -> ge SQL expression (no user SQL ever).
    _DB_SORTS = {
        "name": "name", "domain": "domain", "paid_ads": "runs_google_ads",
        "source": "sources", "industry": "industry", "sub_industry": "sub_industry",
        "location": "location", "businesses": "businesses", "revenue": "total_revenue",
        "employees": "employees", "contacts": "contacts", "phone": "phone",
        "enriched": "enriched", "pipeline": "pipeline",
    }

    @app.get("/api/database/prospects")
    def database_prospects(request: Request, search: str = "", limit: int = 50, offset: int = 0,
                           enriched: str = "", pipeline: str = "", p4sub: str = "", paid_ads: str = "",
                           source: str = "", coverage: str = "", revenue: str = "",
                           tracking: str = "", transparency: str = "", industry: str = "",
                           state: str = "", website: str = "", multilocation: str = "", agency: str = "",
                           rev_min: str = "", rev_max: str = "",
                           sort: str = "revenue", dir: str = "desc") -> JSONResponse:
        """The Database browser: the GIVEN business data (companies), STATIC columns only,
        GROUPED BY DOMAIN with SUMMED revenue (many businesses can share one domain).
        All filters combine with AND; `coverage`/`tracking` may carry several comma-separated
        facets. `pipeline` (p1-p5) + `p4sub` filter by Batch D pipeline membership. `sort`/`dir`
        order by any column (allow-listed). `total` is the full-filter count."""
        _require_db_access(request)
        limit = max(1, min(limit, 200))
        cte, params, conds = _db_cte(search, source, enriched, paid_ads, revenue,
                                     tracking, transparency, industry, state, website=website,
                                     multilocation=multilocation, agency=agency,
                                     pipeline=pipeline, p4sub=p4sub, rev_min=rev_min, rev_max=rev_max)
        params["lim"] = limit
        params["off"] = offset
        conds += _coverage_conds(coverage)
        enr_filter = ("WHERE " + " AND ".join(conds)) if conds else ""
        total = q(cte + f"SELECT count(*) AS n FROM ge {enr_filter}", params)[0]["n"]
        col = _DB_SORTS.get(sort, "total_revenue")
        direction = "ASC" if (dir or "").lower() == "asc" else "DESC"
        # Stable, deterministic tiebreak so paging never repeats/skips rows.
        tiebreak = "total_revenue DESC NULLS LAST, name" if col != "total_revenue" else "businesses DESC, name"
        order = f"{col} {direction} NULLS LAST, {tiebreak}"
        rows = q(
            cte + f"SELECT * FROM ge {enr_filter} ORDER BY {order} LIMIT %(lim)s OFFSET %(off)s",
            params,
        )
        return JSONResponse(jsonable_encoder({"total": total, "limit": limit, "offset": offset,
                                              "rows": rows, "sort": sort, "dir": direction.lower()}))

    @app.get("/api/database/stats")
    def database_stats(request: Request, search: str = "", enriched: str = "",
                       paid_ads: str = "", source: str = "", coverage: str = "",
                       revenue: str = "", tracking: str = "", transparency: str = "",
                       industry: str = "", state: str = "", website: str = "",
                       multilocation: str = "", agency: str = "",
                       rev_min: str = "", rev_max: str = "") -> JSONResponse:
        _require_db_access(request)
        r = q("SELECT (SELECT count(*) FROM companies) AS businesses, "
              "(SELECT count(DISTINCT domain) FROM companies WHERE domain IS NOT NULL) AS domains, "
              "(SELECT count(*) FROM companies WHERE domain IS NULL) AS no_domain, "
              "(SELECT count(*) FROM companies WHERE phone_norm IS NOT NULL) AS with_phone, "
              "(SELECT count(*) FROM enrichment WHERE status='ok') AS enriched")[0]
        # Stat cards are INDEPENDENT reference counts — the breakdown of the current
        # WHO-set (search/source/revenue/industry/state) only. They deliberately ignore the
        # SIGNAL filters (paid_ads/transparency/tracking/coverage/enriched) so e.g.
        # "Transparency checked" (all swept) stays >> "Running Google Ads" instead of
        # collapsing to the active filter. The result-bar total (/prospects.total) reflects
        # the FULL filter; these cards are reference + one-click shortcuts.
        cte, params, conds = _db_cte(search, source, "", "", revenue, "", "", industry, state, website=website,
                                     multilocation=multilocation, agency=agency, rev_min=rev_min, rev_max=rev_max)
        base = ("WHERE " + " AND ".join(conds)) if conds else ""
        cov = q(cte + f"""SELECT
            count(*) FILTER (WHERE ge.scanned) AS scanned,
            count(*) FILTER (WHERE ge.runs_google_ads) AS running_ads,
            count(*) FILTER (WHERE ge.has_meta_pixel) AS meta,
            count(*) FILTER (WHERE ge.dfs_checked) AS transparency,
            count(*) FILTER (WHERE ge.has_apollo) AS apollo,
            count(*) FILTER (WHERE ge.has_intel) AS intel,
            count(*) FILTER (WHERE ge.has_whois) AS whois,
            count(*) FILTER (WHERE ge.has_dataforseo) AS dataforseo,
            count(*) FILTER (WHERE ge.kind='domain') AS domains,
            count(*) FILTER (WHERE ge.pipeline='p1') AS p1,
            count(*) FILTER (WHERE ge.pipeline='p2') AS p2,
            count(*) FILTER (WHERE ge.pipeline='p3') AS p3,
            count(*) FILTER (WHERE ge.pipeline='p4') AS p4,
            count(*) FILTER (WHERE ge.pipeline='p5') AS p5,
            count(*) FILTER (WHERE ge.p4_sub='fresh_ads') AS p4_fresh_ads,
            count(*) FILTER (WHERE ge.p4_sub='captured_3cx' OR ge.p4_sub='captured_aircall') AS p4_captured,
            count(*) AS matching
          FROM ge {base}""", params)[0]
        # a freshness fingerprint so the client can poll cheaply for changes
        f = q("SELECT (SELECT count(*) FROM companies) AS cc, "
              "(SELECT max(created_at) FROM companies) AS cu, "
              "(SELECT max(fetched_at) FROM enrichment) AS eu")[0]
        token = f"{f['cc']}|{f['cu']}|{f['eu']}"
        return JSONResponse(jsonable_encoder({**r, "coverage": cov, "token": token}))

    # ---- Pipeline 2 assignment board (rotation + cadence) --------------- #
    @app.get("/pipeline2", response_class=HTMLResponse)
    def pipeline2_page() -> str:
        return _static("pipeline2.html")

    @app.get("/api/pipeline2")
    def pipeline2_list(request: Request, bde: str = "ALL", due_only: bool = False) -> JSONResponse:
        from ..pipeline2 import list_pipeline2
        scope = _scoped_bde(request, bde) if _is_bde(request) else (bde or "ALL")
        rows = list_pipeline2(pool, bde=(None if scope == "ALL" else scope), due_only=due_only)
        u = getattr(request.state, "user", None) or {}
        return JSONResponse(jsonable_encoder({
            "rows": rows, "can_assign": can_manage_pipeline(u),
            "roster": [r["bde_name"] for r in q(
                "SELECT DISTINCT COALESCE(bde_name, extension) AS bde_name FROM bde_agents "
                "WHERE in_scope AND active ORDER BY 1")],
        }))

    @app.post("/api/pipeline2/sync")
    async def pipeline2_sync_ep(request: Request) -> JSONResponse:
        u = getattr(request.state, "user", None) or {}
        if not can_manage_pipeline(u):
            raise HTTPException(403, "manager access required")
        from ..pipeline2 import sync_pipeline2
        stats = await run_in_threadpool(
            lambda: sync_pipeline2(pool, default_cadence_days=settings.pipeline2_default_cadence_days))
        return JSONResponse({"ok": True, "stats": stats})

    @app.post("/api/pipeline2/{dest9}/assign")
    async def pipeline2_assign_ep(dest9: str, request: Request) -> JSONResponse:
        u = getattr(request.state, "user", None) or {}
        if not can_manage_pipeline(u):
            raise HTTPException(403, "manager access required")
        from ..pipeline2 import assign_prospect
        d = await _form(request)
        bde = (d.get("bde") or "").strip()
        if not bde:
            return JSONResponse({"error": "pick a BDE"}, status_code=400)
        ok = await run_in_threadpool(
            assign_prospect, pool, dest9, bde, by=u.get("email") or "manager",
            next_action_at=d.get("next_action_at") or None, notes=d.get("notes"))
        return JSONResponse({"ok": ok})

    @app.post("/api/pipeline2/{dest9}/dnd")
    async def pipeline2_dnd_ep(dest9: str, request: Request) -> JSONResponse:
        """#5 — BDM/admin manually sets or clears DND on a Pipeline-2 prospect. A manual
        DND persists across syncs; clearing reverts to the AI signal on next sync."""
        u = getattr(request.state, "user", None) or {}
        if not can_manage_pipeline(u):
            raise HTTPException(403, "manager access required")
        d = await _form(request)
        on = d.get("dnd") in (True, "true", "True", "1", 1, "yes")
        reason = (d.get("reason") or "").strip() or "Manual DND by BDM"
        by = u.get("email") or u.get("name") or "bdm"

        def _do():
            with pool.connection() as conn, conn.cursor() as cur:
                if on:
                    cur.execute(
                        "UPDATE prospect_pipeline SET dnd=true, dnd_reason=%s, dnd_by=%s, "
                        "dnd_at=now(), next_action_at=NULL, updated_at=now() WHERE dest9=%s",
                        (reason, by, dest9))
                else:
                    cur.execute(
                        "UPDATE prospect_pipeline SET dnd=false, dnd_reason=NULL, dnd_by=NULL, "
                        "dnd_at=NULL, updated_at=now() WHERE dest9=%s", (dest9,))
                conn.commit()
        await run_in_threadpool(_do)
        return JSONResponse({"ok": True, "dnd": on})

    # ---- calendar ------------------------------------------------------- #
    @app.get("/calendar", response_class=HTMLResponse)
    def calendar_page() -> str:
        return _static("calendar.html")

    def _cal_scope(request: Request, requested_bde: str | None) -> str | None:
        """Returns the bde_name to filter events by, or None for all (manager/kiosk)."""
        if _is_bde(request):
            return _scoped_bde(request, None)
        return requested_bde or None  # manager: optional filter

    @app.get("/api/calendar")
    def calendar_list(request: Request, start: str, end: str, bde: str | None = None) -> JSONResponse:
        from ..calendar import list_events
        rows = list_events(pool, start, end, _cal_scope(request, bde if bde and bde != "ALL" else None))
        return JSONResponse(jsonable_encoder({"events": rows}))

    @app.get("/api/callbacks-today")
    def callbacks_today(request: Request, bde: str = "ALL") -> JSONResponse:
        """How many callbacks are scheduled for today (+ overdue), with the list (#8)."""
        scope = _cal_scope(request, bde if bde and bde != "ALL" else None)
        where = ["type = 'callback'"]
        params: list = []
        if scope:
            where.append("bde_name = %s")
            params.append(scope)
        wc = " AND ".join(where)
        today_n = q(f"SELECT count(*) AS n FROM calendar_events WHERE {wc} AND start_at::date = current_date "
                    "AND status <> 'cancelled'", tuple(params))[0]["n"]
        overdue_n = q(f"SELECT count(*) AS n FROM calendar_events WHERE {wc} AND start_at::date < current_date "
                      "AND status = 'pending'", tuple(params))[0]["n"]
        rows = q(f"SELECT id, bde_name, title, start_at, dest_number, call_id, status "
                 f"FROM calendar_events WHERE {wc} AND start_at::date = current_date AND status <> 'cancelled' "
                 "ORDER BY start_at", tuple(params))
        for r in rows:
            r["start_at"] = str(r["start_at"]) if r.get("start_at") else None
        return JSONResponse(jsonable_encoder({"today": today_n, "overdue": overdue_n, "rows": rows}))

    @app.get("/api/event/{eid}")
    def event_detail(request: Request, eid: int) -> JSONResponse:
        """Full detail for one calendar_events row so the calendar UI can render a rich
        event panel: the event itself, a resolved prospect_key + business_name (drives the
        'Open prospect page' link) and — when the event came from a call — that call's
        evidence JSON (game plan / next_call_points)."""
        rows = q("SELECT id, type, title, start_at, end_at, bde_name, status, notes, "
                 "dest_number, call_id FROM calendar_events WHERE id = %s", (eid,))
        if not rows:
            raise HTTPException(404, "event not found")
        ev = rows[0]
        # A BDE may only open their own events; managers/kiosk see all.
        if _is_bde(request) and ev.get("bde_name") and ev["bde_name"] != _scoped_bde(request, None):
            raise HTTPException(403, "not your event")
        # prospect_key = the dialled number's digits → drives /prospect/<key>. dest9
        # (trailing 9) is the distinct-prospect key the pipeline board / call rows use.
        import re as _re
        prospect_key = _re.sub(r"\D", "", ev.get("dest_number") or "") or None
        dest9 = prospect_key[-9:] if prospect_key else None
        # business_name: prefer the pipeline board (master-file name), else the source
        # call's AI-extracted company, else leave null.
        business_name = None
        if dest9:
            pn = q("SELECT business_name FROM prospect_pipeline WHERE dest9 = %s "
                   "AND NULLIF(business_name,'') IS NOT NULL LIMIT 1", (dest9,))
            if pn:
                business_name = pn[0]["business_name"]
        if not business_name and ev.get("call_id"):
            cn = q("SELECT prospect_company FROM classifications WHERE call_id = %s "
                   "AND NULLIF(prospect_company,'') IS NOT NULL", (ev["call_id"],))
            if cn:
                business_name = cn[0]["prospect_company"]
        # evidence (game plan / next_call_points) from the linked call, if any.
        evidence = None
        if ev.get("call_id"):
            er = q("SELECT evidence FROM classifications WHERE call_id = %s", (ev["call_id"],))
            if er:
                evidence = er[0].get("evidence")
        return JSONResponse(jsonable_encoder({
            "event": ev, "prospect_key": prospect_key,
            "business_name": business_name, "evidence": evidence,
        }))

    @app.post("/api/calendar")
    async def calendar_create(request: Request) -> JSONResponse:
        u = getattr(request.state, "user", None) or {}
        if u.get("role") == "kiosk":
            raise HTTPException(403, "read-only")
        from ..calendar import create_event
        b = await request.json()
        owner = _scoped_bde(request, b.get("bde_name")) if _is_bde(request) else (b.get("bde_name") or None)
        eid = await run_in_threadpool(
            create_event, pool, bde_name=owner, type=b.get("type", "meeting"),
            title=b.get("title", "Untitled"), start_at=b.get("start_at"),
            end_at=b.get("end_at"), notes=b.get("notes"), dest_number=b.get("dest_number"),
            created_by=u.get("email"))
        return JSONResponse({"id": eid})

    @app.post("/api/calendar/{eid}/update")
    async def calendar_update(request: Request, eid: int) -> JSONResponse:
        u = getattr(request.state, "user", None) or {}
        if u.get("role") == "kiosk":
            raise HTTPException(403, "read-only")
        from ..calendar import update_event
        fields = await request.json()
        ok = await run_in_threadpool(update_event, pool, eid, fields,
                                     restrict_bde=_scoped_bde(request, None) if _is_bde(request) else None)
        return JSONResponse({"ok": ok})

    @app.post("/api/calendar/{eid}/delete")
    async def calendar_delete(request: Request, eid: int) -> JSONResponse:
        u = getattr(request.state, "user", None) or {}
        if u.get("role") == "kiosk":
            raise HTTPException(403, "read-only")
        from ..calendar import delete_event
        ok = await run_in_threadpool(delete_event, pool, eid,
                                     restrict_bde=_scoped_bde(request, None) if _is_bde(request) else None)
        return JSONResponse({"ok": ok})

    return app
