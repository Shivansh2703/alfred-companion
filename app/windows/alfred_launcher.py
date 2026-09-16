"""Windows companion launcher (Box 4). Runs under the embeddable Python that ships in the
package, next to daemon/, data/, etc. Double-clicked via Alfred.vbs (hidden, no console).

Job: make sure the companion starts at login (a Startup-folder shortcut, the simplest
autostart primitive on Windows — no registry, no Task Scheduler), start the companion now if
it is not already running, wait for its local web page to come up, and open that page in the
default browser. `Stop Alfred.vbs` -> `--stop`: removes the Startup entry and kills the
companion process, and never touches credentials (it only ever reads a pid file and kills
the process it names).

All paths come from daemon/paths.py's Windows branches (APPDATA, no drive letter or
username baked in). Every side effect goes through an injectable parameter so tests never
touch the real Startup folder, spawn a real process, or open a real browser.
"""
import json
import os
import subprocess
import sys
import time

STARTUP_REL = os.path.join(
    "Microsoft", "Windows", "Start Menu", "Programs", "Startup")
VBS_NAME = "Alfred.vbs"


def package_dir():
    """The folder this file lives in — the root of the staged Windows package."""
    return os.path.dirname(os.path.abspath(__file__))


def _appdata(environ=None):
    env = os.environ if environ is None else environ
    return env.get("APPDATA") or ""


def startup_dir(environ=None):
    return os.path.join(_appdata(environ), STARTUP_REL)


def startup_vbs_path(environ=None):
    return os.path.join(startup_dir(environ), VBS_NAME)


def data_dir(environ=None):
    """Same shape as daemon/paths.py's companion data_dir(): %APPDATA%\\Alfred."""
    return os.path.join(_appdata(environ), "Alfred")


def pid_path(environ=None):
    # Matches scripts/daemon.sh's $ALFRED_DATA_DIR/daemon/aigen.pid.
    return os.path.join(data_dir(environ), "daemon", "aigen.pid")


def webui_path(environ=None):
    return os.path.join(data_dir(environ), "webui.json")


def _startup_vbs_content(pkg_dir):
    """Absolute paths baked in — this file lives in the Startup folder, not next to
    pythonw.exe, so it cannot find its target via GetParentFolderName like the shipped
    Alfred.vbs does."""
    pythonw = os.path.join(pkg_dir, "pythonw.exe")
    launcher = os.path.join(pkg_dir, "alfred_launcher.py")
    return (
        'Set shell = CreateObject("WScript.Shell")\n'
        f'shell.Run """{pythonw}"" ""{launcher}""", 0, False\n'
    )


def ensure_startup_entry(pkg_dir=None, environ=None):
    """Write a Startup-folder vbs pointing at this package, so it runs at login.
    Re-writing every run is idempotent and cheap; no need to detect drift first."""
    pkg_dir = pkg_dir or package_dir()
    dest_dir = startup_dir(environ)
    os.makedirs(dest_dir, exist_ok=True)
    dest = startup_vbs_path(environ)
    with open(dest, "w", encoding="utf-8") as f:
        f.write(_startup_vbs_content(pkg_dir))
    return dest


def remove_startup_entry(environ=None):
    path = startup_vbs_path(environ)
    try:
        os.remove(path)
    except FileNotFoundError:
        pass
    return path


def _tasklist_has_pid(pid, runner=subprocess.run):
    """CSV row shape: "pythonw.exe","10876","Console","1","12,345 K". Match the PID
    field exactly — a substring check (e.g. "876" in "...10876...") would false-positive."""
    import csv
    import io
    try:
        result = runner(
            ["tasklist", "/FI", f"PID eq {pid}", "/FI", "IMAGENAME eq pythonw.exe",
             "/NH", "/FO", "CSV"],
            capture_output=True, text=True)
    except OSError:
        return False
    for row in csv.reader(io.StringIO(result.stdout or "")):
        if len(row) >= 2 and row[1] == str(pid):
            return True
    return False


def companion_running(environ=None, alive_check=_tasklist_has_pid):
    try:
        with open(pid_path(environ)) as f:
            pid = int(f.read().strip())
    except (OSError, ValueError):
        return False
    return alive_check(pid)


def start_companion(pkg_dir=None, environ=None, popen=subprocess.Popen):
    pkg_dir = pkg_dir or package_dir()
    pythonw = os.path.join(pkg_dir, "pythonw.exe")
    daemon_script = os.path.join(pkg_dir, "daemon", "aigen.py")
    proc = popen([pythonw, daemon_script, "--companion"], cwd=pkg_dir)
    pid_file = pid_path(environ)
    os.makedirs(os.path.dirname(pid_file), exist_ok=True)
    with open(pid_file, "w", encoding="utf-8") as f:
        f.write(str(proc.pid))


def stop_companion(environ=None, runner=subprocess.run):
    """Never touches alfred-credentials.json / alfred.local.json — only the pid file
    and the process it names."""
    path = pid_path(environ)
    try:
        with open(path) as f:
            pid = int(f.read().strip())
    except (OSError, ValueError):
        return False
    try:
        runner(["taskkill", "/PID", str(pid), "/F"], capture_output=True, text=True)
    except OSError:
        pass
    try:
        os.remove(path)
    except FileNotFoundError:
        pass
    return True


def wait_for_webui(environ=None, timeout=60, sleep=time.sleep, clock=time.monotonic):
    """Poll <data_dir>/webui.json (Box 6's contract) for up to `timeout` seconds.
    Returns {"port": int, "token": str} or None if it never appeared."""
    path = webui_path(environ)
    deadline = clock() + timeout
    while True:
        try:
            with open(path) as f:
                data = json.load(f)
        except (OSError, ValueError):
            data = None
        if isinstance(data, dict) and "port" in data and "token" in data:
            return data
        if clock() >= deadline:
            return None
        sleep(1)


def open_url(webui, opener=None):
    """No-op if webui is falsy (the companion never came up — open nothing, exit quietly).
    No token in the URL: the page itself reads webui.json for the token (Box 6's contract),
    so nothing sensitive needs to ride in a URL that Windows logs (shell history, browser
    history, Run dialog MRU)."""
    if not webui:
        return
    url = f"http://127.0.0.1:{webui['port']}/"
    opener = opener or getattr(os, "startfile", None)
    if opener is not None:
        opener(url)


def _log_timeout(environ=None):
    """One plain line for whoever opens launcher.log after Alfred.vbs's MsgBox fires."""
    log_path = os.path.join(data_dir(environ), "launcher.log")
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    with open(log_path, "a", encoding="utf-8") as f:
        f.write("Alfred's companion did not come up within 60 seconds. "
                 "Check %APPDATA%\\Alfred\\daemon\\aigen.log and try again.\n")


def run(environ=None, popen=subprocess.Popen, sleep=time.sleep, opener=None,
        alive_check=_tasklist_has_pid):
    """Returns a process exit code: 0 on success, 2 if the companion's page never came up
    (Alfred.vbs shows a MsgBox on a non-zero exit)."""
    ensure_startup_entry(environ=environ)
    if not companion_running(environ=environ, alive_check=alive_check):
        start_companion(environ=environ, popen=popen)
    webui = wait_for_webui(environ=environ, sleep=sleep)
    if webui is None:
        _log_timeout(environ=environ)
        return 2
    open_url(webui, opener=opener)
    return 0


def stop(environ=None, runner=subprocess.run):
    remove_startup_entry(environ=environ)
    stop_companion(environ=environ, runner=runner)


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if "--stop" in argv:
        stop()
        return 0
    return run()


if __name__ == "__main__":
    sys.exit(main())
