#!/bin/bash
# Starts the CC scanner in the background if it's not already running
cd /home/user/Stock
if ! pgrep -f "python3 main.py" > /dev/null; then
    nohup python3 main.py > /dev/null 2>&1 &
    echo "CC Scanner started (PID $!)"
else
    echo "CC Scanner already running"
fi
