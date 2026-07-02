"""Align LOCAL classifications to CLOUD (cloud = production source of truth) for completed days,
then re-aggregate the affected local days so history matches cloud exactly.

Only touches calls whose classification actually differs (surgical). Never touches today's
live day. Run: railway run --service Postgres python3 scripts/reconcile_local_to_cloud.py
(needs DATABASE_PUBLIC_URL for cloud; local is hard-coded)."""
import os, sys
sys.path.insert(0, "/Users/vysakhvijayan/Traffic Radius Projects/3CX Sales-Funnel AI Reporting Agent/src")
import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool
from funnel_agent.config import get_settings
from funnel_agent.aggregate import aggregate_day

s = get_settings()
CLOUD = os.environ["DATABASE_PUBLIC_URL"]
LOCAL = "postgresql://vysakhvijayan@localhost:5432/funnel"

# aggregate-affecting columns — if any of these differ, the day's numbers differ
KEYCOLS = ["rpc_connect","full_pitch","is_lead","qualified","meeting_booked","call_outcome",
           "meeting_confirmation","lead_temperature","pipeline","meeting_rescheduled",
           "booking_status","meeting_datetime","booking_already_exists","company_key"]

def cols(dsn):
    with psycopg.connect(dsn) as c, c.cursor() as cur:
        cur.execute("select column_name from information_schema.columns where table_name='classifications' order by ordinal_position")
        return [r[0] for r in cur.fetchall()]

ALLCOLS = cols(LOCAL)
SET = ", ".join(f"{c}=EXCLUDED.{c}" for c in ALLCOLS if c != "call_id")
INS = f"INSERT INTO classifications ({', '.join(ALLCOLS)}) VALUES ({', '.join(['%s']*len(ALLCOLS))}) " \
      f"ON CONFLICT (call_id) DO UPDATE SET {SET}"

def day_rows(dsn, day):
    q = f"""select {', '.join('cl.'+c for c in ALLCOLS)} from calls c join classifications cl on cl.call_id=c.call_id
            where c.in_scope and (c.started_at at time zone %s)::date=%s"""
    with psycopg.connect(dsn, row_factory=dict_row) as c, c.cursor() as cur:
        cur.execute(q, (s.tz, day)); return {r["call_id"]: r for r in cur.fetchall()}

# completed days with data (exclude today)
with psycopg.connect(CLOUD) as c, c.cursor() as cur:
    cur.execute("""select (started_at at time zone %s)::date d, count(*) filter (where in_scope) n,
                   (now() at time zone %s)::date today
                   from calls where started_at >= now() - interval '25 days'
                   group by d having count(*) filter (where in_scope) > 5 order by d""", (s.tz, s.tz))
    rows = cur.fetchall()
    days = [r[0] for r in rows if r[0] < r[2]]  # exclude today (r[2])
print("completed days to check:", [str(d) for d in days], flush=True)

pool = ConnectionPool(LOCAL, min_size=1, max_size=2, kwargs={"row_factory": dict_row}, open=True)

# PHASE 1: sync ALL divergent classifications across ALL days FIRST (the qualified_booked
# subquery matches a prospect across ALL dates, so every day must see cloud-consistent data
# before ANY re-aggregation).
print("--- phase 1: sync classifications ---", flush=True)
for d in days:
    C, L = day_rows(CLOUD, d), day_rows(LOCAL, d)
    diverg = [cid for cid in (set(C) & set(L)) if any(C[cid][k] != L[cid][k] for k in KEYCOLS)]
    only_cloud = list(set(C) - set(L))
    to_sync = diverg + only_cloud
    if not to_sync:
        print(f"  {d}: already matches", flush=True); continue
    with pool.connection() as lc, lc.cursor() as cur:
        for cid in to_sync:
            r = C[cid]
            cur.execute(INS, [Jsonb(r[c]) if isinstance(r[c], (dict, list)) else r[c] for c in ALLCOLS])
    print(f"  {d}: synced {len(diverg)} divergent + {len(only_cloud)} cloud-only", flush=True)

# PHASE 2: now re-aggregate every day (all cross-day references are cloud-consistent).
print("--- phase 2: re-aggregate all days ---", flush=True)
for d in days:
    aggregate_day(pool, s, d)

print("\n=== verify LOCAL vs CLOUD ALL booked/qbooked after reconcile ===", flush=True)
for d in days:
    with psycopg.connect(LOCAL) as c, c.cursor() as cur:
        cur.execute("select meetings_booked,qualified_booked from daily_funnel where bde_name='ALL' and track='combined' and report_date=%s",(d,))
        lb = cur.fetchone()
    with psycopg.connect(CLOUD) as c, c.cursor() as cur:
        cur.execute("select meetings_booked,qualified_booked from daily_funnel where bde_name='ALL' and track='combined' and report_date=%s",(d,))
        cb = cur.fetchone()
    tag = "OK" if lb == cb else "MISMATCH"
    print(f"  {d}  local={lb}  cloud={cb}  {tag}", flush=True)
pool.close()
print("done.", flush=True)
