"""FULL Transparency sweep over a revenue band's unchecked valid-website domains.
Usage: python3 scripts/transparency_band_all.py <lo> <hi>   e.g. 0.7 0.999
Revenue-desc priority; idempotent/resumable; covers domains with NO enrichment row
(upsert creates one); pauses & waits for auto-topup on low balance."""
import sys, time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
sys.path.insert(0, "/Users/vysakhvijayan/Traffic Radius Projects/3CX Sales-Funnel AI Reporting Agent/src")
from psycopg.types.json import Json
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool
from funnel_agent.config import get_settings
from funnel_agent.enrichment.dataforseo import DataForSEOClient

LO, HI = float(sys.argv[1]), float(sys.argv[2])
WORKERS, BATCH = 25, 2000
MIN_BALANCE, POLL, MAX_WAIT = 6.0, 90, 7200
s = get_settings()
pool = ConnectionPool(s.analytics_db_dsn, min_size=1, max_size=WORKERS + 2, kwargs={"row_factory": dict_row}, open=True)
client = DataForSEOClient(s)

# LEFT JOIN so domains with NO enrichment row are ALSO processed (work() upserts).
PENDING = """
  WITH d AS (SELECT lower(domain) AS dom, max(revenue_musd) AS rev FROM companies
             WHERE source='raghav' AND revenue_musd BETWEEN %s AND %s AND domain IS NOT NULL AND domain<>''
             GROUP BY lower(domain))
  SELECT d.dom FROM d LEFT JOIN enrichment e ON e.domain=d.dom
  WHERE e.domain IS NULL OR e.dataforseo IS NULL
  ORDER BY d.rev DESC NULLS LAST, d.dom LIMIT %s"""


def pending(lim):
    with pool.connection() as c, c.cursor() as cur:
        cur.execute(PENDING, (LO, HI, lim)); return [r["dom"] for r in cur.fetchall()]


def work(dom):
    try:
        ads = client.ads_search(dom)
        data = {"found": True, "transparency_only": True, "ads": ads,
                "running_google_ads": bool(ads.get("running_ads")),
                "fetched_at": datetime.now(timezone.utc).isoformat()}
        with pool.connection() as c, c.cursor() as cur:
            cur.execute("INSERT INTO enrichment (domain, dataforseo, fetched_at) VALUES (%s,%s,now()) "
                        "ON CONFLICT (domain) DO UPDATE SET dataforseo=EXCLUDED.dataforseo, fetched_at=now()",
                        (dom, Json(data)))
            c.commit()
        return data["running_google_ads"], float(ads.get("cost") or 0)
    except Exception as exc:
        print(f"   ! {dom}: {str(exc)[:80]}", flush=True); return None, 0.0


total = len(pending(1_000_000))
print(f"band ${LO}-${HI}M | pending {total:,} | start bal ${client.balance().get('balance')}", flush=True)
processed = running = errors = 0
cost = 0.0
t0 = time.monotonic()
while True:
    bal = client.balance().get("balance") or 0
    if bal < MIN_BALANCE:
        waited = 0
        while bal < MIN_BALANCE and waited < MAX_WAIT:
            print(f"  bal ${bal:.2f} low — waiting {POLL}s for auto-topup ({waited//60}m)...", flush=True)
            time.sleep(POLL); waited += POLL; bal = client.balance().get("balance") or 0
        if bal < MIN_BALANCE:
            print(f"STOP: no topup within {MAX_WAIT//60}m — {total-processed:,} pending (resumable).", flush=True); break
        print(f"  recharged to ${bal:.2f} — continuing.", flush=True)
    doms = pending(BATCH)
    if not doms: break
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        for r, cst in ex.map(work, doms):
            processed += 1; cost += cst
            if r is None: errors += 1
            elif r: running += 1
    el = time.monotonic() - t0
    print(f"  processed={processed:,} running_ads={running:,} errors={errors:,} cost=${cost:.2f} | {processed/el:.1f}/s | bal ${bal:.2f}", flush=True)

print(f"\nDONE band ${LO}-${HI}M: processed={processed:,} NEW running_ads={running:,} errors={errors:,} "
      f"ACTUAL_COST=${cost:.2f} in {time.monotonic()-t0:.0f}s | end bal ${client.balance().get('balance')}", flush=True)
client.close(); pool.close()
