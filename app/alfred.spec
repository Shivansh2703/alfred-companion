# Build in the isolated venv from the staged copy scripts/build_macos_app.sh makes:
#   pyinstaller --noconfirm --clean app/alfred.spec --distpath <repo>/dist/mac-app
# arm64-only (host Mac's own arch); universal2 needs a universal2 Python interpreter, which
# the build script does not assume is present.
from pathlib import Path

ROOT = Path(SPECPATH).parent
datas = [
    (str(ROOT / "daemon"), "daemon"),
    (str(ROOT / "data" / "rules.json"), "data"),
    (str(ROOT / "data" / "purpose_labels_vanilla.json"), "data"),
    (str(ROOT / "tools" / "vanilla_preflight.py"), "tools"),
    (str(ROOT / "vanilla.lock.json"), "."),
    (str(ROOT / "alfred.companion.json"), "."),
    (str(ROOT / ".cache" / "profile"), ".cache"),
    (str(ROOT / "app" / "starting.html"), "app"),
    (str(ROOT / "app" / "move_to_applications.html"), "app"),
]
a = Analysis([str(ROOT / "app" / "alfred_app.py")], datas=datas,
             pathex=[str(ROOT), str(ROOT / "daemon")], binaries=[],
             hiddenimports=["uuid", "paths", "aigen", "webui"])
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, exclude_binaries=True, name="Alfred", console=False)
app = BUNDLE(COLLECT(exe, a.binaries, a.datas, name="Alfred"),
             name="Alfred.app", icon=None, bundle_identifier="com.alfred.companion")
