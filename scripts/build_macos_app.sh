#!/bin/bash
# Build dist/mac-app/Alfred.app from repo sources only — never from a scratch-tmp candidate.
# Stages a copy inside this worktree, refuses to bundle a credential file or a
# Google-key-shaped string (same pattern as tools/build_companion_zip.sh), then runs the
# bundle's --smoke.
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT=$(pwd)
VENV="$ROOT/.build-venv"
STAGE="$ROOT/.app-stage"

for f in data/rules.json data/purpose_labels_vanilla.json tools/vanilla_preflight.py vanilla.lock.json; do
  [ -f "$f" ] || { echo "missing $f" >&2; exit 1; }
done

rm -rf "$STAGE"
mkdir -p "$STAGE/app" "$STAGE/daemon" "$STAGE/data" "$STAGE/tools" "$STAGE/.cache"
cp app/alfred_app.py app/alfred.spec app/starting.html "$STAGE/app/"
cp daemon/aigen.py daemon/paths.py daemon/webui.py "$STAGE/daemon/"
cp data/rules.json data/purpose_labels_vanilla.json "$STAGE/data/"
cp tools/vanilla_preflight.py "$STAGE/tools/"
cp vanilla.lock.json "$STAGE/vanilla.lock.json"
printf '%s' 'vanilla' >"$STAGE/.cache/profile"
cat >"$STAGE/alfred.companion.json" <<'JSON'
{"schema_version":1,"preset":"vanilla","platform":"mac"}
JSON

python3 - "$STAGE" <<'PY'
import re, sys
from pathlib import Path
root = Path(sys.argv[1])
banned = ("alfred-credentials.json", "alfred.local.json")
for p in root.rglob("*"):
    if p.name in banned:
        print(f"REFUSING: app stage contains {p.name}", file=sys.stderr)
        sys.exit(1)
    if p.is_file() and p.suffix in {".json", ".md", ".py", ".sh"}:
        text = p.read_text(errors="ignore")
        if re.search(r"AIzaSy[0-9A-Za-z_-]{20,}", text):
            print(f"REFUSING: Google-key-shaped string in {p}", file=sys.stderr)
            sys.exit(1)
print("app stage secret scan: no credential files, no Google-key-shaped strings")
PY

python3 -m venv "$VENV"
"$VENV/bin/pip" install --upgrade pyinstaller

(
  cd "$STAGE"
  "$VENV/bin/pyinstaller" --noconfirm --clean app/alfred.spec \
    --distpath "$ROOT/dist/mac-app" --workpath "$STAGE/build"
)

"$ROOT/dist/mac-app/Alfred.app/Contents/MacOS/Alfred" --smoke
