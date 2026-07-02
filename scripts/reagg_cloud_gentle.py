"""Gently re-aggregate CLOUD daily_funnel for all days (recent-first), with a pause between days
so the live dashboard is never starved (the earlier all-at-once run overloaded the small Railway
Postgres). Resilient to proxy drops. Run: railway run --service Postgres python3 scripts/reagg_cloud_gentle.py"""
import os, sys, time
sys.path.insert(0, "/Users/vysakhvijayan/Traffic Radius Projects/3CX Sales-Funnel AI Reporting Agent/src")
import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool
from funnel_agent.config import get_settings
from funnel_agent.aggregate import aggregate_day

s = get_settings()
CLOUD = os.environ["DATABASE_PUBLIC_URL"]
PAUSE = 3.0  # seconds between days — keeps the app responsive

def newpool(): return ConnectionPool(CLOUD, min_size=1, max_size=2, kwargs={"row_factory": dict_row}, open=True)
pool = newpool()
with pool.connection() as c, c.cursor() as cur:
    cur.execute("select distinct (started_at at time zone %s)::date d from calls "
                "where in_scope and started_at is not null order by d desc", (s.tz,))
    days = [r["d"] for r in cur.fetchall()]
with pool.connection() as c, c.cursor() as cur:
    cur.execute("select (now() at time zone %s)::date AS d", (s.tz,))
    today = cur.fetchone()["d"]
# skip today — the live loop owns it; re-aggregating it here would race the loop
days = [d for d in days if d != today]
print(f"gently re-aggregating {len(days)} cloud days (recent-first), pause={PAUSE}s", flush=True)
for i, d in enumerate(days, 1):
    for attempt in (1, 2, 3):
        try:
            aggregate_day(pool, s, d); break
        except (psycopg.OperationalError, psycopg.InterfaceError) as e:
            print(f"  {d} attempt {attempt} dropped ({str(e)[:40]}); reconnecting", flush=True)
            try: pool.close()
            except Exception: pass
            pool = newpool()
            if attempt == 3: raise
    if i % 10 == 0: print(f"  ...{i}/{len(days)} done (through {d})", flush=True)
    time.sleep(PAUSE)
pool.close()
print("done gentle cloud re-aggregate.", flush=True)
