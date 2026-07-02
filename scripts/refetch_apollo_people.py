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
from funnel_agent.config import get_settings
from funnel_agent.enrichment.apollo import ApolloClient

s = get_settings()
# Target whichever DB: DATABASE_PUBLIC_URL if set (railway run) else localhost.
LOCAL = os.environ.get("DATABASE_PUBLIC_URL") or "postgresql://vysakhvijayan@localhost:5432/funnel"
LIMIT = int(sys.argv[1]) if len(sys.argv) > 1 else 100000
SLEEP = float(os.environ.get("APOLLO_SLEEP", "1.3"))   # Apollo hard-limits bursts — pace it
assert s.apollo_enabled and s.apollo_api_key, "Apollo not configured"
print(f"target {LOCAL.split('@')[-1][:40]} | limit {LIMIT} | sleep {SLEEP}s (sequential)", flush=True)

conn = psycopg.connect(LOCAL, row_factory=dict_row, autocommit=True)
with conn.cursor() as cur:
    cur.execute("""SELECT domain, apollo->>'id' AS org_id,
                          jsonb_array_length(COALESCE(apollo->'people','[]'::jsonb)) AS n_people,
                          (apollo->'people'->0->>'rank') AS first_rank
                   FROM enrichment
                   WHERE (dataforseo->>'running_google_ads')='true' AND apollo IS NOT NULL""")
    rows = cur.fetchall()
# Only re-fetch rows that are stale (people never ranked) — idempotent on re-runs.
todo = [r for r in rows if r["first_rank"] is None][:LIMIT]
print(f"running-ads with apollo: {len(rows):,} | stale (need re-fetch): {len(todo):,}", flush=True)

apollo = ApolloClient(s)
ok = people_total = fail = empty = 0; t0 = time.monotonic()
for i, r in enumerate(todo, 1):                      # sequential — Apollo rate-limits bursts
    dom = r["domain"]
    try:
        people = apollo.search_decision_makers(dom, org_id=r["org_id"] or None)
        if not people:
            empty += 1                                # do NOT overwrite existing data with empty
        else:
            with conn.cursor() as wcur:
                wcur.execute("UPDATE enrichment SET apollo = jsonb_set(apollo, '{people}', %s::jsonb), fetched_at=now() WHERE domain=%s",
                             (Json(people), dom))
            ok += 1; people_total += len(people)
    except Exception as exc:
        fail += 1
        if fail <= 5: print(f"  fail {dom}: {str(exc)[:80]}", flush=True)
    if i % 100 == 0:
        print(f"  {i:,}/{len(todo):,} | updated={ok:,} empty={empty:,} fail={fail} | {i/(time.monotonic()-t0):.2f}/s", flush=True)
    time.sleep(SLEEP)
apollo.close()
print(f"DONE refetch: {ok:,} updated ({people_total:,} people), {empty:,} empty/skipped, {fail} failed, in {time.monotonic()-t0:.0f}s", flush=True)
