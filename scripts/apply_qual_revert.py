"""Apply the qualification-model revert: the AI self-qualifies every booked meeting (firm or
tentative); BDM overrides win. Steps:
  1) sync qualification_overrides cloud->local (cloud = source of truth) so local matches.
  2) re-aggregate ALL days on BOTH cloud and local with the (new) aggregate rule.
  3) verify local == cloud on completed days.
Run: railway run --service Postgres python3 scripts/apply_qual_revert.py"""
import os, sys
sys.path.insert(0, "/Users/vysakhvijayan/Traffic Radius Projects/3CX Sales-Funnel AI Reporting Agent/src")
import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool
from funnel_agent.config import get_settings
from funnel_agent.aggregate import aggregate_day

s = get_settings()
CLOUD = os.environ["DATABASE_PUBLIC_URL"]
LOCAL = "postgresql://vysakhvijayan@localhost:5432/funnel"

# 1) sync overrides cloud -> local
OCOLS = ["call_id","qualified","reason","override_by","created_at","booking_outcome"]
with psycopg.connect(CLOUD, row_factory=dict_row) as c, c.cursor() as cur:
    cur.execute(f"select {', '.join(OCOLS)} from qualification_overrides")
    crows = cur.fetchall()
with psycopg.connect(LOCAL) as c, c.cursor() as cur:
    cur.execute("select call_id from qualification_overrides")
    lkeys = {r[0] for r in cur.fetchall()}
    ckeys = {r["call_id"] for r in crows}
    # delete local-only (not on cloud) — keeps local a faithful mirror
    stray = lkeys - ckeys
    if stray:
        cur.execute("delete from qualification_overrides where call_id = any(%s)", (list(stray),))
    ins = (f"INSERT INTO qualification_overrides ({', '.join(OCOLS)}) VALUES ({', '.join(['%s']*len(OCOLS))}) "
           f"ON CONFLICT (call_id) DO UPDATE SET " + ", ".join(f"{k}=EXCLUDED.{k}" for k in OCOLS if k!="call_id"))
    for r in crows:
        cur.execute(ins, [r[k] for k in OCOLS])
    c.commit()
print(f"overrides synced: cloud={len(crows)} -> local (deleted {len(stray)} stray local-only)", flush=True)

def days_of(dsn):
    with psycopg.connect(dsn) as c, c.cursor() as cur:
        cur.execute("select distinct (started_at at time zone %s)::date d from calls "
                    "where in_scope and started_at is not null order by d", (s.tz,))
        return [r[0] for r in cur.fetchall()]

def reagg(label, dsn):
    pool = ConnectionPool(dsn, min_size=1, max_size=2, kwargs={"row_factory": dict_row}, open=True)
    ds = days_of(dsn)
    print(f"[{label}] re-aggregating {len(ds)} days ({ds[0]}..{ds[-1]})", flush=True)
    for d in ds:
        aggregate_day(pool, s, d)
    pool.close()

reagg("CLOUD", CLOUD)
reagg("LOCAL", LOCAL)

# 3) verify recent completed days
print("\n=== verify LOCAL vs CLOUD (booked, qbooked) ===", flush=True)
def dfn(dsn):
    with psycopg.connect(dsn) as c, c.cursor() as cur:
        cur.execute("""select report_date, meetings_booked, qualified_booked from daily_funnel
                       where bde_name='ALL' and track='combined'
                       and report_date >= (now() at time zone %s)::date - 14 order by report_date""",(s.tz,))
        return {r[0]:(r[1],r[2]) for r in cur.fetchall()}
L, C = dfn(LOCAL), dfn(CLOUD)
for d in sorted(set(L)|set(C)):
    lv, cv = L.get(d), C.get(d)
    print(f"  {d}  local={lv}  cloud={cv}  {'OK' if lv==cv else 'MISMATCH'}", flush=True)
print("done.", flush=True)
