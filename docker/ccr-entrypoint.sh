#!/bin/sh
set -e

# Run ccr start in foreground to capture output.
# If it exits 0 (daemon mode), wait for port with extended timeout.
# If it blocks (server mode), Docker handles lifecycle normally.

ccr start 2>&1 &
CCR_PID=$!

trap 'kill $CCR_PID 2>/dev/null; exit 0' TERM INT

# Wait for launcher to finish (daemon mode) or keep running (server mode)
sleep 5

if kill -0 $CCR_PID 2>/dev/null; then
    # Still running — foreground server mode
    echo "ccr running in foreground"
    wait $CCR_PID
    exit $?
fi

# Launcher exited — check if server bound the port (up to 15s)
echo "ccr launcher exited, waiting for server on :3456..."
i=0
while [ $i -lt 15 ]; do
    if nc -z 127.0.0.1 3456 2>/dev/null; then
        break
    fi
    sleep 1
    i=$((i + 1))
done

if ! nc -z 127.0.0.1 3456 2>/dev/null; then
    echo "ERROR: port 3456 not open after 20s"
    # Dump any ccr log files
    for f in /root/.claude-code-router/logs/*.log /tmp/ccr*.log; do
        [ -f "$f" ] && echo "=== $f ===" && cat "$f"
    done
    exit 1
fi

echo "ccr server running on :3456"
while nc -z 127.0.0.1 3456 2>/dev/null; do
    sleep 5
done
echo "ccr server stopped"
