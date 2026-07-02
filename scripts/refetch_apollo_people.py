"""Re-fetch Apollo decision-makers with the NEW code (per_page=25 + founder-first ranking) for all
running-ads-confirmed domains, and update enrichment.apollo->people. FREE (api_search, no reveal
flags — no Apollo credits). Fixes stale records enriched with the old per_page=10/no-rank code
(e.g. silverback, whose founder wasn't in the first 10). Local only; re-sync to cloud after.
Run: python3 scripts/refetch_apollo_people.py   (uses localhost + .env Apollo key)"""
import os, sys, time
sys.path.insert(0, "/Users/vysakhvijayan/Traffic Radius Projects/3CX Sales-Funnel AI Reporting Agent/src")
import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Json
from concurrent.futures import ThreadPoolExecutor
from funnel_agent.config import get_settings
from funnel_agent.enrichment.apollo import ApolloClient

s = get_settings()
LOCAL = "postgresql://vysakhvijayan@localhost:5432/funnel"
assert s.apollo_enabled and s.apollo_api_key, "Apollo not configured"

conn = psycopg.connect(LOCAL, row_factory=dict_row, autocommit=True)
with conn.cursor() as cur:
    cur.execute("""SELECT domain, apollo->>'id' AS org_id,
                          jsonb_array_length(COALESCE(apollo->'people','[]'::jsonb)) AS n_people,
                          (apollo->'people'->0->>'rank') AS first_rank
                   FROM enrichment
                   WHERE (dataforseo->>'running_google_ads')='true' AND apollo IS NOT NULL""")
    rows = cur.fetchall()
# Only re-fetch rows that are stale (people never ranked) — idempotent on re-runs.
todo = [r for r in rows if r["first_rank"] is None]
print(f"running-ads with apollo: {len(rows):,} | stale (need re-fetch): {len(todo):,}", flush=True)

apollo = ApolloClient(s)
def work(r):
    dom = r["domain"]
    try:
        people = apollo.search_decision_makers(dom, org_id=r["org_id"] or None)
        if not people:
            return 0  # empty (rate-limited OR genuinely no DMs) — do NOT overwrite existing data
        with psycopg.connect(LOCAL) as wc, wc.cursor() as wcur:
            wcur.execute("UPDATE enrichment SET apollo = jsonb_set(apollo, '{people}', %s::jsonb), fetched_at=now() WHERE domain=%s",
                         (Json(people), dom))
            wc.commit()
        return len(people)
    except Exception as exc:
        print(f"  fail {dom}: {str(exc)[:80]}", flush=True); return -1

ok = people_total = fail = empty = 0; t0 = time.monotonic()
with ThreadPoolExecutor(max_workers=2) as ex:
    for i, n in enumerate(ex.map(work, todo), 1):
        if n < 0: fail += 1
        elif n == 0: empty += 1
        else: ok += 1; people_total += n
        if i % 200 == 0:
            print(f"  {i:,}/{len(todo):,} | updated={ok:,} empty/skip={empty:,} fail={fail} | {i/(time.monotonic()-t0):.1f}/s", flush=True)
apollo.close()
print(f"DONE refetch: {ok:,} updated ({people_total:,} people), {empty:,} empty/skipped, {fail} failed, in {time.monotonic()-t0:.0f}s", flush=True)
