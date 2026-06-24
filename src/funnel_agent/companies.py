"""Load the given business data (e.g. a Dun & Bradstreet export) into `companies`.

UNLIKE the `prospects` table (one row per domain, used for call-matching + pipelines),
`companies` keeps EVERY supplied business as its own row: many businesses can share a
domain, and many have no domain at all. The Database browser groups these by domain
(summing revenue) for the static view; the prospect page expands a domain into its
businesses + dynamic enrichment.

The loader is column-name driven (not positional), so it tolerates the wide trailing
empty columns Excel pads a CSV with. It maps the D&B header to our schema and stores
the up-to-5 contacts as a JSON list. Idempotent per source: re-running replaces that
source's rows (delete-then-insert) so counts never double.
"""

from __future__ import annotations

import csv

from psycopg.types.json import Json
from psycopg_pool import ConnectionPool

from .logging import get_logger
from .prospects import _clean_domain, _to_int, _to_num, normalize_au_phone

log = get_logger(__name__)

# Map our column -> the candidate header names in the supplied file (first match wins).
_HEADERS = {
    "company_name":   ["Company Name"],
    "company_profile": ["Company Profile"],
    "year_started":   ["Year Started", "Year Started "],
    "incorporated":   ["Incorporated"],
    "fiscal_year_end": ["Fiscal Year End"],
    "employees":      ["Employees"],
    "revenue_musd":   ["Revenue (MIL USD)", "Revenue (Mil USD)", "Revenue"],
    "industry":       ["Industry"],
    "sub_industry":   ["Sub Industry"],
    "address":        ["Address"],
    "country":        ["Country"],
    "state":          ["State"],
    "postal_code":    ["Postal Code"],
    "suburb":         ["Suburb"],
    "domain":         ["Website", "Domain"],
    "phone":          ["Phone No", "Phone", "Phone Number"],
    "source_url":     ["Source Url", "Source URL"],
    "parent_company": ["Parent Company Name", "Parent Company"],
    "subsidiaries":   ["Subsidiaries"],
    "branches":       ["Branches"],
}
_CONTACT_PAIRS = [
    ("Contact Name 1", "Contact Name Role 1"),
    ("Contact Name 2", "Contact Name Role 2"),
    ("Contact Name 3", "Contact Name Role 3"),
    ("Contact Name 4", "Contact Name Role 4"),
    ("Contact Name 5", "Contact Name Role 5"),
]


def _pick(row: dict, names: list[str]) -> str:
    for n in names:
        v = row.get(n)
        if v is not None and str(v).strip():
            return str(v).strip()
    return ""


def _contacts(row: dict) -> list[dict]:
    out = []
    for name_col, role_col in _CONTACT_PAIRS:
        name = (row.get(name_col) or "").strip()
        if not name:
            continue
        out.append({"name": name, "role": (row.get(role_col) or "").strip() or None})
    return out


def iter_company_rows(csv_path: str):
    """Yield normalized company dicts from the supplied CSV (column-name driven)."""
    csv.field_size_limit(10**8)
    with open(csv_path, newline="", encoding="utf-8", errors="replace") as f:
        reader = csv.DictReader(f)
        for raw in reader:
            name = _pick(raw, _HEADERS["company_name"])
            if not name:
                continue  # a company name is the minimum to keep a row
            domain = _clean_domain(_pick(raw, _HEADERS["domain"]))
            phone = _pick(raw, _HEADERS["phone"])
            yield {
                "company_name": name,
                "company_profile": _pick(raw, _HEADERS["company_profile"]) or None,
                "year_started": _to_int(_pick(raw, _HEADERS["year_started"])),
                "incorporated": _pick(raw, _HEADERS["incorporated"]) or None,
                "fiscal_year_end": _pick(raw, _HEADERS["fiscal_year_end"]) or None,
                "employees": _to_int(_pick(raw, _HEADERS["employees"])),
                "revenue_musd": _to_num(_pick(raw, _HEADERS["revenue_musd"])),
                "industry": _pick(raw, _HEADERS["industry"]) or None,
                "sub_industry": _pick(raw, _HEADERS["sub_industry"]) or None,
                "address": _pick(raw, _HEADERS["address"]) or None,
                "suburb": _pick(raw, _HEADERS["suburb"]) or None,
                "state": _pick(raw, _HEADERS["state"]) or None,
                "postal_code": _pick(raw, _HEADERS["postal_code"]) or None,
                "country": _pick(raw, _HEADERS["country"]) or None,
                "domain": domain,
                "phone": phone or None,
                "phone_norm": normalize_au_phone(phone),
                "source_url": _pick(raw, _HEADERS["source_url"]) or None,
                "parent_company": _pick(raw, _HEADERS["parent_company"]) or None,
                "subsidiaries": _pick(raw, _HEADERS["subsidiaries"]) or None,
                "branches": _pick(raw, _HEADERS["branches"]) or None,
                "contacts": _contacts(raw),
            }


_INSERT_SQL = """
INSERT INTO companies (
  source, company_name, company_profile, year_started, incorporated, fiscal_year_end,
  employees, revenue_musd, industry, sub_industry, address, suburb, state, postal_code,
  country, domain, phone, phone_norm, source_url, parent_company, subsidiaries, branches, contacts)
VALUES (
  %(source)s, %(company_name)s, %(company_profile)s, %(year_started)s, %(incorporated)s, %(fiscal_year_end)s,
  %(employees)s, %(revenue_musd)s, %(industry)s, %(sub_industry)s, %(address)s, %(suburb)s, %(state)s, %(postal_code)s,
  %(country)s, %(domain)s, %(phone)s, %(phone_norm)s, %(source_url)s, %(parent_company)s, %(subsidiaries)s, %(branches)s, %(contacts)s)
"""


# Friendly source names for the Database browser filter.
SOURCE_LABELS = {"raghav": "Raghav (D&B)", "raven": "Raven", "3cx_calls": "3CX calls",
                 "dnb_wholesale": "Raghav (D&B)", "given_data": "Uploaded"}


def sync_company_sources_from_prospects(pool: ConnectionPool) -> dict:
    """Mirror the Raven master-file (-> source 'raven') and 3CX call-captured
    (-> '3cx_calls') prospects into `companies` so the Database browser shows ALL
    data sources with a source filter. Idempotent: rebuilds those two sources each
    run (never touches Raghav's 'raghav' rows). Set-based + cheap."""
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM companies WHERE source IN ('raven', '3cx_calls')")
        cur.execute(
            """
            INSERT INTO companies (source, company_name, domain, phone, phone_norm, suburb, contacts, created_at)
            SELECT CASE WHEN p.source = 'master_file' THEN 'raven' ELSE '3cx_calls' END,
                   COALESCE(NULLIF(p.business_name, ''), NULLIF(p.domain, ''), 'Unknown'),
                   NULLIF(p.domain, ''),
                   COALESCE(p.contact1_mobile, p.contact1_phone),
                   (p.phones_norm)[1],
                   p.location,
                   COALESCE((SELECT jsonb_agg(x) FROM jsonb_array_elements(jsonb_build_array(
                       CASE WHEN p.contact1_name IS NOT NULL THEN jsonb_build_object('name', p.contact1_name, 'role', p.contact1_title) END,
                       CASE WHEN p.contact2_name IS NOT NULL THEN jsonb_build_object('name', p.contact2_name, 'role', p.contact2_title) END
                   )) x WHERE x IS NOT NULL), '[]'::jsonb),
                   now()
            FROM prospects p WHERE p.source IN ('master_file', 'call_capture')
            """
        )
        n = cur.rowcount
        # Normalise the legacy D&B tag to the friendly 'raghav' source.
        cur.execute("UPDATE companies SET source='raghav' WHERE source IN ('dnb_wholesale','given_data')")
        conn.commit()
    log.info("sync_company_sources", mirrored=n)
    return {"mirrored": n}


def load_companies_from_csv(pool: ConnectionPool, csv_path: str,
                            source: str = "given_data", batch: int = 1000) -> dict:
    """Replace all rows for `source` with the file's rows (idempotent per source)."""
    seen = with_domain = with_phone = inserted = 0
    pending: list[dict] = []

    def flush(cur):
        nonlocal inserted
        if pending:
            cur.executemany(_INSERT_SQL, pending)
            inserted += len(pending)
            pending.clear()

    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM companies WHERE source = %s", (source,))
            for row in iter_company_rows(csv_path):
                seen += 1
                if row["domain"]:
                    with_domain += 1
                if row["phone_norm"]:
                    with_phone += 1
                pending.append({**row, "source": source, "contacts": Json(row["contacts"])})
                if len(pending) >= batch:
                    flush(cur)
            flush(cur)
        conn.commit()

    stats = {"rows_seen": seen, "inserted": inserted,
             "with_domain": with_domain, "with_phone": with_phone}
    log.info("load_companies_done", source=source, **stats)
    return stats
