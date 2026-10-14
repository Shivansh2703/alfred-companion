"""Portable exchange and data directories. No Steam account id."""
import glob
import os
import re
import sys

try:
    import winreg
except ImportError:  # not on darwin/linux
    winreg = None

STEAM_SCRIPTS_GLOB = os.path.join(
    "Library", "Application Support", "Steam", "userdata",
    "*", "1142710", "local", "Warhammer3", "scripts")
WIN_EXCHANGE_REL = os.path.join("The Creative Assembly", "Warhammer3", "scripts")
HINT_NAME = "exchange.path"
COMPANION_DATA = os.path.join("Library", "Application Support", "Alfred")
GAME_APPID = "1142710"
APPMANIFEST_REL = os.path.join("steamapps", f"appmanifest_{GAME_APPID}.acf")


class SetupError(ValueError):
    """One plain-words sentence a player can act on. Subclass of ValueError so
    existing `except ValueError` call sites keep working."""


def _plat(platform):
    return platform or sys.platform


def _env(environ):
    return os.environ if environ is None else environ


def steam_roots(home=None, platform=None, environ=None):
    """Every Steam install root: the platform default, then every library folder
    from libraryfolders.vdf (deduped, existing dirs only)."""
    plat = _plat(platform)
    env = _env(environ)
    home_abs = os.path.abspath(os.path.expanduser(home or "~"))
    candidates = []
    if plat == "win32":
        program_files_x86 = env.get("ProgramFiles(x86)", os.path.join("C:\\", "Program Files (x86)"))
        candidates.append(os.path.join(program_files_x86, "Steam"))
        if winreg is not None:
            try:
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as key:
                    value, _ = winreg.QueryValueEx(key, "SteamPath")
                if value:
                    candidates.append(os.path.normpath(value))
            except OSError:
                pass
    elif plat == "darwin":
        candidates.append(os.path.join(home_abs, "Library", "Application Support", "Steam"))
    else:
        candidates.append(os.path.join(home_abs, ".steam", "steam"))

    for root in list(candidates):
        vdf = os.path.join(root, "steamapps", "libraryfolders.vdf")
        try:
            with open(vdf, encoding="utf-8") as f:
                text = f.read()
        except OSError:
            continue
        for m in re.finditer(r'"path"\s*"((?:[^"\\]|\\.)*)"', text):
            candidates.append(m.group(1).replace("\\\\", "\\"))

    seen, roots = set(), []
    for c in candidates:
        c = os.path.normpath(c)
        if c not in seen and os.path.isdir(c):
            seen.add(c)
            roots.append(c)
    return roots


def find_game_manifest(home=None, platform=None, environ=None):
    for root in steam_roots(home=home, platform=platform, environ=environ):
        manifest = os.path.join(root, APPMANIFEST_REL)
        if os.path.isfile(manifest):
            return manifest
    raise SetupError(
        "Alfred could not find Total War: WARHAMMER III in Steam. "
        "Install the game through Steam, then open Alfred again.")


def game_build_id(manifest_path):
    try:
        with open(manifest_path, encoding="utf-8") as f:
            text = f.read()
    except OSError:
        raise SetupError(
            "Alfred could not read the Warhammer III install info. "
            "Verify the game's files in Steam, then open Alfred again.")
    m = re.search(r'"buildid"\s+"(\d+)"', text)
    if not m:
        raise SetupError(
            "Alfred could not read the Warhammer III install info. "
            "Verify the game's files in Steam, then open Alfred again.")
    return m.group(1)


def game_version(manifest_path, platform=None):
    """The game's own version ("9.0.1"), or None if it cannot be read. Never raises.
    Mac: the app bundle's CFBundleShortVersionString (measured 9.0.1 on build 25937378).
    Windows: Warhammer3.exe's product version (ASSUMED to carry the same numbers; unmeasured)."""
    try:
        with open(manifest_path, encoding="utf-8") as f:
            m = re.search(r'"installdir"\s+"([^"]+)"', f.read())
        game_dir = os.path.join(os.path.dirname(manifest_path), "common", m.group(1))
        if _plat(platform) == "win32":
            return _exe_product_version(os.path.join(game_dir, "Warhammer3.exe"))
        import plistlib
        for plist in glob.glob(os.path.join(game_dir, "*.app", "Contents", "Info.plist")):
            with open(plist, "rb") as f:
                return str(plistlib.load(f)["CFBundleShortVersionString"])
    except Exception:
        return None
    return None


def _exe_product_version(exe):
    import ctypes
    from ctypes import wintypes
    ver = ctypes.windll.version
    size = ver.GetFileVersionInfoSizeW(exe, None)
    if not size:
        return None
    buf = ctypes.create_string_buffer(size)
    if not ver.GetFileVersionInfoW(exe, 0, size, buf):
        return None
    ptr, n = ctypes.c_void_p(), wintypes.UINT()
    if not ver.VerQueryValueW(buf, "\\", ctypes.byref(ptr), ctypes.byref(n)):
        return None
    words = ctypes.cast(ptr, ctypes.POINTER(wintypes.DWORD * 13)).contents
    ms, ls = words[4], words[5]  # VS_FIXEDFILEINFO.dwProductVersionMS / LS
    return f"{ms >> 16}.{ms & 0xFFFF}.{ls >> 16}"


def steam_scripts_dirs(home=None):
    root = os.path.abspath(os.path.expanduser(home or "~"))
    pattern = os.path.join(root, STEAM_SCRIPTS_GLOB)
    return sorted({os.path.abspath(p) for p in glob.glob(pattern) if os.path.isdir(p)})


def discover_exchange_dir(required=False, home=None, platform=None, environ=None):
    env = _env(environ)
    override = env.get("ALFRED_EXCHANGE")
    if override:
        path = os.path.abspath(os.path.expanduser(override))
        if os.path.isdir(path):
            return path
        if required:
            raise SetupError(f"ALFRED_EXCHANGE is not a directory: {path}")
        return ""
    plat = _plat(platform)
    if plat == "win32":
        matches = []
        appdata = env.get("APPDATA")
        if appdata:
            candidate = os.path.join(appdata, WIN_EXCHANGE_REL)
            if os.path.isdir(candidate):
                matches = [os.path.abspath(candidate)]
    else:
        matches = steam_scripts_dirs(home=home)
    if len(matches) == 1:
        return matches[0]
    if required:
        if not matches:
            raise SetupError(
                "no Warhammer 3 scripts directory found; launch the game once, then "
                "open Alfred again (or set ALFRED_EXCHANGE to the Steam userdata "
                ".../1142710/local/Warhammer3/scripts folder)")
        raise SetupError(
            "multiple Warhammer 3 scripts directories; set ALFRED_EXCHANGE to one of: "
            + ", ".join(matches))
    return ""


def companion_manifest(root):
    path = os.path.join(root, "alfred.companion.json")
    try:
        import json
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def data_dir(root, platform=None, environ=None):
    env = _env(environ)
    override = env.get("ALFRED_DATA_DIR")
    if override:
        return os.path.abspath(os.path.expanduser(override))
    if companion_manifest(root) is not None:
        plat = _plat(platform)
        if plat == "win32":
            appdata = env.get("APPDATA")
            if appdata:
                return os.path.abspath(os.path.join(appdata, "Alfred"))
        return os.path.abspath(os.path.join(os.path.expanduser("~"), COMPANION_DATA))
    return os.path.join(root, ".cache")


def hint_path(home=None):
    root = os.path.abspath(os.path.expanduser(home or "~"))
    return os.path.join(root, COMPANION_DATA, HINT_NAME)


def write_exchange_hint(exchange, home=None):
    path = hint_path(home=home)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(os.path.abspath(exchange).rstrip("/") + "\n")
    return path


def check_setup(home=None, platform=None, environ=None):
    """(ok, message) — used by the Mac app status and the daemon refusal."""
    try:
        discover_exchange_dir(required=True, home=home, platform=platform, environ=environ)
        manifest = find_game_manifest(home=home, platform=platform, environ=environ)
        game_build_id(manifest)
    except SetupError as e:
        return False, str(e)
    return True, "Alfred is ready. Start Warhammer III."
