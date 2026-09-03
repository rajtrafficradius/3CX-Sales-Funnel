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
from datetime import datetime, timezone

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


def gads_dnb_gate(enr_alias: str = "e") -> str:
    """WHERE-clause fragment restricting the Google-Ads POOL to D&B-backed domains only.

    Policy (2026-07-10, user rule): a domain counts as part of the Google-Ads pool ONLY if it is
    (a) confirmed running Google Ads AND (b) we hold D&B data for it — i.e. a `companies` row from
    the D&B / raghav load. Raven-only and call-captured domains are still worked as BDE follow-ups
    (Agency & RPC page), but are NOT counted in the pool, its pipeline boards, calendar or counts.
    Single source of truth: every pool filter appends this gate, so the rule stays consistent.
    Reversible — return "" here (or drop the call sites) to restore the ads-only pool.
    `enr_alias` is the enrichment-table alias in the surrounding query whose `.domain` is gated."""
    return (f" AND EXISTS (SELECT 1 FROM companies _dbc "
            f"WHERE _dbc.domain = {enr_alias}.domain AND _dbc.source = 'raghav') ")


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
            # A non-domain here is a MERGE KEY: the lookup below attaches this number to whatever row
            # already holds that string, so one bad value swallows every prospect that shares it.
            # Lisa's speech placeholder "your website" did exactly that — 2,289 unrelated prospects
            # merged into a single record (Raj, 2026-09-03). Reject anything that isn't a real domain;
            # the prospect is still created, just keyed on its phone alone.
            from .lisa import domain_for_column as _dfc
            domain = _dfc(clean_domain(r["website"])) if r["website"] else None
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
    # Same result as before (latest non-null extracted website across a domainless prospect's
    # phone-matched calls) but computed as a plain equijoin: aggregate website-bearing calls by
    # dest9 FIRST, explode prospect phones, then hash-join on the number — instead of a nested
    # loop that recomputed right(regexp_replace(dest_number)) against every prospect × every call
    # (which grew to many minutes as captured prospects accumulated).
    filled = fetch_all(
        pool,
        """
        WITH callweb AS (
          SELECT right(regexp_replace(c.dest_number,'[^0-9]','','g'),9) AS dest9,
                 c.started_at, NULLIF(cl.prospect_website,'') AS website
          FROM calls c JOIN classifications cl ON cl.call_id = c.call_id
          WHERE c.in_scope AND c.dest_number <> '' AND NULLIF(cl.prospect_website,'') IS NOT NULL
        ),
        prno AS (
          SELECT pr.id, ph AS dest9 FROM prospects pr, unnest(pr.phones_norm) AS ph
          WHERE pr.domain IS NULL
        )
        SELECT p.id, (array_agg(cw.website ORDER BY cw.started_at DESC))[1] AS website
        FROM prno p JOIN callweb cw ON cw.dest9 = p.dest9
        GROUP BY p.id
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


def sync_prospect_pipelines(pool: ConnectionPool, *, min_interval_minutes: int = 0) -> dict:
    """Batch D — materialize the 5-pipeline worklist layer on `prospects` (idempotent).

    Runs at the TAIL of the refresh, AFTER capture_called_prospects + classify, and writes
    ONLY the additive columns pipeline_stage / p4_subpipeline / last_called_at /
    last_call_provider / pipeline_synced_at. It reads calls + classifications + enrichment
    read-only, so it can never affect ingest, capture, or booking/qualification reporting.

    One set-based UPDATE recomputes every prospect from scratch, so re-running is stable:
      * last_called_at / last_call_provider — the most recent in-scope call matched by the
        trailing-9 phone (NULL provider/date = never dialled = P4 fresh).
      * pipeline_stage — the precedence rollup (p5 > p2 > p1 > p3) over BOTH the prospect's
        phone-matched calls AND the domain company_key classifications (so a booked company
        is P5 even if the booking call was to a different contact number). Unresolved → 'p4'.
      * p4_subpipeline (only for 'p4' rows): dead (do-not-contact) > captured_<provider>
        (call_capture origin) > attempted (known prospect, dialled, no outcome) >
        fresh_ads (running-ads confirmed, never dialled) > fresh_unscanned (uncalled, ads unknown).

    The full recompute is a heavy scan, so the refresh loop passes min_interval_minutes to
    skip it when the last sync is still fresh (the worklist changes slowly). A one-off
    backfill or CLI run passes 0 to always recompute.
    """
    if min_interval_minutes > 0:
        row = fetch_all(
            pool,
            "SELECT max(pipeline_synced_at) AS last FROM prospects WHERE pipeline_synced_at IS NOT NULL",
        )
        last = row[0]["last"] if row else None
        if last is not None:
            age_min = (datetime.now(timezone.utc) - last).total_seconds() / 60.0
            if age_min < min_interval_minutes:
                return {"skipped": True, "age_min": round(age_min, 1)}
    sql = """
    WITH callagg AS (
      -- Aggregate calls by dest9 FIRST (few thousand distinct numbers), so the prospect join
      -- is a plain equijoin per number instead of a nested loop over every call × every prospect.
      SELECT right(regexp_replace(c.dest_number,'[^0-9]','','g'),9)  AS dest9,
             max(c.started_at)                                       AS last_at,
             (array_agg(c.provider ORDER BY c.started_at DESC))[1]   AS provider,
             bool_or(cl.pipeline_stage = 'p5')                       AS has_p5,
             bool_or(cl.pipeline_stage = 'p2')                       AS has_p2,
             bool_or(cl.pipeline_stage = 'p1')                       AS has_p1,
             bool_or(cl.pipeline_stage = 'p3')                       AS has_p3,
             bool_or(COALESCE(cl.do_not_contact, false))             AS dnc
      FROM calls c LEFT JOIN classifications cl ON cl.call_id = c.call_id
      WHERE c.in_scope AND c.dest_number <> ''
        AND length(right(regexp_replace(c.dest_number,'[^0-9]','','g'),9)) >= 8
      GROUP BY 1
    ),
    prphones AS (
      -- explode each prospect's phone list so the call→prospect match is a plain equijoin
      -- (hash join) on the number, not a per-number GIN probe.
      SELECT pr.id, ph AS phone FROM prospects pr, unnest(pr.phones_norm) AS ph
    ),
    callroll AS (
      SELECT p.id,
             max(ca.last_at)                                       AS last_at,
             (array_agg(ca.provider ORDER BY ca.last_at DESC))[1]  AS provider,
             bool_or(ca.has_p5)  AS has_p5,
             bool_or(ca.has_p2)  AS has_p2,
             bool_or(ca.has_p1)  AS has_p1,
             bool_or(ca.has_p3)  AS has_p3,
             bool_or(ca.dnc)     AS dnc
      FROM callagg ca JOIN prphones p ON p.phone = ca.dest9
      GROUP BY p.id
    ),
    compkey AS (
      -- domain-level rollup: any classification for this prospect's domain company_key
      SELECT pr.id,
             bool_or(cl.pipeline_stage = 'p5') AS c_p5,
             bool_or(cl.pipeline_stage = 'p2') AS c_p2,
             bool_or(cl.pipeline_stage = 'p1') AS c_p1,
             bool_or(cl.pipeline_stage = 'p3') AS c_p3
      FROM prospects pr
      JOIN classifications cl ON pr.domain IS NOT NULL AND cl.company_key = 'dom:' || pr.domain
      GROUP BY pr.id
    ),
    ads AS (
      SELECT pr.id, ((e.dataforseo->>'running_google_ads') = 'true') AS runs_ads
      FROM prospects pr JOIN enrichment e ON e.domain = pr.domain
    ),
    roll AS (
      SELECT base.id,
             cr.last_at, cr.provider,
             (COALESCE(cr.has_p5,false) OR COALESCE(ck.c_p5,false)) AS p5,
             (COALESCE(cr.has_p2,false) OR COALESCE(ck.c_p2,false)) AS p2,
             (COALESCE(cr.has_p1,false) OR COALESCE(ck.c_p1,false)) AS p1,
             (COALESCE(cr.has_p3,false) OR COALESCE(ck.c_p3,false)) AS p3,
             COALESCE(cr.dnc, false)      AS dnc,
             COALESCE(a.runs_ads, false)  AS runs_ads,
             (cr.last_at IS NOT NULL)     AS called,
             base.source
      FROM prospects base
      LEFT JOIN callroll cr ON cr.id = base.id
      LEFT JOIN compkey  ck ON ck.id = base.id
      LEFT JOIN ads       a ON a.id  = base.id
    )
    UPDATE prospects pr SET
      last_called_at     = r.last_at,
      last_call_provider = r.provider,
      pipeline_stage = CASE WHEN r.p5 THEN 'p5' WHEN r.p2 THEN 'p2'
                            WHEN r.p1 THEN 'p1' WHEN r.p3 THEN 'p3' ELSE 'p4' END,
      p4_subpipeline = CASE
        WHEN r.p5 OR r.p2 OR r.p1 OR r.p3 THEN NULL
        WHEN r.dnc THEN 'dead'
        WHEN r.source = 'call_capture' AND r.provider = 'aircall' THEN 'captured_aircall'
        WHEN r.source = 'call_capture' AND r.called THEN 'captured_3cx'
        WHEN r.called THEN 'attempted'
        WHEN r.runs_ads THEN 'fresh_ads'
        ELSE 'fresh_unscanned' END,
      pipeline_synced_at = now()
    FROM roll r
    WHERE pr.id = r.id
    """
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(sql)
        touched = cur.rowcount
        conn.commit()
    dist = fetch_all(
        pool,
        "SELECT COALESCE(pipeline_stage,'?') AS stage, count(*) AS n FROM prospects GROUP BY 1",
    )
    stats = {"prospects": touched, **{str(r["stage"]): int(r["n"]) for r in dist}}
    log.info("sync_prospect_pipelines", **stats)
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
