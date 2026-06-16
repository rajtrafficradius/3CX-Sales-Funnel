#!/bin/bash
# ============================================================================
# Double-click this file in Finder to launch the 3CX Sales-Funnel dashboard.
# It starts the local database, runs the web server, and opens your browser.
# To stop it: come back to this Terminal window and press Ctrl+C.
# ============================================================================
cd "$(dirname "$0")" || exit 1

echo "Starting the local database..."
/opt/homebrew/bin/brew services start postgresql@16 >/dev/null 2>&1

echo "Starting the dashboard..."
source .venv/bin/activate

# Open the browser a couple of seconds after the server boots.
( sleep 2; open "http://localhost:8080" ) &

echo ""
echo "  Dashboard:  http://localhost:8080"
echo "  (Press Ctrl+C in this window to stop it.)"
echo ""
funnel-agent dashboard --host 127.0.0.1 --port 8080
