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
import time
import urllib.error
import urllib.parse
import urllib.request

INFO_FILE = "webui.json"
PROVIDERS = [("gemini", "Gemini"), ("ollama", "Ollama (this computer)")]
DEFAULT_MODELS = {"gemini": "gemini-3.5-flash-lite", "ollama": "qwen3.5:4b"}


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


def live_info(data_dir_path, timeout=1):
    """Return the recorded server only when it answers Alfred's health endpoint."""
    info = read_info(data_dir_path)
    if not info:
        return None
    try:
        with urllib.request.urlopen(url_for(info["port"], path="/health"), timeout=timeout) as response:
            if response.read() != b"alfred\n":
                return None
    except (OSError, ValueError, urllib.error.URLError):
        return None
    return info


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


def _page(status_ok, status_message, token, error=None, saved=False, notice=None,
          stop_available=True, dashboard=None):
    dashboard = dashboard or {}
    configured_provider = dashboard.get("provider")
    provider = configured_provider or "gemini"
    options = "".join(
        f'<option value="{value}"{" selected" if value == provider else ""}>{label}</option>'
        for value, label in PROVIDERS)
    status_class = "ok" if status_ok else "warn"
    banner = ""
    if saved:
        banner = '<p class="ok">Saved.</p>'
    if notice:
        banner += f'<p class="ok">{html.escape(notice)}</p>'
    if error:
        banner += f'<p class="warn">{html.escape(error)}</p>'
    selected = dashboard.get("selected") or {}
    selected_id = str(selected.get("id") or "")
    hidden_campaign = (f'<input type="hidden" name="campaign" '
                       f'value="{html.escape(selected_id)}">') if selected_id else ""
    stop_form = ""
    if stop_available:
        stop_form = f"""<form method="post" action="/stop" class="inline">
<input type="hidden" name="token" value="{html.escape(token)}">
<button id="stop" class="danger" type="submit">Stop companion</button>
</form>"""
    key_saved = bool(dashboard.get("key_saved"))
    key_state = "Saved securely" if key_saved else "No Gemini key saved"
    key_help = ("Leave this blank to keep the saved key. Enter a new key only when you want "
                "to replace it." if key_saved else
                "Paste a Gemini API key. Alfred stores it locally and never displays it again.")
    probe = dashboard.get("probe")
    if probe:
        probe_class = "ok" if probe.get("ok") else "warn"
        probe_text = html.escape(str(probe.get("message") or "Model check finished."))
    else:
        probe_class, probe_text = "muted", "Not tested in this companion session."
    model = html.escape(str(dashboard.get("model") or "Not configured"))
    config_error = dashboard.get("config_error")
    config_note = f'<p class="warn">{html.escape(str(config_error))}</p>' if config_error else ""

    campaigns = dashboard.get("campaigns") or []
    campaign_links = []
    for campaign in campaigns:
        cid = str(campaign.get("id") or "")
        label = html.escape(str(campaign.get("label") or cid))
        turn = campaign.get("turn")
        badge = '<span class="badge">Current snapshot</span>' if campaign.get("current") else ""
        current_class = " active" if cid == selected_id else ""
        campaign_links.append(
            f'<a class="campaign{current_class}" href="/?campaign={urllib.parse.quote(cid)}">'
            f'<strong>{label}</strong><span>Turn {html.escape(str(turn if turn is not None else "?"))}</span>{badge}</a>')
    campaign_nav = "".join(campaign_links) or '<p class="muted">No campaign data yet. Open a campaign and ask Alfred something in-game.</p>'

    campaign_detail = '<p class="muted">Campaign details will appear after Alfred receives its first game snapshot.</p>'
    if selected:
        def metric(label, value):
            shown = "—" if value is None else html.escape(str(value))
            return f'<div class="metric"><span>{label}</span><strong>{shown}</strong></div>'

        last_seen = selected.get("last_seen")
        seen = (time.strftime("%Y-%m-%d %H:%M", time.localtime(last_seen))
                if isinstance(last_seen, (int, float)) else "Unknown")
        wars = selected.get("wars") or []
        war_text = ", ".join(html.escape(str(w)) for w in wars) if wars else "None recorded"
        context_rows = "".join(f"<li>{html.escape(str(line))}</li>" for line in selected.get("context", []))
        chat_rows = []
        for turn in selected.get("history", []):
            you = html.escape(str(turn.get("you", "")))
            alfred = html.escape(str(turn.get("alfred", "")))
            if you:
                chat_rows.append(f'<div class="message you"><b>You</b><p>{you}</p></div>')
            if alfred:
                chat_rows.append(f'<div class="message alfred"><b>Alfred</b><p>{alfred}</p></div>')
        transcript = "".join(chat_rows) or '<p class="muted">No saved chat for this campaign.</p>'
        campaign_detail = f"""
<div class="section-head"><div><h2>{html.escape(str(selected.get('label') or selected_id))}</h2>
<p class="muted">Latest data received: {html.escape(seen)}. This is a snapshot, not a claim that the game is currently open.</p></div>
<a class="button secondary" href="/?campaign={urllib.parse.quote(selected_id)}">Refresh</a></div>
<div class="metrics">
{metric('Turn', selected.get('turn'))}{metric('Treasury', selected.get('treasury'))}
{metric('Income', selected.get('income'))}{metric('Net income', selected.get('net_income'))}
{metric('Regions', selected.get('regions'))}{metric('Armies', selected.get('armies'))}
</div>
<p><strong>Wars:</strong> {war_text}</p>
<h3>What Alfred knows</h3><ul class="context">{context_rows}</ul>
<p class="muted">The model receives this campaign snapshot, legal options, Alfred's measured journal, preferences, and recent conversation. API keys are never included.</p>
<h3>Recent chat</h3><div class="transcript">{transcript}</div>
"""
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>Alfred</title>
<style>
:root {{ color-scheme: light dark; --bg:#f4f1ea; --panel:#fff; --ink:#25231f; --muted:#6c675d;
--line:#ddd6ca; --accent:#27604b; --soft:#e9f1ec; --warn:#9b3a2d; }}
@media (prefers-color-scheme:dark) {{ :root{{--bg:#171815;--panel:#22231f;--ink:#f3efe6;--muted:#aaa59a;--line:#3d3d37;--accent:#8ec9ab;--soft:#28352e;--warn:#f0a092;}} }}
* {{ box-sizing:border-box; }} body {{ margin:0; background:var(--bg); color:var(--ink); font:15px/1.5 -apple-system,system-ui,sans-serif; }}
main {{ max-width:1080px; margin:0 auto; padding:32px 20px 64px; }} h1,h2,h3,p {{ margin-top:0; }}
.eyebrow {{ color:var(--muted); text-transform:uppercase; letter-spacing:.08em; font-size:12px; }}
.grid {{ display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:16px; }}
.panel {{ background:var(--panel); border:1px solid var(--line); border-radius:14px; padding:20px; margin-top:16px; box-shadow:0 4px 18px rgba(0,0,0,.04); }}
.status strong {{ display:block; font-size:18px; }} .ok {{ color:var(--accent); }} .warn {{ color:var(--warn); }} .muted {{ color:var(--muted); }}
label {{ display:block; margin-top:14px; font-weight:600; }} input,select {{ width:100%; padding:10px; margin-top:5px; border:1px solid var(--line); border-radius:8px; background:var(--panel); color:var(--ink); }}
button,.button {{ display:inline-block; margin:14px 8px 0 0; padding:9px 14px; border:0; border-radius:8px; background:var(--accent); color:white; font:inherit; text-decoration:none; cursor:pointer; }}
.secondary {{ background:var(--soft); color:var(--accent); border:1px solid var(--line); }} .danger {{ background:var(--warn); }} .inline {{ display:inline; }}
.campaigns {{ display:grid; grid-template-columns:260px 1fr; gap:18px; }} .campaign-list {{ display:flex; flex-direction:column; gap:8px; }}
.campaign {{ position:relative; display:flex; flex-direction:column; padding:12px; border:1px solid var(--line); border-radius:9px; color:var(--ink); text-decoration:none; }}
.campaign.active {{ border-color:var(--accent); background:var(--soft); }} .campaign span {{ color:var(--muted); font-size:13px; }}
.badge {{ color:var(--accent)!important; font-weight:700; }} .section-head {{ display:flex; justify-content:space-between; gap:16px; align-items:flex-start; }}
.metrics {{ display:grid; grid-template-columns:repeat(3,1fr); gap:8px; }} .metric {{ background:var(--bg); border-radius:8px; padding:10px; }} .metric span,.metric strong {{ display:block; }}
.context {{ padding-left:20px; }} .transcript {{ max-height:480px; overflow:auto; }} .message {{ padding:10px 12px; margin:8px 0; border-left:3px solid var(--line); }}
.message p {{ margin:3px 0 0; white-space:pre-wrap; }} .message.alfred {{ border-color:var(--accent); background:var(--soft); }}
@media(max-width:760px) {{ .grid,.campaigns,.metrics {{ grid-template-columns:1fr; }} .section-head {{ flex-direction:column; }} }}
</style></head>
<body>
<main><p class="eyebrow">Total War: Warhammer III companion</p><h1>Alfred</h1>
{banner}
<div class="grid">
<section class="panel status"><span class="eyebrow">Companion</span><strong class="ok">Running</strong><span>This page is served by Alfred on this computer.</span></section>
<section class="panel status"><span class="eyebrow">Setup</span><strong class="{status_class}">{'Ready' if status_ok else 'Needs attention'}</strong><span>{html.escape(status_message)}</span></section>
<section class="panel status"><span class="eyebrow">Model</span><strong>{html.escape(configured_provider.title() if configured_provider else 'Not configured')}</strong><span>{model}</span></section>
</div>
<section class="panel"><h2>Model and API key</h2>
<p>Alfred runs the selected model in the background whenever the in-game companion needs an answer.</p>
{config_note}<form method="post" action="/setup">
<input type="hidden" name="token" value="{html.escape(token)}">
{hidden_campaign}
<label>Provider<select name="provider" required>{options}</select></label>
<label>Gemini API key — {key_state}<input type="password" name="api_key" autocomplete="new-password"></label>
<p class="muted">{html.escape(key_help)} Ollama uses no API key. Switching providers does not delete the saved Gemini key.</p>
<button type="submit">Save and test</button></form>
<form method="post" action="/test-model" class="inline"><input type="hidden" name="token" value="{html.escape(token)}">{hidden_campaign}<button class="secondary" type="submit">Test connection</button></form>
{f'<form method="post" action="/forget-key" class="inline"><input type="hidden" name="token" value="{html.escape(token)}">{hidden_campaign}<button class="danger" type="submit">Forget saved key</button></form>' if key_saved else ''}
<p class="{probe_class}"><strong>Connection check:</strong> {probe_text}</p>
</section>
<section class="panel"><h2>Campaigns Alfred knows</h2><div class="campaigns"><nav class="campaign-list">{campaign_nav}</nav><div>{campaign_detail}</div></div></section>
<section class="panel"><h2>Companion controls</h2><p class="muted">Stopping Alfred leaves campaign memory, preferences, and credentials on this computer.</p>
{stop_form}
</section>
</main></body></html>"""


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

    def _dashboard(self, selected=None):
        if self.server.dashboard_fn is None:
            return {}
        try:
            return self.server.dashboard_fn(selected)
        except Exception:
            return {"config_error": "Dashboard data is temporarily unavailable."}

    def _render(self, *, selected=None, error=None, saved=False, notice=None):
        ok, message = self._status()
        self._send_html(_page(ok, message, self.server.token, error=error, saved=saved,
                              notice=notice, stop_available=self.server.stop_fn is not None,
                              dashboard=self._dashboard(selected)))

    def do_GET(self):
        if not _host_ok(self.headers.get("Host")):
            return self._reject(403)
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/health":
            body = b"alfred\n"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if parsed.path != "/":
            return self._reject(404)
        selected = urllib.parse.parse_qs(parsed.query).get("campaign", [None])[0]
        self._render(selected=selected)

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
        if parsed.path == "/test-model":
            return self._handle_test_model(fields)
        if parsed.path == "/forget-key":
            return self._handle_forget_key(fields)
        if parsed.path == "/stop":
            return self._handle_stop()
        return self._reject(404)

    def _handle_setup(self, fields):
        provider = fields.get("provider", [""])[0]
        model = DEFAULT_MODELS.get(provider)
        selected = fields.get("campaign", [None])[0]
        if not model:
            return self._render(selected=selected, error="Choose Gemini or Ollama.")
        payload = {"provider": provider, "model": model}
        api_key = fields.get("api_key", [""])[0]
        if api_key:
            payload["api_key"] = api_key
        try:
            self.server.apply_setup_request(payload)
        except ValueError as e:
            return self._render(selected=selected, error=str(e))
        self._render(selected=selected, saved=True)

    def _handle_test_model(self, fields):
        selected = fields.get("campaign", [None])[0]
        if self.server.test_model_fn is None:
            return self._render(selected=selected, error="Model testing is unavailable.")
        ok, message = self.server.test_model_fn()
        self._render(selected=selected, notice=message if ok else None,
                     error=None if ok else message)

    def _handle_forget_key(self, fields):
        selected = fields.get("campaign", [None])[0]
        if self.server.forget_api_key_fn is None:
            return self._render(selected=selected, error="Forgetting the key is unavailable.")
        self.server.forget_api_key_fn()
        self._render(selected=selected, notice="Saved Gemini key forgotten.")

    def _handle_stop(self):
        if self.server.stop_fn is None:
            return self._send_html("Stopping is not available in this mode.", code=404)
        self.server.stop_fn()
        self._send_html(_page(False, "Alfred is stopped.", self.server.token,
                               stop_available=False))


class Server(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def start(data_dir_path, apply_setup_request, stop_fn, check_setup_fn,
          dashboard_fn=None, test_model_fn=None, forget_api_key_fn=None,
          host="127.0.0.1"):
    """Starts the web UI on an ephemeral 127.0.0.1 port in a background thread and
    records port+token at <data_dir_path>/webui.json (0600). Returns (server, thread,
    port, token); server.shutdown() stops it."""
    server = Server((host, 0), Handler)
    server.token = secrets.token_urlsafe(24)
    server.apply_setup_request = apply_setup_request
    server.stop_fn = stop_fn
    server.check_setup_fn = check_setup_fn
    server.dashboard_fn = dashboard_fn
    server.test_model_fn = test_model_fn
    server.forget_api_key_fn = forget_api_key_fn
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
