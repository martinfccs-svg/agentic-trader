#!/bin/bash
# Run trader in background, API in foreground
python trader.py &
TRADER_PID=$!

# Give trader a moment to initialize
sleep 2

# Run API on port 5000
python api.py

# If API dies, kill trader too
kill $TRADER_PID 2>/dev/null || true

