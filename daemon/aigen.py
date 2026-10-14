#!/usr/bin/env python3
"""AI General daemon — watches the exchange dir, computes legal options from the
extracted rulebook, one LLM call per sweep (claude -p headless), writes validated
commands back for the mod to execute.

Usage: daemon/aigen.py            # run: Enter in this terminal = admin sweep (v1 hotkey)
       daemon/aigen.py --dry-run  # everything except the LLM call; prints the prompt
"""
import hashlib, json, os, re, select, ssl, subprocess, sys, time, urllib.error, urllib.parse, urllib.request, uuid

_DAEMON_DIR = os.path.dirname(os.path.abspath(__file__))
if _DAEMON_DIR not in sys.path:
    sys.path.insert(0, _DAEMON_DIR)
from paths import (SetupError, check_setup, companion_manifest, data_dir, discover_exchange_dir,
                    find_game_manifest, game_build_id, write_exchange_hint)
import webui

ROOT = os.path.dirname(_DAEMON_DIR)
EXCHANGE = discover_exchange_dir(required=False)
RULES = os.path.join(ROOT, "data", "rules.json")
PURPOSE_LABELS = os.path.join(ROOT, "data", "purpose_labels_vanilla.json")
STATE = DEV_HOOK = TRIGGER = COMMANDS = CANDIDATES = None
SWEEP_REQUEST = CHAT_REQUEST = CHAT_REPLY = SETUP_REQUEST = None
# TRANSCRIPT (not just the latest reply) — the in-game panel reads its scrollback straight out
# of this file, so no new exchange file has to exist before the game boots (Feral
# negative-lookup cache, docs/research/feral-file-visibility.md). If the panel fails to build,
# the old TriggerPopup path pops this same text and simply shows the history in a popup.
CHAT_HISTORY = os.path.join(data_dir(ROOT), "chat_history.json")  # survives a daemon restart
CHAT_KEEP = 10        # exchanges kept (both for Alfred's memory and for the panel scrollback)
CHAT_WRAP = 58        # chars per rendered row; chat_string rows do not wrap themselves
REPLY_CAP = 1200      # was 700 (a 58-char-wide window walled with text); CHAT_SYSTEM now
# invites an answer as long as the question needs, so the cap needs to be the panel's real
# ceiling, not an artificial second one — see _cap_reply for the truncation itself.
MAX_SKILL_OPTIONS = 12   # per character, keep the prompt terse
PROMPT_FX_MAX = 3        # effects shown per building option (rulebook keeps more)
# Below this, a building's gold figure is a side-effect rather than the reason to buy it, so
# no payback is computed and the payback rule does not apply to it. 5 not 1: the smallest real
# income buildings on both rulebooks are well above it, and the incidental effects seen are 1-3.
PAYBACK_MIN_GOLD = 5

PURPOSES = {"income", "growth", "public_order", "recruitment", "settlement_defence"}
STORED_PURPOSES = PURPOSES | {"unknown"}
_ACTIVE_PROFILE = None
_PENDING_PURPOSE = None  # daemon memory only; never reconstructed from chat history

MIN_SLOT_BREADTH = 3        # permitted slot templates below which a chain is a landmark, not an option
MAX_NEW_BUILD_OPTIONS = 8   # cheapest level-0 chains offered per empty slot, keep the prompt terse
MODEL_CONFIG = "alfred.local.json"


def bind_exchange(path):
    """Point every exchange-file global at one scripts directory."""
    global EXCHANGE, STATE, DEV_HOOK, TRIGGER, COMMANDS, CANDIDATES
    global SWEEP_REQUEST, CHAT_REQUEST, CHAT_REPLY, SETUP_REQUEST
    EXCHANGE = path or ""
    STATE = os.path.join(EXCHANGE, "aigen_state.json")
    DEV_HOOK = os.path.join(EXCHANGE, "aigen_dev.lua")
    TRIGGER = os.path.join(EXCHANGE, "aigen_trigger.txt")
    COMMANDS = os.path.join(EXCHANGE, "aigen_commands.lua")
    CANDIDATES = os.path.join(EXCHANGE, "aigen_candidates.lua")
    SWEEP_REQUEST = os.path.join(EXCHANGE, "aigen_sweep_request.txt")
    CHAT_REQUEST = os.path.join(EXCHANGE, "aigen_chat_request.txt")
    CHAT_REPLY = os.path.join(EXCHANGE, "aigen_chat_reply.txt")
    SETUP_REQUEST = os.path.join(EXCHANGE, "aigen_setup_request.txt")


bind_exchange(EXCHANGE)


def _config_path():
    return os.path.join(data_dir(ROOT), MODEL_CONFIG)


def _credentials_path():
    return os.path.join(data_dir(ROOT), "alfred-credentials.json")


def _valid_base_url(value):
    """Return a safe endpoint base: HTTPS, or plain HTTP only to this machine."""
    if not isinstance(value, str) or not value:
        raise ValueError("model config base_url must be a non-empty URL")
    p = urllib.parse.urlparse(value)
    try:
        p.port  # reject malformed ports while this is still a config error
    except ValueError as e:
        raise ValueError("model config base_url has an invalid port") from e
    loopback = p.hostname in ("localhost", "127.0.0.1", "::1")
    if (p.scheme != "https" and not (p.scheme == "http" and loopback)) or not p.netloc \
       or p.username or p.password or p.query or p.fragment:
        raise ValueError("model config base_url must use HTTPS, or HTTP on localhost only")
    return value.rstrip("/")


def load_model_config():
    """Load the local-only provider choice. No file means the historical Claude path."""
    path = _config_path()
    legacy = os.path.join(ROOT, MODEL_CONFIG)  # pre-0.1.1 location; read until the next save
    if not os.path.exists(path) and os.path.exists(legacy):
        path = legacy
    if not os.path.exists(path):
        return {"provider": "claude", "model": "claude", "timeout_seconds": 240}
    try:
        with open(path) as f:
            raw = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        raise ValueError(f"model config is unreadable: {e}") from e
    if not isinstance(raw, dict):
        raise ValueError("model config must be a JSON object")
    provider = raw.get("provider")
    model = raw.get("model")
    if provider not in ("claude", "ollama", "gemini", "openai_compatible"):
        raise ValueError("model config provider must be claude, ollama, gemini, or openai_compatible")
    if not isinstance(model, str) or not model.strip():
        raise ValueError("model config model must be a non-empty string")
    timeout = raw.get("timeout_seconds", 240)
    if not isinstance(timeout, int) or timeout <= 0:
        raise ValueError("model config timeout_seconds must be a positive integer")
    config = {"provider": provider, "model": model, "timeout_seconds": timeout}
    if provider == "ollama":
        config["base_url"] = _valid_base_url(raw.get("base_url", "http://127.0.0.1:11434"))
    elif provider in ("gemini", "openai_compatible"):
        config["base_url"] = _valid_base_url(raw.get(
            "base_url", "https://generativelanguage.googleapis.com/v1beta/openai" if provider == "gemini" else ""))
        source = raw.get("api_key_source")
        if source == "stored":
            config["api_key_source"] = source
        else:  # Existing explicit environment config remains supported.
            key_env = raw.get("api_key_env", "GEMINI_API_KEY" if provider == "gemini" else None)
            if not isinstance(key_env, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key_env):
                raise ValueError("model config api_key_env must be an environment-variable name")
            config["api_key_env"] = key_env
        effort = raw.get("reasoning_effort")
        if effort is None and provider == "gemini":
            # Defaults are model-specific; do not guess for an unfamiliar Gemini release.
            if model == "gemini-3.5-flash-lite": effort = "minimal"
            elif model.startswith("gemini-3.8-flash"): effort = "low"
            elif model.startswith("gemini-2.5"):
                effort = "low" if "pro" in model.lower() else "none"
        if effort is not None:
            if effort not in ("minimal", "none", "low"):
                raise ValueError("model config reasoning_effort must be minimal, none, or low")
            config["reasoning_effort"] = effort
    return config


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _decision_schema():
    return {"type": "object", "properties": {
        "reply": {"type": "string"},
        "skills": {"type": "array"}, "buildings": {"type": "array"},
    }, "required": ["reply", "skills", "buildings"], "additionalProperties": True}


def _parse_decision(content, *, truncated=False):
    if truncated:
        raise RuntimeError("model response was truncated; no commands were issued")
    if not isinstance(content, str) or not content.strip():
        raise ValueError("model response has no JSON content")
    text = content.strip()
    if text.startswith("```"):
        text = text.strip("`\n").removeprefix("json").strip()
    try:
        decision = json.loads(text)
    except json.JSONDecodeError as e:
        raise ValueError("model response is not valid JSON") from e
    if not isinstance(decision, dict):
        raise ValueError("model response must be a JSON object")
    return decision


def _ssl_context(cadata=None, runner=None):
    """Trusted-root TLS context. Never CERT_NONE, never check_hostname=False.
    On darwin with an empty default store (MEASURED on python.org builds — Box 1 step 4),
    fall back to the system root keychain exported via /usr/bin/security. `runner` is
    injectable so tests never spawn the real binary."""
    ctx = ssl.create_default_context(cadata=cadata) if cadata else ssl.create_default_context()
    if cadata is None and sys.platform == "darwin" and ctx.cert_store_stats()["x509_ca"] == 0:
        run = runner or subprocess.run
        result = run(
            ["/usr/bin/security", "find-certificate", "-a", "-p",
             "/System/Library/Keychains/SystemRootCertificates.keychain"],
            capture_output=True, text=True, check=True)
        ctx = ssl.create_default_context(cadata=result.stdout)
    return ctx


def _http_json(url, payload, timeout, headers=(), context=None):
    body = json.dumps(payload).encode()
    request = urllib.request.Request(url, body, {"Content-Type": "application/json", **dict(headers)})
    opener = urllib.request.build_opener(
        _NoRedirect, urllib.request.HTTPSHandler(context=context or _ssl_context()))
    try:
        with opener.open(request, timeout=timeout) as response:
            return json.loads(response.read().decode())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"model service returned HTTP {e.code}; no commands were issued") from e
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
        raise RuntimeError(f"model service request failed ({type(e).__name__}); no commands were issued") from e


def _stored_api_key():
    try:
        with open(_credentials_path()) as f:
            value = json.load(f).get("api_key")
    except (OSError, json.JSONDecodeError, AttributeError):
        return None
    return value if isinstance(value, str) and value else None


def _atomic_write_json(path, obj, mode, text=None):
    """Write path+'.tmp' at `mode` then os.replace over `path` — a crash or concurrent
    reader never sees a partial file, and the mode is set before any content lands."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "w") as f:
        f.write(text if text is not None else json.dumps(obj, indent=2, sort_keys=True) + "\n")
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def _write_setup_config(config, api_key=None):
    """Write only daemon-owned setup files; configuration intentionally has no secret."""
    if api_key is not None:
        _atomic_write_json(_credentials_path(), None, 0o600,
                            text=json.dumps({"api_key": api_key}))
    _atomic_write_json(_config_path(), config, 0o600)


def forget_setup():
    for path in (_config_path(), os.path.join(ROOT, MODEL_CONFIG), _credentials_path()):
        try: os.remove(path)
        except FileNotFoundError: pass


def parse_setup_request(text):
    """Complete one-line JSON only. None means a torn request and must be retried."""
    if not text or not text.endswith("\n"):
        return None
    try: payload = json.loads(text)
    except json.JSONDecodeError: raise ValueError("setup request is not valid JSON")
    if not isinstance(payload, dict): raise ValueError("setup request must be a JSON object")
    return payload


def apply_setup_request(payload):
    if payload == {"action": "forget"}:
        forget_setup(); return
    if set(payload) - {"provider", "model", "base_url", "api_key", "reasoning_effort"}:
        raise ValueError("setup request has unknown fields")
    provider, model = payload.get("provider"), payload.get("model")
    if provider not in ("gemini", "ollama", "openai_compatible") or not isinstance(model, str) or not model.strip() or len(model) > 200:
        raise ValueError("setup provider or model is invalid")
    if provider == "gemini":
        base_url, effort = "https://generativelanguage.googleapis.com/v1beta/openai", "minimal"
    elif provider == "ollama":
        base_url, effort = "http://127.0.0.1:11434", None
    else:
        base_url, effort = payload.get("base_url"), payload.get("reasoning_effort")
        _valid_base_url(base_url)
        if effort not in (None, "minimal", "none", "low"):
            raise ValueError("setup reasoning_effort is invalid")
    key = payload.get("api_key")
    if provider == "ollama":
        if key is not None: raise ValueError("Ollama does not accept an API key")
        config = {"provider": provider, "model": model, "base_url": base_url, "timeout_seconds": 240}
    else:
        if key is None:
            key = _stored_api_key()
        if not isinstance(key, str) or not key or len(key) > 1000:
            raise ValueError("setup API key is invalid")
        config = {"provider": provider, "model": model, "base_url": base_url, "api_key_source": "stored", "timeout_seconds": 240}
        if effort is not None: config["reasoning_effort"] = effort
        _write_setup_config(config, payload.get("api_key"))
        return
    _write_setup_config(config)


def request_decision(system, prompt, timeout_seconds=None):
    """Ask the configured model once and return only a complete JSON decision object."""
    config = load_model_config()
    started = time.monotonic()
    provider, model = config["provider"], config["model"]
    timeout = config["timeout_seconds"] if timeout_seconds is None else timeout_seconds
    tokens = "unavailable"
    if provider == "claude":
        try:
            result = subprocess.run(headless_argv(system, prompt), capture_output=True, text=True,
                                    timeout=timeout, stdin=subprocess.DEVNULL)
        except subprocess.TimeoutExpired as e:
            raise RuntimeError("Claude timed out; no commands were issued") from e
        except FileNotFoundError as e:
            # No config file defaults to this provider (owner's own machine relies on that),
            # but a fresh install has no `claude` CLI at all — the historical default silently
            # assumed one exists. Caught here, not with a shutil.which precheck, so the same
            # line also covers the CLI vanishing between a check and this call.
            raise SetupError(
                "Alfred doesn't have a model yet. Set your model in the Alfred Companion app "
                "and press Save.") from e
        if result.returncode:
            raise RuntimeError(f"Claude exited {result.returncode}; no commands were issued")
        decision = _parse_decision(result.stdout)
    else:
        if provider == "ollama":
            url = config["base_url"] + "/api/chat"
            payload = {"model": model, "messages": [{"role": "system", "content": system},
                       {"role": "user", "content": prompt}], "stream": False,
                       "format": _decision_schema(), "think": False,
                       "options": {"temperature": 0, "num_ctx": 16384, "num_predict": 4096},
                       "keep_alive": 0}
            response = _http_json(url, payload, timeout)
            message = response.get("message") if isinstance(response, dict) else None
            content = message.get("content") if isinstance(message, dict) else None
            truncated = response.get("done_reason") == "length" if isinstance(response, dict) else False
            tokens = ((response.get("prompt_eval_count", 0) or 0) +
                      (response.get("eval_count", 0) or 0)) if isinstance(response, dict) else "unknown"
        else:
            api_key = _stored_api_key() if config.get("api_key_source") == "stored" \
                else os.environ.get(config["api_key_env"])
            if not api_key:
                raise RuntimeError("configured API key is unavailable; no commands were issued")
            url = config["base_url"] + "/chat/completions"
            payload = {"model": model, "messages": [{"role": "system", "content": system},
                       {"role": "user", "content": prompt}], "temperature": 0,
                       "response_format": {"type": "json_schema", "json_schema": {
                           "name": "alfred_decision", "strict": True, "schema": _decision_schema()}}}
            if config.get("reasoning_effort") is not None:
                payload["reasoning_effort"] = config["reasoning_effort"]
            response = _http_json(url, payload, timeout,
                                  {"Authorization": "Bearer " + api_key})
            choices = response.get("choices") if isinstance(response, dict) else None
            choice = choices[0] if isinstance(choices, list) and choices else None
            message = choice.get("message") if isinstance(choice, dict) else None
            content = message.get("content") if isinstance(message, dict) else None
            truncated = choice.get("finish_reason") == "length" if isinstance(choice, dict) else False
            usage = response.get("usage") if isinstance(response, dict) else None
            tokens = ((usage.get("prompt_tokens", 0) or 0) + (usage.get("completion_tokens", 0) or 0)
                      if isinstance(usage, dict) else "unknown")
        decision = _parse_decision(content, truncated=truncated)
    print(f"model provider={provider} model={model} elapsed={time.monotonic() - started:.1f}s tokens={tokens}")
    return decision


PROBE_SYSTEM = "Return only a JSON object."
PROBE_PROMPT = 'Return {"reply":"ok","skills":[],"buildings":[]}.'
PROBE_TIMEOUT = 30
_PROBE = None  # ((provider, model), ok, message) — one live call per helper start, again on Save


def _redact_probe_text(text):
    if not isinstance(text, str):
        return str(text)
    key = _stored_api_key()
    if key:
        text = text.replace(key, "[redacted]")
    for env_name in ("GEMINI_API_KEY", "OPENAI_API_KEY"):
        val = os.environ.get(env_name)
        if val:
            text = text.replace(val, "[redacted]")
    return text


def _probe_message(exc):
    text = _redact_probe_text(str(exc))
    if "HTTP 401" in text or "HTTP 403" in text:
        return "The model rejected the API key. Check it and Save again."
    if "HTTP 404" in text:
        return "That model is not available. Check the model name and Save again."
    if "HTTP 429" in text:
        return "The model is rate-limited. Wait a minute and Save again."
    lowered = text.lower()
    if "timed out" in lowered or "timeoutexpired" in lowered or "timeout" in lowered:
        return "The model did not answer in time. Check the connection and Save again."
    if "API key is unavailable" in text:
        return "Set your model and API key, then Save."
    return text or "The model test failed. Check the connection and Save again."


def probe_model():
    """One tiny request through the same path as a real decision. Issues no commands."""
    try:
        config = load_model_config()
    except ValueError as e:
        return False, str(e)
    if config["provider"] == "claude":
        return False, "Set your model and API key, then Save."
    try:
        decision = request_decision(PROBE_SYSTEM, PROBE_PROMPT, timeout_seconds=PROBE_TIMEOUT)
    except Exception as e:
        return False, _probe_message(e)
    if not isinstance(decision, dict) or "reply" not in decision:
        return False, "The model replied, but not in the shape Alfred needs."
    return True, "model check passed"


def test_model():
    """Run and cache the same harmless probe used by readiness checks and the dashboard."""
    global _PROBE
    try:
        config = load_model_config()
    except ValueError as e:
        return False, str(e)
    if config["provider"] == "claude":
        return False, "Set your model and API key, then Save."
    ok, message = probe_model()
    _PROBE = ((config["provider"], config["model"]), ok, message)
    return ok, message


def forget_api_key():
    """Forget only the saved secret; provider choice and campaign data stay intact."""
    global _PROBE
    try:
        os.remove(_credentials_path())
    except FileNotFoundError:
        pass
    _PROBE = None


def save_setup(payload):
    """Write setup, then always run the test prompt (forget clears the cache and does not call)."""
    global _PROBE
    apply_setup_request(payload)
    if payload == {"action": "forget"}:
        _PROBE = None
        return
    test_model()


def ready_status():
    """Game folders, this companion's build, then a real model test. Ready means the test passed."""
    global _PROBE
    ok, message = check_setup()
    if not ok:
        return False, message
    try:
        expected = _expected_build_id()
        if expected is not None and expected != game_build_id_live():
            return False, _build_mismatch_message(expected)
    except SetupError as e:
        return False, str(e)
    try:
        config = load_model_config()
    except ValueError as e:
        return False, str(e)
    if config["provider"] == "claude":
        return False, "Set your model and API key, then Save."
    key = (config["provider"], config["model"])
    if _PROBE is None or _PROBE[0] != key:
        ok_p, msg_p = probe_model()
        _PROBE = (key, ok_p, msg_p)
    else:
        ok_p, msg_p = _PROBE[1], _PROBE[2]
    if not ok_p:
        return False, msg_p
    return True, "Alfred is ready. Start Warhammer III."

# skill display names, e.g. wh2_main_skill_hef_tyrion_army_buff_3 -> "Combined Elites".
# .cache is gitignored/per-machine extraction output — best-effort garnish for the prompt
# so the LLM sees names, not bare keys; rules.json (not this) is the durable rulebook.
SKILL_LOC_TSV = os.path.join(ROOT, ".cache", "loc", "text", "db", "character_skills__.loc.tsv")
SKILL_NAME_PREFIX = "character_skills_localised_name_"


def load_skill_names():
    """skill_key -> display name. Fails soft: missing/unreadable/malformed (non-UTF-8)
    file -> {} (options then show by bare key, still fully functional). Runs at import,
    so it must never raise — a bad loc TSV must never kill the daemon."""
    names = {}
    try:
        with open(SKILL_LOC_TSV, encoding="utf-8", errors="replace") as f:
            for line in f:
                parts = line.rstrip("\n").split("\t")
                if len(parts) >= 2 and parts[0].startswith(SKILL_NAME_PREFIX):
                    names[parts[0][len(SKILL_NAME_PREFIX):]] = parts[1]
    except Exception:
        pass
    return names


SKILL_NAMES = load_skill_names()


_SKILL_FX = {}
_CAI = {}


def _cai_score(building_key, rules=None, have=0):
    """CA's own priority for this chain, DECAYED by how many already stand.

    The rulebook stores CA's pair [score_start, score_end] and this returned v[0] — the
    first-one score — always. That threw away the mechanism that makes CA's own AI build a
    BALANCED province instead of eight workshops: cai_construction_system_building_values
    carries per-province and per-faction LIMITS with discounts past them, and the score is a
    RANGE, not a number.

    Owner, 2026-09-01: "im sure CAI takes control and growth into account for buildings". He
    was right and this was the defect. MEASURED on the SFO rulebook, [first, saturated]:
        military 1500 -> 1100   (holds 73%)
        control  500  ->  250   (holds 50%)
        income   1000 ->  200   (holds 20%)
        growth   125  ->   20   (holds 16%)
    So in a province that already has two income buildings CA values the next CONTROL building
    (250) ABOVE the next income one (200) — and reading only the start score hid that
    completely, which is why control never won a ranking race no matter how much of it he
    already had. Income looked permanently twice as good as control.

    `have` is how many of this chain already stand in the province being ranked for. Linear
    interpolation over the first three, which is CA's own limit band shape rather than a curve
    invented here; past three the saturated score is used.

    GROWTH IS THE ONE PLACE WE DO NOT FOLLOW CA (see SYSTEM): 125 decaying to 20 is genuinely
    CA's opinion, and it is wrong for a human player because settlement levels gate 4099
    building levels and open up to 7 slots. That correction lives in the prompt, where it can
    be reasoned about, not smuggled into this function as a number with nothing behind it.
    """
    vals = _CAI.get("building_values") or {}
    # taught scores are consulted AFTER CA's own, never instead of them — but they must be
    # consulted even when CA's table is empty, which the first version got wrong by bailing
    # out on `vals` alone
    taught = _CAI.get("taught_values") or {}
    if not vals and not taught:
        return 0
    v = vals.get(building_key) or taught.get(building_key)
    if v is None and rules is not None:
        ch = (rules["buildings"]["levels"].get(building_key) or {}).get("chain")
        v = vals.get(ch) or taught.get(ch)
    if v is None and _RULES_CACHE:
        ch = (_RULES_CACHE["buildings"]["levels"].get(building_key) or {}).get("chain")
        v = vals.get(ch) or taught.get(ch)
    if not v:
        return 0
    start = v[0]
    end = v[1] if len(v) > 1 and v[1] is not None else start
    if not have:
        return start
    n = min(int(have), 3)
    return start + (end - start) * (n / 3.0)


_RULES_CACHE = None

# CA's own two constants for this, from cai_variables with their own descriptions:
#   CAI_VARIABLE_CONSTRUCTION_SYSTEM_VALUE_FOR_MONEY_CONSIDERATION_TARGET = 1000
#   CAI_VARIABLE_CNSTR_PRIORITY_OVER_COST_MODIFICATION                    = 0.1
CAI_VFM_TARGET = 1000
CAI_COST_WEIGHT = 0.1


def _cai_adjusted(building_key, cost, have=0):
    """CA's priority, adjusted for value for money the way their own AI does.

    Raw priority alone produced a monoculture: eight 6000g special colonies all scoring
    3000, crowding out everything affordable — checked, not assumed. Their value-for-money
    constants fix exactly that, treating 1000g as par and weighting cost at 0.1."""
    p = _cai_score(building_key, have=have)
    if not p or not cost:
        return p
    return p * (CAI_VFM_TARGET / float(cost)) ** CAI_COST_WEIGHT


def _cai_building(u, rules):
    """CA's own priority for this building chain, as a compact hint.

    Their campaign AI ships a start score per chain that decays as you build more of it
    (cai_construction_system_building_values, 2109 rows). It is the game designers' own
    ranking, which is a better prior than anything I could invent — and I previously told
    the owner this layer could not live in files. It could; CA had already written it down.
    Advisory only: it never changes what is legal."""
    # one lookup, shared with the ranking: this had a SECOND copy that consulted only CA's
    # own table, so a taught score ranked a building but then rendered as nothing
    score = _cai_score(u["building"], rules)
    if not score:
        return ""
    vals = _CAI.get("building_values") or {}
    lvl = rules["buildings"]["levels"].get(u["building"]) or {}
    is_ca = u["building"] in vals or lvl.get("chain") in vals
    v = [int(score)]
    # "est." when the number was derived rather than written by CA — Alfred should weigh a
    # 50%-accurate estimate slightly less than a designer's own figure
    return f", CA-priority {v[0]}" if is_ca else f", est. priority {v[0]}"


OPENING_PLAN = os.path.join(ROOT, "data", "opening_plan.json")


SANDBOX = os.path.join(ROOT, "tools", "sandbox.py")


def refresh_opening_plan(turns=40):
    """Recompute the build plan for the campaign as it stands now.

    Subprocess rather than an import: sandbox.py imports the daemon, so importing it back
    would be circular, and a crash in the planner must never take the daemon with it."""
    try:
        r = subprocess.run([sys.executable, SANDBOX, f"--turns={turns}"],
                           capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            print(f"  (plan not refreshed: {(r.stderr or '').strip()[-160:]})")
    except Exception as e:
        print(f"  (plan not refreshed: {e})")


def _opening_plan(state):
    """The sandbox's build order, but ONLY if it was computed for this campaign.

    A plan for another faction or another mod set is worse than no plan, so both are
    checked rather than assumed. Fails soft to None."""
    try:
        with open(OPENING_PLAN) as f:
            p = json.load(f)
    except Exception:
        return None
    if p.get("faction") != (state or {}).get("faction"):
        return None
    try:
        if p.get("profile") != active_profile():
            return None
    except Exception:
        # active_profile now RAISES rather than guessing vanilla. This function's contract is
        # fail-soft, and "cannot tell which rulebook this is" is a good reason to distrust a
        # cached plan — but not a reason to take down the caller. The fail-CLOSED stop lives
        # in load_rules, where the choice of rulebook actually spends money.
        return None
    # A PLAN IS ONLY AS GOOD AS THE RULEBOOK IT WAS SEARCHED AGAINST. The profile can stay
    # the same while the legal move set changes underneath it — MEASURED 2026-09-01: after
    # the landmark and resource-slot filters landed, a plan computed twenty minutes earlier
    # was still recommending the Gold Mining Pit twice, so the prompt's own suggested order
    # named a building its option list no longer offered. The prompt contradicted itself.
    # Mtime rather than a stored fingerprint: the rulebook file IS the input, so "older than
    # its input" is exactly the staleness condition, and it needs no new field to drift.
    try:
        if os.path.getmtime(OPENING_PLAN) < os.path.getmtime(rules_path(active_profile())):
            return None
    except (OSError, RuntimeError):
        return None
    return p if p.get("plan") else None


def _skill_verdict(key):
    """CA tags skills USEFUL_ALWAYS or USELESS_ALWAYS. Surfacing the useless ones stops
    Alfred spending a real, irreversible skill point on something the game's own designers
    marked as worthless. Shown, not filtered — the enumerated set stays complete, so a bad
    tag costs a worse suggestion and never an unavailable option."""
    h = (_CAI.get("skill_hints") or {}).get(key, "")
    return " [CA: USELESS]" if "USELESS" in h else ""


def _skill_label(key, is_levelup=False, brief=False):
    """`brief` drops the effect text. Used only for a level-up whose skill is ALREADY spelled
    out on the character's `taken:` line directly above — MEASURED 2026-09-01, on a lord with
    five multi-level skills taken those repeats were 318 of the prompt's 1804 tokens, 18%,
    and prompt bloat costs option adherence (the reason the new-build list was deduped).
    Never applied to a skill the taken line truncated away: that line caps at 20 and the
    effects would then appear nowhere at all."""
    # em-dash, not parens: parens invite the model to echo "key (Name)" as the key
    label = f"{key} — {SKILL_NAMES[key]}" if key in SKILL_NAMES else key
    if brief:
        return label + (" (level-up)" if is_levelup else "")
    # What the skill DOES. Found 2026-09-01: a skill record carried only {tier, visible}, so
    # Alfred had been choosing skills BY NAME for half of what this mod does — the same
    # defect B3 fixed for buildings, never fixed here. Rendered in the game's own wording.
    fx = _SKILL_FX.get(key)
    if fx:
        parts = []
        for bit in fx.split(";")[:PROMPT_FX_MAX]:
            k, _, v = bit.rpartition(":")
            if not k or not _has_label(k):
                continue
            lbl = _label(k)
            parts.append(lbl.replace("{v}", v) if "{v}" in lbl else f"{lbl} {v}")
        if parts:
            label += " [" + ", ".join(parts) + "]"
    label += _skill_verdict(key)
    return label + " (level-up)" if is_levelup else label


def _rank_tag(rules, skill_key):
    """R1: advisory-only "(rank N+)" tag for a skill that carries a DB unlocked_at_rank gate
    (non-default = gate > 0). Never filters — see legal_skills for why."""
    gate = rules["skills"].get("rank_gate", {}).get(skill_key, 0)
    return f" (rank {gate}+)" if gate else ""


SYSTEM = """You are a competent, conservative campaign administrator for a Total War: \
Warhammer 3 player. You handle leftover admin only: unspent skill points and settlement \
building upgrades/new construction. Standard meta builds; never spend below the treasury \
reserve floor; \
when a building is bought FOR ITS INCOME, a payback longer than the campaign is a \
loss rather than a slow win. The campaign horizon to judge against is ABOUT 40 TURNS from \
now unless the player says otherwise — that number lived only in the planner, so the rule \
was stated without the yardstick it needs. Treat "pays back in 200 turns" as "do not buy", however \
affordable it looks, and say you are holding the money instead. That test applies ONLY \
to income: a building with no gold figure is bought for what it unlocks or defends, and \
is never refused for failing a test it was not taking. \
Prefer economy early because it compounds, and note CA prices a RECRUITMENT building equal \
to an income one at the same cost — do not treat military as a luxury. \
GROWTH AND CONTROL ARE NOT SIDE EFFECTS, they are what the rest of the province is built on. \
Growth accumulates into SETTLEMENT LEVELS, and 4099 building levels in this game are locked \
behind a minimum settlement level, while levels 4 and 5 open 5 and 7 more building slots. So \
a growth building buys BUILDING ACCESS AND SLOTS, not a little income — it raises the ceiling \
on every other building you will ever place there. It also carries army replenishment, so it \
sustains armies as well. Control keeps the province functioning at all: below zero it rebels \
and you can lose the settlement outright. \
CA'S OWN AI UNDERPRICES BOTH and you should not copy it: its valuation is what a building \
yields THIS TURN, which cannot see a compounding effect at all. Judge growth and control by \
what they unlock over the next 20-40 turns, not by their gold this turn. \
On skills, CA weights deepening an existing line at ~100:1 against \
starting a new one, so finish a line before opening another. CA's own AI weights control \
asymmetrically and so should you: +10 per \
point gained up to +2, but -50 per point that drops a province below zero, so never take a \
build that pushes control negative. Choose ONLY from the enumerated options given — never invent keys. \
When you refer to a faction, region, province, building or lord IN WORDS, use the display \
name the player sees on screen, never an internal key — the internal key exists only for \
commands, which you emit separately. \
CA'S OWN NUMBERS (its AI/advisor thresholds; advisory only, not orders): public order warns \
below -10 (act there, not later), calls revolt imminent below -70; pooled per PROVINCE not per region. Treasury < \
4000 warns, < 1000 while losing money is near-bankruptcy, <= 0 is bankruptcy; income bands \
2000/3000/6000 (low/moderate/substantial). Upkeep above 85% of income while losing money is \
CA's own overextension line, target back under 80. CA's own bar for a second army: one \
mobile army AND treasury >= 15000 AND not losing money AND >= 3 enemy mobile armies. Skills: \
CA weights deepening an existing line ~100:1 over opening a new one. Sieges cost the \
besieger too: 10%/turn attrition on both sides. Stances: Land Raid +200 general xp but -100 \
regional GDP/-5 growth/-5 PO; Set Camp +75 ambush defence, foreign replenishment; Forced \
March cannot recruit, -25 ambush defence; Channelling +15 Winds of Magic is HIGH-ELF-ONLY. \
High Elf: exempt from the confederation cooldown every other race pays; confederation needs \
Favour tier 5, +1 per confederation already done; Tyrion's Valiant Imperatives cost 40-150 \
Influence, no gold, 3-10 turn cooldowns; the four base rites are free once event-unlocked but share a 5-turn \
faction-wide cooldown on top of their own. CA prices all 19 strategic resources identically \
-- never claim gold outranks another resource because CA says so. \
Reply with ONLY a JSON object: {"skills": [{"cqi": <int>, "skill": "<key>", "why": "<8 \
words max>"}], "buildings": [{"region": "<key>", "slot_idx": <int>, "building": "<key>", \
"cost": <int>, "why": "<8 words max>"}]}. Empty lists are fine. No other text."""


# B13 (owner ruling 2026-08-31: "installed sfo overhaul ... the mod should work for vanilla
# and sfo"). Which extracted rulebook this campaign is played on. An overhaul rewrites the
# tables Alfred reads — MEASURED for SFO Grimhammer: 4233 vanilla building levels changed,
# 520 of them in COST, and 23 of those are High Elf buildings he can actually build. That
# matters more than missing options: buildings go through the game's OWN queue, so the game
# charges its price while validate() checked ours. A 5000g gate that really costs 10000
# would take him straight through the reserve floor while Alfred reported it was safe.
PROFILE_FILE = os.path.join(data_dir(ROOT), "profile")


EXTRACT_DB = os.path.join(ROOT, "tools", "extract_db.py")


def _extractor():
    """The extractor module, imported rather than duplicated — pack discovery and profile
    naming must agree between the tool that BUILDS a rulebook and the daemon that PICKS
    one, and two copies of that rule would eventually disagree."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("aigen_extract", EXTRACT_DB)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def active_profile():
    """The ruleset this campaign is played on.

    AUTO-DETECTED from the packs actually on disk (owner: "smaller incremental mods that
    are added in should be read by the llm and understood" — installing a mod has to change
    what Alfred knows without anyone editing a config). .cache/profile overrides it when
    present, which is the escape hatch for the one case detection cannot see: a mod that is
    downloaded but not enabled in the launcher. Nothing shipped tells us that — no CA script
    enumerates loaded packs (833 searched, 2026-08-31) and this Feral build's modified.log
    is stale (mtime June), so it is not evidence about tonight.

    A Workshop companion zip is Vanilla-locked: it must not auto-detect SFO from whatever
    else happens to sit in the game data directory."""
    man = companion_manifest(ROOT)
    if man is not None:
        if man.get("preset") != "vanilla":
            raise RuntimeError("companion preset is not vanilla — refusing to start")
        return "vanilla"
    try:
        with open(PROFILE_FILE) as f:
            name = f.read().strip()
        if name:
            return name
    except OSError:
        pass
    try:
        ex = _extractor()
        return ex.profile_name(ex.discover_packs())
    except Exception as e:
        # FAIL CLOSED, not to vanilla. This used to return "vanilla" on any detection error,
        # and load_rules() would then happily load vanilla's rulebook — MEASURED 2026-09-01 by
        # injecting a RuntimeError: it printed "ruleset: vanilla (db.pack) — 5259 building
        # levels" and carried on. 520 buildings differ in COST between vanilla and SFO on this
        # machine (8000 -> 12500 among them), and buildings are issued into the game's OWN
        # queue, so the game charges SFO's price while validate() checked vanilla's. That takes
        # the owner through his reserve floor while the log says he is safe. load_rules' own
        # comment calls exactly this "the worst outcome available" — but its fail-closed guard
        # sat one function DOWNSTREAM of this fail-open, so it never fired.
        #
        # "vanilla" is still returned when detection SUCCEEDS and finds only the game's own
        # pack; that is a real answer. What is refused here is GUESSING vanilla after failing
        # to look.
        raise RuntimeError(
            f"pack detection failed, so which rulebook this campaign needs is unknown: {e}. "
            f"Refusing to guess vanilla — if SFO is installed, its prices are up to 2x "
            f"vanilla's and Alfred would spend against the wrong numbers. Fix the detection "
            f"or write the profile name into .cache/profile.") from e


def rules_path(profile):
    return RULES if profile == "vanilla" else os.path.join(
        ROOT, "data", f"rules_{profile}.json")


def purpose_labels(profile=None):
    """Reviewed Vanilla labels, bound to the exact loaded-rulebook input.

    A missing, malformed, changed, or non-Vanilla registry is deliberately indistinguishable
    from no labels.  The model never derives a purpose from effect text or a building name.
    """
    profile = profile if profile is not None else _ACTIVE_PROFILE
    if profile != "vanilla":
        return None
    try:
        with open(PURPOSE_LABELS, "rb") as f:
            raw = f.read()
        registry = json.loads(raw)
        with open(rules_path(profile), "rb") as f:
            rulebook = f.read()
    except (OSError, ValueError):
        return None
    coverage = registry.get("coverage") if isinstance(registry, dict) else None
    if not isinstance(registry, dict) or registry.get("schema_version") != 1 \
       or registry.get("rulebook_sha256") != hashlib.sha256(rulebook).hexdigest() \
       or not isinstance(coverage, dict) \
       or coverage.get("review_status") != "reviewed_offline_independent_approved":
        return None
    chains = registry.get("chains")
    if not isinstance(chains, dict):
        return None
    for label in chains.values():
        if not isinstance(label, dict) or label.get("purpose") not in STORED_PURPOSES \
           or not label.get("evidence"):
            return None
    return chains


def recruitment_policy(profile=None):
    """The reviewed, small recruitment vocabulary; source unit identity remains in rules."""
    profile = profile if profile is not None else _ACTIVE_PROFILE
    if profile != "vanilla":
        return None
    try:
        with open(PURPOSE_LABELS) as f:
            registry = json.load(f)
    except (OSError, ValueError):
        return None
    aliases, roles = registry.get("recruitment_unit_aliases"), registry.get("recruitment_role_map")
    if aliases is None:
        aliases = {}
    if roles is None:
        roles = {}
    if not isinstance(aliases, dict) or not isinstance(roles, dict) or not all(
            isinstance(name, str) and isinstance(keys, list)
            and all(isinstance(key, str) for key in keys) for name, keys in aliases.items()):
        return None
    if not all(isinstance(role, str) and isinstance(matches, list) and matches and all(
            isinstance(match, dict) and set(match).issubset({"class", "category"})
            and bool(match) and all(isinstance(value, str) for value in match.values())
            for match in matches) for role, matches in roles.items()):
        return None
    return {"unit_aliases": aliases, "role_map": roles}


def _purpose_request(purpose):
    """Return the small immutable request shape, rejecting malformed caller input."""
    if isinstance(purpose, str):
        return {"purpose": purpose} if purpose in PURPOSES else None
    if not isinstance(purpose, dict) or not isinstance(purpose.get("purpose"), str):
        return None
    if purpose["purpose"] not in PURPOSES:
        return None
    request = {"purpose": purpose["purpose"]}
    target = purpose.get("target")
    if target is not None:
        if not isinstance(target, dict) or set(target) != {"kind", "value"} \
           or target.get("kind") not in {"unit", "role"} or not isinstance(target.get("value"), str):
            return None
        request["target"] = {"kind": target["kind"], "value": target["value"]}
    if request["purpose"] != "recruitment" and len(request) != 1:
        return None
    if request["purpose"] == "recruitment" and "target" not in request:
        return None
    return request


def _request_label(request):
    request = _purpose_request(request) or {}
    target = request.get("target") or {}
    return (target.get("value") or request.get("purpose") or "building").replace("_", " ")


def _unit_name(value):
    return re.sub(r"[^a-z0-9]+", " ", (value or "").lower()).strip()


def _level_units(rules, building):
    units = ((rules.get("units") or {}).get("by_building") or {}).get(building)
    stats = ((rules.get("units") or {}).get("stats") or {})
    if not isinstance(units, list) or not isinstance(stats, dict):
        return ()
    return tuple((key, stats.get(key)) for key in units if isinstance(key, str)
                 and isinstance(stats.get(key), dict))


def _matches_role(stats, matches):
    return any(all(stats.get(field) == value for field, value in match.items()) for match in matches)


def _matches_role_for_units(units, matches):
    return isinstance(matches, list) and any(_matches_role(stats, matches) for _key, stats in units)


def purpose_options(rules, options, purpose, profile=None):
    """The one immutable legal subset for an exact reviewed request, or None if unbound."""
    labels, policy = purpose_labels(profile), recruitment_policy(profile)
    request = _purpose_request(purpose)
    if labels is None or request is None:
        return None
    selected = []
    for option in options:
        label = labels.get(_chain_of(option["building"], rules)) or {}
        if label.get("purpose") != request["purpose"]:
            continue
        if request["purpose"] == "recruitment":
            if policy is None:
                continue
            target = request["target"]
            units = _level_units(rules, option["building"])
            if target["kind"] == "unit":
                value = _unit_name(target["value"])
                aliases = {_unit_name(name): keys for name, keys in policy["unit_aliases"].items()}
                if not any(key == target["value"] or _unit_name(stats.get("name")) == value
                           or key in aliases.get(value, ()) for key, stats in units):
                    continue
            elif not _matches_role_for_units(units, policy["role_map"].get(target["value"])):
                continue
        selected.append(option)
    return tuple(selected)


def load_rules():
    try:
        profile = active_profile()
    except RuntimeError as e:
        # Same fail-closed reasoning as the rulebook-missing exit below: running on the wrong
        # prices is worse than not running. A clean message, not a traceback.
        # A4: SetupError (not sys.exit/SystemExit) so background mode (_run_companion)
        # can catch it and wait, instead of the whole companion dying silently.
        raise SetupError(str(e))
    path = rules_path(profile)
    if not os.path.exists(path) and companion_manifest(ROOT) is not None:
        raise SetupError(f"companion is missing its Vanilla rulebook ({os.path.basename(path)})")
    if not os.path.exists(path):
        # Build it. A new pack set means a rulebook that has never been extracted, and
        # making the owner run a tool by hand is exactly the per-campaign config this
        # project exists to remove. ~5s.
        print(f"ruleset {profile!r} has no rulebook yet — extracting it now")
        r = subprocess.run([sys.executable, EXTRACT_DB, f"--profile={profile}", "--fresh"],
                           capture_output=True, text=True)
        print("  " + (r.stdout.strip().replace("\n", "\n  ") or r.stderr.strip()[-400:]))
    if not os.path.exists(path):
        # FAIL CLOSED. Falling back to vanilla here would be the worst outcome available:
        # Alfred would run confidently on prices that are wrong by up to 2x, and under an
        # overhaul that takes the owner through his own reserve floor without saying so.
        raise SetupError(f"profile {profile!r} selected but {os.path.basename(path)} could not be "
                          f"built — run: tools/extract_db.py --profile={profile} --fresh")
    r = json.load(open(path))
    global _EFFECT_LABELS
    _EFFECT_LABELS = r.get("effect_labels") or {}
    global _SKILL_FX
    _SKILL_FX = (r.get("skills") or {}).get("fx") or {}
    global _ACTIVE_PROFILE, _CAI, _RULES_CACHE
    _ACTIVE_PROFILE = profile
    _CAI = r.get("cai") or {}
    _RULES_CACHE = r
    print(f"ruleset: {profile} ({r.get('source', 'unknown')}) — "
          f"{len(r['buildings']['levels'])} building levels")
    return r


def _lua_safe(s):
    """Model-authored strings (e.g. "why") get interpolated RAW into Lua source in the
    exec-channel code builder below (skill_cmds_as_exec) — not passed through lua_quote,
    which only escapes for a proper Lua string literal. A stray quote there breaks
    loadstring. Strip to a safe subset (no quotes at all) and cap length; used on every
    model-authored string embedded directly into generated Lua code."""
    # "/" is on the allowlist because Alfred writes "200 gold/turn" and stripping it left
    # "200 goldturn" in his log. It cannot break a Lua string literal and it cannot start
    # one: the characters that matter here are quotes, backslashes and newlines, and those
    # stay out. Anything not listed is dropped rather than escaped — an allowlist, so a
    # character nobody thought about is refused by default.
    return re.sub(r"[^A-Za-z0-9 _.,;:%+/-]", "", str(s))[:60]


def lua_quote(s):
    return ('"' + str(s).replace("\\", "\\\\").replace('"', '\\"')
            .replace("\n", "\\n") + '"')  # raw newline in a Lua string = syntax error


def write_candidates(rules, subtypes):
    """Per-subtype skill keys the mod probes has_skill against."""
    sk = rules["skills"]
    lines = ["return {"]
    for st in sorted(subtypes):
        set_key = sk["sets_by_subtype"].get(st)
        if not set_key:
            continue
        keys = []
        for item in sk["set_items"].get(set_key, []):
            node = sk["nodes"].get(item)
            if node and node["skill"]:
                keys.append(node["skill"])
        lines.append(f"  [{lua_quote(st)}] = {{")
        lines.extend(f"    {lua_quote(k)}," for k in sorted(set(keys)))
        lines.append("  },")
    lines.append("}")
    inplace_write(CANDIDATES, "\n".join(lines))


def inplace_write(path, content):
    """Rewrite content WITHOUT creating or renaming — the game must only ever see
    files that existed at its boot (Feral negative-lookup cache, measured 08-27).
    Half-read risk on the Lua side is self-healing: a torn commands file fails
    loadfile and is retried next tick."""
    with open(path, "r+" if os.path.exists(path) else "w") as f:
        f.truncate(0)
        f.write(content)


def ensure_exchange_files():
    """Run while the game is CLOSED (or before boot): pre-create every file the
    game-side mod will ever read, so they exist at boot and stay poll-visible."""
    # aigen_dev.lua is on this list even though nothing writes it. It is the DEV HOOK — the
    # file the mod loadfile()s on every load so a mod-side change can be tried with a
    # quicksave-reload instead of a relaunch — and it is the gate this repo requires before
    # any mod change is packed. loadfile returns nil when it is absent, so its absence is
    # safe; but under the Feral negative-lookup cache a file that did not exist at BOOT stays
    # invisible for the whole session, so creating it later does not help. MEASURED
    # 2026-09-01: it was missing from the exchange directory, which would have made the gate
    # unavailable at the next launch, exactly when three unpacked mod changes need it.
    # An empty (comment-only) file loads to a chunk that does nothing.
    defaults = {TRIGGER: "init", COMMANDS: "return {}", CANDIDATES: "return {}",
                SWEEP_REQUEST: "init", CHAT_REQUEST: "init", CHAT_REPLY: "init",
                SETUP_REQUEST: "init",
                DEV_HOOK: "-- dev hook: edit me and quicksave-reload to run Lua in the "
                          "live game.\n-- Empty on purpose; an empty chunk does nothing.\n"}
    for path, content in defaults.items():
        if not os.path.exists(path):
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            fd = os.open(path, flags, 0o600 if path == SETUP_REQUEST else 0o644)
            with os.fdopen(fd, "w") as f:
                f.write(content)
            print(f"  pre-created {os.path.basename(path)}")
        if path == SETUP_REQUEST:
            os.chmod(path, 0o600)


def legal_skills(rules, char):
    """Enumerate legal next skills for one character. Returns [(key, tier, is_levelup), ...]
    — untaken options first (tier-sorted), then already-taken skills eligible for a
    level-up (skills are multi-level; measured 08-27, Tyrion's Combined Elites went
    2->3 on a taken skill). The mod's native add_skill self-refuses at max level, so
    over-offering a level-up that's actually maxed is safe. MAX_SKILL_OPTIONS caps the
    combined list, level-ups sorted after untaken options."""
    if not isinstance(char.get("skills"), list):
        return []  # candidates not yet probed
    sk = rules["skills"]
    set_key = sk["sets_by_subtype"].get(char["subtype"])
    if not set_key:
        return []
    taken = set(char["skills"])
    untaken, levelups = [], []
    for item in sk["set_items"].get(set_key, []):
        node = sk["nodes"].get(item)
        if not node or not node["skill"]:
            continue
        if not node.get("visible", True):
            continue
        # R1: unlocked_at_rank is NOT a filter here (Synthesis §2: read-apis.md documents
        # character:rank() as 1-6, the DB gates go up to 21 — a live read is owed before
        # trusting either scale). A wrong blocking filter costs the owner every deep skill;
        # it is rendered as an advisory "(rank N+)" tag on the option line instead, below.
        is_taken = node["skill"] in taken
        if not is_taken:
            links = sk["parents"].get(item, [])
            # R1 fix, verified by reading 20590 vanilla character_skill_node_links rows:
            # REQUIRED (10470 rows) is a straight AND-prerequisite, SUBSET_REQUIRED (10120
            # rows) is a branch-merge OR-join satisfied once required_num_parents of them are
            # taken. The OLD code lumped every parent into one undifferentiated count
            # (need = required_num_parents or 1) — that already gave SUBSET_REQUIRED its
            # OR-join correctly, but silently UNDER-enforced REQUIRED: 7 vanilla nodes have
            # 2 REQUIRED parents with required_num_parents=0 (the field is only meaningful
            # for SUBSET_REQUIRED), so the old count-based check demanded only 1 of 2
            # mandatory parents. Treating link_type explicitly fixes both directions.
            # A pre-R1 rulebook stores a bare parent key; read it as REQUIRED so a stale
            # data/rules_*.json degrades to the old (stricter-or-equal) behaviour instead of
            # crashing the sweep — the same class of trap as the first-run rulebook gate.
            links = [(l, "REQUIRED") if isinstance(l, str) else tuple(l) for l in links]
            required = [p for p, t in links if t == "REQUIRED"]
            subset = [p for p, t in links if t == "SUBSET_REQUIRED"]
            if any(sk["nodes"].get(p, {}).get("skill") not in taken for p in required):
                continue
            if subset:
                need = node.get("required_num_parents", 0) or 1
                have = sum(1 for p in subset
                           if sk["nodes"].get(p, {}).get("skill") in taken)
                if have < need:
                    continue
        # A SKILL WITH ONE LEVEL CANNOT BE LEVELLED UP. 4611 of 5810 skills cap at one, so
        # offering a level-up for every taken skill was offering a move the game cannot make
        # four times in five — the point spent on nothing, or the command refused. A skill
        # the caps table does not mention stays offerable: no effect rows is not evidence of
        # a one-level skill.
        if is_taken and sk.get("caps", {}).get(node["skill"], 99) < 2:
            continue
        (levelups if is_taken else untaken).append((node["skill"], node["tier"], is_taken))
    # RANK BY CA'S OWN USEFULNESS FIRST, tier second. Sorting by tier alone contradicted the
    # prompt beside it: Alfred is told CA weights a skill tagged useful-always at 100 against
    # a situational one at 1 and a useless one at 0.01, and then handed a list that ignored
    # the tags entirely. MEASURED on a rank-20 Prince: 12 slots held 3 USEFUL and 9 untagged
    # skills while FOUR more USEFUL ones were truncated away — Speed of Asuryan, both
    # Wardings, and Immortality. Tier stays the tie-break, since it encodes progression.
    def _useful_rank(skill_key):
        h = (_CAI.get("skill_hints") or {}).get(skill_key, "")
        if "USELESS" in h:
            return 2
        if "USEFUL" in h:
            return 0
        return 1

    untaken.sort(key=lambda x: (_useful_rank(x[0]), x[1]))
    levelups.sort(key=lambda x: (_useful_rank(x[0]), x[1]))
    # RESERVE ROOM FOR DEEPENING. Concatenating untaken + levelups and truncating meant
    # level-ups were never offered at all: this subtype's tree has 84 skills, so the 12
    # slots were always full of untaken ones before a single level-up was reached
    # (MEASURED 2026-09-01 — a lord with three multi-level skills taken saw zero of them).
    # That contradicted the prompt's own instruction, which tells Alfred that CA weights
    # deepening an existing line about 100:1 against starting a new one and to finish a line
    # before opening another. He was being told to deepen and never shown how.
    # Half the slots each, then whichever side has more tops up the remainder, so neither
    # kind can be starved and a short list is never padded with nothing.
    half = MAX_SKILL_OPTIONS // 2
    keep_lv = levelups[:half]
    keep_un = untaken[:MAX_SKILL_OPTIONS - len(keep_lv)]
    out = keep_lv + keep_un
    if len(out) < MAX_SKILL_OPTIONS:
        out += [x for x in levelups[half:]][:MAX_SKILL_OPTIONS - len(out)]
    return out


_RESOURCE_GATED = None


def resource_gated_chains(rules):
    """Chains that need a matching DEPOSIT, worked out from the permit data rather than named.

    The breadth heuristic (MIN_SLOT_BREADTH) asks how MANY slot templates permit a chain. That
    is the wrong question for resource buildings: a salt mine is permitted by 10 templates, so
    it sails past a threshold of 3 — but all 10 are salt slots. MEASURED 2026-09-01 on the SFO
    rulebook, an Empire level-1 settlement's ENTIRE option list was one salt mine, which the
    game refuses in any region without a salt deposit.

    The real question is what KIND of slot permits it. CA names its generic secondary
    templates `..._secondary_<resource>` (…_salt, …_gems, …_animals) and its one-off
    settlement templates `..._special_…_secondary`. A chain that ordinary slots accept is
    permitted across MANY different resource suffixes — a farm spans 139 templates covering
    animals, gems, medicine and the rest. A deposit-gated chain is permitted only by templates
    carrying ONE suffix, its own.

    MEASURED separation on this rulebook, six races: 0 of 168 ordinary chains have exactly one
    non-empty suffix; 36 of 58 resource chains do (the other 22 have breadth 0 and were
    already excluded). No overlap, so this is a gate and not a judgement call.

    CEILING, stated because it is a heuristic and heuristics rot: this reads CA's template
    NAMING. It is only ever the fallback — when the slot reports its own template, slot_allows
    answers exactly and this is not consulted. A mod that names templates differently loses
    the fallback and falls back in turn to breadth alone, i.e. today's behaviour.
    ponytail: naming-derived; the exact check supersedes it wherever a template is reported.
    """
    global _RESOURCE_GATED
    if _RESOURCE_GATED is not None:
        return _RESOURCE_GATED
    permits = ((rules or {}).get("buildings") or {}).get("slot_permits") or {}
    chain_sets = permits.get("chain_sets") or {}
    template_sets = permits.get("template_sets") or {}
    template_chains = permits.get("template_chains") or {}
    if not chain_sets or not template_sets:
        _RESOURCE_GATED = frozenset()
        return _RESOURCE_GATED
    # reverse index once: set -> templates, so each chain costs a few lookups instead of a
    # scan over every template
    by_set = {}
    for t, sets in template_sets.items():
        for st in sets:
            by_set.setdefault(st, []).append(t)
    out = set()
    for chain, sets in chain_sets.items():
        tmpl = set()
        for st in sets:
            tmpl.update(by_set.get(st, ()))
        tmpl.update(t for t, cs in template_chains.items() if chain in cs)
        sfx = set()
        for t in tmpl:
            if "_special_" in t:      # a one-off settlement slot says nothing about breadth
                continue
            m = re.search(r"_secondary(?:_(.+))?$", t)
            if m:
                sfx.add(m.group(1) or "")
        # build 25509431 added named-settlement slots with the resource as a prefix
        # (…_secondary_salt_al_haikk, …_wine_wurtbad) — same deposit, longer suffix; fold them
        # into the bare resource suffix or salt/wine stop looking single-resource.
        sfx = {s for s in sfx if not any(s != o and o and s.startswith(o + "_") for o in sfx)}
        if len(sfx) == 1 and "" not in sfx:
            out.add(chain)
    _RESOURCE_GATED = frozenset(out)
    return _RESOURCE_GATED


def slot_allows(chain, template, rules):
    """May this chain go in a slot carrying THIS template? None = cannot tell.

    The exact form of the question slot_breadth only approximates. Returns None rather than
    a guess when the slot reports no template (an old pack) or the rulebook has no permits
    index, so the caller keeps the heuristic instead of inventing an answer."""
    permits = ((rules or {}).get("buildings") or {}).get("slot_permits") or {}
    if not template or not permits:
        return None
    if chain in (permits.get("template_chains") or {}).get(template, ()):
        return True
    sets = set((permits.get("chain_sets") or {}).get(chain, ()))
    return bool(sets & set((permits.get("template_sets") or {}).get(template, ())))


def primary_level(region, levels):
    """The level of the region's PRIMARY slot building — the settlement itself.

    3850 of 5259 building levels gate on it (building_levels.primary_slot_building_
    building_level_requirement): the Handmaidens need a level-4 settlement, Hunters tier 3
    needs level 4. Returns None when the dump cannot say — an old pack with no slot "kind",
    or a region whose primary slot is empty — and every caller then declines to filter,
    because offering something the game refuses costs one wasted slot while dropping every
    option costs Alfred his whole answer."""
    for slot in region.get("slots", []) or []:
        if slot.get("kind") != "primary":
            continue
        b = slot.get("building")
        if b and b != "EMPTY" and b in levels:
            return levels[b].get("level")
    return None


def primary_ok(level_rec, prim):
    """Is this building allowed at a settlement of that level? Unknown level = allowed."""
    need = (level_rec or {}).get("primary_req")
    return need is None or prim is None or prim >= need


def dev_ok(level_rec, have):
    """Can the region pay this build's DEVELOPMENT POINT cost as well as its gold?

    739 building levels cost development points — the province growth pool — on top of
    gold, and High Elf settlement upgrades are among them. `have` is None until an installed
    pack dumps region.development_points, and None means allowed: the same fail-open rule as
    the settlement-level gate, because a refused build costs one slot while dropping every
    option costs Alfred his answer."""
    need = (level_rec or {}).get("dev_cost")
    return need is None or have is None or have >= need


def building_upgrades(rules, state):
    """Per owned slot with a building: its next level + cost."""
    levels = rules["buildings"]["levels"]
    # 2026-09-17 faction gate (alfred-20260917-faction-rules-affordability): _building_available
    # was only ever called from building_construction's new-build pool, so a settlement that
    # already carries a faction-locked chain (e.g. Aislinn's Dragonship Colony, captured or
    # inherited) kept offering its NEXT level to any faction with no faction check at all — the
    # race-token heuristic in building_construction never runs here. avail missing entirely
    # (old rulebook) fails OPEN, same convention as primary_ok/dev_ok above.
    avail = rules["buildings"].get("availability") or {}
    faction = state.get("faction")
    culture = state.get("culture")
    subculture = state.get("subculture")
    out = []
    for region in (state.get("regions") if isinstance(state.get("regions"), list) else []):
        if region.get("under_siege") is True:
            continue
        for slot in region["slots"]:
            b = slot["building"]
            if b == "EMPTY" or b not in levels:
                continue
            nxt = levels[b].get("next")
            # the next level can need a bigger settlement than this region has
            if nxt and not (primary_ok(levels.get(nxt), primary_level(region, levels))
                            and dev_ok(levels.get(nxt), region.get("development_points"))
                            and (not avail or _building_available(avail.get(nxt), faction,
                                                                   culture, subculture))):
                continue
            if nxt:
                out.append({"region": region["region"], "slot_idx": slot["idx"],
                            "building": nxt, "cost": levels[nxt]["cost"],
                            "turns": levels[nxt].get("turns", 1), "new": False,
                            "fx": levels[nxt].get("fx", ""),
                            "fx_scope": levels[nxt].get("fx_scope", ""),
                            "name": levels[nxt].get("name", ""),
                            "unlocks": levels[nxt].get("unlocks", 0),
                            "unlock_units": levels[nxt].get("unlock_units", ""),
                            "unlock_cp": levels[nxt].get("unlock_cp", 0),
                            "opens_slots": levels[nxt].get("opens_slots", 0),
                            # AN UPGRADE REPLACES what is there. Carrying the current
                            # building's income lets the prompt show the GAIN rather than
                            # the new building's absolute figure — see _fx_str.
                            "replaces_fx": (levels.get(b) or {}).get("fx", "")})
    return out


def _building_available(cond_list, faction, culture=None, subculture=None):
    """rules.json availability semantics (read + verified against HEF/TK keys):
    a building_key maps to a list of condition dicts; it's available if ANY dict is
    either empty (no restriction) or has "faction" == ours. Dicts also carry
    "culture"/"subculture" keys (2662 + 421 hits across the DB). A single explicit
    qualifier may match the optional native context; ambiguous records remain refused.
    Surprise: [{}] (unrestricted) is common on chains that are clearly faction-flavored
    by name — TK landmarks (wh2_dlc09_special_pyramid_settra, 6000g — harmless, priced
    out of the cheap cap), but also cheap ones (wh2_main_skv_farm_1 "skv" = Skaven,
    wh2_dlc09_tmb_growth_1 "tmb" = Tomb Kings, both 250g): real in-game restriction for
    those is by settlement/region TEMPLATE (which chains a slot's home settlement type
    offers), not by this availability table — rules.json doesn't capture that layer, so
    a HEF empty slot CAN surface a cheap Skaven-flavored option here. Per the owner's
    conservative instruction this is left as-is (not invented culture/template logic);
    flagging for a human decision, not silently patched."""
    cond_list = cond_list or []
    for cond in cond_list:
        if not cond or cond.get("faction") == faction:
            return True
    if len(cond_list) != 1:
        return False
    cond = cond_list[0]
    qualifiers = [key for key in ("culture", "subculture") if cond.get(key)]
    if len(qualifiers) != 1:
        return False
    return cond.get(qualifiers[0]) == {"culture": culture,
                                       "subculture": subculture}[qualifiers[0]]


def _fx_has_positive(fx, tag):
    """F8: `fx` is CA's "key:value;key:value" effects string. A plain substring test
    (`tag in fx`) matches a NEGATIVE row too — e.g. `public_order_base:-2` — so 137
    building levels that HURT growth/control were being counted as growth/control seats
    and offers (wh2_main_def_slaves_1 among them). Only a key that STARTS WITH `tag` and
    carries a value > 0 counts."""
    for part in (fx or "").split(";"):
        k, _, v = part.partition(":")
        k = k.strip()
        if not k.startswith(tag):
            continue
        try:
            if float(v.strip()) > 0:
                return True
        except ValueError:
            continue
    return False


def building_construction(rules, state):
    """Empty slots: cheapest MAX_NEW_BUILD_OPTIONS level-0 chains available to our
    faction (same option set offered at every empty slot — faction doesn't vary)."""
    levels = rules["buildings"]["levels"]
    avail = rules["buildings"]["availability"]
    faction = (state.get("faction") or "")
    parts = faction.split("_")
    if len(parts) < 3:
        return []  # unrecognized faction-key shape: offer nothing rather than
        # skip the race filter, which would reopen the cross-faction leak below
    race = parts[2]   # "wh2_main_hef_eataine" -> "hef"
    # ponytail: race-token substring heuristic (rules.json availability doesn't gate
    # these chains by faction — see _building_available's surprise note) — closes the
    # cross-faction leak (Skaven farm offered to HEF) the owner vetoed. Ceiling: chains
    # that don't carry their race token in the key slip past it either way; real fix
    # needs region/settlement-template junctions extracted into rules.json.
    # A CHAIN THAT ONLY ONE SLOT IN THE GAME ACCEPTS IS NOT A GENERIC BUILD. Every slot
    # carries a slot_template, and slot_template_permitted_building_chains says which chains
    # each template takes; 644 chains are permitted by exactly one template, i.e. they are
    # landmarks and settlement-specific specials that exist in one region and nowhere else.
    # MEASURED 2026-09-01: they rank TOP on CA's own priority, so ranking by priority put
    # eight of them (Shrine of Khaine, Colony of Arnheim, Chamber of the Phoenix Crown...)
    # at the head of every empty-slot list and pushed out farm, court, barracks and worship
    # entirely. Those were dead options crowding out live ones.
    # Threshold 3, and the separation it sits in is wide, not marginal: across
    # hef/dwf/emp/grn/skv/def the ordinary chains run 5-139 permitted templates and no
    # "special" chain exceeds 2.
    # CEILING: this says a chain is buildable SOMEWHERE generic, not that it fits THIS slot.
    # The exact check needs the slot's own template, which the mod cannot report yet — that
    # is the real fix and this is the cheap one. Fails OPEN on an old rulebook with no
    # breadth map, since offering everything is the behaviour that was already shipping.
    breadth = rules["buildings"].get("slot_breadth") or {}

    res_gated = resource_gated_chains(rules)

    port_starts = {v["next"]: k for k, v in levels.items()
                   if "ruin" in k and v.get("next")}

    def _pool(template=None, port=False):
        """New secondary chains or first post-ruin port levels this faction may start.

        `template` = a slot whose own template the dump reported, in which case the EXACT permitted-chain check replaces the breadth
        heuristic — and a chain breadth excluded (a resource building, a landmark) is back in
        play, because in ITS slot it is perfectly legal. That recovery is the whole reason
        the mod reads slot:template_key(); filtering alone could only ever narrow."""
        for k, v in levels.items():
            if (k not in port_starts if port else v["level"] != 0) or \
                    "ruin" in k or f"_{race}_" not in k:
                continue
            # Port level 1 carries its culture requirement while its ruined predecessor
            # carries the faction availability that authorises rebuilding it.
            if not _building_available(avail.get(port_starts[k] if port else k), faction,
                                       state.get("culture"), state.get("subculture")):
                continue
            ch = v.get("chain") or k
            exact = slot_allows(ch, template, rules)
            if port and exact is not True:
                continue
            if exact is None:
                # No template reported, so the heuristic decides. TWO questions, not one:
                # how many templates permit this chain (breadth), and whether they are all
                # slots of a single resource kind (a deposit gate the count cannot see).
                if breadth and breadth.get(ch, 0) < MIN_SLOT_BREADTH:
                    continue
                if ch in res_gated:
                    continue
            elif not exact:
                continue
            yield k, v

    def _chain_of(key):
        return (levels.get(key) or {}).get("chain") or key

    def _rank(kv, have=None):
        # AFFORDABLE FIRST is applied by the caller; within that, CA's own adjusted priority
        # then cost. `have` counts what already stands in the province being ranked for, so
        # CA's own decay applies — the fifth workshop is not worth what the first was.
        n = (have or {}).get(_chain_of(kv[0]), 0)
        return (-_cai_adjusted(kv[0], kv[1]["cost"], n), kv[1]["cost"])

    opts = sorted(
        _pool(),
        # AFFORDABLE FIRST, then by CA's own priority, then by cost.
        #
        # MEASURED 2026-09-01 from CA's documented AI constants:
        #   CAI_VARIABLE_CNSTR_PRIORITY_OVER_COST_MODIFICATION = 0.1
        #   "how much should value for money count versus raw priority
        #    (0.0, not a bit, to 1.0, entirely)"
        # Their AI weights raw priority 90% against value-for-money 10%, so offering the
        # eight CHEAPEST chains was the opposite bias — mine, not the game's — and the best
        # building in a province could simply never appear.
        #
        # But ranking on priority ALONE was worse, and I checked before keeping it: it
        # filled every slot with 6000g special colonies of equal priority and hid every
        # affordable building. CA gets away with pure priority because affordability lives
        # elsewhere in their AI; here it has to live in the same place. So: what he can
        # actually afford, best-first — and if he can afford nothing, the cheapest anyway,
        # so he can still be told what to save for.
        key=_rank)
    budget = state.get("treasury", 0) - max(RESERVE_FLOOR_MIN,
                                            state.get("reserve_floor") or 0)

    _shortlist_cache = {}

    def _family(k):
        # "wh3_dlc27_hef_special_colony_arnheim" -> "wh3_dlc27_hef_special_colony"
        return "_".join(k.split("_")[:5])

    def shortlist(template=None, prim=None, dev=None, have=None, port=False):
        """The offered set for one slot. Cached by template: templates repeat across slots,
        so rebuilding the pool per slot is the same answer at N times the cost.

        template=None keeps the faction-wide pool built above (breadth heuristic). A real
        template rebuilds it with the EXACT permitted-chain check, which both drops chains
        that cannot go there and RESTORES ones the heuristic excluded — a resource building
        or a landmark is perfectly legal in the slot it belongs to.
        """
        # CACHED ON EVERYTHING THAT CHANGES THE ANSWER. It used to key on `template` alone
        # while the settlement-level and development-point gates were applied by the CALLER,
        # AFTER this function had already truncated to the best 8 faction-wide. MEASURED
        # 2026-09-01 on the SFO rulebook: an Empire level-1 settlement saw ONE option and a
        # High Elf one saw its farm and public-order buildings vanish as the player got
        # RICHER — because expensive high-requirement chains became affordable, won the
        # priority race, and were then pruned to nothing. Ranking must choose among what is
        # LEGAL HERE, so the gates move inside.
        ck = (template, prim, dev, tuple(sorted((have or {}).items())), port)
        if ck in _shortlist_cache:
            return _shortlist_cache[ck]
        # Re-sorted with THIS province's counts even when the template is None: the
        # faction-wide `opts` order was computed with have={} and would otherwise ignore
        # everything he has already built here.
        pool = sorted(_pool(template, port) if template else _pool(port=port),
                      key=lambda kv: _rank(kv, have))
        pool = [kv for kv in pool if primary_ok(kv[1], prim) and dev_ok(kv[1], dev)]
        afford = [kv for kv in pool if kv[1]["cost"] <= budget]
        # DIVERSITY CAP. Checked at treasury 9000: without it the list was eight special
        # colonies of one family, all scoring within 8 points of each other — a monoculture
        # is not a choice. At most two per family, so he sees alternatives not synonyms.
        # RESERVED SEATS for growth and control. The decay above fixes their RELATIVE standing
        # as a province fills up, which was a real defect — but it cannot fix their BASE
        # ranking, and CA's base priorities are CA's preferences. MEASURED at a level-3 High
        # Elf settlement with the decay in place: the top eight came back ALL MILITARY, with
        # no growth or control option at all. Owner, 2026-09-01: "growht and control are some
        # of the most improtant mechanics in the game and both increase money and access to
        # better armies".
        #
        # Two measured reasons not to copy CA's ordering here:
        #  - GROWTH: CA scores it 125 against a barracks' 1500, but growth buys SETTLEMENT
        #    LEVELS, and 4099 building levels are gated on settlement level while levels 4-5
        #    open 5 and 7 more slots. CA's number is what a building yields THIS TURN and
        #    cannot see a compounding effect. (Checked whether AI growth cheats explain the
        #    low score — they do not: the only AI growth bonus is hordebuilding_growth_core,
        #    which is for HORDE factions, not settled ones.)
        #  - CONTROL: the HUMAN carries wh_main_effect_public_order_difficulty_level, ranging
        #    -8 to +1 across difficulty settings; the AI carries no equivalent. CA's control
        #    priority is calibrated for something that never faces the penalty the player
        #    does.
        #
        # A SEAT, not a boost. Inventing a multiplier above military would be a number with
        # nothing behind it; one reserved slot each guarantees he is OFFERED the choice, and
        # the prompt carries the reasons to weigh it. Same shape as the diversity cap below,
        # which exists because a monoculture is not a choice either.
        # ADDED, NOT SUBSTITUTED — and that distinction was found by the eval, not by thought.
        # The first version put the reserved options at the FRONT of the pick loop, so they
        # consumed two of the eight slots and EVICTED two better ones. MEASURED: "build the
        # best economy building you can, go" then failed, because the Elven Craftsman Alfred
        # correctly asked for had been pushed off the list and the validator refused it. A
        # guarantee that removes the obvious right answer is not a guarantee, it is a bug.
        # So the normal ranking fills the list first and the seats are appended after, costing
        # two extra prompt lines and no options at all.
        picked, fam = [], {}
        for kv in afford:
            f = _family(kv[0])
            if fam.get(f, 0) >= 2:
                continue
            fam[f] = fam.get(f, 0) + 1
            picked.append(kv)
            if len(picked) >= MAX_NEW_BUILD_OPTIONS:
                break
        if len(picked) < MAX_NEW_BUILD_OPTIONS:
            # top up with the cheapest unaffordable ones so he can still be told what to
            # save toward — a plan needs the thing that is out of reach today
            rest = sorted((kv for kv in pool if kv not in picked),
                          key=lambda kv: kv[1]["cost"])
            picked += rest[:MAX_NEW_BUILD_OPTIONS - len(picked)]
        for tag in ("province_growth", "public_order"):
            if any(_fx_has_positive(kv[1].get("fx"), tag) for kv in picked):
                continue
            for kv in afford:
                if _fx_has_positive(kv[1].get("fx"), tag) and kv not in picked:
                    picked.append(kv)
                    break
        _shortlist_cache[ck] = picked
        return picked

    # Empty ports start at the target after their ruined level, unlike secondary chains,
    # which start at level 0. They need both a known port kind and an exact template match;
    # primary and unknown slots still get no construction offers.
    out = []
    for region in (state.get("regions") if isinstance(state.get("regions"), list) else []):
        if region.get("under_siege") is True:
            continue
        # the shortlist is faction-wide, but whether a chain may START here depends on how
        # big this region's settlement is — so the level gate is applied per region, not
        # once when the shortlist is built
        prim = primary_level(region, levels)
        dev_have = region.get("development_points")
        # WHAT THIS PROVINCE ALREADY HAS, by chain. CA's limits are per-province and
        # per-faction (cai_construction_system_building_values), and the province is the scope
        # that decides whether the next one of a thing is still worth what the first was.
        have_here = {}
        for _r in (state.get("regions") or []):
            if _r.get("province") != region.get("province"):
                continue
            for _s in _r.get("slots") or []:
                b = _s.get("building")
                if b and b != "EMPTY":
                    c = _chain_of(b)
                    have_here[c] = have_here.get(c, 0) + 1
        for slot in region["slots"]:
            kind = slot.get("kind")
            if slot["building"] != "EMPTY" or kind not in ("secondary", "port"):
                continue
            # BOX S: a LOCKED slot (settlement level hasn't unlocked it yet, mod-side
            # region:settlement():active_secondary_slots()) reads as EMPTY but the game
            # refuses any build into it. Refuse it here too, at the single place every
            # empty-slot option is generated, so it never enters `ups`/upgrade_options and
            # therefore never enters validate()'s legal_ups either.
            if slot.get("locked"):
                continue
            for k, v in shortlist(slot.get("template"), prim, dev_have, have_here,
                                  port=kind == "port"):
                out.append({"region": region["region"], "slot_idx": slot["idx"],
                            "building": k, "cost": v["cost"],
                            "turns": v.get("turns", 1), "new": True,
                            "fx": v.get("fx", ""),
                            "fx_scope": v.get("fx_scope", ""),
                            "name": v.get("name", ""),
                            "unlocks": v.get("unlocks", 0),
                            "unlock_units": v.get("unlock_units", ""),
                            "unlock_cp": v.get("unlock_cp", 0),
                            "opens_slots": v.get("opens_slots", 0)})
    return out


def recruit_candidates(rules, state):
    """Box C, daemon half, INERT (see src/lua/aigen_recruit.lua header — no capability row,
    so nothing calls this into a command yet).

    For each army in state["armies"] (shape {"cqi": int, "units": [unit_key,...]} — that
    field arrives from Box B; fail soft to [] if absent, same law as every other reader of
    data a sibling box owns), list the faction's recruitable unit keys (rules["units"]
    ["by_building"], keyed by building — B8's extractor, tools/extract_db.py:863) joined to
    the buildings currently standing, with cost and cp/cost ratio against the RULEBOOK'S OWN
    median ratio.

    ponytail: armies carry no region yet (that's a later box), so "buildings standing" is the
    union of buildings across every region in state instead of per-army locality — upgrade to
    per-army/per-region once armies report one.

    NEVER hardcode the median: docs/research/autoresolve.md measured it moves with the
    installed pack set (0.50 under SFO, ~1.0 vanilla) because SFO detaches cp from cost. It is
    computed here from whatever rulebook is actually loaded.
    """
    import statistics

    armies = state.get("armies") or []
    if not armies:
        return []

    units = rules.get("units", {})
    by_building = units.get("by_building", {})
    stats = units.get("stats", {})

    standing = set()
    for region in state.get("regions") or []:
        for slot in region.get("slots") or []:
            b = slot.get("building")
            if b and b != "EMPTY":
                standing.add(b)

    unit_keys = set()
    for b in standing:
        unit_keys.update(by_building.get(b, []))

    ratios = [u["cp"] / u["cost"] for u in stats.values()
              if u.get("cost") and u.get("cp")]
    median_ratio = statistics.median(ratios) if ratios else 0.0

    out = []
    for army in armies:
        candidates = []
        for unit_key in sorted(unit_keys):
            u = stats.get(unit_key) or {}
            cost = u.get("cost")
            if not cost:
                continue
            ratio = (u.get("cp") or 0) / cost
            candidates.append({"unit": unit_key, "cost": cost, "cp": u.get("cp"),
                                "ratio": ratio, "above_median": ratio >= median_ratio})
        out.append({"cqi": army.get("cqi"), "candidates": candidates,
                    "median_ratio": median_ratio})
    return out


def _disp(u):
    """The name the PLAYER sees, next to the key Alfred must emit.

    MEASURED 2026-09-01 01:2x: he asked Alfred to queue "the plaza". Alfred identified the
    right building from its effects but could not match the word, because the prompt carried
    only wh2_main_hef_order_1 while the game's tooltip says "Plaza". Names are in the loc
    tables and cost almost nothing to carry. The KEY stays first and unquoted so the model
    still copies it verbatim into a command.
    """
    n = u.get("name")
    return f' "{n}"' if n else ""


def _region_name(rules, key):
    """BOX E: the game's own onscreen name for a region key, or the key itself if the
    rulebook carries none (older rulebook, or the key is unknown). No shipped CA Lua idiom
    localises a region key directly (searched .cache/cascripts, none found), so this reads
    the loc table extract_db.py now pulls into rules['locations']['regions']."""
    return ((rules or {}).get("locations") or {}).get("regions", {}).get(key) or key


def _province_name(rules, key):
    """Same as _region_name, for provinces."""
    return ((rules or {}).get("locations") or {}).get("provinces", {}).get(key) or key


def _faction_name(state, key):
    """The player-visible faction name, dumped mod-side as state['faction_names'] (a
    key->name table covering the player faction plus every faction it has met — see
    src/lua/aigen.lua serialize_diplomacy/dump_state). Falls back to the raw key on an
    older pack that never dumped this."""
    if not key:
        return key
    return ((state or {}).get("faction_names") or {}).get(key) or key


def _edict_name(rules, key):
    """BOX D1 (fixed per review 61b7e4c): display name for an active region edict key
    (region:get_active_edict_key(), wh2_dlc11_vampire_coast_loyalty.lua:221 — dumped
    mod-side by Box M1).

    `rules['locations']['edicts']` is real CA loc now (extract_db.py loc_names(
    "provincial_initiative_records_localised_name_", ("provincial_initiative_records__",)))
    — 117 rows, e.g. wh2_main_edict_skv_devious_planning -> "Exploitative Planning", which
    the humaniser below got WRONG ("Devious Planning"). The humaniser survives only as a
    fail-open fallback for a key that dict doesn't carry (older rulebook, or a key it
    misses): strip the "edict_" marker and the short culture-code token that follows it."""
    name = ((rules or {}).get("locations") or {}).get("edicts", {}).get(key)
    if name:
        return name
    tail = key.split("edict_", 1)[-1] if key and "edict_" in key else (key or "")
    parts = [p for p in tail.split("_") if p]
    if len(parts) > 1 and len(parts[0]) <= 4:
        parts = parts[1:]
    return " ".join(p.capitalize() for p in parts) or key


def _corruption_label(rules, ptype):
    """BOX D1 (fixed per review 61b7e4c): CA's pooled-resource type key -> "<god>
    Corruption" for the corruption line (cm:get_highest_corruption_in_region(),
    lib_campaign_manager.lua:9808-9847, dumped by Box M1).

    `rules['locations']['corruption']` is real CA loc now (extract_db.py loc_names(
    "pooled_resources_display_name_", ("pooled_resources__",))) — 7 rows, e.g.
    wh3_main_corruption_khorne -> "Khorne Corruption". The humaniser below survives only as
    a fail-open fallback for a key that dict doesn't carry: strip CA's own
    "pooled_resource_corruption_"/"corruption_" prefix and title-case the remainder."""
    if not isinstance(ptype, str):
        return "Corruption"
    name = ((rules or {}).get("locations") or {}).get("corruption", {}).get(ptype)
    if name:
        return name
    s = ptype
    for pfx in ("pooled_resource_corruption_", "corruption_"):
        if pfx in s:
            s = s.split(pfx, 1)[-1]
            break
    s = s.replace("_", " ").title()
    return f"{s} Corruption" if s else "Corruption"


def _region_tags(rules, region, ups):
    """BOX D1: short non-default tags for one region line — climate suitability, capital,
    siege, active edict, and whether growth is banked for the primary slot's next level.
    Every source field is Box M1's, and every one fails open (list stays empty) when the
    dump predates it — see the module docstring's "old dumps render identically" rule."""
    tags = []
    suit = region.get("suitability")
    if isinstance(suit, str) and suit.startswith("suitability_"):
        lvl = suit[len("suitability_"):]
        if lvl in ("poor", "verypoor"):
            tags.append(f"climate {lvl}")
    if region.get("capital"):
        tags.append("CAPITAL")
    if region.get("under_siege"):
        tags.append("UNDER SIEGE")
    edict = region.get("edict")
    if edict:
        tags.append(f"edict: {_edict_name(rules, edict)}")
    if region.get("can_upgrade_dev"):
        prim_idx = next((s.get("idx") for s in region.get("slots", []) or []
                          if s.get("kind") == "primary"), None)
        if prim_idx is not None and any(
                u["region"] == region["region"] and u["slot_idx"] == prim_idx and not u["new"]
                for u in ups):
            tags.append("dev-points ready")
    return tags


_EFFECT_LABELS = {}


def _label(key):
    """The game's own wording for an effect key, e.g. economy_gdp_culture -> "Income
    generated". Falls back to the raw key: a missing label is less readable, never wrong."""
    return _EFFECT_LABELS.get(key, key)


def _has_label(key):
    """Does this effect have the game's own on-screen wording?

    BOX E audit, 2026-09-01: three effect keys (wh_rite_cost_hidden,
    wh_effect_siege_defending_defence, cap_hef_sea_guard) reached the prompt as bare
    "<key> <value>" — _label()'s fallback. They have no effects_description_ loc entry at
    all, in vanilla or any profile pack, which means the game itself never shows the player
    a name for them either. An effect hidden from the player in-game is hidden from Alfred
    too: this lets callers OMIT the bit rather than print a key the model would echo back as
    if it were meaningful."""
    return key in _EFFECT_LABELS


def _fx_str(u):
    """What a building does, for one option line: top effects, then what it unlocks.

    B8: a military building has NO effect rows — its value is the units it unlocks. Without
    that, barracks reads "cost 3000" next to "farm (cost 1000, +18 growth)" and the model
    deprioritises military with apparent evidence. 210 unit-unlocking buildings have no fx at
    all, so for them the unlock list is the ONLY thing on the line."""
    # Render effects in the GAME'S OWN WORDS, not raw keys. Owner 2026-09-01: Alfred was
    # reasoning over economy_gdp_culture:50 while the player's tooltip says "Income
    # generated: 50". The wording ships in the game's loc tables (17033 of them), so this
    # costs nothing to know and is what he actually sees on screen.
    parts = []
    for bit in (u.get("fx") or "").split(";")[:PROMPT_FX_MAX]:
        if not bit:
            continue
        key, _, val = bit.rpartition(":")
        if not key:
            parts.append(bit)
            continue
        if not _has_label(key):
            # No on-screen wording for this effect anywhere in the game's own loc — it is
            # hidden from the player in-game too, so it is omitted here rather than printed
            # as a bare key the model would treat as meaningful. See _has_label.
            continue
        lbl = _label(key)
        # GDP IS GOLD PER TURN, 1:1. B3 refused to say so — "GDP is an abstract contribution
        # CA aggregates with tax/tech/subculture, so a gold figure would be fabricated" —
        # and that was over-cautious. MEASURED 2026-09-01 against the live campaign:
        # faction_gdp_other 3000 + building GDP 625 + difficulty bonus 1000 = 4625 against a
        # reported 4905. The GDP term enters the total unscaled. "+400 gold/turn" is now a
        # statement of fact, and it is the number a spending decision actually turns on.
        if key.startswith("economy_gdp") and "_mod_" not in key and lbl.startswith("Income"):
            try:
                shown = float(val)
                if u.get("replaces_fx"):
                    # show the GAIN over what is being replaced, not the absolute
                    prev = 0.0
                    for pb in (u["replaces_fx"] or "").split(";"):
                        pk, _, pv = pb.rpartition(":")
                        if pk == key:
                            prev = float(pv)
                    shown -= prev
                if shown:
                    parts.append(f"{'+' if shown > 0 else ''}{int(shown)} gold/turn")
                continue
            except ValueError:
                pass
        parts.append(lbl.replace("{v}", val) if "{v}" in lbl else f"{lbl} {val}")
    # PAYBACK. cost / gold-per-turn is the number a spending decision actually turns on, and
    # it only became computable once the income formula was measured (GDP enters income 1:1).
    # Alfred could reason about "+175 gold/turn" and "cost 2500" separately all night; he
    # could not divide them, because until now nobody had established that the first was gold.
    def _gold(fx):
        g = 0.0
        for bit in (fx or "").split(";"):
            k, _, v = bit.rpartition(":")
            if k.startswith("economy_gdp") and "_mod_" not in k:
                try:
                    g += float(v)
                except ValueError:
                    pass
        return g

    # AN UPGRADE REPLACES the building already there, so its GAIN is the difference — not
    # the new building's absolute income. MEASURED 2026-09-01: the Harbour of Lothern
    # upgrade was being shown as "+400 gold/turn, pays back in 15 turns" when port_1
    # already produces 200, so the real figures are +200 and 30 turns. Every upgrade was
    # overstated by whatever the existing building already earned, and the payback with it.
    gold = _gold(u.get("fx")) - _gold(u.get("replaces_fx"))
    # MATERIALITY. The prompt tells Alfred that a payback longer than the campaign means "do
    # not buy" — so rendering a payback figure is what puts a building on trial. A building
    # with an incidental 1 gold/turn tweak is not "bought FOR ITS INCOME", but it used to
    # render as "pays back in 2500 turns" and read exactly like a building that is. Audit
    # finding, 2026-09-01: reachable on SFO, where military buildings carry small economy
    # side-effects vanilla's do not. Below the threshold the figure is simply not shown, which
    # returns the building to the prompt's own other branch — bought for what it unlocks or
    # defends, and never refused for failing a test it was not taking.
    if gold >= PAYBACK_MIN_GOLD and u.get("cost"):
        parts.append(f"pays back in {int(round(u['cost'] / gold))} turns")
    elif gold > 0:
        parts.append(f"+{gold:g} gold/turn (incidental)")
    # A settlement upgrade's real value is mostly the SLOTS it opens — level 4 gives 5,
    # level 5 gives 7 (MEASURED, campaign_building_chain_slot_unlocks). Without this a
    # 10000g settlement upgrade reads as a weak income building, when what it actually buys
    # is room for five more buildings. Appended BEFORE the join: the first version added it
    # to `parts` after they had already been joined, so it never appeared.
    if u.get("opens_slots"):
        parts.append(f"OPENS {u['opens_slots']} BUILDING SLOTS")
    fx = ", ".join(parts)
    if u.get("unlocks"):
        bit = "unlocks: " + (u.get("unlock_units") or f"{u['unlocks']} units")
        # COMBAT POTENTIAL of what it unlocks. B8 gave military buildings their unit NAMES
        # so they would not read as pure cost beside an economy building — but a name is not
        # a quantity, and Alfred still had no way to weigh "unlocks Sea Guard" against "+400
        # gold/turn". cp is CA's own measure of what a unit is worth in a fight, and it
        # prices ~1:1 against recruitment gold (exact for 44% of costed units), so the two
        # sides of that comparison are finally in comparable units.
        cp = u.get("unlock_cp") or 0
        if cp:
            bit += f" ({int(cp)} combat power)"
        fx = f"{fx}, {bit}" if fx else bit
    return fx


# R2: 179 distinct effect_scope values exist (Synthesis §6); only tag the ones that change
# what the winning effect (fx's first, largest-magnitude key) means for THIS option's price —
# region_to_region_own is the common case and stays untagged. Everything else not listed here
# is left untagged too (proposal's own rule), not guessed at.
_SCOPE_TAGS = {
    "building_to_building_own": "this building only",
    "province_to_province_own_unseen": "province-wide",
    "region_to_province_own_unseen": "province-wide",
    "building_to_forces_own_regionwide_if_garrison": "only with a garrison",
}


def _fx_scope_tag(u):
    """`[province-wide]` etc. for the winning (first/biggest) effect in u["fx"], from the
    parallel "key:scope;key:scope" string extract_db.py emits. Untagged when the winning key
    carries no non-default scope, or when fx_scope has nothing for it (old rulebook)."""
    fx = u.get("fx") or ""
    if not fx:
        return ""
    top_key = fx.split(";", 1)[0].rpartition(":")[0]
    if not top_key:
        return ""
    for bit in (u.get("fx_scope") or "").split(";"):
        k, _, scope = bit.partition(":")
        if k == top_key:
            tag = _SCOPE_TAGS.get(scope)
            return f" [{tag}]" if tag else ""
    return ""


def _cai_cap_tag(rules, building_key, faction_counts, province_counts, province=None):
    """`[CA caps this chain at N per faction/province]` — only when the limit column is
    non-zero AND this chain is already at or over it (R3). Module-level (not a build_prompt
    closure) so it is directly testable."""
    ch = _chain_of(building_key, rules)
    lim = ((rules.get("cai") or {}).get("building_limits") or {}).get(ch) or {}
    pf_lim = lim.get("per_faction_limit") or 0
    if pf_lim > 0 and faction_counts.get(ch, 0) >= pf_lim:
        return f" [CA caps this chain at {pf_lim} per faction]"
    pp_lim = lim.get("per_province_limit") or 0
    if province is not None and pp_lim > 0 and province_counts.get(province, {}).get(ch, 0) >= pp_lim:
        return f" [CA caps this chain at {pp_lim} per province]"
    return ""


# CA's own synergy weights, MEASURED from cai_construction_system_synergy_levels. The scale
# is symmetric — low 0.15, medium_low 0.5, medium 0.75, high 2.5 — applied as a modifier to
# the candidate's priority. "absolute" is not on that scale: its row also carries a huge
# negative absolute_effect, i.e. a veto rather than a penalty, so it is kept separate.
SYNERGY_WEIGHT = {
    "cai_cs_synergy_level_low": 0.15,
    "cai_cs_synergy_level_medium_low": 0.5,
    "cai_cs_synergy_level_medium": 0.75,
    "cai_cs_synergy_level_high": 2.5,
    "cai_cs_negative_level_low": -0.15,
    "cai_cs_negative_level_medium_low": -0.5,
    "cai_cs_negative_level_medium": -0.75,
    "cai_cs_negative_level_high": -2.5,
    "cai_cs_negative_level_absolute": None,   # veto, not a penalty
}
SYNERGY_POLICY = "cai_cs_synergy_policy_default"   # the 294 universal rows


def _chain_of(key, rules=None):
    lv = ((rules or _RULES_CACHE or {}).get("buildings") or {}).get("levels") or {}
    return (lv.get(key) or {}).get("chain")


def _level_of(key, rules=None):
    lv = ((rules or _RULES_CACHE or {}).get("buildings") or {}).get("levels") or {}
    return (lv.get(key) or {}).get("level")


def _in_level_range(present_key, a_from, a_to, rules=None):
    """Is the building actually standing there inside the row's level range?

    The range endpoints are level KEYS, and the key suffix is not the level — a chain can
    start at _1/level 0 (farm) or at _3/level 3 (settlement_major). Both ends are therefore
    resolved through the same "level" int the present building is read with, so the
    comparison is in one set of units. An endpoint we cannot resolve widens the range
    rather than narrowing it: a missed veto is a build the model still has to justify,
    while a phantom veto silently removes a legal option."""
    lo = _level_of(a_from, rules) if a_from else None
    hi = _level_of(a_to, rules) if a_to else None
    cur = _level_of(present_key, rules)
    if cur is None:
        return True
    if lo is not None and cur < lo:
        return False
    if hi is not None and cur > hi:
        return False
    return True


def synergy_verdicts(state, rules):
    """CA's own read on each candidate chain, given what is ALREADY standing.

    Their construction AI does not score a building in isolation — it scores it against the
    province it would go in, and 319 of the 450 rows are NEGATIVE. Alfred scored every
    option alone, so the whole table sat in the rulebook unread: the single largest piece of
    CA's building judgement we had already extracted and were not using.

    Returns {(region, chain): (worst_weight_or_None, reason)}; None means CA vetoes it.

    ADVISORY, NOT ENFORCED. The verdict annotates the option and does not drop it: the
    scope/level matching here is READ off column names, never watched firing in-game, and a
    silent drop of a legal option is a worse failure than a note the model can weigh. Tighten
    to a hard filter only once real turns agree with it."""
    lv = ((rules or {}).get("buildings") or {}).get("levels") or {}
    syn = (_CAI or {}).get("synergies") or []
    if not syn or not isinstance(syn[0], dict):
        return {}          # pre-scope rulebook (list rows): nothing reliable to say
    # What is standing, by region and by province. The dump is a FLAT region list carrying
    # its own province field — the same shape building_upgrades reads — and the province
    # fallback matches build_prompt's exactly (an old pack predates the field, and a region
    # without one is its own province). Grouping differently here from the prompt would put
    # a province-scoped verdict on a line the model reads as a different province.
    by_region, by_province, prov_of = {}, {}, {}
    for region in (state.get("regions") if isinstance(state.get("regions"), list) else []):
        rk = region.get("region") or ""
        pk = region.get("province") or rk
        prov_of[rk] = pk
        present = [s.get("building") for s in region.get("slots", []) or []
                   if s.get("building") and s.get("building") != "EMPTY"]
        by_region[rk] = present
        by_province.setdefault(pk, []).extend(present)

    out = {}
    for row in syn:
        if row.get("policy") and row["policy"] != SYNERGY_POLICY:
            continue
        w = SYNERGY_WEIGHT.get(row.get("level"))
        if row.get("level") not in SYNERGY_WEIGHT:
            continue
        for rk, present in by_region.items():
            pool = present if row.get("scope") == "region" else by_province.get(prov_of.get(rk, ""), [])
            hit = next((k for k in pool if _chain_of(k, rules) == row["a"]
                        and _in_level_range(k, row.get("a_from"), row.get("a_to"), rules)), None)
            if not hit:
                continue
            reason = _disp_name(hit, lv)
            key = (rk, row["b"], row.get("b_from") or "", row.get("b_to") or "")
            # a veto outranks any weight; otherwise the most negative reading wins, so a
            # candidate is never sold on a bonus that a penalty in the same province cancels
            rank = float("-inf") if w is None else w
            prev = out.get(key)
            if prev is None or rank < (float("-inf") if prev[0] is None else prev[0]):
                out[key] = (w, reason)
    return out


def _disp_name(key, levels):
    return (levels.get(key) or {}).get("name") or key


def _syn_note(building_key, regions, verdicts, rules=None):
    """CA's own verdict on putting this chain in these regions, as a short prompt suffix.

    NEW options are collapsed across every empty slot in the prompt (one line for all of
    them), so a verdict that differs by region has to name the region or it would read as a
    blanket claim. Upgrades pass a single region and get the short form."""
    ch = _chain_of(building_key, rules)
    if not ch:
        return ""
    return _syn_note_for_chain(ch, regions, verdicts, rules, building_key)


def _syn_note_for_chain(ch, regions, verdicts, rules=None, building_key=None):
    """The rendering half, keyed by chain so its precedence is assertable on its own."""
    # The row's CANDIDATE side is level-ranged too, and it is the level being BUILT that has
    # to fall inside it. Ignoring this read "never build a level-1 court in a minor
    # settlement" as "never own a court there", and vetoed the upgrade of one already
    # standing — the opposite of what CA wrote.
    hits = [(r, v) for (r, c, bf, bt), v in verdicts.items()
            if c == ch and r in regions
            and (building_key is None or _in_level_range(building_key, bf, bt, rules))]
    if not hits:
        return ""
    vetoes = [r for r, (w, _) in hits if w is None]
    if vetoes:
        why = next(why for _, (w, why) in hits if w is None)
        where = "" if len(regions) == 1 else " in " + ",".join(
            _region_name(rules, r) for r in vetoes)
        return f", CA'S AI NEVER BUILDS THIS{where} (alongside {why})"
    # no veto: report the strongest signed weight, which is what CA would actually apply
    r, (w, why) = max(hits, key=lambda h: abs(h[1][0]))
    where = "" if len(regions) == 1 else f" in {_region_name(rules, r)}"
    verb = "rates this higher" if w > 0 else "rates this lower"
    return f", CA's AI {verb}{where} ({w:+.2f}, alongside {why})"


def _stance_name(raw):
    """MILITARY_FORCE_ACTIVE_STANCE_TYPE_DEFAULT -> "default".

    NOT a loc lookup: searched .cache/cascripts for a shipped idiom that localises a stance
    type (grepped for MILITARY_FORCE_ACTIVE_STANCE_TYPE, military_force_stance, stance_name,
    current_stance across script/_lib/*.lua) and campaign_stances_tables itself carries no
    localised_fields in the schema — nothing shipped names these for the player. This is a
    plain string fallback, stated as one: strip CA's constant prefix and lowercase, never
    presented as the game's own wording."""
    if not raw or not isinstance(raw, str):
        return "unknown stance"
    s = raw
    for pfx in ("MILITARY_FORCE_ACTIVE_STANCE_TYPE_", "MILITARY_FORCE_STANCE_TYPE_"):
        if s.startswith(pfx):
            s = s[len(pfx):]
            break
    return s.lower().replace("_", " ") if s else "unknown stance"


def situation_lines(state, rules=None, cap_armies=4):
    """BOX B: the SITUATION block — war list + a few armies. Capped ~6 lines total.

    FAIL SOFT, not fail open: an older pack's dump has no 'armies'/'diplomacy' keys at all
    (mod-side reads never shipped), and this must render NOTHING, not a crash and not a
    misleading "0 armies, at peace with everyone". Presence of the key, not its content, is
    the fail-open/fail-soft boundary here — the mod side already turned "API missing" into
    "field absent" (src/lua/aigen.lua serialize_armies/serialize_diplomacy)."""
    armies = state.get("armies")
    dip = state.get("diplomacy")
    if not isinstance(armies, list) or not isinstance(dip, dict):
        return []
    war = dip.get("at_war_with")
    war = war if isinstance(war, list) else []
    lines = ["\nSITUATION:"]
    war_names = [_faction_name(state, k) for k in war]
    lines.append("At war with: " + (", ".join(war_names) if war_names else "nobody"))
    # R3: CA's own construction-budget weight for the state we're actually in (war/peace
    # already computed above, no new dump field needed) — one line, advisory only.
    tmpl = ((rules or {}).get("cai") or {}).get("templates") or {}
    ctx = "war" if war_names else "peace"
    w = tmpl.get(ctx)
    if w:
        lines.append(
            f"CA's own AI, {'at war' if ctx == 'war' else 'at peace'}, would weight "
            f"military {w.get('military', 0)} / defence {w.get('defence', 0)} / "
            f"support {w.get('support', 0)} of its construction budget (advisory).")
    # BOX D1 (M3 dump: upkeep, ap_pct, is_armed_citizenry). A militia force
    # (military_force:is_armed_citizenry(), lib_campaign_invasion_manager.lua:1227) is a
    # garrison auto-raise, not a force he commands — counting it in "{N} armies" alongside
    # his real stacks was the wrong number to hand the model, so it gets its own tally.
    n_militia = sum(1 for a in armies if isinstance(a, dict) and a.get("is_armed_citizenry"))
    n_regular = len(armies) - n_militia
    militia_str = f", +{n_militia} militia" if n_militia else ""
    lines.append(f"{n_regular} armies{militia_str}:")
    for a in armies[:cap_armies]:
        if not isinstance(a, dict):
            continue
        name = a.get("name_display") or a.get("name") or "unknown lord"
        units = a.get("units")
        n_units = len(units) if isinstance(units, list) else 0
        x, y = a.get("x"), a.get("y")
        pos = f"{x},{y}" if isinstance(x, (int, float)) and isinstance(y, (int, float)) \
            else "unknown position"
        stance = _stance_name(a.get("stance"))
        tags = []
        upkeep = a.get("upkeep")
        if isinstance(upkeep, (int, float)):
            tags.append(f"upkeep {int(upkeep)}")
        ap = a.get("ap_pct")
        if isinstance(ap, (int, float)):
            tags.append(f"AP {int(ap)}%" + (" (stuck)" if ap < 8 else ""))
        tag_str = (", " + ", ".join(tags)) if tags else ""
        prefix = "[militia] " if a.get("is_armed_citizenry") else ""
        lines.append(f"- {prefix}{name} at {pos}, {n_units} units, {stance}{tag_str}")
    if len(armies) > cap_armies:
        lines.append(f"  +{len(armies) - cap_armies} more")
    return lines


def build_prompt(rules, state, journal=(), prefs=None, intent="both", purpose=None):
    prefs = prefs or {"stance": "balanced", "reserve_floor": RESERVE_FLOOR_MIN,
                       "capabilities": dict(CAP_DEFAULTS)}
    floor = effective_floor(state, prefs)
    # .get, not [] — a state dump missing a key must degrade, never crash the daemon.
    # MEASURED 2026-09-01 in a robustness pass: a dump without 'income' or 'turn' raised
    # KeyError and took the whole daemon down. An older pack, or a dump interrupted
    # mid-write, produces exactly that.
    lines = [f"Turn {state.get('turn', '?')}. "
             f"Faction {_faction_name(state, state.get('faction', 'unknown'))}. "
             f"Treasury {state.get('treasury', 0)} (reserve floor {floor}), "
             f"income {state.get('income', '?')}, net {state.get('net_income', '?')}."]
    # BOX D1 (M2 dump: trade_value, upkeep_income_percent, influence, losing_money). Every
    # field is optional and omitted when absent, so an old dump's header is untouched.
    _hdr = []
    tv = state.get("trade_value")
    if isinstance(tv, (int, float)):
        _hdr.append(f"trade {int(tv)}")
    uip = state.get("upkeep_income_percent")
    if isinstance(uip, (int, float)):
        _hdr.append(f"upkeep {int(uip)}% of income" + (" — OVEREXTENDED" if uip > 85 else ""))
    infl = state.get("influence")
    if isinstance(infl, (int, float)):
        _hdr.append(f"influence {int(infl)}")
    # dumped losing_money is CA's own bookkeeping (wh3_narrative_shared_chains.lua:2203);
    # fall back to the net<0 inference only for a dump too old to carry the field.
    losing = state.get("losing_money")
    if losing is None:
        _ni = state.get("net_income")
        losing = isinstance(_ni, (int, float)) and _ni < 0
    if losing:
        _hdr.append("losing money")
    if _hdr:
        lines[0] += " " + ", ".join(_hdr) + "."
    if prefs.get("stance", "balanced") != "balanced":
        lines.append("PLAYER PREFERENCE: " + STANCES[prefs["stance"]])
    caps = clamp_caps(prefs.get("capabilities"))
    lines.append(TONE_LINE)
    # An unchecked capability is not mentioned, not offered, and not explained. Telling him
    # "you may not do X" is still telling him X exists, and he will suggest it in prose.
    off = [k for k in CAPABILITIES if not caps[k]]
    if off:
        lines.append("PERMITTED: " + (", ".join(
            CAPABILITIES[k][0] for k in CAPABILITIES if caps[k]) or "nothing at all"))
    # B6a: this campaign's own measured record. Kept to one line — it is context, and the
    # sweep prompt has a hard-won token budget (19.8k -> 3.5k, 2026-08-31).
    econ = econ_summary(journal)
    if econ:
        lines.append("THIS CAMPAIGN SO FAR: " + econ)
    # BOX D: shrink the DESCRIPTIVE half of the prompt once the journal has history to lean
    # on. NEVER touches `ups` (below) — the enumerated option list stays full and legal
    # either way, only how much standing state gets described around it changes.
    prior_rows = _prior_rows(journal, state.get("turn"))
    is_full = render_full(state, prior_rows)
    if not is_full and prior_rows:  # guard: delta needs a prior row (review MED, unreachable in normal flow)
        lines += delta_lines(state, prior_rows[-1])
    lines += situation_lines(state, rules)
    # THE SANDBOX PLAN. tools/sandbox.py searches build orders against CA's own income
    # formula (faction_gdp_other + building GDP x modifiers + difficulty bonus, measured
    # within 6% of his real income) and writes the best order it found. This is what
    # "training Alfred" amounts to without a fine-tuning API: hand him a plan computed from
    # the game's own numbers, the way CA's priorities are already handed to him.
    #
    # GUIDANCE, NOT ORDERS. It is stale the moment the campaign moves, it models no war,
    # diplomacy or public order, and it is only offered when it matches the campaign the
    # prompt is actually about.
    plan = _opening_plan(state)
    if plan:
        lines.append("SUGGESTED ORDER (searched against the game's own income formula over "
                     f"{plan['turns']} turns; guidance, not orders — it knows nothing of "
                     "war, corruption or public order):")
        for i, step in enumerate(plan["plan"][:6], 1):
            lines.append(f"  {i}. {step['name']} ({step['kind']}, {step['cost']}g)")

    movers = region_movers(journal)
    if movers:
        lines.append("REGION OUTPUT: " + movers)
    show_skill_options = intent in ("skills", "both")
    show_building_options = intent in ("buildings", "both")
    lines.append("\nCharacters with unspent skill points — role and existing build"
                 + (", and LEGAL options (pick in-role, continue lines already started):"
                    if show_skill_options else ":"))
    n_chars = 0
    # CAPABILITY GATE, done by emptying the list rather than by branching around the section:
    # the renderer below already handles "no characters" and prints "- none", so switching
    # skills off reuses a path that is exercised every time a campaign has no spare points.
    # Branching around 25 lines would have been a second, untested rendering path.
    _chars = state.get("characters") if isinstance(state.get("characters"), list) else []
    if not caps["skills"]:
        _chars = []
    # BOX D1 (M1 dump: loyalty). CA only runs the loyalty mechanic for Dark Elves and
    # Skaven (wh_campaign_interventions.lua:640,750: only those cultures' start_loyalty_
    # interventions fire) and the pirate variant for Vampire Coast (:797-799) — a High Elf
    # character's loyalty() call, if it even returns a number, describes nothing the player
    # sees in the UI, so the daemon (not the mod) decides whether to show it.
    _loyalty_culture = any(t in (state.get("faction") or "") for t in ("_def_", "_skv_", "_cst_"))
    for c in _chars:
        pts = c.get("points")
        if pts in (None, "unknown", 0):
            continue
        opts = legal_skills(rules, c) if show_skill_options else ()
        if show_skill_options and not opts:
            continue
        n_chars += 1
        taken = c["skills"] if isinstance(c.get("skills"), list) else []
        taken_labels = [_skill_label(k) for k in taken]
        if len(taken_labels) > 20:
            taken_str = ", ".join(taken_labels[:20]) + f", +{len(taken_labels) - 20} more"
        else:
            taken_str = ", ".join(taken_labels) or "none"
        # a level-up whose effects are already on the taken line above is rendered brief;
        # `shown` is what that line actually printed, so a truncated one keeps its effects
        shown = set(taken[:20]) if len(taken_labels) > 20 else set(taken)
        opts_str = ", ".join(
            _skill_label(k, lvl, brief=(lvl and k in shown)) + _rank_tag(rules, k)
            for k, _, lvl in opts)
        loyalty_str = ""
        if _loyalty_culture and isinstance(c.get("loyalty"), (int, float)):
            loyalty_str = f", loyalty {int(c['loyalty'])}"
        lines.append(f"- cqi={c['cqi']} {c.get('name_display') or c['name']} "
                     f"({c['subtype']}, rank {c['rank']}, "
                     f"{pts} points{loyalty_str})")
        lines.append(f"    taken: {taken_str}")
        if show_skill_options:
            lines.append(f"    options: {opts_str}")
    if n_chars == 0:
        lines.append("- none" if caps["skills"]
                     else "- skill spending is switched off by the player")
    # CAPABILITY GATE for buildings, at the ONE place the option list is built. `ups` is both
    # what the prompt renders and what validate() builds its legal set from, so emptying it
    # here switches off the suggestion and the permission together — they cannot drift apart.
    ups = ((building_upgrades(rules, state) + building_construction(rules, state))
           if caps["buildings"] else [])
    if purpose is not None:
        # This exact tuple is both rendered and later validated.  Never regenerate it for a
        # retry: a changed state/option list would let a response escape the reviewed subset.
        ups = purpose_options(rules, ups, purpose)
        if ups is None:
            ups = ()
            lines.append("BUILDING PURPOSE GUARD: reviewed Vanilla labels are unavailable; do not issue a building order.")
        else:
            lines.append(f"BUILDING PURPOSE GUARD: issue building commands only for {_request_label(purpose)} from the listed options.")
    # 2026-09-17 affordability (alfred-20260917-faction-rules-affordability, owner: "treasury
    # empty = attrition"): an option costing more than treasury-minus-floor was rendered and
    # offered as a pick same as any other, with no signal it would be refused. `afford_ups` is
    # the RENDER-ONLY subset below — `ups` itself is returned unfiltered, so a named pick of an
    # option that's legal-but-over-budget still falls through validate()'s existing reserve-
    # floor branch (real reason, FALSE_CLAIM re-ask) instead of the silent "not enumerated" one
    # a hallucinated building gets; only what's OFFERED as a suggestion changes here.
    afford_ups = ups
    if ups:
        budget = (state.get("treasury") or 0) - floor
        afford_ups = [u for u in ups if u["cost"] <= budget]
        if not afford_ups:
            lines.append(f"BUILDING OPTIONS: none affordable right now — treasury "
                         f"{state.get('treasury', 0)}, reserve {floor}. Tell him to save up; "
                         f"do not list or propose any building.")
    # CA scores a building against the province it goes in, not in isolation (450 rows,
    # 319 of them negative). Computed once here and attached to every option line below.
    verdicts = synergy_verdicts(state, rules)
    # R3: how many of each chain stand already, faction-wide and per province — the two
    # scopes CA's own per-faction/per-province building_limits are counted against. Counted
    # once here, used by _cai_cap_tag below on every option line.
    faction_counts, province_counts = {}, {}
    for region in (state.get("regions") if isinstance(state.get("regions"), list) else []):
        prov_key = region.get("province") or region["region"]
        pc = province_counts.setdefault(prov_key, {})
        for slot in region.get("slots") or []:
            b = slot.get("building")
            if not b or b == "EMPTY":
                continue
            ch = _chain_of(b, rules)
            faction_counts[ch] = faction_counts.get(ch, 0) + 1
            pc[ch] = pc.get(ch, 0) + 1

    lines.append("\nBuildings by province (existing chains + empty slots"
                 + (", then UPGRADE/NEW options — so the LLM can diversify instead of "
                    "judging slots blind):" if show_building_options else "):"))
    # old installed pack predates the province field (fixed pack loads at his next
    # relaunch) — fall back to region key as its own group so this never KeyErrors.
    provinces, region_prov = {}, {}
    for region in (state.get("regions") if isinstance(state.get("regions"), list) else []):
        prov_key = region.get("province") or region["region"]
        provinces.setdefault(prov_key, []).append(region)
        region_prov[region["region"]] = prov_key
    if not provinces:
        lines.append("- none")
    levels = (rules.get("buildings") or {}).get("levels") or {}
    for prov in sorted(provinces):
        lines.append(f"\n[{_province_name(rules, prov)}]")
        # BOX D1 (M1 dump: province-scoped corruption, `state['provinces'][<key>]`). CA's
        # own first band is 50 (cm:get_highest_corruption_in_region(),
        # lib_campaign_manager.lua:9808-9847); 75 is its second, rendered here as "severe".
        prov_data = (state.get("provinces") or {}).get(prov)
        if isinstance(prov_data, dict):
            val = prov_data.get("value")
            if isinstance(val, (int, float)) and val >= 50:
                sev = " (severe)" if val >= 75 else ""
                lines.append(f"{_corruption_label(rules, prov_data.get('type'))} "
                             f"{int(val)}{sev}")
        region_keys = {r["region"] for r in provinces[prov]}
        for region in provinces[prov]:
            chains = [levels.get(s["building"], {}).get("name") or s["building"]
                      for s in region["slots"] if s["building"] != "EMPTY"]
            # BOX S: a LOCKED slot reads as EMPTY but the game refuses any build into it
            # (the settlement level hasn't unlocked it) — never count it as an option, but
            # tell Alfred it exists so he knows an upgrade opens it.
            empty = sum(1 for s in region["slots"]
                        if s["building"] == "EMPTY" and not s.get("locked"))
            locked_n = sum(1 for s in region["slots"] if s.get("locked"))
            po = region.get("public_order")
            po_str = ""
            if isinstance(po, (int, float)):
                # flagged, not just reported: CA penalises a build that pushes control
                # negative at -50 per point against +10 for gaining it, and a bare number
                # is easy to skim past
                po_str = f", control {int(po)}" + (" — DO NOT push lower" if po <= 0 else "")
            # BOX D: in delta mode, a region with nothing on offer this turn (no empty slot,
            # no upgrade below) doesn't need its full chain list re-stated every sweep — one
            # line is enough to keep it in view. A region WITH an option still gets the full
            # line, same as full mode, since that's what the [UPGRADE] lines below are about.
            has_upgrade = any(u["region"] == region["region"] and not u["new"] for u in afford_ups)
            rname = _region_name(rules, region["region"])
            rtags = _region_tags(rules, region, afford_ups)
            tag_str = (" [" + ", ".join(rtags) + "]") if rtags else ""
            if not is_full and empty == 0 and not has_upgrade:
                lines.append(f"- {rname}: {len(chains)} buildings{po_str}{tag_str}")
                continue
            locked_str = f", {locked_n} locked" if locked_n else ""
            lines.append(f"- {rname}: {', '.join(chains) or 'none'} "
                         f"({empty} empty slot{'s' if empty != 1 else ''}"
                         f"{locked_str}{po_str}){tag_str}")
        if show_building_options:
            for u in afford_ups:
                if u["region"] not in region_keys:
                    continue
                # NEW options are rendered ONCE below, not per empty slot. Measured 2026-08-31:
                # the same 8 buildings were repeated across 49 empty slots = 392 lines and 16,641
                # of 19,831 prompt tokens, and B3/B8 had just made each of those lines 5x longer.
                # The option set is identical for every empty secondary slot, so collapsing is
                # lossless. PRESENTATION ONLY — `ups` is unchanged, so validate()'s legal set
                # still contains every (region, slot, building) combination.
                if u["new"]:
                    continue
                # CEILING, MEASURED 2026-09-01: upgrade lines are NOT capped — every upgradeable
                # slot gets one — so the prompt grows linearly with the empire. 2 regions = 786
                # tokens, 12 = 2464, 24 = 4491, 40 = 7193, 60 = 10571 against a 3-5k target.
                # Deliberately left uncapped: he is on 2 regions, and every capping rule tried
                # tonight (cheapest-first, raw priority) hid the best option before Alfred saw
                # it, twice. A cap here would need the same ranking-and-proving work the
                # new-build list got, and it is not worth doing before it bites. If a sweep ever
                # reads long or adherence drops on a wide empire, this is the line to revisit —
                # rank by _cai_adjusted and cap, the way building_construction does.
                tag = "UPGRADE"
                # B3: what the building DOES, straight from the game's own effect table. Before
                # this the model saw a key and a price and nothing else, so any claim about what a
                # building was worth came from its training memory rather than this campaign's data.
                # Raw keys/values on purpose — GDP is an abstract contribution, not gold, and
                # fabricating a gold figure would be quietly wrong.
                # prompt-side trim: the rulebook keeps 6 effects, the prompt shows the top 3.
                # Measured 2026-08-31: untrimmed fx grew this prompt 69% (11.5k -> 19.5k tokens)
                # against a 3-5k spec target, and prompt bloat costs option adherence — the exact
                # thing the enumerate-the-options design exists to protect.
                fx = _fx_str(u)
                lines.append(f"  [{tag}] region={u['region']} slot={u['slot_idx']} -> "
                             f"{u['building']}{_disp(u)} (cost {u['cost']}"
                             + _cai_building(u, rules)
                             + _syn_note(u["building"], [u["region"]], verdicts, rules)
                             + (f", {fx}" if fx else "") + ")"
                             + _fx_scope_tag(u)
                             + _cai_cap_tag(rules, u["building"], faction_counts, province_counts,
                                            region_prov.get(u["region"])))

    # Keep the costly explanation once per building, but print every legal command triple.
    new_opts, slots = {}, []
    for u in afford_ups:
        if not u["new"]:
            continue
        pair = (u["region"], u["slot_idx"])
        if pair not in slots:
            slots.append(pair)
        new_opts.setdefault(u["building"], []).append(u)
    if show_building_options and new_opts:
        by_region = {}
        for r, i in slots:
            by_region.setdefault(r, []).append(str(i))
        slot_str = "; ".join(f"{_region_name(rules, r)} slots {','.join(v)}"
                              for r, v in by_region.items())
        lines.append(f"\nEMPTY SLOTS ({len(slots)}): {slot_str}")
        lines.append("NEW build options:")
        for building, options in sorted(new_opts.items(),
                                        key=lambda item: (item[1][0]["cost"], item[0])):
            u = options[0]
            fx = _fx_str(u)
            triples = "; ".join(
                f"(region={v['region']}, slot={v['slot_idx']}, building={v['building']})"
                for v in sorted(options, key=lambda v: (v["region"], v["slot_idx"])))
            cap_tags = { _cai_cap_tag(rules, building, faction_counts, province_counts,
                                      region_prov.get(v["region"])) for v in options }
            lines.append(f"  [NEW] {building}{_disp(u)} (cost {u['cost']}"
                         + _cai_building(u, rules)
                         + _syn_note(building, sorted({v["region"] for v in options}), verdicts, rules)
                         + (f", {fx}" if fx else "") + ")"
                         + _fx_scope_tag(u)
                         + "".join(sorted(cap_tags))
                         + f"\n    legal: {triples}")
    return "\n".join(lines), ups


def headless_argv(system, prompt):
    """The one place that builds argv for a headless `claude -p` call.

    WHY (measured 2026-09-01): each unflagged `claude -p` boots the full Claude
    Code harness -- context7 + apple-events MCP servers, hooks, settings, CLAUDE.md -- for
    one prompt, then exits. The eval fired one every few seconds: 23 claude + 12 node
    processes, 1-min load 294 against a hard cap of 100, zombies, and ~100 finished-session
    hook firings onto the fleet board. A one-shot rulebook call has no business paying for
    any of that.

    Flags:
      --restricted          no user/project/local settings, no hooks, no CLAUDE.md
      --strict-mcp-config    only use MCP servers named by --mcp-config
      --mcp-config '{"mcpServers":{}}'   ...and name none (bare '{}' is rejected: the flag
                                          wants an mcpServers record, not an empty object)
    """
    return ["claude", "-p", "--restricted", "--strict-mcp-config",
            "--mcp-config", '{"mcpServers":{}}', "--system-prompt", system, prompt]


# B1b: how many skills one character may receive in a sweep when the mod could not
# read their real unspent-point count. One, not unlimited.
UNKNOWN_POINTS_CAP = 1


def _dropped_building_label(rules, b):
    """Player-facing label for a DROP entry — display name + region, same lookup
    _describe_option uses, so a re-ask about a dropped building reads like the rest of the
    prompt rather than a raw key."""
    levels = (rules.get("buildings") or {}).get("levels") or {}
    name = (levels.get(b.get("building")) or {}).get("name") or b.get("building") or "a building"
    return f"{name} at {_region_name(rules, b.get('region'))}"


def _dropped_skill_label(chars, s):
    c = chars.get(s.get("cqi"))
    return (c.get("name") if c else None) or f"cqi {s.get('cqi')}"


def validate(decision, rules, state, upgrade_options, prefs=None, may_act=True,
             building_only=False, dropped=None):
    """Belt: only enumerated choices survive (mod is the suspenders).

    B7: `may_act` False drops every command regardless of legality — the player asked a
    question, not for work. This gate is here in Python rather than only in the prompt,
    where a stored preference or a persuasive message could talk past it.

    `dropped`, when given a list, gets one {"kind", "label", "reason"} entry appended per
    command this call refuses — structured, plain-language, and label-matched to what the
    print() lines already say — so a caller (chat()'s FALSE_CLAIM fix) can re-ask the model
    with the real reason instead of leaving a silently-dropped order looking sent."""
    cmds = []
    prefs = prefs or {"stance": "balanced", "reserve_floor": RESERVE_FLOOR_MIN,
                       "capabilities": dict(CAP_DEFAULTS)}
    floor = effective_floor(state, prefs)
    # THE MODEL'S OUTPUT IS UNTRUSTED SHAPE, not just untrusted content. Everything below
    # assumes a dict of two lists of dicts, and MEASURED 2026-09-01 it threw AttributeError
    # on six of nine malformed shapes — including {"skills": null}, which is not adversarial
    # at all: the prompt says those keys may be omitted, and a null is one obvious reading of
    # that. In a sweep the throw is caught by poll() and the sweep is silently lost; in chat
    # it costs the reply. Coerced instead, so a bad shape yields "no commands" rather than an
    # exception, and the enumerated-options check still decides everything that survives.
    if not isinstance(decision, dict):
        print(f"  DROP everything: the model returned {type(decision).__name__}, not an object")
        return []

    def _entries(key):
        v = decision.get(key)
        if not isinstance(v, list):
            if v not in (None, ""):
                print(f"  DROP {key}: expected a list, got {type(v).__name__}")
            return []
        return [x for x in v if isinstance(x, dict)]

    decision = {"skills": _entries("skills"), "buildings": _entries("buildings")}
    if building_only and decision["skills"]:
        print(f"  DROP all {len(decision['skills'])} skill command(s): building-purpose request")
        decision["skills"] = []
    # SECOND, INDEPENDENT CAPABILITY GATE. build_prompt already refuses to offer an unchecked
    # capability, but a gate that only exists in the prompt is a sentence the model can be
    # talked past — the same reason the act gate and the reserve floor were moved into Python.
    # This one drops the command whatever the prompt said. Logged, never silent: a refused
    # command is indistinguishable from a completed one from outside, which is the failure
    # mode this whole file is built around.
    chars = {c["cqi"]: c for c in (state.get("characters") or [])}
    caps = clamp_caps(prefs.get("capabilities"))
    for key in ("skills", "buildings"):
        if decision[key] and not caps[key]:
            print(f"  DROP all {len(decision[key])} {key} command(s): "
                  f"{key} is switched off by the player")
            if dropped is not None:
                for item in decision[key]:
                    label = (_dropped_building_label(rules, item) if key == "buildings"
                             else _dropped_skill_label(chars, item))
                    dropped.append({"kind": key[:-1], "label": label,
                                     "reason": f"{key} are switched off in Alfred's settings — "
                                     "turn them on in the Alfred page of the Mod "
                                     "Configuration Tool"})
            decision[key] = []
    if not may_act:
        n = len(decision.get("skills", [])) + len(decision.get("buildings", []))
        if n:
            print(f"  DROP all {n} command(s): the player asked a question, not for action")
        return []
    skill_used = {}  # cqi -> accepted skill commands so far this sweep
    for s in decision.get("skills", []):
        c = chars.get(s.get("cqi"))
        legal = {k for k, _, _ in legal_skills(rules, c)} if c else set()
        cqi = s.get("cqi")
        pts = c.get("points") if c else None
        # B1b: an unknown point count is NOT a licence to spend freely. When the mod has
        # not probed points, `pts` is the string "unknown" and the old test
        # (`not isinstance(pts, int) or ...`) short-circuited the cap entirely, so the
        # model could stack any number of skills on one character. Unknown now means the
        # most conservative real answer: at most one skill this sweep.
        is_legal = bool(c and s.get("skill") in legal)
        cap = pts if isinstance(pts, int) else UNKNOWN_POINTS_CAP
        under_cap = skill_used.get(cqi, 0) < cap
        # A SKILL CANNOT BE LEVELLED PAST ITS LAST LEVEL. The point cap alone does not stop
        # two points going into one skill that has only one level left: it counts points per
        # character, not per skill. The dump says a skill is taken but not at which level, so
        # the tightest sound bound is (levels - 1) — a taken skill is at level 1 or higher,
        # and 739 skills have three levels while 460 have two, so this is a real ceiling
        # rather than a formality. An untaken skill spends its first point on level 1 and is
        # bounded by the cap itself.
        same = sum(1 for x in cmds
                   if x.get("kind") == "skill" and x.get("cqi") == cqi
                   and x.get("skill") == s.get("skill"))
        lv_cap = (rules["skills"].get("caps") or {}).get(s.get("skill"))
        taken_already = bool(c and s.get("skill") in set(c.get("skills") or []))
        room = None if lv_cap is None else (lv_cap - 1 if taken_already else lv_cap)
        under_levels = room is None or same < room
        if is_legal and under_cap and under_levels:
            cmds.append({"id": uuid.uuid4().hex[:12], "kind": "skill",
                         "cqi": s["cqi"], "skill": s["skill"],
                         "why": s.get("why", "")})
            skill_used[cqi] = skill_used.get(cqi, 0) + 1
        elif not is_legal:
            print(f"  DROP skill {s} (not in enumerated options)")
            if dropped is not None:
                dropped.append({"kind": "skill", "label": _dropped_skill_label(chars, s),
                                 "reason": f"no such skill option for "
                                 f"{_dropped_skill_label(chars, s)}"})
        elif not under_levels:
            print(f"  DROP skill {s} (only {room} level(s) left in that skill)")
            if dropped is not None:
                dropped.append({"kind": "skill", "label": _dropped_skill_label(chars, s),
                                 "reason": "already at the highest level"})
        else:
            print(f"  DROP skill {s} (exceeds {cqi}'s {pts} skill points this sweep)")
            if dropped is not None:
                dropped.append({"kind": "skill", "label": _dropped_skill_label(chars, s),
                                 "reason": "no skill points left this sweep"})
    legal_ups = {(u["region"], u["slot_idx"], u["building"]) for u in upgrade_options}
    sieged_regions = {r.get("region") for r in (state.get("regions") or [])
                      if r.get("under_siege") is True}
    budget = state.get("treasury") or 0
    used_slots = set()  # (region, slot_idx): first accepted command per slot wins
    for b in decision.get("buildings", []):
        # slot_idx is specified as an int and the prompt shows it as one, but a model that
        # returns "1" would have every pick silently dropped as "illegal" — the tuple simply
        # would not match. Coerced when it is a digit string, which cannot weaken anything:
        # the enumerated-options check below still decides, on exactly the same set.
        if isinstance(b.get("slot_idx"), str) and b["slot_idx"].strip().lstrip("-").isdigit():
            b = dict(b, slot_idx=int(b["slot_idx"]))
        key = (b.get("region"), b.get("slot_idx"), b.get("building"))
        slot_key = (b.get("region"), b.get("slot_idx"))
        if key[0] in sieged_regions:
            print(f"  DROP building {b} (region is under siege)")
            if dropped is not None:
                dropped.append({"kind": "building", "label": _dropped_building_label(rules, b),
                                 "reason": "the region is under siege"})
            continue
        cost = next((u["cost"] for u in upgrade_options
                     if (u["region"], u["slot_idx"], u["building"]) == key), None)
        if slot_key in used_slots:
            print(f"  DROP building {b} (slot {slot_key} already has a command this sweep)")
        elif key in legal_ups and budget - cost >= floor:
            budget -= cost
            used_slots.add(slot_key)
            turns = next(u.get("turns", 1) for u in upgrade_options
                         if (u["region"], u["slot_idx"], u["building"]) == key)
            cmds.append({"id": uuid.uuid4().hex[:12], "kind": "building",
                         "region": b["region"], "slot_idx": b["slot_idx"],
                         "building": b["building"], "cost": cost, "turns": turns,
                         "why": b.get("why", "")})
        elif key not in legal_ups:
            # NOT captured into `dropped`, deliberately: a hallucinated tuple here already
            # went through chat()'s own one-retry mismatch-correction before ever reaching
            # validate() (it re-asks the model to copy an exact printed tuple), and
            # _strip_false_completion_claims already strips any false "ordered" claim
            # generically. Adding a second, reason-specific re-ask on top would fire a THIRD
            # model call for the same hallucination and contradicts the one-retry contract
            # (H2 test: exactly one retry).
            print(f"  DROP building {b} (not in the enumerated options)")
        else:
            # SAY WHICH. "illegal or breaks reserve floor" conflated a hallucinated key with
            # a legal pick he simply cannot afford — two very different problems reading
            # identically in the log, and the second is the one worth telling him about.
            print(f"  DROP building {b} (costs {cost}, leaves "
                  f"{budget - (cost or 0)} against a floor of {floor})")
            if dropped is not None:
                dropped.append({"kind": "building", "label": _dropped_building_label(rules, b),
                                 "reason": f"would take treasury below the {floor} reserve"})
    return cmds


CCO_ENV_BRIDGE = ('local env for l=1,6 do local ok,e=pcall(getfenv,l) '
                  'if ok and type(e)=="table" and type(e.find_uicomponent)=="function" '
                  'then env=e break end end')


def skill_cmds_as_exec(cmds):
    """Bridge both command kinds over the live exec channel (installed pack predates
    the fixes). Skills: CA's real 4-arg add_skill, native mode, verified. Buildings:
    the REAL game queue via CcoBuildingLevelRecord.IssueCommandForBuildingBrowser —
    idiom from CA's own wh3_dlc24_matters_of_state.lua; gold charged up-front,
    visible timer, no internal pending queue."""
    out = []
    for c in cmds:
        why = _lua_safe(c.get("why", ""))
        if c["kind"] == "skill":
            # B1b: NATIVE ONLY. The retry with ignore_requirements=true is gone. It
            # disabled the game's own prerequisite check — the suspenders half of this
            # design's belt-and-suspenders — so a skill whose in-game prereqs are unmet
            # would be granted anyway. It was also never the call that worked: the one
            # owner-witnessed grant (2026-08-31 19:31:27, Tyrion's Combined Elites) came
            # from the native (false, false) form. A refusal here is the game correctly
            # saying no, and is reported rather than overridden.
            code = (f'local c = cm:get_character_by_cqi({c["cqi"]}) '
                    f'if not c then return "skill: no cqi {c["cqi"]}" end '
                    f'cm:add_skill(c, "{c["skill"]}", false, false) '
                    f'local ok = c:has_skill("{c["skill"]}") '
                    f'return "SKILL {c["skill"]}: granted=" .. tostring(ok) .. " ({why})"')
        elif c["kind"] == "building":
            code = (f'{CCO_ENV_BRIDGE} if not env then return "build: no env" end '
                    f'local sett = env.cco("CcoCampaignRoot",""):Call(\'SettlementList.FirstContext(RegionRecordKey == "{c["region"]}")\') '
                    f'if sett == nil then return "build: no settlement {c["region"]}" end '
                    f'local slot = sett:Call("BuildingSlotList.At({c["slot_idx"]})") '
                    f'if slot == nil then return "build: no slot {c["slot_idx"]}" end '
                    f'slot:Call(\'DatabaseRecordContext("CcoBuildingLevelRecord", "{c["building"]}").IssueCommandForBuildingBrowser(this)\') '
                    f'return "BUILD {c["building"]} queued: {c["region"]} slot {c["slot_idx"]} ({why})"')
        else:
            out.append(c)
            continue
        # CAPABILITY STAMP, carried through the rewrite. Every command becomes kind="exec"
        # here, so the executor in-game cannot tell a building from a skill any more — it
        # would have to read the generated Lua. The capability travels as its own field
        # instead, and the mod refuses anything whose capability the player has switched off.
        #
        # HONEST SCOPE, because a gate that overclaims is worse than none: this defends
        # against a daemon BUG and against a stale commands file — a batch decided before he
        # unchecked the box, which is a real case since the file outlives the decision. It is
        # NOT a defence against a maliciously rewritten daemon, which could simply stamp a
        # different capability. The daemon-side gates in validate() are the real enforcement;
        # this is the one that survives them being wrong.
        out.append({"id": c["id"], "kind": "exec", "code": code,
                    "cap": "skills" if c["kind"] == "skill" else "buildings"})
    return out


CARRY_CAP = 50  # ponytail: cap carried-forward commands so a long session's file can't
# grow unbounded; oldest carried entries fall off first, new writes always win

_ENTRY_RE = re.compile(r'^\{\s*(.*?)\s*\},?$')
_ID_RE = re.compile(r'\bid\s*=\s*("(?:[^"\\]|\\.)*"|-?\d+)')


def _id_token(v):
    return str(v) if isinstance(v, int) else lua_quote(v)


def _read_existing_commands():
    """Parse aigen_commands.lua's entry lines before we truncate-rewrite it, so
    write_commands can carry forward anything the mod hasn't executed yet (it dedupes by
    id, tracked in aigen_done_<id>) instead of wholesale-clobbering an in-flight sweep's
    or chat's not-yet-consumed commands (finding 7). Returns [(id_token_or_None, raw_line)]
    in file order; a line that doesn't parse is kept with id_token=None so it's always
    carried forward verbatim rather than dropped."""
    if not os.path.exists(COMMANDS):
        return []
    try:
        text = open(COMMANDS).read()
    except OSError:
        return []
    entries = []
    for line in text.splitlines():
        s = line.strip()
        m = _ENTRY_RE.match(s)
        if not m:
            continue
        idm = _ID_RE.search(m.group(1))
        entries.append((idm.group(1) if idm else None, s.rstrip(",")))
    return entries


def write_commands(cmds, cid=None, turn=None):
    """B5: stamp the campaign this batch was decided for. aigen_commands.lua is ONE file
    shared by every campaign while the done-flags that stop re-execution live in the
    savegame, so an unstamped command written for campaign A executes in campaign B the
    first time he switches saves. The mod skips anything whose stamp isn't its own.

    cid None (pre-B5 pack, which never reports an id) writes no stamp — and the pre-B5 mod
    doesn't check for one, so the old pack keeps working exactly as it does today."""
    if cid is not None:
        cmds = [dict(c, campaign=cid) for c in cmds]
    # AND THE TURN IT WAS DECIDED ON. The campaign stamp above stops commands leaking
    # between campaigns; it cannot stop them leaking BACKWARDS inside one. The done-flags
    # that prevent re-execution live in the savegame and roll back with it, while this file
    # does not — so loading a save from ten turns ago finds up to CARRY_CAP carried commands
    # with their flags gone, and replays orders decided for a position that no longer
    # exists. The executor skips anything stamped later than the current turn.
    if turn is not None:
        cmds = [dict(c, turn=int(turn)) for c in cmds]
    new_tokens = {_id_token(c["id"]) for c in cmds if c.get("id") is not None}
    carried = [raw for (tok, raw) in _read_existing_commands()
               if tok is None or tok not in new_tokens]
    carried = carried[-CARRY_CAP:]
    lines = ["return {"]
    for raw in carried:
        lines.append("  " + raw + ",")
    for c in cmds:
        fields = ", ".join(
            f"{k} = {v if isinstance(v, int) else lua_quote(v)}"
            for k, v in c.items())
        lines.append("  { " + fields + " },")
    lines.append("}")
    inplace_write(COMMANDS, "\n".join(lines))


def trigger_and_wait_for_state(rules):
    """Shared by sweep() and chat(): fire the mod's state dump and block for it.
    Returns the fresh state dict, or None if the game never answered (30s)."""
    print("sweep: triggering state dump...")
    inplace_write(TRIGGER, uuid.uuid4().hex)
    mtime0 = os.path.getmtime(STATE) if os.path.exists(STATE) else 0
    for _ in range(90):
        time.sleep(1)
        if os.path.exists(STATE) and os.path.getmtime(STATE) > mtime0:
            break
    else:
        print("  no state dump appeared in 30s — is the game running with the mod?")
        return None
    state = json.load(open(STATE))
    # first contact: mod had no candidates file -> ship one and re-trigger once
    if any(not isinstance(c.get("skills"), list) for c in (state.get("characters") or [])):
        print("  shipping skill candidates for", (state.get("faction") or ""))
        write_candidates(rules, {c.get("subtype") for c in (state.get("characters") or []) if c.get("subtype")})
        inplace_write(TRIGGER, uuid.uuid4().hex)
        time.sleep(3)
        if os.path.exists(STATE):
            state = json.load(open(STATE))
    return state


def sweep(rules):
    state = trigger_and_wait_for_state(rules)
    if state is None:
        return
    cid = campaign_id(state)
    # Refresh the sandbox plan against THIS state before deciding. It costs 0.3s against a
    # ~9s model call, so there is no reason to serve a stale plan: the previous one was
    # computed for a different treasury, and possibly a different set of slots. Failure is
    # silent by design — a missing plan simply adds nothing to the prompt.
    refresh_opening_plan()
    # record BEFORE deciding, so the prompt sees this turn's numbers in the trend and a
    # turn regression is trimmed before anything is read back out of it
    journal_record(cid, state)
    prefs = load_prefs(cid)
    prefs, _mct_note = apply_dumped_settings(cid, state, prefs)
    if _mct_note:
        print("  " + _mct_note)
    # A file/hotkey sweep carries no requested purpose or explicit confirmation.  It must
    # never revive one from transcript text, so it cannot spend on a generic sweep.
    print("sweep: no building order — name one specific purpose in chat first")
    journal_record(cid, state, [])
    return


# Owner, after using it: "the ai feels very dumb and verbose right now."
# He was right on both counts, and both were this prompt's fault.
#   VERBOSE: nothing here asked for brevity, only a 1200-char ceiling — so he wrote to the
#   ceiling every time, in a chat window 58 characters wide.
#   DUMB: "only act if the player clearly asked" made him re-confirm after the player had
#   already said "queue the plaza please". The Python act gate is the real guard, so this
#   line can stop hedging and let him act.
CHAT_SYSTEM = """You are Alfred, campaign administrator to a Total War: Warhammer 3 player, \
talking to him in a narrow in-game chat window. You handle settlement construction and \
unspent skill points.

HOW TO WRITE. Be brief. Answer as long as the question needs — short for an order, fuller \
for advice — never pad to fill space and never cut an answer short of what he asked. No \
preamble, no restating his question, no summary at the end, no "my lord". Never repeat a \
number he can already see unless it changes your answer. Do not say what you did NOT do. \
Use a numbered list whenever you offer options; plain sentences otherwise. When answering \
whether to save or spend, name the chosen listed income option and its printed payback in \
turns.

HOW TO ACT. If he asks you to do something, DO IT — do not ask him to confirm what he just \
told you. Buildings are named: he will say "Plaza", you must emit the key beside it. If his \
words match no option, say so in one line and name the closest one. Only ask a question \
back when two options genuinely fit and the choice changes the outcome. When you issue a \
command, tell him the order was SENT this turn — never that it is queued or done; the game \
confirms it asynchronously and you will not know the result until he tells you. An order \
sent is not built: never claim a building exists, is upgraded, or is finished unless the \
state itself lists it — a command you sent, even last turn, proves nothing about now. When \
a SYSTEM NOTE says a gate found nothing or needs him to choose, explain that in your own \
words — never a stock phrase, and never invent a reason the note didn't give you. When a \
SYSTEM NOTE hands you a numbered list of options, present exactly those, in that order, \
best first, and end with a short line inviting a pick by number — never a bare question \
with no options, and never a guess.
A specific requested building role or unit is an instruction: match it to a listed option by its displayed name, key, unlocks, or function, and do not replace it with a generic economy choice when that listed option is legal and affordable. Use only the option's printed legal slot(s); do not invent a CA ban, requirement, or location restriction that is not printed.

THE GAME. It is an economy builder. Money buys armies, armies take regions, regions make \
more money. Every building you pick should end up serving one of: more money, better armies, \
or holding what he has so the first two keep compounding. Early on, economy compounds and \
military does not — but a province that falls loses everything in it, so enough defence to \
keep it is economy too.

HOW CA PRICES EACH KIND OF BUILDING, measured by holding cost constant at 1000g so the \
comparison is fair (their scores scale with investment): income 1000, RECRUITMENT 1000, \
growth 250, control 125. A RECRUITMENT building is worth the SAME as an income building at \
the same price — CA does not treat military as a luxury. \
BUT DO NOT COPY CA ON GROWTH AND CONTROL. Those two numbers are what a building yields THIS \
TURN, and both of those things pay in ways a single-turn valuation cannot see: \
GROWTH accumulates into SETTLEMENT LEVELS, and 4099 building levels in this game are locked \
behind a minimum settlement level while levels 4 and 5 open 5 and 7 more building slots. A \
growth building buys BUILDING ACCESS AND SLOTS — it raises the ceiling on everything you will \
ever place in that province — and it carries army replenishment besides. Judge it on the next \
20-40 turns, not on its gold this turn. \
CONTROL is worth more to YOU than to CA: the human player carries a public-order handicap that \
scales with campaign difficulty (down to -8) and the AI carries none, so CA's own valuation \
was calibrated without the penalty you are paying. \
CA also DECAYS these scores as a province fills up — the first building of a CHAIN is worth \
1000 and the fourth of that SAME chain about 200, while control holds 500 down to 250. So \
when the second or third building of the SAME chain is being weighed against a fresh control \
chain, the control building outranks it by CA's own maths — this is per chain, not per \
category, so two different income chains in one province do not decay each other. \
When a building is bought FOR ITS INCOME, a payback longer than the campaign is a loss \
rather than a slow win: refusing those was worth 4-17% of the treasury simulated against \
CA's own income formula, more than any ordering change, so buying nothing is a real move. \
That test applies only to income — a building with no gold figure is bought for what it \
unlocks or defends and is judged on that. \
CONTROL IS ASYMMETRIC: CA penalises losing it at -50 per point against +10 for gaining it, \
and the gain stops counting once a province is comfortably positive. So chase it when a \
province is near or below zero — losing control loses you the settlement outright — ease off \
once it is safely positive, and never take a build that pushes one negative. \
Food has the same shape at +0.9 to gain against -3.0 to spend.

HOW CA SPENDS, measured from their own budget tables — useful sanity checks, not orders. \
Their default AI spends 90% of net income each turn rather than hoarding, and splits the \
budget army 55% / construction 40% / agents 3% / diplomacy 2%. An economy-focused province \
template is 75% support buildings to 25% military. So: sitting on a large treasury is a \
mistake whenever something passes the payback test above, and roughly two economy \
buildings per military one is the shape CA aims at. Those two are one rule: spend freely \
on what pays, hold when nothing does, and never buy merely because the money is there.

HOW CA PICKS SKILLS, measured from their own selection policy: they weight DEEPENING a line \
the character has already started at mean 100 (range 1-200), against starting a fresh chain \
at mean 1 (range 1-10). That is roughly 100:1 for continuing what is begun, so finish a line \
before opening another unless the new one is plainly better. They also weight a skill marked useful-always at 100, a \
situational one at 1, and one marked useless at 0.01 — a ten-thousand-fold spread. Treat \
anything tagged USELESS in the options below as effectively never worth a point, and prefer \
skills that help every turn over ones that help in a single kind of fight.

The thresholds above are CA's own AI/advisor numbers, advisory only — never orders. CA \
prices all 19 strategic resources identically: never claim gold outranks another resource \
because CA says so.

RULES. Choose ONLY from the enumerated options — never invent a key. For each building command, \
copy one exact `(region=..., slot=..., building=...)` tuple from a NEW option, or the exact \
triple on an UPGRADE option; do not derive, rename, or translate any field. On RETRY, correct \
the prior JSON by copying that exact triple. Use display names only in reply text. Never spend below the \
reserve floor. Prefer economy early. Commands are discarded unless he asked for action, so \
there is nothing to gain by issuing them uninvited. In your reply, refer to factions, \
regions, buildings and lords by their display names — never by an internal key.

Reply with ONLY a JSON object: {"reply": "<plain text, <=600 chars>", "skills": \
[{"cqi": <int>, "skill": "<key>", "why": "<8 words max>"}], "buildings": [{"region": \
"<key>", "slot_idx": <int>, "building": "<key>", "cost": <int>, "why": "<8 words max>"}]}. \
skills/buildings are optional (omit or empty list if none). No other text."""


_RECRUITMENT_ROLE_ALIASES = {
    "infantry": "infantry", "melee infantry": "melee_infantry",
    "ranged infantry": "ranged_infantry", "missile infantry": "missile_infantry",
    "cavalry": "cavalry", "shock cavalry": "shock_cavalry",
    "missile cavalry": "missile_cavalry", "melee cavalry": "melee_cavalry",
    "chariot": "chariots", "chariots": "chariots", "monster": "monsters",
    "monsters": "monsters", "artillery": "artillery", "war machine": "war_machine",
    "war machines": "war_machine",
}


def _recruitment_subject(message):
    match = re.search(r"\brecruit(?:ment)?(?:\s+(?:for|of))?\s+(.+?)\s*$", message or "", re.I)
    if not match:
        return ""
    return re.sub(r"[^a-z0-9]+", " ", match.group(1).lower()).strip()


def requested_purpose(message, labels=None, policy=None, rules=None):
    """Return (immutable request, problem).  Vague or unsupported requests do not guess."""
    low = " ".join((message or "").lower().replace("-", " ").split())
    found = set()
    if re.search(r"\b(income|money|revenue)\b", low):
        found.add("income")
    if re.search(r"\bgrowth\b", low):
        found.add("growth")
    if re.search(r"\b(public order|publicorder|control)\b", low):
        found.add("public_order")
    if re.search(r"\b(wall|walls|garrison)\b", low):
        found.add("settlement_defence")
    if re.search(r"\brecruit(?:ment)?\b", low):
        subject = _recruitment_subject(low)
        requested = {role for phrase, role in _RECRUITMENT_ROLE_ALIASES.items()
                     if subject == phrase}
        aliases = {_unit_name(name): keys for name, keys in ((policy or {}).get("unit_aliases") or {}).items()}
        stats = ((rules or {}).get("units") or {}).get("stats") or {}
        if subject and (subject in aliases or any(
                subject == _unit_name(stats.get("name"))
                for stats in stats.values() if isinstance(stats, dict))):
            found.add(("recruitment", "unit", subject))
        elif len(requested) == 1 and next(iter(requested)) in ((policy or {}).get("role_map") or {}):
            found.add(("recruitment", "role", next(iter(requested))))
        else:
            return None, "Name an exact known unit or a reviewed recruitment role, such as ranged infantry or cavalry."
    if len(found) == 1:
        found = found.pop()
        if isinstance(found, tuple):
            purpose, kind, value = found
            return {"purpose": purpose, "target": {"kind": kind, "value": value}}, None
        return {"purpose": found}, None
    if len(found) > 1:
        return None, "Choose one building purpose: income, growth, public order, recruitment, or settlement defence."
    if re.search(r"\b(defence|defense|military|economy)\b", low):
        return None, "Name one specific purpose: income, growth, public order, a recruitment role, or walls/garrison."
    return None, None


def _explicit_purpose_followup(message):
    low = " ".join((message or "").lower().split())
    return low in ACT_CONFIRMATIONS or low in {
        "do it", "go ahead", "make it so", "handle it", "sort it", "take care of it",
        "proceed", "execute",
    }


def pending_purpose(cid, turn, message):
    """Consume one explicit same-campaign, same-turn confirmation; unrelated chat expires it."""
    global _PENDING_PURPOSE
    pending = _PENDING_PURPOSE
    if not pending:
        return None
    if pending["campaign_id"] != cid or pending["turn"] != turn:
        _PENDING_PURPOSE = None
        return None
    if _explicit_purpose_followup(message):
        _PENDING_PURPOSE = None
        return pending["request"]
    _PENDING_PURPOSE = None
    return None


def remember_purpose(cid, turn, request):
    global _PENDING_PURPOSE
    request = _purpose_request(request)
    if isinstance(cid, str) and isinstance(turn, int) and request is not None:
        _PENDING_PURPOSE = {"campaign_id": cid, "turn": turn, "request": request}


# ---------------------------------------------------------------- numbered-options offer
#
# Owner, 2026-09-16 (DECISIONS.md ids alfred-20260916-reply-options,
# alfred-20260916-unsure-options): "also would be better if gemini responded with options
# like claude does" / "if unsure come back with option". Whenever chat() would otherwise
# leave the player to guess (several legal buildings fit, or a question with nothing named),
# it offers a short numbered list instead of a bare question — and a follow-up naming one BY
# NUMBER acts on it at once, the same way a single-legal-option request already does.
#
# The list Alfred actually presented must be the one a pick resolves against, or "2" could
# silently mean a different building than the one the player read. So the OFFERED subset —
# never re-derived, never re-ranked — is what gets remembered, exactly as with
# _PENDING_PURPOSE, and a turn/campaign mismatch discards it the same way.
_PENDING_OPTIONS = None
MAX_OFFERED_OPTIONS = 5


def _describe_option(rules, u):
    """One line for a numbered offer — display name, where, cost, and CA's own effect hint
    (income buildings get their payback-in-turns via _fx_str, same as the option list)."""
    levels = (rules.get("buildings") or {}).get("levels") or {}
    name = (levels.get(u["building"]) or {}).get("name") or u.get("name") or u["building"]
    bits = [f"cost {u['cost']}"]
    fx = _fx_str(u)
    if fx:
        bits.append(fx)
    return f"{name} at {_region_name(rules, u['region'])} ({', '.join(bits)})"


# "no" alone is too broad a substring match: "no gold" and "no problem" contain the word "no"
# without pushing back on anything. Restricted to "no" as the WHOLE message's leading word,
# only when followed by end-of-message, a comma, or a period — never when it leads straight
# into another word ("no gold"). The other pushback words stay plain substrings; they don't
# have "no"'s false-positive-as-a-determiner problem.
# REVERT CHECK (executed): reverting to the old bare `\bno\b` (matching "no" anywhere as its
# own word) makes the "no gold" test below fail — it would be misread as pushback.
_PUSHBACK_RE = re.compile(
    r"\b(not|isn't|isnt|didn't|didnt|wrong)\b|^\s*no\b(?:[,.]|\s*$)", re.I)

# The wrapper cannot cancel or undo a command it already wrote to aigen_commands.lua — that
# has to happen in-game. Used only against the fact that the last thing decided this turn was
# already sent (see chat()'s dedupe-collapse fix).
_CANCEL_RE = re.compile(r"\b(cancel|undo|revert|take\s+back|stop\s+that)\b", re.I)

# A question about which offered option costs least/most — the SYSTEM NOTE for these must
# tell the model to answer directly from the costs already printed in the option list,
# instead of just re-listing them (finding: "whats the cheapest option overall" went
# unanswered even though the cost was right there in the prompt).
COST_QUESTION_RE = re.compile(r"\b(cheap|cheapest|expensive|priciest|cost|costs|price)\b", re.I)

# A "<Name> at <Region> (cost <N>" phrase — the shape a cost/comparison reply's own numbered
# list uses (real-Gemini pass 2, finding #5: a "most expensive" answer invented two buildings
# and a fake region name in exactly this shape). Used by _reply_has_unlisted_building below.
_NAMED_OPTION_RE = re.compile(r"([A-Za-z][\w'’\-]*(?:\s+[A-Za-z][\w'’\-]*)*?)\s+at\s+"
                              r"([A-Za-z][\w'’\-]*(?:\s+[A-Za-z][\w'’\-]*)*?)\s*"
                              r"\(cost\s+(\d+)", re.I)


def _reply_has_unlisted_building(reply, rules, options):
    """A targeted hallucination check for a cost/comparison-question reply: a
    '<Name> at <Region> (cost <N>' phrase in the model's own text that names a building,
    region, or price combination that isn't one of the `options` it was actually offered is
    treated as invented. Deliberately narrow — it only catches this exact printed shape, the
    one the model itself used when it fabricated buildings — not a general fact-checker."""
    if not reply or not options:
        return False
    levels = (rules.get("buildings") or {}).get("levels") or {}
    legal = set()
    for u in options:
        name = (levels.get(u["building"]) or {}).get("name") or u.get("name") or u["building"]
        legal.add((name.strip().lower(), _region_name(rules, u["region"]).strip().lower(),
                   u.get("cost")))
    for m in _NAMED_OPTION_RE.finditer(reply):
        found = (m.group(1).strip().lower(), m.group(2).strip().lower(), int(m.group(3)))
        if found not in legal:
            return True
    return False


# "most expensive"/"cheapest" — used both to steer the SYSTEM NOTE and to post-check the
# reply actually named the TRUE extreme among what was offered (real-Gemini pass 3, finding
# #3: "and whats the most expensive one" got a truncated, correctly-real-but-INCOMPLETE list
# that quietly dropped the actual most expensive option).
_SUPERLATIVE_MIN_RE = re.compile(r"\bcheap(?:est)?\b", re.I)
_SUPERLATIVE_MAX_RE = re.compile(r"\b(most\s+expensive|priciest|expensive)\b", re.I)


def _reply_misses_superlative(reply, rules, options, message):
    """For a "most expensive"/"cheapest" question, the reply must name the option that is
    ACTUALLY the max/min cost among what was offered — a truncated or reordered re-list that
    quietly omits it answers a different, unasked question. Narrow like
    _reply_has_unlisted_building: only fires when the message asks for a specific extreme and
    `reply` never says that option's display name at all."""
    if not reply or not options:
        return False
    low = " ".join((message or "").lower().split())
    want_min = bool(_SUPERLATIVE_MIN_RE.search(low))
    want_max = bool(_SUPERLATIVE_MAX_RE.search(low))
    if not (want_min or want_max) or (want_min and want_max):
        return False
    target = (min if want_min else max)(options, key=lambda u: u.get("cost") or 0)
    levels = (rules.get("buildings") or {}).get("levels") or {}
    name = (levels.get(target["building"]) or {}).get("name") or target.get("name") \
        or target["building"]
    return name.strip().lower() not in reply.lower()


# Comparison/alternative vocabulary meaning "not the thing that was already sent" — used to
# stop the already-sent override from firing on a message that names (even vaguely) something
# ELSE (real-Gemini pass 3, finding #2: "the expensive one" after the cheap one shipped got
# told "that was already sent", which is misleading about the one actually asked for).
_DIFFERENT_TARGET_RE = re.compile(
    r"\b(other|another|different|instead|expensive|priciest|cheap|cheapest)\b", re.I)

# Bare/short state-metric queries (real-Gemini pass 3, finding #4: "income"/"treasury" are in
# chat_prompt_intent's own "question" set and still drifted into a building re-list without an
# explicit note; "upkeep percent"/"corruption level" aren't in that set at all and fell
# through to intent="both", which offers buildings outright). Filler words are stripped so
# "whats my public order like" reduces the same way a bare "public order" does.
_METRIC_FILLER_RE = re.compile(
    r"\b(whats|what's|what is|what|hows|how's|my|our|the|percent|level|like|doing|right now)\b",
    re.I)
_METRIC_KEYS = {"income": "income", "treasury": "treasury", "gold": "treasury",
                "money": "treasury", "upkeep": "upkeep", "public order": "public_order",
                "growth": "growth", "control": "control", "corruption": "corruption"}


def _metric_query(message):
    """A bare/short factual query naming a state metric with no act marker — the caller only
    uses this when `not may_act` already, so an order that happens to mention "income"
    ("build something for income") is never intercepted here."""
    low = " ".join((message or "").lower().split()).strip(" ?!.")
    stripped = " ".join(_METRIC_FILLER_RE.sub(" ", low).split())
    return _METRIC_KEYS.get(stripped)


def _state_metric_fact(state, metric):
    """The real number for a faction-wide metric, or None for a per-region one (public
    order/growth/control/corruption vary by region — no single faction figure to state, but
    the prompt already renders them per region, so the note points there instead)."""
    if metric == "income":
        return f"income is {state.get('income', '?')} gold/turn, net {state.get('net_income', '?')}"
    if metric == "treasury":
        return f"treasury is {state.get('treasury', '?')} gold"
    if metric == "upkeep":
        uip = state.get("upkeep_income_percent")
        return (f"upkeep is {uip}% of income" if isinstance(uip, (int, float))
                else "upkeep isn't in this state dump")
    return None


def _already_sent_phrasing(base_desc, message):
    """The tail of an "already sent" SYSTEM NOTE. Plain by default. When the player's
    message pushes back — "not", "isn't", "didn't", "no", "wrong" — the model is told to
    acknowledge the disagreement, explain the order already left this turn and will show in
    the settlement's build queue next time the panel refreshes, and offer to send it again
    only if he says so next turn, instead of just repeating "already sent" (real-Gemini
    replay wording bounce, 2026-09-16)."""
    if _PUSHBACK_RE.search(" ".join((message or "").lower().split())):
        return (base_desc + "; he seems to disagree — acknowledge that, explain the order "
                "already left this turn and will show in the settlement's build queue next "
                "time the panel refreshes, and offer to send it again only if he says so "
                "next turn")
    return base_desc + "; tell him it was already sent, do not send it again"


def _offer_note(cid, turn, rules, candidates, context):
    """Build the SYSTEM NOTE for 'present these options, best first' and remember exactly
    what was offered (capped, in this order) so a later pick resolves unambiguously.

    A single-item offer ends with "Say yes to order it" instead of "Pick one by number" —
    there is nothing to number when only one option exists (real-Gemini replay wording
    bounce, 2026-09-16)."""
    offered = list(candidates[:MAX_OFFERED_OPTIONS])
    remember_options(cid, turn, offered)
    lines = [f"{i + 1}) {_describe_option(rules, u)}" for i, u in enumerate(offered)]
    invite = ("Say yes to order it." if len(offered) == 1
              else "Pick one by number.")
    return (context + " Present exactly these options to him, numbered exactly as listed, "
            "best first, in your own words — never invent, guess, or reorder them:\n" +
            "\n".join(lines) +
            f"\nEnd with one short line inviting him to respond: {invite} Issue no "
            "building command until he does.")


def remember_options(cid, turn, options):
    global _PENDING_OPTIONS
    if isinstance(cid, str) and isinstance(turn, int) and options:
        _PENDING_OPTIONS = {"campaign_id": cid, "turn": turn, "options": list(options)}


def pending_options(cid, turn):
    """The list Alfred just offered, if it's still this exact campaign+turn — a stale list
    (an earlier turn, or a different campaign) is discarded, never guessed against."""
    global _PENDING_OPTIONS
    pending = _PENDING_OPTIONS
    if not pending:
        return None
    if pending["campaign_id"] != cid or pending["turn"] != turn:
        _PENDING_OPTIONS = None
        return None
    return pending["options"]


# The last single building candidate chat() decided on THIS turn — whether it fired, or was
# already sent — so a vague follow-up ("rerun it", "no its not queue") that narrows to
# nothing new can still be told "already sent" instead of a fresh numbered list. Bounce
# review, real Gemini replay: after an order went through, "rerun it" re-printed the
# numbered list instead of saying the thing it clearly meant was already done.
_LAST_SINGLE_CANDIDATE = None


def remember_last_candidate(cid, turn, u):
    global _LAST_SINGLE_CANDIDATE
    if isinstance(cid, str) and isinstance(turn, int):
        _LAST_SINGLE_CANDIDATE = {"campaign_id": cid, "turn": turn, "region": u["region"],
                                   "slot_idx": u["slot_idx"], "building": u["building"]}


def last_candidate(cid, turn):
    p = _LAST_SINGLE_CANDIDATE
    if not p or p["campaign_id"] != cid or p["turn"] != turn:
        return None
    return p


_ORDINAL_WORDS = {"first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5}

# Filler that wraps a direct order/confirmation/pick without changing what it means — "do it
# NOW", "JUST do it", "OK do it", "YES, do the second", "go for it PLS". Box 13 follow-up
# (coordinator, item 1): the plain forms already worked; these did not, because
# _QUESTION_OVERRIDE_RE/_SOLE_OPTION_CONFIRM_RE/_PICK_NUMBER_RE all matched the WHOLE message
# with no room for a leading/trailing pleasantry.
_CONFIRM_WRAP_LEAD_RE = re.compile(r"^(?:just|ok|okay|yes)[,\s]+", re.I)
_CONFIRM_WRAP_TAIL_RE = re.compile(r"\s+(?:now|please|pls)$", re.I)


def _strip_confirm_wrapper(low):
    """Strip one leading 'just'/'ok'/'okay'/'yes[,]' and one trailing 'now'/'please'/'pls'
    from an already-lowercased, whitespace-normalized message. Used ONLY to decide whether a
    message IS a direct order/confirmation/pick — never to build player-facing text."""
    out = _CONFIRM_WRAP_LEAD_RE.sub("", low, count=1)
    out = _CONFIRM_WRAP_TAIL_RE.sub("", out, count=1)
    return out


# The whole (normalized) message must match one of: a bare number/ordinal, or one of these
# small lead-in phrases followed by a number/ordinal, with only "one" and/or "please" allowed
# to trail. Anything else around the number means it wasn't a pick.
#
# "no, " is a lead-in too (box 13, real-Gemini pass 2, finding #4): "no, the second one" is an
# AFFIRMATION of a choice ("no [not what I said before], [I mean] the second one"), not a
# refusal — "no" alone, or "no" followed by anything that isn't a number/ordinal phrase (e.g.
# "no, don't"), still falls through to None here exactly as before, since the comma-plus-pick
# shape is the only thing this adds.
# "do the second"/"do 2" are pick lead-ins too (box 13, item 0: `_is_question`'s override
# whitelists these as orders, not questions — this is what then resolves WHICH one).
_PICK_LEAD = (r"(?:the\s+|option\s*#?\s*|number\s*#?\s*|#\s*|lets\s+do\s+|let's\s+do\s+|"
              r"go\s+with\s+|take\s+(?:the\s+)?|no,\s+(?:the\s+)?|do\s+(?:the\s+)?)?")
_PICK_TAIL = r"(?:\s+one)?(?:\s+please)?"
_PICK_NUMBER_RE = re.compile(
    _PICK_LEAD + r"(?:(\d+)(?:st|nd|rd|th)?|(first|second|third|fourth|fifth))" + _PICK_TAIL)


def _extract_pick_number(message):
    """The plain 1-based number a message seems to be picking, regardless of whether it is
    in range — used both to resolve a pick and to catch an OUT-OF-RANGE one ("2" with only
    one option offered), so Alfred can say so instead of silently doing something else, or
    silently doing nothing.

    MEASURED 2026-09-16 (bounce review, round 2): the previous version was a bare
    `\\b(\\d+)\\b` search anywhere in the message, so "build 2 farms" or "what about slot 2"
    picked option 2 by accident. Fixed to require the digit to BE the whole message, or
    immediately follow "option"/"number"/"#" with nothing after it.

    MEASURED 2026-09-16 (bounce review, round 3): ordinal WORDS ("first", "second", "1st")
    were still matched anywhere in the message with `re.search`, so "first i need money" and
    "second, what about walls" issued real builds while an offer was pending. Ordinals now
    get the same whole-message discipline as digits: the entire (normalized) message must be
    a bare number/ordinal, or one of a short list of natural lead-ins ("option 2 please",
    "lets do 2", "go with 2", "take the first", "the second one") — never a number or ordinal
    sitting inside an unrelated sentence."""
    low = " ".join((message or "").lower().split())
    stripped = _strip_confirm_wrapper(low.strip(" ?!."))
    m = _PICK_NUMBER_RE.fullmatch(stripped)
    if not m:
        return None
    if m.group(1):
        return int(m.group(1))
    return _ORDINAL_WORDS[m.group(2)]


_SOLE_OPTION_CONFIRM_RE = re.compile(
    r"^(?:yes|yep|yeah|yup|go|ok|okay|sure|confirm|y|do\s+it|do\s+that|go\s+for\s+it)$", re.I)


def _parse_pick(message, options):
    """A bare number ("2"), "option 2", an ordinal word/suffix ("the second", "1st"), or a
    message that names exactly one offered option, resolved to its 0-based index — or None
    when nothing in `options` is clearly meant, OR the picked number is out of range (the
    caller checks that separately via _extract_pick_number, to report "no option N" rather
    than silently falling through)."""
    if not options:
        return None
    n = _extract_pick_number(message)
    if n is not None:
        return n - 1 if 1 <= n <= len(options) else None
    low = " ".join((message or "").lower().split())
    # "do it"/"do that"/"go for it" (and the plain bare confirmations) name no number, so
    # they can only ever resolve when there's exactly ONE thing they could mean.
    if len(options) == 1 and _SOLE_OPTION_CONFIRM_RE.match(
            _strip_confirm_wrapper(low.strip(" .!"))):
        return 0
    # COST VOCABULARY: "the cheap one"/"the cheapest" is unambiguous — resolve to whichever
    # offered option actually costs least. "the expensive one"/"the best one" is deliberately
    # NOT handled here: "best" is subjective (not literally the highest-priced option) and
    # resolving it would be a guess dressed up as a pick, so those fall through to None and
    # the caller re-lists instead.
    if re.search(r"\bcheap(?:est)?\b", low):
        return min(range(len(options)), key=lambda i: options[i].get("cost") or 0)
    hits = [i for i, u in enumerate(options)
            if (u.get("name") and u["name"].lower() in low)
            or u["building"].replace("_", " ").lower() in low]
    return hits[0] if len(hits) == 1 else None


def _find_option(ups, u):
    """The CURRENT legal dict matching a remembered option's triple, or None if the state
    moved on and it's no longer legal (slot filled, turn advanced, funds changed the list)."""
    return next((x for x in ups if x["region"] == u["region"]
                 and x["slot_idx"] == u["slot_idx"] and x["building"] == u["building"]), None)


def chat_prompt_intent(message):
    """Select a render-only chat view; unknown or mixed requests keep every option."""
    low = " ".join((message or "").lower().split())
    skills = bool(re.search(r"\b(skill|talent|spec|level\s+up|skill\s+up|put\s+points|"
                            r"assign)\b", low))
    buildings = bool(re.search(r"\b(build|building|upgrade|construct|queue|settlement|slot|"
                               r"barracks|farm|port|plaza)\b", low))
    if skills == buildings:
        status = re.sub(r"[^a-z0-9]+", " ", low.replace("\'", "").replace("’", "")).strip()
        return "question" if status in {
            "situation", "status", "income", "treasury", "war", "at war", "armies",
            "public order", "control", "whats the situation", "what is the situation",
            "how are we doing",
        } else "both"
    return "skills" if skills else "buildings"


def build_chat_prompt(rules, state, message, history=(), journal=(), prefs=None, intent=None,
                      purpose=None):
    prompt, ups = build_prompt(rules, state, journal, prefs,
                               intent or chat_prompt_intent(message), purpose)
    if history:
        # Alfred was single-shot until now: he could not follow up on his own last answer.
        # Prior turns go in before the current message so "and the other thing?" resolves.
        prompt += "\n\nCONVERSATION SO FAR (oldest first):\n" + "\n".join(
            f"Player: {t.get('you','')}\nYou: {t.get('alfred','')}" for t in history)
    prompt += "\n\nPLAYER MESSAGE:\n" + message
    return prompt, ups


# ------------------------------------------------------------------- B7: preferences
#
# Owner's frame: Alfred should understand "the player's preferences". The obvious build —
# let him tell Alfred in chat and store what Alfred understood — is the one thing this must
# not do. Chat history already goes into the prompt verbatim, so a stored free-text
# preference is durable prompt injection against Alfred's own rules, written by whoever
# types into the box. So preferences are an ENUM THE DAEMON OWNS. The model never authors
# one; Python recognises an exact phrase and sets a value from a fixed list.
PREFS = os.path.join(data_dir(ROOT), "prefs.json")

# The floor lives HERE, in Python, not in the Lua dump it used to be read from. The dump is
# a trusted-client value: whatever the mod says the floor is, validate() believed. A
# preference that could lower it would then be a preference that widens a hard gate. It
# cannot: prefs may RAISE the floor and never lower it, and a dumped value below this
# constant is ignored.
RESERVE_FLOOR_MIN = 1500

# ---------------------------------------------------------------- capability checkboxes
#
# Owner ruling 2026-09-01: "make the capabilities checkboxes so alfred knows what to suggest
# and do". A capability is a checkbox, not a rung on a ladder: the spec's Advisor/Steward/
# Governor/Lord-Administrator tiers forced capabilities to arrive in a fixed order, so he had
# to accept army movement to get recruitment. He can now take one and refuse the other.
#
# ONLY WHAT IS REAL GOES IN THIS TABLE. A checkbox for something Alfred cannot actually do is
# a lie told by the UI, so a capability is added here in the same change that makes it work —
# never in advance as a placeholder. Today that is buildings and skills, both owner-witnessed
# in-game. Recruitment and movement have APIs named in SPEC.md and NEVER run, so they are not
# here yet; see the blast-radius note below for why that ordering matters more than usual.
#
# BLAST RADIUS is recorded per capability because it is the thing that decides how much
# caution a capability deserves, and it is not obvious from the name. Buildings and skills are
# RECOVERABLE: gold comes back, a skill point is one point. Movement and diplomacy are not — a
# lord walked into a doomstack loses a campaign and no setting undoes it.
CAPABILITIES = {
    "buildings": ("construct and upgrade buildings",
                  "recoverable: gold and a build slot"),
    "skills": ("spend lord and hero skill points",
               "recoverable: one skill point"),
}

# Both default ON: they are what Alfred already does, and this change must not silently take
# away working behaviour. Turning one OFF is the new power.
CAP_DEFAULTS = {k: True for k in CAPABILITIES}


def clamp_caps(got):
    """Coerce anything into a full set of real booleans, on READ as well as write.

    Unknown keys are DROPPED rather than carried: a capability that no longer exists in this
    build must not keep an entry that some later table lookup would treat as enabled."""
    out = dict(CAP_DEFAULTS)
    if isinstance(got, dict):
        for k in CAPABILITIES:
            if k in got:
                out[k] = bool(got[k])
    return out


CAP_PHRASE = re.compile(
    r"\b(enable|disable|turn on|turn off|allow|stop)\b[^.]{0,30}?\b(buildings?|skills?)\b")


# ---------------------------------------------------------------- tone (was: the TARS sliders)
#
# Owner ruling 2026-09-01 on: trust+discretion -> authority (Box F); 2026-09-05: humour
# removed (Box H); 2026-09-16: authority removed (alfred-20260916-question-never-acts),
# leaving honesty as the last slider. 2026-09-17 (alfred-20260917-no-sliders-strip-bloat),
# owner verbatim: "just remove all sliders for now". Honesty is gone the same way the rest
# went: not read from anywhere, not on the MCT page, not settable in chat. An old prefs.json
# or an older pack's dump carrying a "sliders" key (honesty or any earlier name) loads fine
# and the key is simply never read — no code anywhere still looks for it.
#
# Tone is now ONE FIXED LINE (see TONE_LINE below): honest and direct, never softens a hard
# truth. HARD_TRUTHS is kept — it is not slider machinery, it is the standing carve-out that
# a bad plan is always called out regardless of tone.
HARD_TRUTHS = ("a payback longer than the campaign horizon", "a spend that breaches the "
               "reserve floor", "a build the game will refuse")

TONE_LINE = ("Be honest and direct. Always state plainly, never softened: " +
             ", ".join(HARD_TRUTHS) + ". Narrate moderately: report what matters — a real "
             "choice, a risk, a spend — without a line for every routine move.")


STANCES = {
    "economy": "Prefer economy buildings; take growth and income over military.",
    "military": "Prefer military buildings and recruitment capacity over pure economy.",
    "hold": "Hold cash. Spend only on clearly high-value options.",
    "balanced": "No stated preference; judge each option on its merits.",
}

# Exact phrases the daemon recognises. Deterministic Python, never the model's reading of
# what was meant — the whole point is that the stored value cannot be argued into existence.
STANCE_PHRASES = {
    "prefer economy": "economy", "focus economy": "economy", "economy first": "economy",
    "prefer military": "military", "focus military": "military",
    "military first": "military",
    "hold cash": "hold", "stop spending": "hold", "save money": "hold",
    "no preference": "balanced", "balanced": "balanced",
}

# A question mark, a leading WH/aux word, or a "worth it"-shaped phrase anywhere. WIDENED
# 2026-09-16 (real-Gemini replay, box 13 item 0): the old leading list was just
# what/which/who/when/why/how plus a few exact multi-word phrases, so mark-less phrasings
# like "is the elven workshop worth it" and "whats my public order like" — neither starting
# with a listed word nor carrying a "?" — fell all the way through to `allow_commands` reading
# their act marker ("build"/"buy" is never IN these two messages, but the single-legal-option
# and purpose-narrowing shortcuts don't require one) and spent real gold on a pure question.
# Bare leading aux words (is/are/do/does/did/can/could/should/would/will/shall) now count too
# — they used to be deliberately excluded because "do it"/"go ahead" collide with them, which
# is what _QUESTION_OVERRIDE_RE below exists to carve back out.
_QUESTION_RE = re.compile(
    r"\?|^\s*(what's|whats|what|which|who's|whos|who|when|where|why|how's|hows|how|"
    r"is|are|am|was|were|do|does|did|can|could|should|would|will|shall|any|"
    r"tell\s+me|explain|show\s+me)\b"
    r"|\b(worth\s+it|worth\s+building|good\s+idea|any\s+point)\b", re.I)

# A message that LOOKS like it starts with a question word but is actually a direct order or
# confirmation — "do it", "do that", "do the second", "do 2", "do both", "go for it". Matched
# against the WHOLE (normalized) message only, so a question that merely contains one of these
# words ("do you think I should do it eventually?") never slips past the question rule.
_QUESTION_OVERRIDE_RE = re.compile(
    r"^(?:do\s+(?:it|that|both|the\s+(?:first|second|third|fourth|fifth|\d+)|\d+)"
    r"|go\s+for\s+it)\s*[.!]?$", re.I)


def _is_question(message):
    low = " ".join((message or "").lower().split()).strip(" .!")
    # box 13 follow-up, item 1: "do it now"/"just do it"/"ok do it"/"go for it pls" are the
    # SAME override, just wrapped in a pleasantry the un-stripped match couldn't see through.
    if _QUESTION_OVERRIDE_RE.match(_strip_confirm_wrapper(low)):
        return False
    return bool(_QUESTION_RE.search(low))


# Words that mean "do it", as opposed to "tell me about it". See allow_commands.
ACT_MARKERS = (
    "build", "upgrade", "construct", "spend", "buy", "queue",
    # "level" ALONE is a topic word: "is that a good level of income" is a question, and it
    # granted permission to act until 2026-09-01. Only the instruction forms remain.
    "level up", "level him", "level her", "level them",
    "skill up", "spec", "put points", "assign",
    "do it", "go ahead", "make it so", "handle it", "sort it", "take care of it",
    "act", "execute", "proceed",
)

# Bare confirmations, matched as WHOLE WORDS. MEASURED 2026-09-01 01:19: Alfred proposed a
# building and ended with "say go and I'll queue it"; the owner replied "yes"; the model
# emitted the command — and this gate DROPPED it, because "yes" and "go" were in neither
# list. A gate that refuses the answer to the question the assistant just asked is broken,
# not safe. Whole-word so "yesterday" and "going" do not count.
# NOT "do" or "please": "what should i do" and "tell me please" are questions, and this
# list must not turn a question into an authorisation. "do it" is already in ACT_MARKERS.
ACT_CONFIRMATIONS = ("yes", "yep", "yeah", "yup", "go", "ok", "okay", "sure", "confirm", "y")


def load_prefs(cid):
    """Preferences for one campaign. Fails soft, and refuses to serve another campaign's."""
    out = {"stance": "balanced", "reserve_floor": RESERVE_FLOOR_MIN,
                       "capabilities": dict(CAP_DEFAULTS)}
    if cid is None:
        return out
    try:
        with open(PREFS) as f:
            blob = json.load(f)
        got = blob.get(cid) if isinstance(blob, dict) else None
    except Exception:
        got = None
    if isinstance(got, dict):
        if got.get("stance") in STANCES:
            out["stance"] = got["stance"]
        if isinstance(got.get("mct_seen"), dict):
            out["mct_seen"] = got["mct_seen"]
        # "sliders" (honesty, or any earlier slider name) is read from neither an old
        # prefs.json nor a dumped settings blob any more — the key loads fine and is simply
        # ignored (alfred-20260917-no-sliders-strip-bloat).
        out["capabilities"] = clamp_caps(got.get("capabilities"))
        rf = got.get("reserve_floor")
        # RAISE-ONLY, enforced on read as well as on write: a hand-edited file must not be
        # able to lower the floor either.
        if isinstance(rf, int) and rf > RESERVE_FLOOR_MIN:
            out["reserve_floor"] = rf
    return out


def save_prefs(cid, prefs):
    if cid is None:
        return
    try:
        try:
            with open(PREFS) as f:
                blob = json.load(f)
        except Exception:
            blob = {}
        if not isinstance(blob, dict):
            blob = {}
        blob[cid] = prefs
        os.makedirs(os.path.dirname(PREFS), exist_ok=True)
        with open(PREFS, "w") as f:
            json.dump(blob, f)
    except Exception as e:
        print(f"  (prefs not saved: {e})")


def apply_pref_phrases(cid, message, prefs):
    """Recognise an exact preference instruction in the player's own words and store it.

    Returns (prefs, note) — note is plain text for the reply so a stored preference is never
    silent. Python does the recognition: the model is never asked what the player meant, so
    it cannot be talked into storing something the player did not say."""
    low = " ".join((message or "").lower().split())
    new, note = dict(prefs), None
    for phrase, stance in STANCE_PHRASES.items():
        if phrase in low:
            new["stance"] = stance
            note = f"Noted: {STANCES[stance]}"
            break
    # 1-7 digits, not 3-7: "set the floor to 0" is exactly the instruction that must be
    # recognised and REFUSED out loud, and a 3-digit minimum silently ignored it instead.
    m = re.search(r"(?:reserve|floor|keep\s+(?:the\s+)?(?:floor|reserve))\D{0,20}?(\d{1,7})",
                  low)
    if m:
        want = int(m.group(1))
        if want > new["reserve_floor"]:
            new["reserve_floor"] = want
            note = ((note + " ") if note else "") + f"Reserve floor raised to {want}."
        elif want < RESERVE_FLOOR_MIN:
            note = ((note + " ") if note else "") + (
                f"I can raise the reserve floor but not lower it below "
                f"{RESERVE_FLOOR_MIN} — leaving it at {new['reserve_floor']}.")
    # No sliders left to recognise from chat (alfred-20260917-no-sliders-strip-bloat).

    # THE CAPABILITY CHECKBOXES. Same deterministic treatment, and deliberately narrow: the
    # MCT page is the real surface for these, this exists so he can say it in the chat window
    # without opening a menu mid-turn.
    cp = dict(clamp_caps(new.get("capabilities")))
    changed = []
    for m in CAP_PHRASE.finditer(low):
        verb, what = m.group(1), m.group(2).rstrip("s")
        key = "buildings" if what == "building" else "skills"
        on = verb in ("enable", "turn on", "allow")
        if cp.get(key) != on:
            cp[key] = on
            changed.append(f"{key} {'on' if on else 'off'}")
    if changed:
        new["capabilities"] = cp
        note = ((note + " ") if note else "") + "Capabilities: " + ", ".join(changed) + "."

    if note:
        save_prefs(cid, new)
    return new, note


def apply_dumped_settings(cid, state, prefs):
    """Take the harness controls (capability checkboxes) from the state dump, if the player
    moved one.

    TWO WRITERS, ONE OWNER. He can set a checkbox in the MCT menu or say it in chat, and both
    must work without fighting. The rule: PREFS ARE THE VALUE, the dump is an EVENT. A dumped
    setting is applied only when it DIFFERS from what the dump last said — so moving a
    checkbox in the menu changes it, while a menu that keeps reporting its unchanged default
    does not overwrite something set in chat five minutes ago. Without that, every dump would
    silently revert a chat command, which is the kind of bug that looks like the model
    ignoring you.

    Everything is re-clamped: clamp_caps runs on whatever arrives, so a modded or older MCT
    cannot put a junk value into a gate. MCT is an input surface, never the authority — the
    standing rule for this harness.
    """
    got = (state or {}).get("settings")
    if not isinstance(got, dict):
        return prefs, None
    seen = prefs.get("mct_seen") or {}
    # "sliders" in a dump (this pack's or an older one's) is not read here any more — no
    # slider gate exists to apply it to (alfred-20260917-no-sliders-strip-bloat).
    new_cp = clamp_caps(got.get("caps"))
    old_cp = clamp_caps(seen.get("caps"))
    changed = []
    out = dict(prefs)
    if new_cp != old_cp:
        cur = clamp_caps(out.get("capabilities"))
        for k, v in new_cp.items():
            if v != old_cp.get(k) and v != cur.get(k):
                cur[k] = v
                changed.append(f"{k} {'on' if v else 'off'}")
        out["capabilities"] = cur
    # mct_seen must be persisted on EVERY dump, even when nothing applied — a value skipped
    # by the per-key guard above (because it already matched the current pref) must still
    # update "last seen", or a LATER genuinely-changed dump gets compared against a stale
    # snapshot and reverts a chat command with an untouched menu (F2).
    out["mct_seen"] = {"caps": new_cp}
    if not changed:
        if out.get("mct_seen") != seen:
            save_prefs(cid, out)
        return out, None
    save_prefs(cid, out)
    return out, "From the menu: " + ", ".join(changed) + "."


def effective_floor(state, prefs):
    """The floor validate() actually enforces: the Python constant, the dumped value and
    the preference, whichever is HIGHEST. Never lower than RESERVE_FLOOR_MIN."""
    floors = [RESERVE_FLOOR_MIN, prefs.get("reserve_floor") or 0]
    dumped = (state or {}).get("reserve_floor")
    if isinstance(dumped, int):
        floors.append(dumped)
    return max(floors)


def affordable(ups, state, prefs):
    """The subset of `ups` chat() may actually OFFER or auto-issue this turn — same floor
    validate() enforces (effective_floor). Bounce fix, 2026-09-17
    (alfred-20260917-faction-rules-affordability, box 16): build_prompt's own render only
    trims the province/UPGRADE/NEW catalog text; chat()'s numbered pick list (_offer_note)
    and its single-candidate auto-issue path both still worked off the UNFILTERED `ups` —
    so a 2500g option on a 1500g budget was still numbered "1) ..." and told to the model
    verbatim, and a lone unaffordable candidate was auto-issued with only validate() left to
    catch it. Callers use this result ONLY for what to present/issue; `ups`/`legal` handed to
    validate()/_find_option stays the full, unfiltered set, so a NAMED pick of a legal-but-
    over-budget option still gets the real reserve-floor reason via the existing FALSE_CLAIM
    re-ask instead of a silent "not enumerated" drop."""
    floor = effective_floor(state, prefs)
    budget = (state.get("treasury") or 0) - floor
    return [u for u in ups if u["cost"] <= budget]


# Words that REVERSE an instruction. Without these the gate reads a refusal as permission.
# What ENDS a refusal: a sentence break, or a word that explicitly turns to the alternative.
# A comma is NOT here — "don't spend, buy the farm" reads as one refusal to most people.
CLAUSE_BREAK = r"(?:[.;!?]|\b(?:but|instead|however|rather|then)\b)"

NEGATORS = ("dont", "don t", "do not", "never", "no", "not", "stop", "avoid", "without",
            "rather not", "hold off", "leave it")


def _acts_unnegated(low):
    """Is there an act marker that is NOT under a negation?

    MEASURED 2026-09-01: the old test was `any(m in low for m in ACT_MARKERS)` — a plain
    SUBSTRING match with no negation handling — and it returned True for all of:
        "i don't want to build anything"      (a refusal read as permission)
        "no, don't spend anything"            (same)
        "please do not buy the port"          (same)
        "which character should get points"   ("act" is inside "char-act-er")
    So the gate whose entire job is to stop Alfred acting unasked was granting permission on
    sentences telling him NOT to act. Three things fix it:

      1. WORD BOUNDARIES, so "act" does not match inside "character".
      2. NEGATION, checked per OCCURRENCE rather than per message: "don't build the farm,
         build the barracks" must still act, because a message may refuse one thing and order
         another.
      3. A NEGATOR BINDS TO THE NEAREST FOLLOWING MARKER and cannot reach past one. A plain
         four-word window could not express both cases above at once — "don't" sits three
         words before "build" in the first and four before the second "build" in the third,
         so any fixed window got one of them wrong. Stopping the lookback at the previous
         marker is what actually separates them.

    Conservative by design: when every marker is negated this returns False, and the worst
    that costs is Alfred answering instead of spending, which this gate's docstring already
    accepts. It can still only ever SUBTRACT.

    DIRECT BINDING NARROWED (2026-09-16, coordinator follow-up item 4, real-Gemini pass 3):
    "no gold left, build the farm" did not act — the old 4-word lookback found "no" three
    words back and read it as refusing "build", when "no" is really a determiner on "gold"
    ("no gold left"), not a refusal at all. A negator now binds a marker DIRECTLY only when
    it is the LAST negator before the marker in its clause AND nothing — a comma, "so", or
    more than 3 words — sits between the negator and the marker. "no gold left, build the
    farm" now acts (the comma breaks "no"'s reach); "no, build the farm" now acts too (the
    comma right after "no" reads as rejecting something else, same as "no, the second one"
    elsewhere reads as an affirmed pick, not a refusal); "dont build the farm" still doesn't
    (nothing breaks the binding). Clause-scoped PROPAGATION — a negation carrying forward to
    a LATER, un-preceded marker in the same clause ("don't build the farm, build the
    barracks") — is separate and unchanged; only the direct-binding half of the check is
    narrowed."""
    low = low.replace("'", "").replace("’", "")  # once, so "don't" matches the "dont" negator
    neg = "|".join(n.replace(" ", r"\s+") for n in NEGATORS)
    hits = []
    for m in ACT_MARKERS:
        pat = re.escape(m).replace(r"\ ", r"\s+")
        hits += [(h.start(), h.end()) for h in re.finditer(r"\b" + pat + r"\b", low)]
    hits.sort()
    prev_end, prev_negated = 0, False
    for i, (st, en) in enumerate(hits):
        lo = max(prev_end, st - 40)
        # A clause break also ENDS the lookback, not just the carried refusal. Otherwise the
        # window reads "no farm; upgrade the port" as a refusal to upgrade, because "no" is
        # two words back across the semicolon. Same rule, applied consistently: a refusal
        # does not survive its own clause.
        brk = [m.end() for m in re.finditer(CLAUSE_BREAK, low[:st])]
        if brk:
            lo = max(lo, brk[-1])
        seg = low[lo:st]
        # DIRECT CHECK: the CLOSEST negator to this marker within its clause, binding only
        # if nothing separates it from the marker — a comma, "so", or more than 3 words.
        neg_hits = list(re.finditer(r"\b(" + neg + r")\b", seg))
        negated = False
        if neg_hits:
            between = seg[neg_hits[-1].end():]
            if ("," not in between and not re.search(r"\bso\b", between)
                    and len(between.split()) <= 3):
                negated = True
        # NEGATION SCOPES TO THE CLAUSE (propagation, separate from the direct check above).
        # A four-word lookback alone let the second verb of a conjoined refusal walk free —
        # "don't build or upgrade anything" negated `build` and acted on `upgrade`. Chaining
        # across bare conjunctions fixed that pair but not "never build or recruit or spend",
        # because `recruit` is not a marker and broke the chain. Rather than keep adding words
        # to a pattern — which is writing a parser badly — the refusal simply RUNS UNTIL A
        # CLAUSE ENDS, and a comma is NOT a clause break for this half of the check.
        #
        # DELIBERATE CONSEQUENCE, stated rather than discovered: "don't build the farm, build
        # the barracks" no longer acts. A comma is not a clause boundary here, so the whole
        # sentence reads as a refusal and Alfred answers instead of spending. That is the
        # failure this gate's docstring already accepts, and the owner recovers by saying
        # "build the barracks" — three words. The reverse error spends his gold.
        if prev_negated and not re.search(CLAUSE_BREAK, low[prev_end:st]):
            negated = True
        if not negated:
            return True
        prev_negated = True
        prev_end = en
    return False


def allow_commands(message):
    """Did the player actually ask Alfred to DO something, for the BUILDINGS path (chat()'s
    skill path has its own separate act-gate and is unaffected by this function)?

    This rule existed only inside CHAT_SYSTEM — a sentence in a prompt, which a stored
    preference or a persuasive message could argue against. It is a Python gate now, and it
    can only ever SUBTRACT commands: a sweep is inherently an instruction to act and is not
    routed through here, so the worst this does is make Alfred answer instead of spending.

    A QUESTION IS NEVER AN ORDER, full stop (2026-09-16 ruling, alfred-20260916-question-
    never-acts, owner verbatim: "Just answer. No a question is a question. Remove authority
    slider for now") — not via a bare act-marker substring landing inside it ("what should i
    build next?" contains "build"), not via a bare confirmation word landing inside it
    ("should i go with the plaza?" contains "go"), and not via anything else: there used to
    be an authority slider whose high band treated a recommendation-question as standing
    permission to act; it is gone, along with that exception. A question about buildings
    always gets an answer (and, when it names or narrows to a legal option, an offer to act
    on it — see chat()'s numbered-options path), never a command on its own.

    Otherwise: an explicit instruction ("build the barracks", "do it") or a bare confirmation
    ("yes", "go") acts; anything else does not. The invariant holds regardless: this never
    CREATES a command, it only decides whether to keep ones the model already produced, and
    validate() still refuses anything illegal, unaffordable, below the reserve floor, or in
    an unchecked capability.
    """
    if _is_question(message):
        return False
    low = " ".join((message or "").lower().split())
    if _acts_unnegated(low):
        return True
    words = set(re.findall(r"[a-z']+", low))
    return any(c in words for c in ACT_CONFIRMATIONS)


# ---------------------------------------------------------------- B6: campaign journal
#
# Owner's frame (2026-08-31): "the mod should have a per campaign memory and also a better
# understanding of the campaign, game and player's preferences."
#
# The four-layer answer he was given: what a thing CAN do and what it DOES belong in files
# (that is the rulebook). What a thing is WORTH does not — GDP is an abstract contribution
# CA aggregates with tax, tech, subculture and province, so a modelled gold figure would be
# invented, it would drift every patch, and it would be wrong under any overhaul. So value
# is MEASURED here instead: journal what the treasury and income actually did, alongside
# what was issued, and let the record say what a building was worth IN THIS CAMPAIGN.
#
# His own standing law applies hard: ONE READING IS NOT A STATE. Income moves for many
# reasons in a turn — conquest, corruption, tax changes, several buildings completing at
# once — so a single before/after delta proves nothing and is never reported as if it did.
JOURNAL = os.path.join(data_dir(ROOT), "journal.json")
JOURNAL_KEEP = 200        # turns retained per campaign
ECON_MIN_SAMPLES = 3      # agreeing samples before a per-chain figure is stated at all


def load_journal(cid):
    """Turn entries for one campaign, oldest first. Fails soft to []."""
    if cid is None:
        return []
    try:
        with open(JOURNAL) as f:
            blob = json.load(f)
    except Exception:
        return []
    if not isinstance(blob, dict):
        return []
    rows = blob.get(cid)
    return rows if isinstance(rows, list) else []


def save_journal(cid, rows):
    if cid is None:
        return
    try:
        try:
            with open(JOURNAL) as f:
                blob = json.load(f)
        except Exception:
            blob = {}
        if not isinstance(blob, dict):
            blob = {}
        blob[cid] = rows[-JOURNAL_KEEP:]
        os.makedirs(os.path.dirname(JOURNAL), exist_ok=True)
        with open(JOURNAL, "w") as f:
            json.dump(blob, f)
    except Exception as e:
        print(f"  (journal not saved: {e})")


def _merge_issued(carried, cmds):
    """This turn's issued commands, earlier ones kept, deduped by (kind, what).

    Deduped because the sweep records the same turn twice and the mod's own carry-forward can
    re-present a command that has not executed yet; counting one build twice would inflate the
    only measurement this project has of what a building was actually worth."""
    out, seen = [], set()
    for c in list(carried) + [c for c in cmds if c.get("kind") in ("building", "skill")]:
        what = c.get("what") if "what" in c else (c.get("building") or c.get("skill"))
        key = (c.get("kind"), what)
        if key in seen:
            continue
        seen.add(key)
        out.append({"kind": c.get("kind"), "what": what, "cost": c.get("cost")})
    return out


def _prior_rows(journal, turn):
    """Journal rows strictly before this turn — works whether `journal` already carries
    a same-turn stub (sweep's own journal_record call) or not (chat's load_journal)."""
    if not isinstance(turn, int):
        return []
    return [r for r in journal if isinstance(r.get("turn"), int) and r["turn"] < turn]


def render_full(state, prior_rows):
    """BOX D: does this sweep need the whole empire re-rendered, or can it lean on history?

    Full on: no history yet (first sweep of a campaign — nothing to delta against), the
    dump's own schema changed since the last row (a mod update may have changed what a
    field means, so a stale delta could misdescribe the campaign), or every 10th turn (a
    delta-only prompt would otherwise never re-state ground truth, and small per-turn
    rounding could drift silently forever)."""
    if not prior_rows:
        return True
    if prior_rows[-1].get("schema") != state.get("schema"):
        return True
    turn = state.get("turn")
    if isinstance(turn, int) and turn % 10 == 0:
        return True
    return False


def _unspent_points(state):
    total = 0
    for c in (state.get("characters") if isinstance(state.get("characters"), list) else []):
        p = c.get("points")
        if isinstance(p, int):
            total += p
    return total


def delta_lines(state, prior):
    """BOX D: what changed since the last sweep, in <=6 lines total (header included).

    Deliberately narrower than econ_summary (the whole-campaign trend) — this is one hop:
    last row to now. Any field absent or non-numeric on either side is simply not spoken to,
    same fail-soft shape as the rest of this file."""
    if prior is None:
        return []
    lines = [f"\nSINCE LAST SWEEP (turn {prior.get('turn', '?')} -> {state.get('turn', '?')}):"]

    def d(cur, prev):
        return cur - prev if isinstance(cur, (int, float)) and isinstance(prev, (int, float)) \
            else None

    money = []
    for label, key, cur in (("treasury", "treasury", state.get("treasury")),
                             ("income", "income", state.get("income")),
                             ("net", "net_income", state.get("net_income"))):
        delta = d(cur, prior.get(key))
        if delta is not None:
            money.append(f"{label} {'+' if delta >= 0 else ''}{int(delta)}")
    if money:
        lines.append(", ".join(money))
    issued = prior.get("issued") or []
    if issued:
        lines.append("Issued last sweep: " + ", ".join(i.get("what") or "?" for i in issued[:6]))
    rdelta = d(len(state.get("regions") or []), prior.get("regions"))
    if rdelta:
        lines.append(f"Regions {'+' if rdelta >= 0 else ''}{int(rdelta)}")
    pdelta = d(_unspent_points(state), prior.get("points"))
    if pdelta:
        lines.append(f"Unspent skill points {'+' if pdelta >= 0 else ''}{int(pdelta)}")
    dip = state.get("diplomacy") if isinstance(state.get("diplomacy"), dict) else {}
    cur_war = set(dip.get("at_war_with") or []) if isinstance(dip.get("at_war_with"), list) else set()
    prior_war = set(prior.get("at_war_with") or [])
    if cur_war != prior_war:
        parts = []
        gained, ended = cur_war - prior_war, prior_war - cur_war
        if gained:
            parts.append("new war with " + ", ".join(sorted(gained)))
        if ended:
            parts.append("peace with " + ", ".join(sorted(ended)))
        if parts:
            lines.append("; ".join(parts))
    return lines[:6]


def journal_record(cid, state, cmds=()):
    """Append (or replace) this turn's entry and return the trimmed journal.

    TURN REGRESSION TRIMS FORWARD ENTRIES. cm:set_saved_value rolls back with the savegame
    but .cache does not, so loading an older save leaves the journal describing a future
    that no longer exists — Alfred would confidently recount spends that never happened in
    the campaign now being played. Anything recorded at a turn later than the one actually
    on screen is dropped."""
    if cid is None:
        return []
    turn = state.get("turn")
    if not isinstance(turn, int):
        return load_journal(cid)
    prior = load_journal(cid)
    # SAME-TURN MERGE, not replace. This function drops any row at or after `turn` and appends
    # a fresh one, which was correct while only the sweep called it — the sweep calls it twice
    # per turn and the second call carries the commands. The chat path now calls it too, and
    # the owner can chat several times in one turn: without merging, the second exchange's row
    # would silently erase what the first one issued, and the economics summary attributes
    # value using exactly that list.
    same = [r for r in prior if r.get("turn") == turn]
    carried = list(same[-1].get("issued") or []) if same else []
    rows = [r for r in prior if r.get("turn", 0) < turn]
    # BOX D: recorded for future sweeps to delta against, and to log which render mode
    # THIS sweep got — build_prompt makes its own (identical) call from the journal it's
    # handed, this field is the visible record of that decision, not its source of truth.
    rows.append({
        "turn": turn,
        "recorded_at": int(time.time()),
        "campaign_name": state.get("campaign_name"),
        "faction": state.get("faction"),
        "faction_name": _faction_name(state, state.get("faction")),
        "treasury": state.get("treasury"),
        "income": state.get("income"),
        "net_income": state.get("net_income"),
        "regions": len(state.get("regions") or []),
        "points": _unspent_points(state),
        "schema": state.get("schema"),
        "render": "full" if render_full(state, rows) else "delta",
        # B12: per-region economic output, so a change can be attributed to the region
        # where a building actually completed instead of to faction-level noise.
        "gdp": {r["region"]: r["gdp"] for r in (state.get("regions") or [])
                if isinstance(r.get("gdp"), (int, float))},
        "issued": _merge_issued(carried, cmds),
        # BOX B: military/diplomatic situation, same fail-soft shape as gdp above — an
        # older dump without these keys writes 0/[] rather than raising.
        "armies": len(state.get("armies")) if isinstance(state.get("armies"), list) else 0,
        "at_war_with": (state.get("diplomacy") or {}).get("at_war_with")
        if isinstance((state.get("diplomacy") or {}).get("at_war_with"), list) else [],
    })
    save_journal(cid, rows)
    return rows[-JOURNAL_KEEP:]


def region_movers(rows, top=3):
    """Regions whose own output moved most across the journal window.

    B12. Faction income is noise for this question — conquest, corruption, tax and several
    completions all move it in the same turn. Region gdp is the narrower measurement, and
    it is the one CA itself uses for gold maths (region:gdp(), 7 shipped call sites).

    Still reports a MOVEMENT, never a cause. A region's output changes for reasons that have
    nothing to do with what Alfred built there, and the journal cannot separate them. Same
    sample floor as econ_summary: below it, nothing is said."""
    have = [r for r in rows if isinstance(r.get("gdp"), dict) and r["gdp"]]
    if len(have) < ECON_MIN_SAMPLES:
        return None
    first, last = have[0]["gdp"], have[-1]["gdp"]
    deltas = [(k, last[k] - first[k]) for k in last if k in first]
    deltas = [d for d in deltas if abs(d[1]) >= 1]
    if not deltas:
        return None
    deltas.sort(key=lambda kv: -abs(kv[1]))
    span = have[-1]["turn"] - have[0]["turn"]
    parts = [f"{k} {'+' if v > 0 else ''}{int(v)}" for k, v in deltas[:top]]
    return (f"Region output moved most over {span} turns: {', '.join(parts)}. "
            f"Movement only — not attributed to any building.")


def econ_summary(rows):
    """What this campaign's own numbers say, stated only as strongly as the data allows.

    Deliberately does NOT attribute income changes to individual buildings. A turn's delta
    has many causes and the journal cannot separate them; claiming 'the farm earned you
    340g' from one sample would be exactly the fabricated-value problem the rulebook was
    kept clear of. What IS honest from this data: the trend, and how much of it is real."""
    pts = [r for r in rows if isinstance(r.get("net_income"), (int, float))]
    if len(pts) < ECON_MIN_SAMPLES:
        return None  # one reading is not a state
    recent = pts[-ECON_MIN_SAMPLES:]
    first, last = recent[0], recent[-1]
    span = last["turn"] - first["turn"]
    if span <= 0:
        return None
    delta = last["net_income"] - first["net_income"]
    built = sum(len([i for i in r.get("issued", []) if i["kind"] == "building"])
                for r in rows)
    trend = "flat" if abs(delta) < 50 else ("rising" if delta > 0 else "falling")
    return (f"Net income over the last {span} turns: {first['net_income']} -> "
            f"{last['net_income']} ({trend}). {built} building order(s) issued this "
            f"campaign so far. Measured in this campaign, not a game constant.")


def read_state():
    """The last state dump the mod wrote, or None. Used only on error paths that need the
    campaign id without triggering a fresh dump."""
    try:
        with open(STATE) as f:
            return json.load(f)
    except Exception:
        return None


def campaign_id(state):
    """B5. The playthrough this state dump came from, or None.

    None is the honest answer for a pack older than B5, and it FAILS CLOSED: no memory is
    read and none is written. The alternative — falling back to a shared key — is what
    makes "per-campaign memory" a lie the first time he starts a second campaign, because
    campaign B would silently inherit campaign A's conversation."""
    if not isinstance(state, dict):
        return None
    cid = state.get("campaign_id")
    return cid if isinstance(cid, str) and cid else None


def load_history(cid):
    """[{"you": str, "alfred": str}, ...], oldest first, for ONE campaign. Fails soft to
    [] — a missing or corrupt history must never stop Alfred answering — and returns []
    for an unknown campaign rather than serving another playthrough's transcript."""
    if cid is None:
        return []
    try:
        with open(CHAT_HISTORY) as f:
            h = json.load(f)
    except Exception:
        return []
    if not isinstance(h, dict):
        return []  # pre-B5 flat list: not attributable to a campaign, so not inherited
    turns = h.get(cid)
    return turns[-CHAT_KEEP:] if isinstance(turns, list) else []


def save_history(cid, history):
    """Best-effort: .cache may not exist on a fresh machine, and losing the transcript is
    never worth killing the daemon over. A write for an unknown campaign is dropped —
    writing it under any shared key would leak into the next campaign that asks."""
    if cid is None:
        return
    try:
        try:
            with open(CHAT_HISTORY) as f:
                blob = json.load(f)
        except Exception:
            blob = {}
        if not isinstance(blob, dict):
            blob = {}  # a pre-B5 flat list is discarded, not merged: it has no campaign
        blob[cid] = history[-CHAT_KEEP:]
        os.makedirs(os.path.dirname(CHAT_HISTORY), exist_ok=True)
        with open(CHAT_HISTORY, "w") as f:
            json.dump(blob, f)
    except Exception as e:
        print(f"  (history not saved: {e})")


def render_transcript(history):
    """History -> the exact lines the in-game panel renders, one chat_string row per line.
    Rows do not wrap themselves, so wrapping happens here; embedded newlines are flattened
    for the same reason (one line out must mean one row in)."""
    import textwrap
    rows = []
    for turn in history[-CHAT_KEEP:]:
        for who, key in (("You", "you"), ("Alfred", "alfred")):
            msg = " ".join(str(turn.get(key, "")).split())
            if not msg:
                continue
            wrapped = textwrap.wrap(msg, CHAT_WRAP) or [""]
            rows.append(f"{who}: {wrapped[0]}")
            rows.extend("    " + w for w in wrapped[1:])
        rows.append("")
    return "\n".join(rows).rstrip("\n")


def _read_object(path):
    try:
        with open(path) as f:
            value = json.load(f)
    except Exception:
        return {}
    return value if isinstance(value, dict) else {}


def dashboard_snapshot(selected_campaign=None):
    """Return the safe, read-only state rendered by the local companion dashboard."""
    try:
        config = load_model_config()
        provider, model, config_error = config["provider"], config["model"], None
    except ValueError as e:
        provider, model, config_error = None, None, str(e)
    probe = None
    if provider and _PROBE and _PROBE[0] == (provider, model):
        probe = {"ok": bool(_PROBE[1]), "message": _redact_probe_text(_PROBE[2])}

    history_blob = _read_object(CHAT_HISTORY)
    journal_blob = _read_object(JOURNAL)
    prefs_blob = _read_object(PREFS)
    state = read_state()
    current_id = campaign_id(state)
    ids = set(history_blob) | set(journal_blob) | set(prefs_blob)
    if current_id:
        ids.add(current_id)

    campaigns = []
    for cid in ids:
        if not isinstance(cid, str) or not cid:
            continue
        rows = journal_blob.get(cid) if isinstance(journal_blob.get(cid), list) else []
        latest = rows[-1] if rows and isinstance(rows[-1], dict) else {}
        current = cid == current_id
        source = state if current and isinstance(state, dict) else latest
        faction = source.get("faction")
        faction_name = (_faction_name(state, faction) if current else source.get("faction_name")) \
            or faction
        if not faction_name or faction_name == faction:
            faction_name = f"Campaign …{cid[-6:]}"
        prefs = load_prefs(cid)
        history = history_blob.get(cid)
        if not isinstance(history, list):
            history = []
        history = [row for row in history[-CHAT_KEEP:] if isinstance(row, dict)]
        if current:
            diplomacy = source.get("diplomacy") if isinstance(source.get("diplomacy"), dict) else {}
            wars = [_faction_name(state, key) for key in diplomacy.get("at_war_with", [])]
            regions = len(source.get("regions")) if isinstance(source.get("regions"), list) else None
            armies = len(source.get("armies")) if isinstance(source.get("armies"), list) else None
            last_seen = int(os.path.getmtime(STATE)) if STATE and os.path.exists(STATE) else None
        else:
            wars = latest.get("at_war_with") if isinstance(latest.get("at_war_with"), list) else []
            regions, armies = latest.get("regions"), latest.get("armies")
            last_seen = latest.get("recorded_at")
        caps = prefs.get("capabilities") or {}
        context = [
            f"Turn {source.get('turn', '?')} with {regions if regions is not None else '?'} regions and "
            f"{armies if armies is not None else '?'} armies.",
            f"Treasury {source.get('treasury', '?')}; net income {source.get('net_income', '?')}; "
            f"reserve floor {prefs.get('reserve_floor', RESERVE_FLOOR_MIN)}.",
            f"Preference: {prefs.get('stance', 'balanced')}. Buildings "
            f"{'enabled' if caps.get('buildings') else 'disabled'}; skills "
            f"{'enabled' if caps.get('skills') else 'disabled'}.",
            f"Alfred remembers {len(history)} recent chat {'exchange' if len(history) == 1 else 'exchanges'} "
            f"and {len(rows)} campaign journal {'entry' if len(rows) == 1 else 'entries'}.",
        ]
        campaigns.append({
            "id": cid, "label": faction_name, "current": current,
            "campaign_name": source.get("campaign_name"), "faction": faction,
            "turn": source.get("turn"), "last_seen": last_seen,
            "treasury": source.get("treasury"), "income": source.get("income"),
            "net_income": source.get("net_income"), "regions": regions, "armies": armies,
            "wars": wars, "prefs": prefs, "history": history, "context": context,
        })
    campaigns.sort(key=lambda row: (not row["current"], -(row.get("turn") or -1), row["id"]))
    selected = next((row for row in campaigns if row["id"] == selected_campaign), None)
    if selected is None and campaigns:
        selected = campaigns[0]
    return {
        "provider": provider, "model": model, "config_error": config_error,
        "key_saved": _stored_api_key() is not None, "probe": probe,
        "campaigns": campaigns, "selected": selected,
    }


def parse_chat_request(text):
    """"<token>\\n<message>\\n" -> (token, message), or None if malformed/torn/empty.
    The mod's write_file (open "w" then write) truncates before writing, so the 1s-
    polling daemon can catch a request file mid-write: empty, or a token with no
    message yet. The trailing "\\n" is a completeness sentinel — the mod always writes
    it last, so a torn/partial read simply won't have it -> None -> retried next tick."""
    if not text or not text.endswith("\n"):
        return None
    token, _, rest = text[:-1].partition("\n")
    if not token:
        return None
    return token, rest


def _issued_summary(cmds, rules=None):
    """Player-facing status line — display names only, never raw keys (2026-09-16). "Issued"
    means the order was SENT to the game this turn, not that it completed: the mod confirms
    each command asynchronously (aigen_log.txt), so this line never claims done.

    Nothing-issued no longer gets a line here: the model already says so, in its own words,
    and a second Python-authored "No orders issued." on top of that was itself the doubled-
    text bug on a different sentence (chat() now calls this ONLY when cmds is non-empty)."""
    if not cmds:
        return ""
    levels = ((rules or {}).get("buildings") or {}).get("levels") or {}
    parts = []
    for c in cmds:
        if c["kind"] == "skill":
            parts.append(f"skill {_skill_label(c['skill'])} for cqi {c['cqi']}")
        elif c["kind"] == "building":
            bname = (levels.get(c["building"]) or {}).get("name") or c["building"]
            parts.append(f"building {bname} at {_region_name(rules, c['region'])} "
                         f"slot {c['slot_idx']}")
    return "Issued: " + "; ".join(parts) + "."


def _narrow_by_target(message, rules, ups):
    """Advisory narrowing: when the message plainly names a settlement or a building, point
    the model at the one legal option that matches instead of leaving it to guess among many.

    Never makes anything MORE legal — it only picks a subset of an already-legal `ups` list —
    so a false match costs nothing worse than a wasted hint; validate() still checks the
    model's actual output against the full `ups` it was given.

    ponytail: substring matching, not tokenised or fuzzy — good enough for a display name or
    a settlement mentioned by name; add real matching if short/generic names start colliding."""
    low = " ".join((message or "").lower().split())
    if not low or len(ups) <= 1:
        return ups
    def mentioned(u):
        for name in (u.get("name"), u["building"].replace("_", " "),
                     _region_name(rules, u["region"])):
            if name and name.lower() in low:
                return True
        return False
    hit = [u for u in ups if mentioned(u)]
    return hit or ups


def _delint_keys(rules, state, text):
    """CHAT_SYSTEM already tells the model to use display names, never internal keys — this
    is the enforcement, since a system prompt is a request the model can still miss. Swaps
    any building/region key that leaked into reply text for its display name."""
    if not text:
        return text
    levels = (rules.get("buildings") or {}).get("levels") or {}
    keymap = {}
    for k, v in levels.items():
        name = (v or {}).get("name")
        if name and name != k:
            keymap[k] = name
    for r in (state.get("regions") if isinstance(state.get("regions"), list) else []):
        rk = r.get("region")
        if rk and rk not in keymap:
            name = _region_name(rules, rk)
            if name != rk:
                keymap[rk] = name
    # longest key first: a short key that is a PREFIX of a longer one must not clobber it
    # ("wh2_main_hef_farm_1" replaced before "wh2_main_hef_farm_1_something" gets a chance).
    for key in sorted(keymap, key=len, reverse=True):
        if key in text:
            text = text.replace(key, keymap[key])
    return text


def _cmd_key(c):
    if c.get("kind") == "building":
        return ("building", c.get("region"), c.get("slot_idx"), c.get("building"))
    if c.get("kind") == "skill":
        return ("skill", c.get("cqi"), c.get("skill"))
    return None


def _already_sent(cid, turn, cmds):
    """Which of `cmds` already sit in COMMANDS for this exact campaign+turn — a defence
    against the model reissuing an identical order it already sent (owner-reported, real
    Gemini replay: seven copies of the same building landed in one turn). Matches on the
    literal strings skill_cmds_as_exec bakes into each command's generated Lua, since that
    rewrite is what actually reaches COMMANDS and it loses the structured region/slot/
    building/cqi/skill fields — there is nothing more structured left to compare."""
    if cid is None or turn is None or not cmds:
        return set()
    try:
        text = open(COMMANDS).read()
    except OSError:
        return set()
    stamp = (f"campaign = {lua_quote(cid)}", f"turn = {int(turn)}")
    dup = set()
    for line in text.splitlines():
        raw = _ENTRY_RE.match(line.strip())
        if not raw:
            continue
        raw = raw.group(1)
        if stamp[0] not in raw or stamp[1] not in raw:
            continue
        for c in cmds:
            key = _cmd_key(c)
            if key is None or key in dup:
                continue
            # The whole generated "code" string is itself lua_quote()d, so every embedded
            # string literal's quotes come out ESCAPED (\") in the written file — a bare
            # '"value"' search never matches. Bare numbers (slot_idx, cqi) are never quoted
            # in the generated code, so those stay unescaped.
            if key[0] == "building":
                _, region, slot_idx, building = key
                if (f'\\"{region}\\"' in raw and f"({slot_idx})" in raw
                        and f'\\"{building}\\"' in raw):
                    dup.add(key)
            elif key[0] == "skill":
                _, cqi, skill = key
                if f"get_character_by_cqi({cqi})" in raw and f'\\"{skill}\\"' in raw:
                    dup.add(key)
    return dup


def _building_already_sent(cid, turn, u):
    return bool(_already_sent(cid, turn, [{"kind": "building", "region": u["region"],
                                            "slot_idx": u["slot_idx"],
                                            "building": u["building"]}]))


# A POSITIVE list of first-person/present-tense claims about THIS turn specifically — never
# a negative "unless it says already/last turn" exclusion. MEASURED (bounce review, real
# Gemini replay): a negative-exclusion version deleted TRUE past-tense statements like "The
# order I issued last turn is still building" (no "already" in it, so the old exclusion
# missed it) and "The barracks was queued earlier and is still under construction" — neither
# claims anything happened THIS turn, so neither belongs on this list, and a bare "issued"/
# "queued"/"sent" anywhere in a sentence is not enough to flag it. Only an EXPLICIT this-turn
# or first-person-present claim matches: "sent/issued/queued this turn", "order (was/has
# been) sent/issued/queued", "I have sent/issued/queued", or a leading "Issued:"/"Sent:"
# label (the shape _issued_summary itself uses).
_COMPLETION_CLAIM_RE = re.compile(
    r"\b(?:sent|issued|queued)\s+this\s+turn\b"
    r"|\border\s+(?:was\s+|has\s+been\s+)?(?:sent|issued|queued)\b"
    r"|\bi\s+have\s+(?:sent|issued|queued)\b"
    r"|^\s*(?:issued|sent)\s*:",
    re.I)


def _strip_false_completion_claims(reply, cmds, prompt=None):
    """Contradiction fix (real Gemini replay, "rerun it"): the model wrote "order was sent
    this turn" while nothing was issued. Nothing was actually sent THIS turn -> any sentence
    matching _COMPLETION_CLAIM_RE is false and must not reach the player as if it were true.
    Only fires when cmds is empty; a genuine issue always has its own real summary appended
    separately, so its own true claim is never touched.

    If stripping would leave a true remainder, that remainder is returned.

    If EVERY sentence is a false this-turn claim (2026-09-16 bounce item 3: the earlier
    version returned the reply UNCHANGED here, passing the contradiction straight through —
    "Order was sent this turn." reached the player verbatim), and a `prompt` was given to
    re-ask against, the model is asked ONCE MORE via _chat_call with a SYSTEM NOTE saying
    plainly that nothing was sent, and its answer is used (stripped the same way, in case it
    still slips in a claim). If that retry is unavailable (`prompt` is None — the bare
    unit-level call this function also supports), the caller keeps the model's ORIGINAL text
    unchanged (the historical fallback). NEVER RETURNS EMPTY when a retry ran and produced
    real text (2026-09-16, non-blocking item 4a: an all-false retry used to come back as "",
    which is worse than a false claim — the player got no reply at all): the retry's text
    minus its own claim sentences is used, and if THAT strips to nothing too (the retry is
    all-false as well), the retry's raw text is returned unstripped rather than an empty
    string."""
    if cmds or not reply:
        return reply
    sentences = re.split(r"(?<=[.!?])\s+", reply.strip())
    kept = [s for s in sentences if not _COMPLETION_CLAIM_RE.search(s)]
    if kept:
        return " ".join(kept).strip()
    if prompt is None:
        return reply
    retry_prompt = (prompt + "\n\nSYSTEM NOTE: no order was sent this turn; answer without "
                    "claiming one was.")
    reply2, _decision2, err2 = _chat_call(retry_prompt)
    if err2 or not reply2:
        return reply
    sentences2 = re.split(r"(?<=[.!?])\s+", reply2.strip())
    kept2 = [s for s in sentences2 if not _COMPLETION_CLAIM_RE.search(s)]
    # REVERT CHECK (executed): restoring the old `return ""` here whenever kept2 is empty
    # (the retry itself came back all-false) makes the "re-ask comes back all-false too"
    # test below fail — it must return reply2 unstripped, never "".
    return " ".join(kept2).strip() if kept2 else reply2


def record_turn(cid, history, message, reply):
    """Append this exchange, persist it, and return the WHOLE rendered transcript — which is
    what now goes into CHAT_REPLY for the panel to draw. Every return path in chat() goes
    through here, so an error reply is remembered and shown like any other. `cid` None
    (pre-B5 pack) still renders the transcript for this session; it just isn't persisted."""
    history = list(history) + [{"you": message, "alfred": reply}]
    save_history(cid, history)
    return render_transcript(history)


def _cap_reply(text, cap=REPLY_CAP):
    """Truncate at a sentence or word boundary with '...' rather than mid-word with nothing —
    a blunt `[:cap]` slice used to cut a name or a number in half with no sign anything was
    lost. Looks back up to 40 chars from the cut for a boundary; a text with none that close
    (one very long word) falls back to the blunt cut rather than eating a third of the reply."""
    if not text or len(text) <= cap:
        return text
    cut = text[:cap]
    for pat in (r"[.!?]\s", r"\s"):
        m = None
        for m in re.finditer(pat, cut[-40:]):
            pass
        if m:
            boundary = len(cut) - 40 + m.start()
            if boundary > cap * 0.5:  # don't chop off most of the reply hunting a boundary
                return cut[:boundary].rstrip() + "..."
    return cut.rstrip() + "..."


def _chat_call(prompt):
    """Ask the model once; return (reply_text, decision_dict, error_text). error_text is set
    (decision None) only on a transport/shape failure the model itself never got a chance to
    speak to — those two lines stay Python's, because there is no reply to shape."""
    print("--- chat prompt ---\n" + prompt + "\n-------------------")
    try:
        decision = request_decision(CHAT_SYSTEM, prompt)
    except SetupError as e:
        return None, None, str(e)  # already the one plain-words sentence; don't re-wrap it
    except (ValueError, RuntimeError, subprocess.TimeoutExpired) as e:
        return None, None, f"couldn't get a reply ({e})."
    if not isinstance(decision, dict):
        return None, None, f"got a {type(decision).__name__} instead of an answer. Ask again?"
    reply = _cap_reply(str(decision.get("reply", ""))) or "(no reply text)"
    return reply, decision, None


def chat(rules, message):
    global _PENDING_PURPOSE, _PENDING_OPTIONS
    print(f"chat: {message!r}")
    # B5: state FIRST. History is keyed by campaign and the campaign id only exists in the
    # state dump, so loading history before the dump could only ever load the wrong thing.
    state = trigger_and_wait_for_state(rules)
    cid = campaign_id(state)
    if cid is None:
        # Visible, not silent: the installed pack predates B5, so there is no campaign to
        # key memory to and Alfred answers this turn with no recall. Better said out loud
        # than discovered by the owner asking a follow-up and getting a blank stare.
        print("  (no campaign id in the state dump — pre-B5 pack; memory is OFF this turn)")
    history = load_history(cid)
    if state is None:
        # No prompt can be built without a live state dump, so there is no model to ask —
        # this one line stays Python's, unlike the guardrail refusals below.
        return record_turn(cid, history, message,
                           "couldn't reach the campaign — is the game running?")
    turn = state.get("turn")
    prefs, _mct_note = apply_dumped_settings(cid, state, load_prefs(cid))
    prefs, pref_note = apply_pref_phrases(cid, message, prefs)
    if _mct_note:
        pref_note = ((pref_note + " ") if pref_note else "") + _mct_note
    labels, recruitment = purpose_labels(), recruitment_policy()
    intent = chat_prompt_intent(message)
    purpose, problem = requested_purpose(message, labels, recruitment, rules)
    if intent == "skills":
        purpose, problem = None, None
    if intent != "skills" and purpose is None and problem is None:
        purpose = pending_purpose(cid, turn, message)
    elif intent != "skills":
        # A named new request replaces a pending one; it cannot accidentally confirm it.
        _PENDING_PURPOSE = None
    may_act = allow_commands(message)
    journal = load_journal(cid)
    is_q = _is_question(message)

    # `note` is a SYSTEM NOTE line appended to the prompt — never player-facing text on its
    # own. The model reads it and explains in its own words (CHAT_SYSTEM says so); Python
    # never hands the player a sentence it wrote itself for a gate result (2026-09-16 ruling:
    # "loosen it" — purpose/legality/reserve/capability gates stay exactly as strong, only the
    # conversation flow and reply source change).
    note = None
    building_only = False
    act_now = may_act
    ups = ()

    # PICK FROM A LIST ALFRED JUST OFFERED — checked before anything else, since a bare "2"
    # or "the second one" carries no purpose word and no narrowable name of its own; it only
    # means anything against the exact list _offer_note remembered THIS campaign+turn.
    offered = pending_options(cid, turn)
    pick_idx = _parse_pick(message, offered) if offered else None
    pick_number = _extract_pick_number(message) if offered else None

    # CANCEL/UNDO — checked before EVERYTHING else, including any offer note, so it always
    # wins (real-Gemini pass 3, finding #1: "cancel that" fell through to a silent re-list
    # because the old check only fired when something was sent THIS exact turn; a cancel
    # request about an earlier turn's order got no acknowledgement at all). The wrapper can
    # never cancel a sent order regardless of which turn it went out on, so this is
    # unconditional on `_CANCEL_RE` alone.
    if _CANCEL_RE.search(message or ""):
        building_only = True
        prompt, ups = build_chat_prompt(rules, state, message, history, journal, prefs,
                                        "buildings")
        lc = last_candidate(cid, turn)
        if lc and _building_already_sent(cid, turn, lc):
            note = (f"he seems to be asking to cancel or undo {_dropped_building_label(rules, lc)} "
                    f"(slot {lc['slot_idx']}), sent this turn — this system "
                    "cannot cancel or undo a sent order from here; tell him plainly, and "
                    "that he can cancel it himself from that settlement's build queue "
                    "in-game")
        else:
            note = ("he seems to be asking to cancel or undo an order — this system cannot "
                    "cancel or undo anything already sent to the game, whichever turn it "
                    "went out on; tell him plainly, and that he can cancel it himself from "
                    "the relevant settlement's build queue in-game")
        act_now = False
    # BARE STATE-METRIC QUERIES ("treasury", "upkeep percent", "corruption level") —
    # real-Gemini pass 3, finding #4: these fell through to a building re-list (intent="both"
    # for words outside chat_prompt_intent's small status set) instead of stating the real
    # number. Gated on `not may_act` (an order mentioning a metric word never lands here) AND
    # `purpose is None and problem is None`: "income"/"growth"/"money"/"public order"/
    # "control" are ALSO purpose words (requested_purpose(), above) with their own pending-
    # ask/confirm machinery — MEASURED (test_offline_runner_stays_runtime_only broke) that
    # intercepting them here instead, ahead of the purpose branch below, silently ate that
    # flow (a bare "growth" never reached remember_purpose(), so the confirming "go ahead"
    # had nothing pending to confirm). Left running through the existing, tested purpose path
    # for those overlapping words; only the metric words with no purpose-label meaning at all
    # (treasury/gold/money/upkeep/corruption; "public order"/"control"/"growth" only when
    # requested_purpose found nothing for the exact phrase) reach the fact-note below.
    elif not may_act and purpose is None and problem is None and _metric_query(message):
        metric = _metric_query(message)
        prompt, ups = build_chat_prompt(rules, state, message, history, journal, prefs,
                                        "question")
        fact = _state_metric_fact(state, metric)
        label = metric.replace("_", " ")
        if fact:
            note = f"he's asking about {label} — {fact}; answer directly, do not offer or list buildings"
        else:
            note = (f"he's asking about {label}, which is per-region — answer using the "
                    "per-region numbers already shown above, do not offer or list buildings")
        act_now = False
    elif offered and pick_number is not None and pick_idx is None:
        # A clear pick ATTEMPT ("2") whose number is out of range for what was actually
        # offered — say so plainly and re-list, rather than silently falling through to an
        # unrelated path (bounce review, item 6b).
        building_only = True
        prompt, ups = build_chat_prompt(rules, state, message, history, journal, prefs,
                                        "buildings")
        note = _offer_note(cid, turn, rules, offered,
                            f"He asked for option {pick_number}, but only {len(offered)} "
                            "were offered; tell him plainly there is no option that high")
        act_now = False
    elif pick_idx is not None and is_q:
        # A question that happens to name a pick ("which is better, 1 or 2?") never acts —
        # a question is never an order, on every path (2026-09-16 ruling). The offered list
        # is kept (not cleared) so a follow-up "2" without a question mark can still pick it.
        building_only = True
        prompt, ups = build_chat_prompt(rules, state, message, history, journal, prefs,
                                        "buildings")
        note = (f"he asked a question naming option {pick_idx + 1} from the list you "
                "offered; answer it in your own words, and issue no building command unless "
                "he then picks one without a question")
        act_now = False
    elif pick_idx is not None:
        picked = offered[pick_idx]
        _PENDING_OPTIONS = None  # consumed either way — a picked list is not re-offered
        building_only = True
        prompt, ups = build_chat_prompt(rules, state, message, history, journal, prefs,
                                        "buildings")
        current = _find_option(ups, picked)
        if current is None:
            note = (f"he picked option {pick_idx + 1} from the list you offered — "
                    f"{_dropped_building_label(rules, picked)} — but it is no longer a "
                    "legal option this turn; say so plainly and offer to look again. "
                    "Issue no building command")
        elif _building_already_sent(cid, turn, current):
            note = _already_sent_phrasing(
                f"he picked option {pick_idx + 1} — "
                f"{_dropped_building_label(rules, current)} (slot {current['slot_idx']}) — "
                "but that already matches a command sent this turn", message)
            act_now = False  # nothing new may be issued on a duplicate (bounce item 3)
        else:
            note = (f"he picked option {pick_idx + 1} from the list you offered — "
                    f"region={current['region']}, slot={current['slot_idx']}, "
                    f"building={current['building']}. Issue exactly that command")
            act_now = True
            ups = [current]
            remember_last_candidate(cid, turn, current)
    elif problem:
        # An ambiguous or unsupported purpose word (e.g. two purposes named at once, or an
        # unrecognised recruitment role) — let the model ask him to be specific.
        prompt, ups = build_chat_prompt(rules, state, message, history, journal, prefs, intent)
        note = (f"his request named an unclear building purpose ({problem}); present the "
                "legal purposes (income, growth, public order, recruitment, settlement "
                "defence) as a short numbered list and ask him to name one. Issue no "
                "building command")
        act_now = False
    elif purpose is not None:
        building_only = True
        label = _request_label(purpose)
        prompt, ups = build_chat_prompt(rules, state, message, history, journal, prefs,
                                        "buildings", purpose)
        if not ups:
            reason = ("reviewed Vanilla purpose labels are unavailable" if labels is None
                       else f"no legal reviewed {label} building exists this turn")
            note = reason + ". Say so in your own words; issue no building command"
            act_now = False
        else:
            # EXACTLY ONE LEGAL OPTION SURVIVING THE PURPOSE FILTER EXECUTES AT ONCE — no
            # "go ahead" needed, whether or not the message read as an explicit order
            # (2026-09-16 ruling #1: supersedes the old mandatory purpose-then-confirm flow
            # outright). A message that ALSO names a settlement or building narrows further
            # (#1's second clause), which only matters when several options still fit.
            # A QUESTION never triggers this — "which public order building is best here?"
            # must get an answer, not a command, even with one legal option and even if
            # allow_commands() somehow said yes (H1 bounce finding: it must not depend on
            # allow_commands at all, the message's own shape is the guard).
            narrowed = _narrow_by_target(message, rules, ups)
            candidates = narrowed if len(narrowed) < len(ups) else ups
            # Bounce fix (box 16, 2026-09-17): affordability applies to what's OFFERED or
            # AUTO-ISSUED, checked before either — `candidates` (legality) stays what
            # `len(candidates) == len(ups)` below compares against.
            afford = affordable(candidates, state, prefs)
            if not afford:
                floor = effective_floor(state, prefs)
                note = (f"no legal {label} option is affordable right now — treasury "
                        f"{state.get('treasury', 0)}, reserve {floor}. Tell him to save up; "
                        "do not list, offer, or issue any building")
                act_now = False
            elif len(afford) == 1 and not is_q:
                u = afford[0]
                remember_last_candidate(cid, turn, u)
                if _building_already_sent(cid, turn, u):
                    note = _already_sent_phrasing(
                        f"the only legal {label} option — "
                        f"{_dropped_building_label(rules, u)} (slot {u['slot_idx']}) — "
                        "already matches a command sent this turn", message)
                    act_now = False  # nothing new may be issued on a duplicate (bounce item 3)
                else:
                    note = (f"exactly one legal {label} option matches his request — "
                            f"region={u['region']}, slot={u['slot_idx']}, "
                            f"building={u['building']}. Issue that command")
                    act_now = True
                    ups = [u]  # H2: restrict validate()/retry to the option that decided this
            elif not act_now:
                # Several options fit (or it was a question) and this wasn't an order. But
                # if narrowing found NOTHING NEW (candidates is still the full purpose
                # subset) and the last thing chat() decided on this turn is already sent, a
                # vague follow-up ("rerun it", "no its not queue") almost certainly means
                # that, not a fresh request — say so instead of re-listing the same options
                # (bounce item 6a).
                lc = last_candidate(cid, turn)
                if (lc and len(candidates) == len(ups)
                        and _building_already_sent(cid, turn, lc)):
                    note = _already_sent_phrasing(
                        "the last building you considered this turn — "
                        f"{_dropped_building_label(rules, lc)} (slot {lc['slot_idx']}) — "
                        "already matches a command sent this turn", message)
                else:
                    remember_purpose(cid, turn, purpose)
                    note = _offer_note(cid, turn, rules, afford,
                                       f"Several legal {label} options fit and he did not "
                                       "name one, or asked which is best." if len(afford) > 1
                                       else f"Only one legal {label} option exists, but this "
                                       "reads as a question, not an order.")
    else:
        prompt, ups = build_chat_prompt(rules, state, message, history, journal, prefs, intent)
        narrowed = ()
        if intent in ("buildings", "both") and ups:
            # No purpose word at all — a request can still narrow to one legal building by
            # naming a settlement or the building itself (2026-09-16 #1), with no purpose
            # word and no "go ahead" required. Never for a question (H1).
            narrowed = _narrow_by_target(message, rules, ups)
        if len(narrowed) == 1 and act_now and not is_q:
            u = narrowed[0]
            remember_last_candidate(cid, turn, u)
            if _building_already_sent(cid, turn, u):
                note = _already_sent_phrasing(
                    "the option matching his request — "
                    f"{_dropped_building_label(rules, u)} (slot {u['slot_idx']}) — already "
                    "matches a command sent this turn", message)
                act_now = False  # nothing new may be issued on a duplicate (bounce item 3)
            else:
                note = (f"exactly one legal option matches his request — "
                        f"region={u['region']}, slot={u['slot_idx']}, building={u['building']}. "
                        "Issue that command")
                ups = [u]  # H2: restrict validate()/retry to the option that decided this
        elif intent != "skills":
            # An act marker alone (e.g. a bare "go ahead" with no pending purpose and no
            # named target), OR a question that happened to contain one (e.g. "what should
            # i build?"), is not a standing licence to spend on whatever the model dreams
            # up — the same invariant the old purpose gate enforced, just without a canned
            # refusal: the model still answers (now with a numbered offer when there is a
            # concrete list to offer), validate() still drops every command, and nothing
            # was silently loosened for the undirected case.
            act_now = False
            # DEDUPE COLLAPSE FIX (2026-09-16, real Gemini replay): a vague follow-up that
            # narrows to nothing new, right after the last thing chat() decided on this turn
            # was already sent, means THAT — but ONLY when the message itself is ABOUT that
            # order: repeating it (an act marker/confirmation, via `may_act`) or pushing back
            # on it (_PUSHBACK_RE — "that didn't work", "no it's not queued"). Before this
            # fix, `len(narrowed) == len(ups)` alone triggered the override — which is ALWAYS
            # true when only one legal building exists (narrowing is a no-op on a one-item
            # list), so every follow-up after a single-option order — a genuinely unrelated
            # question ("whats my upkeep?"), "cancel that" — collapsed to "already sent this
            # turn" instead of being answered.
            # REVERT CHECK (executed): restoring the old unconditional
            # `if (lc and len(narrowed) == len(ups) and ...)` (dropping the `not is_q and
            # (may_act or pushback)` guard) makes the "whats my upkeep?" test below fail —
            # the SYSTEM NOTE becomes the bare "already sent" override instead of a normal
            # answer path with the fact carried as context.
            # (a cancel-shaped message never reaches this branch at all any more — the
            # top-of-chat() CANCEL check above takes it first, on every turn, whether or
            # not anything was sent THIS specific turn; real-Gemini pass 3 finding #1.)
            lc = last_candidate(cid, turn)
            lc_sent = bool(lc and intent in ("buildings", "both") and ups
                           and _building_already_sent(cid, turn, lc))
            # SAME-TARGET ONLY (real-Gemini pass 3, finding #2): the override must fire only
            # when the message is about the SAME thing that was sent — "ok build the other
            # one... the expensive one" was told "that was already sent", which is true of
            # the cheap one that WAS sent but false by implication about the expensive one he
            # actually asked for. `_narrow_by_target` finds no name match for vocabulary like
            # this (nothing in `ups` is literally named "other"/"expensive"), so it falls back
            # to the FULL set and `len(narrowed) == len(ups)` alone couldn't tell "nothing
            # named" apart from "he named something else that just isn't a literal building
            # name". _DIFFERENT_TARGET_RE catches the comparison/alternative words that mean
            # the latter, and routes to the normal offer/context path instead.
            # REVERT CHECK (executed): removing `and not _DIFFERENT_TARGET_RE.search(...)`
            # makes the "the expensive one" test below fail — the reply becomes the bare
            # already-sent line instead of the offer/context path.
            if (lc_sent and not is_q and len(narrowed) == len(ups)
                  and (may_act or _PUSHBACK_RE.search(message or ""))
                  and not _DIFFERENT_TARGET_RE.search(message or "")):
                note = _already_sent_phrasing(
                    "the last building you considered this turn — "
                    f"{_dropped_building_label(rules, lc)} (slot {lc['slot_idx']}) — already "
                    "matches a command sent this turn", message)
            elif intent in ("buildings", "both") and ups:
                # Bounce fix (box 16, 2026-09-17): same affordability gate as the purpose
                # branch above — never number/offer an option he cannot afford.
                afford = affordable(ups, state, prefs)
                if not afford:
                    floor = effective_floor(state, prefs)
                    note = (f"no legal building option is affordable right now — treasury "
                            f"{state.get('treasury', 0)}, reserve {floor}. Tell him to save "
                            "up; do not list, offer, or issue any building")
                else:
                    intro = ("No purpose or single target was named and several legal building "
                             "options exist, or he asked which is best.")
                    if lc_sent:
                        intro = (f"{_dropped_building_label(rules, lc)} (slot {lc['slot_idx']}) "
                                 "was already sent this turn (mention that in passing, in the "
                                 "build queue after the panel refreshes) — but answer what he "
                                 "actually asked, don't just repeat that it was sent. " + intro)
                    note = _offer_note(cid, turn, rules, afford, intro)

    # COST/COMPARISON QUESTIONS (box 13, real-Gemini pass 2, findings #5/#7): "whats the
    # cheapest option overall" went unanswered even with costs printed right in the option
    # list, and "whats the most expensive one" got two INVENTED buildings and a fake region
    # name back instead. Whenever a note already lists real options (built by _offer_note,
    # which always shows each one's cost) and the message is asking about cost, tell the
    # model plainly to answer FROM that exact list and invent nothing else.
    if note and is_q and COST_QUESTION_RE.search(message or ""):
        note += ("; he is asking about cost or which option is cheapest/most expensive — "
                 "answer directly using ONLY the costs already listed above, and name "
                 "nothing that is not in that list, instead of just re-listing them and "
                 "asking him to pick")

    # BACK-REFERENCES WITHIN A TURN (box 13, real-Gemini pass 2, finding #6): "the cheap
    # one", "how much was that again", "same for the other province" all went unresolved
    # because whatever WAS offered earlier this turn was nowhere in the prompt for a message
    # that didn't itself trigger a fresh offer. Whenever an offer is still standing for this
    # exact campaign+turn and this turn's note isn't ALREADY a fresh numbered re-offer of it
    # (spotted by the "1) " line _offer_note always starts with), carry it forward as labelled
    # context so the model can resolve a reference to it even off the pick shortcut.
    _standing_offer = pending_options(cid, turn)
    if _standing_offer and not (note and re.search(r"(?:^|\n)1\) ", note)):
        backref = ("options offered earlier this turn: " + "; ".join(
            f"{i + 1}) {_describe_option(rules, u)}" for i, u in enumerate(_standing_offer)))
        note = (note + "; " + backref) if note else backref

    if note:
        prompt += "\n\nSYSTEM NOTE: " + note.rstrip(".") + "."

    reply, decision, err = _chat_call(prompt)
    if err:
        return record_turn(cid, history, message, err)

    # CALL BUDGET (box 13 follow-up, item 5, coordinator-measured "4-call stacking path"): up
    # to four DIFFERENT re-asks could each fire off the same turn — cost/superlative
    # hallucination, the mismatch retry, the FALSE_CLAIM dropped-reason re-ask, and
    # _strip_false_completion_claims's own internal re-ask. Capped here at 3 model calls total
    # per turn (the first call, plus at most 2 more) — the mismatch retry stays its own
    # reserved slot in practice (it is the only one that ever fires before validate()), and
    # every other re-ask below checks `calls < 3` before spending the shared second slot.
    # REVERT CHECK (executed): dropping the `calls < 3` guards (and passing `prompt=prompt`
    # unconditionally to _strip_false_completion_claims) lets the item-5 stacking test below
    # make 4 calls again instead of 3.
    calls = 1

    # COST-QUESTION QUALITY GUARD (box 13, pass 2 finding #5 + pass 3 finding #3/#9): the note
    # above tells the model to answer only from the real list and to name the true max/min,
    # but a system prompt is a request the model can still miss — this is the enforcement.
    # Combined into ONE possible re-ask (never two) covering both an invented building/price
    # AND a superlative question whose reply omits the actual cheapest/priciest option.
    # REVERT CHECK (executed): skipping this block (never re-asking) makes the hallucinated-
    # building test below fail — the invented "Archive"/"Shrine of Asuryan" text survives.
    if (calls < 3 and is_q and COST_QUESTION_RE.search(message or "") and _standing_offer
            and (_reply_has_unlisted_building(reply, rules, _standing_offer)
                 or _reply_misses_superlative(reply, rules, _standing_offer, message))):
        retry_prompt = (prompt + "\n\nSYSTEM NOTE: your last answer either named a building, "
                         "region, or price that is not in the list above, or missed the "
                         "actual cheapest/most expensive option among them. Only those "
                         "options exist this turn — answer again using ONLY them, name the "
                         "real cheapest/most expensive one if he asked for it, and invent "
                         "nothing else.")
        reply2, decision2, err2 = _chat_call(retry_prompt)
        calls += 1
        if not err2 and reply2:
            reply, decision = reply2, decision2

    if calls < 3 and act_now and ups:
        # ONE RETRY, kept exactly as before: give the model one chance to correct a tuple
        # that doesn't match an enumerated option before falling through to validate(), which
        # is the real backstop either way — an illegal tuple never survives it regardless.
        legal = {(u["region"], u["slot_idx"], u["building"]) for u in ups}
        proposed = [b for b in (decision.get("buildings") or []) if isinstance(b, dict)]
        if proposed and any((b.get("region"), b.get("slot_idx"), b.get("building")) not in legal
                             for b in proposed):
            retry_prompt = (prompt + "\n\nRETRY: Copy one exact printed (region, slot, "
                            "building) tuple; do not derive a region key. Return one such "
                            "building, or no buildings.")
            reply2, decision2, err2 = _chat_call(retry_prompt)
            calls += 1
            if not err2:
                reply, decision = reply2, decision2

    dropped = []
    cmds = validate(decision, rules, state, ups, prefs, act_now, building_only=building_only,
                     dropped=dropped)
    if cmds:
        # DEDUPE against what THIS campaign+turn already sent (real Gemini replay, owner-
        # reported: seven copies of the same building landed in one turn). Applies uniformly
        # after validate() so it catches every path, not just the ones above that could
        # check a single known candidate in advance.
        dup_keys = _already_sent(cid, turn, cmds)
        if dup_keys:
            for c in cmds:
                if _cmd_key(c) in dup_keys:
                    print(f"  DROP {c['kind']} (already sent this turn): "
                          f"{c.get('skill') or c.get('building')}")
            cmds = [c for c in cmds if _cmd_key(c) not in dup_keys]
    # FALSE_CLAIM FIX (real Gemini replay): validate() dropping a command (reserve floor,
    # a switched-off capability, an illegal building/skill) is silent from the model's own
    # side — it already wrote "ordered"/"sending" before validate() ever ran. Re-ask ONCE
    # with the real reason(s) so the reply the player sees says what actually happened,
    # instead of leaving the earlier (now false) claim to survive un-contradicted.
    # REVERT CHECK (executed): skipping this block (keeping `reply` from the first _chat_call
    # untouched whenever `dropped`) makes the reserve-floor/capability-off/illegal-skill
    # FALSE_CLAIM tests below fail — the reply keeps its "ordered"/"sending" wording instead
    # of naming the drop reason.
    if calls < 3 and dropped:
        reasons = "; ".join(f"{d['label']} — {d['reason']}" for d in dropped)
        sent_note = (" " + _issued_summary(cmds, rules)) if cmds else ""
        retry_prompt = (prompt + "\n\nSYSTEM NOTE: the following was NOT sent this turn — "
                         + reasons + "." + sent_note + " Tell him plainly what was not done "
                         "and the REAL reason above, in your own words, without claiming it "
                         "was ordered, without proposing a substitute he did not ask for, and "
                         "without inventing a different (in-game) reason — use the real one "
                         "given here even if it names a setting rather than a game rule.")
        reply2, _decision2, err2 = _chat_call(retry_prompt)
        calls += 1
        if not err2 and reply2:
            reply = reply2
    reply = _delint_keys(rules, state, reply)
    # CONTRADICTION FIX (real Gemini replay, "rerun it"): nothing was actually sent this
    # turn, so any sentence CLAIMING one was — the model's own text, not ours — must not
    # reach the player as if it were true. `prompt=None` once the call budget is spent
    # disables its own internal re-ask (see the CALL BUDGET note above) — it still strips
    # what it can locally either way.
    reply = _strip_false_completion_claims(reply, cmds, prompt if calls < 3 else None)
    if pref_note:
        reply = _cap_reply(reply + " " + pref_note)  # never store a preference silently
    if cmds:  # don't clobber a sweep's not-yet-executed commands with an empty chat write
        write_commands(skill_cmds_as_exec(cmds), cid, turn)
    # THE CHAT PATH NEVER JOURNALLED. It has always READ the journal (load_journal above) but
    # never written one, so every build ordered through the chat window was missing from the
    # campaign's own record — and since the economics summary attributes value using that
    # list, chat-issued builds were invisible to the only measurement of what a building was
    # worth. Worse, a row is created only when this is called: an owner who uses the chat
    # window rather than sweeps could never reach the 3-sample floor, leaving Alfred's memory
    # structurally empty. Recorded unconditionally, so a turn spent talking still leaves a
    # treasury/income sample behind.
    journal_record(cid, state, cmds)
    for c in cmds:
        print(f"  CMD {c['kind']}: {c.get('skill') or c.get('building')} — {c['why']}")
    # Don't repeat what Alfred already said, and don't add a Python-authored "No orders
    # issued." on top of his own text — he already says so, in his own words, whenever
    # nothing was sent. The summary earns its place only when something REAL happened.
    summary = _issued_summary(cmds, rules) if cmds else ""
    full = (reply + " " + summary) if summary else reply
    print(f"chat done: {len(cmds)} commands written")
    return record_turn(cid, history, message, full)


def _build_mismatch_message(expected):
    # Dev checkout (no alfred.companion.json): the old actionable hint stays, since
    # whoever sees it can actually re-run the extractor. A companion build's player
    # cannot, so it gets the plain "download the newest Alfred Companion" text instead.
    if companion_manifest(ROOT) is None:
        return (f"the rulebook is for game build {expected} but the installed game "
                 f"differs — re-run tools/extract_db.py")
    return ("Warhammer III was updated and this Alfred Companion does not match it "
            "yet. Download the newest Alfred Companion.")


def _disarm_setup_request():
    """A3: the daemon never reads or applies aigen_setup_request.txt (key entry moved
    to the companion app). Any leftover content from an older build or a stray write is
    cleared in place at startup and never parsed; apply_setup_request stays importable
    as a plain function for Box 3."""
    try:
        cur = open(SETUP_REQUEST).read() if os.path.exists(SETUP_REQUEST) else None
    except OSError:
        return
    if cur not in (None, "", "init"):
        inplace_write(SETUP_REQUEST, "init")


def _expected_build_id():
    """The rulebook's own game_build_id WITHOUT json.load()'ing the whole file (it can
    be ~16MB of nested dicts/lists) — a regex over the raw bytes instead, so the
    --companion wait loop's build-mismatch retries stay cheap. None on anything that
    is not a clean single match (unknown profile, unreadable file, missing field); the
    caller then falls through to the real load_rules(), which raises its own clear
    error for that case."""
    try:
        profile = active_profile()
    except RuntimeError:
        return None
    try:
        with open(rules_path(profile), "rb") as f:
            data = f.read()
    except OSError:
        return None
    m = re.search(rb'"game_build_id":"([^"]*)"', data)
    return m.group(1).decode() if m else None


def _startup():
    """Discover the exchange dir, load the rulebook, pre-create exchange files, and
    refuse (SetupError) on anything a player needs to fix before the loop can run.
    The build-id check runs BEFORE load_rules() on a cheap expected-value read, so a
    stale companion sitting on a patched game does not re-parse the full rulebook on
    every 30s wait-loop retry."""
    bind_exchange(discover_exchange_dir(required=True))
    write_exchange_hint(EXCHANGE)
    expected = _expected_build_id()
    if expected is not None and expected != game_build_id_live():
        raise SetupError(_build_mismatch_message(expected))
    rules = load_rules()
    ensure_exchange_files()
    _disarm_setup_request()
    if rules["game_build_id"] != game_build_id_live():
        raise SetupError(_build_mismatch_message(rules["game_build_id"]))
    return rules


_WEBUI_STARTED = False


def _start_webui(stop_fn):
    """Box 6: serves the local setup/status page for as long as the companion runs.
    Started once per process, before the wait loop, so the page works even while setup
    is still incomplete. Never lets a webui failure (e.g. no free port) take the
    companion down with it."""
    global _WEBUI_STARTED
    if _WEBUI_STARTED:
        return
    _WEBUI_STARTED = True
    try:
        webui.start(data_dir(ROOT), save_setup, stop_fn, ready_status,
                    dashboard_snapshot, test_model, forget_api_key)
    except Exception as e:
        print(f"webui failed to start: {e}")


def _run_companion(sleep=time.sleep, wait_seconds=30, stop_fn=None):
    """A4: background mode waits, never exits. Only the double-click path (Box 3) shows
    a dialog for a SetupError; this one logs the plain message and re-checks every
    `wait_seconds` (injectable `sleep`, so a test proves the loop iterates with no
    SystemExit and no real wait) until setup passes, then runs the normal loop.
    Catches Exception, not just SetupError: with no launchd KeepAlive an uncaught error
    here is a silent death, not a restart. The message is only printed when it changes,
    so a stuck refusal does not spam the log once per 30s forever."""
    _start_webui(stop_fn)
    last_message = None
    while True:
        try:
            rules = _startup()
        except SetupError as e:
            message = str(e)
        except Exception as e:
            message = f"companion startup failed ({type(e).__name__}: {e}); retrying"
        else:
            return _run_loop(rules)
        if message != last_message:
            print(message)
            last_message = message
        sleep(wait_seconds)


def main():
    if "--check-model" in sys.argv:
        ok, msg = probe_model()
        if not ok:
            sys.exit(msg)
        print("model check passed")
        return
    if "--companion" in sys.argv:
        return _run_companion()
    try:
        rules = _startup()
    except SetupError as e:
        sys.exit(str(e))
    return _run_loop(rules)


def _stdin_watchable(platform=None):
    """False under pythonw (sys.stdin is None), on win32 (select() there only works on
    sockets — a bare TypeError/OSError on a file/pipe/console handle, Windows companion
    blocker found 2026-09-16), or when stdin isn't a readable stream at all. The Mac/dev
    keyboard path (a real tty, or daemon.sh's `</dev/null`) is unchanged — both are
    win32-free and readable, so they still reach select()."""
    plat = platform or sys.platform
    if plat == "win32" or sys.stdin is None:
        return False
    try:
        return sys.stdin.readable()
    except (AttributeError, ValueError):
        return False


def _run_loop(rules):
    print(f"AI General daemon. Exchange: {EXCHANGE}")
    print("ENTER = admin sweep, or click 'AI Sweep' in-game. Ctrl-C quits.")
    # stale token already on disk at boot is a baseline, not a fresh request (mirrors
    # the mod's own first/baseline handling of TRIGGER in aigen.lua's poll())
    try:
        last_request = open(SWEEP_REQUEST).read() if os.path.exists(SWEEP_REQUEST) else None
    except OSError:
        last_request = None
    try:
        last_chat_request = open(CHAT_REQUEST).read() if os.path.exists(CHAT_REQUEST) else None
    except OSError:
        last_chat_request = None
    stdin_live = _stdin_watchable()  # False once stdin hits EOF (e.g. Ctrl-D, or
    # backgrounded with no tty) — readline() then returns "" forever and select()
    # reports it readable forever too, which would busy-loop firing real sweep() LLM
    # calls. Also False up front whenever stdin can't be select()ed at all (see above).
    while True:
        did_sweep = False
        try:
            watch = [sys.stdin] if stdin_live else []
            if watch and select.select(watch, [], [], 1.0)[0]:
                if sys.stdin.readline() == "":
                    stdin_live = False
                    print("stdin closed (EOF) — keyboard sweep trigger disabled, "
                          "file-watch + Ctrl-C still live")
                else:
                    did_sweep = True
            else:
                if not watch:
                    time.sleep(1.0)  # no stdin to select on; keep the 1s poll cadence
                try:
                    cur = open(SWEEP_REQUEST).read() if os.path.exists(SWEEP_REQUEST) else None
                except OSError:
                    continue  # torn read mid-write by the game; retry next tick
                if cur != last_request:
                    last_request = cur
                    print("sweep requested from in-game button")
                    did_sweep = True
                try:
                    chat_cur = (open(CHAT_REQUEST).read()
                                if os.path.exists(CHAT_REQUEST) else None)
                except OSError:
                    chat_cur = None  # torn read; retry next tick (don't touch last_chat_request)
                if chat_cur is not None and chat_cur != last_chat_request:
                    parsed = parse_chat_request(chat_cur)
                    if parsed is None:
                        pass  # empty/no trailing-newline sentinel yet (mid-write or
                        # the "init" default) — leave last_chat_request as-is so a
                        # torn read doesn't get baselined as "seen"; retry next tick
                    else:
                        last_chat_request = chat_cur  # only advance the baseline on a real act
                        token, message = parsed
                        print("chat requested from in-game HUD popup")
                        try:
                            reply = chat(rules, message)
                        except Exception as e:
                            # still go through the transcript, or a crashed turn would
                            # blank the panel's whole scrollback with one error line
                            # chat() blew up before it could hand us a campaign id, so
                            # recover it from the state file already on disk — otherwise
                            # a crashed turn would be recorded against no campaign at all
                            # (dropped) or, worse, a shared key.
                            _cid = campaign_id(read_state())
                            reply = record_turn(_cid, load_history(_cid), message,
                                                f"chat failed ({e}).")
                        inplace_write(CHAT_REPLY, token + "\n" + reply + "\n")
        except KeyboardInterrupt:
            print("bye")
            return
        if not did_sweep:
            continue
        try:
            sweep(rules)
        except Exception as e:
            print(f"sweep failed, skipping (manual play unaffected): {e}")


def game_build_id_live():
    return game_build_id(find_game_manifest())


if __name__ == "__main__":
    main()
