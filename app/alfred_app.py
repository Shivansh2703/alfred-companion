"""Alfred.app entry point (Box 3): setup check, per-user LaunchAgent install/stop, and the
--companion background mode. Every subprocess call goes through an injectable `runner`
(default subprocess.run) so tests never spawn real launchctl. Status and model/key entry
are a local web page served by the companion (Box 6, daemon/webui.py) instead of a native
dialog; plain_open() only opens a browser tab pointed at it.

Contract with Box 1 (daemon/paths.py): SetupError, check_setup(home=None, platform=None,
environ=None) -> (ok, message), and daemon/aigen.py's _run_companion() (A4: waits and
re-checks every 30s on SetupError, never exits).
"""
import os
import plistlib
import subprocess
import sys
import time
import webbrowser
from pathlib import Path

APP_ID = "com.alfred.companion"
PLIST_NAME = APP_ID + ".plist"


def resource_root():
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[1]))


def _load_daemon_modules():
    """Import daemon/paths.py and daemon/aigen.py from the bundled (or repo) root."""
    daemon_dir = str(resource_root() / "daemon")
    if daemon_dir not in sys.path:
        sys.path.insert(0, daemon_dir)
    import paths  # noqa: E402
    import aigen  # noqa: E402
    return paths, aigen


check_setup = _load_daemon_modules()[0].check_setup


def _load_webui():
    daemon_dir = str(resource_root() / "daemon")
    if daemon_dir not in sys.path:
        sys.path.insert(0, daemon_dir)
    import webui  # noqa: E402
    return webui


def app_ok(executable_path):
    """False for an App-Translocated or non-Applications launch (a LaunchAgent pointing at
    either breaks after reboot)."""
    if "/AppTranslocation/" in executable_path:
        return False
    p = str(Path(executable_path).resolve())
    home_apps = str(Path.home() / "Applications")
    return p.startswith("/Applications/") or p.startswith(home_apps + "/")


def data_dir():
    paths_mod, _ = _load_daemon_modules()
    return Path(paths_mod.data_dir(str(resource_root())))


def plist_path(home=None):
    home = Path(home) if home else Path.home()
    return home / "Library" / "LaunchAgents" / PLIST_NAME


def write_plist(executable, home=None):
    """A4: RunAtLoad only, no KeepAlive — a crashing companion should not be relaunched in a
    tight loop by launchd; the player restarts it by reopening Alfred.app."""
    path = plist_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    log_dir = data_dir() / "daemon"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = str(log_dir / "aigen.log")
    plist = {
        "Label": APP_ID,
        "ProgramArguments": [str(executable), "--companion"],
        "RunAtLoad": True,
        "StandardOutPath": log_path,
        "StandardErrorPath": log_path,
    }
    with open(path, "wb") as f:
        plistlib.dump(plist, f)
    return path


def install(executable, runner=subprocess.run, home=None):
    """Bootout any stale registration first (ignored — real launchctl has no reliable
    "already loaded" string, e.g. "Bootstrap failed: 5: Input/output error"), then
    bootstrap. A bootstrap failure is folded into the returned warning rather than raised —
    the LaunchAgent write already succeeded and the status dialog should say so, not crash
    the app silently."""
    plist = write_plist(executable, home=home)
    uid = os.getuid()
    runner(["launchctl", "bootout", f"gui/{uid}/{APP_ID}"], capture_output=True, text=True)
    result = runner(["launchctl", "bootstrap", f"gui/{uid}", str(plist)],
                     capture_output=True, text=True)
    warning = None
    if result.returncode != 0:
        text = ((result.stderr or "") + (result.stdout or "")).strip()
        warning = f"launchctl bootstrap failed: {text}"
    return plist, warning


def _unlink_webui_info():
    """Removes the stale port+token record so a dead companion can never look alive: a
    leftover webui.json from a previous run would otherwise point a browser tab at a port
    nothing is listening on, or worse, one a later unrelated process now owns."""
    webui = _load_webui()
    try:
        os.remove(webui.info_path(str(data_dir())))
    except FileNotFoundError:
        pass


def stop(runner=subprocess.run, home=None):
    """Bootout + delete the plist. Never touches credentials or chat history (owner data)."""
    uid = os.getuid()
    runner(["launchctl", "bootout", f"gui/{uid}/{APP_ID}"], capture_output=True, text=True)
    path = plist_path(home)
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    _unlink_webui_info()
    return path


def run_companion():
    _, aigen = _load_daemon_modules()
    aigen._run_companion(stop_fn=stop)


def _wait_for_webui(data_dir_path, tries=40, sleep=time.sleep):
    """Poll for the companion's webui.json for a bounded moment (~12s at the default 0.3s
    step) — it races the LaunchAgent's own startup (webui starts before the wait loop, so
    it's normally fast). ponytail: no fallback that starts a webui server inside THIS
    process — the companion always serves it; if it never shows up, plain_open() opens the
    bundled starting.html instead of leaving nothing open."""
    webui = _load_webui()
    for _ in range(tries):
        info = webui.read_info(data_dir_path)
        if info:
            return info
        sleep(0.3)
    return None


def plain_open(runner=subprocess.run, home=None, opener=webbrowser.open, sleep=time.sleep):
    """Double-click entry: check setup, refuse a translocated/non-Applications launch,
    otherwise install the LaunchAgent (which starts the companion, which serves the local
    setup/status page) and open that page in the default browser."""
    executable = sys.executable
    if not app_ok(executable):
        opener((resource_root() / "app" / "move_to_applications.html").as_uri())
        return
    _unlink_webui_info()  # a stale port+token from a previous run must never win the race below
    install(executable, runner=runner, home=home)
    webui = _load_webui()
    info = _wait_for_webui(str(data_dir()), sleep=sleep)
    if info:
        opener(webui.url_for(info["port"]))
    else:
        opener((resource_root() / "app" / "starting.html").as_uri())


def smoke():
    paths_mod, aigen = _load_daemon_modules()
    print(f"Alfred smoke: paths={paths_mod.__file__} aigen={aigen.__file__}")
    print("Alfred bundle smoke: PASS")


def main(argv=None):
    argv = sys.argv if argv is None else argv
    if "--smoke" in argv:
        smoke()
        return
    if "--companion" in argv:
        run_companion()
        return
    plain_open()


if __name__ == "__main__":
    main()
