#!/bin/sh
set -e

# Apply the analytics schema (idempotent) so a fresh Railway Postgres is ready.
funnel-agent init-db

# Web service: bind to Railway's injected $PORT on all interfaces.
# Any other command (daily, roster-sync, backfill, report) passes straight through.
if [ "$1" = "dashboard" ]; then
  exec funnel-agent dashboard --host 0.0.0.0 --port "${PORT:-8080}"
fi

exec funnel-agent "$@"
