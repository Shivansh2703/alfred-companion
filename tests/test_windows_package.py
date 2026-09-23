#!/usr/bin/env python3
"""Windows companion package tests (Box 4). Offline, no network, no real Windows.
Injects platform/environ/home/runner into app/windows/alfred_launcher.py and checks the
staged-package layout and secret scan. Uses fake %APPDATA% dirs under tempfile, never the
real Startup folder or a real process.
"""
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


launcher = _load("alfred_launcher", ROOT / "app" / "windows" / "alfred_launcher.py")
sys.path.insert(0, str(ROOT / "tools"))
import win_stage_scan  # noqa: E402
sys.path.insert(0, str(ROOT / "daemon"))
import aigen  # noqa: E402


class StartupPath(unittest.TestCase):
    def test_startup_dir_under_fake_appdata(self):
        environ = {"APPDATA": r"C:\Users\Fake\AppData\Roaming"}
        self.assertEqual(
            launcher.startup_dir(environ),
            launcher.os.path.join(environ["APPDATA"], "Microsoft", "Windows",
                                   "Start Menu", "Programs", "Startup"))
        self.assertEqual(
            launcher.startup_vbs_path(environ),
            launcher.os.path.join(launcher.startup_dir(environ), "Alfred.vbs"))

    def test_data_dir_and_pid_path(self):
        environ = {"APPDATA": r"C:\Users\Fake\AppData\Roaming"}
        self.assertTrue(launcher.data_dir(environ).endswith(
            launcher.os.path.join("Roaming", "Alfred")))
        self.assertTrue(launcher.pid_path(environ).endswith(
            launcher.os.path.join("Alfred", "daemon", "aigen.pid")))
        self.assertTrue(launcher.webui_path(environ).endswith(
            launcher.os.path.join("Alfred", "webui.json")))


class EnsureAndRemoveStartupEntry(unittest.TestCase):
    def test_ensure_writes_absolute_path_vbs(self):
        with tempfile.TemporaryDirectory() as appdata, tempfile.TemporaryDirectory() as pkg:
            environ = {"APPDATA": appdata}
            dest = launcher.ensure_startup_entry(pkg_dir=pkg, environ=environ)
            self.assertTrue(Path(dest).is_file())
            content = Path(dest).read_text()
            self.assertIn(pkg, content)
            self.assertIn("pythonw.exe", content)
            self.assertIn("alfred_launcher.py", content)

    def test_stop_removes_startup_entry_and_keeps_credentials(self):
        with tempfile.TemporaryDirectory() as appdata, tempfile.TemporaryDirectory() as pkg:
            environ = {"APPDATA": appdata}
            launcher.ensure_startup_entry(pkg_dir=pkg, environ=environ)
            vbs = launcher.startup_vbs_path(environ)
            self.assertTrue(Path(vbs).is_file())

            # A fake credential file living where paths.py's companion data_dir() would put
            # it (%APPDATA%\Alfred) — --stop must never touch it.
            data_dir = Path(launcher.data_dir(environ))
            data_dir.mkdir(parents=True, exist_ok=True)
            cred = data_dir / "alfred-credentials.json"
            cred.write_text('{"api_key": "fake"}')

            calls = []
            launcher.stop(environ=environ, runner=lambda *a, **k: calls.append(a))

            self.assertFalse(Path(vbs).exists())
            self.assertTrue(cred.is_file())
            self.assertEqual(cred.read_text(), '{"api_key": "fake"}')

    def test_stop_with_no_startup_entry_is_a_noop(self):
        with tempfile.TemporaryDirectory() as appdata:
            environ = {"APPDATA": appdata}
            # Must not raise even though nothing was ever created.
            launcher.stop(environ=environ, runner=lambda *a, **k: None)


class PidFileAndSingleInstance(unittest.TestCase):
    class _FakePopen:
        def __init__(self, pid):
            self.pid = pid

    def test_start_writes_pid_file(self):
        with tempfile.TemporaryDirectory() as appdata, tempfile.TemporaryDirectory() as pkg:
            environ = {"APPDATA": appdata}
            launcher.start_companion(
                pkg_dir=pkg, environ=environ,
                popen=lambda *a, **k: self._FakePopen(9999))
            self.assertEqual(Path(launcher.pid_path(environ)).read_text().strip(), "9999")

    def test_second_run_does_not_start_second_companion_while_page_is_live(self):
        with tempfile.TemporaryDirectory() as appdata, tempfile.TemporaryDirectory() as pkg:
            environ = {"APPDATA": appdata}
            launcher.start_companion(
                pkg_dir=pkg, environ=environ,
                popen=lambda *a, **k: self._FakePopen(1234))

            starts = []
            with mock.patch.object(launcher, "wait_for_webui",
                                   return_value={"port": 4321, "token": "abc"}):
                launcher.run(
                    environ=environ,
                    popen=lambda *a, **k: starts.append(1) or self._FakePopen(1),
                    sleep=lambda s: None, opener=lambda url: None,
                    alive_check=lambda pid, runner=None: pid == 1234)
            self.assertEqual(starts, [])

    def test_alive_process_with_stale_server_record_is_restarted(self):
        with tempfile.TemporaryDirectory() as appdata:
            environ = {"APPDATA": appdata}
            pid = Path(launcher.pid_path(environ))
            pid.parent.mkdir(parents=True)
            pid.write_text("1234")
            starts = []
            with mock.patch.object(launcher, "wait_for_webui", side_effect=[
                    None, {"port": 4321, "token": "fresh"}]), \
                 mock.patch.object(launcher, "stop_companion", return_value=True) as stop:
                rc = launcher.run(
                    environ=environ,
                    popen=lambda *a, **k: starts.append(1) or self._FakePopen(2),
                    sleep=lambda s: None, opener=lambda url: None,
                    alive_check=lambda pid, runner=None: True)
            self.assertEqual(rc, 0)
            stop.assert_called_once_with(environ=environ)
            self.assertEqual(starts, [1])

    def test_stop_removes_pid_file(self):
        with tempfile.TemporaryDirectory() as appdata, tempfile.TemporaryDirectory() as pkg:
            environ = {"APPDATA": appdata}
            launcher.start_companion(
                pkg_dir=pkg, environ=environ,
                popen=lambda *a, **k: self._FakePopen(4242))
            self.assertTrue(Path(launcher.pid_path(environ)).is_file())
            launcher.stop(environ=environ, runner=lambda *a, **k: None)
            self.assertFalse(Path(launcher.pid_path(environ)).exists())


class StartCompanionDetached(unittest.TestCase):
    """Box 9: without DETACHED_PROCESS the companion dies when the (synchronous)
    Alfred.vbs launcher exits — MEASURED on windows-latest CI run 35157108916."""

    class _FakePopen:
        def __init__(self, pid):
            self.pid = pid

    def _start_and_capture_kwargs(self):
        calls = []

        def fake_popen(*a, **k):
            calls.append(k)
            return self._FakePopen(1)

        with tempfile.TemporaryDirectory() as appdata, tempfile.TemporaryDirectory() as pkg:
            launcher.start_companion(
                pkg_dir=pkg, environ={"APPDATA": appdata}, popen=fake_popen)
        return calls[0]

    def test_win32_passes_detached_flags(self):
        # DETACHED_PROCESS/CREATE_NEW_PROCESS_GROUP only exist on the real Windows
        # subprocess module; fake them here since this test runs on any platform.
        with mock.patch.object(launcher.subprocess, "DETACHED_PROCESS", 0x00000008,
                                create=True), \
             mock.patch.object(launcher.subprocess, "CREATE_NEW_PROCESS_GROUP",
                                0x00000200, create=True):
            kwargs = self._start_and_capture_kwargs()
        self.assertEqual(kwargs["creationflags"], 0x00000008 | 0x00000200)
        self.assertTrue(kwargs["close_fds"])
        self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)

    @unittest.skipIf(sys.platform == "win32", "host subprocess has the Windows constants")
    def test_darwin_passes_no_creationflags(self):
        # On a platform with neither constant (real Mac/Linux), the guarded getattr
        # falls back to 0 rather than raising AttributeError. Neither constant exists
        # in this process's subprocess module already (this test IS the Mac case),
        # so no patching is needed — just confirm the fallback value.
        kwargs = self._start_and_capture_kwargs()
        self.assertEqual(kwargs["creationflags"], 0)


class TasklistExactPidMatch(unittest.TestCase):
    def test_exact_row_match(self):
        class _Result:
            stdout = '"pythonw.exe","1234","Console","1","12,345 K"\n'
        self.assertTrue(launcher._tasklist_has_pid(1234, runner=lambda *a, **k: _Result()))

    def test_substring_pid_does_not_match(self):
        # "876" must not match a row whose real PID is "10876".
        class _Result:
            stdout = '"pythonw.exe","10876","Console","1","12,345 K"\n'
        self.assertFalse(launcher._tasklist_has_pid(876, runner=lambda *a, **k: _Result()))

    def test_no_rows_means_false(self):
        class _Result:
            stdout = ""
        self.assertFalse(launcher._tasklist_has_pid(1234, runner=lambda *a, **k: _Result()))


class WebUiTimeout(unittest.TestCase):
    def test_wait_for_webui_default_timeout_is_60s(self):
        with tempfile.TemporaryDirectory() as appdata:
            environ = {"APPDATA": appdata}
            ticks = iter([0, 61])
            webui = launcher.wait_for_webui(
                environ=environ, sleep=lambda s: None, clock=lambda: next(ticks, 61))
            self.assertIsNone(webui)

    def test_run_returns_2_and_logs_on_timeout(self):
        with tempfile.TemporaryDirectory() as appdata, tempfile.TemporaryDirectory() as pkg:
            environ = {"APPDATA": appdata}
            with mock.patch.object(launcher, "wait_for_webui", return_value=None):
                rc = launcher.run(
                    environ=environ,
                    popen=lambda *a, **k: PidFileAndSingleInstance._FakePopen(1),
                    sleep=lambda s: None, opener=lambda url: None,
                    alive_check=lambda pid, runner=None: False)
            self.assertEqual(rc, 2)
            log_path = Path(launcher.data_dir(environ)) / "launcher.log"
            self.assertTrue(log_path.is_file())
            self.assertIn("60 seconds", log_path.read_text())


class WebUiPolling(unittest.TestCase):
    def test_open_url_uses_token_from_fake_webui(self):
        with tempfile.TemporaryDirectory() as appdata:
            environ = {"APPDATA": appdata}
            data_dir = Path(launcher.data_dir(environ))
            data_dir.mkdir(parents=True, exist_ok=True)
            (data_dir / "webui.json").write_text(json.dumps({"port": 4321, "token": "abc123"}))

            webui = launcher.wait_for_webui(
                environ=environ, timeout=1, sleep=lambda s: None, probe=lambda data: True)
            self.assertEqual(webui, {"port": 4321, "token": "abc123"})

            opened = []
            launcher.open_url(webui, opener=opened.append)
            self.assertEqual(opened, ["http://127.0.0.1:4321/"])

    def test_missing_webui_json_means_no_open_call(self):
        with tempfile.TemporaryDirectory() as appdata:
            environ = {"APPDATA": appdata}
            ticks = iter([0, 25])  # clock jumps straight past the timeout: no real wait
            webui = launcher.wait_for_webui(
                environ=environ, timeout=20, sleep=lambda s: None,
                clock=lambda: next(ticks, 25))
            self.assertIsNone(webui)

            opened = []
            launcher.open_url(webui, opener=opened.append)
            self.assertEqual(opened, [])


class StagingScan(unittest.TestCase):
    def test_clean_stage_passes(self):
        with tempfile.TemporaryDirectory() as stage:
            (Path(stage) / "daemon").mkdir()
            (Path(stage) / "daemon" / "aigen.py").write_text("# ok\n")
            win_stage_scan.scan(stage)  # must not raise

    def test_credential_file_refused(self):
        with tempfile.TemporaryDirectory() as stage:
            (Path(stage) / "alfred-credentials.json").write_text("{}")
            with self.assertRaises(win_stage_scan.StageRefused):
                win_stage_scan.scan(stage)

    def test_local_json_refused(self):
        with tempfile.TemporaryDirectory() as stage:
            (Path(stage) / "alfred.local.json").write_text("{}")
            with self.assertRaises(win_stage_scan.StageRefused):
                win_stage_scan.scan(stage)

    def test_key_shaped_string_refused(self):
        # Built from parts, not a contiguous literal: a real-looking key must never sit in
        # this repo's own git history, including a public companion export of this file.
        fake_key = "AIza" + "Sy" + "ABCDEFGHIJKLMNOPQRSTUVWXYZ01234"
        with tempfile.TemporaryDirectory() as stage:
            (Path(stage) / "notes.md").write_text("key: " + fake_key)
            with self.assertRaises(win_stage_scan.StageRefused):
                win_stage_scan.scan(stage)


class BuildScriptGlobCopy(unittest.TestCase):
    """The build script must copy daemon/*.py by glob (not a named list), and its
    stdlib-only grep must scan that same glob — otherwise a future daemon module (Box 6's
    webui.py) ships without ever passing the stdlib check. Exercises the real build
    script end to end, in a throwaway source tree, reusing the cached embeddable-Python
    zip and heavy data files via symlink so this costs no network and little disk."""

    def test_planted_extra_module_is_staged(self):
        cache = ROOT / ".build-cache"
        if not any(cache.glob("python-3.12*-embed-amd64.zip")) if cache.is_dir() else True:
            self.skipTest("no cached embeddable Python zip; build script downloads it")

        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "src"
            for rel_dir in ("daemon", "data", "tools", "app/windows"):
                (src / rel_dir).mkdir(parents=True)
            for name in ("aigen.py", "paths.py"):
                (src / "daemon" / name).write_text(
                    (ROOT / "daemon" / name).read_text())
            # Plant the fake module the bounce item describes.
            (src / "daemon" / "extra_module.py").write_text("# fake box6 module\n")
            (src / "data" / "rules.json").symlink_to(ROOT / "data" / "rules.json")
            (src / "data" / "purpose_labels_vanilla.json").symlink_to(
                ROOT / "data" / "purpose_labels_vanilla.json")
            (src / "tools" / "vanilla_preflight.py").write_text(
                (ROOT / "tools" / "vanilla_preflight.py").read_text())
            (src / "tools" / "win_stage_scan.py").write_text(
                (ROOT / "tools" / "win_stage_scan.py").read_text())
            (src / "vanilla.lock.json").write_text(
                (ROOT / "vanilla.lock.json").read_text())
            for name in ("alfred_launcher.py", "Alfred.vbs"):
                (src / "app" / "windows" / name).write_text(
                    (ROOT / "app" / "windows" / name).read_text())
            (src / "app" / "windows" / "Stop Alfred.vbs").write_text(
                (ROOT / "app" / "windows" / "Stop Alfred.vbs").read_text())
            (src / "tools" / "build_windows_package.sh").write_text(
                (ROOT / "tools" / "build_windows_package.sh").read_text())
            (src / ".build-cache").symlink_to(cache)

            import subprocess
            import zipfile
            result = subprocess.run(
                ["bash", str(src / "tools" / "build_windows_package.sh")],
                cwd=src, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            with zipfile.ZipFile(src / "dist" / "windows" / "Alfred-Windows.zip") as z:
                names = z.namelist()
            self.assertIn("Alfred/daemon/extra_module.py", names)
            self.assertNotIn("Alfred/daemon/test_aigen.py", names)


class EmbeddablePth(unittest.TestCase):
    def test_pth_content_has_package_root(self):
        # The exact file this build ships; downloaded once by the build script and cached
        # under .build-cache/. Skip (don't fail) if the cache is empty — no network here.
        cache = ROOT / ".build-cache"
        zips = sorted(cache.glob("python-3.12*-embed-amd64.zip")) if cache.is_dir() else []
        if not zips:
            self.skipTest("no cached embeddable Python zip; build script downloads it")
        import zipfile
        with zipfile.ZipFile(zips[0]) as z:
            content = z.read("python312._pth").decode()
        lines = [line.strip() for line in content.splitlines()]
        self.assertIn(".", lines, "python312._pth must list the package root ('.') so "
                                   "daemon/alfred_launcher import with no pip/venv")


class StagedFiles(unittest.TestCase):
    def test_launcher_files_exist(self):
        win = ROOT / "app" / "windows"
        self.assertTrue((win / "Alfred.vbs").is_file())
        self.assertTrue((win / "Stop Alfred.vbs").is_file())
        self.assertTrue((win / "alfred_launcher.py").is_file())

    def test_daemon_is_stdlib_only(self):
        import re
        banned = re.compile(r"^\s*(import|from)\s+(tkinter|pip|numpy|requests)\b")
        for path in (ROOT / "daemon").glob("*.py"):
            if path.name == "test_aigen.py":
                continue
            text = path.read_text()
            for line in text.splitlines():
                self.assertIsNone(banned.match(line), f"{path.name}: {line!r}")


class StdinSelectWindowsBlocker(unittest.TestCase):
    """select.select() on Windows only supports sockets — calling it on stdin (a file/
    pipe/console handle) raises there. pythonw also gives sys.stdin=None. Either must
    keep the run loop file-watch only, and --companion mode must never touch stdin."""

    def test_stdin_watchable_false_on_win32(self):
        with mock.patch.object(aigen.sys, "stdin", object()):
            self.assertFalse(aigen._stdin_watchable(platform="win32"))

    def test_stdin_watchable_false_when_stdin_is_none(self):
        with mock.patch.object(aigen.sys, "stdin", None):
            self.assertFalse(aigen._stdin_watchable(platform="darwin"))

    def test_stdin_watchable_true_for_real_readable_stream_on_mac(self):
        class _FakeTTY:
            def readable(self):
                return True
        with mock.patch.object(aigen.sys, "stdin", _FakeTTY()):
            self.assertTrue(aigen._stdin_watchable(platform="darwin"))

    def test_one_loop_iteration_never_calls_select_under_win32_pythonw(self):
        """sys.stdin=None + platform win32 injected (pythonw with no console): one
        _run_loop iteration must run to completion without calling select.select — a
        call there would raise on real Windows, so raise here too if it happens."""
        with tempfile.TemporaryDirectory() as exchange:
            aigen.bind_exchange(exchange)
            aigen.ensure_exchange_files()

            def _forbidden_select(*a, **k):
                raise AssertionError("select.select() must not be called under win32/pythonw")

            class _StopLoop(Exception):
                pass

            calls = []

            def fake_time_sleep(seconds):
                calls.append(seconds)
                raise _StopLoop  # stop after the loop's own no-stdin 1s poll sleep

            with mock.patch.object(aigen.sys, "stdin", None), \
                 mock.patch.object(aigen.sys, "platform", "win32"), \
                 mock.patch.object(aigen.select, "select", _forbidden_select), \
                 mock.patch.object(aigen.time, "sleep", fake_time_sleep):
                try:
                    aigen._run_loop({})
                except _StopLoop:
                    pass
                else:
                    self.fail("_run_loop returned instead of looping")
            self.assertEqual(calls, [1.0])


if __name__ == "__main__":
    unittest.main()
