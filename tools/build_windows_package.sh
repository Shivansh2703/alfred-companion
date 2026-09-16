#!/bin/bash
# Build dist/windows/Alfred-Windows.zip on a Mac: no Windows build machine, so this stages
# python.org's embeddable Windows Python next to the daemon and the vbs launcher (Box 4,
# functionally the same for the player as a real .exe until GitHub Actions exists — see
# DECISIONS.md 2026-09-16 16:32).
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT=$(pwd)

PY_VERSION=3.12.7
PY_ZIP="python-${PY_VERSION}-embed-amd64.zip"
PY_URL="https://www.python.org/ftp/python/${PY_VERSION}/${PY_ZIP}"
# Published in python.org's own .spdx.json for this exact file (python.org/ftp/python/
# 3.12.7/python-3.12.7-embed-amd64.zip.spdx.json), cross-checked against a fresh download.
PY_SHA256="0d57bb6cb078b74d23dbfe91f77d6780d45bed328911609f1f7ee2ba1606bf44"

CACHE="$ROOT/.build-cache"
STAGE="$ROOT/.win-stage/Alfred"

for f in data/rules.json data/purpose_labels_vanilla.json tools/vanilla_preflight.py vanilla.lock.json; do
  [ -f "$f" ] || { echo "missing $f" >&2; exit 1; }
done

mkdir -p "$CACHE"
downloaded_fresh=0
if [ ! -f "$CACHE/$PY_ZIP" ]; then
  echo "downloading $PY_URL"
  curl -fsSL -o "$CACHE/$PY_ZIP.part" "$PY_URL"
  mv "$CACHE/$PY_ZIP.part" "$CACHE/$PY_ZIP"
  downloaded_fresh=1
fi

got_sha=$(shasum -a 256 "$CACHE/$PY_ZIP" | awk '{print $1}')
if [ "$got_sha" != "$PY_SHA256" ]; then
  # Only delete a bad zip we just downloaded ourselves — an existing cache entry may be
  # bad for a reason worth investigating (a stale PY_SHA256 in this script, e.g.), and
  # silently deleting someone else's cached file on every run papers over that.
  if [ "$downloaded_fresh" = 1 ]; then
    rm -f "$CACHE/$PY_ZIP"
  fi
  echo "REFUSING: $PY_ZIP sha256 mismatch (got $got_sha, want $PY_SHA256)" >&2
  exit 1
fi
echo "python sha256 verified: $got_sha"

rm -rf "$ROOT/.win-stage" "$ROOT/dist/windows"
mkdir -p "$STAGE/daemon" "$STAGE/data" "$STAGE/tools"
unzip -q "$CACHE/$PY_ZIP" -d "$STAGE"

# Glob, not a named list: whatever daemon/*.py ships (e.g. Box 6's webui.py) rides along
# automatically, and the stdlib-only grep below scans the exact same glob so a new module
# can't skip that check by being added after this line was last edited.
cp daemon/*.py "$STAGE/daemon/"
rm -f "$STAGE/daemon/test_aigen.py"
cp data/rules.json data/purpose_labels_vanilla.json "$STAGE/data/"
cp tools/vanilla_preflight.py "$STAGE/tools/"
cp "$ROOT/vanilla.lock.json" "$STAGE/vanilla.lock.json"
mkdir -p "$STAGE/.cache"
printf '%s' 'vanilla' >"$STAGE/.cache/profile"
cat >"$STAGE/alfred.companion.json" <<'JSON'
{"schema_version":1,"preset":"vanilla","platform":"windows"}
JSON
cp app/windows/alfred_launcher.py app/windows/Alfred.vbs "$STAGE/"
cp "app/windows/Stop Alfred.vbs" "$STAGE/"

# The embeddable distribution ships python312._pth with "." on its own line (package root
# on sys.path) already — that's what lets `import daemon.aigen` / `alfred_launcher` resolve
# with no pip and no venv. Fail loudly if a future embeddable zip drops that line, since
# nothing else would import.
PTH="$STAGE/python312._pth"
if [ ! -f "$PTH" ] || ! tr -d '\r' <"$PTH" | grep -qx '\.'; then
  echo "REFUSING: $PTH missing a bare '.' entry — daemon/alfred_launcher would not import" >&2
  exit 1
fi

# Confirm stdlib-only before shipping a package with no pip to fall back on — same glob as
# the daemon/*.py copy above, minus the one file that copy also excludes, so a module can't
# ship without passing through this check.
shipped_daemon_py=$(ls "$STAGE"/daemon/*.py)
if grep -rEn '^\s*(import|from)\s+(tkinter|pip|numpy|requests)\b' $shipped_daemon_py; then
  echo "REFUSING: daemon imports a non-stdlib module the embeddable Python cannot provide" >&2
  exit 1
fi

python3 "$ROOT/tools/win_stage_scan.py" "$STAGE"
python3 tools/vanilla_preflight.py --root "$STAGE" --lock "$STAGE/vanilla.lock.json"

mkdir -p "$ROOT/dist/windows"
( cd "$ROOT/.win-stage" && zip -qr "$ROOT/dist/windows/Alfred-Windows.zip" Alfred )

python3 - "$PY_VERSION" <<'PY'
import hashlib, json, sys
from pathlib import Path
py_version = sys.argv[1]
z = Path("dist/windows/Alfred-Windows.zip")
manifest = {
    "zip": z.name,
    "sha256": hashlib.sha256(z.read_bytes()).hexdigest(),
    "bytes": z.stat().st_size,
    "python_version": py_version,
    "preset": "vanilla",
    "platform": "windows",
}
Path("dist/windows/MANIFEST.json").write_text(json.dumps(manifest, indent=2) + "\n")
print(f"windows zip sha256 {manifest['sha256']} {manifest['bytes']} bytes")
PY
ls -la "$ROOT/dist/windows/Alfred-Windows.zip" "$ROOT/dist/windows/MANIFEST.json"
