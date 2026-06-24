#!/bin/sh
# Bulk-copy the LOCAL analytics DB into the Railway analytics DB so the cloud
# dashboard shows EXACTLY the same data as local. Idempotent: truncates the
# Railway analytics tables, then reloads them from local. No secrets are stored
# in this file — pass the Railway DSN as an argument.
#
#   Usage:  scripts/sync_local_to_railway.sh "<RAILWAY_ANALYTICS_DB_DSN>"
#
# The LOCAL DSN is read from .env (ANALYTICS_DB_DSN). Run from the repo root.
set -e

RAILWAY_DSN="$1"
if [ -z "$RAILWAY_DSN" ]; then
  echo "usage: $0 <railway-analytics-db-dsn>" >&2
  exit 2
fi

# Homebrew libpq (pg_dump / psql) — adjust if your tools live elsewhere.
export PATH="/opt/homebrew/opt/libpq/bin:$PATH"

LOCAL_DSN=$(grep -E '^ANALYTICS_DB_DSN=' .env | sed 's/^ANALYTICS_DB_DSN=//' | tr -d '"')
if [ -z "$LOCAL_DSN" ]; then
  echo "ANALYTICS_DB_DSN not found in .env" >&2
  exit 2
fi

# Sync ALL persistent tables so cloud == local. Order is not critical: the data load
# uses --disable-triggers (FK checks off during COPY) and TRUNCATE … CASCADE clears
# dependents. `users` is included so the team can log in on cloud (no lockout);
# `companies`/`prospects`/pipeline/enrichment power the Database view + pipelines.
# `sessions` is intentionally EXCLUDED (ephemeral login cookies — don't copy).
TABLES="bde_agents calls transcripts classifications meetings daily_funnel processing_state prospects companies enrichment prospect_pipeline prospect_assignments calendar_events qualification_overrides whatsapp_messages users"
CSV=$(echo "$TABLES" | tr ' ' ',')

echo "1/4  ensuring Railway schema is current (adds new columns if missing)…"
ANALYTICS_DB_DSN="$RAILWAY_DSN" .venv/bin/funnel-agent init-db

echo "2/4  clearing Railway analytics tables…"
psql "$RAILWAY_DSN" -v ON_ERROR_STOP=1 -c "TRUNCATE $CSV RESTART IDENTITY CASCADE;"

echo "3/4  copying local data -> Railway…"
DUMP_ARGS=""
for t in $TABLES; do DUMP_ARGS="$DUMP_ARGS -t $t"; done
# shellcheck disable=SC2086
pg_dump "$LOCAL_DSN" --data-only --no-owner --no-privileges --disable-triggers $DUMP_ARGS \
  | psql "$RAILWAY_DSN" -v ON_ERROR_STOP=1 -1

echo "4/4  verifying row counts (local vs railway)…"
for t in $TABLES; do
  L=$(psql "$LOCAL_DSN"   -tAc "SELECT count(*) FROM $t")
  R=$(psql "$RAILWAY_DSN" -tAc "SELECT count(*) FROM $t")
  if [ "$L" = "$R" ]; then status="OK"; else status="MISMATCH"; fi
  printf '  %-18s local=%-7s railway=%-7s %s\n' "$t" "$L" "$R" "$status"
done

echo "sync complete."
