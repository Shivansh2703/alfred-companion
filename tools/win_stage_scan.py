"""Shared credential/key-shape refusal scan for a staged package directory.

Same rule as tools/build_companion_zip.sh and scripts/build_macos_app.sh: no
alfred-credentials.json or alfred.local.json file anywhere in the stage, and no
Google-key-shaped string in any text file. One copy so the Windows build script and
its test can't drift apart (ponytail: reuse over reinventing the same regex twice).
"""
import re
import sys
from pathlib import Path

BANNED_NAMES = ("alfred-credentials.json", "alfred.local.json")
KEY_SHAPE = re.compile(r"AIzaSy[0-9A-Za-z_-]{20,}")
TEXT_SUFFIXES = {".json", ".md", ".py", ".sh", ".pth", ".vbs", ".txt"}


class StageRefused(ValueError):
    """One plain sentence naming what was found and where."""


def scan(root):
    """Raise StageRefused on a credential file or key-shaped string under `root`.
    Returns None (no value) when the stage is clean."""
    root = Path(root)
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        if p.name in BANNED_NAMES:
            raise StageRefused(f"stage contains {p.name} at {p}")
        if p.suffix in TEXT_SUFFIXES:
            text = p.read_text(errors="ignore")
            if KEY_SHAPE.search(text):
                raise StageRefused(f"Google-key-shaped string in {p}")


if __name__ == "__main__":
    try:
        scan(sys.argv[1])
    except StageRefused as e:
        print(f"REFUSING: {e}", file=sys.stderr)
        sys.exit(1)
    print("stage secret scan: no credential files, no Google-key-shaped strings")
