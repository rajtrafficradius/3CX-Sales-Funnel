"""Emma Collins — the 3rd AI staff member: EVERYTHING AFTER THE BOOKING.

Role (owner-confirmed, Raj/Vysakh): scheduling, calendar invites, replies, reschedules and
reminders for every booked meeting across the whole operation — Lisa-1 bookings, Lisa-4 reveals
AND human-BDE booked meetings (classifications.meeting_booked ⋈ calls) — one unified daily queue.

Hard rules:
  • MANDATORY APPROVAL — nothing sends without a human (Kiran) clicking "Approve & schedule".
    Emma only PRE-DRAFTS each invite (prospect, company, agreed time, email, summary/context);
    incomplete drafts sit in a 'needs-info' (needs time/email) tray. The approval gate applies
    to CHANGES too (reschedules). The ONLY approval-exempt sends are REMINDERS for meetings a
    human already approved (morning-of + Friday-for-Monday).
  • SEND = Microsoft Graph, app-only (client_credentials): events are created on the scheduler
    mailbox (SCHEDULER_MAILBOX) with the prospect as attendee and isOnlineMeeting=true (Teams).
    Until MS_TENANT_ID / MS_CLIENT_ID / MS_CLIENT_SECRET / SCHEDULER_MAILBOX are ALL set, an
    approval lands in QUEUED mode (status 'approved-awaiting-creds') and goes out automatically
    the moment credentials appear — the full Graph client below is built NOW so ONLY the 4 env
    values are needed later.
  • TRACKING — (a) attendee accepted/declined/tentative via Graph event polling → status chip +
    CRM note; (b) an email-open pixel in Emma's confirmation email (<img src=/api/emma/px/{token}>,
    served by the dashboard) — opens are labelled SIGNAL, NOT PROOF.

Statuses (emma_meetings.status):
  draft -> needs-info -> approved-awaiting-creds -> scheduled -> accepted | declined
                                                             -> reschedule-requested -> cancelled
Every transition is journalled in emma_events.
"""
from __future__ import annotations

import base64
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from html import escape as _esc
from zoneinfo import ZoneInfo

from psycopg.types.json import Json
from psycopg_pool import ConnectionPool

from .config import Settings
from .logging import get_logger

log = get_logger(__name__)

GRAPH_BASE = "https://graph.microsoft.com/v1.0"
LOGIN_BASE = "https://login.microsoftonline.com"

# Transparent 1×1 GIF served by GET /api/emma/px/{token} — the email-open tracking pixel.
PIXEL_GIF = base64.b64decode("R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7")

# Statuses a prospect can still interact with (poll responses / match replies against these).
_ACTIVE_STATUSES = ("scheduled", "accepted", "declined", "reschedule-requested")
# A reply that reads like "that time doesn't work" flips the meeting to reschedule-requested.
_RESCHED_RE = re.compile(
    r"reschedul|another time|different time|can'?t make|cannot make|won'?t make|"
    r"move (?:the |our )?(?:meeting|call|time)|push (?:it|back)|postpone|"
    r"doesn'?t work|does not work|clash|conflict", re.I)


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #
def _rows(pool: ConnectionPool, sql: str, params=None) -> list[dict]:
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def _exec(pool: ConnectionPool, sql: str, params=None) -> int:
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        n = cur.rowcount
        conn.commit()
        return n


def _row1(pool: ConnectionPool, sql: str, params=None) -> dict | None:
    r = _rows(pool, sql, params)
    return r[0] if r else None


def _d9(number: str | None) -> str:
    return re.sub(r"[^0-9]", "", str(number or ""))[-9:]


def _tables_exist(pool: ConnectionPool, *names: str) -> bool:
    """True when EVERY named table exists — the local DB has no Lisa tables (Lisa lives on the
    cloud deployment), so each queue source is skipped cleanly where its tables are absent."""
    r = _row1(pool, "SELECT bool_and(to_regclass(t) IS NOT NULL) AS ok "
                    "FROM unnest(%s::text[]) t", (list(names),))
    return bool(r and r.get("ok"))


def _log_event(pool: ConnectionPool, meeting_id: int | None, kind: str, **detail) -> None:
    """Journal a transition/action into emma_events (never raises — logging must not break flow)."""
    try:
        _exec(pool, "INSERT INTO emma_events (meeting_id, kind, detail) VALUES (%s,%s,%s)",
              (meeting_id, kind, Json({k: v for k, v in detail.items() if v is not None})))
    except Exception as exc:
        log.warning("emma_event_log_failed", kind=kind, error=str(exc)[:120])


def _crm_note(pool: ConnectionPool, dest9: str | None, note: str) -> None:
    """Append Emma's tracking note onto the prospect's booked_crm row (status chip + CRM note rule).
    Appends (never clobbers a human's note); creates the row/table if missing; never raises."""
    if not dest9:
        return
    sql = ("INSERT INTO booked_crm (dest9, note, updated_by, updated_at) "
           "VALUES (%s, left(%s,400), 'emma', now()) "
           "ON CONFLICT (dest9) DO UPDATE SET "
           "  note=left(COALESCE(NULLIF(booked_crm.note,'')||' | ','')||EXCLUDED.note, 400), "
           "  updated_by='emma', updated_at=now()")
    try:
        _exec(pool, sql, (dest9, note))
    except Exception:
        try:  # first-ever run on a fresh DB — the CRM table comes from the Lisa subsystem
            _exec(pool, "CREATE TABLE IF NOT EXISTS booked_crm ("
                        "  dest9 text PRIMARY KEY, status text, note text, updated_by text,"
                        "  updated_at timestamptz DEFAULT now(), replies_seen_at timestamptz)")
            _exec(pool, sql, (dest9, note))
        except Exception as exc:
            log.warning("emma_crm_note_failed", dest9=dest9, error=str(exc)[:120])


def _local_now(settings: Settings) -> datetime:
    return datetime.now(ZoneInfo(settings.tz))


def _fmt_local(settings: Settings, dt: datetime | None) -> str:
    if not dt:
        return ""
    try:
        return dt.astimezone(ZoneInfo(settings.tz)).strftime("%A %d %B %Y, %H:%M")
    except Exception:
        return str(dt)


def _tz_label(settings: Settings) -> str:
    return (settings.tz or "").split("/")[-1].replace("_", " ") + " time"


def _parse_cc(raw: str | None) -> list[str]:
    """Normalise a free-text 'CC / extra attendees' field (comma/semicolon/space separated)
    into a clean, de-duplicated list of email addresses. Cap at 10 — an invite is a meeting,
    not a mail-out."""
    out: list[str] = []
    seen: set[str] = set()
    for tok in re.split(r"[,;\s]+", raw or ""):
        tok = tok.strip().strip("<>").strip()
        if "@" in tok and "." in tok.split("@")[-1]:
            key = tok.lower()
            if key not in seen:
                seen.add(key)
                out.append(tok)
    return out[:10]


# --------------------------------------------------------------------------- #
# agreed-time parser — "Monday at 11 am" → a concrete Melbourne datetime
# --------------------------------------------------------------------------- #
_WEEKDAYS = {"monday": 0, "mon": 0, "tuesday": 1, "tues": 1, "tue": 1,
             "wednesday": 2, "weds": 2, "wed": 2,
             "thursday": 3, "thurs": 3, "thur": 3, "thu": 3, "friday": 4, "fri": 4,
             "saturday": 5, "sat": 5, "sunday": 6, "sun": 6}
_WD_RE = re.compile(r"\b(monday|tuesday|wednesday|thursday|friday|saturday|sunday|"
                    r"mon|tues|tue|weds|wed|thurs|thur|thu|fri|sat|sun)\b")
_MON_RE = "jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec"
_MONTHS = {m: i + 1 for i, m in enumerate((
    "jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"))}
_WORDNUMS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
             "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12}
_WORDNUM_RE = "eleven|twelve|one|two|three|four|five|six|seven|eight|nine|ten"


def parse_agreed_time(text: str | None, booked_at: datetime | None,
                      tz: str = "Australia/Melbourne") -> datetime | None:
    """Best-effort PURE-PYTHON parse of the free-text time agreed on a call — 'Monday at
    11 am', 'Friday 3:30 pm', 'tomorrow morning about ten', 'Tuesday, the 18th at 12:00',
    'Friday 21 August, 11:00 a.m.' — into a CONCRETE tz-aware datetime: the NEXT occurrence
    of that weekday/date relative to booked_at, in the operation's timezone (Melbourne).

    Rules (owner spec): morning with no hour = 10:00; arvo/afternoon = 14:00 (evening =
    16:00); bare hours land in business time (1–7 → pm, 8–11 → am, 12 → noon). Deliberately
    CONSERVATIVE — returns None whenever there is no day cue or no usable time-of-day; the
    result only PREFILLS start_at on a draft, Kiran still approves/adjusts it."""
    t = (text or "").strip().lower()
    if not t or len(t) > 400:
        return None
    zone = ZoneInfo(tz)
    anchor = booked_at or datetime.now(zone)
    anchor = anchor.astimezone(zone) if anchor.tzinfo else anchor.replace(tzinfo=zone)
    t = re.sub(r"\b([ap])\.\s*m\b\.?", r"\1m", t)        # "a.m." / "p.M." → am / pm
    t = t.replace("‘", "'").replace("’", "'")

    # ---- which day? explicit date > tomorrow/today > 'the 18th' > weekday ---------- #
    day: date | None = None
    weekday_only = False        # weekday-derived dates roll +7 when the slot already passed
    m = (re.search(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+(?:of\s+)?({_MON_RE})[a-z]*\b", t)
         or re.search(rf"\b({_MON_RE})[a-z]*\s+(\d{{1,2}})(?:st|nd|rd|th)?\b", t))
    if m:
        g1, g2 = m.group(1), m.group(2)
        dom, mon = (int(g1), g2) if g1.isdigit() else (int(g2), g1)
        try:
            day = date(anchor.year, _MONTHS[mon], dom)
        except ValueError:
            return None
        if day < anchor.date():                          # "21 August" said in September
            try:
                day = date(anchor.year + 1, _MONTHS[mon], dom)
            except ValueError:
                return None
    elif re.search(r"\btomorrow\b", t):
        day = anchor.date() + timedelta(days=1)
    elif re.search(r"\btoday\b|\btonight\b|\bthis (?:morning|afternoon|arvo|evening)\b", t):
        day = anchor.date()
    else:
        # "the 18th" / "Tuesday 11th" / bare "19th" — an ordinal day-of-month outranks the
        # weekday word next to it (the prospect named a DATE; the weekday is just flavour).
        m = (re.search(r"\bthe\s+(\d{1,2})(?:st|nd|rd|th)?\b", t)
             or re.search(r"\b(\d{1,2})(?:st|nd|rd|th)\b", t))
        if m:                                            # "Tuesday, the 18th" — the 18th wins
            dom = int(m.group(1))
            base = anchor.date()
            y, mo = (base.year, base.month) if dom >= base.day else (
                (base.year + 1, 1) if base.month == 12 else (base.year, base.month + 1))
            try:
                day = date(y, mo, dom)
            except ValueError:
                return None
        else:
            m = _WD_RE.search(t)
            if m:
                ahead = (_WEEKDAYS[m.group(1)] - anchor.weekday()) % 7
                if ahead == 0 and re.search(r"\bnext\b", t):
                    ahead = 7                            # "next Wednesday" said on a Wednesday
                day = anchor.date() + timedelta(days=ahead)
                weekday_only = True
    if day is None:
        return None

    # ---- what time? ----------------------------------------------------------------- #
    daypart = ("morning" if re.search(r"\bmorning\b", t) else
               "afternoon" if re.search(r"\bafternoon\b|\barvo\b", t) else
               "evening" if re.search(r"\bevening\b|\btonight\b", t) else None)
    hour: int | None = None
    minute = 0
    marker: str | None = None
    if re.search(r"\bnoon\b|\bmid-?day\b", t):
        hour, marker = 12, "pm"
    if hour is None:
        m = re.search(r"\b(\d{1,2}):(\d{2})\s*(am|pm)?\b", t)          # 3:30 pm / 12:00
        if m:
            hour, minute, marker = int(m.group(1)), int(m.group(2)), m.group(3)
    if hour is None:
        m = re.search(r"\b(\d{1,2})(?:\.(\d{2}))?\s*(am|pm)\b", t)     # 11 am / 10.30am
        if m:
            hour, minute, marker = int(m.group(1)), int(m.group(2) or 0), m.group(3)
    if hour is None:
        m = re.search(rf"\b(half|quarter)\s+(past|to)\s+(\d{{1,2}}|{_WORDNUM_RE})\b", t)
        if m:                                                          # half past ten
            hh = m.group(3)
            hour = int(hh) if hh.isdigit() else _WORDNUMS[hh]
            if m.group(2) == "past":
                minute = 30 if m.group(1) == "half" else 15
            else:
                hour, minute = (hour - 1 or 12), (45 if m.group(1) == "quarter" else 30)
    if hour is None:
        m = (re.search(rf"\b(?:at|about|around|by)\s+(?:about\s+|around\s+)?({_WORDNUM_RE})\b", t)
             or re.search(rf"\b({_WORDNUM_RE})\s+o'?clock\b", t)       # "about ten" / "ten o'clock"
             or re.search(rf"\b({_WORDNUM_RE})\s*(am|pm)\b", t))       # "Two PM, Monday"
        if m:
            hour = _WORDNUMS[m.group(1)]
            if len(m.groups()) >= 2 and m.group(2):
                marker = m.group(2)
    if hour is None:
        m = (re.search(r"\b(\d{1,2})\s+o'?clock\b", t)
             or re.search(r"\b(?:at|about|around|by)\s+(\d{1,2})\b(?![:.]?\d)", t))
        if m:
            hour = int(m.group(1))
    if hour is None:
        if daypart == "morning":
            hour = 10                    # owner spec: morning with no hour = 10:00
        elif daypart == "afternoon":
            hour = 14                    # arvo = 14:00
        elif daypart == "evening":
            hour = 16
        else:
            return None                  # a day but no usable time → stays needs-info
    elif marker == "pm":
        hour += 12 if hour < 12 else 0
    elif marker == "am":
        hour = 0 if hour == 12 else hour
    elif hour <= 12:                     # no am/pm — infer from daypart, else business hours
        if daypart == "morning":
            pass                         # 8, 9, 10, 11 …
        elif daypart in ("afternoon", "evening"):
            hour += 12 if hour < 12 else 0
        elif 1 <= hour <= 7:
            hour += 12                   # "at 4" → 16:00; 8–11 stay am; 12 stays noon
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    out = datetime(day.year, day.month, day.day, hour, minute, tzinfo=zone)
    if weekday_only and out <= anchor:   # "Wednesday at 8am" said Wednesday 9:30 → next week
        out += timedelta(days=7)
    return out


# --------------------------------------------------------------------------- #
# schema — emma_meetings (reworked meeting_invites) + emma_events + emma_control
# --------------------------------------------------------------------------- #
_TABLES_READY = False


def ensure_emma_tables(pool: ConnectionPool, force: bool = False) -> None:
    """Create/patch Emma's tables. Same pattern as lisa.ensure_tables: run the DDL ONCE per
    process, with a cheap READ-ONLY probe first so re-runs never take an AccessExclusiveLock
    against the dashboard's constant reads. Bump the sentinel column when adding columns."""
    global _TABLES_READY
    if _TABLES_READY and not force:
        return
    if not force:
        try:
            with pool.connection() as conn, conn.cursor() as cur:
                cur.execute("SELECT 1 FROM information_schema.columns "
                            "WHERE table_name='emma_meetings' AND column_name='reminders_enabled'")
                if cur.fetchone() is not None:
                    _TABLES_READY = True
                    return
        except Exception:
            pass
    with pool.connection() as conn, conn.cursor() as cur:
        # One row per PROSPECT (dest9) — the single live invite for their booked meeting.
        cur.execute(
            "CREATE TABLE IF NOT EXISTS emma_meetings ("
            "  id bigserial PRIMARY KEY,"
            "  dest9 text UNIQUE NOT NULL,"           # prospect identity (trailing-9), matches booked_crm
            "  source text,"                          # 'lisa1' | 'lisa4' | 'bde'
            "  bde text,"                             # booking BDE (source='bde') — drives private-BDE hiding
            "  call_id text,"                         # the booking call
            "  company text, contact_name text, domain text,"
            "  attendee_email text,"                  # editable before approval
            "  agreed_text text,"                     # free-text time agreed on the call
            "  start_at timestamptz, duration_min integer,"
            "  start_at_parsed_from text,"            # agreed phrase start_at was PARSED from (prefill provenance)
            "  cc_emails text,"                       # comma list — extra required attendees on the invite
            "  title text, notes text,"               # Emma's pre-drafted invite (editable)
            "  summary text,"                         # booking-call context shown on the card
            # draft | needs-info | approved-awaiting-creds | scheduled | accepted | declined |
            # reschedule-requested | cancelled
            "  status text NOT NULL DEFAULT 'draft',"
            "  graph_event_id text, teams_join_url text,"
            "  attendee_response text,"               # raw Graph response incl 'tentative'
            "  last_reply text, last_reply_at timestamptz,"
            "  pixel_token text UNIQUE,"              # /api/emma/px/{token} open-tracking
            "  pixel_opened_at timestamptz,"
            "  confirmation_sent_at timestamptz,"
            "  reminder_sent_at timestamptz,"
            "  reminders_enabled boolean NOT NULL DEFAULT true,"  # per-meeting reminder opt-out (console toggle)
            "  response_checked_at timestamptz,"
            "  approved_by text, approved_at timestamptz,"
            "  error text, booked_at timestamptz,"
            "  created_at timestamptz DEFAULT now(), updated_at timestamptz DEFAULT now())")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_emma_meetings_status ON emma_meetings(status)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_emma_meetings_start ON emma_meetings(start_at)")
        # v2 columns — agreed-time-parse provenance + CC list (existing installs).
        cur.execute("ALTER TABLE emma_meetings ADD COLUMN IF NOT EXISTS start_at_parsed_from text")
        cur.execute("ALTER TABLE emma_meetings ADD COLUMN IF NOT EXISTS cc_emails text")
        # v3 column — per-meeting reminders on/off (console toggle; default ON).
        cur.execute("ALTER TABLE emma_meetings ADD COLUMN IF NOT EXISTS "
                    "reminders_enabled boolean NOT NULL DEFAULT true")
        # Full journal of everything Emma does / observes per meeting.
        cur.execute(
            "CREATE TABLE IF NOT EXISTS emma_events ("
            "  id bigserial PRIMARY KEY, meeting_id bigint, kind text, detail jsonb,"
            "  created_at timestamptz DEFAULT now())")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_emma_events_meeting ON emma_events(meeting_id, created_at)")
        # Single-row control: self-throttle watermarks for the tick's sub-tasks.
        cur.execute(
            "CREATE TABLE IF NOT EXISTS emma_control ("
            "  id integer PRIMARY KEY, sync_at timestamptz, responses_at timestamptz,"
            "  inbox_scanned_at timestamptz, updated_at timestamptz DEFAULT now())")
        cur.execute("INSERT INTO emma_control (id) VALUES (1) ON CONFLICT (id) DO NOTHING")
        # ABSORB the legacy meeting_invites queue (prior stopped run): explicit send requests that
        # were parked as 'pending-creds' become approved-awaiting-creds rows — they were human-
        # requested sends, so they keep their approval. Idempotent (dest9 conflict = skip).
        cur.execute("SELECT to_regclass('meeting_invites') IS NOT NULL AS t")
        if (cur.fetchone() or {}).get("t"):
            cur.execute(
                "INSERT INTO emma_meetings (dest9, source, attendee_email, start_at, title, notes,"
                "                           booked_at, status, approved_by, approved_at, pixel_token)"
                " SELECT dest9, 'bde', attendee_email, start_at, title, notes, created_at,"
                "        CASE WHEN attendee_email IS NOT NULL AND start_at IS NOT NULL"
                "             THEN 'approved-awaiting-creds' ELSE 'needs-info' END,"
                "        requested_by, created_at,"
                "        md5(random()::text || clock_timestamp()::text || dest9)"
                " FROM meeting_invites WHERE status='pending-creds' AND dest9 IS NOT NULL"
                " ON CONFLICT (dest9) DO NOTHING")
        conn.commit()
    _TABLES_READY = True


# --------------------------------------------------------------------------- #
# queue builder — one unified daily queue from all three booking sources
# --------------------------------------------------------------------------- #
# Placeholder "names" that sometimes reach source tables (old classifier runs wrote literal
# 'Unknown', uploads carry 'N/A', …). The display-name chain must skip PAST these — a card
# reading 'Unknown' is exactly the bug the chain exists to kill.
_JUNK_NAMES = ("('', 'unknown', '(unknown)', 'unknown company', 'unknown business', "
               "'n/a', 'na', 'none', 'null', '-', '--', 'not stated', 'not provided', "
               "'not specified', 'not mentioned', 'tbc', 'tbd')")


def _clean(expr: str) -> str:
    """SQL guard around a candidate display name: NULL when it's empty or a junk
    placeholder, else the trimmed value — so COALESCE chains skip past placeholders."""
    return (f"CASE WHEN lower(btrim(COALESCE({expr},''))) IN {_JUNK_NAMES} "
            f"THEN NULL ELSE btrim({expr}) END")


def _company_fallbacks(pool: ConnectionPool, d9: str, dom: str) -> tuple[str, list[str]]:
    """Server-side display-name fallback chain — a queue card must NEVER read '(unknown)'.
    Returns (extra LATERAL JOIN sql, COALESCE columns in priority order) that look up the
    best company name for a prospect: the companies table (D&B/raghav) by phone/domain,
    the master prospects DB by phone/domain, then ANY classification of the same number
    whose transcript named the company. `d9`/`dom` are SQL expressions for the prospect's
    trailing-9 number and bare domain. Each lookup is included only where its tables exist
    (local DBs differ from cloud), so every caller degrades cleanly."""
    joins, cols = [], []
    if _tables_exist(pool, "companies"):
        joins.append(f"""
          LEFT JOIN LATERAL (SELECT co.company_name FROM companies co
                             WHERE {_clean('co.company_name')} IS NOT NULL
                               AND (co.phone_norm = {d9}
                                    OR (({dom}) IS NOT NULL AND co.domain = ({dom})))
                             ORDER BY (co.phone_norm = {d9}) DESC, co.id LIMIT 1) nf_co ON true""")
        cols.append("nf_co.company_name")
    if _tables_exist(pool, "prospects"):
        joins.append(f"""
          LEFT JOIN LATERAL (SELECT pr.business_name FROM prospects pr
                             WHERE {_clean('pr.business_name')} IS NOT NULL
                               AND (pr.phones_norm @> ARRAY[{d9}]
                                    OR (({dom}) IS NOT NULL AND pr.domain = ({dom})))
                             ORDER BY (pr.phones_norm @> ARRAY[{d9}]) DESC, pr.id LIMIT 1) nf_pr ON true""")
        cols.append("nf_pr.business_name")
    if _tables_exist(pool, "calls", "classifications"):
        joins.append(f"""
          LEFT JOIN LATERAL (SELECT cl2.prospect_company FROM classifications cl2
                             JOIN calls c2 ON c2.call_id = cl2.call_id
                             WHERE right(regexp_replace(COALESCE(c2.dest_number,''),'[^0-9]','','g'),9) = {d9}
                               AND {_clean('cl2.prospect_company')} IS NOT NULL
                             ORDER BY cl2.classified_at DESC NULLS LAST LIMIT 1) nf_cl ON true""")
        cols.append("nf_cl.prospect_company")
    return "".join(joins), cols


def sync_queue(pool: ConnectionPool, settings: Settings, *, min_interval_seconds: int = 60,
               force: bool = False) -> dict:
    """Pre-draft an invite for booked meetings (ALL Lisa-1 + Lisa-4 bookings; human-BDE
    bookings only when QUALIFIED and RECENT — see the human branch below) into
    emma_meetings. Drafts refresh from source-of-truth each pass; rows a human has already
    approved (or beyond) are NEVER touched. Complete drafts (time + email) sit in 'draft'
    (pending approval); incomplete ones in 'needs-info'. Idempotent; self-throttled."""
    ensure_emma_tables(pool)
    if not force:
        c = _row1(pool, "SELECT sync_at FROM emma_control WHERE id=1")
        if c and c.get("sync_at") and c["sync_at"] > datetime.now(timezone.utc) - timedelta(seconds=min_interval_seconds):
            return {"skipped": "throttled"}
    _exec(pool, "UPDATE emma_control SET sync_at=now(), updated_at=now() WHERE id=1")
    # --- Lisa lines (Lisa-1 marketing + Lisa-4 websites) — origin of truth on overlap ------- #
    lisa: int | str = "skipped (no lisa tables here)"
    if _tables_exist(pool, "lisa_calls", "lisa_briefs", "lisa_dm", "lisa4_pool", "calendar_events"):
        # Display-name rule: never '(unknown)'. Chain = Lisa call/pool/brief company →
        # companies/prospects/classifications lookups → the CONTACT NAME (a person's name
        # beats '(unknown)') → the dialled number itself as the absolute last resort.
        nf_join, nf_cols = _company_fallbacks(pool, "b.dest9", "COALESCE(lp.domain, lb.domain)")
        lisa_company = "COALESCE(" + ", ".join(
            [_clean("b.company_name"), _clean("lp.company"), _clean("lb.company_name")]
            + nf_cols + [_clean("b.prospect_name"), "'0'||b.dest9"]) + ")"
        # Line attribution: match against the FULL Lisa-4 caller-ID registry (all lines Lisa-4 has ever owned),
        # not just the original 0256 — otherwise the newer L4 lines (Buraq/ZS etc.) mis-tag as lisa1.
        from .lisa4 import L4_LINE_RX  # single source of truth for the Lisa-4 line set
        lisa = _exec(pool, f"""
        WITH booked AS (
          SELECT DISTINCT ON (dest9) dest9, call_id, company_name, prospect_name,
                 agreed_day_time, created_at AS booked_at, confirmed_email, prospect_email,
                 -- business line: Lisa-4 sells WEBSITES on any of her registered lines; everything else is
                 -- Lisa-1 selling organic & paid marketing (same rule as the Booked CRM). Match EITHER leg
                 -- against the full L4 caller-ID registry so inbound call-backs attribute to the right line.
                 (COALESCE(from_number,'') ~ '{L4_LINE_RX}'
                  OR COALESCE(to_number,'') ~ '{L4_LINE_RX}') AS is_lisa4
          FROM lisa_calls
          WHERE COALESCE(meeting_agreed,false) AND dest9 IS NOT NULL
            AND created_at > now() - interval '60 days'
          ORDER BY dest9, created_at ASC),
        src AS (
          SELECT b.dest9, CASE WHEN b.is_lisa4 THEN 'lisa4' ELSE 'lisa1' END AS source, b.call_id,
                 {lisa_company} AS company,
                 NULLIF(b.prospect_name,'') AS contact_name,
                 COALESCE(lp.domain, lb.domain) AS domain,
                 COALESCE(NULLIF(b.confirmed_email,''), NULLIF(b.prospect_email,''),
                          NULLIF(lb.brief->>'email',''), NULLIF(lb.brief->>'prospect_email',''),
                          NULLIF(ld.dm_email,'')) AS email,
                 NULLIF(b.agreed_day_time,'') AS agreed_text, b.booked_at, b.is_lisa4,
                 ce.start_at AS meeting_at,
                 (SELECT left(call_summary, 600) FROM lisa_calls WHERE call_id=b.call_id) AS summary
          FROM booked b
          LEFT JOIN lisa4_pool lp ON lp.dest9=b.dest9
          LEFT JOIN lisa_briefs lb ON lb.dest9=b.dest9
          LEFT JOIN lisa_dm ld ON ld.domain=COALESCE(lp.domain, lb.domain){nf_join}
          LEFT JOIN LATERAL (SELECT start_at FROM calendar_events ce
                             WHERE ce.dest_number LIKE '%%'||b.dest9
                               AND ce.type IN ('reveal','meeting') AND ce.status <> 'cancelled'
                             ORDER BY (ce.status='pending') DESC, ce.start_at DESC LIMIT 1) ce ON true)
        INSERT INTO emma_meetings (dest9, source, call_id, company, contact_name, domain,
                                   attendee_email, agreed_text, start_at, duration_min,
                                   title, notes, summary, booked_at, status, pixel_token)
        SELECT s.dest9, s.source, s.call_id, s.company, s.contact_name, s.domain,
               s.email, s.agreed_text, s.meeting_at, %(dur)s,
               CASE WHEN s.is_lisa4 THEN 'Website reveal — '||s.company||' × Traffic Radius'
                    ELSE 'Strategy session — '||s.company||' × Traffic Radius' END,
               'Booked by '||CASE WHEN s.is_lisa4 THEN 'Lisa (websites line)'
                                  ELSE 'Lisa (marketing line)' END
               ||COALESCE('. Agreed on the call: '||s.agreed_text, '')||'.',
               s.summary, s.booked_at,
               CASE WHEN s.meeting_at IS NOT NULL AND s.email IS NOT NULL
                    THEN 'draft' ELSE 'needs-info' END,
               md5(random()::text || clock_timestamp()::text || s.dest9)
        FROM src s
        ON CONFLICT (dest9) DO UPDATE SET
          source=EXCLUDED.source, call_id=EXCLUDED.call_id, company=EXCLUDED.company,
          contact_name=COALESCE(EXCLUDED.contact_name, emma_meetings.contact_name),
          domain=COALESCE(EXCLUDED.domain, emma_meetings.domain),
          summary=COALESCE(EXCLUDED.summary, emma_meetings.summary),
          title=COALESCE(emma_meetings.title, EXCLUDED.title),
          notes=COALESCE(emma_meetings.notes, EXCLUDED.notes),
          agreed_text=COALESCE(EXCLUDED.agreed_text, emma_meetings.agreed_text),
          attendee_email=COALESCE(emma_meetings.attendee_email, EXCLUDED.attendee_email),
          start_at=COALESCE(emma_meetings.start_at, EXCLUDED.start_at),
          booked_at=EXCLUDED.booked_at,
          status=CASE WHEN COALESCE(emma_meetings.start_at, EXCLUDED.start_at) IS NOT NULL
                       AND COALESCE(emma_meetings.attendee_email, EXCLUDED.attendee_email) IS NOT NULL
                      THEN 'draft' ELSE 'needs-info' END,
          updated_at=now()
        WHERE emma_meetings.status IN ('draft','needs-info')""",
        {"dur": int(getattr(settings, "emma_default_duration_min", 45))})
    # --- human-BDE bookings (classifications.meeting_booked ⋈ calls) ----------------------- #
    # Lisa is origin-of-truth on overlap: an existing lisa1/lisa4 row is never re-sourced 'bde'
    # (the conflict-update below only touches rows that are ALREADY source='bde' drafts).
    # Raj's rule: a human-BDE meeting enters Emma's queue ONLY when
    #   (a) BOOKED — the IDENTICAL canonical predicate the funnel aggregate/leaderboard uses
    #       (aggregate.py meetings_booked): connected (answered + real talk time + not a
    #       voicemail), a NEW booking (firm, or tentative WITH a specific agreed time),
    #       honouring BDM booking_outcome overrides ('counts'/'not_booking' definitive,
    #       'confirmation'/'rescheduled' force those exclusions), EXCLUDING confirmation-only
    #       and reschedule calls, and deduped to the FIRST booking per prospect/company
    #       (booking_already_exists + prior-booking-signal NOT EXISTS) — 'counts' bypasses
    #       the dedupe. Keep this in lockstep with aggregate.py: the leaderboard's
    #       "Meeting Booked" and Emma's queue must never disagree on what a booking is;
    #   (b) QUALIFIED — same predicate the funnel uses: a BDM/admin override
    #       (qualification_overrides.qualified) is DEFINITIVE both ways (true includes,
    #       false excludes, even against the AI verdict); with no override, the AI verdict
    #       (classifications.qualified) decides; AND
    #   (c) RECENT — the booking call is within the last 14 days OR the meeting time
    #       (best calendar_events match) is in the future.
    # Lisa branches stay all-bookings. The same filtered set drives BOTH the upsert and the
    # purge below, so they can never drift apart.
    human: int | str = "skipped (no funnel tables here)"
    purged: int | str = 0
    if _tables_exist(pool, "calls", "classifications", "qualification_overrides",
                     "transcripts", "calendar_events"):
        # Same never-'(unknown)' rule as the Lisa branch: classification company →
        # companies/prospects/any-classification lookups → contact name → dialled number.
        nf_join, nf_cols = _company_fallbacks(pool, "h.dest9", "NULLIF(h.prospect_website,'')")
        bde_company = "COALESCE(" + ", ".join(
            [_clean("h.prospect_company")] + nf_cols
            + [_clean("h.prospect_contact_name"), "'0'||h.dest9"]) + ")"
        human_src = f"""
        WITH human AS (
          SELECT DISTINCT ON (dest9) * FROM (
            SELECT right(regexp_replace(COALESCE(c.dest_number,''),'[^0-9]','','g'),9) AS dest9,
                   c.call_id, c.started_at AS booked_at,
                   COALESCE(c.bde_name, c.bde_extension) AS bde,
                   cl.prospect_company, cl.prospect_contact_name, cl.prospect_email,
                   cl.prospect_website, cl.meeting_datetime, cl.problem_summary
            FROM calls c
            JOIN classifications cl ON cl.call_id = c.call_id
            LEFT JOIN qualification_overrides qo ON qo.call_id = c.call_id
            WHERE c.in_scope
              AND NOT COALESCE(cl.not_a_prospect, false)
              -- (a) BOOKED: canonical funnel predicate — VERBATIM from aggregate.py
              -- meetings_booked. Connected = answered + real talk time + not a voicemail.
              AND c.answered AND c.talk_seconds >= %(rpc_min)s AND COALESCE(cl.call_outcome, '') <> 'voicemail'
              -- A booking = firm OR a TENTATIVE meeting WITH a SPECIFIC agreed time (clock
              -- time / am-pm / noon). Honours a BDM booking-outcome override.
              AND (CASE WHEN qo.booking_outcome='counts' THEN true
                        WHEN qo.booking_outcome='not_booking' THEN false
                        ELSE (cl.meeting_booked OR (cl.booking_status='tentative' AND cl.meeting_datetime ~* '[0-9]:[0-9]|[0-9][[:space:]]*[ap][.]?m|noon|midday' AND NOT COALESCE(cl.callback_requested,false))) END)
              AND NOT (CASE WHEN qo.booking_outcome='confirmation' THEN true
                            WHEN qo.booking_outcome IN ('counts','not_booking','rescheduled') THEN false
                            ELSE COALESCE(cl.meeting_confirmation, false) END)
              AND NOT (CASE WHEN qo.booking_outcome='rescheduled' THEN true
                            WHEN qo.booking_outcome IN ('counts','not_booking','confirmation') THEN false
                            ELSE COALESCE(cl.meeting_rescheduled, false) END)
              -- BDM 'counts' override is DEFINITIVE: bypasses the referral guard AND the
              -- once-per-prospect dedupe (same as the funnel aggregate).
              AND (qo.booking_outcome='counts' OR (NOT COALESCE(cl.booking_already_exists, false)
              AND NOT EXISTS (
                    SELECT 1 FROM calls pc JOIN classifications pcl ON pcl.call_id = pc.call_id
                    WHERE pc.in_scope
                      -- same PROSPECT: same phone number OR same COMPANY (domain/name-slug key)
                      AND (right(regexp_replace(pc.dest_number,'[^0-9]','','g'),9) = right(regexp_replace(c.dest_number,'[^0-9]','','g'),9)
                           OR (cl.company_key IS NOT NULL AND pcl.company_key IS NOT NULL AND pcl.company_key = cl.company_key))
                      AND pc.started_at < c.started_at
                      AND pc.answered AND pc.talk_seconds >= %(rpc_min)s AND COALESCE(pcl.call_outcome,'') <> 'voicemail'
                      -- ANY prior booking signal (booking, confirmation OR reschedule) means a
                      -- booking already exists for this prospect → later bookings are repeats.
                      AND (pcl.meeting_booked OR COALESCE(pcl.meeting_confirmation,false) OR COALESCE(pcl.meeting_rescheduled,false)
                           OR (pcl.booking_status='tentative' AND pcl.meeting_datetime ~* '[0-9]:[0-9]|[0-9][[:space:]]*[ap][.]?m|noon|midday' AND NOT COALESCE(pcl.callback_requested,false))))))
              -- (b) QUALIFIED: override wins both ways; else the AI verdict.
              AND COALESCE(qo.qualified, cl.qualified, false)
              AND c.started_at > now() - interval '60 days'
          ) h WHERE dest9 <> ''
          ORDER BY dest9, booked_at DESC),
        src AS (
          SELECT h.dest9, h.call_id, h.booked_at, h.bde,
                 {bde_company} AS company,
                 NULLIF(h.prospect_contact_name,'') AS contact_name,
                 NULLIF(h.prospect_website,'') AS domain,
                 NULLIF(h.prospect_email,'') AS email,
                 NULLIF(h.meeting_datetime,'') AS agreed_text,
                 COALESCE(NULLIF(left(h.problem_summary,600),''), left(t.summary,600)) AS summary,
                 ce.start_at AS meeting_at
          FROM human h
          LEFT JOIN transcripts t ON t.call_id = h.call_id{nf_join}
          LEFT JOIN LATERAL (SELECT start_at FROM calendar_events ce
                             WHERE right(regexp_replace(COALESCE(ce.dest_number,''),'[^0-9]','','g'),9)=h.dest9
                               AND ce.type IN ('meeting','reveal') AND ce.status <> 'cancelled'
                             ORDER BY (ce.status='pending') DESC, ce.start_at DESC LIMIT 1) ce ON true
          -- (c) RECENT: fresh call OR a future meeting time keeps it in the queue.
          WHERE h.booked_at > now() - interval '14 days' OR ce.start_at > now())"""
        human = _exec(pool, human_src + """
        INSERT INTO emma_meetings (dest9, source, bde, call_id, company, contact_name, domain,
                                   attendee_email, agreed_text, start_at, duration_min,
                                   title, notes, summary, booked_at, status, pixel_token)
        SELECT s.dest9, 'bde', s.bde, s.call_id, s.company, s.contact_name, s.domain,
               s.email, s.agreed_text, s.meeting_at, %(dur)s,
               'Strategy session — '||s.company||' × Traffic Radius',
               'Booked by '||COALESCE(s.bde,'a BDE')
               ||COALESCE('. Agreed on the call: '||s.agreed_text, '')||'.',
               s.summary, s.booked_at,
               CASE WHEN s.meeting_at IS NOT NULL AND s.email IS NOT NULL
                    THEN 'draft' ELSE 'needs-info' END,
               md5(random()::text || clock_timestamp()::text || s.dest9)
        FROM src s
        ON CONFLICT (dest9) DO UPDATE SET
          call_id=EXCLUDED.call_id, bde=EXCLUDED.bde, company=EXCLUDED.company,
          contact_name=COALESCE(EXCLUDED.contact_name, emma_meetings.contact_name),
          domain=COALESCE(EXCLUDED.domain, emma_meetings.domain),
          summary=COALESCE(EXCLUDED.summary, emma_meetings.summary),
          title=COALESCE(emma_meetings.title, EXCLUDED.title),
          notes=COALESCE(emma_meetings.notes, EXCLUDED.notes),
          agreed_text=COALESCE(EXCLUDED.agreed_text, emma_meetings.agreed_text),
          attendee_email=COALESCE(emma_meetings.attendee_email, EXCLUDED.attendee_email),
          start_at=COALESCE(emma_meetings.start_at, EXCLUDED.start_at),
          booked_at=EXCLUDED.booked_at,
          status=CASE WHEN COALESCE(emma_meetings.start_at, EXCLUDED.start_at) IS NOT NULL
                       AND COALESCE(emma_meetings.attendee_email, EXCLUDED.attendee_email) IS NOT NULL
                      THEN 'draft' ELSE 'needs-info' END,
          updated_at=now()
        WHERE emma_meetings.source='bde' AND emma_meetings.status IN ('draft','needs-info')""",
        {"dur": int(getattr(settings, "emma_default_duration_min", 45)),
         "rpc_min": settings.rpc_min_talk_seconds})
        # Purge now-stale drafts: a human-BDE row whose underlying booking no longer passes
        # the qualified+recent filter above leaves the queue. Rows a human has already
        # approved (or beyond) are NEVER touched — only 'draft'/'needs-info' are purged.
        purged = _exec(pool, human_src + """
        DELETE FROM emma_meetings em
        WHERE em.source='bde' AND em.status IN ('draft','needs-info')
          AND NOT EXISTS (SELECT 1 FROM src s WHERE s.dest9 = em.dest9)""",
        {"rpc_min": settings.rpc_min_talk_seconds})
    # --- never-'(unknown)' repair pass ------------------------------------------------ #
    # Legacy rows (any status) can still carry a placeholder company ('(unknown)',
    # 'Unknown', 'N/A', …) AND have it baked into the title (the conflict-update keeps
    # old titles). Re-resolve them with the same fallback chain — current GOOD company
    # first (so a human-edited company patches the title rather than being overwritten),
    # then companies/prospects/classifications, then the contact name, then the dialled
    # number — and patch the old placeholder inside the title. Display-only; idempotent.
    nf_join, nf_cols = _company_fallbacks(pool, "em2.dest9", "NULLIF(em2.domain,'')")
    name_expr = "COALESCE(" + ", ".join(
        [_clean("em2.company")] + nf_cols
        + [_clean("em2.contact_name"), "'0'||em2.dest9"]) + ")"
    # `seg` = the company segment baked into an auto-generated title ('… — <seg> × Traffic
    # Radius') — the conflict-update keeps OLD titles, so a placeholder can survive there
    # even after the company column itself was fixed.
    seg_expr = "substring(em2.title from '— (.*) ×')"
    renamed = _exec(pool, f"""
        UPDATE emma_meetings em SET
          company = f.name,
          title   = CASE
                      WHEN f.seg IS NOT NULL AND lower(btrim(f.seg)) IN {_JUNK_NAMES}
                        THEN replace(em.title, '— '||f.seg||' ×', '— '||f.name||' ×')
                      WHEN COALESCE(em.company,'') <> '' AND em.title IS NOT NULL
                           AND position(em.company in em.title) > 0 AND em.company <> f.name
                        THEN replace(em.title, em.company, f.name)
                      WHEN em.title IS NOT NULL AND position('(unknown)' in em.title) > 0
                        THEN replace(em.title, '(unknown)', f.name)
                      ELSE em.title END,
          updated_at = now()
        FROM (SELECT em2.id, {name_expr} AS name, {seg_expr} AS seg
              FROM emma_meetings em2{nf_join}
              WHERE lower(btrim(COALESCE(em2.company,''))) IN {_JUNK_NAMES}
                 OR em2.title LIKE '%(unknown)%'
                 OR lower(btrim(COALESCE({seg_expr},'~'))) IN {_JUNK_NAMES}) f
        WHERE f.id = em.id""")
    # --- agreed-time PREFILL (doubles as the backfill for pre-existing rows) --------- #
    # A pre-approval row with an agreed phrase ("Monday at 11 am") but no concrete time
    # gets start_at PREFILLED from the parsed phrase — next occurrence relative to
    # booked_at, Melbourne wall-clock (morning=10:00, arvo=14:00). Status stays in the
    # pre-approval trays: Kiran sees the parsed time already filled in and adjusts if
    # wrong; the card shows 'parsed from: "<agreed text>"' via start_at_parsed_from.
    # Rows a human approved (or beyond) are NEVER touched. Idempotent: only fires while
    # start_at IS NULL, so a human clearing/overriding the time is never fought.
    parsed = 0
    for r in _rows(pool, "SELECT id, agreed_text, booked_at FROM emma_meetings"
                         " WHERE status IN ('draft','needs-info') AND start_at IS NULL"
                         " AND COALESCE(agreed_text,'') <> ''"):
        try:
            dt = parse_agreed_time(r["agreed_text"], r.get("booked_at"), settings.tz)
        except Exception as exc:
            log.warning("emma_agreed_parse_failed", meeting_id=r["id"], error=str(exc)[:120])
            continue
        if not dt:
            continue
        n = _exec(pool, "UPDATE emma_meetings SET start_at=%s, start_at_parsed_from=agreed_text,"
                        " status=CASE WHEN attendee_email IS NOT NULL"
                        "             THEN 'draft' ELSE 'needs-info' END, updated_at=now()"
                        " WHERE id=%s AND status IN ('draft','needs-info') AND start_at IS NULL",
                  (dt, r["id"]))
        if n:
            parsed += 1
            _log_event(pool, r["id"], "time-parsed", start_at=dt.isoformat(),
                       from_text=(r["agreed_text"] or "")[:200])
    return {"lisa": lisa, "human": human, "purged": purged, "renamed": renamed, "parsed": parsed}


# --------------------------------------------------------------------------- #
# Microsoft Graph client (app-only / client_credentials) — COMPLETE, creds-gated
# --------------------------------------------------------------------------- #
_TOKEN: dict = {}   # process-wide token cache: {"val": str, "exp": datetime}


def missing_creds(settings: Settings) -> list[str]:
    """Env vars still required before Emma can actually send. Empty list = ready."""
    need = {"MS_TENANT_ID": settings.ms_tenant_id, "MS_CLIENT_ID": settings.ms_client_id,
            "MS_CLIENT_SECRET": settings.ms_client_secret,
            "SCHEDULER_MAILBOX": settings.scheduler_mailbox}
    return [k for k, v in need.items() if not (v or "").strip()]


def graph_token(settings: Settings) -> str:
    """App-only access token via client_credentials (scope=graph/.default), cached to expiry."""
    now = datetime.now(timezone.utc)
    if _TOKEN.get("val") and _TOKEN.get("exp") and _TOKEN["exp"] > now:
        return _TOKEN["val"]
    data = urllib.parse.urlencode({
        "grant_type": "client_credentials",
        "client_id": settings.ms_client_id,
        "client_secret": settings.ms_client_secret,
        "scope": "https://graph.microsoft.com/.default"}).encode()
    req = urllib.request.Request(
        f"{LOGIN_BASE}/{settings.ms_tenant_id}/oauth2/v2.0/token", data=data, method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            tok = json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"Graph token failed ({exc.code}): {exc.read().decode()[:300]}") from None
    _TOKEN["val"] = tok["access_token"]
    _TOKEN["exp"] = now + timedelta(seconds=max(60, int(tok.get("expires_in", 3600)) - 120))
    return _TOKEN["val"]


def _graph(settings: Settings, method: str, path: str, body: dict | None = None) -> dict:
    """One Graph call. `path` starts with '/'. Returns {} on empty (202/204) responses."""
    req = urllib.request.Request(
        f"{GRAPH_BASE}{path}",
        data=(json.dumps(body).encode() if body is not None else None), method=method,
        headers={"Authorization": f"Bearer {graph_token(settings)}",
                 "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=45) as resp:
            t = resp.read().decode()
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"Graph {method} {path} -> {exc.code}: {exc.read().decode()[:300]}") from None
    return json.loads(t) if t.strip() else {}


def _event_times(settings: Settings, start_at: datetime, duration_min: int) -> tuple[dict, dict]:
    """Graph start/end blocks in the operation's IANA timezone."""
    local = start_at.astimezone(ZoneInfo(settings.tz))
    end = local + timedelta(minutes=max(5, int(duration_min or 45)))
    fmt = "%Y-%m-%dT%H:%M:%S"
    return ({"dateTime": local.strftime(fmt), "timeZone": settings.tz},
            {"dateTime": end.strftime(fmt), "timeZone": settings.tz})


def _attendee_blocks(attendee_email: str, attendee_name: str | None,
                     cc_emails: list[str] | None) -> list[dict]:
    """Graph attendees array: the prospect + every CC address, ALL as required attendees
    (owner spec — CC people are expected in the room, not FYI'd)."""
    addr: dict = {"address": attendee_email}
    if attendee_name:
        addr["name"] = attendee_name
    out = [{"emailAddress": addr, "type": "required"}]
    for e in cc_emails or []:
        if e.strip().lower() != (attendee_email or "").strip().lower():
            out.append({"emailAddress": {"address": e.strip()}, "type": "required"})
    return out


def graph_create_event(settings: Settings, *, subject: str, start_at: datetime, duration_min: int,
                       attendee_email: str, attendee_name: str | None = None,
                       body_html: str = "", cc_emails: list[str] | None = None) -> dict:
    """POST /users/{SCHEDULER_MAILBOX}/calendar/events with the prospect (+ any CC extras) as
    required attendees and isOnlineMeeting=true (Teams). Returns {event_id, join_url, web_link}."""
    start, end = _event_times(settings, start_at, duration_min)
    ev = _graph(settings, "POST", f"/users/{settings.scheduler_mailbox}/calendar/events", {
        "subject": subject,
        "body": {"contentType": "HTML", "content": body_html or ""},
        "start": start, "end": end,
        "attendees": _attendee_blocks(attendee_email, attendee_name, cc_emails),
        "isOnlineMeeting": True,
        "onlineMeetingProvider": "teamsForBusiness"})
    return {"event_id": ev.get("id"),
            "join_url": (ev.get("onlineMeeting") or {}).get("joinUrl"),
            "web_link": ev.get("webLink")}


def graph_patch_event_time(settings: Settings, event_id: str, start_at: datetime,
                           duration_min: int) -> None:
    """PATCH the SAME event to a new slot — Graph emails the update to the attendee itself."""
    start, end = _event_times(settings, start_at, duration_min)
    _graph(settings, "PATCH", f"/users/{settings.scheduler_mailbox}/events/{event_id}",
           {"start": start, "end": end})


def graph_patch_event_attendees(settings: Settings, event_id: str, attendees: list[dict]) -> None:
    """PATCH the SAME event with a new attendees array (prospect + CC list) — Graph sends the
    updated invite itself. The array REPLACES the event's list, so callers always pass the
    full set, prospect included."""
    _graph(settings, "PATCH", f"/users/{settings.scheduler_mailbox}/events/{event_id}",
           {"attendees": attendees})


def graph_cancel_event(settings: Settings, event_id: str, comment: str = "") -> None:
    """POST /events/{id}/cancel — Graph sends the cancellation notice to the attendee."""
    _graph(settings, "POST", f"/users/{settings.scheduler_mailbox}/events/{event_id}/cancel",
           {"comment": comment or "This meeting has been cancelled."})


def graph_event_response(settings: Settings, event_id: str, attendee_email: str) -> str | None:
    """The attendee's current RSVP from the live event: 'accepted' | 'declined' | 'tentative' |
    'none' (not yet responded), or None when the attendee isn't on the event."""
    ev = _graph(settings, "GET",
                f"/users/{settings.scheduler_mailbox}/events/{event_id}"
                f"?{urllib.parse.urlencode({'$select': 'attendees'})}")
    want = (attendee_email or "").lower()
    for a in ev.get("attendees") or []:
        if ((a.get("emailAddress") or {}).get("address") or "").lower() == want:
            resp = ((a.get("status") or {}).get("response") or "none")
            return {"accepted": "accepted", "declined": "declined",
                    "tentativelyAccepted": "tentative"}.get(resp, "none")
    return None


def graph_list_inbox(settings: Settings, since: datetime, top: int = 40) -> list[dict]:
    """Recent scheduler-mailbox inbox messages (from/subject/preview/received) since a watermark —
    how Emma reads prospect replies (reschedule requests etc.)."""
    qs = urllib.parse.urlencode({
        "$top": str(top), "$orderby": "receivedDateTime desc",
        "$select": "from,subject,bodyPreview,receivedDateTime",
        "$filter": "receivedDateTime ge " + since.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")})
    out = _graph(settings, "GET", f"/users/{settings.scheduler_mailbox}/mailFolders/inbox/messages?{qs}")
    msgs = []
    for m in out.get("value") or []:
        msgs.append({
            "from": ((m.get("from") or {}).get("emailAddress") or {}).get("address") or "",
            "subject": m.get("subject") or "",
            "preview": m.get("bodyPreview") or "",
            "received": m.get("receivedDateTime") or ""})
    return msgs


def graph_send_mail(settings: Settings, to_email: str, subject: str, html: str) -> None:
    """POST /users/{SCHEDULER_MAILBOX}/sendMail — Emma's confirmation + reminder emails."""
    _graph(settings, "POST", f"/users/{settings.scheduler_mailbox}/sendMail", {
        "message": {"subject": subject,
                    "body": {"contentType": "HTML", "content": html},
                    "toRecipients": [{"emailAddress": {"address": to_email}}]},
        "saveToSentItems": True})


# --------------------------------------------------------------------------- #
# emails Emma writes (confirmation with open-pixel; reminders)
# --------------------------------------------------------------------------- #
def _confirmation_html(settings: Settings, row: dict) -> str:
    when = _fmt_local(settings, row.get("start_at"))
    join = row.get("teams_join_url") or ""
    name = (row.get("contact_name") or "").split(" ")[0] or "there"
    pixel = ""
    base = (getattr(settings, "public_base_url", "") or "").rstrip("/")
    if base and row.get("pixel_token"):
        # open-tracking pixel — served by GET /api/emma/px/{token}; opens are SIGNAL, not proof.
        pixel = (f'<img src="{base}/api/emma/px/{row["pixel_token"]}" width="1" height="1" '
                 f'alt="" style="display:none">')
    join_html = (f'<p><a href="{join}">Join the Microsoft Teams meeting</a></p>' if join else "")
    return (f"<p>Hi {name},</p>"
            f"<p>Emma Collins here from Traffic Radius — your meeting is locked in.</p>"
            f"<p><b>{row.get('title') or 'Your meeting with Traffic Radius'}</b><br>"
            f"{when} ({_tz_label(settings)})</p>"
            f"{join_html}"
            f"<p>If that time no longer works, just reply to this email and I'll move it.</p>"
            f"<p>Talk soon,<br>Emma Collins<br>Traffic Radius</p>{pixel}")


def _reminder_html(settings: Settings, row: dict, flavour: str) -> str:
    when = _fmt_local(settings, row.get("start_at"))
    join = row.get("teams_join_url") or ""
    name = (row.get("contact_name") or "").split(" ")[0] or "there"
    lead = ("A quick reminder before the weekend — we're meeting on Monday."
            if flavour == "friday-for-monday" else "A quick reminder — we're meeting today.")
    join_html = (f'<p><a href="{join}">Join the Microsoft Teams meeting</a></p>' if join else "")
    return (f"<p>Hi {name},</p><p>{lead}</p>"
            f"<p><b>{row.get('title') or 'Your meeting with Traffic Radius'}</b><br>"
            f"{when} ({_tz_label(settings)})</p>{join_html}"
            f"<p>Need to move it? Just reply to this email.</p>"
            f"<p>See you then,<br>Emma Collins<br>Traffic Radius</p>")


# --------------------------------------------------------------------------- #
# invite BODY templates — the branded HTML Graph puts in the meeting invitation
# --------------------------------------------------------------------------- #
# TWO variants, chosen by booking source: 'website' (Lisa-4 website reveals) vs
# 'marketing' (Lisa-1 marketing + human-BDE bookings). Email-safe: table layout, inline
# CSS only, no external fonts, Outlook-tolerant. Traffic Radius branding.
#
# STRICT WHITELIST — render_invite_html() may ONLY receive
#   {first_name, company, meeting_dt_melbourne, duration, variant}
# and NOTHING else (no notes/status/internal fields). Enforced by its keyword-only
# signature. The Teams join link and the open-tracking pixel are DELIBERATELY not template
# inputs: the template reserves an opaque placement marker for each, and the caller substitutes
# the real values via _finalise_invite() AFTER rendering — so no runtime/internal value can
# ever leak into the template's content.
_BRAND_INK = "#1b2532"       # Traffic Radius navy — the hero band
_BRAND_INK_SOFT = "#6a778a"  # muted slate for captions/footers
_BRAND_BLUE = "#33a7dc"      # logo blue accent (buttons + rules)
_BRAND_GREEN = "#7cc242"     # logo green accent
_BRAND_TEXT = "#33404f"      # body copy
_BRAND_PANEL = "#f4f7fb"     # light panel behind the date-time line
_BRAND_LINE = "#dce3ec"
# Hosted wordmark asset (served publicly at /logo.png, auth-exempt) under the public base.
_LOGO_URL = "https://3cx-sales-funnel-production.up.railway.app/logo.png"
_SITE_URL = "https://www.trafficradius.com.au"
_SITE_LABEL = "trafficradius.com.au"
# Opaque placement markers the caller fills in post-render (never template inputs).
_INVITE_JOIN_MARK = "{{__EMMA_INVITE_JOIN__}}"
_INVITE_PIXEL_MARK = "{{__EMMA_INVITE_PIXEL__}}"

# Per-variant copy (owner-approved wording).
_INVITE_COPY = {
    "website": {
        "title": "Website Preview Session",
        "eyebrow": "You&rsquo;re booked in",
        "intro": "Thanks for booking a Website Preview Session with Traffic Radius. "
                 "It&rsquo;s a quick, no-pressure look at something we&rsquo;ve already built for you.",
        "heading": "What to expect",
        "bullets": [
            "A live screen-share walkthrough with our web designer",
            "A website we&rsquo;ve already built &mdash; designed specifically for {company}",
            "Completely free &mdash; nothing to prepare, and nothing to sign",
        ],
    },
    "marketing": {
        "title": "Digital Strategy Session",
        "eyebrow": "You&rsquo;re booked in",
        "intro": "Thanks for booking a Digital Strategy Session with Traffic Radius. "
                 "Here&rsquo;s what we&rsquo;ll walk through together.",
        "heading": "What we&rsquo;ll cover",
        "bullets": [
            "Where {company} is winning &mdash; and losing &mdash; visibility online",
            "The gaps quietly sending ready-to-buy customers to your competitors",
            "A clear, no-obligation plan to close those gaps",
        ],
    },
}


def _spell_invite_dt(dt) -> str:
    """The meeting time spelled out for the invite: 'Thursday 14 August 2026 · 10:30 AM'.
    Accepts a (Melbourne-local) datetime, or a plain string used verbatim (e.g. when a time
    isn't set yet for a preview). Returned value is HTML-escaped."""
    if not isinstance(dt, datetime):
        return _esc(str(dt))
    hour12 = dt.hour % 12 or 12
    ampm = "AM" if dt.hour < 12 else "PM"
    return _esc(f"{dt.strftime('%A')} {dt.day} {dt.strftime('%B %Y')} "
                f"· {hour12}:{dt.minute:02d} {ampm}")


def _invite_bullets_html(items: list[str]) -> str:
    """A checklist block (brand-green tick + copy) as an Outlook-safe table."""
    rows = ""
    for it in items:
        rows += (
            '<tr>'
            f'<td valign="top" style="padding:0 10px 13px 0;font-family:Arial,Helvetica,sans-serif;'
            f'font-size:15px;line-height:22px;color:{_BRAND_GREEN};font-weight:700;">&#10003;</td>'
            f'<td valign="top" style="padding:0 0 13px 0;font-family:Arial,Helvetica,sans-serif;'
            f'font-size:15px;line-height:22px;color:{_BRAND_TEXT};">{it}</td>'
            '</tr>')
    return ('<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
            f'border="0">{rows}</table>')


def _invite_join_live(url: str) -> str:
    """Bulletproof brand-blue 'Join the Microsoft Teams meeting' button (URL known)."""
    href = _esc(url)
    return (
        '<table role="presentation" align="center" cellpadding="0" cellspacing="0" border="0" '
        'style="margin:2px auto 4px;"><tr>'
        f'<td align="center" bgcolor="{_BRAND_BLUE}" style="border-radius:8px;">'
        f'<a href="{href}" target="_blank" style="display:inline-block;padding:13px 32px;'
        'font-family:Arial,Helvetica,sans-serif;font-size:15px;font-weight:700;color:#ffffff;'
        'text-decoration:none;border-radius:8px;">Join the Microsoft&nbsp;Teams meeting</a>'
        '</td></tr></table>')


def _invite_join_pending() -> str:
    """The join PLACEMENT when we don't hold the URL yet — the invitation Graph sends carries
    the live Teams 'Join' button / dial-in itself (isOnlineMeeting), so we point to it."""
    return (
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">'
        '<tr><td align="center" style="padding:2px 0 0;">'
        f'<div style="display:inline-block;padding:12px 28px;border:1.5px solid {_BRAND_LINE};'
        f'border-radius:8px;font-family:Arial,Helvetica,sans-serif;font-size:14px;font-weight:700;'
        f'color:{_BRAND_INK};">Microsoft Teams meeting</div>'
        f'<div style="margin-top:9px;font-family:Arial,Helvetica,sans-serif;font-size:12.5px;'
        f'line-height:19px;color:{_BRAND_INK_SOFT};">Your <strong>Join</strong> button and dial-in '
        'details are on this calendar invitation.</div>'
        '</td></tr></table>')


def render_invite_html(*, first_name: str, company: str, meeting_dt_melbourne,
                       duration: int, variant: str) -> str:
    """THE invite-body template (strict whitelist — these 5 keyword-only inputs and no others).

    Returns a complete, email-safe HTML document (table layout, inline CSS, no external fonts)
    carrying Traffic Radius branding, with two opaque markers the caller fills in afterwards:
    _INVITE_JOIN_MARK (Teams join placement) and _INVITE_PIXEL_MARK (open-tracking pixel).
    `variant` selects the copy/timing: 'website' (15-min Website Preview Session) or anything
    else → 'marketing' (45-min Digital Strategy Session)."""
    v = "website" if variant == "website" else "marketing"
    copy = _INVITE_COPY[v]
    fn = _esc((first_name or "").strip()) or "there"
    co = _esc((company or "").strip()) or "your business"
    when = _spell_invite_dt(meeting_dt_melbourne)
    try:
        dur = int(duration)
    except (TypeError, ValueError):
        dur = 15 if v == "website" else 45
    title = copy["title"]
    bullets = _invite_bullets_html([b.replace("{company}", co) for b in copy["bullets"]])
    intro = copy["intro"].replace("{company}", co)
    preheader = _esc(f"Your {title} with Traffic Radius is booked in — {when}")
    ff = "font-family:Arial,Helvetica,sans-serif;"
    return (
        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
        f"<title>{title} &middot; Traffic Radius</title></head>"
        f"<body style=\"margin:0;padding:0;background:#eef1f6;{ff}\">"
        # hidden inbox-preview text
        f"<div style=\"display:none;max-height:0;overflow:hidden;opacity:0;\">{preheader}</div>"
        "<table role=\"presentation\" width=\"100%\" cellpadding=\"0\" cellspacing=\"0\" border=\"0\" "
        "style=\"background:#eef1f6;\"><tr><td align=\"center\" style=\"padding:26px 14px;\">"
        # ---- the card ------------------------------------------------------------------ #
        "<table role=\"presentation\" width=\"600\" cellpadding=\"0\" cellspacing=\"0\" border=\"0\" "
        "style=\"width:600px;max-width:100%;background:#ffffff;border:1px solid #e2e8f1;"
        "border-radius:14px;overflow:hidden;\">"
        # header — hosted wordmark on white, brand accent rule beneath
        "<tr><td align=\"center\" style=\"padding:26px 30px 18px;\">"
        f"<img src=\"{_LOGO_URL}\" width=\"188\" alt=\"Traffic Radius\" "
        "style=\"display:block;width:188px;max-width:70%;height:auto;border:0;\"></td></tr>"
        f"<tr><td style=\"height:4px;line-height:4px;font-size:0;background:{_BRAND_BLUE};\">&nbsp;</td></tr>"
        # hero band — brand navy, the meeting title
        f"<tr><td style=\"background:{_BRAND_INK};padding:22px 30px;\">"
        f"<div style=\"{ff}font-size:11px;font-weight:700;letter-spacing:.14em;text-transform:uppercase;"
        f"color:{_BRAND_BLUE};\">{copy['eyebrow']}</div>"
        f"<div style=\"{ff}font-size:23px;font-weight:800;color:#ffffff;margin-top:6px;"
        f"letter-spacing:-.01em;\">{title}</div></td></tr>"
        # body
        "<tr><td style=\"padding:28px 30px 6px;\">"
        f"<p style=\"{ff}font-size:16px;line-height:24px;color:{_BRAND_INK};margin:0 0 14px;\">"
        f"Hi {fn},</p>"
        f"<p style=\"{ff}font-size:15px;line-height:23px;color:{_BRAND_TEXT};margin:0 0 20px;\">"
        f"{intro}</p>"
        # date-time panel
        f"<table role=\"presentation\" width=\"100%\" cellpadding=\"0\" cellspacing=\"0\" border=\"0\" "
        f"style=\"background:{_BRAND_PANEL};border:1px solid {_BRAND_LINE};border-radius:10px;\">"
        "<tr><td style=\"padding:15px 18px;\">"
        f"<div style=\"{ff}font-size:11px;font-weight:700;letter-spacing:.1em;text-transform:uppercase;"
        f"color:{_BRAND_INK_SOFT};margin-bottom:5px;\">When</div>"
        f"<div style=\"{ff}font-size:16px;font-weight:800;color:{_BRAND_INK};line-height:22px;\">{when}</div>"
        f"<div style=\"{ff}font-size:13px;color:{_BRAND_INK_SOFT};margin-top:3px;\">"
        f"Melbourne time &middot; {dur}-minute session</div>"
        "</td></tr></table>"
        # Teams join placement (Graph injects the live link)
        "<div style=\"margin:22px 0 6px;\">" + _INVITE_JOIN_MARK + "</div>"
        # what we'll cover / what to expect
        f"<div style=\"{ff}font-size:13px;font-weight:800;letter-spacing:.02em;color:{_BRAND_INK};"
        f"margin:22px 0 13px;\">{copy['heading']}</div>"
        f"{bullets}"
        # reschedule line
        f"<p style=\"{ff}font-size:14px;line-height:22px;color:{_BRAND_TEXT};margin:20px 0 0;"
        f"padding-top:18px;border-top:1px solid {_BRAND_LINE};\">"
        "Need a different time? Just reply to this email, or text the number that called you "
        "&mdash; we&rsquo;ll take care of it.</p>"
        # signature
        f"<p style=\"{ff}font-size:14px;line-height:21px;color:{_BRAND_TEXT};margin:22px 0 4px;\">"
        f"See you then,<br><strong style=\"color:{_BRAND_INK};\">Emma Collins</strong><br>"
        f"<span style=\"color:{_BRAND_INK_SOFT};\">Scheduling Coordinator &middot; Traffic Radius</span><br>"
        f"<a href=\"{_SITE_URL}\" style=\"color:{_BRAND_BLUE};text-decoration:none;\">{_SITE_LABEL}</a></p>"
        "</td></tr>"
        # footer
        f"<tr><td style=\"padding:20px 30px 26px;border-top:1px solid #eef1f6;\">"
        f"<div style=\"{ff}font-size:11.5px;line-height:18px;color:{_BRAND_INK_SOFT};\">"
        "Traffic Radius &middot; Growing Your Business the Smart Way &middot; "
        f"<a href=\"{_SITE_URL}\" style=\"color:{_BRAND_INK_SOFT};text-decoration:underline;\">{_SITE_LABEL}</a>"
        "</div></td></tr>"
        "</table></td></tr></table>"
        + _INVITE_PIXEL_MARK +
        "</body></html>")


def _invite_pixel_img(settings: Settings, pixel_token: str | None) -> str:
    """The 1×1 open-tracking pixel <img> for the invite (existing /api/emma/px/{token} route).
    Opens are SIGNAL, not proof. Empty string when the base URL or token is missing."""
    base = (getattr(settings, "public_base_url", "") or "").rstrip("/")
    if not (base and pixel_token):
        return ""
    return (f'<img src="{base}/api/emma/px/{pixel_token}" width="1" height="1" alt="" '
            'style="display:none;max-height:1px;max-width:1px;opacity:0;overflow:hidden;">')


def _finalise_invite(html: str, *, join_url: str | None, pixel_img: str) -> str:
    """Fill the two opaque placements the template reserved: the Teams join block (live button
    when the URL is known, else the 'it's on this invitation' placement) and the open pixel."""
    join = _invite_join_live(join_url) if (join_url or "").strip() else _invite_join_pending()
    return html.replace(_INVITE_JOIN_MARK, join).replace(_INVITE_PIXEL_MARK, pixel_img or "")


def _invite_variant(row: dict) -> str:
    """WEBSITE for Lisa-4 website reveals; MARKETING for Lisa-1 marketing + human-BDE bookings."""
    return "website" if (row.get("source") == "lisa4") else "marketing"


def _invite_duration(row: dict, variant: str) -> int:
    """Invite duration: 15 min for a website preview, 45 for a marketing session (owner spec).
    sync_queue stamps every draft with the 45-min default, so a stored 45 is treated as 'unset'
    and yields the variant default; a human-set NON-45 duration always wins."""
    default = 15 if variant == "website" else 45
    d = row.get("duration_min")
    return int(d) if (d and int(d) != 45) else default


def _invite_render_for_row(settings: Settings, row: dict, *, join_url: str | None,
                           pixel_img: str) -> str:
    """Render + finalise the branded invite body for an emma_meetings row. Feeds the template
    ONLY the 5 whitelisted fields (first name, company, Melbourne time, duration, variant);
    the join link + pixel are stitched in afterwards by _finalise_invite()."""
    variant = _invite_variant(row)
    first = (row.get("contact_name") or "").split(" ")[0] or "there"
    company = row.get("company") or "your business"
    start = row.get("start_at")
    when = (start.astimezone(ZoneInfo(settings.tz)) if isinstance(start, datetime)
            else "a time we’ll confirm with you")
    html = render_invite_html(first_name=first, company=company, meeting_dt_melbourne=when,
                              duration=_invite_duration(row, variant), variant=variant)
    return _finalise_invite(html, join_url=join_url, pixel_img=pixel_img)


def invite_body_preview(settings: Settings, row: dict) -> str:
    """The invite body EXACTLY as the prospect will receive it, for the console preview pane.
    The open pixel is deliberately OMITTED (a preview render must never log a false 'open') and
    the token never leaves the server; the live Teams button shows only once the invite carries
    a Teams link."""
    return _invite_render_for_row(settings, row, join_url=row.get("teams_join_url"), pixel_img="")


# --------------------------------------------------------------------------- #
# actions (all behind the human approval gate in the dashboard)
# --------------------------------------------------------------------------- #
def _send_invite(pool: ConnectionPool, settings: Settings, mid: int) -> dict:
    """Create the Teams calendar event for an APPROVED meeting + send Emma's confirmation email
    (with the open pixel). On failure the row keeps status approved-awaiting-creds with the error
    recorded — the tick retries. Returns {status, event_id, join_url}."""
    row = _row1(pool, "SELECT * FROM emma_meetings WHERE id=%s", (mid,))
    if not row:
        raise LookupError("meeting not found")
    # Branded invite body (the HTML the prospect gets in the meeting invitation) from the
    # source-selected template: WEBSITE (Lisa-4, 15-min) vs MARKETING (Lisa-1/BDE, 45-min).
    # The Teams join link is left as a placement — Graph's onlineMeeting injects the live Join
    # button/dial-in into the invitation it sends. The open pixel rides the existing token.
    variant = _invite_variant(row)
    duration = _invite_duration(row, variant)
    body = _invite_render_for_row(settings, row, join_url=None,
                                  pixel_img=_invite_pixel_img(settings, row.get("pixel_token")))
    try:
        ev = graph_create_event(
            settings, subject=row.get("title") or "Meeting — Traffic Radius",
            start_at=row["start_at"], duration_min=duration,
            attendee_email=row["attendee_email"], attendee_name=row.get("contact_name"),
            body_html=body, cc_emails=_parse_cc(row.get("cc_emails")))
    except Exception as exc:
        _exec(pool, "UPDATE emma_meetings SET error=left(%s,300), updated_at=now() WHERE id=%s",
              (str(exc), mid))
        _log_event(pool, mid, "error", action="send", error=str(exc)[:300])
        raise
    _exec(pool,
          "UPDATE emma_meetings SET status='scheduled', graph_event_id=%s, teams_join_url=%s,"
          " attendee_response=NULL, error=NULL, updated_at=now() WHERE id=%s",
          (ev.get("event_id"), ev.get("join_url"), mid))
    _log_event(pool, mid, "sent", event_id=ev.get("event_id"), join_url=ev.get("join_url"))
    _crm_note(pool, row.get("dest9"),
              f"Emma: invite sent for {_fmt_local(settings, row.get('start_at'))}")
    # Confirmation email (carries the open pixel). Never un-sends the event on failure.
    try:
        row2 = {**row, "teams_join_url": ev.get("join_url")}
        graph_send_mail(settings, row["attendee_email"],
                        f"Locked in: {row.get('title') or 'your meeting with Traffic Radius'}",
                        _confirmation_html(settings, row2))
        _exec(pool, "UPDATE emma_meetings SET confirmation_sent_at=now(), updated_at=now() "
                    "WHERE id=%s", (mid,))
        _log_event(pool, mid, "confirmation-email", to=row["attendee_email"])
    except Exception as exc:
        _log_event(pool, mid, "error", action="confirmation-email", error=str(exc)[:300])
    return {"status": "scheduled", "event_id": ev.get("event_id"), "join_url": ev.get("join_url")}


def approve_meeting(pool: ConnectionPool, settings: Settings, mid: int, *, start_at: str | None,
                    attendee_email: str | None, title: str | None = None, notes: str | None = None,
                    duration_min: int | None = None, cc_emails: str | None = None,
                    approver: str = "?") -> dict:
    """THE approval gate — a human clicked 'Approve & schedule'. Persists the (possibly edited)
    time/email/title/notes/CC, then sends via Graph when configured, else queues the row as
    'approved-awaiting-creds' (sent automatically by the tick once the 4 env values land).
    cc_emails: None = leave as stored; a string (even '') replaces the stored CC list."""
    ensure_emma_tables(pool)
    row = _row1(pool, "SELECT * FROM emma_meetings WHERE id=%s", (mid,))
    if not row:
        raise LookupError("meeting not found")
    if row["status"] not in ("draft", "needs-info", "approved-awaiting-creds"):
        raise ValueError(f"cannot approve from status '{row['status']}' — use reschedule/cancel")
    email = (attendee_email or row.get("attendee_email") or "").strip()[:200]
    if not (start_at or row.get("start_at")):
        raise ValueError("needs a meeting time before it can be approved")
    if not email or "@" not in email:
        raise ValueError("needs the prospect's email before it can be approved")
    cc = ", ".join(_parse_cc(cc_emails)) if cc_emails is not None else None
    _exec(pool,
          "UPDATE emma_meetings SET start_at=COALESCE(%s, start_at), attendee_email=%s,"
          " title=COALESCE(NULLIF(%s,''), title), notes=COALESCE(%s, notes),"
          " duration_min=COALESCE(%s, duration_min),"
          " cc_emails=CASE WHEN %s THEN NULLIF(%s,'') ELSE cc_emails END,"
          " start_at_parsed_from=NULL,"   # human signed the time off — the parse hint retires
          " status='approved-awaiting-creds',"
          " approved_by=%s, approved_at=now(), error=NULL, updated_at=now() WHERE id=%s",
          (start_at or None, email, (title or "").strip()[:200], notes, duration_min,
           cc is not None, cc or "", approver[:120], mid))
    _log_event(pool, mid, "approved", by=approver, start_at=start_at, attendee_email=email,
               cc=cc or None)
    if not settings.graph_configured:
        return {"status": "approved-awaiting-creds", "queued": True,
                "note": "approved — queued until the 4 MS Graph env values are set"}
    return _send_invite(pool, settings, mid)


def reschedule_meeting(pool: ConnectionPool, settings: Settings, mid: int, *, start_at: str,
                       approver: str = "?") -> dict:
    """Approve a NEW slot (approval gate applies to changes too). For a live Graph event the SAME
    event is PATCHed (Graph emails the update); pre-send rows just take the new time."""
    ensure_emma_tables(pool)
    if not (start_at or "").strip():
        raise ValueError("start_at required")
    row = _row1(pool, "SELECT * FROM emma_meetings WHERE id=%s", (mid,))
    if not row:
        raise LookupError("meeting not found")
    if row["status"] == "cancelled":
        raise ValueError("meeting is cancelled — approve a fresh draft instead")
    _exec(pool, "UPDATE emma_meetings SET start_at=%s, start_at_parsed_from=NULL,"
                " approved_by=%s, approved_at=now(),"
                " updated_at=now() WHERE id=%s", (start_at, approver[:120], mid))
    if row["status"] in ("draft", "needs-info"):
        # still pre-approval — just a time edit; recompute the tray it sits in
        _exec(pool, "UPDATE emma_meetings SET status=CASE WHEN attendee_email IS NOT NULL"
                    " THEN 'draft' ELSE 'needs-info' END, updated_at=now() WHERE id=%s", (mid,))
        _log_event(pool, mid, "time-set", by=approver, start_at=start_at)
        return {"status": "draft", "note": "time updated — still awaiting approval"}
    if row.get("graph_event_id") and settings.graph_configured:
        try:
            graph_patch_event_time(settings, row["graph_event_id"],
                                   _row1(pool, "SELECT start_at FROM emma_meetings WHERE id=%s",
                                         (mid,))["start_at"],
                                   row.get("duration_min") or 45)
        except Exception as exc:
            _exec(pool, "UPDATE emma_meetings SET error=left(%s,300), updated_at=now() WHERE id=%s",
                  (str(exc), mid))
            _log_event(pool, mid, "error", action="reschedule", error=str(exc)[:300])
            raise
        _exec(pool, "UPDATE emma_meetings SET status='scheduled', attendee_response=NULL,"
                    " error=NULL, updated_at=now() WHERE id=%s", (mid,))
        _log_event(pool, mid, "patched", by=approver, start_at=start_at)
        _crm_note(pool, row.get("dest9"), f"Emma: meeting moved to {start_at} (invite updated)")
        return {"status": "scheduled", "note": "same event patched — attendee gets the update"}
    # approved but not yet sent (awaiting creds) — the queued send will carry the new time
    _exec(pool, "UPDATE emma_meetings SET status='approved-awaiting-creds', updated_at=now() "
                "WHERE id=%s", (mid,))
    _log_event(pool, mid, "approved-new-time", by=approver, start_at=start_at)
    return {"status": "approved-awaiting-creds", "queued": True}


def cancel_meeting(pool: ConnectionPool, settings: Settings, mid: int, *, reason: str = "",
                   approver: str = "?") -> dict:
    """Cancel the meeting (and the live Graph event, which notifies the attendee)."""
    ensure_emma_tables(pool)
    row = _row1(pool, "SELECT * FROM emma_meetings WHERE id=%s", (mid,))
    if not row:
        raise LookupError("meeting not found")
    if row.get("graph_event_id") and settings.graph_configured:
        try:
            graph_cancel_event(settings, row["graph_event_id"], reason)
        except Exception as exc:
            _log_event(pool, mid, "error", action="cancel", error=str(exc)[:300])
    _exec(pool, "UPDATE emma_meetings SET status='cancelled', updated_at=now() WHERE id=%s", (mid,))
    _log_event(pool, mid, "cancelled", by=approver, reason=reason[:200] or None)
    _crm_note(pool, row.get("dest9"), "Emma: meeting cancelled" + (f" — {reason[:120]}" if reason else ""))
    return {"status": "cancelled"}


# Human-readable labels for the decline reason picker (console → journal → CRM note).
_DECLINE_REASONS = {
    "already-scheduled": "Already scheduled manually",
    "not-proceeding": "Not proceeding",
    "duplicate": "Duplicate",
    "other": "Other",
}


def decline_meeting(pool: ConnectionPool, settings: Settings, mid: int, *, reason_code: str = "",
                    note: str = "", approver: str = "?") -> dict:
    """Kiran's "Decline / don't send" — a PRE-SEND meeting that must never go out (owner
    requirement). Sets status='cancelled' with the reason, drops it from Needs-approval, and
    NEVER touches Graph (nothing was ever sent, so there is nothing to cancel externally).

    Deliberately distinct from cancel_meeting(): decline is only valid BEFORE an invite exists
    — a meeting whose Teams event already went out must be CANCELLED (which notifies the
    attendee), not silently declined. `reason_code` is one of _DECLINE_REASONS; `note` is the
    optional free-text that rides with 'other' (or any code)."""
    ensure_emma_tables(pool)
    row = _row1(pool, "SELECT * FROM emma_meetings WHERE id=%s", (mid,))
    if not row:
        raise LookupError("meeting not found")
    if row.get("graph_event_id"):
        raise ValueError("invite already sent — cancel the meeting instead of declining")
    if row["status"] not in ("draft", "needs-info", "approved-awaiting-creds"):
        raise ValueError(f"cannot decline from status '{row['status']}' — nothing pending to send")
    label = _DECLINE_REASONS.get(reason_code, "")
    reason = " — ".join(x for x in (label, (note or "").strip()[:200]) if x) or "declined"
    _exec(pool, "UPDATE emma_meetings SET status='cancelled', error=NULL, updated_at=now() "
                "WHERE id=%s", (mid,))
    _log_event(pool, mid, "declined", by=approver, reason=reason,
               reason_code=reason_code or None)
    _crm_note(pool, row.get("dest9"), f"Emma: invite declined (not sent) — {reason[:160]}")
    return {"status": "cancelled", "declined": True, "reason": reason}


def set_reminders(pool: ConnectionPool, settings: Settings, mid: int, *, enabled: bool,
                  approver: str = "?") -> dict:
    """Toggle automatic reminder emails for one meeting (owner requirement — per-card + drill-down
    switch). Reminders are the ONLY approval-exempt sends; silencing them here removes a meeting
    from send_due_reminders' set without touching anything else."""
    ensure_emma_tables(pool)
    row = _row1(pool, "SELECT id FROM emma_meetings WHERE id=%s", (mid,))
    if not row:
        raise LookupError("meeting not found")
    _exec(pool, "UPDATE emma_meetings SET reminders_enabled=%s, updated_at=now() WHERE id=%s",
          (bool(enabled), mid))
    _log_event(pool, mid, "reminders-toggled", by=approver,
               flavour=("on" if enabled else "off"))
    return {"ok": True, "reminders_enabled": bool(enabled)}


def update_cc(pool: ConnectionPool, settings: Settings, mid: int, *, cc: str,
              approver: str = "?") -> dict:
    """Set the 'CC / extra attendees' list for a meeting (comma emails; '' clears it).
    Pre-send rows just store it — approve/send picks it up; a meeting with a LIVE Graph
    event gets the SAME event PATCHed (full attendee array: prospect + CC), so the extra
    people land on the existing invite and Graph mails them the update itself."""
    ensure_emma_tables(pool)
    row = _row1(pool, "SELECT * FROM emma_meetings WHERE id=%s", (mid,))
    if not row:
        raise LookupError("meeting not found")
    if row["status"] == "cancelled":
        raise ValueError("meeting is cancelled — nothing to CC")
    prospect = (row.get("attendee_email") or "").strip()
    emails = [e for e in _parse_cc(cc) if e.lower() != prospect.lower()]
    val = ", ".join(emails)
    _exec(pool, "UPDATE emma_meetings SET cc_emails=NULLIF(%s,''), updated_at=now() WHERE id=%s",
          (val, mid))
    _log_event(pool, mid, "cc-updated", by=approver, cc=val or None)
    patched = False
    if row.get("graph_event_id") and settings.graph_configured and prospect:
        try:
            graph_patch_event_attendees(
                settings, row["graph_event_id"],
                _attendee_blocks(prospect, row.get("contact_name"), emails))
        except Exception as exc:
            _exec(pool, "UPDATE emma_meetings SET error=left(%s,300), updated_at=now() WHERE id=%s",
                  (str(exc), mid))
            _log_event(pool, mid, "error", action="cc-patch", error=str(exc)[:300])
            raise
        patched = True
        _exec(pool, "UPDATE emma_meetings SET error=NULL, updated_at=now() WHERE id=%s", (mid,))
        _log_event(pool, mid, "cc-patched", event_id=row["graph_event_id"], cc=val or None)
        _crm_note(pool, row.get("dest9"),
                  f"Emma: invite attendees updated (CC: {val or 'none'})")
    return {"ok": True, "cc": val, "patched": patched}


def record_pixel_open(pool: ConnectionPool, token: str) -> bool:
    """Log the FIRST open of Emma's confirmation email (the 1×1 gif hit). Signal, not proof."""
    if not re.fullmatch(r"[0-9a-f]{16,64}", token or ""):
        return False
    ensure_emma_tables(pool)
    rows = _rows(pool, "UPDATE emma_meetings SET pixel_opened_at=now(), updated_at=now() "
                       "WHERE pixel_token=%s AND pixel_opened_at IS NULL RETURNING id", (token,))
    for r in rows:
        _log_event(pool, r["id"], "pixel-open")
    return bool(rows)


def send_test_invite(pool: ConnectionPool, settings: Settings, email: str) -> dict:
    """Validate the 4 Graph env values end-to-end + send a REAL test invite to `email`
    (20-minute Teams event ~15 min from now on the scheduler mailbox). /api/emma/test."""
    ensure_emma_tables(pool)
    missing = missing_creds(settings)
    if missing:
        return {"ok": False, "missing": missing,
                "note": "set these env values, then re-run the test"}
    email = (email or "").strip()
    if "@" not in email:
        return {"ok": False, "error": "a destination email address is required"}
    start = _local_now(settings) + timedelta(minutes=15)
    ev = graph_create_event(
        settings, subject="Emma Collins — test invite (safe to ignore)",
        start_at=start, duration_min=20, attendee_email=email,
        body_html="<p>This is a test invite from Emma Collins (Traffic Radius scheduling). "
                  "If you can read this, the Microsoft Graph wiring works end-to-end.</p>")
    _log_event(pool, None, "test", to=email, event_id=ev.get("event_id"))
    return {"ok": True, "event_id": ev.get("event_id"), "join_url": ev.get("join_url"),
            "mailbox": settings.scheduler_mailbox}


# --------------------------------------------------------------------------- #
# background workers (run from emma_tick; all creds-gated, all idempotent)
# --------------------------------------------------------------------------- #
def process_awaiting(pool: ConnectionPool, settings: Settings, limit: int = 10) -> dict:
    """Send approved-awaiting-creds rows the moment credentials exist. A row that errored waits
    10 minutes before its next attempt so one bad address can't hot-loop the tick."""
    if not settings.graph_configured:
        return {"skipped": "graph not configured"}
    rows = _rows(pool, "SELECT id FROM emma_meetings WHERE status='approved-awaiting-creds'"
                       " AND start_at IS NOT NULL AND attendee_email IS NOT NULL"
                       " AND (error IS NULL OR updated_at < now() - interval '10 minutes')"
                       " ORDER BY start_at ASC LIMIT %s", (limit,))
    sent = failed = 0
    for r in rows:
        try:
            _send_invite(pool, settings, r["id"])
            sent += 1
        except Exception as exc:
            failed += 1
            log.warning("emma_send_failed", meeting_id=r["id"], error=str(exc)[:160])
    return {"sent": sent, "failed": failed}


def poll_responses(pool: ConnectionPool, settings: Settings, limit: int = 20) -> dict:
    """Attendee RSVP tracking via Graph event polling → status chip + CRM note. Re-checks each
    live meeting at most every 10 minutes."""
    if not settings.graph_configured:
        return {"skipped": "graph not configured"}
    rows = _rows(pool, "SELECT * FROM emma_meetings WHERE status = ANY(%s)"
                       " AND graph_event_id IS NOT NULL AND attendee_email IS NOT NULL"
                       " AND (response_checked_at IS NULL OR response_checked_at < now() - interval '10 minutes')"
                       " AND start_at > now() - interval '2 days'"
                       " ORDER BY response_checked_at ASC NULLS FIRST LIMIT %s",
                 (list(_ACTIVE_STATUSES), limit))
    changed = 0
    for r in rows:
        _exec(pool, "UPDATE emma_meetings SET response_checked_at=now() WHERE id=%s", (r["id"],))
        try:
            resp = graph_event_response(settings, r["graph_event_id"], r["attendee_email"])
        except Exception as exc:
            log.warning("emma_poll_failed", meeting_id=r["id"], error=str(exc)[:160])
            continue
        if not resp or resp == "none" or resp == (r.get("attendee_response") or ""):
            continue
        new_status = {"accepted": "accepted", "declined": "declined"}.get(resp, r["status"])
        _exec(pool, "UPDATE emma_meetings SET attendee_response=%s, status=%s, updated_at=now() "
                    "WHERE id=%s", (resp, new_status, r["id"]))
        _log_event(pool, r["id"], "response", response=resp)
        _crm_note(pool, r.get("dest9"), f"Emma: prospect {resp} the calendar invite")
        changed += 1
    return {"checked": len(rows), "changed": changed}


def scan_replies(pool: ConnectionPool, settings: Settings) -> dict:
    """Read the scheduler mailbox for prospect replies. A reply that reads like 'that time
    doesn't work' flips the meeting to reschedule-requested (Kiran then approves a new slot —
    the gate applies to changes too). Every matched reply is journalled + CRM-noted."""
    if not settings.graph_configured:
        return {"skipped": "graph not configured"}
    c = _row1(pool, "SELECT inbox_scanned_at FROM emma_control WHERE id=1") or {}
    since = c.get("inbox_scanned_at") or (datetime.now(timezone.utc) - timedelta(days=1))
    try:
        msgs = graph_list_inbox(settings, since)
    except Exception as exc:
        log.warning("emma_inbox_failed", error=str(exc)[:160])
        return {"error": str(exc)[:120]}
    _exec(pool, "UPDATE emma_control SET inbox_scanned_at=now(), updated_at=now() WHERE id=1")
    if not msgs:
        return {"messages": 0, "matched": 0}
    live = _rows(pool, "SELECT id, dest9, lower(attendee_email) AS em FROM emma_meetings"
                       " WHERE attendee_email IS NOT NULL AND status = ANY(%s)",
                 (list(_ACTIVE_STATUSES) + ["approved-awaiting-creds"],))
    by_email = {r["em"]: r for r in live}
    matched = resched = 0
    for m in msgs:
        row = by_email.get((m["from"] or "").lower())
        if not row:
            continue
        matched += 1
        snippet = (m["subject"] + " — " + m["preview"])[:300]
        _exec(pool, "UPDATE emma_meetings SET last_reply=left(%s,300), last_reply_at=now(),"
                    " updated_at=now() WHERE id=%s", (snippet, row["id"]))
        _log_event(pool, row["id"], "reply", from_=m["from"], subject=m["subject"][:150],
                   preview=m["preview"][:300])
        if _RESCHED_RE.search(m["subject"] + " " + m["preview"]):
            resched += 1
            _exec(pool, "UPDATE emma_meetings SET status='reschedule-requested', updated_at=now()"
                        " WHERE id=%s AND status <> 'cancelled'", (row["id"],))
            _crm_note(pool, row.get("dest9"), f"Emma: reschedule requested by reply — {snippet[:120]}")
        else:
            _crm_note(pool, row.get("dest9"), f"Emma: prospect replied — {snippet[:120]}")
    return {"messages": len(msgs), "matched": matched, "reschedule_requested": resched}


def send_due_reminders(pool: ConnectionPool, settings: Settings, limit: int = 10) -> dict:
    """The ONLY approval-exempt sends: reminder emails for meetings a human ALREADY approved —
    morning-of (from 07:00 local) + Friday-for-Monday. One reminder per rule per meeting."""
    if not settings.graph_configured:
        return {"skipped": "graph not configured"}
    tz = settings.tz
    rows = _rows(pool, """
        SELECT *, CASE
            WHEN (start_at AT TIME ZONE %(tz)s)::date = (now() AT TIME ZONE %(tz)s)::date
                 THEN 'morning-of' ELSE 'friday-for-monday' END AS flavour
        FROM emma_meetings
        WHERE status IN ('scheduled','accepted') AND start_at IS NOT NULL
          AND attendee_email IS NOT NULL
          AND COALESCE(reminders_enabled, true)          -- Kiran can silence reminders per meeting
          AND ( ((start_at AT TIME ZONE %(tz)s)::date = (now() AT TIME ZONE %(tz)s)::date
                 AND extract(hour FROM now() AT TIME ZONE %(tz)s) >= 7
                 AND start_at > now()
                 AND (reminder_sent_at IS NULL
                      OR (reminder_sent_at AT TIME ZONE %(tz)s)::date < (now() AT TIME ZONE %(tz)s)::date))
             OR (extract(isodow FROM now() AT TIME ZONE %(tz)s) = 5
                 AND (start_at AT TIME ZONE %(tz)s)::date = (now() AT TIME ZONE %(tz)s)::date + 3
                 AND reminder_sent_at IS NULL) )
        ORDER BY start_at ASC LIMIT %(lim)s""", {"tz": tz, "lim": limit})
    sent = 0
    for r in rows:
        try:
            graph_send_mail(settings, r["attendee_email"],
                            ("See you Monday — " if r["flavour"] == "friday-for-monday"
                             else "Today: ") + (r.get("title") or "your meeting with Traffic Radius"),
                            _reminder_html(settings, r, r["flavour"]))
        except Exception as exc:
            _log_event(pool, r["id"], "error", action="reminder", error=str(exc)[:300])
            continue
        _exec(pool, "UPDATE emma_meetings SET reminder_sent_at=now(), updated_at=now() WHERE id=%s",
              (r["id"],))
        _log_event(pool, r["id"], "reminder", flavour=r["flavour"], to=r["attendee_email"])
        sent += 1
    return {"due": len(rows), "sent": sent}


# --------------------------------------------------------------------------- #
# the tick — wired into the cli.py background loop next to sync_qualifier_calls
# --------------------------------------------------------------------------- #
def emma_tick(pool: ConnectionPool, settings: Settings) -> dict:
    """Emma's heartbeat: refresh the unified queue, then (only when the Graph credentials are
    present) send approved invites, poll RSVPs, read replies and fire due reminders. Everything
    inside is throttled/idempotent, so the loop can call this every cycle. Never raises."""
    try:
        ensure_emma_tables(pool)
        stats: dict = {"queue": sync_queue(pool, settings, min_interval_seconds=120)}
        if settings.graph_configured:
            stats["send"] = process_awaiting(pool, settings)
            stats["responses"] = poll_responses(pool, settings)
            stats["replies"] = scan_replies(pool, settings)
            stats["reminders"] = send_due_reminders(pool, settings)
        else:
            stats["graph"] = "awaiting creds (approvals queue safely)"
        return stats
    except Exception as exc:
        log.warning("emma_tick_failed", error=str(exc)[:160])
        return {"error": str(exc)[:160]}
