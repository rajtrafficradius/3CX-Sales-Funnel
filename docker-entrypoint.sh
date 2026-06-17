#!/bin/sh
set -e

if [ -z "$ANALYTICS_DB_DSN" ]; then
  echo "WARNING: ANALYTICS_DB_DSN is not set. On Railway set it to \${{Postgres.DATABASE_URL}}." >&2
fi

# Apply the analytics schema, retrying while the Postgres plugin finishes
# provisioning. Don't let a transient DB hiccup crash-loop the web service —
# after a few attempts we start anyway so /healthz can respond and the deploy
# goes green; the dashboard recovers automatically once the DB is reachable.
n=0
until funnel-agent init-db; do
  n=$((n + 1))
  if [ "$n" -ge 12 ]; then
    echo "init-db still failing after $n attempts; starting the server anyway." >&2
    break
  fi
  echo "init-db failed (attempt $n) — waiting 5s for the database..." >&2
  sleep 5
done

# Web service: bind to Railway's injected $PORT on all interfaces.
# Any other command (daily, roster-sync, backfill, report) passes straight through.
if [ "$1" = "dashboard" ]; then
  exec funnel-agent dashboard --host 0.0.0.0 --port "${PORT:-8080}"
fi

exec funnel-agent "$@"
