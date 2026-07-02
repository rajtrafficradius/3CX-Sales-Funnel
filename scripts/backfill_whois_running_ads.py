"""Paced WHOIS backfill for ALL running-ads-confirmed domains missing WHOIS (any revenue band —
the loop's trickle only covers $1-10M, leaving other bands empty). auDA hard-rate-limits .au
WHOIS per IP, so this is SEQUENTIAL + small inter-request sleep + resilient; run off-hours, it
fills gradually over hours. Idempotent (only domains with whois IS NULL). Writes to whichever DB:
DATABASE_PUBLIC_URL if set (railway run --service Postgres) else localhost.
Run: railway run --service Postgres python3 scripts/backfill_whois_running_ads.py [limit]"""
import os, sys, time
sys.path.insert(0, "/Users/vysakhvijayan/Traffic Radius Projects/3CX Sales-Funnel AI Reporting Agent/src")
import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Json
from funnel_agent.enrichment.whois_lookup import lookup_whois

DSN = os.environ.get("DATABASE_PUBLIC_URL") or "postgresql://vysakhvijayan@localhost:5432/funnel"
LIMIT = int(sys.argv[1]) if len(sys.argv) > 1 else 100000
SLEEP = float(os.environ.get("WHOIS_SLEEP", "0.4"))
print(f"target {DSN.split('@')[-1][:40]} | limit {LIMIT} | sleep {SLEEP}s", flush=True)

conn = psycopg.connect(DSN, row_factory=dict_row)
with conn.cursor() as cur:
    cur.execute("""SELECT domain FROM enrichment
                   WHERE (dataforseo->>'running_google_ads')='true' AND whois IS NULL
                   ORDER BY domain LIMIT %s""", (LIMIT,))
    domains = [r["domain"] for r in cur.fetchall()]
print(f"running-ads domains missing WHOIS: {len(domains):,}", flush=True)

# Transient statuses (rate-limit / network) must NOT be persisted — leave whois NULL so the
# domain is retried later. Only persist a definitive result (found, or a genuine not-found).
TRANSIENT = {"http_429", "error", "timeout"}
def _transient(w):
    st = str(w.get("status") or "").lower()
    return (not w.get("found")) and ("429" in st or "timeout" in st or "timed out" in st or st in TRANSIENT)

found = tried = err = skip = throttle = 0; t0 = time.monotonic()
for i, dom in enumerate(domains, 1):
    try:
        w = lookup_whois(dom)
    except Exception as exc:
        w = {"found": False, "status": "error", "error": str(exc)[:100]}
    tried += 1
    if _transient(w):
        throttle += 1
        # back off harder when auDA starts throttling, and skip persisting
        if throttle % 5 == 0: time.sleep(30)
        time.sleep(SLEEP); continue
    if w.get("found"): found += 1
    with conn.cursor() as cur:
        cur.execute("INSERT INTO enrichment (domain, whois, fetched_at) VALUES (%s,%s,now()) "
                    "ON CONFLICT (domain) DO UPDATE SET whois=EXCLUDED.whois, fetched_at=now()",
                    (dom, Json(w)))
        conn.commit()
    if i % 50 == 0:
        print(f"  {i:,}/{len(domains):,} | found={found:,} throttled(skipped)={throttle:,} | {i/(time.monotonic()-t0):.2f}/s", flush=True)
    time.sleep(SLEEP)
err = throttle
conn.close()
print(f"DONE whois backfill: tried {tried:,}, found {found:,}, errors {err}, in {time.monotonic()-t0:.0f}s", flush=True)
