"""Company-level booking MEMORY for the classifier.

A booking belongs to a COMPANY, not a phone number. When a BDE books a meeting with one
contact (e.g. a manager, Ravi) and later calls a colleague (Asim) at the SAME company to
brief them, that second call must NOT count as a new booking. To make the AI aware of this
we (1) derive a stable per-company key, and (2) before classifying a call, look up whether
that company already has a firm booking on record and feed it to the model as context.

company_key precedence: resolved website domain -> normalised company-name slug -> phone.
Domain is the most stable; the name slug catches companies with no known website (the common
case for cold 3CX/Aircall calls); phone is the last-resort per-number fallback.
"""

from __future__ import annotations

import re

from psycopg_pool import ConnectionPool

from .classifier import normalize_domain

# generic tokens dropped from a company name before slugging, so "Classic Homeware Pty Ltd"
# and "Classic Homeware & Gifts" collapse to the same stable key.
_STOP = {"pty", "ltd", "limited", "inc", "llc", "co", "company", "group", "holdings",
         "the", "and", "australia", "aus", "au", "services", "solutions", "enterprises"}


def dest9(number: str | None) -> str:
    """Last 9 significant digits of a phone number (matches the funnel's dedup key)."""
    digits = re.sub(r"\D", "", number or "")
    return digits[-9:] if digits else ""


def slug_company(name: str | None) -> str:
    if not name:
        return ""
    toks = [t for t in re.split(r"[^a-z0-9]+", name.lower()) if t and t not in _STOP]
    return "".join(toks)


def company_key(domain: str | None, company: str | None, number: str | None) -> str | None:
    """Stable company identity: dom:<domain> | co:<name-slug> | tel:<dest9>. None if nothing."""
    dom = normalize_domain(domain)
    if dom:
        return f"dom:{dom}"
    slug = slug_company(company)
    if len(slug) >= 6:                 # avoid junk/colliding keys from short generic names
        return f"co:{slug}"
    d9 = dest9(number)
    return f"tel:{d9}" if d9 else None


def _master_domain(pool: ConnectionPool, number: str | None) -> str | None:
    """Domain for a phone number from the master prospects table (pre-classification)."""
    d9 = dest9(number)
    if not d9:
        return None
    try:
        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT domain FROM prospects WHERE %s = ANY(phones_norm) AND domain IS NOT NULL "
                "LIMIT 1", (d9,))
            row = cur.fetchone()
        return (row.get("domain") if isinstance(row, dict) else (row[0] if row else None)) or None
    except Exception:
        return None


def lookup_booking_memory(pool: ConnectionPool, number: str | None,
                          domain: str | None = None, before_call_id: str | None = None) -> dict | None:
    """The earliest firm, genuinely-NEW booking already on record for this call's company.

    Matches by same phone number (dest9) OR same DOMAIN — domain is the only reliable
    cross-contact company identity (name slugs collide), resolved from the master prospects
    table (pass `domain` to reuse an already-resolved value). Excludes reschedules/
    confirmations/already-exists (those aren't original bookings). Returns {when, bde,
    contact, company, via} or None. Best-effort; never raises.
    """
    d9 = dest9(number)
    dom = normalize_domain(domain) if domain else _master_domain(pool, number)
    if not d9 and not dom:
        return None
    conds = []
    params: dict = {"cid": before_call_id or ""}
    if d9:
        conds.append("right(regexp_replace(c.dest_number,'[^0-9]','','g'),9) = %(d9)s")
        params["d9"] = d9
    if dom:
        conds.append("cl.prospect_website = %(dom)s")
        params["dom"] = dom
    where_company = " OR ".join(conds)
    sql = f"""
        SELECT c.started_at, c.bde_name, cl.prospect_contact_name, cl.prospect_company
        FROM classifications cl JOIN calls c ON c.call_id = cl.call_id
        WHERE c.in_scope AND cl.meeting_booked
          AND NOT COALESCE(cl.meeting_confirmation, false)
          AND NOT COALESCE(cl.meeting_rescheduled, false)
          AND NOT COALESCE(cl.booking_already_exists, false)
          AND cl.call_id <> %(cid)s
          AND ({where_company})
        ORDER BY c.started_at, c.call_id
        LIMIT 1
    """
    try:
        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute(sql, params)
            row = cur.fetchone()
    except Exception:
        return None
    if not row:
        return None
    g = (lambda k: row.get(k)) if isinstance(row, dict) else None
    when = g("started_at") if g else row[0]
    bde = g("bde_name") if g else row[1]
    contact = g("prospect_contact_name") if g else row[2]
    company = g("prospect_company") if g else row[3]
    return {"when": str(when)[:16] if when else None, "bde": bde,
            "contact": contact, "company": company, "via": ("domain" if dom else "phone")}


def format_memory(mem: dict | None) -> str:
    """Render the prior-booking memory as a prompt context block ('' if none)."""
    if not mem:
        return ""
    bits = []
    if mem.get("company"):
        bits.append(f"company “{mem['company']}”")
    if mem.get("contact"):
        bits.append(f"with {mem['contact']}")
    if mem.get("bde"):
        bits.append(f"by BDE {mem['bde']}")
    if mem.get("when"):
        bits.append(f"on {mem['when']}")
    detail = " ".join(bits) if bits else "this company"
    return (
        "[COMPANY MEMORY]\n"
        f"A meeting was ALREADY booked for {detail} on an earlier call. If THIS call is a "
        "follow-up, briefing, or hand-off to another person at the SAME company about that "
        "same already-arranged meeting (not a brand-new, separate meeting), set "
        "booking_already_exists.value = true so it is not double-counted.\n"
    )
