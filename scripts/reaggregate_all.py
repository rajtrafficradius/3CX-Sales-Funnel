"""Re-aggregate daily_funnel for ALL days with call data (applies the CURRENT aggregation rules
to history). Targets cloud when DATABASE_PUBLIC_URL is set (railway run --service Postgres),
else local. Uses the local (deployed) aggregate_day code, so it applies whatever rules are in
this checkout regardless of which DB it writes to."""
import os, sys
sys.path.insert(0, "/Users/vysakhvijayan/Traffic Radius Projects/3CX Sales-Funnel AI Reporting Agent/src")
import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool
from funnel_agent.config import get_settings
from funnel_agent.aggregate import aggregate_day

s = get_settings()
DSN = os.environ.get("DATABASE_PUBLIC_URL") or "postgresql://vysakhvijayan@localhost:5432/funnel"
print("target:", DSN.split("@")[-1][:45], flush=True)
pool = ConnectionPool(DSN, min_size=1, max_size=2, kwargs={"row_factory": dict_row}, open=True)
with pool.connection() as c, c.cursor() as cur:
    cur.execute("select distinct (started_at at time zone %s)::date d from calls "
                "where in_scope and started_at is not null order by d", (s.tz,))
    days = [r["d"] for r in cur.fetchall()]
print(f"re-aggregating {len(days)} days ({days[0]}..{days[-1]})", flush=True)
for d in days:
    aggregate_day(pool, s, d)
print("done. recent ALL booked/qbooked:", flush=True)
with pool.connection() as c, c.cursor() as cur:
    cur.execute("select report_date, meetings_booked, qualified_booked from daily_funnel "
                "where bde_name='ALL' and track='combined' order by report_date desc limit 5")
    for r in cur.fetchall():
        print(f"   {r['report_date']}  booked={r['meetings_booked']}  qbooked={r['qualified_booked']}", flush=True)
pool.close()
