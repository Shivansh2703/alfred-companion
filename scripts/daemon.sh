#!/bin/bash
# Run the aigen daemon detached so sweeping (asked of Alfred in chat as of Box I,
# 2026-09-01 — the MCT "Run sweep now" button is gone) keeps working while the owner
# plays. The 08-28 nohup daemon silently died with a reboot; this gives start/stop/status.
# Usage: scripts/daemon.sh start|stop|status|log
set -euo pipefail
cd "$(dirname "$0")/.."
RUN="${ALFRED_DATA_DIR:-.cache}/daemon"; PID=$RUN/aigen.pid; LOG=$RUN/aigen.log
mkdir -p "$RUN"

alive() { [ -f "$PID" ] && kill -0 "$(cat "$PID")" 2>/dev/null; }

case "${1:-status}" in
  start)
    if alive; then echo "already running (pid $(cat "$PID"))"; exit 0; fi
    # </dev/null: stdin EOF = file-watch only (aigen.py handles it); -u: unbuffered log
    nohup python3 -u daemon/aigen.py </dev/null >>"$LOG" 2>&1 &
    echo $! >"$PID"; sleep 2
    if alive; then echo "started pid $(cat "$PID"), log $LOG"; tail -3 "$LOG"
    else echo "FAILED to start:"; tail -5 "$LOG"; exit 1; fi ;;
  stop)
    if alive; then kill "$(cat "$PID")" && rm -f "$PID" && echo stopped
    else rm -f "$PID"; echo "not running"; fi ;;
  status)
    if alive; then echo "running pid $(cat "$PID")"; else echo "not running"; exit 1; fi ;;
  log) tail -f "$LOG" ;;
  *) echo "usage: $0 start|stop|status|log"; exit 2 ;;
esac
