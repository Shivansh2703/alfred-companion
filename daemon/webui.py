"""Local web UI (Box 6): replaces the native dialogs with one small page served by
the companion on 127.0.0.1. stdlib only (http.server), one codebase for Mac and Windows.

Security: bound to 127.0.0.1 only; every POST needs the token (written 0600 next to the
other daemon data) as a hidden form field, and a Host header of 127.0.0.1/localhost —
that pair is the CSRF + DNS-rebinding guard. The API key never gets echoed back into the
page and is never printed/logged here; it only ever appears in the POST body and the
0600 credential file that apply_setup_request() writes.
"""
import html
import http.server
import json
import os
import secrets
import threading
import urllib.parse

INFO_FILE = "webui.json"
PROVIDERS = ["gemini", "ollama", "openai_compatible"]


def info_path(data_dir_path):
    return os.path.join(data_dir_path, INFO_FILE)


def read_info(data_dir_path):
    try:
        with open(info_path(data_dir_path)) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or "port" not in data or "token" not in data:
        return None
    return data


def _write_info(data_dir_path, port, token):
    os.makedirs(data_dir_path, exist_ok=True)
    path = info_path(data_dir_path)
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump({"port": port, "token": token}, f)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    return path


def _host_ok(host_header):
    host = (host_header or "").split(":")[0]
    return host in ("127.0.0.1", "localhost")


def _page(status_ok, status_message, token, error=None, saved=False, stop_available=True):
    options = "".join(f'<option value="{p}">{p}</option>' for p in PROVIDERS)
    status_class = "ok" if status_ok else "warn"
    banner = ""
    if saved:
        banner = '<p class="ok">Saved.</p>'
    if error:
        banner += f'<p class="warn">{html.escape(error)}</p>'
    stop_form = ""
    if stop_available:
        stop_form = f"""<form method="post" action="/stop">
<input type="hidden" name="token" value="{html.escape(token)}">
<button id="stop" type="submit">Stop Alfred</button>
</form>"""
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>Alfred</title>
<style>
body {{ font-family: -apple-system, system-ui, sans-serif; max-width: 480px; margin: 3em auto;
        padding: 0 1em; color: #222; }}
p.ok {{ color: #146c2e; }} p.warn {{ color: #a12; }}
label {{ display: block; margin-top: 1em; }}
input, select {{ width: 100%; padding: 0.4em; box-sizing: border-box; }}
button {{ margin-top: 1.5em; padding: 0.5em 1em; }}
#stop {{ background: #a12; color: #fff; border: none; }}
</style></head>
<body>
<h1>Alfred</h1>
<p class="{status_class}">{html.escape(status_message)}</p>
{banner}
<form method="post" action="/setup">
<input type="hidden" name="token" value="{html.escape(token)}">
<label>Provider<select name="provider" required>{options}</select></label>
<label>Model<input type="text" name="model" required></label>
<label>API key (not needed for Ollama)<input type="password" name="api_key"></label>
<button type="submit">Save</button>
</form>
{stop_form}
</body></html>"""


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "Alfred/1"
    protocol_version = "HTTP/1.1"

    def log_message(self, *a, **kw):
        pass  # ponytail: quiet by default; swap for real logging if debugging needs it

    def _reject(self, code):
        body = b"forbidden" if code == 403 else b"not found"
        self.send_response(code)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, body_str, code=200):
        body = body_str.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _status(self):
        try:
            return self.server.check_setup_fn()
        except Exception as e:  # never let a status probe crash the page
            return False, f"status check failed: {e}"

    def do_GET(self):
        if not _host_ok(self.headers.get("Host")):
            return self._reject(403)
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != "/":
            return self._reject(404)
        ok, message = self._status()
        self._send_html(_page(ok, message, self.server.token,
                               stop_available=self.server.stop_fn is not None))

    # ponytail: 64KB is generous for a form post of provider/model/api_key; nothing
    # this page collects needs more, so a bigger claim is refused before it's read.
    MAX_BODY = 64 * 1024

    def _read_body(self):
        """Parses Content-Length defensively, before any token/setup work. Returns the raw
        body bytes, or None after already sending an error response."""
        raw_length = self.headers.get("Content-Length")
        try:
            length = int(raw_length) if raw_length is not None else 0
        except ValueError:
            self._reject(400)
            return None
        if length < 0 or length > self.MAX_BODY:
            self._reject(413)
            return None
        return self.rfile.read(length) if length else b""

    def do_POST(self):
        if not _host_ok(self.headers.get("Host")):
            return self._reject(403)
        raw = self._read_body()
        if raw is None:
            return
        fields = urllib.parse.parse_qs(raw.decode("utf-8", errors="replace"),
                                        keep_blank_values=True)
        token = fields.get("token", [""])[0]
        if not secrets.compare_digest(token, self.server.token):
            return self._reject(403)
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/setup":
            return self._handle_setup(fields)
        if parsed.path == "/stop":
            return self._handle_stop()
        return self._reject(404)

    def _handle_setup(self, fields):
        payload = {"provider": fields.get("provider", [""])[0],
                   "model": fields.get("model", [""])[0]}
        api_key = fields.get("api_key", [""])[0]
        if api_key:
            payload["api_key"] = api_key
        stop_available = self.server.stop_fn is not None
        ok, message = self._status()
        try:
            self.server.apply_setup_request(payload)
        except ValueError as e:
            return self._send_html(_page(ok, message, self.server.token, error=str(e),
                                          stop_available=stop_available))
        ok, message = self._status()
        self._send_html(_page(ok, message, self.server.token, saved=True,
                               stop_available=stop_available))

    def _handle_stop(self):
        if self.server.stop_fn is None:
            return self._send_html("Stopping is not available in this mode.", code=404)
        self.server.stop_fn()
        self._send_html(_page(False, "Alfred is stopped.", self.server.token,
                               stop_available=False))


class Server(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def start(data_dir_path, apply_setup_request, stop_fn, check_setup_fn, host="127.0.0.1"):
    """Starts the web UI on an ephemeral 127.0.0.1 port in a background thread and
    records port+token at <data_dir_path>/webui.json (0600). Returns (server, thread,
    port, token); server.shutdown() stops it."""
    server = Server((host, 0), Handler)
    server.token = secrets.token_urlsafe(24)
    server.apply_setup_request = apply_setup_request
    server.stop_fn = stop_fn
    server.check_setup_fn = check_setup_fn
    port = server.server_address[1]
    _write_info(data_dir_path, port, server.token)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread, port, server.token


def url_for(port, token=None, path="/"):
    """token is unused (kept for call-site compatibility): the page never needs it in the
    URL, since GET / requires no token and the token reaches /setup and /stop only through
    the hidden POST field the page itself renders."""
    return f"http://127.0.0.1:{port}{path}"
