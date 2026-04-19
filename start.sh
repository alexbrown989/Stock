#!/bin/bash
# Starts both the Covered Call Scanner and Theta Harvest daemon in the background.
# Both processes run every 4 hours automatically.
# Logs are written to logs/scanner.log and logs/theta_harvest.log

set -e
cd "$(dirname "$0")"

mkdir -p logs

# ── Covered Call Scanner (main.py) ─────────────────────────────────────────────
if pgrep -f "python3 main.py" > /dev/null 2>&1; then
    echo "CC Scanner already running (PID $(pgrep -f 'python3 main.py'))"
else
    nohup python3 main.py >> logs/scanner.log 2>&1 &
    echo "CC Scanner started (PID $!)"
fi

# ── Theta Harvest Daemon (pipeline.py daemon) ──────────────────────────────────
if pgrep -f "pipeline.py daemon" > /dev/null 2>&1; then
    echo "Theta Harvest daemon already running (PID $(pgrep -f 'pipeline.py daemon'))"
else
    nohup python3 pipeline.py daemon --interval 4 >> logs/theta_harvest.log 2>&1 &
    echo "Theta Harvest daemon started (PID $!)"
fi

echo ""
echo "Both processes are running. Tail logs with:"
echo "  tail -f logs/scanner.log"
echo "  tail -f logs/theta_harvest.log"
