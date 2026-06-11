#!/bin/bash
# Viral Idea Engine — Mac launcher
cd "$(dirname "$0")"

# Create venv if missing
if [ ! -d ".venv" ]; then
  echo "Setting up for first time... (this takes about a minute)"
  python3 -m venv .venv
fi

# Activate and install / upgrade deps
source .venv/bin/activate
pip install -q -r requirements.txt

# Launch server
echo "Starting Viral Idea Engine..."
python3 -m uvicorn app.main:app --host 127.0.0.1 --port 8787 &
SERVER_PID=$!

# Wait for server to be ready
sleep 2

# Open browser
open "http://127.0.0.1:8787"

echo ""
echo "Server is running at http://127.0.0.1:8787"
echo "Close this window or press Ctrl+C to stop."
echo ""

# Wait for Ctrl+C
trap "kill $SERVER_PID 2>/dev/null; exit 0" INT TERM
wait $SERVER_PID
