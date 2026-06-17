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


def _row(label: str, fresh, followup, total, conv: str = "") -> str:
    return (f"{label:<{_LABEL_W}}{str(fresh):>{_NUM_W}}{str(followup):>{_NUM_W}}"
            f"{str(total):>{_NUM_W}}{str(conv):>{_NUM_W}}")


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

    # Funnel stages; CONV% is each stage over the PREVIOUS stage (drop-off).
    stages = [
        ("Calls Made", "calls_made"),
        ("Connected", "connected"),
        ("RPC Connect", "rpc_connect"),
        ("Full Pitch", "full_pitch"),
        ("Lead (Meeting Booked)", "meetings_booked"),
    ]
    lines = [
        f"{header_name:<{_LABEL_W + _NUM_W}}{str(day):>{_NUM_W * 3}}",
        _row("", "FRESH", "FOLLOWUP", "TOTAL", "CONV%"),
    ]
    prev = None
    for i, (label, key) in enumerate(stages):
        tot = g(c, key)
        conv = "" if i == 0 else _pct(tot, prev)
        lines.append(_row(label, g(f, key), g(u, key), tot, conv))
        if key == "calls_made":
            cov = _pct(g(c, "transcribed"), g(c, "calls_made"))
            lines.append(_row("  (transcribed)", g(f, "transcribed"),
                              g(u, "transcribed"), g(c, "transcribed"), cov))
        prev = tot
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
