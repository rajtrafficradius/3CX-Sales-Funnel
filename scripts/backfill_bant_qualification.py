"""Backfill `qualified` to the definitive BANT rule on BOTH DBs, then re-aggregate.

Rule: qualified = Authority (decision-maker) AND (>=2 of Budget/Problem/Urgency/Aspiration/
Open-to-listening present).  Updates BOTH the boolean column AND the evidence JSON (call page
reads the JSON; the aggregate reads the column — they must agree).  Then syncs the 28 real
overrides cloud->local and re-aggregates every day on both DBs, and verifies they match.

Run: railway run --service Postgres python3 scripts/backfill_bant_qualification.py"""
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

# Qualification: authority AND problem(need) AND >=1 more of {budget, urgency(timeline),
# aspiration, open_to_listening}.  n = count of those "extra" supporting signals.
N_SQL = ("(COALESCE(budget,false)::int + COALESCE(urgency,false)::int "
         "+ COALESCE(aspiration,false)::int + COALESCE(open_to_listening,false)::int)")
Q_SQL = f"(COALESCE(authority,false) AND COALESCE(problem,false) AND {N_SQL} >= 1)"
BACKFILL = f"""
WITH calc AS (
  SELECT call_id, {Q_SQL} AS q, {N_SQL} AS n, COALESCE(authority,false) AS auth,
         COALESCE(problem,false) AS prob FROM classifications
)
UPDATE classifications c SET
  qualified = calc.q,
  evidence = CASE WHEN c.evidence ? 'qualified' THEN
    jsonb_set(
      jsonb_set(c.evidence, '{{qualified,value}}', to_jsonb(calc.q)),
      '{{qualified,evidence}}',
      to_jsonb(CASE
        WHEN NOT calc.auth THEN 'Not qualified — not the decision-maker (no buying authority).'
        WHEN NOT calc.prob THEN 'Not qualified — decision-maker but no real marketing problem/need established.'
        WHEN calc.q THEN 'Qualified — decision-maker with a real Need (Problem) plus ' || calc.n ||
                         ' more supporting signal(s) (Budget/Timeline/Aspiration/Open-to-listening).'
        ELSE 'Not qualified — decision-maker with a Need but no other supporting signal '
             '(Budget/Timeline/Aspiration/Open-to-listening).' END))
    ELSE c.evidence END
FROM calc WHERE calc.call_id = c.call_id
  AND (c.qualified IS DISTINCT FROM calc.q OR (c.evidence ? 'qualified' AND (c.evidence->'qualified'->>'value')::bool IS DISTINCT FROM calc.q));
"""

def backfill(label, dsn):
    with psycopg.connect(dsn) as c, c.cursor() as cur:
        cur.execute("select count(*) filter (where qualified) from classifications")
        before = cur.fetchone()[0]
        cur.execute(BACKFILL)
        changed = cur.rowcount
        c.commit()
        cur.execute("select count(*) filter (where qualified) from classifications")
        after = cur.fetchone()[0]
    print(f"[{label}] qualified rows: {before} -> {after}  ({changed} flipped)", flush=True)

# 1) overrides cloud -> local
OCOLS = ["call_id","qualified","reason","override_by","created_at","booking_outcome"]
with psycopg.connect(CLOUD, row_factory=dict_row) as c, c.cursor() as cur:
    cur.execute(f"select {', '.join(OCOLS)} from qualification_overrides"); crows = cur.fetchall()
with psycopg.connect(LOCAL) as c, c.cursor() as cur:
    cur.execute("select call_id from qualification_overrides"); lkeys={r[0] for r in cur.fetchall()}
    stray = lkeys - {r["call_id"] for r in crows}
    if stray: cur.execute("delete from qualification_overrides where call_id=any(%s)",(list(stray),))
    ins=(f"INSERT INTO qualification_overrides ({', '.join(OCOLS)}) VALUES ({', '.join(['%s']*len(OCOLS))}) "
         f"ON CONFLICT (call_id) DO UPDATE SET "+", ".join(f"{k}=EXCLUDED.{k}" for k in OCOLS if k!='call_id'))
    for r in crows: cur.execute(ins,[r[k] for k in OCOLS])
    c.commit()
print(f"overrides synced cloud({len(crows)}) -> local (deleted {len(stray)} stray)", flush=True)

# 2) backfill qualified on both
backfill("CLOUD", CLOUD)
backfill("LOCAL", LOCAL)

# 3) re-aggregate all days on both
def reagg(label, dsn, pause=0.0):
    # Resilient to the cloud proxy dropping the connection mid-run: retry each day on a fresh pool.
    # `pause` between days keeps a shared/live DB (cloud) responsive — set 0 for isolated local.
    import time as _t
    def newpool(): return ConnectionPool(dsn, min_size=1, max_size=2, kwargs={"row_factory": dict_row}, open=True)
    pool = newpool()
    with pool.connection() as c, c.cursor() as cur:
        cur.execute("select distinct (started_at at time zone %s)::date d from calls where in_scope and started_at is not null order by d desc",(s.tz,))
        ds=[r["d"] for r in cur.fetchall()]
    print(f"[{label}] re-aggregating {len(ds)} days (pause={pause}s)",flush=True)
    for d in ds:
        for attempt in (1, 2, 3):
            try:
                aggregate_day(pool, s, d); break
            except (psycopg.OperationalError, psycopg.InterfaceError) as e:
                print(f"  [{label}] {d} attempt {attempt} dropped ({str(e)[:50]}); reconnecting", flush=True)
                try: pool.close()
                except Exception: pass
                pool = newpool()
                if attempt == 3: raise
        if pause: _t.sleep(pause)
    pool.close()
reagg("LOCAL", LOCAL, pause=0.0)     # isolated dev DB — fast
reagg("CLOUD", CLOUD, pause=2.5)     # live DB — paced so the dashboard stays responsive

# 4) verify
print("\n=== LOCAL vs CLOUD (booked, qbooked) last 14d ===", flush=True)
def dfn(dsn):
    with psycopg.connect(dsn) as c, c.cursor() as cur:
        cur.execute("""select report_date, meetings_booked, qualified_booked from daily_funnel
          where bde_name='ALL' and track='combined' and report_date >= (now() at time zone %s)::date - 14
          order by report_date""",(s.tz,))
        return {r[0]:(r[1],r[2]) for r in cur.fetchall()}
L,C=dfn(LOCAL),dfn(CLOUD)
for d in sorted(set(L)|set(C)):
    print(f"  {d}  local={L.get(d)}  cloud={C.get(d)}  {'OK' if L.get(d)==C.get(d) else 'MISMATCH'}",flush=True)
print("done.",flush=True)
