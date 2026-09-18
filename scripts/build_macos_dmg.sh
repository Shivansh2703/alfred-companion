#!/bin/bash
# Build dist/mac-app/Alfred-Mac.dmg from an already-built dist/mac-app/Alfred.app: a drag-to-
# Applications disk image (stage folder with Alfred.app + an "Applications" symlink to
# /Applications, then hdiutil create -format UDZO). stdlib/macOS tools only, no network.
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT=$(pwd)
APP="$ROOT/dist/mac-app/Alfred.app"
DMG="$ROOT/dist/mac-app/Alfred-Mac.dmg"
STAGE="$ROOT/.dmg-stage"

[ -d "$APP" ] || { echo "missing $APP — run scripts/build_macos_app.sh first" >&2; exit 1; }

rm -rf "$STAGE"
mkdir -p "$STAGE"
cp -R "$APP" "$STAGE/Alfred.app"
ln -s /Applications "$STAGE/Applications"

rm -f "$DMG"
hdiutil create -volname "Alfred" -srcfolder "$STAGE" -ov -format UDZO "$DMG"

rm -rf "$STAGE"

echo "sha256: $(shasum -a 256 "$DMG" | awk '{print $1}')"
echo "size: $(stat -f%z "$DMG" 2>/dev/null || stat -c%s "$DMG") bytes"
