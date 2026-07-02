"""Sync enrichment.website + enrichment.dataforseo LOCAL -> CLOUD for raghav domains in a
revenue band, via a SAFE per-column gap-fill MERGE (COALESCE keeps cloud's existing values;
only fills NULLs). Never clobbers cloud's apollo/whois/business_intel/semrush.
Usage: railway run --service Postgres python3 scripts/sync_enrichment_to_cloud.py <lo> <hi>
       (DATABASE_PUBLIC_URL is injected for the cloud side; local is localhost)."""
import os, sys, time
import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Json

LO = float(sys.argv[1]) if len(sys.argv) > 1 else 0
HI = float(sys.argv[2]) if len(sys.argv) > 2 else 999999
CLOUD = os.environ.get("DATABASE_PUBLIC_URL")
LOCAL = "postgresql://vysakhvijayan@localhost:5432/funnel"
assert CLOUD, "no DATABASE_PUBLIC_URL (run via railway run --service Postgres)"
print(f"band ${LO}-${HI}M | cloud {CLOUD.split('@')[-1][:40]} | local localhost", flush=True)

SCOPE = f"""(SELECT DISTINCT lower(domain) AS d FROM companies
             WHERE source='raghav' AND revenue_musd BETWEEN {LO} AND {HI}
               AND domain IS NOT NULL AND domain<>'')"""
SEL = f"""SELECT e.domain, e.website, e.dataforseo, e.status FROM enrichment e
          JOIN {SCOPE} s ON s.d = e.domain
          WHERE e.website IS NOT NULL OR e.dataforseo IS NOT NULL"""
UPSERT = """INSERT INTO enrichment (domain, website, dataforseo, status, fetched_at)
  VALUES (%s,%s,%s,%s, now())
  ON CONFLICT (domain) DO UPDATE SET
    website    = COALESCE(enrichment.website, EXCLUDED.website),
    dataforseo = COALESCE(enrichment.dataforseo, EXCLUDED.dataforseo),
    status     = COALESCE(enrichment.status, EXCLUDED.status),
    fetched_at = now()"""

lconn = psycopg.connect(LOCAL, row_factory=dict_row)
cconn = psycopg.connect(CLOUD, connect_timeout=30)
with lconn.cursor() as cur:
    cur.execute(f"SELECT count(*) AS n FROM ({SEL}) x"); total = cur.fetchone()["n"]
print(f"local rows to merge: {total:,}", flush=True)

sent = 0; t0 = time.monotonic(); batch = []
def flush():
    global sent
    if not batch: return
    with cconn.cursor() as cc:
        cc.executemany(UPSERT, batch)
    cconn.commit(); sent += len(batch)
    print(f"  merged {sent:,}/{total:,}", flush=True); batch.clear()

with lconn.cursor(name="enr_stream") as scur:
    scur.itersize = 2000; scur.execute(SEL)
    for r in scur:
        batch.append((r["domain"],
                      Json(r["website"]) if r["website"] is not None else None,
                      Json(r["dataforseo"]) if r["dataforseo"] is not None else None,
                      r["status"]))
        if len(batch) >= 2000: flush()
    flush()

with cconn.cursor() as cc:
    cc.execute(f"""SELECT count(*) FILTER (WHERE e.dataforseo IS NOT NULL) AS dfs,
        count(*) FILTER (WHERE (e.dataforseo->>'running_google_ads')='true') AS running
        FROM enrichment e JOIN {SCOPE} s ON s.d=e.domain""")
    row = cc.fetchone()
print(f"\nCLOUD band ${LO}-${HI}M -> dataforseo:{row[0]:,} running_ads:{row[1]:,} | merged {sent:,} in {time.monotonic()-t0:.0f}s", flush=True)
lconn.close(); cconn.close()
