#!/bin/sh
set -e

if [ -z "$ANALYTICS_DB_DSN" ]; then
  echo "WARNING: ANALYTICS_DB_DSN is not set. On Railway set it to \${{Postgres.DATABASE_URL}}." >&2
fi

# Web service: bind to Railway's injected $PORT on all interfaces.
# Any other command (daily, roster-sync, backfill, report) passes straight through.
if [ "$1" = "dashboard" ]; then
  # IMPORTANT (deploy reliability): start the web server FIRST and run schema-migrate
  # (init-db) + the refresh loop in the BACKGROUND. init-db issues DDL (ALTER TABLE …)
  # which can be briefly BLOCKED by a lock held by the PREVIOUS deploy's refresh loop
  # during the zero-downtime overlap; if that DDL ran before binding the port, /healthz
  # would time out and the deploy would FAIL (healthcheck). /healthz needs no DB, so we
  # bind immediately and migrate in the background (idempotent; columns already exist).
  (
    n=0
    until funnel-agent init-db; do
      n=$((n + 1))
      if [ "$n" -ge 24 ]; then
        echo "init-db still failing after $n attempts; continuing (schema likely already current)." >&2
        break
      fi
      echo "init-db blocked/failed (attempt $n) — retrying in 5s..." >&2
      sleep 5
    done
    # Self-updating loop: keep the data live (ingest -> transcribe -> classify ->
    # aggregate -> capture -> pipeline2 -> whatsapp) every REFRESH_INTERVAL seconds.
    # Disable with REFRESH_IN_WEB=0.
    if [ "${REFRESH_IN_WEB:-1}" = "1" ]; then
      sleep "${REFRESH_INITIAL_DELAY:-30}"
      while true; do
        echo "[refresh-loop] $(date -u +%FT%TZ) starting refresh" >&2
        funnel-agent refresh --days "${REFRESH_DAYS:-2}" >&2 || \
          echo "[refresh-loop] refresh failed (continuing)" >&2
        sleep "${REFRESH_INTERVAL:-60}"
      done
    fi
  ) &
  exec funnel-agent dashboard --host 0.0.0.0 --port "${PORT:-8080}"
fi

# Non-dashboard commands: run schema-migrate first (foreground), then the command.
n=0
until funnel-agent init-db; do
  n=$((n + 1))
  if [ "$n" -ge 12 ]; then
    echo "init-db still failing after $n attempts; running the command anyway." >&2
    break
  fi
  echo "init-db failed (attempt $n) — waiting 5s for the database..." >&2
  sleep 5
done
exec funnel-agent "$@"
