"""Phase I — render the daily funnel report (per BDE + overall) as Markdown + JSON.

Reads `daily_funnel` only. The layout matches the strategy doc: Fresh / Followup /
Total columns, the transcript-coverage line, and stage-over-stage conversion %.
"""

from __future__ import annotations

import json
from datetime import date

from psycopg_pool import ConnectionPool

from .config import Settings
from .logging import get_logger

log = get_logger(__name__)

_LABEL_W = 22
_NUM_W = 9


def _row(label: str, fresh: int | str, followup: int | str, total: int | str) -> str:
    return f"{label:<{_LABEL_W}}{str(fresh):>{_NUM_W}}{str(followup):>{_NUM_W}}{str(total):>{_NUM_W}}"


def _pct(num: int, den: int) -> str:
    return f"{round(100 * num / den)}%" if den else "—"


def fetch_day(pool: ConnectionPool, day: date) -> dict[str, dict[str, dict]]:
    """Return {bde_name: {track: funnel_row}} for a report date."""
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT * FROM daily_funnel WHERE report_date = %s", (day,))
        rows = cur.fetchall()
    out: dict[str, dict[str, dict]] = {}
    for r in rows:
        out.setdefault(r["bde_name"], {})[r["track"]] = r
    return out


def _ext_map(pool: ConnectionPool) -> dict[str, str]:
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT COALESCE(bde_name, extension) AS name, extension FROM bde_agents")
        return {str(r["name"]): str(r["extension"]) for r in cur.fetchall()}


def render_block(bde_name: str, ext: str | None, tracks: dict[str, dict], day: date) -> str:
    f = tracks.get("fresh", {})
    u = tracks.get("followup", {})
    c = tracks.get("combined", {})

    def g(d: dict, k: str) -> int:
        return int(d.get(k) or 0)

    header_name = "OVERALL (all BDEs)" if bde_name == "ALL" else f"BDE: {bde_name}"
    if ext and bde_name != "ALL":
        header_name += f"  (ext {ext})"

    lines = [
        f"{header_name:<{_LABEL_W + _NUM_W}}{str(day):>{_NUM_W * 2}}",
        _row("", "FRESH", "FOLLOWUP", "TOTAL"),
        _row("Calls Made", g(f, "calls_made"), g(u, "calls_made"), g(c, "calls_made")),
        _row("  (connected, CDR)", g(f, "connected"), g(u, "connected"), g(c, "connected")),
        _row("  (transcribed)", g(f, "transcribed"), g(u, "transcribed"), g(c, "transcribed")),
        _row("RPC Connect", g(f, "rpc_connect"), g(u, "rpc_connect"), g(c, "rpc_connect")),
        _row("Full Pitch", g(f, "full_pitch"), g(u, "full_pitch"), g(c, "full_pitch")),
        _row("Lead", g(f, "leads"), g(u, "leads"), g(c, "leads")),
        "-" * (_LABEL_W + _NUM_W * 3),
        _row("Qualified Lead", g(f, "qualified"), g(u, "qualified"), g(c, "qualified")),
        _row("Meeting Booked", g(f, "meetings_booked"), g(u, "meetings_booked"), g(c, "meetings_booked")),
    ]
    # "Meeting Done" (calendar/CRM) is optional; only show it if that adapter is feeding data.
    if g(c, "meetings_done"):
        lines.append(_row("Meeting Done (cal)", g(f, "meetings_done"), g(u, "meetings_done"), g(c, "meetings_done")))

    tr = g(c, "transcribed")
    conv = (
        f"Conversion (Total, over transcribed): "
        f"Connect {_pct(g(c, 'rpc_connect'), tr)} | "
        f"Pitch {_pct(g(c, 'full_pitch'), g(c, 'rpc_connect'))} | "
        f"Lead {_pct(g(c, 'leads'), g(c, 'full_pitch'))} | "
        f"Qual {_pct(g(c, 'qualified'), g(c, 'leads'))}"
    )
    lines += ["", conv]
    return "\n".join(lines)


def build_markdown(
    pool: ConnectionPool, day: date, *, only_bde: str | None = None, only_all: bool = False
) -> str:
    data = fetch_day(pool, day)
    exts = _ext_map(pool)
    if not data:
        return f"# Funnel report — {day}\n\n_No data for this date. Run the pipeline first._\n"

    blocks: list[str] = [f"# Funnel report — {day}\n"]

    # Overall first.
    if "ALL" in data and not only_bde:
        blocks.append("```\n" + render_block("ALL", None, data["ALL"], day) + "\n```")

    if not only_all:
        names = sorted(n for n in data if n != "ALL")
        if only_bde:
            # only_bde may be an extension or a name
            name_by_ext = {v: k for k, v in exts.items()}
            target = name_by_ext.get(only_bde, only_bde)
            names = [n for n in names if n == target]
        for name in names:
            blocks.append("```\n" + render_block(name, exts.get(name), data[name], day) + "\n```")

    return "\n\n".join(blocks) + "\n"


def build_json(pool: ConnectionPool, day: date) -> dict:
    data = fetch_day(pool, day)
    serial = {
        bde: {track: {k: (str(v) if isinstance(v, date) else v) for k, v in row.items()}
              for track, row in tracks.items()}
        for bde, tracks in data.items()
    }
    return {"report_date": str(day), "funnel": serial}


def send_email(settings: Settings, subject: str, body_markdown: str) -> bool:
    """Email the report via SMTP if configured. Returns True if sent."""
    if not (settings.smtp_host and settings.report_email_to and settings.report_email_from):
        return False
    import smtplib
    from email.message import EmailMessage

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = settings.report_email_from
    msg["To"] = settings.report_email_to
    msg.set_content(body_markdown)

    with smtplib.SMTP(settings.smtp_host, settings.smtp_port) as smtp:
        smtp.starttls()
        if settings.smtp_user:
            smtp.login(settings.smtp_user, settings.smtp_password)
        smtp.send_message(msg)
    log.info("report_emailed", to=settings.report_email_to)
    return True


def write_report_files(markdown: str, payload: dict, out_dir: str, day: date) -> tuple[str, str]:
    import os

    os.makedirs(out_dir, exist_ok=True)
    md_path = os.path.join(out_dir, f"funnel-{day}.md")
    json_path = os.path.join(out_dir, f"funnel-{day}.json")
    with open(md_path, "w", encoding="utf-8") as fh:
        fh.write(markdown)
    with open(json_path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, default=str)
    return md_path, json_path
