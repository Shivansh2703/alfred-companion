"""Box 3 (Alfred.app): plist shape, translocation refusal, Stop, and plain_open opening the
Box 6 web UI. Temp HOME only, no network beyond 127.0.0.1 in-process. The fake runner FAILS
the test if anything ever calls it with a real launchctl binary outside this module's
control, and osascript must never be spawned at all (Box 6 replaced those dialogs)."""
import importlib.util
import os
import plistlib
import re
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

if sys.platform != "darwin":
    raise unittest.SkipTest("Mac app")

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app" / "alfred_app.py"

spec = importlib.util.spec_from_file_location("alfred_app", APP)
alfred_app = importlib.util.module_from_spec(spec)
spec.loader.exec_module(alfred_app)


class FakeResult:
    def __init__(self, stdout="", stderr="", returncode=0):
        self.stdout, self.stderr, self.returncode = stdout, stderr, returncode


class FakeRunner:
    """Records every call; never touches a real process. A call to anything other than
    launchctl fails the test outright — osascript is gone."""

    def __init__(self, replies=None):
        self.calls = []
        self.replies = list(replies or [])

    def __call__(self, cmd, **kwargs):
        self.calls.append(cmd)
        binary = cmd[0]
        if binary != "launchctl":
            raise AssertionError(f"real subprocess spawn attempted: {cmd!r}")
        if self.replies:
            return self.replies.pop(0)
        return FakeResult()


class Plist(unittest.TestCase):
    def test_plist_has_right_executable_and_companion_flag(self):
        with tempfile.TemporaryDirectory() as home:
            os.environ.pop("ALFRED_DATA_DIR", None)
            with mock.patch.dict(os.environ, {"ALFRED_DATA_DIR": str(Path(home) / "data")}):
                path = alfred_app.write_plist("/Applications/Alfred.app/Contents/MacOS/Alfred",
                                              home=home)
            self.assertTrue(path.is_file())
            data = plistlib.loads(path.read_bytes())
            self.assertEqual(data["ProgramArguments"],
                             ["/Applications/Alfred.app/Contents/MacOS/Alfred", "--companion"])
            self.assertTrue(data["RunAtLoad"])

    def test_no_keepalive(self):
        with tempfile.TemporaryDirectory() as home:
            with mock.patch.dict(os.environ, {"ALFRED_DATA_DIR": str(Path(home) / "data")}):
                path = alfred_app.write_plist("/Applications/Alfred.app/Contents/MacOS/Alfred",
                                              home=home)
            data = plistlib.loads(path.read_bytes())
            self.assertNotIn("KeepAlive", data)

    def test_key_absent_from_plist(self):
        with tempfile.TemporaryDirectory() as home:
            with mock.patch.dict(os.environ, {"ALFRED_DATA_DIR": str(Path(home) / "data")}):
                path = alfred_app.write_plist("/Applications/Alfred.app/Contents/MacOS/Alfred",
                                              home=home)
            self.assertNotIn("AIza", path.read_text(errors="ignore"))
            self.assertNotIn("secret-key", path.read_text(errors="ignore"))


class Translocation(unittest.TestCase):
    def test_app_ok_rejects_translocation(self):
        self.assertFalse(alfred_app.app_ok(
            "/private/var/folders/xx/AppTranslocation/ABC/d.app/Contents/MacOS/Alfred"))

    def test_app_ok_rejects_outside_applications(self):
        self.assertFalse(alfred_app.app_ok("/Users/x/Downloads/Alfred.app/Contents/MacOS/Alfred"))

    def test_app_ok_accepts_applications(self):
        self.assertTrue(alfred_app.app_ok("/Applications/Alfred.app/Contents/MacOS/Alfred"))

    def test_app_ok_rejects_dot_dot_traversal_out_of_applications(self):
        # looks like it starts with "/Applications/" as a string, but really resolves outside it
        self.assertFalse(alfred_app.app_ok(
            "/Applications/../Users/x/Downloads/Alfred.app/Contents/MacOS/Alfred"))

    def test_plain_open_translocated_installs_nothing_and_opens_nothing_real(self):
        with tempfile.TemporaryDirectory() as home:
            runner = FakeRunner()
            opened = []
            with mock.patch.object(sys, "executable",
                                   "/private/var/folders/xx/AppTranslocation/y/Alfred.app/Contents/MacOS/Alfred"):
                alfred_app.plain_open(runner=runner, home=home, opener=opened.append)
            self.assertFalse((Path(home) / "Library/LaunchAgents" / alfred_app.PLIST_NAME).exists())
            self.assertEqual(runner.calls, [])
            self.assertEqual(len(opened), 1)
            self.assertTrue(opened[0].endswith("move_to_applications.html"))

    def test_plain_open_non_applications_opens_move_page_installs_nothing(self):
        """Box 11 (live-found): a Downloads launch used to open about:blank with nothing
        else shown. It must now open the move-to-Applications page, and still install no
        LaunchAgent (a plist pointed at a Downloads path breaks after reboot anyway)."""
        with tempfile.TemporaryDirectory() as home:
            runner = FakeRunner()
            opened = []
            with mock.patch.object(sys, "executable",
                                   "/Users/x/Downloads/Alfred.app/Contents/MacOS/Alfred"):
                alfred_app.plain_open(runner=runner, home=home, opener=opened.append)
            self.assertFalse((Path(home) / "Library/LaunchAgents" / alfred_app.PLIST_NAME).exists())
            self.assertEqual(runner.calls, [])
            self.assertEqual(len(opened), 1)
            uri = opened[0]
            self.assertTrue(uri.startswith("file://"))
            self.assertTrue(uri.endswith("move_to_applications.html"))


class MoveToApplicationsPageStaged(unittest.TestCase):
    """The move-to-Applications page must be staged the same way starting.html already is
    (app/alfred.spec datas, scripts/build_macos_app.sh staging) — starting.html was left
    out of one of these once before and that is exactly the gap this guards."""

    def test_spec_bundles_move_to_applications_html(self):
        spec_text = (ROOT / "app" / "alfred.spec").read_text()
        self.assertIn("move_to_applications.html", spec_text)

    def test_build_script_stages_move_to_applications_html(self):
        script_text = (ROOT / "scripts" / "build_macos_app.sh").read_text()
        self.assertIn("move_to_applications.html", script_text)


class Stop(unittest.TestCase):
    def test_stop_removes_plist_and_bootouts(self):
        with tempfile.TemporaryDirectory() as home:
            with mock.patch.dict(os.environ, {"ALFRED_DATA_DIR": str(Path(home) / "data")}):
                path = alfred_app.write_plist("/Applications/Alfred.app/Contents/MacOS/Alfred",
                                              home=home)
            self.assertTrue(path.exists())
            runner = FakeRunner()
            alfred_app.stop(runner=runner, home=home)
            self.assertFalse(path.exists())
            self.assertEqual(runner.calls[0][:2], ["launchctl", "bootout"])
            self.assertIn(alfred_app.APP_ID, runner.calls[0][2])

    def test_stop_leaves_credentials_in_place(self):
        with tempfile.TemporaryDirectory() as home:
            data_dir = Path(home) / "data"
            data_dir.mkdir()
            cred = data_dir / "alfred-credentials.json"
            cred.write_text('{"api_key": "fake"}')
            with mock.patch.dict(os.environ, {"ALFRED_DATA_DIR": str(data_dir)}):
                alfred_app.write_plist("/Applications/Alfred.app/Contents/MacOS/Alfred", home=home)
                runner = FakeRunner()
                alfred_app.stop(runner=runner, home=home)
            self.assertTrue(cred.exists())
            self.assertEqual(cred.read_text(), '{"api_key": "fake"}')


class Install(unittest.TestCase):
    def test_bootout_runs_before_bootstrap(self):
        with tempfile.TemporaryDirectory() as home:
            with mock.patch.dict(os.environ, {"ALFRED_DATA_DIR": str(Path(home) / "data")}):
                runner = FakeRunner()
                plist, warning = alfred_app.install(
                    "/Applications/Alfred.app/Contents/MacOS/Alfred", runner=runner, home=home)
            self.assertIsNone(warning)
            self.assertEqual(runner.calls[0][:2], ["launchctl", "bootout"])
            self.assertEqual(runner.calls[1][:2], ["launchctl", "bootstrap"])

    def test_bootstrap_failure_is_non_fatal(self):
        with tempfile.TemporaryDirectory() as home:
            with mock.patch.dict(os.environ, {"ALFRED_DATA_DIR": str(Path(home) / "data")}):
                runner = FakeRunner(replies=[
                    FakeResult(returncode=0),  # bootout
                    FakeResult(returncode=5, stderr="Bootstrap failed: 5: Input/output error"),
                ])
                plist, warning = alfred_app.install(
                    "/Applications/Alfred.app/Contents/MacOS/Alfred", runner=runner, home=home)
            self.assertTrue(plist.exists())
            self.assertIsNotNone(warning)
            self.assertIn("Input/output error", warning)


class PlainOpenWebUI(unittest.TestCase):
    """Box 6: plain_open no longer shows any dialog — it installs the LaunchAgent (which
    starts the companion, which serves the local page) and opens that page in the default
    browser, using the port+token the companion recorded."""

    def test_plain_open_opens_recorded_webui_url_with_token(self):
        with tempfile.TemporaryDirectory() as home:
            data_dir = Path(home) / "data"
            with mock.patch.dict(os.environ, {"ALFRED_DATA_DIR": str(data_dir)}):
                daemon_dir = str(ROOT / "daemon")
                if daemon_dir not in sys.path:
                    sys.path.insert(0, daemon_dir)
                import webui

                def fake_install(executable, runner=None, home=None):
                    return None, None

                runner = FakeRunner()
                opened = []
                with mock.patch.object(alfred_app, "install", fake_install), \
                     mock.patch.object(webui, "live_info", side_effect=[
                         None, {"port": 54321, "token": "tok-abc"}]), \
                     mock.patch.object(sys, "executable",
                                       "/Applications/Alfred.app/Contents/MacOS/Alfred"):
                    alfred_app.plain_open(runner=runner, home=home, opener=opened.append,
                                           sleep=lambda s: None)
            self.assertEqual(len(opened), 1)
            self.assertIn("54321", opened[0])  # port only; the token never rides the URL

    def test_plain_open_reuses_reachable_companion_without_reinstalling(self):
        with tempfile.TemporaryDirectory() as home:
            data_dir = Path(home) / "data"
            with mock.patch.dict(os.environ, {"ALFRED_DATA_DIR": str(data_dir)}):
                import webui
                opened = []
                with mock.patch.object(webui, "live_info", return_value={
                         "port": 54321, "token": "tok-abc"}), \
                     mock.patch.object(alfred_app, "install") as install, \
                     mock.patch.object(sys, "executable",
                                       "/Applications/Alfred.app/Contents/MacOS/Alfred"):
                    alfred_app.plain_open(home=home, opener=opened.append)
            install.assert_not_called()
            self.assertEqual(opened, ["http://127.0.0.1:54321/"])

    def test_plain_open_never_calls_osascript(self):
        with tempfile.TemporaryDirectory() as home:
            data_dir = Path(home) / "data"
            with mock.patch.dict(os.environ, {"ALFRED_DATA_DIR": str(data_dir)}):
                runner = FakeRunner()
                opened = []
                with mock.patch.object(sys, "executable",
                                       "/Applications/Alfred.app/Contents/MacOS/Alfred"):
                    alfred_app.plain_open(runner=runner, home=home, opener=opened.append,
                                           sleep=lambda s: None)
            for call in runner.calls:
                self.assertNotEqual(call[0], "osascript")

    def test_plain_open_gives_up_after_bounded_wait_with_no_webui_file(self):
        with tempfile.TemporaryDirectory() as home:
            data_dir = Path(home) / "data"
            with mock.patch.dict(os.environ, {"ALFRED_DATA_DIR": str(data_dir)}):
                runner = FakeRunner()
                opened = []
                slept = []
                with mock.patch.object(sys, "executable",
                                       "/Applications/Alfred.app/Contents/MacOS/Alfred"):
                    alfred_app.plain_open(runner=runner, home=home, opener=opened.append,
                                           sleep=slept.append)
            # never hangs open with nothing to show: opens the bundled starting page instead
            self.assertEqual(len(opened), 1)
            self.assertIn("starting.html", opened[0])
            self.assertGreater(len(slept), 0)

    def test_plain_open_unlinks_stale_webui_info_before_install(self):
        """A dead companion's leftover webui.json must never win the race against the
        fresh one the freshly-installed companion writes. fake_install writes NOTHING, so
        if the stale file survived, plain_open would open it (port 11111); the only way
        this test can pass is if the stale file was unlinked, leaving the bounded wait to
        time out onto the starting page."""
        with tempfile.TemporaryDirectory() as home:
            data_dir = Path(home) / "data"
            with mock.patch.dict(os.environ, {"ALFRED_DATA_DIR": str(data_dir)}):
                daemon_dir = str(ROOT / "daemon")
                if daemon_dir not in sys.path:
                    sys.path.insert(0, daemon_dir)
                import webui
                webui._write_info(str(data_dir), 11111, "stale-tok")

                def fake_install(executable, runner=None, home=None):
                    return None, None  # the real companion is never started in this test

                runner = FakeRunner()
                opened = []
                with mock.patch.object(alfred_app, "install", fake_install), \
                     mock.patch.object(webui, "live_info", return_value=None), \
                     mock.patch.object(sys, "executable",
                                       "/Applications/Alfred.app/Contents/MacOS/Alfred"):
                    alfred_app.plain_open(runner=runner, home=home, opener=opened.append,
                                           sleep=lambda s: None)
            self.assertEqual(len(opened), 1)
            self.assertNotIn("11111", opened[0])
            self.assertIn("starting.html", opened[0])
            self.assertNotIn("11111", opened[0])
            self.assertFalse(Path(webui.info_path(str(data_dir))).exists())

    def test_stop_unlinks_webui_info(self):
        with tempfile.TemporaryDirectory() as home:
            data_dir = Path(home) / "data"
            with mock.patch.dict(os.environ, {"ALFRED_DATA_DIR": str(data_dir)}):
                daemon_dir = str(ROOT / "daemon")
                if daemon_dir not in sys.path:
                    sys.path.insert(0, daemon_dir)
                import webui
                webui._write_info(str(data_dir), 33333, "tok")
                runner = FakeRunner()
                alfred_app.stop(runner=runner, home=home)
                self.assertFalse(Path(webui.info_path(str(data_dir))).exists())


class DmgScript(unittest.TestCase):
    """Box 11 scope addition: the Mac download is a drag-to-Applications disk image."""

    def test_dmg_script_has_applications_symlink_and_udzo(self):
        script = (ROOT / "scripts" / "build_macos_dmg.sh").read_text()
        self.assertIn("ln -s /Applications", script)
        self.assertIn("UDZO", script)

    def test_build_script_calls_dmg_script(self):
        script = (ROOT / "scripts" / "build_macos_app.sh").read_text()
        self.assertIn("build_macos_dmg.sh", script)

    def test_dmg_mounts_with_app_and_applications_symlink(self):
        import shutil
        app = ROOT / "dist" / "mac-app" / "Alfred.app"
        if shutil.which("hdiutil") is None or not app.is_dir():
            self.skipTest("hdiutil unavailable or Alfred.app not built")
        # build_macos_dmg.sh resolves ROOT from its own script location (`cd $(dirname "$0")/..`)
        # and always writes dist/mac-app/Alfred-Mac.dmg under that ROOT with `rm -f` first — run
        # it against a real copy of the repo's script tree, never the real one, so this test
        # never rewrites a release artifact someone may be about to ship.
        fake_root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, fake_root, ignore_errors=True)
        (fake_root / "scripts").mkdir()
        shutil.copy2(ROOT / "scripts" / "build_macos_dmg.sh", fake_root / "scripts" / "build_macos_dmg.sh")
        (fake_root / "dist" / "mac-app").mkdir(parents=True)
        shutil.copytree(app, fake_root / "dist" / "mac-app" / "Alfred.app")
        dmg_script = fake_root / "scripts" / "build_macos_dmg.sh"
        subprocess.run([str(dmg_script)], check=True, cwd=str(fake_root))
        dmg = fake_root / "dist" / "mac-app" / "Alfred-Mac.dmg"
        self.assertTrue(dmg.is_file())
        mount = tempfile.mkdtemp()
        try:
            subprocess.run(["hdiutil", "attach", "-nobrowse", "-readonly",
                             "-mountpoint", mount, str(dmg)], check=True,
                            capture_output=True, text=True)
            self.assertTrue((Path(mount) / "Alfred.app").exists())
            self.assertTrue((Path(mount) / "Applications").is_symlink())
        finally:
            subprocess.run(["hdiutil", "detach", mount], capture_output=True, text=True)


class StartingPagePackaged(unittest.TestCase):
    def test_starting_html_exists_and_is_plain_text(self):
        page = (ROOT / "app" / "starting.html").read_text()
        self.assertIn("Alfred is starting", page)

    def test_starting_html_in_spec_datas(self):
        spec = (ROOT / "app" / "alfred.spec").read_text()
        self.assertIn("starting.html", spec)


class StagingList(unittest.TestCase):
    def test_build_script_never_stages_credentials(self):
        script = (ROOT / "scripts" / "build_macos_app.sh").read_text()
        for banned in ("alfred-credentials.json", "alfred.local.json"):
            # the refusal scan names them; no cp/stage line may copy either in
            self.assertFalse(re.search(rf"^cp .*{re.escape(banned)}", script, re.MULTILINE))
        self.assertIn('banned = ("alfred-credentials.json", "alfred.local.json")', script)
        self.assertNotIn("/private/tmp", script)


class NoOsascriptAnywhere(unittest.TestCase):
    def test_no_osascript_reference_in_app_source(self):
        self.assertNotIn("osascript", APP.read_text())


if __name__ == "__main__":
    unittest.main()
