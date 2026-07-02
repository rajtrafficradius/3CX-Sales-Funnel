"""Sync the DEEP enrichment (apollo + business_intel + website) LOCAL -> CLOUD for all
Google-ads-confirmed (running-ads) domains, via a SAFE per-column gap-fill MERGE (COALESCE keeps
cloud's existing values; only fills where cloud is NULL). Never clobbers cloud data.

Scope = local enrichment rows where running_google_ads='true' AND (apollo OR business_intel present).
Run: railway run --service Postgres python3 scripts/sync_running_ads_enrichment.py"""
import os, sys, time
import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Json

CLOUD = os.environ.get("DATABASE_PUBLIC_URL")
LOCAL = "postgresql://vysakhvijayan@localhost:5432/funnel"
assert CLOUD, "no DATABASE_PUBLIC_URL (run via railway run --service Postgres)"
print(f"cloud {CLOUD.split('@')[-1][:40]} | local localhost", flush=True)

SEL = """SELECT domain, apollo, business_intel, website, dataforseo, status
         FROM enrichment
         WHERE (dataforseo->>'running_google_ads')='true'
           AND (apollo IS NOT NULL OR business_intel IS NOT NULL)"""
# Gap-fill: only fill cloud NULLs; keep any value cloud already has.
UPSERT = """INSERT INTO enrichment (domain, apollo, business_intel, website, dataforseo, status, fetched_at)
  VALUES (%s,%s,%s,%s,%s,%s, now())
  ON CONFLICT (domain) DO UPDATE SET
    apollo         = COALESCE(enrichment.apollo, EXCLUDED.apollo),
    business_intel = COALESCE(enrichment.business_intel, EXCLUDED.business_intel),
    website        = COALESCE(enrichment.website, EXCLUDED.website),
    dataforseo     = COALESCE(enrichment.dataforseo, EXCLUDED.dataforseo),
    status         = COALESCE(enrichment.status, EXCLUDED.status),
    fetched_at     = now()"""

lconn = psycopg.connect(LOCAL, row_factory=dict_row)
cconn = psycopg.connect(CLOUD, connect_timeout=30)
with lconn.cursor() as cur:
    cur.execute(f"SELECT count(*) AS n FROM ({SEL}) x"); total = cur.fetchone()["n"]
print(f"local running-ads rows with deep enrichment: {total:,}", flush=True)

def J(v): return Json(v) if v is not None else None
sent = 0; t0 = time.monotonic(); batch = []
def flush():
    global sent
    if not batch: return
    with cconn.cursor() as cc:
        cc.executemany(UPSERT, batch)
    cconn.commit(); sent += len(batch)
    print(f"  merged {sent:,}/{total:,}", flush=True); batch.clear()

with lconn.cursor(name="ra_stream") as scur:
    scur.itersize = 1000; scur.execute(SEL)
    for r in scur:
        batch.append((r["domain"], J(r["apollo"]), J(r["business_intel"]),
                      J(r["website"]), J(r["dataforseo"]), r["status"]))
        if len(batch) >= 1000: flush()
    flush()

with cconn.cursor() as cc:
    cc.execute("""SELECT
      count(*) FILTER (WHERE (dataforseo->>'running_google_ads')='true') running,
      count(*) FILTER (WHERE (dataforseo->>'running_google_ads')='true' AND apollo IS NOT NULL) w_apollo,
      count(*) FILTER (WHERE (dataforseo->>'running_google_ads')='true' AND business_intel IS NOT NULL) w_intel,
      count(*) FILTER (WHERE (dataforseo->>'running_google_ads')='true' AND apollo IS NOT NULL AND business_intel IS NOT NULL) fully
      FROM enrichment""")
    r = cc.fetchone()
print(f"\nCLOUD after sync -> running:{r[0]:,} apollo:{r[1]:,} intel:{r[2]:,} fully:{r[3]:,} | merged {sent:,} in {time.monotonic()-t0:.0f}s", flush=True)
lconn.close(); cconn.close()
