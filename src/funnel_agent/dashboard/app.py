"""FastAPI app for the funnel dashboard."""

from __future__ import annotations

from contextlib import asynccontextmanager
from importlib import resources

from fastapi import FastAPI, HTTPException
from fastapi.encoders import jsonable_encoder
from fastapi.responses import HTMLResponse, JSONResponse

from ..config import Settings, get_settings
from ..db.analytics import make_analytics_pool

# Funnel stages in order, with the daily_funnel column each maps to.
STAGES = [
    ("Calls Made", "calls_made"),
    ("Connected", "connected"),
    ("RPC Connect", "rpc_connect"),
    ("Full Pitch", "full_pitch"),
    ("Lead (Meeting Booked)", "meetings_booked"),
]


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

    # ---- pages ---------------------------------------------------------- #
    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return _index_html()

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
    def summary() -> JSONResponse:
        dates = [str(r["report_date"]) for r in q(
            "SELECT DISTINCT report_date FROM daily_funnel ORDER BY report_date DESC")]
        bdes = [r["bde_name"] for r in q(
            "SELECT DISTINCT bde_name FROM daily_funnel WHERE bde_name <> 'ALL' "
            "ORDER BY bde_name")]
        review = q("SELECT count(*) AS n FROM classifications WHERE needs_human_review")
        return JSONResponse({
            "dates": dates,
            "bdes": ["ALL", *bdes],
            "review_count": review[0]["n"] if review else 0,
            "stages": [s[0] for s in STAGES],
        })

    @app.get("/api/funnel")
    def funnel(date: str, bde: str = "ALL") -> JSONResponse:
        rows = q(
            "SELECT * FROM daily_funnel WHERE report_date=%s AND bde_name=%s",
            (date, bde),
        )
        tracks = {r["track"]: r for r in rows}
        if not tracks:
            return JSONResponse({"found": False, "tracks": {}, "stages": [], "conversion": {}})
        c = tracks.get("combined", {})

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
        cm = c.get("calls_made") or 0
        conv = {
            "connect": _pct(c.get("connected") or 0, cm),
            "rpc": _pct(c.get("rpc_connect") or 0, c.get("connected") or 0),
            "pitch": _pct(c.get("full_pitch") or 0, c.get("rpc_connect") or 0),
            "booked": _pct(c.get("meetings_booked") or 0, c.get("full_pitch") or 0),
        }
        coverage = _pct(c.get("transcribed") or 0, cm)
        return JSONResponse({
            "found": True, "stages": stage_rows, "conversion": conv, "coverage": coverage,
            "calls_made": int(cm), "transcribed": int(c.get("transcribed") or 0),
        })

    @app.get("/api/leaderboard")
    def leaderboard(date: str) -> JSONResponse:
        rows = q(
            "SELECT bde_name, calls_made, connected, transcribed, rpc_connect, "
            "full_pitch, leads, qualified, meetings_booked FROM daily_funnel "
            "WHERE report_date=%s AND track='combined' AND bde_name<>'ALL' "
            "ORDER BY leads DESC, calls_made DESC",
            (date,),
        )
        out = []
        for r in rows:
            out.append({
                **{k: int(r[k] or 0) for k in
                   ("calls_made", "connected", "transcribed", "rpc_connect",
                    "full_pitch", "leads", "qualified", "meetings_booked")},
                "bde_name": r["bde_name"],
                "conv_connect": _pct(r["connected"] or 0, r["calls_made"] or 0),
                "conv_rpc": _pct(r["rpc_connect"] or 0, r["connected"] or 0),
                "conv_pitch": _pct(r["full_pitch"] or 0, r["rpc_connect"] or 0),
                "conv_booked": _pct(r["meetings_booked"] or 0, r["full_pitch"] or 0),
            })
        return JSONResponse({"rows": out})

    @app.get("/api/trend")
    def trend(bde: str = "ALL", days: int = 30) -> JSONResponse:
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

    @app.get("/api/review-queue")
    def review_queue(limit: int = 50) -> JSONResponse:
        rows = q(
            "SELECT cl.call_id, c.bde_name, c.dest_number, c.started_at, cl.call_outcome, "
            "cl.full_pitch, cl.is_lead, cl.qualified, cl.meeting_booked, "
            "LEAST(cl.pitch_confidence, cl.lead_confidence, cl.qual_confidence) AS min_conf, "
            "cl.model "
            "FROM classifications cl JOIN calls c ON c.call_id = cl.call_id "
            "WHERE cl.needs_human_review ORDER BY min_conf ASC NULLS FIRST LIMIT %s",
            (limit,),
        )
        for r in rows:
            r["started_at"] = str(r["started_at"]) if r["started_at"] else None
            r["min_conf"] = float(r["min_conf"]) if r["min_conf"] is not None else None
        return JSONResponse({"rows": rows})

    _STAGE_COND = {
        "calls_made": "TRUE",
        "connected": "c.answered AND c.talk_seconds >= %(thr)s",
        "unconnected": "NOT (c.answered AND c.talk_seconds >= %(thr)s)",
        "rpc_connect": "cl.rpc_connect",
        "full_pitch": "cl.full_pitch",
        "meetings_booked": "cl.meeting_booked",
        "lead": "cl.is_lead",
        "qualified": "cl.qualified",
    }

    @app.get("/api/stage-calls")
    def stage_calls(date: str, stage: str, bde: str = "ALL",
                    track: str = "combined", limit: int = 500) -> JSONResponse:
        """List the individual calls behind a funnel-stage count (for validation)."""
        cond = _STAGE_COND.get(stage, "TRUE")
        where = ["c.in_scope", "c.started_at >= %(d)s::date",
                 "c.started_at < (%(d)s::date + 1)", f"({cond})"]
        params: dict = {"d": date, "thr": settings.rpc_min_talk_seconds, "lim": limit}
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

    @app.get("/call/{call_id}", response_class=HTMLResponse)
    def call_page(call_id: str) -> str:
        return resources.files("funnel_agent.dashboard").joinpath(
            "static/call.html").read_text(encoding="utf-8")

    @app.get("/api/call/{call_id}")
    def call_detail(call_id: str) -> JSONResponse:
        calls = q("SELECT * FROM calls WHERE call_id=%s", (call_id,))
        if not calls:
            raise HTTPException(404, "call not found")
        call = calls[0]
        call["started_at"] = str(call["started_at"]) if call["started_at"] else None
        tr = q("SELECT text, sentiment, summary, diarized FROM transcripts WHERE call_id=%s",
               (call_id,))
        cl = q("SELECT * FROM classifications WHERE call_id=%s", (call_id,))
        # jsonable_encoder converts numeric->float and datetime->iso so confidences serialize.
        return JSONResponse(jsonable_encoder({
            "call": call,
            "transcript": tr[0] if tr else None,
            "classification": cl[0] if cl else None,
        }))

    return app
