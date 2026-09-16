#!/bin/bash
# Mac companion entry: Vanilla lock, then the same daemon start/stop/status/log.
# Start this BEFORE the game so exchange files exist at boot (Feral negative-lookup).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export ALFRED_DATA_DIR="${ALFRED_DATA_DIR:-$HOME/Library/Application Support/Alfred}"
mkdir -p "$ALFRED_DATA_DIR"
python3 "$ROOT/tools/vanilla_preflight.py" --root "$ROOT" --lock "$ROOT/vanilla.lock.json"
exec "$ROOT/scripts/daemon.sh" "${1:-status}"
