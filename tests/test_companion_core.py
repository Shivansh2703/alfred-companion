#!/usr/bin/env python3
"""Box 1 (companion core): Steam discovery across Mac/Windows libraries, plain-words
SetupError refusals, TLS that verifies against trusted roots, --companion's wait loop,
and A3's setup-request disarm. Temp HOME / temp dirs only, no network, no real security
or launchctl calls (the darwin TLS fallback runner is injected)."""
import io
import json
import os
import ssl
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "daemon"))
import paths  # noqa: E402
import aigen  # noqa: E402


def _steam_manifest(root):
    (root / "steamapps").mkdir(parents=True, exist_ok=True)
    (root / "steamapps" / "appmanifest_1142710.acf").write_text('"buildid"\t"9001"\n')


class SteamRoots(unittest.TestCase):
    def test_mac_second_library_from_vdf(self):
        with tempfile.TemporaryDirectory() as home:
            home = Path(home)
            default = home / "Library/Application Support/Steam"
            (default / "steamapps").mkdir(parents=True)
            other_lib = home / "external-drive/SteamLibrary"
            other_lib.mkdir(parents=True)
            (default / "steamapps/libraryfolders.vdf").write_text(
                '"libraryfolders"\n{\n\t"1"\n\t{\n\t\t"path"\t"%s"\n\t}\n}\n'
                % str(other_lib).replace("\\", "\\\\"))
            _steam_manifest(other_lib)  # manifest ONLY in the second library
            manifest = paths.find_game_manifest(home=str(home), platform="darwin", environ={})
            self.assertEqual(manifest, str(other_lib / "steamapps/appmanifest_1142710.acf"))
            self.assertEqual(paths.game_build_id(manifest), "9001")

    def test_windows_default_root_and_vdf_library_no_winreg(self):
        with tempfile.TemporaryDirectory() as home:
            home = Path(home)
            program_files = home / "Program Files (x86)"
            default = program_files / "Steam"
            (default / "steamapps").mkdir(parents=True)
            lib2 = home / "AltLibrary"
            lib2.mkdir()
            (default / "steamapps/libraryfolders.vdf").write_text(
                '"libraryfolders"\n{\n\t"0"\n\t{\n\t\t"path"\t"%s"\n\t}\n}\n'
                % str(lib2).replace("\\", "\\\\"))
            _steam_manifest(lib2)
            appdata = home / "AppData/Roaming"
            appdata.mkdir(parents=True)
            environ = {"ProgramFiles(x86)": str(program_files), "APPDATA": str(appdata)}
            with mock.patch.object(paths, "winreg", None):  # no winreg on this Mac anyway
                manifest = paths.find_game_manifest(home=str(home), platform="win32", environ=environ)
            self.assertEqual(manifest, str(lib2 / "steamapps/appmanifest_1142710.acf"))

            exchange = appdata / "The Creative Assembly/Warhammer3/scripts"
            exchange.mkdir(parents=True)
            found = paths.discover_exchange_dir(required=True, home=str(home),
                                                 platform="win32", environ=environ)
            self.assertEqual(found, str(exchange))

    def test_missing_game_setuperror_plain_words(self):
        with tempfile.TemporaryDirectory() as home:
            with self.assertRaises(paths.SetupError) as ctx:
                paths.find_game_manifest(home=home, platform="darwin", environ={})
            text = str(ctx.exception)
            self.assertNotIn(os.sep, text)
            self.assertNotIn("\\", text)
            self.assertNotIn("Traceback", text)
            self.assertIsInstance(ctx.exception, ValueError)  # subclass contract


class MainRefusal(unittest.TestCase):
    def test_main_missing_manifest_exits_plain(self):
        """_startup() must exit with find_game_manifest()'s plain refusal when Steam has no
        WARHAMMER III install. The old version of this test set ALFRED_EXCHANGE, which makes
        discover_exchange_dir() succeed and _startup() fall through to profile/rulebook
        auto-detection FIRST (active_profile() -> tools/extract_db.py) -- present in this
        dev checkout, but not in a companion-only export, where it masked this refusal behind
        an unrelated 'pack detection failed' error. Profile/rulebook detection is mocked out
        here so the test isolates the one thing it's meant to check; find_game_manifest()
        itself is NOT mocked, so this still runs the real production code and its real
        message against a real empty $HOME (no Steam anywhere to find)."""
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as exchange:
            with mock.patch.dict(os.environ, {"HOME": home}), \
                 mock.patch.object(aigen, "discover_exchange_dir", return_value=exchange), \
                 mock.patch.object(aigen, "_expected_build_id", return_value=None), \
                 mock.patch.object(aigen, "load_rules", return_value={"game_build_id": "unreachable"}):
                buf = io.StringIO()
                with self.assertRaises(SystemExit) as ctx, redirect_stdout(buf):
                    aigen.main()
        self.assertIn("Alfred could not find Total War", str(ctx.exception))


class EnsureExchangeFiles(unittest.TestCase):
    def test_creates_setup_request_marker_init(self):
        with tempfile.TemporaryDirectory() as exchange:
            aigen.bind_exchange(exchange)
            aigen.ensure_exchange_files()
            self.assertTrue(os.path.exists(aigen.SETUP_REQUEST))
            self.assertEqual(open(aigen.SETUP_REQUEST).read(), "init")


class SetupRequestDisarmed(unittest.TestCase):
    """A3: the daemon never applies aigen_setup_request.txt; a leftover request is
    emptied in place at startup, never parsed or logged."""
    def test_fake_key_emptied_no_credential_no_log(self):
        with tempfile.TemporaryDirectory() as exchange, tempfile.TemporaryDirectory() as root:
            aigen.bind_exchange(exchange)
            aigen.ensure_exchange_files()
            marker = "SETUP_SECRET_MARKER_DO_NOT_LEAK"
            with open(aigen.SETUP_REQUEST, "w") as f:
                f.write(json.dumps({"provider": "gemini", "model": "m", "api_key": marker}) + "\n")
            old_root = aigen.ROOT
            aigen.ROOT = root
            buf = io.StringIO()
            try:
                with redirect_stdout(buf):
                    aigen._disarm_setup_request()
            finally:
                aigen.ROOT = old_root
            self.assertEqual(open(aigen.SETUP_REQUEST).read(), "init")
            credential = os.path.join(root, ".cache", "alfred-credentials.json")
            self.assertFalse(os.path.exists(credential))
            self.assertNotIn(marker, buf.getvalue())

    def test_plain_init_left_alone(self):
        with tempfile.TemporaryDirectory() as exchange:
            aigen.bind_exchange(exchange)
            aigen.ensure_exchange_files()
            aigen._disarm_setup_request()
            self.assertEqual(open(aigen.SETUP_REQUEST).read(), "init")

    def test_apply_setup_request_stays_importable(self):
        self.assertTrue(callable(aigen.apply_setup_request))


class CompanionWaitLoop(unittest.TestCase):
    """A4: background mode waits, never exits."""
    def test_missing_exchange_dir_waits_no_systemexit(self):
        with tempfile.TemporaryDirectory() as home:
            env = {"HOME": home}
            env.pop("ALFRED_EXCHANGE", None)
            calls = []

            def fake_sleep(seconds):
                calls.append(seconds)
                if len(calls) >= 3:
                    raise _StopLoop

            class _StopLoop(Exception):
                pass

            with mock.patch.dict(os.environ, env, clear=True):
                try:
                    aigen._run_companion(sleep=fake_sleep, wait_seconds=30)
                except _StopLoop:
                    pass
                else:
                    self.fail("companion loop returned instead of waiting")
            self.assertEqual(calls, [30, 30, 30])

    def test_generic_exception_keeps_waiting_not_silent_death(self):
        """No launchd KeepAlive means an uncaught exception here is a silent death, not
        a restart — _run_companion must catch Exception, not just SetupError."""
        calls = []

        class _StopLoop(Exception):
            pass

        def fake_sleep(seconds):
            calls.append(seconds)
            if len(calls) >= 2:
                raise _StopLoop

        with mock.patch.object(aigen, "_startup", side_effect=RuntimeError("boom")):
            buf = io.StringIO()
            with redirect_stdout(buf):
                try:
                    aigen._run_companion(sleep=fake_sleep, wait_seconds=5)
                except _StopLoop:
                    pass
                else:
                    self.fail("companion loop returned instead of waiting")
        self.assertEqual(calls, [5, 5])
        self.assertIn("boom", buf.getvalue())

    def test_same_message_printed_once_per_change(self):
        """A stuck refusal must not spam the log once per 30s forever."""
        calls = []
        prints = []

        class _StopLoop(Exception):
            pass

        def fake_sleep(seconds):
            calls.append(seconds)
            if len(calls) >= 4:
                raise _StopLoop

        errors = iter([paths.SetupError("same message"), paths.SetupError("same message"),
                       paths.SetupError("different message"), paths.SetupError("different message")])

        def fake_startup():
            raise next(errors)

        real_print = print

        def capturing_print(*a, **kw):
            prints.append(" ".join(str(x) for x in a))

        with mock.patch.object(aigen, "_startup", side_effect=fake_startup), \
             mock.patch("builtins.print", capturing_print):
            try:
                aigen._run_companion(sleep=fake_sleep, wait_seconds=1)
            except _StopLoop:
                pass
            else:
                self.fail("companion loop returned instead of waiting")
        self.assertEqual(prints, ["same message", "different message"])


class SslContextTls(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cert = os.path.join(self.tmp.name, "cert.pem")
        self.key = os.path.join(self.tmp.name, "key.pem")
        subprocess.run(
            ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
             "-subj", "/CN=localhost", "-addext", "subjectAltName=DNS:localhost",
             "-keyout", self.key, "-out", self.cert],
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.server = ThreadingHTTPServer(("localhost", 0), BaseHTTPRequestHandler)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(self.cert, self.key)
        self.server.socket = ctx.wrap_socket(self.server.socket, server_side=True)
        import threading
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.shutdown)
        self.url = f"https://localhost:{self.server.server_port}"

    def test_default_refuses_self_signed(self):
        with self.assertRaises(RuntimeError) as ctx:
            aigen._http_json(self.url, {}, 2)
        self.assertIn("URLError", str(ctx.exception))

    def test_accepted_with_cert_as_cadata(self):
        cadata = open(self.cert).read()
        context = aigen._ssl_context(cadata=cadata)
        with self.assertRaises(RuntimeError) as ctx:
            aigen._http_json(self.url, {}, 2, context=context)
        # BaseHTTPRequestHandler has no do_POST -> 501, but TLS was trusted to get there
        self.assertIn("HTTP 501", str(ctx.exception))

    def test_context_defaults_secure(self):
        ctx = aigen._ssl_context()
        self.assertEqual(ctx.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(ctx.check_hostname)

    def test_darwin_fallback_calls_injected_runner(self):
        calls = []

        def fake_runner(cmd, **kwargs):
            calls.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, stdout=open(self.cert).read(), stderr="")

        class _EmptyStoreCtx:
            def cert_store_stats(self):
                return {"x509_ca": 0}

        real_create = ssl.create_default_context
        calls_to_create = []

        def fake_create_default_context(*a, **kw):
            calls_to_create.append((a, kw))
            if not kw.get("cadata"):
                return _EmptyStoreCtx()
            return real_create(*a, **kw)

        with mock.patch.object(sys, "platform", "darwin"), \
             mock.patch.object(ssl, "create_default_context", fake_create_default_context):
            aigen._ssl_context(runner=fake_runner)
        self.assertEqual(len(calls), 1)
        self.assertIn("/usr/bin/security", calls[0])


if __name__ == "__main__":
    unittest.main()
    print("PASS: test_companion_core.py")
