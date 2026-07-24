#!/bin/bash

cd "$(dirname "$0")"
source .venv/bin/activate

mkdir -p logs

cleanup() {
    echo "Shutting down..."
    kill $PID_DJANGO $PID_LISTENER $PID_INGESTION 2>/dev/null
    wait
    exit 0
}

trap cleanup SIGINT SIGTERM

echo "Starting Daphne on port 80..."
daphne -b 0.0.0.0 -p 80 wis2_monitor.asgi:application > logs/daphne.log 2>&1 &
PID_DJANGO=$!
echo "Starting alert listener..."
python wis2_listener.py > logs/listener.log 2>&1 &
PID_LISTENER=$!

echo "Starting MQTT ingestion..."
python wis2_ingestion.py > logs/ingestion.log 2>&1 &
PID_INGESTION=$!

echo "All services started."
echo "  Daphne PID: $PID_DJANGO"
echo "  Listener PID: $PID_LISTENER"
echo "  Ingestion PID: $PID_INGESTION"
echo "Logs: logs/"

wait
