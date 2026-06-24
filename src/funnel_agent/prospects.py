"""Master prospect database — seeding, phone normalization, and call→prospect matching.

The team keeps a known-prospect universe (websites + decision-maker contacts +
SEMrush metrics) in the `prospects` table, seeded from DATA_MASTER_FILE. Each 3CX
call is linked to a prospect either by PHONE (normalized to canonical AU form) or
by the business name resolved from the transcript → domain.

Phone matching is the reliable path: AU numbers come in many shapes
(`0450885235`, `+61450885235`, `450885235`, `0411 797 127`) — we reduce every one
to the trailing 9 significant digits and match on that.
"""

from __future__ import annotations

import csv
import re
from collections.abc import Iterable

from psycopg.types.json import Json
from psycopg_pool import ConnectionPool

from .db import fetch_all, fetch_one
from .logging import get_logger

log = get_logger(__name__)

# DATA_MASTER_FILE column order (the only 16 real columns; the rest are empty
# trailing-comma padding from the Excel export).
_MASTER_COLS = [
    "domain", "location",
    "c1_name", "c1_title", "c1_mobile", "c1_phone",
    "c2_name", "c2_title", "c2_mobile", "c2_phone",
    "competitor_relevance", "common_keywords",
    "organic_keywords", "organic_traffic", "organic_cost", "adwords_keywords",
]


def normalize_au_phone(raw: str | None) -> str | None:
    """Reduce any AU phone string to its canonical trailing-9-digit form.

    Returns None when there aren't enough digits to be a real number.
    Examples: '0450885235'→'450885235', '+61 450 885 235'→'450885235',
    '400760360'→'400760360', '0411 797 127'→'411797127', '(08) 9384 9200'→'893849200'.
    """
    if not raw:
        return None
    digits = re.sub(r"\D", "", str(raw))
    if not digits:
        return None
    # Drop an international 0011/00 prefix, then the country code 61, then trunk 0.
    digits = re.sub(r"^(?:0011|00)", "", digits)
    if digits.startswith("61") and len(digits) > 9:
        digits = digits[2:]
    digits = digits.lstrip("0")
    if len(digits) < 8:  # too short to be a real AU landline/mobile
        return None
    return digits[-9:]


def clean_domain(raw: str | None) -> str | None:
    """Public alias for domain normalization (see _clean_domain)."""
    return _clean_domain(raw)


def _clean_domain(raw: str | None) -> str | None:
    """Bare, lowercased domain: strip scheme, www., path, whitespace."""
    if not raw:
        return None
    d = str(raw).strip().lower()
    d = re.sub(r"^https?://", "", d)
    d = re.sub(r"^www\.", "", d)
    d = d.split("/")[0].split("?")[0].strip()
    return d or None


def _to_int(raw: str | None) -> int | None:
    if raw is None:
        return None
    s = re.sub(r"[^\d\-]", "", str(raw))
    try:
        return int(s) if s not in ("", "-") else None
    except ValueError:
        return None


def _to_num(raw: str | None) -> float | None:
    if raw is None:
        return None
    s = re.sub(r"[^\d.\-]", "", str(raw))
    try:
        return float(s) if s not in ("", "-", ".") else None
    except ValueError:
        return None


def _phones_for_row(row: dict) -> list[str]:
    """All distinct normalized phone forms across both contacts of a master row."""
    out: list[str] = []
    for key in ("c1_mobile", "c1_phone", "c2_mobile", "c2_phone"):
        n = normalize_au_phone(row.get(key))
        if n and n not in out:
            out.append(n)
    return out


def _business_name_from_domain(domain: str) -> str:
    """Fallback display name: 'curaprox.com.au' → 'Curaprox'."""
    core = domain.split(".")[0] if domain else ""
    return core.replace("-", " ").title()


def iter_master_rows(csv_path: str) -> Iterable[dict]:
    """Yield cleaned dict rows from DATA_MASTER_FILE (skips blank/header-less rows)."""
    csv.field_size_limit(10**8)
    with open(csv_path, newline="", encoding="utf-8", errors="replace") as f:
        reader = csv.reader(f)
        header = next(reader, None)  # discard the header row
        for raw in reader:
            cells = [(raw[i].strip() if i < len(raw) else "") for i in range(16)]
            if not any(cells):
                continue
            row = dict(zip(_MASTER_COLS, cells))
            row["domain"] = _clean_domain(row["domain"])
            if not row["domain"]:
                continue
            yield row


# Idempotent upsert keyed on domain. Master-file fields are refreshed on conflict,
# but we DON'T clobber pipeline/assignment state the app may have set.
_UPSERT_SQL = """
INSERT INTO prospects (
  domain, business_name, location,
  contact1_name, contact1_title, contact1_mobile, contact1_phone,
  contact2_name, contact2_title, contact2_mobile, contact2_phone,
  phones_norm, competitor_relevance, common_keywords,
  organic_keywords, organic_traffic, organic_cost, adwords_keywords,
  source, updated_at
) VALUES (
  %(domain)s, %(business_name)s, %(location)s,
  %(c1_name)s, %(c1_title)s, %(c1_mobile)s, %(c1_phone)s,
  %(c2_name)s, %(c2_title)s, %(c2_mobile)s, %(c2_phone)s,
  %(phones_norm)s, %(competitor_relevance)s, %(common_keywords)s,
  %(organic_keywords)s, %(organic_traffic)s, %(organic_cost)s, %(adwords_keywords)s,
  'master_file', now()
)
ON CONFLICT (domain) DO UPDATE SET
  business_name = EXCLUDED.business_name,
  location = EXCLUDED.location,
  contact1_name = EXCLUDED.contact1_name, contact1_title = EXCLUDED.contact1_title,
  contact1_mobile = EXCLUDED.contact1_mobile, contact1_phone = EXCLUDED.contact1_phone,
  contact2_name = EXCLUDED.contact2_name, contact2_title = EXCLUDED.contact2_title,
  contact2_mobile = EXCLUDED.contact2_mobile, contact2_phone = EXCLUDED.contact2_phone,
  phones_norm = EXCLUDED.phones_norm,
  competitor_relevance = EXCLUDED.competitor_relevance,
  common_keywords = EXCLUDED.common_keywords,
  organic_keywords = EXCLUDED.organic_keywords,
  organic_traffic = EXCLUDED.organic_traffic,
  organic_cost = EXCLUDED.organic_cost,
  adwords_keywords = EXCLUDED.adwords_keywords,
  updated_at = now()
"""


def seed_prospects_from_csv(pool: ConnectionPool, csv_path: str, batch: int = 1000) -> dict:
    """Upsert every master-file row into `prospects`. Idempotent; returns stats."""
    seen = 0
    upserted = 0
    with_phone = 0
    skipped_dupe = 0
    seen_domains: set[str] = set()
    pending: list[dict] = []

    def flush(cur):
        nonlocal upserted
        if pending:
            cur.executemany(_UPSERT_SQL, pending)
            upserted += len(pending)
            pending.clear()

    with pool.connection() as conn:
        with conn.cursor() as cur:
            for row in iter_master_rows(csv_path):
                seen += 1
                domain = row["domain"]
                if domain in seen_domains:
                    skipped_dupe += 1
                    continue
                seen_domains.add(domain)
                phones = _phones_for_row(row)
                if phones:
                    with_phone += 1
                params = {
                    "domain": domain,
                    "business_name": (row.get("c1_name") and _business_name_from_domain(domain))
                    or _business_name_from_domain(domain),
                    "location": row.get("location") or None,
                    "c1_name": row.get("c1_name") or None,
                    "c1_title": row.get("c1_title") or None,
                    "c1_mobile": row.get("c1_mobile") or None,
                    "c1_phone": row.get("c1_phone") or None,
                    "c2_name": row.get("c2_name") or None,
                    "c2_title": row.get("c2_title") or None,
                    "c2_mobile": row.get("c2_mobile") or None,
                    "c2_phone": row.get("c2_phone") or None,
                    "phones_norm": phones,
                    "competitor_relevance": row.get("competitor_relevance") or None,
                    "common_keywords": _to_int(row.get("common_keywords")),
                    "organic_keywords": _to_int(row.get("organic_keywords")),
                    "organic_traffic": _to_int(row.get("organic_traffic")),
                    "organic_cost": _to_num(row.get("organic_cost")),
                    "adwords_keywords": _to_int(row.get("adwords_keywords")),
                }
                pending.append(params)
                if len(pending) >= batch:
                    flush(cur)
            flush(cur)
        conn.commit()

    return {
        "rows_seen": seen,
        "upserted": upserted,
        "with_phone": with_phone,
        "skipped_duplicate_domain": skipped_dupe,
        "unique_domains": len(seen_domains),
    }


def match_prospect_by_phone(pool: ConnectionPool, dest_number: str | None) -> dict | None:
    """Find the master prospect whose contacts include this dialled number."""
    norm = normalize_au_phone(dest_number)
    if not norm:
        return None
    return fetch_one(
        pool,
        "SELECT * FROM prospects WHERE %s = ANY(phones_norm) LIMIT 1",
        (norm,),
    )


def capture_called_prospects(pool: ConnectionPool) -> dict:
    """Add master-DB records for numbers we've CALLED that aren't on file yet.

    The original master file only has known prospects; ~half of dialled numbers
    aren't in it. This pulls each such number into `prospects` using the business
    name / website / industry the AI extracted from the transcript, so they show in
    the Database browser, get a consolidated page, and (when a website is known) can
    be enriched. Rows are tagged source='call_capture' so they're distinct from the
    seed file and fully reversible. Idempotent: a number already on file is skipped.
    """
    candidates = fetch_all(
        pool,
        """
        WITH called AS (
          SELECT right(regexp_replace(c.dest_number,'[^0-9]','','g'),9) AS dest9,
                 (array_agg(c.dest_number ORDER BY c.started_at DESC))[1] AS dest_number,
                 (array_remove(array_agg(NULLIF(cl.prospect_company,'') ORDER BY c.started_at DESC), NULL))[1] AS company,
                 (array_remove(array_agg(NULLIF(cl.prospect_website,'') ORDER BY c.started_at DESC), NULL))[1] AS website,
                 (array_remove(array_agg(NULLIF(cl.prospect_industry,'') ORDER BY c.started_at DESC), NULL))[1] AS industry
          FROM calls c JOIN classifications cl ON cl.call_id=c.call_id
          WHERE c.in_scope AND c.dest_number IS NOT NULL AND c.dest_number <> ''
          GROUP BY 1
        )
        SELECT * FROM called ca
        WHERE ca.dest9 IS NOT NULL AND length(ca.dest9) >= 8
          AND NOT EXISTS (SELECT 1 FROM prospects p WHERE ca.dest9 = ANY(p.phones_norm))
        """,
    )
    created = merged = 0
    with pool.connection() as conn, conn.cursor() as cur:
        for r in candidates:
            dest9 = r["dest9"]
            domain = clean_domain(r["website"]) if r["website"] else None
            name = (r["company"] or "").strip() or None
            if domain:
                cur.execute("SELECT id FROM prospects WHERE domain=%s", (domain,))
                ex = cur.fetchone()
                if ex:
                    # Same business already on file (by website) — just attach this number.
                    cur.execute(
                        "UPDATE prospects SET phones_norm = "
                        "(SELECT array_agg(DISTINCT x) FROM unnest(phones_norm || %s::text[]) x), "
                        "business_name = COALESCE(business_name, %s), updated_at = now() WHERE id=%s",
                        ([dest9], name, ex["id"]),
                    )
                    merged += 1
                    continue
            cur.execute(
                "INSERT INTO prospects (domain, business_name, phones_norm, source, updated_at) "
                "VALUES (%s, %s, %s, 'call_capture', now())",
                (domain, name, [dest9]),
            )
            created += 1
        conn.commit()

    # Phase 2 — BACKFILL: a prospect already on file with NO website, whose calls now
    # carry an AI-extracted website (e.g. recovered from an email after a prompt change),
    # gets that website set so it can be enriched. Merges if the domain already exists.
    filled = fetch_all(
        pool,
        """
        WITH cand AS (
          SELECT pr.id,
                 (array_remove(array_agg(NULLIF(cl.prospect_website,'') ORDER BY c.started_at DESC), NULL))[1] AS website
          FROM prospects pr
          JOIN calls c ON c.in_scope AND c.dest_number <> ''
            AND right(regexp_replace(c.dest_number,'[^0-9]','','g'),9) = ANY(pr.phones_norm)
          JOIN classifications cl ON cl.call_id = c.call_id
          WHERE pr.domain IS NULL
          GROUP BY pr.id
        )
        SELECT id, website FROM cand WHERE website IS NOT NULL
        """,
    )
    backfilled = 0
    with pool.connection() as conn, conn.cursor() as cur:
        for row in filled:
            domain = clean_domain(row["website"])
            if not domain:
                continue
            cur.execute("SELECT id FROM prospects WHERE domain=%s", (domain,))
            ex = cur.fetchone()
            if ex and ex["id"] != row["id"]:
                # domain already belongs to another prospect → move phones over, drop the blank row
                cur.execute(
                    "UPDATE prospects t SET phones_norm = "
                    "(SELECT array_agg(DISTINCT x) FROM unnest(t.phones_norm || src.phones_norm) x), updated_at=now() "
                    "FROM prospects src WHERE t.id=%s AND src.id=%s",
                    (ex["id"], row["id"]))
                cur.execute("DELETE FROM prospects WHERE id=%s AND source='call_capture'", (row["id"],))
            elif not ex:
                cur.execute("UPDATE prospects SET domain=%s, updated_at=now() WHERE id=%s", (domain, row["id"]))
            backfilled += 1
        conn.commit()

    stats = {"candidates": len(candidates), "created": created, "merged": merged, "backfilled": backfilled}
    log.info("capture_called_prospects", **stats)
    return stats


def match_prospects_by_name(pool: ConnectionPool, name: str | None, limit: int = 5) -> list[dict]:
    """Fuzzy candidate prospects for a business name parsed from a transcript.

    Used to resolve a website when the dialled number isn't in the master DB:
    the caller confirms the right match before we enrich.
    """
    if not name or not name.strip():
        return []
    term = name.strip().lower()
    core = re.sub(r"[^a-z0-9]+", "", term)
    return fetch_all(
        pool,
        """
        SELECT id, domain, business_name, location, contact1_name, contact1_title
          FROM prospects
         WHERE lower(business_name) LIKE %(like)s
            OR lower(domain) LIKE %(like)s
            OR regexp_replace(lower(domain), '[^a-z0-9]', '', 'g') LIKE %(core)s
         ORDER BY (lower(business_name) = %(exact)s) DESC, length(domain)
         LIMIT %(limit)s
        """,
        {"like": f"%{term}%", "core": f"%{core}%", "exact": term, "limit": limit},
    )
