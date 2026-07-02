"""Deep-enrich ALL Google-ads-confirmed domains (any revenue band): website / SEMrush / Apollo /
business-intel via enrich_domain_full. DataForSEO OFF (transparency already done; the keyword/gap
audit is a separate Batch-E step). WHOIS OFF in bulk (auDA throttles bulk .au — stays on-demand +
loop trickle). Idempotent: only domains missing apollo OR business_intel. Run:
  python3 scripts/enrich_running_ads.py
"""
import sys, time
from concurrent.futures import ThreadPoolExecutor
sys.path.insert(0, "/Users/vysakhvijayan/Traffic Radius Projects/3CX Sales-Funnel AI Reporting Agent/src")
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool
from funnel_agent.config import get_settings
from funnel_agent.enrich import enrich_domain_full

s = get_settings()
WORKERS = 6
pool = ConnectionPool(s.analytics_db_dsn, min_size=1, max_size=WORKERS + 2, kwargs={"row_factory": dict_row}, open=True)

with pool.connection() as c, c.cursor() as cur:
    cur.execute("""SELECT e.domain FROM enrichment e
        WHERE (e.dataforseo->>'running_google_ads')='true'
          AND (e.apollo IS NULL OR e.business_intel IS NULL)
        ORDER BY e.domain""")
    domains = [r["domain"] for r in cur.fetchall()]
print(f"running-ads domains to deep-enrich (missing apollo/intel): {len(domains):,}", flush=True)

from funnel_agent.transcribe import _openai
oai = _openai(s)
apollo = None
if s.apollo_enabled and s.apollo_api_key:
    from funnel_agent.enrichment.apollo import ApolloClient
    apollo = ApolloClient(s)

done = [0]; t0 = time.monotonic()
def work(dom):
    try:
        enrich_domain_full(pool, s, dom, with_dataforseo=False, with_whois=False, oai=oai, apollo=apollo)
    except Exception as exc:
        print(f"   ! {dom}: {str(exc)[:90]}", flush=True)
    done[0] += 1
    if done[0] % 200 == 0:
        el = time.monotonic() - t0
        print(f"  {done[0]:,}/{len(domains):,} | {done[0]/el:.1f}/s | {el:.0f}s", flush=True)

with ThreadPoolExecutor(max_workers=WORKERS) as ex:
    list(ex.map(work, domains))
print(f"DONE deep-enrich running-ads: {done[0]:,} in {time.monotonic()-t0:.0f}s", flush=True)
if apollo:
    apollo.close()
pool.close()
