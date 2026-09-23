"""Box 6: the local web UI that replaced the osascript dialogs. In-process server on
127.0.0.1 only, no real browser/launchctl, no network beyond 127.0.0.1. Every test uses a
temp data dir; the API key must never be echoed into a response body or a log."""
import http.client
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "daemon"))
import webui  # noqa: E402
import aigen  # noqa: E402


class FakeSetup:
    def __init__(self):
        self.calls = []

    def __call__(self, payload):
        self.calls.append(payload)
        if len(payload.get("api_key", "")) > 1000:
            raise ValueError("setup API key is invalid")


class WebUITestCase(unittest.TestCase):
    """Starts one real server per test on 127.0.0.1:0 and tears it down after."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data_dir = self.tmp.name
        self.apply_setup = FakeSetup()
        self.stop_calls = []
        self.test_calls = []
        self.forget_calls = []
        campaign = {
            "id": "campaign-1", "label": "Yvresse", "current": True, "turn": 8,
            "last_seen": 1_700_000_000, "treasury": 3533, "income": 4432,
            "net_income": -1665, "regions": 6, "armies": 10, "wars": ["Broken Axe"],
            "context": ["Turn 8 with 6 regions.", "Buildings enabled; skills enabled."],
            "history": [{"you": "What now? <script>alert(1)</script>",
                         "alfred": "Hold cash."}],
        }
        self.dashboard_data = {
            "provider": "gemini", "model": "gemini-3.5-flash-lite",
            "key_saved": True, "probe": {"ok": True, "message": "model check passed"},
            "campaigns": [campaign], "selected": campaign,
        }

        def dashboard(selected=None):
            data = dict(self.dashboard_data)
            if selected:
                data["selected"] = next(
                    (row for row in data["campaigns"] if row["id"] == selected),
                    data.get("selected"))
            return data

        self.server, self.thread, self.port, self.token = webui.start(
            self.data_dir, self.apply_setup, lambda: self.stop_calls.append(True),
            lambda: (True, "Alfred is ready. Start Warhammer III."), dashboard,
            lambda: (self.test_calls.append(True) or (True, "model check passed")),
            lambda: self.forget_calls.append(True))

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def _conn(self):
        return http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)

    def _post(self, path, body, host=None):
        conn = self._conn()
        data = body.encode()
        if host is None:
            conn.request("POST", path, body=data,
                         headers={"Content-Type": "application/x-www-form-urlencoded"})
        else:
            conn.putrequest("POST", path, skip_host=True)
            conn.putheader("Host", host)
            conn.putheader("Content-Type", "application/x-www-form-urlencoded")
            conn.putheader("Content-Length", str(len(data)))
            conn.endheaders()
            conn.send(data)
        resp = conn.getresponse()
        return resp.status, resp.read().decode()


class Get(WebUITestCase):
    def test_get_returns_status_text_and_form(self):
        conn = self._conn()
        conn.request("GET", "/")
        resp = conn.getresponse()
        body = resp.read().decode()
        self.assertEqual(resp.status, 200)
        self.assertIn("Alfred is ready. Start Warhammer III.", body)
        self.assertIn("<form", body)
        self.assertIn('name="provider"', body)
        self.assertNotIn('name="model"', body)
        self.assertIn('name="api_key"', body)
        self.assertIn("Gemini", body)
        self.assertIn("Ollama (this computer)", body)
        self.assertNotIn("SECRET-XYZ", body)

    def test_dashboard_explains_key_reuse_and_shows_safe_campaign_context(self):
        conn = self._conn()
        conn.request("GET", "/?campaign=campaign-1")
        body = conn.getresponse().read().decode()
        self.assertIn("Leave this blank to keep the saved key", body)
        self.assertIn("Saved securely", body)
        self.assertIn("Campaigns Alfred knows", body)
        self.assertIn("Yvresse", body)
        self.assertIn("What Alfred knows", body)
        self.assertIn("Recent chat", body)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", body)
        self.assertNotIn("<script>alert(1)</script>", body)
        self.assertIn('<option value="gemini" selected>', body)

    def test_live_info_accepts_only_the_running_alfred_server(self):
        self.assertEqual(webui.live_info(self.data_dir)["port"], self.port)
        webui._write_info(self.data_dir, 1, "stale")
        self.assertIsNone(webui.live_info(self.data_dir, timeout=0.1))


class Security(WebUITestCase):
    def test_post_without_token_is_forbidden(self):
        status, _ = self._post("/setup", "provider=gemini&model=x")
        self.assertEqual(status, 403)
        self.assertEqual(self.apply_setup.calls, [])

    def test_post_with_wrong_host_is_forbidden(self):
        status, _ = self._post("/setup", f"token={self.token}&provider=gemini&model=x",
                                host="evil.example.com")
        self.assertEqual(status, 403)
        self.assertEqual(self.apply_setup.calls, [])

    def test_server_binds_only_127_0_0_1(self):
        self.assertEqual(self.server.server_address[0], "127.0.0.1")


class Setup(WebUITestCase):
    def test_valid_setup_calls_apply_and_never_echoes_key(self):
        status, body = self._post(
            "/setup", f"token={self.token}&provider=gemini&api_key=SECRET-XYZ")
        self.assertEqual(status, 200)
        self.assertEqual(self.apply_setup.calls,
                         [{"provider": "gemini", "model": "gemini-3.5-flash-lite",
                           "api_key": "SECRET-XYZ"}])
        self.assertNotIn("SECRET-XYZ", body)

    def test_posted_model_is_ignored(self):
        status, body = self._post(
            "/setup", f"token={self.token}&provider=gemini&model=some-other-model&api_key=SECRET-XYZ")
        self.assertEqual(status, 200)
        self.assertEqual(self.apply_setup.calls[0]["model"], "gemini-3.5-flash-lite")
        self.assertNotIn("some-other-model", body)

    def test_ollama_uses_default_model_and_needs_no_key(self):
        status, body = self._post("/setup", f"token={self.token}&provider=ollama")
        self.assertEqual(status, 200)
        self.assertEqual(self.apply_setup.calls,
                         [{"provider": "ollama", "model": "qwen3.5:4b"}])

    def test_blank_gemini_key_is_not_added_to_the_request(self):
        status, body = self._post(
            "/setup", f"token={self.token}&provider=gemini&api_key=")
        self.assertEqual(status, 200)
        self.assertEqual(self.apply_setup.calls,
                         [{"provider": "gemini", "model": "gemini-3.5-flash-lite"}])
        self.assertNotIn('value="SECRET', body)

    def test_invalid_key_shows_error_and_writes_nothing(self):
        long_key = "A" * 2000
        status, body = self._post(
            "/setup", f"token={self.token}&provider=gemini&api_key={long_key}")
        self.assertEqual(status, 200)
        self.assertIn("setup API key is invalid", body)
        self.assertNotIn(long_key, body)

    def test_connection_test_and_forget_key_are_explicit_actions(self):
        status, body = self._post("/test-model", f"token={self.token}&campaign=campaign-1")
        self.assertEqual(status, 200)
        self.assertEqual(self.test_calls, [True])
        self.assertIn("model check passed", body)
        status, body = self._post("/forget-key", f"token={self.token}&campaign=campaign-1")
        self.assertEqual(status, 200)
        self.assertEqual(self.forget_calls, [True])
        self.assertIn("Saved Gemini key forgotten", body)


class SetupWithRealAigen(unittest.TestCase):
    """Same invalid-key case through the real apply_setup_request, proving no credential
    file gets written."""

    def test_invalid_key_writes_no_credential_file(self):
        with tempfile.TemporaryDirectory() as home:
            data_dir = Path(home) / "data"
            with mock.patch.dict(os.environ, {"ALFRED_DATA_DIR": str(data_dir)}):
                server, thread, port, token = webui.start(
                    str(data_dir), aigen.apply_setup_request, lambda: None,
                    lambda: (True, "ok"))
                try:
                    long_key = "A" * 2000
                    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                    body = f"token={token}&provider=gemini&api_key={long_key}".encode()
                    conn.request("POST", "/setup", body=body,
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
                    resp = conn.getresponse()
                    text = resp.read().decode()
                    self.assertEqual(resp.status, 200)
                    self.assertNotIn(long_key, text)
                finally:
                    server.shutdown()
                    server.server_close()
            cred = data_dir / "alfred-credentials.json"
            self.assertFalse(cred.exists())

    def test_blank_gemini_key_reuses_saved_key_without_revealing_it(self):
        with tempfile.TemporaryDirectory() as home:
            data_dir = Path(home) / "data"
            marker = "SAVED-SECRET-XYZ"
            with mock.patch.dict(os.environ, {"ALFRED_DATA_DIR": str(data_dir)}):
                aigen.apply_setup_request({"provider": "gemini",
                                           "model": "gemini-3.5-flash-lite",
                                           "api_key": marker})
                server, thread, port, token = webui.start(
                    str(data_dir), aigen.apply_setup_request, lambda: None,
                    lambda: (True, "ok"))
                try:
                    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                    body = f"token={token}&provider=gemini&api_key=".encode()
                    conn.request("POST", "/setup", body=body,
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
                    resp = conn.getresponse()
                    text = resp.read().decode()
                    self.assertEqual(resp.status, 200)
                    self.assertNotIn(marker, text)
                finally:
                    server.shutdown()
                    server.server_close()
                self.assertEqual(aigen._stored_api_key(), marker)


class Stop(WebUITestCase):
    def test_stop_calls_injected_stop_fn(self):
        status, body = self._post("/stop", f"token={self.token}")
        self.assertEqual(status, 200)
        self.assertEqual(self.stop_calls, [True])


class StopUnavailable(unittest.TestCase):
    """Box 3's bare-daemon mode passes stop_fn=None; the page must not offer a Stop button
    that would silently no-op, and a direct POST must not crash the handler."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.apply_setup = FakeSetup()
        self.server, self.thread, self.port, self.token = webui.start(
            self.tmp.name, self.apply_setup, None,
            lambda: (True, "Alfred is ready. Start Warhammer III."))

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def _conn(self):
        return http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)

    def test_page_has_no_stop_form_when_stop_fn_is_none(self):
        conn = self._conn()
        conn.request("GET", "/")
        body = conn.getresponse().read().decode()
        self.assertNotIn('action="/stop"', body)

    def test_post_stop_with_no_stop_fn_does_not_call_anything_and_is_not_ok(self):
        conn = self._conn()
        data = f"token={self.token}".encode()
        conn.request("POST", "/stop", body=data,
                     headers={"Content-Type": "application/x-www-form-urlencoded"})
        resp = conn.getresponse()
        resp.read()
        self.assertNotEqual(resp.status, 200)


class PostBodyValidation(WebUITestCase):
    """Content-Length is parsed defensively BEFORE any token/setup work: a malformed or
    oversized value must be refused without ever touching apply_setup_request, and without
    hanging the handler waiting on a body that never fully arrives."""

    def _raw_post(self, content_length_header):
        conn = self._conn()
        conn.putrequest("POST", "/setup", skip_host=True)
        conn.putheader("Host", "127.0.0.1")
        conn.putheader("Content-Type", "application/x-www-form-urlencoded")
        conn.putheader("Content-Length", content_length_header)
        conn.endheaders()
        conn.send(b"token=x")  # deliberately short/absent vs. the claimed length
        resp = conn.getresponse()
        status = resp.status
        resp.read()
        conn.close()
        return status

    def test_non_numeric_content_length_is_rejected(self):
        status = self._raw_post("abc")
        self.assertEqual(status, 400)
        self.assertEqual(self.apply_setup.calls, [])

    def test_negative_content_length_is_rejected(self):
        status = self._raw_post("-1")
        self.assertGreaterEqual(status, 400)
        self.assertLess(status, 500)
        self.assertEqual(self.apply_setup.calls, [])

    def test_oversized_content_length_is_rejected(self):
        status = self._raw_post("200000")
        self.assertEqual(status, 413)
        self.assertEqual(self.apply_setup.calls, [])


class Logging(WebUITestCase):
    def test_key_never_printed(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            self._post("/setup",
                      f"token={self.token}&provider=gemini&api_key=SECRET-XYZ")
        self.assertNotIn("SECRET-XYZ", buf.getvalue())


class UrlFor(unittest.TestCase):
    def test_token_never_rides_the_url(self):
        self.assertNotIn("tok-abc", webui.url_for(54321, "tok-abc"))


class DashboardSnapshot(unittest.TestCase):
    def test_snapshot_combines_current_and_historical_campaigns_without_secrets(self):
        with tempfile.TemporaryDirectory() as d:
            paths = {name: str(Path(d) / filename) for name, filename in {
                "STATE": "state.json", "CHAT_HISTORY": "chat.json",
                "JOURNAL": "journal.json", "PREFS": "prefs.json"}.items()}
            Path(paths["STATE"]).write_text(json.dumps({
                "campaign_id": "current", "faction": "faction_key",
                "faction_names": {"faction_key": "Yvresse", "enemy": "Broken Axe"},
                "turn": 8, "treasury": 3533, "income": 4432, "net_income": -1665,
                "regions": [{}, {}], "armies": [{}],
                "diplomacy": {"at_war_with": ["enemy"]},
            }))
            Path(paths["CHAT_HISTORY"]).write_text(json.dumps({
                "current": [{"you": "hello", "alfred": "ready"}],
                "old": [{"you": "old", "alfred": "memory"}],
            }))
            Path(paths["JOURNAL"]).write_text(json.dumps({
                "old": [{"turn": 3, "faction_name": "Eataine", "recorded_at": 123,
                         "treasury": 1000, "net_income": 200, "regions": 2,
                         "armies": 1, "at_war_with": []}],
            }))
            Path(paths["PREFS"]).write_text(json.dumps({
                "current": {"stance": "hold", "reserve_floor": 4000,
                            "capabilities": {"buildings": True, "skills": False}},
            }))
            with mock.patch.multiple(aigen, **paths), \
                 mock.patch.object(aigen, "load_model_config", return_value={
                     "provider": "gemini", "model": "gemini-3.5-flash-lite"}), \
                 mock.patch.object(aigen, "_stored_api_key", return_value="NEVER-RETURN-ME"), \
                 mock.patch.object(aigen, "_PROBE", (("gemini", "gemini-3.5-flash-lite"),
                                                      True, "model check passed")):
                snapshot = aigen.dashboard_snapshot("old")
            self.assertTrue(snapshot["key_saved"])
            self.assertNotIn("NEVER-RETURN-ME", repr(snapshot))
            self.assertEqual([c["id"] for c in snapshot["campaigns"]], ["current", "old"])
            self.assertEqual(snapshot["campaigns"][0]["label"], "Yvresse")
            self.assertEqual(snapshot["campaigns"][0]["wars"], ["Broken Axe"])
            self.assertEqual(snapshot["selected"]["label"], "Eataine")
            self.assertIn("reserve floor 4000", " ".join(snapshot["campaigns"][0]["context"]))

    def test_corrupt_campaign_files_degrade_to_an_empty_dashboard(self):
        with tempfile.TemporaryDirectory() as d:
            paths = {name: str(Path(d) / filename) for name, filename in {
                "STATE": "state.json", "CHAT_HISTORY": "chat.json",
                "JOURNAL": "journal.json", "PREFS": "prefs.json"}.items()}
            for path in paths.values():
                Path(path).write_text("not json")
            with mock.patch.multiple(aigen, **paths), \
                 mock.patch.object(aigen, "load_model_config", return_value={
                     "provider": "ollama", "model": "qwen3.5:4b"}), \
                 mock.patch.object(aigen, "_stored_api_key", return_value=None), \
                 mock.patch.object(aigen, "_PROBE", None):
                snapshot = aigen.dashboard_snapshot()
            self.assertEqual(snapshot["campaigns"], [])
            self.assertIsNone(snapshot["selected"])


class AtomicWrites(unittest.TestCase):
    """Box 6 bounce item 4: both credential-adjacent files land via tmp+os.replace, so a
    crash mid-write never leaves a truncated file for the webui/daemon to load next boot."""

    def test_write_info_leaves_no_tmp_and_is_0600(self):
        with tempfile.TemporaryDirectory() as d:
            path = webui._write_info(d, 4242, "tok")
            self.assertFalse(os.path.exists(path + ".tmp"))
            if sys.platform != "win32":
                self.assertEqual(stat_mode_bits(path), 0o600)

    def test_write_setup_config_leaves_no_tmp_and_is_0600(self):
        with tempfile.TemporaryDirectory() as home:
            data_dir = Path(home) / "data"
            # _config_path() resolves to the real repo root by default — never let this test
            # write alfred.local.json there (it would clobber the owner's real config and,
            # under scripts/check.sh, silently repoint the live daemon at "https://x").
            with mock.patch.object(aigen, "_config_path", lambda: str(Path(home) / "alfred.local.json")), \
                 mock.patch.dict(os.environ, {"ALFRED_DATA_DIR": str(data_dir)}):
                aigen._write_setup_config({"provider": "ollama", "model": "m",
                                            "base_url": "http://127.0.0.1:11434"})
                cfg_path = aigen._config_path()
                cred_path = aigen._credentials_path()
                aigen._write_setup_config({"provider": "gemini", "model": "m",
                                            "base_url": "https://x", "api_key_source": "stored"},
                                           api_key="SECRET")
            self.assertFalse(os.path.exists(cfg_path + ".tmp"))
            self.assertFalse(os.path.exists(cred_path + ".tmp"))
            if sys.platform != "win32":
                self.assertEqual(stat_mode_bits(cfg_path), 0o600)
                self.assertEqual(stat_mode_bits(cred_path), 0o600)


def stat_mode_bits(path):
    import stat
    return stat.S_IMODE(os.stat(path).st_mode)


if __name__ == "__main__":
    unittest.main()
