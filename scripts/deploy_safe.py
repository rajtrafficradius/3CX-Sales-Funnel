#!/usr/bin/env python3
"""SAFE DEPLOY for the Lisa floor: PAUSE -> DRAIN -> DEPLOY -> RESUME -> VERIFY.

Why this exists (Vysakh, standing rule): a bare `railway up` restarts the web process and DROPS in-flight
Retell webhooks, so live calls get stuck at status='ongoing' and the concurrency gate jams. The only safe
sequence is to stop new dials, wait for the floor to go quiet, deploy, then turn the dials back on and
INDEPENDENTLY re-read the toggles to prove they came back.

Two failure modes this script exists to make impossible, both of which have actually bitten us:
  1. A silent PAUSE no-op. Hand-written UPDATEs have used the wrong column name (`autodial_enabled` — the
     real column is `autodial`), so the "pause" errored, the dialer kept dialling, and the drain loop was
     measuring a floor that was still live. Every write here is VERIFIED by reading the row back.
  2. A silent RESUME failure. A psycopg PoolTimeout once left the dialers paused after a deploy and nobody
     noticed until the morning. RESUME retries, then re-reads the toggles from a fresh connection.

Usage:
    python3 scripts/deploy_safe.py                 # full pause -> drain -> railway up -> resume -> verify
    python3 scripts/deploy_safe.py --resume-only   # recovery: just turn the dialers back on + verify
    python3 scripts/deploy_safe.py --no-deploy     # pause + drain only (then deploy by hand)

NEVER use `railway redeploy` — it rebuilds from the last *committed* image and reverts uncommitted work.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time

# id=1 singleton rows. lisa_control is Lisa-1, which is deliberately OFF — we record its state and restore
# exactly what we found rather than switching it on.
CONTROLS = ("lisa4_control", "lisa5_control", "lisa_control")
SERVICE = "3CX-Sales-Funnel"


def _env() -> None:
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
    for line in open(path):
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k, v.strip().strip('"').strip("'"))


def _conn():
    import psycopg
    from psycopg.rows import dict_row
    c = psycopg.connect(os.environ["RAILWAY_ANALYTICS_DSN"], autocommit=True,
                        row_factory=dict_row, connect_timeout=20)
    with c.cursor() as cur:
        cur.execute("SET statement_timeout='40s'")
    return c


def _read_toggles(c) -> dict[str, bool]:
    out = {}
    with c.cursor() as cur:
        for t in CONTROLS:
            cur.execute(f"SELECT autodial FROM {t} WHERE id=1")
            row = cur.fetchone()
            out[t] = bool(row and row["autodial"])
    return out


def _set(c, table: str, on: bool, who: str) -> bool:
    """Write ONE toggle and read it back. Returns True only when the DB actually reflects the value."""
    with c.cursor() as cur:
        cur.execute(f"UPDATE {table} SET autodial=%s, updated_at=now(), updated_by=%s WHERE id=1", (on, who))
        cur.execute(f"SELECT autodial FROM {table} WHERE id=1")
        row = cur.fetchone()
    return bool(row) and bool(row["autodial"]) is on


def pause(c, who: str) -> dict[str, bool]:
    before = _read_toggles(c)
    print(f"  state before: {before}")
    for t in CONTROLS:
        for attempt in range(3):
            if _set(c, t, False, who):
                print(f"  PAUSED  {t}")
                break
            time.sleep(2)
        else:
            sys.exit(f"FATAL: could not pause {t} — refusing to deploy onto a live floor.")
    return before


def drain(c, max_wait: int = 600) -> None:
    """Wait for in-flight calls to finish. A call still 'ongoing' when the process restarts never gets its
    completion webhook, so it stays 'ongoing' forever and eats a concurrency slot."""
    waited = 0
    while waited <= max_wait:
        with c.cursor() as cur:
            cur.execute("SELECT count(*) n FROM lisa_calls WHERE status='ongoing'")
            n = cur.fetchone()["n"]
        print(f"  drain t+{waited}s ongoing={n}", flush=True)
        if n == 0:
            return
        time.sleep(15)
        waited += 15
    sys.exit(f"FATAL: floor still busy after {max_wait}s — not deploying.")


def deploy() -> None:
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env = dict(os.environ)
    if env.get("RAILWAY_TOKEN_2"):
        env["RAILWAY_TOKEN"] = env["RAILWAY_TOKEN_2"]
    print(f"  railway up --service {SERVICE} --detach")
    r = subprocess.run(["railway", "up", "--service", SERVICE, "--detach"],
                       cwd=root, env=env, capture_output=True, text=True, timeout=900)
    print((r.stdout or "")[-2000:], (r.stderr or "")[-2000:])
    if r.returncode != 0:
        # Resume anyway — a paused floor is worse than an un-deployed fix.
        print("  deploy FAILED — resuming dialers before exiting")
        resume(_conn(), {"lisa4_control": True, "lisa5_control": True, "lisa_control": False})
        sys.exit("FATAL: railway up failed.")


def resume(c, before: dict[str, bool]) -> None:
    """Restore each toggle to what it was BEFORE the pause, then prove it from a fresh connection."""
    for t in CONTROLS:
        want = before.get(t, t != "lisa_control")
        for attempt in range(4):
            try:
                if _set(c, t, want, "deploy-safe-resume"):
                    print(f"  RESUMED {t} -> {want}")
                    break
            except Exception as exc:
                print(f"  retry {t}: {exc}")
                c = _conn()
            time.sleep(3)
        else:
            print(f"  *** WARNING: {t} did NOT resume — set it by hand ***")
    time.sleep(2)
    print(f"  VERIFY (fresh connection): {_read_toggles(_conn())}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--resume-only", action="store_true")
    ap.add_argument("--no-deploy", action="store_true")
    ap.add_argument("--who", default="deploy-safe")
    a = ap.parse_args()
    _env()
    c = _conn()

    if a.resume_only:
        print("RESUME ONLY")
        resume(c, {"lisa4_control": True, "lisa5_control": True, "lisa_control": False})
        return

    print("1) PAUSE");  before = pause(c, a.who)
    print("2) DRAIN");  drain(c)
    if a.no_deploy:
        print("--no-deploy: floor is paused + drained. Deploy, then run --resume-only.")
        return
    print("3) DEPLOY"); deploy()
    print("4) RESUME"); resume(_conn(), before)


if __name__ == "__main__":
    main()
