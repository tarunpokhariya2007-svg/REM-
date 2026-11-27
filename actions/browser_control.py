
from __future__ import annotations

import asyncio
import concurrent.futures
import importlib.util
import ipaddress
import os
import platform
import re
import shutil
import subprocess
import threading
import time
from urllib.parse import parse_qsl, urlsplit
import webbrowser
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from core import appidentity, outcome

# Importing playwright cost 198 ms at every launch, paid by action discovery
# whether or not a browser was ever opened. It is the second-largest import in
# the project after pyautogui, and together the two were 78% of all module
# import time on the boot path.
#
# WHY THIS STILL RAISES WHEN PLAYWRIGHT IS MISSING
#     The eager import had a second job nobody wrote down: when playwright was
#     absent this module failed to load, action_loader dropped it, and the model
#     was never offered a browser tool it could not serve. Deferring the import
#     without keeping that would declare `browser_control` on a machine that
#     cannot run it — the model would call it, get an exception, and describe a
#     page it never opened. Exactly the failure mode this project spent Mark LVI
#     removing from local engines.
#
#     So the check stays at module scope and still stops the module loading; it
#     just asks find_spec instead of executing the package. 1 ms instead of 198,
#     same outcome, and a sentence that says what to install.
if importlib.util.find_spec("playwright") is None:      # pragma: no cover
    raise ImportError(
        "browser_control needs playwright, which is not installed. "
        "Run: pip install playwright  &&  playwright install chromium"
    )

# Annotations only — `from __future__ import annotations` above makes every one
# of them a string, so these names are never looked up at runtime and cost
# nothing outside a type checker.
if TYPE_CHECKING:
    from playwright.async_api import BrowserContext, Page, Playwright

# WHY THIS FILE DOES NOT USE core/lazyimport.py's LazyModule LIKE THE OTHERS
#     Every other deferral in this project stands a proxy in for a whole module,
#     because the call sites say `pyautogui.press(...)` and a proxy answers that
#     the same way the module would. What this file needs are two SYMBOLS out of
#     a module: a factory function, and an exception class used in `except`.
#
#     `except` will not take a proxy. It requires a real class inheriting from
#     BaseException, checked by the interpreter at the moment the exception is
#     raised, and a LazyModule there raises TypeError instead of catching
#     anything — the original error then disappears underneath a complaint about
#     the except clause. So these two are bound as real globals instead.
#
# PlaywrightTimeout starts as a private class that is never raised, so an
# `except PlaywrightTimeout:` somehow reached before a session exists catches
# nothing rather than failing that same way.
class _PlaywrightNotLoaded(Exception):
    """Placeholder exception — declared so `except` clauses stay valid, and
    deliberately never raised anywhere."""


async_playwright = None
PlaywrightTimeout: type[BaseException] = _PlaywrightNotLoaded


def _load_playwright() -> None:
    """Import playwright and bind the two runtime symbols. Idempotent."""
    global async_playwright, PlaywrightTimeout
    if async_playwright is not None:
        return
    from playwright.async_api import (          # noqa: PLC0415 — deliberate
        async_playwright as _ap,
        TimeoutError as _timeout,
    )
    async_playwright = _ap
    PlaywrightTimeout = _timeout


_OS = platform.system()   # "Windows" | "Darwin" | "Linux"
_LEGACY_PROFILE_ROOT = ".jarvis_profiles"


def _profile_root(home: Path | None = None) -> Path:
    """Return the destination for new browser profiles."""
    home = home or Path.home()
    return home / ".rem_profiles"


def _profile_path(name: str, legacy_name: str | None = None) -> Path:
    home = Path.home()
    current = home / ".rem_profiles" / name
    if current.exists():
        return current
    if legacy_name:
        legacy = home / _LEGACY_PROFILE_ROOT / legacy_name
        if legacy.exists():
            return legacy
    legacy = home / _LEGACY_PROFILE_ROOT / name
    if legacy.exists():
        return legacy
    return _profile_root() / name


def _configured_browser_cdp_endpoint(browser: str,
                                     value: str | None = None) -> str | None:
    """Return an explicitly configured loopback-only Chromium CDP endpoint."""
    browser = str(browser or "").casefold().strip()
    env_name = {"chrome": "REM_CHROME_CDP_URL",
                "edge": "REM_EDGE_CDP_URL"}.get(browser)
    if env_name is None:
        return None
    endpoint = (os.environ.get(env_name, "") if value is None
                else str(value)).strip()
    if not endpoint:
        return None
    try:
        parsed = urlsplit(endpoint)
        if (parsed.scheme.lower() not in {"http", "https", "ws", "wss"}
                or not parsed.hostname or parsed.username or parsed.password
                or parsed.query or parsed.fragment):
            raise ValueError
        host = parsed.hostname.casefold().rstrip(".")
        try:
            loopback = ipaddress.ip_address(host).is_loopback
        except ValueError:
            loopback = host == "localhost"
        if not loopback:
            raise ValueError
        if parsed.port is None:
            raise ValueError
    except (ValueError, UnicodeError):
        raise ValueError(
            f"{env_name} must be an explicit HTTP or WebSocket endpoint "
            "on localhost/127.0.0.1/::1 with a port and no credentials or query."
        ) from None
    return endpoint


def _configured_chrome_cdp_endpoint(value: str | None = None) -> str | None:
    """Backward-compatible helper for the opt-in Chrome CDP endpoint."""
    return _configured_browser_cdp_endpoint("chrome", value)


_ORDINAL_WORDS: dict[str, int] = {
    "first": 0, "1st": 0,
    "second": 1, "2nd": 1,
    "third": 2, "3rd": 2,
    "fourth": 3, "4th": 3,
    "fifth": 4, "5th": 4,
    "sixth": 5, "6th": 5,
    "seventh": 6, "7th": 6,
    "eighth": 7, "8th": 7,
    "ninth": 8, "9th": 8,
    "tenth": 9, "10th": 9,
    "last": -1,
}
_INDEX_TO_ORDINAL: dict[int, str] = {
    0: "first", 1: "second", 2: "third", 3: "fourth", 4: "fifth",
    5: "sixth", 6: "seventh", 7: "eighth", 8: "ninth", 9: "tenth", -1: "last",
}


def _parse_ordinal_index(text: str) -> int | None:
    """Extract 0-based video/result index (or -1 for last) from natural-language description."""
    s = str(text or "")
    m = re.search(
        r"\b(first|1st|second|2nd|third|3rd|fourth|4th|fifth|5th|"
        r"sixth|6th|seventh|7th|eighth|8th|ninth|9th|tenth|10th|last)"
        r"\b(?:\s+\w+){0,3}\s+(?:videos?|results?|thumbnails?|cards?|links?|items?|chats?|contacts?|one|horizontally|row)\b",
        s,
        re.IGNORECASE,
    )
    if not m:
        m = re.search(
            r"\b(?:video|result|card|item|thumbnail)\s*(?:#|no\.?|number)?\s*(10|[1-9])\b",
            s,
            re.IGNORECASE,
        )
        if m:
            return int(m.group(1)) - 1
    if not m:
        m = re.fullmatch(
            r"\s*(?:the\s+)?(first|1st|second|2nd|third|3rd|fourth|4th|fifth|5th|"
            r"sixth|6th|seventh|7th|eighth|8th|ninth|9th|tenth|10th|last)\s*",
            s,
            re.IGNORECASE,
        )
    if not m:
        return None
    return _ORDINAL_WORDS.get(m.group(1).lower())


_CANONICAL_URLS = {
    "www.youtube": "https://www.youtube.com/",
    "www.youtube.com": "https://www.youtube.com/",
    "whatsapp": "https://web.whatsapp.com/",
    "whatsapp web": "https://web.whatsapp.com/",
    "web.whatsapp": "https://web.whatsapp.com/",
    "whatsapp.com": "https://web.whatsapp.com/",
    "web.whatsapp.com": "https://web.whatsapp.com/",
}


def _normalize_url(url: str) -> str:
    """
    Known site names → their canonical homepages; unknown bare words are
    rejected rather than silently guessed as an unrelated ``.com`` address.
    Domains like "instagram.com" → "https://instagram.com"
    Full URLs are validated so truncated hosts (e.g. ``https://web.whatsapp``
    or ``https://www.youtube.``) are canonicalized or rejected cleanly.
    """
    url = str(url or "").strip()
    url = url.rstrip(".,;:!?'\"")
    if not url:
        return "about:blank"

    canonical_hit = _CANONICAL_URLS.get(url.casefold())
    if canonical_hit is not None:
        return canonical_hit

    if "://" in url:
        try:
            parsed = urlsplit(url)
            if (parsed.scheme.lower() not in {"http", "https"}
                    or not parsed.hostname
                    or parsed.username or parsed.password):
                raise ValueError
            _ = parsed.port  # validate malformed ports before handing off
            host = parsed.hostname.rstrip(".").casefold()
            if host in _CANONICAL_URLS and not parsed.path.strip("/") and not parsed.query:
                return _CANONICAL_URLS[host]
            if host in _CANONICAL_URLS and host in {"web.whatsapp", "www.youtube", "youtube", "whatsapp"}:
                canon = urlsplit(_CANONICAL_URLS[host])
                return f"{canon.scheme}://{canon.netloc}{parsed.path or '/'}" + (f"?{parsed.query}" if parsed.query else "")
            try:
                is_ip = bool(ipaddress.ip_address(host))
            except ValueError:
                is_ip = False
            if host != "localhost" and not is_ip:
                parts = [p for p in host.split(".") if p]
                if len(parts) < 2 or len(parts[-1]) < 2:
                    raise ValueError
            if parsed.hostname != host:
                netloc = host + (f":{parsed.port}" if parsed.port else "")
                return f"{parsed.scheme.lower()}://{netloc}{parsed.path or ''}" + (f"?{parsed.query}" if parsed.query else "")
            return url
        except ValueError:
            raise ValueError("Only valid HTTP or HTTPS browser URLs are supported.") from None

    prefix = url.split(":", 1)[0]
    if ":" in url and prefix.isalpha():
        if prefix.casefold() != "localhost":
            raise ValueError("Only HTTP or HTTPS browser URLs are supported.")
    host = url.split("/", 1)[0].split(":", 1)[0].strip("[]").casefold()
    try:
        is_loopback = ipaddress.ip_address(host).is_loopback
    except ValueError:
        is_loopback = host == "localhost"
    if is_loopback:
        return "http://" + url
    if "." not in url:
        aliases = {
            "google": "google.com", "youtube": "youtube.com",
            "whatsapp": "web.whatsapp.com", "whatsapp web": "web.whatsapp.com",
            "gmail": "gmail.com", "google drive": "drive.google.com",
            "github": "github.com", "reddit": "reddit.com",
            "wikipedia": "wikipedia.org", "bing": "bing.com",
            "amazon": "amazon.com", "instagram": "instagram.com",
            "facebook": "facebook.com", "linkedin": "linkedin.com",
            "microsoft": "microsoft.com", "netflix": "netflix.com",
            "x": "x.com", "twitter": "x.com",
        }
        canonical = aliases.get(url.casefold())
        if canonical is None:
            raise ValueError("ambiguous website name")
        url = canonical
    return "https://" + url


def _user_agent() -> str:
    if _OS == "Windows":
        return (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        )
    if _OS == "Darwin":
        return (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        )
    return (
        "Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )


def _real_profile_dir(browser: str) -> str:
    home  = Path.home()
    local = os.environ.get("LOCALAPPDATA", "")
    roam  = os.environ.get("APPDATA", "")

    candidates: list[Path] = []

    if _OS == "Windows":
        m = {
            "chrome":   [Path(local) / "Google"          / "Chrome"          / "User Data"],
            "edge":     [Path(local) / "Microsoft"        / "Edge"            / "User Data"],
            "brave":    [Path(local) / "BraveSoftware"    / "Brave-Browser"   / "User Data"],
            "vivaldi":  [Path(local) / "Vivaldi"          / "User Data"],
            "opera":    [Path(roam)  / "Opera Software"   / "Opera Stable",
                         Path(local) / "Opera Software"   / "Opera Stable"],
            "operagx":  [Path(roam)  / "Opera Software"   / "Opera GX Stable",
                         Path(local) / "Opera Software"   / "Opera GX Stable"],
        }
        candidates = m.get(browser, [])

    elif _OS == "Darwin":
        lib = home / "Library" / "Application Support"
        m = {
            "chrome":   [lib / "Google"             / "Chrome"],
            "edge":     [lib / "Microsoft Edge"],
            "brave":    [lib / "BraveSoftware"       / "Brave-Browser"],
            "vivaldi":  [lib / "Vivaldi"],
            "opera":    [lib / "com.operasoftware.Opera"],
            "operagx":  [lib / "com.operasoftware.OperaGX"],
        }
        candidates = m.get(browser, [])

    elif _OS == "Linux":
        cfg = home / ".config"
        m = {
            "chrome":   [cfg / "google-chrome", cfg / "chromium"],
            "edge":     [cfg / "microsoft-edge"],
            "brave":    [cfg / "BraveSoftware" / "Brave-Browser"],
            "vivaldi":  [cfg / "vivaldi"],
            "opera":    [cfg / "opera"],
            "operagx":  [cfg / "opera-gx"],
        }
        candidates = m.get(browser, [])

    for p in candidates:
        if p.exists():
            print(f"[Browser] ✅ Real profile found for {browser}: {p}")
            return str(p)

    # Keep existing cookie/sign-in data in place; new profiles use the REM root.
    fallback = _profile_path(browser)
    fallback.mkdir(parents=True, exist_ok=True)
    print(f"[Browser] ⚠️  Real profile not found for {browser}, using: {fallback}")
    return str(fallback)

def _firefox_profile_dir() -> Optional[str]:
    home = Path.home()

    if _OS == "Windows":
        base = Path(os.environ.get("APPDATA", "")) / "Mozilla" / "Firefox"
    elif _OS == "Darwin":
        base = home / "Library" / "Application Support" / "Firefox"
    else:
        base = home / ".mozilla" / "firefox"

    ini = base / "profiles.ini"
    if not ini.exists():
        return None

    current: dict[str, str] = {}
    default_path: Optional[str] = None

    for line in ini.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if line.startswith("["):
            p = current.get("Path", "")
            if p and current.get("Default") == "1":
                is_rel = current.get("IsRelative", "1") == "1"
                default_path = str(base / p) if is_rel else p
            current = {}
        elif "=" in line:
            k, _, v = line.partition("=")
            current[k.strip()] = v.strip()

    p = current.get("Path", "")
    if p and current.get("Default") == "1":
        is_rel = current.get("IsRelative", "1") == "1"
        default_path = str(base / p) if is_rel else p

    if default_path and Path(default_path).exists():
        print(f"[Browser] Firefox real profile: {default_path}")
        return default_path
    return None

def _find_opera_windows() -> Optional[str]:
    local  = os.environ.get("LOCALAPPDATA", "")
    prog   = os.environ.get("PROGRAMFILES", "")
    prog86 = os.environ.get("PROGRAMFILES(X86)", "")

    candidates = [
        Path(local)  / "Programs" / "Opera"    / "opera.exe",
        Path(local)  / "Programs" / "Opera GX" / "opera.exe",
        Path(prog)   / "Opera"    / "opera.exe",
        Path(prog86) / "Opera"    / "opera.exe",
    ]
    for p in candidates:
        if p.exists():
            print(f"[Browser] Opera found at: {p}")
            return str(p)

    try:
        import winreg
        keys = [
            r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\opera.exe",
            r"SOFTWARE\Clients\StartMenuInternet\OperaStable\shell\open\command",
            r"SOFTWARE\Clients\StartMenuInternet\OperaGXStable\shell\open\command",
            r"SOFTWARE\Clients\StartMenuInternet\opera\shell\open\command",
        ]
        for key_path in keys:
            for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
                try:
                    k   = winreg.OpenKey(hive, key_path)
                    val = winreg.QueryValue(k, None)
                    winreg.CloseKey(k)
                    exe = val.strip().strip('"').split('"')[0].split(" --")[0].strip()
                    if exe and Path(exe).exists():
                        print(f"[Browser] Opera found via registry: {exe}")
                        return exe
                except Exception:
                    continue
    except Exception:
        pass

    return shutil.which("opera") or None

def _find_exe_windows(prog_name: str) -> Optional[str]:
    try:
        import winreg
        paths_to_try = [
            rf"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\{prog_name}.exe",
            rf"SOFTWARE\Clients\StartMenuInternet\{prog_name}\shell\open\command",
        ]
        for key_path in paths_to_try:
            for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
                try:
                    k   = winreg.OpenKey(hive, key_path)
                    val = winreg.QueryValue(k, None)
                    winreg.CloseKey(k)
                    exe = val.strip().strip('"').split('"')[0].split(" --")[0].strip()
                    if exe and Path(exe).exists():
                        return exe
                except Exception:
                    continue
    except Exception:
        pass
    return None

_BROWSER_SPECS: dict[str, dict] = {
    "Windows": {
        "chrome":   {"engine": "chromium", "channel": "chrome",  "bins": []},
        "edge":     {"engine": "chromium", "channel": "msedge",  "bins": []},
        "firefox":  {"engine": "firefox",  "channel": None,      "bins": ["firefox.exe"]},
        "opera":    {"engine": "chromium", "channel": None,      "bins": ["opera.exe"],  "special": "opera_windows"},
        "operagx":  {"engine": "chromium", "channel": None,      "bins": [],             "special": "opera_windows"},
        "brave":    {"engine": "chromium", "channel": None,      "bins": ["brave.exe"]},
        "vivaldi":  {"engine": "chromium", "channel": None,      "bins": ["vivaldi.exe"]},
        "safari":   None,
    },
    "Darwin": {
        "chrome":   {"engine": "chromium", "channel": "chrome",  "bins": []},
        "edge":     {"engine": "chromium", "channel": "msedge",  "bins": ["microsoft-edge"]},
        "firefox":  {"engine": "firefox",  "channel": None,      "bins": ["firefox"]},
        "opera":    {"engine": "chromium", "channel": None,      "bins": ["opera"]},
        "operagx":  {"engine": "chromium", "channel": None,      "bins": ["opera"]},
        "brave":    {"engine": "chromium", "channel": None,      "bins": ["brave browser", "brave"]},
        "vivaldi":  {"engine": "chromium", "channel": None,      "bins": ["vivaldi"]},
        "safari":   {"engine": "webkit",   "channel": None,      "bins": []},
    },
    "Linux": {
        "chrome":   {"engine": "chromium", "channel": None,
                     "bins": ["google-chrome", "google-chrome-stable", "chromium-browser", "chromium"]},
        "edge":     {"engine": "chromium", "channel": None,
                     "bins": ["microsoft-edge", "microsoft-edge-stable"]},
        "firefox":  {"engine": "firefox",  "channel": None, "bins": ["firefox"]},
        "opera":    {"engine": "chromium", "channel": None, "bins": ["opera", "opera-stable"]},
        "operagx":  {"engine": "chromium", "channel": None, "bins": ["opera", "opera-stable"]},
        "brave":    {"engine": "chromium", "channel": None, "bins": ["brave-browser", "brave"]},
        "vivaldi":  {"engine": "chromium", "channel": None, "bins": ["vivaldi-stable", "vivaldi"]},
        "safari":   None,
    },
}

_ALIASES: dict[str, str] = {
    "google chrome":   "chrome",
    "google-chrome":   "chrome",
    "microsoft edge":  "edge",
    "ms edge":         "edge",
    "msedge":          "edge",
    "mozilla firefox": "firefox",
    "opera gx":        "operagx",
    "opera_gx":        "operagx",
}


def _resolve_browser(name: str) -> dict | None:
    name   = _ALIASES.get(name.lower().strip(), name.lower().strip())
    os_map = _BROWSER_SPECS.get(_OS, {})
    spec   = os_map.get(name)
    if spec is None:
        return None

    engine  = spec["engine"]
    channel = spec.get("channel")
    bins    = spec.get("bins", [])
    exe     = None

    if spec.get("special") == "opera_windows":
        exe = _find_opera_windows()
        if not exe:
            print(f"[Browser] ⚠️  Opera executable not found on Windows.")
        return {"engine": engine, "exe": exe, "channel": channel}

    for b in bins:
        found = shutil.which(b)
        if found:
            exe = found
            break

    if not exe and _OS == "Darwin":
        app_names = {
            "chrome":  ["Google Chrome.app"],
            "edge":    ["Microsoft Edge.app"],
            "firefox": ["Firefox.app"],
            "opera":   ["Opera.app", "Opera GX.app"],
            "brave":   ["Brave Browser.app"],
            "vivaldi": ["Vivaldi.app"],
        }
        for app in app_names.get(name, []):
            app_dir = Path("/Applications") / app / "Contents" / "MacOS"
            if app_dir.exists():
                found_bins = list(app_dir.iterdir())
                if found_bins:
                    exe = str(found_bins[0])
                    break

    if not exe and _OS == "Windows":
        exe = _find_exe_windows(_WIN_EXE_HINTS.get(name, name))

    return {"engine": engine, "exe": exe, "channel": channel}


def _detect_default_browser() -> str:
    try:
        if _OS == "Windows":
            import winreg
            k = winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\Shell\Associations"
                r"\UrlAssociations\http\UserChoice",
            )
            prog_id = winreg.QueryValueEx(k, "ProgId")[0].lower()
            winreg.CloseKey(k)
            for kw in ("edge", "firefox", "opera", "brave", "vivaldi", "chrome"):
                if kw in prog_id:
                    return kw
        elif _OS == "Darwin":
            out = subprocess.run(
                ["defaults", "read",
                 "com.apple.LaunchServices/com.apple.launchservices.secure",
                 "LSHandlers"],
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=5,
            ).stdout.lower()
            for kw in ("firefox", "opera", "brave", "vivaldi", "safari", "chrome", "edge"):
                if kw in out:
                    return kw
        elif _OS == "Linux":
            out = subprocess.run(
                ["xdg-settings", "get", "default-web-browser"],
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=5,
            ).stdout.lower()
            for kw in ("firefox", "opera", "brave", "vivaldi", "chrome", "edge"):
                if kw in out:
                    return kw
    except Exception:
        pass
    return "chrome"


def _detect_running_browser() -> str:
    """Return an already-running browser, preferring foreground then visible Chrome windows."""
    try:
        from core import screenview
        fg_app, _fg_title, _pid, _hwnd = screenview._foreground()
        fg_cf = str(fg_app or "").casefold()
        for b in ("chrome", "firefox", "brave", "opera", "vivaldi", "edge"):
            if b in fg_cf or (b == "edge" and "msedge" in fg_cf):
                return b
        # Prefer browsers that actually have visible windows over background Windows Edge processes.
        wins = [(str(a or "").casefold(), str(t or "").casefold()) for a, t in screenview.all_windows()]
        for b in ("chrome", "firefox", "brave", "opera", "vivaldi", "edge"):
            if any(b in a or (b == "edge" and "msedge" in a) or (b == "chrome" and "google chrome" in t) for a, t in wins):
                return b
    except Exception:
        pass
    for b in ("chrome", "firefox", "brave", "opera", "vivaldi"):
        try:
            if appidentity.running(b):
                return b
        except Exception:
            continue
    return ""


def select_browser(explicit_browser: str | None = None, *,
                   task_browser: str | None = None,
                   user_request: str = "") -> str:
    """Centralized browser-selection mechanism.

    Priority:
      1. Explicit browser parameter or browser explicitly named in user_request.
      2. Previously selected browser bound to the current task.
      3. Currently active browser session / visible running browser.
      4. OS default browser (only when 1-3 provide no selection).
    """
    explicit = str(explicit_browser or "").strip().lower()
    if explicit:
        return _ALIASES.get(explicit, explicit)
    if user_request:
        try:
            from core import desktop_control
            from_text = desktop_control.extract_browser_name(user_request)
            if from_text:
                return _ALIASES.get(from_text, from_text)
        except Exception:
            pass
    tb = str(task_browser or "").strip().lower()
    if tb:
        return _ALIASES.get(tb, tb)
    if _registry._active_browser:
        return _registry._active_browser
    if _registry._task_browser:
        return _registry._task_browser
    running = _detect_running_browser()
    if running:
        return running
    return _detect_default_browser()


def open_in_selected_browser(url: str, browser: str | None = None) -> str:
    """Open URL through the centralized browser selector (used by sibling helpers)."""
    chosen = select_browser(browser)
    if _registry.has(chosen):
        return _registry.get(chosen).run(_registry.get(chosen).go_to(url))
    return _open_native(url, chosen)


def _discover_running_cdp_endpoint(browser_name: str) -> str | None:
    """Best-effort loopback CDP discovery ONLY for an already-running browser."""
    import socket
    browser = str(browser_name or "").casefold().strip()
    if browser not in {"chrome", "edge"}:
        return None
    try:
        if not appidentity.running(browser):
            return None
    except Exception:
        return None
    candidates: list[tuple[str, int]] = []
    if _OS == "Windows" and browser == "chrome":
        local = os.environ.get("LOCALAPPDATA", "")
        if local:
            port_file = Path(local) / "Google" / "Chrome" / "User Data" / "DevToolsActivePort"
            if port_file.exists():
                try:
                    lines = port_file.read_text(encoding="utf-8", errors="ignore").splitlines()
                    port = int(lines[0].strip())
                    if 1024 <= port <= 65535:
                        candidates.append(("127.0.0.1", port))
                except Exception:
                    pass
    candidates.append(("127.0.0.1", 9222))
    for host, port in candidates:
        try:
            with socket.create_connection((host, port), timeout=0.15):
                return f"http://{host}:{port}"
        except OSError:
            continue
    return None


_SEARCH_ENGINES: dict[str, str] = {
    "google":     "https://www.google.com/search?q=",
    "youtube":    "https://www.youtube.com/results?search_query=",
    "bing":       "https://www.bing.com/search?q=",
    "duckduckgo": "https://duckduckgo.com/?q=",
    "yandex":     "https://yandex.com/search/?text=",
}

_MAC_APP_NAMES: dict[str, str] = {
    "chrome":  "Google Chrome",
    "edge":    "Microsoft Edge",
    "firefox": "Firefox",
    "opera":   "Opera",
    "operagx": "Opera GX",
    "brave":   "Brave Browser",
    "vivaldi": "Vivaldi",
    "safari":  "Safari",
}

# Windows registry lookup names for browsers whose spec has no explicit binary
_WIN_EXE_HINTS: dict[str, str] = {"chrome": "chrome", "edge": "msedge"}


# Chromium builds the accessibility tree for a PAGE only when it believes
# something is listening. Nothing an outside program can do convinces it — both
# a UIA walk and a WM_GETOBJECT probe were tried against a running browser and
# returned the same empty panes.
#
# But the browser we START is ours to configure, and one flag changes the whole
# picture. Measured on Edge, same page, same profile:
#
#     plain                                21 elements, page text: 4
#     --force-renderer-accessibility       25 elements, page text: 7,
#                                          including a clickable link
#
# That is the difference between a page the assistant can read and click by
# name, in the user's OWN signed-in browser, and one it has to guess at from a
# screenshot or drive from a second window where nobody is logged in.
#
# It applies only when this launches the browser. A browser already running
# ignores the flag and simply takes the URL, which is correct: we are not
# restarting somebody's browser to gain a feature.
_CHROMIUM_FAMILY = frozenset({"chrome", "edge", "opera", "operagx", "brave",
                              "vivaldi"})


def _launch_flags(name: str) -> list[str]:
    return (["--force-renderer-accessibility"]
            if name in _CHROMIUM_FAMILY else [])


def _expected_site_hints(url: str) -> tuple[str, tuple[str, ...]]:
    try:
        host = (urlsplit(url).hostname or "").casefold().lstrip("www.")
    except Exception:
        host = ""
    hints = [host] if host else []
    if "whatsapp" in host:
        hints.extend(["whatsapp", "web.whatsapp.com"])
    elif "youtube" in host:
        hints.extend(["youtube", "youtube.com"])
    elif "google" in host:
        hints.extend(["google"])
    elif host:
        base = host.split(".")[0]
        if len(base) >= 3:
            hints.append(base)
    return host, tuple(dict.fromkeys(h for h in hints if h))


NAV_REQUESTED = "REQUESTED"
NAV_BROWSER_RESOLVED = "BROWSER_RESOLVED"
NAV_TAB_SELECTED = "TAB_SELECTED"
NAV_NAVIGATION_STARTED = "NAVIGATION_STARTED"
NAV_NAVIGATION_COMPLETED = "NAVIGATION_COMPLETED"
NAV_PAGE_IDENTITY_VERIFIED = "PAGE_IDENTITY_VERIFIED"
NAV_FOREGROUND_VERIFIED = "FOREGROUND_VERIFIED"
NAV_SUCCESS = "SUCCESS"

ACTION_REQUESTED = "ACTION_REQUESTED"
TARGET_IDENTIFIED = "TARGET_IDENTIFIED"
ACTION_ATTEMPTED = "ACTION_ATTEMPTED"
ACTION_VERIFIED = "ACTION_VERIFIED"
ACTION_FAILED = "ACTION_FAILED"
ACTION_UNVERIFIED = "ACTION_UNVERIFIED"

ELEM_VIDEO_CARD = "VIDEO_CARD"
ELEM_CATEGORY_CHIP = "CATEGORY_CHIP"
ELEM_NAVIGATION_ITEM = "NAVIGATION_ITEM"
ELEM_SHORTS_CARD = "SHORTS_CARD"
ELEM_AD_CARD = "AD_CARD"
ELEM_SEARCH_CONTROL = "SEARCH_CONTROL"
ELEM_PLAYER_CONTROL = "PLAYER_CONTROL"
ELEM_OTHER = "OTHER"

_YT_CHIP_ANCHORS = frozenset({
    "all", "music", "mixes", "gaming", "live", "news", "sports", "podcasts",
    "recently uploaded", "watched", "new to you", "for you", "posts", "playlists",
    "science fiction", "movies", "comedy", "trending", "learning", "fashion",
})


def _log_browser_action_diag(
    *,
    task_id: str = "",
    intent: str = "",
    parsed_target: str = "",
    browser_hwnd: int = 0,
    browser_pid: int = 0,
    requested_site: str = "",
    verified_site: str = "",
    active_tab: str = "",
    action: str = "",
    target_class: str = "",
    selected_target: str = "",
    selection_method: str = "none",
    target_bbox: object = None,
    auth_result: str = "",
    auth_reason: str = "",
    click_method: str = "",
    fg_hwnd_before: int = 0,
    fg_hwnd_after: int = 0,
    verification_result: str = ACTION_UNVERIFIED,
    failure_reason: str = "",
    elapsed_ms: float = 0.0,
) -> None:
    """Emit structured diagnostic metadata without logging message contents or secrets."""
    safe_tab = " ".join(str(active_tab or "").split())[:96]
    safe_target = " ".join(str(selected_target or "").split())[:96]
    safe_intent = " ".join(str(intent or "").split())[:96]
    safe_parsed = " ".join(str(parsed_target or "").split())[:64]
    safe_reason = " ".join(str(failure_reason or "").split())[:120]
    print(
        f"[BrowserDiag] task_id={task_id or 'none'} intent={safe_intent or 'none'!r} "
        f"parsed_target={safe_parsed or 'none'!r} hwnd={int(browser_hwnd or 0)} "
        f"pid={int(browser_pid or 0)} fg_before={int(fg_hwnd_before or 0)} fg_after={int(fg_hwnd_after or 0)} "
        f"req_site={requested_site or 'none'} ver_site={verified_site or 'none'} "
        f"tab={safe_tab!r} action={action or 'none'} class={target_class or 'OTHER'} "
        f"target={safe_target!r} method={selection_method or 'none'} click_method={click_method or 'none'} "
        f"bbox={target_bbox!r} auth={auth_result or 'n/a'}:{auth_reason or 'none'} "
        f"result={verification_result} reason={safe_reason or 'none'} elapsed_ms={round(float(elapsed_ms or 0.0), 1)}"
    )


def _cluster_youtube_video_rows(
    items: list[dict],
    *,
    row_gap: int = 95,
) -> tuple[list[dict], list[list[dict]], bool, list[str]]:
    """Order YouTube video items in deterministic reading order (left-to-right per row, top-to-bottom).

    Items sharing vertical row proximity (`<= row_gap` px from the row anchor `top`) belong
    to the same visual horizontal row and sort left-to-right. Returns
    `(ordered_items, rows, ambiguous_sections, section_names)`.
    """
    if not items:
        return [], [], False, []

    def _top_left(it: dict) -> tuple[int, int]:
        rect = it.get("rect")
        if isinstance(rect, (list, tuple)) and len(rect) >= 2:
            return int(rect[1] or 0), int(rect[0] or 0)
        return int(it.get("top", 0) or 0), int(it.get("left", 0) or 0)

    has_coords = any(_top_left(it) != (0, 0) for it in items)
    if not has_coords:
        ordered = [dict(it) for it in items]
        for idx, it in enumerate(ordered, 1):
            it["position"] = idx
            it.setdefault("row_index", 1)
            it.setdefault("col_index", idx)
        return ordered, [ordered], False, []

    sorted_by_top = sorted(items, key=lambda it: (_top_left(it)[0], _top_left(it)[1]))
    rows: list[list[dict]] = []
    row_tops: list[int] = []
    for it in sorted_by_top:
        t, _l = _top_left(it)
        if not rows or abs(t - row_tops[-1]) > row_gap:
            rows.append([dict(it)])
            row_tops.append(t)
        else:
            rows[-1].append(dict(it))

    ordered: list[dict] = []
    sections: list[str] = []
    for r_idx, row in enumerate(rows, 1):
        row.sort(key=lambda it: _top_left(it)[1])
        for c_idx, it in enumerate(row, 1):
            it["row_index"] = r_idx
            it["col_index"] = c_idx
            it["position"] = len(ordered) + 1
            sec = str(it.get("section") or "").strip()
            if sec and sec not in sections:
                sections.append(sec)
            ordered.append(it)

    # Genuine ambiguity: multiple distinct named shelf sections (>=2 items in each) compete cleanly
    section_counts: dict[str, int] = {}
    for it in ordered:
        sec = str(it.get("section") or "").strip()
        if sec:
            section_counts[sec] = section_counts.get(sec, 0) + 1
    competing_sections = [s for s, cnt in section_counts.items() if cnt >= 2]
    ambiguous = len(competing_sections) >= 2
    return ordered, rows, ambiguous, competing_sections


def _classify_page_state(title: str, body: str = "") -> str:
    text = f"{title}\n{body}".casefold()
    if any(k in text for k in (
        "this site can't be reached", "err_name_not_resolved", "err_connection",
        "err_internet_disconnected", "404 not found", "page isn't working",
        "problem loading page", "dns_probe_finished",
    )):
        return "error"
    if any(k in text for k in (
        "scan qr code", "use whatsapp on your computer", "link with phone number",
        "sign in", "log in", "enter your password", "choose an account",
    )):
        return "login_required"
    if any(k in text for k in ("loading", "connecting", "opening")):
        return "loading"
    return "loaded" if title.strip() else "unverified"


def _is_non_target_chrome_title(title: str, url: str = "") -> tuple[bool, str]:
    """Detect blank tabs, DevTools/Docs overview pages, and Google Search SERPs."""
    t = str(title or "").strip().casefold()
    u = str(url or "").strip().casefold()
    if not u and not t:
        return True, "empty"
    if u in {"about:blank", "chrome://newtab", "chrome://newtab/", "edge://newtab", "edge://newtab/"} or t in {
        "new tab", "new tab - google chrome", "new tab - microsoft edge", "untitled",
    }:
        return True, "blank"
    if "project overview" in t:
        return True, "wrong_tab"
    if "google search" in t or t.endswith(" - google search - google chrome") or (
        "google." in u and "/search" in u
    ):
        return True, "google_search"
    return False, ""


def _site_key_from_url_or_title(url: str = "", title: str = "") -> str:
    url_cf = str(url or "").casefold()
    title_cf = str(title or "").casefold()
    non_target, kind = _is_non_target_chrome_title(title, url)
    if non_target:
        return "google" if kind == "google_search" else ""
    combined = f"{url_cf} {title_cf}"
    if "web.whatsapp.com" in url_cf or "whatsapp" in combined:
        return "whatsapp"
    if "youtube.com" in url_cf or "youtu.be" in url_cf or "youtube" in combined:
        return "youtube"
    if "google.com" in url_cf or title_cf.strip() in {"google", "google - google chrome"}:
        return "google"
    return ""


def verify_site_page_identity(
    site: str,
    *,
    url: str = "",
    title: str = "",
    body: str = "",
    dom_ready: bool | None = None,
    uia_elements=None,
) -> dict:
    """Verify independent site identity; reject New Tab, Project Overview, and SERPs."""
    site_cf = str(site or "").strip().casefold()
    url_s = str(url or "").strip()
    url_cf = url_s.casefold()
    title_s = str(title or "").strip()
    title_cf = title_s.casefold()
    uia_text = " ".join(
        str(getattr(el, "name", "") or getattr(el, "label", "") or getattr(el, "value", "") or "")
        for el in (uia_elements or ())
    )
    content_cf = f"{title_s}\n{body}\n{uia_text}".casefold()

    base_state = _classify_page_state(title_s, body)
    if base_state == "error":
        return {
            "verified": False, "target_matched": False, "site": site_cf,
            "page_state": "error", "reason": f"browser error page displayed ({title_s or url_s})",
            "url_match": False, "title_match": False, "content_match": False,
        }

    non_target, kind = _is_non_target_chrome_title(title_s, url_s)
    if kind == "blank":
        return {
            "verified": False, "target_matched": False, "site": site_cf,
            "page_state": "blank", "reason": "active tab is blank or New Tab",
            "url_match": False, "title_match": False, "content_match": False,
        }
    if kind == "wrong_tab":
        return {
            "verified": False, "target_matched": False, "site": site_cf,
            "page_state": "wrong_tab", "reason": f"wrong Chrome window/tab active ({title_s})",
            "url_match": False, "title_match": False, "content_match": False,
        }
    if kind == "google_search" and site_cf in {"whatsapp", "youtube"}:
        return {
            "verified": False, "target_matched": False, "site": site_cf,
            "page_state": "wrong_tab",
            "reason": f"Chrome shows a Google Search results page ({title_s}), not {site_cf}",
            "url_match": False, "title_match": False, "content_match": False,
        }

    url_host = ""
    if url_cf:
        try:
            url_host = (urlsplit(url_s).hostname or "").casefold()
        except Exception:
            url_host = ""

    url_match = False
    title_match = False
    content_match = False
    if site_cf == "whatsapp":
        url_match = ("web.whatsapp.com" in url_host) or ("web.whatsapp.com" in url_cf)
        if url_cf and not url_match:
            return {
                "verified": False, "target_matched": False, "site": site_cf,
                "page_state": "wrong_tab", "reason": f"tab URL '{url_s}' is not web.whatsapp.com",
                "url_match": False, "title_match": False, "content_match": False,
            }
        title_match = ("whatsapp" in title_cf) and ("google search" not in title_cf)
        content_match = any(
            k in content_cf for k in (
                "chats", "search or start new chat", "whatsapp", "scan qr code",
                "use whatsapp on your computer", "link with phone number", "type a message",
            )
        )
        matched = bool((url_match or (not url_cf and title_match)) and (title_match or content_match or url_match))
    elif site_cf == "youtube":
        url_match = ("youtube.com" in url_host or "youtu.be" in url_host or "youtube.com" in url_cf)
        if url_cf and not url_match:
            return {
                "verified": False, "target_matched": False, "site": site_cf,
                "page_state": "wrong_tab", "reason": f"tab URL '{url_s}' is not youtube.com",
                "url_match": False, "title_match": False, "content_match": False,
            }
        title_match = ("youtube" in title_cf) and ("google search" not in title_cf)
        content_match = any(
            k in content_cf for k in (
                "youtube", "search", "watch", "shorts", "subscriptions", "home",
                "trending", "history", "play", "pause", "हिन्दी", "hindi", "खोजें",
            )
        )
        matched = bool((url_match or (not url_cf and title_match)) and (title_match or content_match or url_match))
    elif site_cf:
        host, hints = _expected_site_hints(url_s or site_cf)
        url_match = bool(host and host in url_host) if url_cf else False
        title_match = any(h in title_cf for h in hints) if title_cf else False
        if url_cf and host and not url_match and not title_match:
            return {
                "verified": False, "target_matched": False, "site": site_cf,
                "page_state": "wrong_tab", "reason": f"tab URL '{url_s}' does not match expected site '{site_cf}'",
                "url_match": False, "title_match": False, "content_match": False,
            }
        content_match = title_match or url_match
        matched = bool(url_match or title_match)
    else:
        matched = bool((url_cf and url_cf != "about:blank") or title_s)

    if base_state == "loading" or dom_ready is False:
        return {
            "verified": False, "target_matched": False, "site": site_cf,
            "page_state": "loading", "reason": "target page is still loading",
            "url_match": url_match, "title_match": title_match, "content_match": content_match,
        }

    if not matched:
        return {
            "verified": False, "target_matched": False, "site": site_cf,
            "page_state": "wrong_tab" if (title_s or url_s) else "unverified",
            "reason": f"visible page '{title_s or url_s or 'unknown'}' does not match requested site '{site_cf}'",
            "url_match": url_match, "title_match": title_match, "content_match": content_match,
        }

    if base_state == "login_required":
        return {
            "verified": True, "target_matched": True, "site": site_cf,
            "page_state": "login_required", "reason": f"{site_cf} login/QR screen verified",
            "url_match": url_match, "title_match": title_match, "content_match": content_match,
        }

    return {
        "verified": True, "target_matched": True, "site": site_cf,
        "page_state": "loaded" if (title_s or url_match) else "unverified",
        "reason": f"{site_cf or 'page'} identity verified",
        "url_match": url_match, "title_match": title_match, "content_match": content_match,
    }


def youtube_hindi_intent(text: str) -> dict:
    """Detect Hindi localization requests for YouTube (`Open YouTube in Hindi` / language change)."""
    cf = " ".join(str(text or "").casefold().split())
    if not cf or ("youtube" not in cf) or ("hindi" not in cf and "हिन्दी" not in cf):
        return {"is_hindi": False, "mode": "", "url": "", "language": ""}
    is_change = any(k in cf for k in ("change", "switch", "set", "language", "badlo", "karo"))
    return {
        "is_hindi": True,
        "mode": "change_language" if is_change else "open_in_hindi",
        "url": "https://www.youtube.com/?hl=hi&gl=IN",
        "language": "hi",
    }


def _infer_requested_site(action: str, params: dict | None = None, request_text: str = "") -> str:
    p = params or {}
    act = str(action or "").lower().strip()
    if act in ("search_contact", "select_contact", "compose_message", "update_draft"):
        return "whatsapp"
    url_hint = _site_key_from_url_or_title(str(p.get("url") or ""), "")
    if url_hint:
        return url_hint
    for k in ("description", "text", "target", "query", "engine"):
        site = _site_key_from_url_or_title("", str(p.get(k) or ""))
        if site:
            return site
    if not request_text:
        try:
            from core import desktop_control as _dc
            ctx = _dc.current_bound_auth_context()
            if ctx is not None:
                request_text = str(getattr(ctx, "instruction", "") or "")
                site_req = _site_key_from_url_or_title("", request_text)
                if site_req:
                    return site_req
                parent = str(getattr(ctx, "parent_instruction", "") or "")
                site_p = _site_key_from_url_or_title("", parent)
                if site_p:
                    return site_p
        except Exception:
            pass
    site = _site_key_from_url_or_title("", request_text)
    if site:
        return site
    if act in ("play_video", "replay_video", "next_video", "pause_video", "resume_video",
               "seek_forward", "seek_backward", "set_playback_position", "focus_search"):
        return "youtube"
    # Preserve last verified site in active browser target registry across follow-ups ("Scroll down", etc.)
    try:
        from core import desktop_control as _dc
        tgt = _dc.get_active_target(_dc.current_bound_auth_context())
        if tgt:
            tab_site = _site_key_from_url_or_title(str(tgt.get("tab_url") or ""), str(tgt.get("tab_title") or ""))
            if tab_site:
                return tab_site
    except Exception:
        pass
    try:
        reg = _registry.get_target_registry(_registry._active_browser or "chrome")
        fg = reg.get("foreground") or {}
        fg_site = str(fg.get("site") or "").strip().casefold()
        if fg_site in ("whatsapp", "youtube"):
            return fg_site
    except Exception:
        pass
    return ""


def _win32_window_identity_for_title(browser_name: str, title_query: str = "", site_hint: str = "") -> tuple[int, int, str, str]:
    """Return (hwnd, pid, app, title) for the best matching external browser window on Windows."""
    if _OS != "Windows":
        return 0, 0, "", ""
    try:
        import ctypes
        from ctypes import wintypes
        import psutil
        from core import screenview

        user32 = ctypes.windll.user32
        WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
        tq = str(title_query or "").strip().casefold()
        sh = str(site_hint or "").strip().casefold()
        matches: list[tuple[int, int, int, str, str]] = []

        def _visit(hwnd, _lp):
            if not user32.IsWindowVisible(hwnd):
                return True
            length = user32.GetWindowTextLengthW(hwnd)
            if length <= 0:
                return True
            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buf, length + 1)
            title = buf.value or ""
            pid = wintypes.DWORD(0)
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            pid_i = int(pid.value or 0)
            if pid_i and screenview.is_own_pid(pid_i):
                return True
            app_name = ""
            try:
                app_name = psutil.Process(pid_i).name()
            except Exception:
                pass
            if not _proc_matches_browser(app_name, title, browser_name):
                return True
            t_cf = title.casefold()
            score = 1
            if sh and _site_key_from_url_or_title("", title) == sh:
                score += 10
            if tq and tq in t_cf:
                score += 5
            matches.append((score, int(hwnd), pid_i, app_name or f"{browser_name}.exe", title))
            return True

        user32.EnumWindows(WNDENUMPROC(_visit), 0)
        if not matches:
            return 0, 0, "", ""
        matches.sort(key=lambda item: item[0], reverse=True)
        _, hwnd, pid_i, app_name, title = matches[0]
        return hwnd, pid_i, app_name, title
    except Exception:
        return 0, 0, "", ""


def inspect_open_chrome_tabs_local(browser_name: str = "chrome", *, site_hint: str = "") -> list[str]:
    """Return a non-intrusive local summary of open browser windows/tabs."""
    found: list[str] = []
    try:
        from core import screenview
        for app, title in screenview.all_windows():
            app_cf = str(app or "").casefold()
            title_cf = str(title or "").casefold()
            if browser_name in app_cf or "chrome" in title_cf or "edge" in title_cf:
                entry = f"{app or browser_name}: {title}"
                if site_hint and _site_key_from_url_or_title("", title) == site_hint:
                    found.insert(0, entry)
                else:
                    found.append(entry)
    except Exception:
        pass
    try:
        if _registry.has(browser_name):
            sess = _registry.get(browser_name)
            ctx = getattr(sess, "_context", None)
            pages = [p for p in (getattr(ctx, "pages", ()) or ()) if not p.is_closed()] if ctx is not None else []
            for p in pages:
                if site_hint and _site_key_from_url_or_title(getattr(p, "url", ""), "") == site_hint:
                    found.insert(0, f"CDP tab: {p.url}")
                    break
            else:
                page = getattr(sess, "_page", None)
                if page is not None and not page.is_closed():
                    found.insert(0, f"CDP tab: {page.url}")
    except Exception:
        pass
    return found


def _verify_native_browser(
    name: str,
    url: str,
    *,
    timeout_s: float | None = None,
    reused_existing: bool = False,
    initial_timings: dict | None = None,
) -> str:
    """Confirm active tab URL/title, page identity, load state and foreground visibility."""
    import time as _time
    t_start = _time.perf_counter()
    timings = dict(initial_timings or {})
    if timeout_s is None:
        try:
            default_t = "3.5" if (reused_existing and _site_key_from_url_or_title(url, "") == "whatsapp") else "2.5"
            timeout_s = float(os.environ.get("REM_BROWSER_VERIFY_TIMEOUT", default_t))
        except ValueError:
            timeout_s = 2.5

    host, hints = _expected_site_hints(url)
    site_hint = _site_key_from_url_or_title(url, "")
    deadline = _time.monotonic() + max(0.0, timeout_s)
    running = bool(appidentity.running(name))
    matched_title = ""
    matched_app = ""
    observed_url = ""
    matched_hwnd = 0
    matched_pid = 0
    matched_ident: dict = {}
    saw_loading = False
    loading_title = ""
    error_title = ""
    all_browser_titles: list[str] = []

    while True:
        t_win0 = _time.perf_counter()
        running = bool(appidentity.running(name))
        all_browser_titles = []
        matched_ident = {}
        try:
            from core import screenview
            for app, title in screenview.all_windows():
                if _proc_matches_browser(app, title, name):
                    all_browser_titles.append(str(title or ""))
                    if site_hint:
                        ident = verify_site_page_identity(site_hint, url="", title=str(title or ""))
                        if ident["page_state"] == "error":
                            error_title = str(title or "")
                            matched_ident = ident
                            break
                        if ident["page_state"] == "loading":
                            saw_loading = True
                            loading_title = str(title or "")
                            continue
                        if ident["verified"]:
                            matched_title = str(title or "")
                            matched_app = str(app or "")
                            matched_ident = ident
                            break
                    else:
                        title_cf = str(title or "").casefold()
                        if any(h in title_cf for h in hints):
                            ident = verify_site_page_identity("", url=url, title=str(title or ""))
                            if ident["page_state"] == "loading":
                                saw_loading = True
                                loading_title = str(title or "")
                                continue
                            matched_title = str(title or "")
                            matched_app = str(app or "")
                            matched_ident = ident
                            break
            if not matched_title and not error_title:
                fg_app, fg_title, fg_pid, fg_hwnd = screenview._foreground()
                if not screenview.is_own_pid(int(fg_pid or 0)) and _proc_matches_browser(fg_app, fg_title, name):
                    view = screenview.look(budget_s=0.6, allow_ocr=False, allow_pixel_digest=False)
                    for el in getattr(view, "elements", ()) or ():
                        val = str(getattr(el, "value", "") or getattr(el, "name", "") or "")
                        if host and host in val.casefold():
                            cand_url = val if "://" in val else f"https://{val}"
                            cand_title = str(fg_title or getattr(view, "title", "") or "")
                            ident = verify_site_page_identity(
                                site_hint, url=cand_url, title=cand_title, uia_elements=getattr(view, "elements", ()),
                            )
                            if ident["page_state"] == "error":
                                error_title = cand_title
                                observed_url = cand_url
                                matched_ident = ident
                                break
                            if ident["page_state"] == "loading":
                                saw_loading = True
                                loading_title = cand_title
                                observed_url = cand_url
                                continue
                            if ident["verified"]:
                                observed_url = cand_url
                                matched_title = cand_title
                                matched_app = str(fg_app or "")
                                matched_hwnd = int(fg_hwnd or 0)
                                matched_pid = int(fg_pid or 0)
                                matched_ident = ident
                                break
        except Exception:
            pass
        timings["window_resolution_ms"] = round(
            float(timings.get("window_resolution_ms", 0.0)) + (_time.perf_counter() - t_win0) * 1000.0, 1
        )
        if matched_title or error_title or _time.monotonic() >= deadline:
            break
        _time.sleep(0.35)

    if not url:
        timings["total_ms"] = round((_time.perf_counter() - t_start) * 1000.0, 1)
        if running:
            print(f"[Browser] Verified launch browser={name} reused={reused_existing} windows={len(all_browser_titles)}")
            return outcome.done(
                "browser_control", browser=name, opened=True,
                verified=True, browser_launched=True,
                reused_existing=reused_existing,
                navigation_lifecycle=NAV_SUCCESS,
                page_state="loaded" if all_browser_titles else "open",
                total_ms=timings["total_ms"],
            )
        print(f"[Browser] Unverified launch browser={name} (process not detected)")
        return outcome.unverified(
            "browser_control",
            "the browser was launched but has not appeared in the process list yet",
            browser=name, browser_launched=False, reused_existing=reused_existing,
            navigation_lifecycle=NAV_BROWSER_RESOLVED,
            page_state="unverified", verification_step="process_presence",
            total_ms=timings["total_ms"],
        )

    if error_title:
        timings["total_ms"] = round((_time.perf_counter() - t_start) * 1000.0, 1)
        print(f"[Browser] Navigation error detected browser={name} url={url} title={error_title!r}")
        return outcome.failed(
            "browser_control",
            f"the page reported an error ({error_title})",
            browser=name, url=observed_url or url, title=error_title,
            page_state="error", browser_launched=running,
            reused_existing=reused_existing, navigation_requested=True,
            navigation_completed=False, target_matched=False, foreground_verified=False,
            navigation_lifecycle=NAV_NAVIGATION_COMPLETED,
            total_ms=timings["total_ms"],
        )

    if matched_title:
        page_state = matched_ident.get("page_state") or _classify_page_state(matched_title)
        cand_win = {
            "found": True,
            "foreground": False,
            "browser": name,
            "app": matched_app or name,
            "title": matched_title,
            "hwnd": matched_hwnd,
            "pid": matched_pid,
        }
        t_act0 = _time.perf_counter()
        ok_fg, fg_win, fg_err = _ensure_browser_foreground(
            name, cand_win, title_hint=matched_title, site_hint=site_hint,
        )
        timings["tab_activation_ms"] = round((_time.perf_counter() - t_act0) * 1000.0, 1)
        timings["total_ms"] = round((_time.perf_counter() - t_start) * 1000.0, 1)
        if not ok_fg:
            return outcome.unverified(
                "browser_control",
                f"the page loaded ({matched_title}) but {name} did not reach the visible foreground: {fg_err}",
                browser=name, url=observed_url or url, title=matched_title,
                active_tab=matched_title, page_state=page_state,
                browser_launched=running, reused_existing=reused_existing,
                navigation_requested=True, navigation_completed=True,
                target_matched=True, foreground_verified=False, verified=False,
                navigation_lifecycle=NAV_PAGE_IDENTITY_VERIFIED,
                **{k: v for k, v in timings.items()},
            )
        if site_hint:
            _registry.note_site_target(name, site_hint, {
                "hwnd": int(fg_win.get("hwnd", 0) or matched_hwnd or 0),
                "pid": int(fg_win.get("pid", 0) or matched_pid or 0),
                "title": str(fg_win.get("title") or matched_title),
                "url": observed_url or url,
                "site": site_hint,
            })
        print(f"[Browser] Verified native navigation browser={name} reused={reused_existing} url={observed_url or url} page_state={page_state}")
        return outcome.done(
            "browser_control",
            verified=True,
            browser=name,
            url=observed_url or url,
            title=str(fg_win.get("title") or matched_title),
            active_tab=str(fg_win.get("title") or matched_title),
            window_hwnd=int(fg_win.get("hwnd", 0) or matched_hwnd or 0),
            window_pid=int(fg_win.get("pid", 0) or matched_pid or 0),
            page_state=page_state,
            browser_launched=running,
            reused_existing=reused_existing,
            navigation_requested=True,
            navigation_completed=True,
            target_matched=True,
            foreground_verified=True,
            navigation_lifecycle=NAV_SUCCESS,
            **{k: v for k, v in timings.items()},
        )

    timings["total_ms"] = round((_time.perf_counter() - t_start) * 1000.0, 1)
    if saw_loading:
        print(f"[Browser] Timed out while page remained loading browser={name} url={url} title={loading_title!r}")
        return outcome.timed_out(
            "browser_control",
            f"the page stayed in a loading state ({loading_title or url}) before identity could be verified",
            browser=name,
            url=observed_url or url,
            title=loading_title,
            browser_launched=running,
            reused_existing=reused_existing,
            navigation_requested=True,
            navigation_completed=False,
            target_matched=False,
            foreground_verified=False,
            page_state="loading",
            navigation_lifecycle=NAV_NAVIGATION_STARTED,
            **{k: v for k, v in timings.items()},
        )

    print(f"[Browser] Unverified native navigation browser={name} url={url} running={running}")
    return outcome.unverified(
        "browser_control",
        "navigation was requested, but active tab URL and visible target page identity could not be confirmed",
        browser=name,
        url=url,
        browser_launched=running,
        reused_existing=reused_existing,
        navigation_requested=True,
        navigation_completed=False,
        target_matched=False,
        foreground_verified=False,
        page_state="wrong_tab" if all_browser_titles else "unverified",
        verification_step="active_tab_url_and_title",
        navigation_lifecycle=NAV_NAVIGATION_STARTED,
        observed_tabs="; ".join(all_browser_titles[:4]) if all_browser_titles else None,
        **{k: v for k, v in timings.items()},
    )


def _native_outcome(name: str, url: str, *, reused_existing: bool = False) -> str:
    """Backward-compatible wrapper returning structured browser verification."""
    return _verify_native_browser(name, url, reused_existing=reused_existing)


def _open_native(url: str, browser_name: Optional[str]) -> str:
    """
    Opens the user's REAL browser normally — with their own profile,
    logged-in accounts and extensions. Never silently substitutes another
    browser when `browser_name` is explicitly specified.
    """
    url = _normalize_url(url) if url and url.strip() else ""
    if url == "about:blank":
        url = ""

    explicit_browser = bool(browser_name and str(browser_name).strip())
    name = None
    if explicit_browser:
        name = _ALIASES.get(browser_name.lower().strip(), browser_name.lower().strip())
        _registry.note_active_browser(name)
    else:
        name = _registry._active_browser or _detect_running_browser() or _detect_default_browser()

    # Specific browser -> launch or reuse its own executable, never silently substitute Edge.
    if name:
        t_res0 = time.perf_counter()
        try:
            reused_existing = bool(appidentity.running(name))
        except Exception:
            reused_existing = False
        req_text = ""
        try:
            from core import desktop_control as _dc
            _ctx = _dc.current_bound_auth_context()
            req_text = str(getattr(_ctx, "instruction", "") or "") if _ctx is not None else ""
        except Exception:
            pass
        yt_hi = youtube_hindi_intent(req_text)
        if yt_hi.get("is_hindi") and url and "youtube.com" in url.casefold() and "hl=hi" not in url.casefold():
            if yt_hi["mode"] == "open_in_hindi":
                url = yt_hi["url"]
        site_hint = _site_key_from_url_or_title(url, "") if url else ""
        if reused_existing and site_hint:
            existing_win = _find_browser_window(name, site_hint=site_hint)
            win_res_ms = round((time.perf_counter() - t_res0) * 1000.0, 1)
            wants_specific_query = bool(url and (urlsplit(url).query or len(urlsplit(url).path.strip("/")) > 0))
            if existing_win.get("found") and not existing_win.get("site_mismatch") and not wants_specific_query:
                ident = verify_site_page_identity(
                    site_hint, url="", title=str(existing_win.get("title") or ""),
                )
                if ident.get("verified"):
                    t_act0 = time.perf_counter()
                    ok_fg, fg_win, _fg_err = _ensure_browser_foreground(
                        name, existing_win,
                        title_hint=("WhatsApp" if site_hint == "whatsapp" else "YouTube" if site_hint == "youtube" else site_hint),
                        site_hint=site_hint,
                    )
                    act_ms = round((time.perf_counter() - t_act0) * 1000.0, 1)
                    if ok_fg:
                        final_title = str(fg_win.get("title") or existing_win.get("title") or "")
                        _registry.note_site_target(
                            name, site_hint,
                            {
                                "hwnd": int(fg_win.get("hwnd", 0) or 0),
                                "pid": int(fg_win.get("pid", 0) or 0),
                                "title": final_title,
                                "url": url,
                                "site": site_hint,
                            },
                        )
                        _registry.invalidate_sync(name)
                        return outcome.done(
                            "browser_control",
                            opened=url,
                            browser=name,
                            url=url,
                            title=final_title,
                            active_tab=final_title,
                            window_hwnd=int(fg_win.get("hwnd", 0) or 0),
                            window_pid=int(fg_win.get("pid", 0) or 0),
                            page_state=ident.get("page_state") or _classify_page_state(final_title),
                            browser_launched=True,
                            reused_existing=True,
                            focused_existing=True,
                            target_matched=True,
                            foreground_verified=True,
                            navigation_requested=True,
                            navigation_completed=True,
                            navigation_lifecycle=NAV_SUCCESS,
                            window_resolution_ms=win_res_ms,
                            tab_activation_ms=act_ms,
                            total_ms=round((time.perf_counter() - t_res0) * 1000.0, 1),
                        )
                elif ident.get("page_state") == "loading":
                    _ensure_browser_foreground(name, existing_win, title_hint=site_hint, site_hint=site_hint)
                    return _verify_native_browser(
                        name, url, reused_existing=True,
                        initial_timings={"window_resolution_ms": win_res_ms},
                    )
        # Deduplicate concurrent open requests for the same browser+site so repeated
        # commands like "Open WhatsApp in Chrome" wait/inspect cleanly instead of spawning duplicates.
        if site_hint and _is_opening_inflight(name, site_hint):
            return _wait_or_inspect_inflight_open(name, site_hint, url=url)
        if _OS == "Darwin":
            app = _MAC_APP_NAMES.get(name)
            if app:
                cmd = ["open", "-a", app] + ([url] if url else [])
                try:
                    subprocess.run(cmd, check=True, timeout=10)
                    return _native_outcome(name, url, reused_existing=reused_existing)
                except Exception as e:
                    print(f"[Browser] 'open -a {app}' failed ({e}), trying binary…")

        spec = _resolve_browser(name)
        exe  = spec.get("exe") if spec else None
        if not exe and _OS == "Windows":
            if name in ("opera", "operagx"):
                exe = _find_opera_windows()
            else:
                exe = _find_exe_windows(_WIN_EXE_HINTS.get(name, name))
        if exe:
            _mark_opening_inflight(name, site_hint, True)
            try:
                args = [exe] + (_launch_flags(name) if not reused_existing else []) + ([url] if url else [])
                subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return _native_outcome(name, url, reused_existing=reused_existing)
            except Exception as e:
                print(f"[Browser] Native launch failed for {name}: {e}")
                if explicit_browser:
                    return outcome.failed(
                        "browser_control",
                        f"requested browser '{name}' failed to launch ({e}); did not substitute another browser",
                        browser=name, url=url, reused_existing=reused_existing,
                    )
            finally:
                _mark_opening_inflight(name, site_hint, False)
        if explicit_browser:
            return outcome.failed(
                "browser_control",
                f"requested browser '{name}' was not found on this system; did not substitute another browser",
                browser=name, url=url, reused_existing=False,
            )
        print(f"[Browser] '{name}' not found — falling back to default browser.")

    if not url:
        return outcome.failed(
            "browser_control",
            "no browser was found and no address was given to open")

    # Default browser via the OS — exactly like the user clicking a link.
    try:
        if _OS == "Windows":
            os.startfile(url)                       # ShellExecute → default browser
        elif _OS == "Darwin":
            subprocess.run(["open", url], check=True, timeout=10)
        else:
            subprocess.Popen(
                ["xdg-open", url],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        # HANDED TO THE OPERATING SYSTEM, NOT CONFIRMED.
        #
        # os.startfile / open / xdg-open all return as soon as the request is
        # accepted. None of them waits for a browser, and none reports whether
        # the page loaded — the same gap that made open_app say "Opened
        # Spotify." off the back of a shell starting. Unlike the playwright
        # path above there is no page object here to read a url from, so this
        # says what it actually knows.
        return outcome.unverified(
            "browser_control",
            "the address was handed to the system's default browser, which "
            "does not report back - the page is not confirmed open",
            url=url)
    except Exception:
        try:
            if webbrowser.open(url):
                return outcome.unverified(
                    "browser_control",
                    "the address was handed to the default browser, which does "
                    "not report back - the page is not confirmed open",
                    url=url)
        except Exception:
            pass
        return outcome.failed(
            "browser_control",
            "no browser on this system could be opened", url=url)


class _SeparateProfileBlockedError(RuntimeError):
    """Raised when CDP is unavailable on an already-running browser so we never open a separate profile silently."""


class _BrowserSession:
    """
    A full session for one browser instance.
    All browsers open on the real profile via launch_persistent_context.
    """

    def __init__(self, browser_name: str):
        self.browser_name = browser_name
        self._spec        = _resolve_browser(browser_name)

        self._loop:    asyncio.AbstractEventLoop | None = None
        self._thread:  threading.Thread | None          = None
        self._ready    = threading.Event()

        self._pw:      Playwright     | None = None
        self._context: BrowserContext | None = None
        self._page:    Page           | None = None
        self._browser = None
        self._op_lock = threading.RLock()
        self.profile_kind: str = ""

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(
            target=self._run_loop,
            daemon=True,
            name=f"BrowserThread-{self.browser_name}",
        )
        self._thread.start()
        self._ready.wait(timeout=20)

    def _run_loop(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._loop.run_until_complete(self._async_init())
        self._ready.set()
        self._loop.run_forever()

    async def _async_init(self):
        # The one door into a live session: nothing in this class reaches
        # async_playwright or PlaywrightTimeout without coming through here
        # first, so binding them at this point is enough for the whole file.
        _load_playwright()
        self._pw = await async_playwright().start()

    def run(self, coro, timeout: int = 60) -> str:
        if not self._loop:
            raise RuntimeError(f"Session for '{self.browser_name}' not started.")
        lock = getattr(self, "_op_lock", None)
        if lock is None:
            lock = self._op_lock = threading.RLock()
        with lock:
            future = asyncio.run_coroutine_threadsafe(coro, self._loop)
            from core import desktop_control
            cancel = future.cancel
            desktop_control.register_cancel(cancel)
            try:
                return future.result(timeout=timeout)
            finally:
                desktop_control.unregister_cancel(cancel)

    def close(self):
        if self._loop:
            asyncio.run_coroutine_threadsafe(self._async_close(), self._loop).result(10)

    async def _async_close(self):
        if self._browser is not None:
            # Browser.close() on a CDP-connected Playwright Browser disconnects
            # from the browser server; closing the attached default context
            # would instead close the user's actual Chrome tabs.
            try:
                await self._browser.close()
            except Exception:
                pass
            self._browser = None
        elif self._context:
            try:
                await self._context.close()
            except Exception:
                pass
        if self._pw:
            try:
                await self._pw.stop()
            except Exception:
                pass
        self._context = self._page = None

    async def _adopt_page(self) -> Page:
        """
        launch_persistent_context already opens a starting tab.
        Instead of opening a new blank tab (about:blank), it adopts that tab —
        so the user never sees an extra blank tab.
        """
        await asyncio.sleep(0.3)
        pages = self._context.pages
        return pages[0] if pages else await self._context.new_page()

    async def _launch(self):
        """
        Launches the browser with the real user profile.
        Does nothing if the context is already open.
        """
        if self._context is not None:
            try:
                if (callable(getattr(self._context, "is_closed", None))
                        and self._context.is_closed()):
                    raise RuntimeError("browser context is closed")
                if (self._browser is not None
                        and callable(getattr(self._browser, "is_connected", None))
                        and not self._browser.is_connected()):
                    raise RuntimeError("attached browser disconnected")
                pages = [page for page in self._context.pages if not page.is_closed()]
                if self._page is None or self._page.is_closed():
                    self._page = pages[-1] if pages else None
                return
            except Exception:
                # Drop stale handles. The configured CDP endpoint or the
                # existing REM-owned persistent profile is reopened below;
                # never silently switch an attached session to another profile.
                self._browser = self._context = self._page = None

        endpoint = _configured_browser_cdp_endpoint(self.browser_name)
        auto_cdp = False
        if not endpoint:
            endpoint = _discover_running_cdp_endpoint(self.browser_name)
            auto_cdp = bool(endpoint)
        if endpoint:
            if self.browser_name in {"chrome", "edge"}:
                browser = None
                try:
                    browser = await self._pw.chromium.connect_over_cdp(
                        endpoint, timeout=5_000, no_defaults=True)
                    contexts = browser.contexts
                    if not contexts:
                        raise RuntimeError("the attached browser exposed no browser context")
                    context = contexts[0]
                    pages = [page for page in context.pages if not page.is_closed()]
                    self._browser = browser
                    self._context = context
                    # CDP does not report a reliable foreground-tab identity;
                    # use the most recently listed live tab and expose its URL
                    # through the ordinary browser read tools before acting.
                    self._page = pages[-1] if pages else None
                    self.profile_kind = "attached"
                    print(f"[Browser] Attached to existing local {self.browser_name.title()} CDP session")
                    return
                except Exception as exc:
                    if browser is not None:
                        try:
                            await browser.close()
                        except Exception:
                            pass
                    if not auto_cdp:
                        raise RuntimeError(
                            "Could not attach to the configured local browser CDP endpoint; "
                            "no browser action was taken. Check that the selected browser "
                            "was explicitly started with a loopback-only debugging port."
                        ) from exc

        if self._spec is None:
            raise RuntimeError(
                f"'{self.browser_name}' bu platformda ({_OS}) desteklenmiyor."
            )

        engine_name = self._spec["engine"]
        exe         = self._spec["exe"]
        channel     = self._spec["channel"]
        engine_obj  = getattr(self._pw, engine_name)

        if engine_name == "firefox":
            profile = _firefox_profile_dir() or str(_profile_path("firefox"))
            kwargs: dict = {
                "headless":    False,
                "slow_mo":     0,
                "viewport":    None,
                "no_viewport": True,
                "timeout":     25_000,
            }
            if exe:
                kwargs["executable_path"] = exe
            try:
                self._context = await engine_obj.launch_persistent_context(profile, **kwargs)
            except Exception as e:
                print(f"[Browser] Firefox real profile failed ({e}), using REM profile")
                profile = str(_profile_path("firefox_rem", "firefox_jarvis"))
                Path(profile).mkdir(parents=True, exist_ok=True)
                self._context = await engine_obj.launch_persistent_context(profile, **kwargs)

            self._page = await self._adopt_page()
            print(f"[Browser] ✅ Firefox launched")
            return

        if engine_name == "webkit":
            safari_profile = str(_profile_path("safari"))
            Path(safari_profile).mkdir(parents=True, exist_ok=True)
            kwargs = {
                "headless":    False,
                "slow_mo":     0,
                "viewport":    None,
                "no_viewport": True,
                "timeout":     25_000,
            }
            self._context = await engine_obj.launch_persistent_context(safari_profile, **kwargs)
            self._page = await self._adopt_page()
            print(f"[Browser] ✅ Safari launched")
            return

        profile = _real_profile_dir(self.browser_name)

        kwargs = {
            "headless":    False,
            "slow_mo":     0,
            "viewport":    None,
            "no_viewport": True,
            "timeout":     25_000,
            "args": [
                "--start-maximized",
                "--disable-blink-features=AutomationControlled",
                "--no-first-run",
                "--disable-default-apps",
                "--no-default-browser-check",
            ],
        }

        if exe:
            kwargs["executable_path"] = exe
        elif channel:
            kwargs["channel"] = channel

        label = (
            f"{self.browser_name}"
            + (f"/{channel}" if channel else "")
            + (f" @ {exe}" if exe else "")
        )

        # ASK FIRST, INSTEAD OF WAITING 25 SECONDS TO BE TOLD.
        #
        # A Chromium profile can only be open once. If the user already has the
        # browser running — which is the normal state of a browser — then
        # launch_persistent_context on their real profile cannot succeed, and
        # the way it fails is by sitting on the timeout above before raising.
        # Every "open this page" then cost twenty-five seconds of nothing
        # before the fallback even started, and the fallback is a profile with
        # none of their sign-ins in it.
        #
        # Whether the browser is running is a question the process table
        # answers in milliseconds, and core/appidentity was already built to
        # ask it. Knowing up front turns a long silence into an immediate,
        # explainable outcome.
        already = appidentity.running(self.browser_name)
        if already:
            if os.environ.get("REM_ALLOW_SEPARATE_BROWSER_PROFILE", "").strip() != "1":
                print(
                    f"[Browser] {self.browser_name} is already running "
                    f"({len(already)} processes) without CDP — keeping user's "
                    "visible browser window instead of opening a separate REM profile."
                )
                raise _SeparateProfileBlockedError(
                    f"{self.browser_name} is already running without an exposed CDP debugging port; "
                    "not launching a separate automation profile."
                )
            print(f"[Browser] {self.browser_name} is already running "
                  f"({len(already)} processes) — its profile is locked, so the "
                  f"separate automation profile will be used.")
        else:
            try:
                self._context = await engine_obj.launch_persistent_context(profile, **kwargs)
                self._page = await self._adopt_page()
                self.profile_kind = "real"
                print(f"[Browser] ✅ Launched [{label}] profile={profile}")
                return
            except Exception as e:
                print(f"[Browser] ⚠️  Real profile failed for {label}: {e}")

        # The real profile could not be opened (browser already open / locked
        # profile / newer Chrome versions block the real profile under
        # automation). Fall back to a persistent REM automation profile —
        # accounts logged in here once stay logged in on later sessions too.
        profile_path = str(_profile_path(self.browser_name))
        Path(profile_path).mkdir(parents=True, exist_ok=True)
        print(f"[Browser] Retrying with REM profile: {profile_path}")

        try:
            self._context = await engine_obj.launch_persistent_context(profile_path, **kwargs)
            self._page = await self._adopt_page()
            self.profile_kind = "separate"
            print(f"[Browser] ✅ Launched [{label}] with REM profile "
                  f"(sign-ins persist across sessions)")
        except Exception as e2:
            raise RuntimeError(f"Could not launch {self.browser_name}: {e2}") from e2


    async def _get_page(self) -> Page:
        await self._launch()
        if self._page is None or self._page.is_closed():
            try:
                live = [p for p in (getattr(self._context, "pages", ()) or ()) if not p.is_closed()]
                self._page = (await self._foreground_page(live)) if live else await self._context.new_page()
            except Exception as exc:
                # Stale/closed BrowserContext (e.g. browser closed manually or CDP restarted):
                # drop handles cleanly and recover the session once instead of failing new_page.
                print(f"[Browser] Recovering closed {self.browser_name} context ({type(exc).__name__})")
                self._browser = self._context = self._page = None
                await self._launch()
                if self._page is None or self._page.is_closed():
                    self._page = await self._context.new_page()
            await asyncio.sleep(0.15)
        return self._page

    @staticmethod
    async def _foreground_page(pages):
        """The tab the person is looking at (visible), else the last one. The old code
        always took the LAST tab, which is whatever opened most recently."""
        for p in reversed(list(pages)):
            try:
                if await p.evaluate("document.visibilityState === 'visible'") is True:
                    return p
            except Exception:
                continue
        return list(pages)[-1]

    async def _youtube_page(self):
        """The YouTube tab player controls must act on, or None.

        Prefers a /watch tab, then a visible one. Never returns a non-YouTube tab,
        so a key meant for the player cannot land in WhatsApp or the address bar.
        """
        def _is_yt(page) -> bool:
            try:
                h = (urlsplit(page.url).hostname or "").casefold()
            except Exception:
                return False
            return h == "youtu.be" or h == "youtube.com" or h.endswith(".youtube.com")

        if getattr(self, "_context", None) is None:
            page = await self._get_page()          # launches/attaches when needed
            if getattr(self, "_context", None) is None:
                return page if _is_yt(page) else None
        pages = [p for p in (getattr(self._context, "pages", ()) or ()) if not p.is_closed()]
        cands = []
        for p in pages:
            try:
                u = urlsplit(p.url)
            except Exception:
                continue
            host = (u.hostname or "").casefold()
            if host == "youtu.be" or host == "youtube.com" or host.endswith(".youtube.com"):
                try:
                    vis = (await p.evaluate("document.visibilityState === 'visible'")) is True
                except Exception:
                    vis = False
                cands.append((u.path == "/watch", vis, p is self._page, p))
        if not cands:
            return None
        cands.sort(key=lambda c: (c[0], c[1], c[2]), reverse=True)
        chosen = cands[0][3]
        if chosen is not self._page:
            self._page = chosen
            try:
                await chosen.bring_to_front()
            except Exception:
                pass
        return chosen

    async def _page_visible_for_action(self, page) -> bool:
        """Refuse input to a hidden/background page whose actions are not observable."""
        try:
            return await page.evaluate("document.visibilityState === 'visible'") is True
        except Exception:
            return False

    async def _get_page_for_site(self, site_hint: str = "") -> Page:
        page = await self._get_page()
        sh = str(site_hint or "").strip().casefold()
        if not sh or getattr(self, "_context", None) is None:
            return page
        pages = [p for p in (getattr(self._context, "pages", ()) or ()) if not p.is_closed()]
        matching = [p for p in pages if _site_key_from_url_or_title(getattr(p, "url", ""), "") == sh]
        if matching:
            chosen = page if page in matching else matching[-1]
            self._page = chosen
            try:
                await chosen.bring_to_front()
            except Exception:
                pass
            return chosen
        return page

    async def go_to(self, url: str) -> str:
        t0 = time.perf_counter()
        try:
            url = _normalize_url(url)
        except ValueError:
            return outcome.ask(
                "browser_control",
                "Which website did you mean? Please give me its name or web address.",
                verified=False,
            )
        yt_hi: dict = {"is_hindi": False, "mode": "", "url": "", "language": ""}
        try:
            from core import desktop_control as _dc
            _ctx = _dc.current_bound_auth_context()
            _req_txt = str(getattr(_ctx, "instruction", "") or "") if _ctx is not None else ""
            yt_hi = youtube_hindi_intent(_req_txt)
            if yt_hi.get("is_hindi") and yt_hi.get("mode") == "open_in_hindi" and "youtube.com" in url.casefold() and "hl=hi" not in url.casefold():
                url = yt_hi["url"]
        except Exception:
            pass

        site_hint = _site_key_from_url_or_title(url, "")
        t_tab0 = time.perf_counter()
        page = await self._get_page_for_site(site_hint)
        tab_resolution_ms = round((time.perf_counter() - t_tab0) * 1000.0, 1)
        prev_url = page.url or ""
        navigation_error = None

        # Tab reuse: if selected tab already hosts the target site and no distinct sub-path/query/language change was asked
        wants_specific_nav = bool(urlsplit(url).query or len(urlsplit(url).path.strip("/")) > 0)
        if site_hint and _site_key_from_url_or_title(prev_url, "") == site_hint and not wants_specific_nav and not yt_hi.get("is_hindi"):
            if await self._page_visible_for_action(page):
                try:
                    cur_title = await page.title()
                except Exception:
                    cur_title = ""
                try:
                    cur_body = (await page.locator("body").inner_text(timeout=800))[:800]
                except Exception:
                    cur_body = ""
                ident = verify_site_page_identity(
                    site_hint, url=prev_url, title=cur_title, body=cur_body, dom_ready=True,
                )
                if ident.get("verified"):
                    return outcome.done(
                        "browser_control",
                        opened=prev_url,
                        browser=self.browser_name,
                        url=prev_url,
                        title=cur_title or "",
                        page_state=ident.get("page_state") or "loaded",
                        profile=self.profile_kind or "real",
                        reused_existing=True,
                        tab_reused=True,
                        verified=True,
                        navigation_confirmed=True,
                        target_matched=True,
                        foreground_verified=True,
                        page_ready=True,
                        requested_url=url,
                        navigation_lifecycle=NAV_SUCCESS,
                        tab_resolution_ms=tab_resolution_ms,
                        total_ms=round((time.perf_counter() - t0) * 1000.0, 1),
                    )

        # If current tab holds a DIFFERENT live site (e.g. switching between WhatsApp and YouTube),
        # prefer opening a tab only when no matching site tab existed and current tab isn't blank.
        prev_site = _site_key_from_url_or_title(prev_url, "")
        if site_hint and prev_site and prev_site != site_hint and getattr(self, "_context", None) is not None:
            try:
                page = await self._context.new_page()
                self._page = page
                prev_url = page.url or ""
                try:
                    await page.bring_to_front()
                except Exception:
                    pass
            except Exception:
                pass

        if not await self._page_visible_for_action(page):
            return outcome.unverified(
                "browser_control", "browser page is hidden; navigation was not attempted",
                requested=url, actual=prev_url, navigation_confirmed=False,
                foreground_verified=False, target_matched=False,
                navigation_lifecycle=NAV_TAB_SELECTED,
            )

        async def _do_goto(p: Page) -> str:
            """Attempt navigation and return the resulting URL (may still be blank)."""
            nonlocal navigation_error
            try:
                await p.goto(url, wait_until="domcontentloaded", timeout=30_000)
                await asyncio.sleep(0.3)
            except PlaywrightTimeout:
                navigation_error = "timeout"
            except Exception as e:
                navigation_error = type(e).__name__
            return p.url

        t_nav0 = time.perf_counter()
        result_url = await _do_goto(page)

        if result_url in ("about:blank", "", None, prev_url) and prev_url in ("about:blank", "", None):
            print(f"[Browser] Still blank after goto — retrying on new tab: {url}")
            try:
                new_page   = await self._context.new_page()
                self._page = new_page
                page = new_page
                result_url = await _do_goto(new_page)
            except Exception as e:
                print(f"[Browser] New-tab retry failed: {e}")
        navigation_ms = round((time.perf_counter() - t_nav0) * 1000.0, 1)

        if result_url and result_url not in ("about:blank", "", None):
            t_dom0 = time.perf_counter()
            page_ready = False
            title = ""
            body_sample = ""
            ident: dict = {}
            settle_deadline = time.monotonic() + 2.5
            while True:
                try:
                    page_ready = await page.evaluate(
                        "() => document.readyState === 'interactive' || document.readyState === 'complete'")
                except Exception:
                    page_ready = False
                try:
                    title = await page.title()
                except Exception:
                    title = ""
                try:
                    body_sample = (await page.locator("body").inner_text(timeout=1_200))[:1200]
                except Exception:
                    body_sample = ""
                ident = verify_site_page_identity(
                    site_hint, url=result_url, title=title, body=body_sample, dom_ready=page_ready,
                )
                if ident.get("page_state") != "loading" or time.monotonic() >= settle_deadline:
                    break
                await asyncio.sleep(0.25)
            dom_verification_ms = round((time.perf_counter() - t_dom0) * 1000.0, 1)

            if not page_ready:
                if navigation_error == "timeout" or ident.get("page_state") == "loading":
                    return outcome.timed_out(
                        "browser_control",
                        "the browser URL changed, but the page remained loading past the readiness deadline",
                        requested=url, actual=result_url, navigation_confirmed=True,
                        page_ready=False, target_matched=False, foreground_verified=False,
                        error=navigation_error, page_state="loading",
                        navigation_lifecycle=NAV_NAVIGATION_STARTED,
                        tab_resolution_ms=tab_resolution_ms, navigation_ms=navigation_ms,
                        dom_verification_ms=dom_verification_ms,
                    )
                return outcome.unverified(
                    "browser_control",
                    "the browser URL changed, but the page did not confirm DOM readiness",
                    requested=url, actual=result_url, navigation_confirmed=True,
                    page_ready=False, target_matched=False, foreground_verified=False,
                    error=navigation_error, navigation_lifecycle=NAV_NAVIGATION_COMPLETED,
                )
            if ident.get("page_state") == "error":
                print(f"[Browser] Navigation error page browser={self.browser_name} url={result_url} title={title!r}")
                return outcome.failed(
                    "browser_control",
                    f"the requested page failed to load ({title or result_url})",
                    browser=self.browser_name, url=result_url, title=title, page_state="error",
                    target_matched=False, foreground_verified=False,
                    navigation_lifecycle=NAV_NAVIGATION_COMPLETED,
                )
            if not await self._page_visible_for_action(page):
                return outcome.unverified(
                    "browser_control",
                    "the URL loaded, but the browser page is hidden; visible navigation is not confirmed",
                    requested=url, actual=result_url, navigation_confirmed=False,
                    page_ready=True, target_matched=bool(ident.get("target_matched")),
                    foreground_verified=False, browser=self.browser_name, page_state="hidden",
                    navigation_lifecycle=NAV_PAGE_IDENTITY_VERIFIED,
                )
            if site_hint and not ident.get("verified"):
                return outcome.unverified(
                    "browser_control",
                    ident.get("reason") or "the loaded tab did not match the requested target site",
                    requested=url, actual=result_url, title=title or "",
                    browser=self.browser_name, page_state=ident.get("page_state") or "wrong_tab",
                    navigation_confirmed=False, target_matched=False,
                    foreground_verified=True, page_ready=page_ready,
                    navigation_lifecycle=NAV_NAVIGATION_COMPLETED,
                )
            page_state = ident.get("page_state") or _classify_page_state(title or result_url, body_sample)
            if yt_hi.get("mode") == "change_language":
                try:
                    html_lang = str(await page.evaluate("() => (document.documentElement.lang || '').toLowerCase()") or "")
                except Exception:
                    html_lang = ""
                hindi_confirmed = (
                    html_lang.startswith("hi")
                    or "hl=hi" in result_url.casefold()
                    or any(w in f"{title}\n{body_sample}" for w in ("हिन्दी", "खोजें", "होम"))
                )
                if not hindi_confirmed:
                    return outcome.unverified(
                        "browser_control",
                        "I opened YouTube, but could not confirm that the display language switched to Hindi; you can set Settings > Language > हिन्दी.",
                        requested=url, actual=result_url, title=title or "",
                        browser=self.browser_name, page_state=page_state,
                        target_matched=True, foreground_verified=True,
                        language_verified=False,
                        navigation_lifecycle=NAV_FOREGROUND_VERIFIED,
                    )
            print(f"[Browser] Verified navigation browser={self.browser_name} url={result_url} page_state={page_state}")
            return outcome.done(
                "browser_control", opened=result_url,
                browser=self.browser_name, url=result_url,
                title=title or "", page_state=page_state,
                profile=self.profile_kind or "real",
                verified=True, navigation_confirmed=True,
                target_matched=True, foreground_verified=True,
                page_ready=True, requested_url=url,
                requested_url_match=(result_url.rstrip("/") == url.rstrip("/")),
                navigation_lifecycle=NAV_SUCCESS,
                tab_resolution_ms=tab_resolution_ms,
                navigation_ms=navigation_ms,
                dom_verification_ms=dom_verification_ms,
                total_ms=round((time.perf_counter() - t0) * 1000.0, 1),
                profile_notice=(
                    "separate profile; existing signed-in profile was not used"
                    if self.profile_kind == "separate" else None
                ),
            )
        if navigation_error == "timeout":
            return outcome.timed_out(
                "browser_control",
                "navigation timed out before the page loaded",
                requested=url, actual=result_url or "", navigation_confirmed=False,
                target_matched=False, foreground_verified=False,
                navigation_lifecycle=NAV_NAVIGATION_STARTED,
            )
        if result_url == prev_url and prev_url not in ("about:blank", "", None):
            return outcome.unverified(
                "browser_control",
                "the requested navigation was accepted, but the current page URL did not change",
                requested=url, actual=result_url, navigation_confirmed=False,
                page_ready=False, target_matched=False, foreground_verified=False,
                error=navigation_error, navigation_lifecycle=NAV_NAVIGATION_STARTED,
            )
        return outcome.failed(
            "browser_control",
            "the page stayed blank - the address may be wrong, or the site may "
            "not have answered", url=url, target_matched=False, foreground_verified=False,
            navigation_lifecycle=NAV_NAVIGATION_COMPLETED,
        )

    async def search(self, query: str, engine: str = "google") -> str:
        chosen = str(engine or "").lower().strip()
        if not chosen or chosen == "google":
            try:
                cur_host = (urlsplit(self._page.url).hostname or "").casefold() if self._page else ""
                if "youtube.com" in cur_host and engine in ("", None, "youtube"):
                    chosen = "youtube"
            except Exception:
                pass
        base = _SEARCH_ENGINES.get(chosen or "google", _SEARCH_ENGINES["google"])
        return await self.go_to(base + query.replace(" ", "+"))

    async def click(self, selector: str = None, text: str = None) -> str:
        page = await self._get_page()
        try:
            before = await self._page_state(page)
            if text:
                loc = page.get_by_text(text, exact=False)
            if selector:
                loc = page.locator(selector)
            if text and selector:
                return outcome.failed("browser_control", "name a text target or a CSS selector, not both")
            return outcome.failed("browser_control",
                                  "no element was named to click") if not (text or selector) else await self._click_unique(
                                      page, loc, before, text or selector)
        except PlaywrightTimeout:
            return outcome.failed(
                "browser_control",
                "no element matching that appeared on the page in time",
                selector=selector or text)
        except Exception as e:
            return f"Click error: {e}"

    async def type_text(self, selector: str = None, text: str = "",
                        clear_first: bool = True) -> str:
        page = await self._get_page()
        try:
            el = page.locator(selector) if selector else page.locator(":focus")
            count = await el.count()
            if count != 1:
                return (f"Ambiguous input: {count} elements matched. Provide a unique accessible label."
                        if count > 1 else "No input matched.")
            if not await el.is_editable():
                return "The matched element is not an editable field."
            if (await el.get_attribute("type") or "").casefold() == "password":
                return "Password fields are protected. Enter credentials yourself."
            if clear_first:
                await el.fill(str(text))
            else:
                await el.press_sequentially(str(text), delay=35)
            value = await el.input_value()
            if value != str(text) and clear_first:
                return outcome.unverified("browser_control", "text entry did not read back exactly")
            return outcome.done("browser_control", typed=True, verified=True)
        except Exception as e:
            return f"Type error: {e}"

    async def scroll(self, direction: str = "down", amount: int = 500) -> str:
        page = await self._get_page()
        try:
            d = str(direction or "down").lower().strip()
            mag = max(80, min(2000, abs(int(amount or 500))))
            dx, dy = 0, 0
            if d == "up":
                dy = -mag
            elif d == "left":
                dx = -mag
            elif d == "right":
                dx = mag
            else:
                dy = mag
            await page.mouse.wheel(dx, dy)
            try:
                from core import desktop_control as _dc
                _dc.clear_task_targets()
            except Exception:
                pass
            return f"Scrolled {d}."
        except Exception as e:
            return f"Scroll error: {e}"

    async def press(self, key: str) -> str:
        page = await self._get_page()
        try:
            if not await self._page_visible_for_action(page):
                return outcome.unverified(
                    "browser_control", "browser page is hidden; no key was sent")
            search_submit = False
            before_url = page.url
            if str(key).strip().casefold() in ("enter", "return"):
                focused = page.locator(":focus")
                if await focused.count() == 1:
                    meta = await focused.evaluate("el => ({tag:el.tagName, type:el.type||'', role:el.getAttribute('role')||'', label:el.getAttribute('aria-label')||'', placeholder:el.getAttribute('placeholder')||'', name:el.getAttribute('name')||'', editable:el.isContentEditable})")
                    text = " ".join(str(v) for v in meta.values()).casefold()
                    search = (meta.get("type") == "search" or meta.get("role") in ("searchbox", "combobox")
                              or any(x in text for x in ("search", "query", "address")))
                    search_submit = search and (
                        meta.get("tag") in ("INPUT", "TEXTAREA")
                        or meta.get("role") in ("searchbox", "combobox")
                    )
                    chat = any(x in text for x in ("message", "reply", "chat", "comment"))
                    if chat:
                        return "I will not submit a message or comment through a generic Enter key. Use the explicit send action and confirm it."
                    if ((meta.get("editable") and not search)
                            or (meta.get("tag") in ("INPUT", "TEXTAREA")
                                and not search)):
                        return "Enter could submit this form; use an explicit named button, which will request confirmation when consequential."
            await page.keyboard.press(key)
            if search_submit:
                actual_url = page.url
                for _ in range(12):
                    await asyncio.sleep(0.15)
                    actual_url = page.url
                    if actual_url != before_url:
                        break
                try:
                    title = await page.title()
                except Exception:
                    title = ""
                results_loaded = (actual_url != before_url) or ("search_query=" in actual_url) or ("q=" in actual_url)
                if results_loaded:
                    return outcome.done(
                        "browser_control", action="search_submit", verified=True,
                        submission_completed=True, page_state="search_results_loaded",
                        navigation_confirmed=True, actual_url=actual_url, title=title)
                return outcome.unverified(
                    "browser_control", "Enter was sent to the search field, but no URL or results change was confirmed",
                    submission_completed=False, actual_url=actual_url)
            return f"Pressed: {key}; current page: {page.url}"
        except Exception as e:
            return f"Key error: {e}"

    async def get_text(self) -> str:
        page = await self._get_page()
        try:
            text = await page.inner_text("body")
            return text[:4_000]
        except Exception as e:
            return f"Could not get page text: {e}"

    async def get_url(self, site_hint: str = "") -> str:
        page = await self._get_page_for_site(site_hint)
        return page.url

    async def fill_form(self, fields: dict) -> str:
        page    = await self._get_page()
        if not await self._page_visible_for_action(page):
            return outcome.unverified(
                "browser_control", "browser page is hidden; no form fields were changed")
        results = []
        for selector, value in fields.items():
            try:
                el = page.locator(selector)
                count = await el.count()
                if count != 1:
                    results.append(f"unverified: {selector!r} matched {count}; skipped")
                    continue
                if not await el.is_editable():
                    results.append(f"failed: {selector!r} is not editable")
                    continue
                if (await el.get_attribute("type") or "").casefold() == "password":
                    results.append(f"protected: password field {selector!r} skipped")
                    continue
                await el.fill(str(value))
                verified = await el.input_value() == str(value)
                results.append(("verified" if verified else "unverified") + f": {selector!r}")
            except Exception as e:
                results.append(f"✗ {selector}: {e}")
        return "Form filled: " + ", ".join(results)

    async def smart_click(self, description: str) -> str:
        page = await self._get_page()
        if not await self._page_visible_for_action(page):
            return outcome.unverified(
                "browser_control", "browser page is hidden; no click was attempted")
        before = await self._page_state(page)
        desc_str = str(description or "").strip()
        desc_cf = desc_str.casefold()
        try:
            host = (urlsplit(page.url).hostname or "").casefold().rstrip(".")
        except Exception:
            host = ""
        is_youtube = (host == "youtube.com" or host.endswith(".youtube.com"))

        if desc_cf in {"pause", "pause video", "pause playback", "pause the video"}:
            return await self.pause_video()
        if desc_cf in {"resume", "resume video", "resume playback", "resume the video"}:
            return await self.resume_video()
        if desc_cf in {"play this video", "play the video", "play video"}:
            return await self.resume_video()
        if is_youtube and (desc_cf.startswith("replay") or "play this video again" in desc_cf or "play again" in desc_cf):
            return await self.replay_video(desc_str)
        if is_youtube and desc_cf in {"next video", "play next video", "play the next video"}:
            return await self._click_youtube_next_video(page)

        if is_youtube:
            chip_res = await self._try_click_youtube_category_chip(page, desc_str)
            if chip_res is not None:
                return chip_res

        ordinal_idx = _parse_ordinal_index(desc_str)
        if ordinal_idx is not None:
            if is_youtube:
                return await self._click_youtube_video_ordinal(page, ordinal_idx, desc_str)
            return await self._click_ordinal_result(
                page, _INDEX_TO_ORDINAL.get(ordinal_idx, f"#{ordinal_idx + 1}"), before)

        is_whatsapp = ("web.whatsapp.com" in str(page.url or "").casefold()) or ("whatsapp" in host)
        if is_whatsapp and not _is_search_bar_query(desc_str):
            from plugins import _whatsapp_core as _wa
            wa_intent = _wa.parse_whatsapp_intent_target(desc_str)
            if wa_intent.get("is_intent") or any(k in desc_cf for k in ("chat", "contact")):
                c_query = str(wa_intent.get("contact") or "").strip()
                if not c_query:
                    c_query = re.sub(
                        r"\b(?:open|select|click|chat|conversation|contact|named|called|with|for|the|one|now|shed)\b",
                        " ", desc_str, flags=re.IGNORECASE,
                    )
                    c_query = " ".join(c_query.split()).strip(" .,!?'\"-")
                if c_query:
                    return await self._open_whatsapp_contact_bounded(page, c_query)
                return outcome.ask(
                    "browser_control",
                    "Which WhatsApp contact or chat would you like me to open, Tarun-kun?",
                    browser=self.browser_name, verified=False,
                )

        if is_youtube:
            title_match = re.search(r"\b(?:video\s+)?titled\s+[\"']?(.+?)[\"']?$", desc_str, re.IGNORECASE)
            if title_match:
                return await self._click_youtube_video_by_title(page, title_match.group(1).strip())
        for role in ("button", "link", "searchbox", "textbox", "menuitem", "tab"):
            try:
                loc = page.get_by_role(role, name=description)
                count = await loc.count()
                if count > 1:
                    return f"Multiple {role} controls match {description!r}; specify a more specific label."
                if count == 1:
                    return await self._click_unique(page, loc, before, f"{role} {description}")
            except Exception:
                pass
        for loc in (page.get_by_text(description, exact=False),
                    page.get_by_placeholder(description, exact=False),
                    page.get_by_role("img", name=description)):
            try:
                count = await loc.count()
                if count > 1:
                    return f"Multiple page elements match {description!r}; specify a more specific label."
                if count == 1:
                    return await self._click_unique(page, loc, before, description)
            except Exception:
                continue
        return f"Could not find element: '{description}'"

    async def _click_ordinal_result(self, page, ordinal: str, before: tuple) -> str:
        """Select a unique visible result link in a semantic results container."""
        try:
            rows = await page.evaluate("""() => {
              const selectorGroups = [
                '#search .MjjYud a:has(h3)[href]',
                '#b_results li.b_algo h2 a[href]',
                '[data-testid="result"] a[href]',
                '#search article h2 a[href], #search article h3 a[href]',
                'main#results a[href]', '[role=main] article a[href]',
                'main article a[href]'
              ];
              let anchors = [];
              for (const selector of selectorGroups) {
                anchors = [...document.querySelectorAll(selector)];
                if (anchors.length) break;
              }
              if (!anchors.length)
                anchors = [...document.querySelectorAll('main a[href], [role=main] a[href]')];
              const seen = new Set(), all = [...document.querySelectorAll('a[href]')];
              return anchors.flatMap(a => {
                const r = a.getBoundingClientRect(), s = getComputedStyle(a);
                const label = (a.innerText || a.getAttribute('aria-label') || a.title || '').trim();
               const href = a.getAttribute('href');
                const parentText = (a.closest('[data-text-ad], .uEierd, [aria-label*=Sponsored]')?.innerText || '').toLowerCase();
                if (!label || !href || !r.width || !r.height || s.display === 'none' ||
                    s.visibility === 'hidden' || a.closest('nav,header,footer,aside') ||
                    /sponsored|advertisement/.test(parentText) || seen.has(href)) return [];
                seen.add(href);
                return [{label: label.slice(0,180), href,
                  index: all.indexOf(a)}];
              });
            }""")
            index = {"first": 0, "second": 1, "third": 2}.get(ordinal)
            if ordinal == "last":
                index = len(rows) - 1
            if index is None or index < 0 or index >= len(rows):
                return f"I found {len(rows)} visible non-ad results in the main results area; {ordinal} is unavailable, so nothing was clicked."
            selected = rows[index]
            all_links = page.locator("a[href]")
            loc = all_links.nth(int(selected["index"]))
            current_href = await loc.get_attribute("href")

            if current_href != selected["href"]:
                return "The result list changed during selection; nothing was clicked."
            if not await loc.is_visible() or not await loc.is_enabled():
                return "The selected result is no longer visible and enabled; nothing was clicked."
            return await self._click_unique(
                page, loc, before, f"{ordinal} result {selected['label']}"
            )
        except Exception as exc:
            return outcome.failed("browser_control",
                                  f"result selection failed safely: {type(exc).__name__}")

    async def _extract_youtube_videos(self, page, *, include_shorts: bool = False) -> list[dict]:
        """Extract ordered eligible non-ad, non-playlist, non-channel YouTube video results."""
        inc_literal = "true" if include_shorts else "false"
        js = f"""els => {{
          const includeShorts = {inc_literal};
          const seen = new Set();
          const out = [];
          for (const card of els) {{
            const r = card.getBoundingClientRect(), s = getComputedStyle(card);
            if (r.width < 2 || r.height < 2 || s.display === 'none' || s.visibility === 'hidden') continue;
            if (card.hasAttribute('is-ad')
                || card.closest('yt-chip-cloud-chip-renderer, #chips-wrapper, iron-selector#chips, [role="tablist"]')
                || card.closest('ytd-ad-slot-renderer, ytd-promoted-sparkles-web-renderer')
                || card.querySelector('ytd-ad-slot-renderer, ytd-promoted-sparkles-web-renderer, #ad-badge, [id*=ad-badge], ytd-badge-supported-renderer[aria-label*="Sponsored" i]')) {{
              continue;
            }}
            if (card.querySelector('ytd-playlist-renderer, ytd-channel-renderer')) continue;
            if (!includeShorts && card.closest('ytd-reel-shelf-renderer')) continue;
            // Prefer the actual result-title anchor over thumbnail duration overlays.
            const a = card.querySelector('a#video-title[href*="/watch"], a#video-title-link[href*="/watch"], a.yt-lockup-metadata-view-model__title[href*="/watch"]')
                || card.querySelector('a[href*="/watch?v="]:not([aria-hidden="true"])');
            if (!a) continue;
            const href = String(a.href || a.getAttribute('href') || '');
            if (href.includes('/playlist') && !href.includes('/watch')) continue;
            if (!includeShorts && href.includes('/shorts/')) continue;
            const ar = a.getBoundingClientRect();
            if (ar.width < 2 || ar.height < 2) continue;
            const u = new URL(href, location.href);
            const id = u.pathname === '/watch' ? u.searchParams.get('v') : null;
            const title = (a.getAttribute('title')
                || a.querySelector('yt-formatted-string, span')?.innerText
                || a.innerText || a.textContent || '').trim();
            const chEl = card.querySelector('ytd-channel-name a, #channel-name a, ytd-channel-name #text, .yt-content-metadata-view-model__metadata-row a');
            const channel = (chEl?.innerText || chEl?.textContent || '').trim();
            const thumbEl = card.querySelector('ytd-thumbnail, yt-thumbnail-view-model, a#thumbnail, img.yt-core-image');
            const tr = thumbEl ? thumbEl.getBoundingClientRect() : null;
            if (!id || !title || seen.has(id)) continue;
            seen.add(id);
            out.push({{
              position: out.length + 1,
              id,
              url: `https://www.youtube.com/watch?v=${{id}}`,
              title: title.slice(0, 160),
              exact_title: title.slice(0, 160),
              channel: channel.slice(0, 80),
              rect: [Math.round(ar.left), Math.round(ar.top), Math.round(ar.width), Math.round(ar.height)],
              card_bbox: [Math.round(r.left), Math.round(r.top), Math.round(r.width), Math.round(r.height)],
              title_bbox: [Math.round(ar.left), Math.round(ar.top), Math.round(ar.width), Math.round(ar.height)],
              thumbnail_bbox: tr ? [Math.round(tr.left), Math.round(tr.top), Math.round(tr.width), Math.round(tr.height)] : null,
              section: (card.closest('ytd-rich-section-renderer, ytd-shelf-renderer')?.querySelector('#title, h2')?.innerText || '').trim().slice(0, 60),
              is_ad: false,
              is_playable: true,
              extraction_source: 'DOM',
              association_confidence: 0.98,
              card_ordering: 'row_major',
              evidence_status: 'OBSERVED',
            }});
          }}
          return out;
        }}"""
        selector = "ytd-video-renderer, ytd-rich-item-renderer, ytd-compact-video-renderer, yt-lockup-view-model, ytd-grid-video-renderer"
        cards = await page.locator(selector).evaluate_all(js)
        if not cards and hasattr(page, "wait_for_selector"):
            try:
                await page.wait_for_selector(
                    "ytd-video-renderer a#video-title, ytd-rich-item-renderer a#video-title, "
                    "yt-lockup-view-model a[href*='/watch']",
                    timeout=3_500,
                )
                cards = await page.locator(selector).evaluate_all(js)
            except Exception:
                pass
        ordered, _rows, _ambig, _secs = _cluster_youtube_video_rows(cards or [])
        try:
            from core import screen_observation as _so
            obs_ts = round(time.time(), 3)
            page_url = str(getattr(page, "url", "") or "")
            try:
                tab_title = str(await page.title() or "")
            except Exception:
                tab_title = ""
            obs_rec = _so.record_observation_provenance(
                captured_at=obs_ts,
                active_app=self.browser_name,
                active_url=page_url,
                tab_identity=tab_title,
                source="DOM",
                dom_evidence=[
                    {"id": c.get("id", ""), "title": c.get("exact_title", ""), "bbox": c.get("card_bbox")}
                    for c in ordered[:20]
                ],
                confidence=("high" if ordered else "medium"),
                stale=False,
            )
            obs_id = obs_rec["observation_id"]
            for c in ordered:
                c["observation_id"] = obs_id
                c["screenshot_timestamp"] = obs_ts
                c["active_url"] = page_url
                c["tab_identity"] = tab_title
            _so.update_observation_targets(obs_id, ordered[:20], extraction_source="DOM")
        except Exception:
            pass
        return ordered

    async def _open_whatsapp_contact_bounded(self, page, contact_query: str) -> str:
        """Bounded WhatsApp contact workflow via #side search without composer/send/call."""
        from plugins import _whatsapp_core as _wa
        q = str(contact_query or "").strip()
        if not q:
            return outcome.ask(
                "browser_control",
                "Which WhatsApp contact should I open, Tarun-kun?",
                browser=self.browser_name, verified=False,
            )
        try:
            sb = await _wa.dom_locate_whatsapp_search_bar(page)
            if not sb.get("found") or not sb.get("enabled"):
                return outcome.failed(
                    "browser_control",
                    f"Could not find an editable WhatsApp sidebar search field to look up {q!r}.",
                    action="smart_click", browser=self.browser_name,
                    failure_reason="whatsapp_search_field_not_found", verified=False,
                )
            loc = page.locator(
                '#side div[contenteditable="true"][role="textbox"][data-tab="3"], '
                '#side [data-testid="chat-list-search"], '
                '#side [aria-label*="Search input" i]'
            ).first
            if await loc.count() < 1:
                return outcome.failed(
                    "browser_control",
                    "WhatsApp sidebar search field is not present in the current tab.",
                    action="smart_click", browser=self.browser_name,
                    failure_reason="whatsapp_search_field_not_found", verified=False,
                )
            await loc.click(timeout=5_000)
            await page.keyboard.press("Control+A")
            await page.keyboard.press("Backspace")
            await page.keyboard.type(q)
            await asyncio.sleep(0.45)
            rows = page.locator('#pane-side [role="listitem"], #pane-side [role="row"], #side [data-testid="cell-frame-container"]')
            rcount = await rows.count()
            matched_idx = None
            matched_name = ""
            exact_idx = None
            matches: list[tuple[int, str]] = []
            for i in range(min(rcount, 12)):
                try:
                    raw_txt = str(await rows.nth(i).inner_text(timeout=1_000) or "")
                except Exception:
                    raw_txt = ""
                disp = _wa._strip_composite_contact_name(raw_txt)
                if not disp:
                    continue
                mkind = _wa.contact_match(q, disp)
                if mkind != "none":
                    matches.append((i, disp))
                    if disp.casefold() == q.casefold() and exact_idx is None:
                        exact_idx = i
                        matched_name = disp
            if exact_idx is not None:
                matched_idx = exact_idx
            elif len(matches) == 1:
                matched_idx, matched_name = matches[0]
            elif len(matches) > 1:
                names = ", ".join(m[1] for m in matches[:4])
                return outcome.ask(
                    "browser_control",
                    f"I found {len(matches)} WhatsApp chats matching {q!r}: {names}. Which one should I open?",
                    browser=self.browser_name, unique_contact_count=len(matches), verified=False,
                )
            if matched_idx is None:
                return outcome.unverified(
                    "browser_control",
                    f"I searched WhatsApp for {q!r}, Tarun-kun, but couldn't find a matching chat.",
                    action="smart_click", browser=self.browser_name,
                    selected_chat_verified=False, target_identified=False, verified=False,
                )
            await rows.nth(matched_idx).click(timeout=5_000)
            await asyncio.sleep(0.35)
            hdr_loc = page.locator('#main header span[title], #main header [data-testid="conversation-info-header-chat-title"]')
            hdr_txt = ""
            if await hdr_loc.count() >= 1:
                hdr_txt = str(await hdr_loc.first.inner_text(timeout=1_500) or "").strip()
            chat_ok = bool(hdr_txt and _wa.contact_match(matched_name or q, hdr_txt) != "none")
            if not chat_ok:
                return outcome.unverified(
                    "browser_control",
                    f"I clicked {matched_name or q}, but I couldn't confirm that the chat header opened.",
                    action="smart_click", browser=self.browser_name,
                    resolved_target=matched_name or q, selected_chat_verified=False, verified=False,
                )
            return outcome.done(
                "browser_control",
                action="smart_click", browser=self.browser_name,
                resolved_target=hdr_txt or matched_name,
                active_contact=hdr_txt or matched_name,
                selected_chat_verified=True, verified=True,
            )
        except Exception as exc:
            return outcome.failed(
                "browser_control",
                f"WhatsApp contact lookup failed safely: {type(exc).__name__}: {exc}",
                action="smart_click", browser=self.browser_name, verified=False,
            )

    async def _is_empty_youtube_home(self, page) -> bool:
        try:
            body = await page.locator("body").inner_text(timeout=1_500)
            low = str(body or "").casefold()
            if "try searching to get started" in low:
                return True
        except Exception:
            pass
        return False

    async def _verify_youtube_playback(self, page, video_id: str, title: str,
                                       channel: str = "", position: int | None = None) -> str:
        """Open check + REAL player-state verification on /watch.

        ``done`` is returned only when the scoped YouTube player reports the
        expected video actually playing (not paused, not loading, not an advert).
        """
        from core import media_state as _ms
        try:
            await page.wait_for_url(
                re.compile(r"https?://(?:www\.)?youtube\.com/watch\?.*\bv=" + re.escape(video_id) + r"(?:&|$)"),
                timeout=6_000,
            )
        except Exception:
            pass
        current = urlsplit(page.url)
        opened_id = next((value for key, value in parse_qsl(current.query) if key == "v"), "")
        if current.path != "/watch" or opened_id != video_id:
            return outcome.failed(
                "browser_control",
                f"clicked video {title!r}, but resulting URL ({page.url}) did not match video {video_id}",
                browser=self.browser_name, video_id=video_id, url=page.url, playback_verified=False,
            )
        judge = lambda s: _ms.judge_playing(s, expected_id=video_id, expected_title=title)
        verdict, st = await _ms.wait_until(page, judge, timeout=6.0)
        if verdict.status == "paused":
            # Autoplay was blocked: ask the player once, then look again.
            await _ms.act(page, "play")
            verdict, st = await _ms.wait_until(page, judge, timeout=3.0)
        ord_label = _INDEX_TO_ORDINAL.get((position or 1) - 1, f"#{position or 1}")
        common = dict(action="play_video", browser=getattr(self, "browser_name", "chrome"),
                      position=position, channel=channel, url=page.url, **_ms.facts(st))
        common["video_id"] = video_id
        common["title"] = title or common.get("title", "")
        if verdict.ok:
            return outcome.done(
                "browser_control", playback_verified=True, verified=True,
                detail=f"Opened and verified the {ord_label} YouTube video: {title!r}, and it is playing.",
                **common)
        if verdict.status in {"consent", "unavailable", "wrong_video", "no_player"}:
            return outcome.failed("browser_control", verdict.reason, playback_verified=False, **common)
        return outcome.unverified(
            "browser_control",
            f"opened video {title!r}, but playback is not confirmed: {verdict.reason}",
            playback_verified=False, blocked_by=verdict.status, **common)

    async def _try_click_youtube_category_chip(self, page, description: str) -> str | None:
        """Resolve and verify a click on a YouTube category chip (`CATEGORY_CHIP`) such as 'All'."""
        raw = str(description or "").strip()
        cleaned = re.sub(
            r"\b(?:click|on|the|category|chip|filter|pill|tab|button|select)\b",
            "", raw, flags=re.IGNORECASE,
        ).strip()
        if not cleaned or _parse_ordinal_index(raw) is not None or "video" in raw.casefold():
            return None
        try:
            chips = await page.evaluate(
                """() => {
                    const out = [];
                    const els = document.querySelectorAll('yt-chip-cloud-chip-renderer, #chips-wrapper [role="tab"], iron-selector#chips [role="tab"]');
                    for (let i = 0; i < els.length; i++) {
                        const el = els[i];
                        const r = el.getBoundingClientRect(), s = getComputedStyle(el);
                        if (r.width < 12 || r.height < 12 || s.display === 'none' || s.visibility === 'hidden') continue;
                        const label = (el.innerText || el.getAttribute('aria-label') || '').trim();
                        if (!label) continue;
                        const selected = (el.getAttribute('aria-selected') === 'true') || el.hasAttribute('selected') || el.classList.contains('iron-selected');
                        out.push({index: i, label, selected, rect: [Math.round(r.left), Math.round(r.top), Math.round(r.width), Math.round(r.height)]});
                    }
                    return out;
                }"""
            )
        except Exception:
            return None
        if not chips:
            return None
        want_cf = cleaned.casefold()
        match = next((c for c in chips if c["label"].strip().casefold() == want_cf), None)
        if match is None:
            return None
        try:
            chip_loc = page.locator('yt-chip-cloud-chip-renderer, #chips-wrapper [role="tab"], iron-selector#chips [role="tab"]').nth(int(match["index"]))
            await chip_loc.click(timeout=5_000)
            await asyncio.sleep(0.35)
            after_chips = await page.evaluate(
                """() => [...document.querySelectorAll('yt-chip-cloud-chip-renderer, #chips-wrapper [role="tab"], iron-selector#chips [role="tab"]')].map((el, i) => ({
                    index: i,
                    label: (el.innerText || el.getAttribute('aria-label') || '').trim(),
                    selected: (el.getAttribute('aria-selected') === 'true') || el.hasAttribute('selected') || el.classList.contains('iron-selected'),
                }))"""
            )
            now_chip = next((c for c in (after_chips or []) if c["label"].strip().casefold() == want_cf), None)
            sel_ok = bool(now_chip and now_chip.get("selected"))
            _log_browser_action_diag(
                requested_site="youtube", verified_site="youtube", active_tab=str(getattr(page, "url", "") or ""),
                action="smart_click", target_class=ELEM_CATEGORY_CHIP, selected_target=match["label"],
                selection_method="DOM", target_bbox=match["rect"], click_method="dom_click",
                verification_result=(ACTION_VERIFIED if sel_ok else ACTION_UNVERIFIED),
                failure_reason=("" if sel_ok else "category_chip_selected_state_not_confirmed"),
            )
            if not sel_ok:
                return outcome.unverified(
                    "browser_control",
                    f"Clicked the '{match['label']}' category chip, but YouTube did not confirm it became active.",
                    action="smart_click", browser=self.browser_name, target_class=ELEM_CATEGORY_CHIP,
                    resolved_target=match["label"], action_state=ACTION_UNVERIFIED, verified=False,
                )
            return outcome.done(
                "browser_control", action="smart_click", browser=self.browser_name,
                target_class=ELEM_CATEGORY_CHIP, resolved_target=match["label"],
                chip_selected=True, action_state=ACTION_VERIFIED, verified=True,
            )
        except Exception as exc:
            return outcome.failed("browser_control", f"Category chip click failed: {type(exc).__name__}: {exc}", action="smart_click", action_state=ACTION_FAILED)

    async def _click_youtube_video_ordinal(self, page, ordinal: int | str, before: tuple | str = ()) -> str:
        """Select a real visible non-ad YouTube video by 0-based ordinal index or word (e.g. 'second')."""
        try:
            raw_label = before if isinstance(before, str) else ""
            cards = await self._extract_youtube_videos(page, include_shorts=("short" in raw_label.casefold()))
            idx = (_ORDINAL_WORDS.get(str(ordinal).lower()) if isinstance(ordinal, str) else int(ordinal))
            ordered, rows, ambiguous, sections = _cluster_youtube_video_rows(cards)
            cards = ordered
            if "horizontal" in raw_label.casefold() and rows:
                horiz_rows = [r for r in rows if len(r) >= 2]
                if not horiz_rows:
                    return outcome.unverified(
                        "browser_control",
                        "I do not see a multi-column horizontal video row on YouTube right now.",
                        action="play_video", browser=self.browser_name,
                        action_state=ACTION_UNVERIFIED, selection_method="DOM",
                        video_count=len(cards), verified=False,
                    )
                primary_row = horiz_rows[0]
                if idx is not None and idx != -1 and idx >= len(primary_row):
                    return outcome.unverified(
                        "browser_control",
                        f"The first horizontal row only shows {len(primary_row)} videos, so position {idx + 1} is unavailable.",
                        action="play_video", browser=self.browser_name,
                        action_state=ACTION_UNVERIFIED, selection_method="DOM",
                        video_count=len(primary_row), verified=False,
                    )
                cards = primary_row
                ambiguous = False
            elif ambiguous and sections:
                return outcome.ask(
                    "browser_control",
                    f"I see multiple video sections on YouTube ({', '.join(sections[:3])}). Which section or video title should I click?",
                    browser=self.browser_name, action_state=ACTION_UNVERIFIED, verified=False,
                )
            if idx == -1:
                idx = len(cards) - 1
            if idx is None or idx < 0 or idx >= len(cards):
                if len(cards) == 0 and await self._is_empty_youtube_home(page):
                    return (
                        "I see the YouTube home page ('Try searching to get started') with 0 visible "
                        "video results; tell me what you want to search for or name a specific video title."
                    )
                return f"I found {len(cards)} eligible non-ad YouTube video results; requested position is unavailable, so nothing was clicked."
            selected = cards[idx]
            video_id = str(selected.get("id", ""))
            title = str(selected.get("exact_title") or selected.get("title") or "").strip()
            ev_status = str(selected.get("evidence_status") or "OBSERVED").upper()
            ord_label = _INDEX_TO_ORDINAL.get(idx, f"#{idx + 1}")
            if ev_status in ("UNKNOWN", "INFERRED") or not title:
                return outcome.unverified(
                    "browser_control",
                    f"I can see the video cards, but I cannot reliably read the {ord_label} video's title.",
                    action="play_video", browser=self.browser_name,
                    evidence_status=ev_status or "UNKNOWN",
                    target_identified=False, playback_verified=False, verified=False,
                )
            channel = str(selected.get("channel", ""))
            if not re.fullmatch(r"[A-Za-z0-9_-]{6,20}", video_id):
                return outcome.unverified(
                    "browser_control",
                    f"I can see the video cards, but I cannot reliably verify the {ord_label} video's identity.",
                    action="play_video", browser=self.browser_name,
                    evidence_status="UNKNOWN", target_identified=False, playback_verified=False, verified=False,
                )
            loc = page.locator(f'a#video-title[href*="watch?v={video_id}"]')
            count = await loc.count()
            if count == 0:
                loc = page.locator(f'a[href*="watch?v={video_id}"]')
                count = await loc.count()
            target_loc = loc if count == 1 else (loc.first if count > 1 else None)
            if target_loc is None or not await target_loc.is_visible() or not await target_loc.is_enabled():
                return "The inspected YouTube video target is no longer visible and clickable; nothing was clicked."
            await target_loc.click(timeout=8_000)
            return await self._verify_youtube_playback(page, video_id, title, channel=channel, position=idx + 1)
        except Exception as exc:
            return outcome.failed("browser_control", f"YouTube video targeting failed safely: {type(exc).__name__}: {exc}")

    async def _click_youtube_video_by_title(self, page, wanted_title: str) -> str:
        try:
            cards = await self._extract_youtube_videos(page, include_shorts=False)
            want = " ".join(wanted_title.casefold().split())
            matches = [c for c in cards if want and want in c.get("title", "").casefold()]
            if not matches:
                return outcome.failed("browser_control", f"no eligible YouTube video matching title {wanted_title!r} was found among {len(cards)} results")
            selected = matches[0]
            video_id = str(selected["id"])
            loc = page.locator(f'a#video-title[href*="watch?v={video_id}"]')
            count = await loc.count()
            if count == 0:
                loc = page.locator(f'a[href*="watch?v={video_id}"]')
                count = await loc.count()
            target_loc = loc if count == 1 else loc.first
            await target_loc.click(timeout=8_000)
            return await self._verify_youtube_playback(
                page, video_id, selected["title"],
                channel=selected.get("channel", ""), position=selected.get("position"),
            )
        except Exception as exc:
            return outcome.failed("browser_control", f"YouTube title selection failed: {type(exc).__name__}: {exc}")

    async def _player_precheck(self, action: str):
        """Return (page, state, failure_string_or_None) for a player control."""
        from core import media_state as _ms
        page = await self._youtube_page()
        if page is None:
            return None, _ms.PlayerState(), outcome.failed(
                "browser_control", "no YouTube tab is open, so no player key or command was sent",
                action=action, browser=self.browser_name, blocked_by="no_youtube_tab")
        st = await _ms.read_state(page)
        blocked = _ms.judge_page(st)
        if blocked:
            fn = outcome.failed if blocked.status in {"consent", "no_player", "unavailable"} else outcome.unverified
            return page, st, fn("browser_control", blocked.reason, action=action,
                                browser=self.browser_name, url=page.url, blocked_by=blocked.status)
        return page, st, None

    async def pause_video(self) -> str:
        from core import media_state as _ms
        try:
            page, st, fail = await self._player_precheck("pause_video")
            if fail:
                return fail
            if not st.paused:
                await _ms.act(page, "pause")
            verdict, st = await _ms.wait_until(page, _ms.judge_paused, timeout=2.0)
            if verdict.ok:
                return outcome.done("browser_control", action="pause_video", browser=self.browser_name,
                                    playback_state="paused", verified=True, url=page.url, **_ms.facts(st))
            return outcome.unverified("browser_control", verdict.reason, action="pause_video",
                                      browser=self.browser_name, url=page.url, verified=False)
        except Exception as exc:
            return outcome.failed("browser_control", f"pause failed: {type(exc).__name__}: {exc}")

    async def resume_video(self) -> str:
        from core import media_state as _ms
        try:
            page, st, fail = await self._player_precheck("resume_video")
            if fail:
                return fail
            if st.paused:
                await _ms.act(page, "play")
            verdict, st = await _ms.wait_until(page, _ms.judge_playing, timeout=3.0)
            if verdict.ok:
                return outcome.done("browser_control", action="resume_video", browser=self.browser_name,
                                    playback_state="playing", verified=True, url=page.url, **_ms.facts(st))
            return outcome.unverified("browser_control", verdict.reason, action="resume_video",
                                      browser=self.browser_name, url=page.url, verified=False)
        except Exception as exc:
            return outcome.failed("browser_control", f"resume failed: {type(exc).__name__}: {exc}")

    async def seek_video(self, seconds: float = 10.0) -> str:
        from core import media_state as _ms
        try:
            delta = float(seconds)
            act_name = "seek_forward" if delta >= 0 else "seek_backward"
            page, before, fail = await self._player_precheck(act_name)
            if fail:
                return fail
            t0 = time.monotonic()
            res = await _ms.act(page, "seek_by", delta)
            if not res.get("ok"):
                return outcome.unverified("browser_control", f"the player refused the seek ({res.get('blocked')})",
                                          action=act_name, browser=self.browser_name, url=page.url)
            verdict, after = await _ms.wait_until(
                page, lambda s: _ms.judge_seek(before, s, delta, elapsed=time.monotonic() - t0), timeout=2.5)
            facts = dict(seconds=round(abs(delta), 2), playback_before=round(before.current_time, 1),
                         playback_after=round(after.current_time, 1), url=page.url)
            if verdict.ok:
                return outcome.done("browser_control", action=act_name, browser=self.browser_name,
                                    verification_status="verified", verified=True, **facts)
            return outcome.unverified("browser_control", verdict.reason, action=act_name,
                                      browser=self.browser_name, verified=False, **facts)
        except Exception as exc:
            return outcome.failed("browser_control", f"seek failed: {type(exc).__name__}: {exc}")

    async def set_volume(self, percent: float) -> str:
        """Set the YouTube PLAYER volume (not the Windows master volume)."""
        from core import media_state as _ms
        try:
            target = max(0.0, min(100.0, float(percent)))
            page, st, fail = await self._player_precheck("set_volume")
            if fail:
                return fail
            await _ms.act(page, "set_volume", target)
            verdict, st = await _ms.wait_until(page, lambda s: _ms.judge_volume(s, target), timeout=1.5)
            if verdict.ok:
                return outcome.done("browser_control", action="set_volume", browser=self.browser_name,
                                    verified=True, target_volume=round(target), url=page.url, **_ms.facts(st))
            return outcome.unverified("browser_control", verdict.reason, action="set_volume",
                                      browser=self.browser_name, verified=False, url=page.url)
        except Exception as exc:
            return outcome.failed("browser_control", f"set volume failed: {type(exc).__name__}: {exc}")

    async def volume_step(self, delta: float) -> str:
        from core import media_state as _ms
        page, st, fail = await self._player_precheck("volume_up" if delta >= 0 else "volume_down")
        if fail:
            return fail
        if st.volume is None:
            return outcome.unverified("browser_control", "the player did not report its volume",
                                      action="volume_step", browser=self.browser_name)
        return await self.set_volume(st.volume + delta)

    async def set_muted(self, muted: bool) -> str:
        """Idempotent mute/unmute: reads state, acts only if needed, verifies."""
        from core import media_state as _ms
        name = "mute" if muted else "unmute"
        try:
            page, st, fail = await self._player_precheck(name)
            if fail:
                return fail
            if st.muted is not muted:
                await _ms.act(page, name)
            verdict, st = await _ms.wait_until(page, lambda s: _ms.judge_muted(s, muted), timeout=1.5)
            if verdict.ok:
                return outcome.done("browser_control", action=name, browser=self.browser_name,
                                    verified=True, url=page.url, **_ms.facts(st))
            return outcome.unverified("browser_control", verdict.reason, action=name,
                                      browser=self.browser_name, verified=False)
        except Exception as exc:
            return outcome.failed("browser_control", f"{name} failed: {type(exc).__name__}: {exc}")

    async def set_fullscreen(self, want: bool) -> str:
        """Enter/exit fullscreen with a REAL key event after verifying player focus."""
        from core import media_state as _ms
        name = "fullscreen" if want else "exit_fullscreen"
        try:
            page, st, fail = await self._player_precheck(name)
            if fail and want:
                return fail
            if st.fullscreen == want:
                return outcome.done("browser_control", action=name, browser=self.browser_name,
                                    verified=True, already=True, url=page.url, **_ms.facts(st))
            if want:
                foc = await _ms.act(page, "focus")
                if not foc.get("focused"):
                    return outcome.failed("browser_control",
                                          "the player could not be focused, so no key was sent",
                                          action=name, browser=self.browser_name)
                await page.keyboard.press("f")
            else:
                await page.keyboard.press("Escape")
            verdict, st = await _ms.wait_until(
                page, lambda s: _ms.judge_fullscreen(s, want), timeout=2.0)
            if verdict.ok:
                return outcome.done("browser_control", action=name, browser=self.browser_name,
                                    verified=True, url=page.url, **_ms.facts(st))
            return outcome.unverified("browser_control", verdict.reason, action=name,
                                      browser=self.browser_name, verified=False)
        except Exception as exc:
            return outcome.failed("browser_control", f"{name} failed: {type(exc).__name__}: {exc}")

    async def focus_search_cdp(self) -> str:
        page = await self._get_page()
        try:
            focused = await page.evaluate(
                """() => {
                    const sel = 'input#search, ytd-searchbox input, input[name="search_query"]';
                    const el = document.querySelector(sel);
                    if (!el) return {found: false};
                    el.focus();
                    el.click();
                    return {found: true, active: document.activeElement === el};
                }"""
            )
            if focused and focused.get("found"):
                return outcome.done(
                    "browser_control", action="focus_search", browser=self.browser_name,
                    resolved_target="YouTube search bar", dispatch_status="completed",
                    input_status="completed", verification_status=("verified" if focused.get("active") else "unverified"),
                    verified=bool(focused.get("active")),
                    detail="The YouTube search field is focused. What should I search for?",
                )
        except Exception:
            pass
        return outcome.failed("browser_control", "Could not locate YouTube search field on this page", browser=self.browser_name)

    async def replay_video(self, target: str = "") -> str:
        """Restart current video from 0:00 or re-select a requested ordinal result."""
        ordinal_idx = _parse_ordinal_index(target) if target else None
        if ordinal_idx is not None:
            page = await self._get_page()
            cards = await self._extract_youtube_videos(page, include_shorts=("short" in target.casefold()))
            if not cards and "/watch" in str(page.url or ""):
                try:
                    await page.go_back(timeout=8_000)
                except Exception:
                    pass
            res = await self._click_youtube_video_ordinal(page, ordinal_idx, target)
            if res.startswith("status=done"):
                try:
                    await page.evaluate("async () => { const v = document.querySelector('video'); if (v) { v.currentTime = 0; await v.play(); } }")
                except Exception:
                    pass
                return res + " replayed=true"
            return res

        from core import media_state as _ms
        try:
            page, st, fail = await self._player_precheck("replay_video")
            if fail:
                return fail
            await _ms.act(page, "seek_to", 0)
            await _ms.act(page, "play")
            verdict, st = await _ms.wait_until(
                page, lambda s: _ms.judge_playing(s) if s.current_time < 8 else _ms.Verdict(False, "unchanged", "did not restart"),
                timeout=3.0)
            if verdict.ok:
                return outcome.done("browser_control", action="replay_video", browser=self.browser_name,
                                    replayed=True, verified=True, url=page.url, **_ms.facts(st))
            return outcome.unverified("browser_control", verdict.reason, action="replay_video",
                                      browser=self.browser_name, verified=False, url=page.url)
        except Exception as exc:
            return outcome.failed("browser_control", f"replay failed: {type(exc).__name__}: {exc}")

    async def _click_youtube_next_video(self, page) -> str:
        """Select the next video on watch page or next recommended card."""
        try:
            btn = page.locator("a.ytp-next-button[href*='watch?v=']")
            if await btn.count() == 1 and await btn.is_visible():
                href = str(await btn.get_attribute("href") or "")
                m = re.search(r"[?&]v=([A-Za-z0-9_-]{6,20})", href)
                title = str(await btn.get_attribute("data-tooltip-text") or await btn.get_attribute("title") or "Next video")
                await btn.click(timeout=6_000)
                if m:
                    return await self._verify_youtube_playback(page, m.group(1), title)
            return await self._click_youtube_video_ordinal(page, 0, "next video")
        except Exception as exc:
            return outcome.failed("browser_control", f"next video failed: {type(exc).__name__}: {exc}")

    async def _click_unique(self, page, loc, before: tuple, label: str) -> str:
        count = await loc.count()
        if count != 1:
            return f"Target {label!r} matched {count} elements; no click was made."
        try:
            if not await loc.is_visible() or not await loc.is_enabled():
                return f"Target {label!r} is not visible and enabled."
            details = await loc.evaluate("""el => {
              const form = el.form || el.closest('form');
              return {text: (el.innerText || el.value || '').slice(0,120),
                aria: el.getAttribute('aria-label') || '',
                title: el.getAttribute('title') || '',
                type: (el.getAttribute('type') || '').toLowerCase(),
                role: (el.getAttribute('role') || '').toLowerCase(),
                method: form ? (form.method || 'get').toLowerCase() : '',
                searchForm: !!(form && (form.role === 'search' ||
                  form.querySelector('input[type=search], [role=searchbox]')))};
            }""")
            semantics = " ".join(str(v) for v in details.values()).casefold()
            risky = ("send", "submit", "delete", "remove", "purchase", "buy",
                     "place order", "pay", "transfer", "share", "install",
                     "allow access", "disable security")
            if (any(term in semantics for term in risky)
                    or (details.get("type") == "submit" and not details.get("searchForm")
                        and details.get("method") == "post")):
                return "CONFIRM_REQUIRED: " + str(details.get("aria") or details.get("text") or label)[:80]
            await loc.click(timeout=8_000)
            await asyncio.sleep(0.25)
            after = await self._page_state(page)
            if after != before:
                return f"Clicked {label!r}; verified page state changed."
            if details.get("type") == "search" or details.get("role") == "searchbox":
                focused = await loc.evaluate("el => document.activeElement === el")
                if focused:
                    return outcome.done("browser_control", action="focus_searchbox",
                                        target=label, verified=True)
            return outcome.unverified("browser_control",
                                      f"clicked {label!r}, but no page-state change was observed")
        except Exception as exc:
            return outcome.failed("browser_control", f"click on {label!r} failed: {exc}")

    @staticmethod
    async def _page_state(page) -> tuple:
        try:
            title = await page.title()
            text = await page.locator("body").inner_text(timeout=2_000)
            return (page.url, title, text[:4000])
        except Exception:
            return (page.url, "", "")

    async def _locate_search_input(self, page):
        """Return (method, locator) for a real visible editable search input (YouTube/generic)."""
        try:
            host = (urlsplit(page.url).hostname or "").casefold()
        except Exception:
            host = ""
        selectors = []
        if "youtube.com" in host:
            selectors.extend([
                ("youtube_search_selector", "ytd-searchbox input#search"),
                ("youtube_search_selector", "input[name='search_query']"),
                ("youtube_search_combobox", "input[role='combobox'][aria-label*='Search' i]"),
            ])
        selectors.extend([
            ("searchbox_selector", "input[type='search']"),
            ("role=searchbox", None),
        ])
        for method, sel in selectors:
            try:
                loc = page.locator(sel) if sel else page.get_by_role("searchbox")
                cnt = await loc.count()
                if cnt == 0:
                    continue
                for idx in range(min(cnt, 6)):
                    cand = loc.nth(idx) if cnt > 1 else loc
                    if await cand.is_visible() and await cand.is_editable():
                        return method, cand, cnt
            except Exception:
                continue
        return None, None, 0

    async def locate_element_screen_rect(self, description: str) -> dict | None:
        """Return desktop-screen coordinates for a DOM control when page is visible."""
        page = await self._get_page()
        if not await self._page_visible_for_action(page):
            return None
        desc_cf = str(description or "").strip().casefold()
        loc = None
        if desc_cf in {"search", "search bar", "search box", "search field", "searchbox", "youtube search bar"}:
            _m, loc, _cnt = await self._locate_search_input(page)
        if loc is None:
            for probe in (
                page.get_by_role("searchbox", name=description),
                page.get_by_role("combobox", name=description),
                page.get_by_role("textbox", name=description),
                page.get_by_label(description, exact=False),
                page.get_by_role("button", name=description),
            ):
                try:
                    if await probe.count() == 1 and await probe.is_visible():
                        loc = probe
                        break
                except Exception:
                    continue
        if loc is None:
            return None
        box = await loc.bounding_box()
        if not box or box["width"] <= 0 or box["height"] <= 0:
            return None
        win = await page.evaluate(
            "() => ({sx: window.screenX || 0, sy: window.screenY || 0, "
            "ow: window.outerWidth || 0, oh: window.outerHeight || 0, "
            "iw: window.innerWidth || 0, ih: window.innerHeight || 0})"
        )
        chrome_x = max(0.0, (float(win.get("ow", 0)) - float(win.get("iw", 0))) / 2.0)
        chrome_y = max(0.0, float(win.get("oh", 0)) - float(win.get("ih", 0)) - 8.0)
        cx = int(round(float(win.get("sx", 0)) + chrome_x + box["x"] + box["width"] / 2.0))
        cy = int(round(float(win.get("sy", 0)) + chrome_y + box["y"] + box["height"] / 2.0))
        return {
            "x": cx, "y": cy,
            "left": int(round(float(win.get("sx", 0)) + chrome_x + box["x"])),
            "top": int(round(float(win.get("sy", 0)) + chrome_y + box["y"])),
            "width": int(round(box["width"])),
            "height": int(round(box["height"])),
            "url": page.url,
            "browser": self.browser_name,
        }

    async def clear_field(self, description: str = "") -> str:
        """Focus and clear an editable DOM input / WhatsApp Web lexical composer and verify empty."""
        page = await self._get_page()
        if not await self._page_visible_for_action(page):
            return outcome.unverified("browser_control", "browser page is hidden; nothing was cleared", action="clear_field")
        if "web.whatsapp.com" in str(page.url or "").casefold():
            from plugins import _whatsapp_core as _wa
            chat_v = await _wa.dom_verify_active_chat(page)
            if not chat_v.get("verified"):
                return outcome.failed("browser_control", chat_v.get("reason") or "WhatsApp active chat is not verified; nothing was cleared.", action="clear_field", verified=False)
            try:
                loc = page.locator('#main footer div[contenteditable="true"], div[contenteditable="true"][data-tab="10"], footer [role="textbox"]').first
                await loc.click(timeout=5_000)
                pre = await _wa.dom_locate_whatsapp_composer(page)
                if not pre.get("focus_verified"):
                    # Ctrl+A / Backspace anywhere else would act on the wrong control.
                    return outcome.failed("browser_control", "the WhatsApp message box did not take keyboard focus; no keys were sent",
                                          action="clear_field", verified=False)
                await page.keyboard.press("Control+A")
                await page.keyboard.press("Backspace")
                await page.keyboard.press("Delete")
                after = await _wa.dom_locate_whatsapp_composer(page)
                empty = not bool(str(after.get("composer_text") or "").strip())
                if empty:
                    from core import desktop_control as _dc
                    _dc.set_active_composition_state(composition_requested=True, selected_contact=chat_v.get("active_contact", ""), generated_content="", typing_authorized=True, message_sent=False)
                    _dc.record_diagnostics(composition_status="cleared", typing_status="empty_verified", sending_status="not_sent")
                    return outcome.done("browser_control", action="clear_field", browser=self.browser_name, composer_found=True, composer_empty=True, focus_verified=bool(after.get("focus_verified")), active_chat_id=chat_v.get("chat_id", ""), verified=True, detail="The message box is empty now.")
                return outcome.unverified("browser_control", "Cleared composer, but residual text remained.", action="clear_field", composer_empty=False, verified=False)
            except Exception as exc:
                return outcome.failed("browser_control", f"clearing WhatsApp composer failed: {type(exc).__name__}", action="clear_field", verified=False)
        try:
            _m, el, _cnt = await self._locate_search_input(page)
            if el is None:
                return outcome.failed("browser_control", f"no editable input matching {description or 'field'!r} was found", action="clear_field", verified=False)
            await el.click(timeout=5_000)
            await el.fill("")
            val = await el.input_value()
            empty = (val == "")
            return outcome.done("browser_control", action="clear_field", browser=self.browser_name, composer_empty=empty, verified=empty) if empty else outcome.unverified("browser_control", "field still contains text", action="clear_field", verified=False)
        except Exception as exc:
            return outcome.failed("browser_control", f"clear_field failed: {type(exc).__name__}", action="clear_field", verified=False)

    async def smart_type(self, description: str, text: str) -> str:
        page = await self._get_page()
        if not await self._page_visible_for_action(page):
            return outcome.unverified(
                "browser_control", "browser page is hidden; no text was entered")
        if "web.whatsapp.com" in str(page.url or "").casefold() and _is_search_bar_query(description):
            from plugins import _whatsapp_core as _wa
            try:
                sb = await _wa.dom_locate_whatsapp_search_bar(page)
                if sb.get("found") and sb.get("enabled"):
                    loc = page.locator('#side div[contenteditable="true"][role="textbox"][data-tab="3"], #side [data-testid="chat-list-search"], #side [aria-label*="Search input" i]').first
                    if await loc.count() >= 1:
                        await loc.click(timeout=5_000)
                        await page.keyboard.press("Control+A")
                        await page.keyboard.press("Backspace")
                        await page.keyboard.type(str(text))
                        after_sb = await _wa.dom_locate_whatsapp_search_bar(page)
                        got_q = str(after_sb.get("value") or "").strip()
                        q_ok = bool(str(text).strip() and str(text).strip().casefold() in got_q.casefold())
                        return outcome.done(
                            "browser_control", action="smart_type", field=sb.get("label", "WhatsApp search field"),
                            method=sb.get("method", "whatsapp_dom"), field_detected=True,
                            field_focused=bool(after_sb.get("focus_verified", True)),
                            text_entered=True, entered_text=(got_q or str(text)),
                            query_verified=q_ok, verified=True,
                        )
            except Exception:
                pass
        if "web.whatsapp.com" in str(page.url or "").casefold() and not _is_search_bar_query(description):
            from plugins import _whatsapp_core as _wa
            try:
                loc = page.locator('#main footer div[contenteditable="true"], div[contenteditable="true"][data-tab="10"], footer [role="textbox"]').first
                if await loc.count() == 1 and await loc.is_visible():
                    await loc.click(timeout=5_000)
                    await page.keyboard.press("Control+A")
                    await page.keyboard.press("Backspace")
                    await page.keyboard.type(str(text))
                    after = await _wa.dom_locate_whatsapp_composer(page)
                    read_txt = str(after.get("composer_text") or "").strip()
                    ok_txt = bool(str(text).strip() and str(text).strip()[:20].casefold() in read_txt.casefold())
                    if ok_txt:
                        return outcome.done("browser_control", action="smart_type", field="Type a message", method="dom_contenteditable", field_detected=True, field_focused=bool(after.get("focus_verified")), text_entered=True, entered_text=read_txt, message_sent=False, verified=True)
                    return outcome.unverified("browser_control", "composer text did not match typed draft", action="smart_type", field_detected=True, text_entered=False, message_sent=False)
            except Exception:
                pass
        generic_search = str(description or "").strip().casefold() in {
            "search", "search bar", "search box", "search field", "searchbox", "youtube search bar",
        }
        if generic_search:
            try:
                method, el, cnt = await self._locate_search_input(page)
                if el is None:
                    return (
                        "Multiple or no visible editable search boxes matched cleanly."
                        if cnt > 1 else
                        "No visible, editable search box was found on this page."
                    )
                if (await el.get_attribute("type") or "").casefold() == "password":
                    return "Password fields are protected. Enter credentials yourself."
                await el.click(timeout=5_000)
                field_focused = bool(await el.evaluate("el => document.activeElement === el"))
                await el.fill(str(text))
                entered_value = await el.input_value()
                placeholder_text = str(await el.get_attribute("placeholder") or "")
                text_entered = (
                    entered_value == str(text)
                    and not (placeholder_text and entered_value == placeholder_text and str(text) != placeholder_text)
                )
                if text_entered:
                    return outcome.done(
                        "browser_control",
                        field=description or "search bar",
                        method=method,
                        field_detected=True,
                        field_focused=field_focused,
                        text_entered=True,
                        entered_text=entered_value,
                        submission_completed=False,
                        verified=True,
                    )
                return outcome.unverified(
                    "browser_control",
                    "search box input value did not match the requested text after typing",
                    field_detected=True,
                    field_focused=field_focused,
                    text_entered=False,
                    entered_text=entered_value,
                    submission_completed=False,
                )
            except Exception as exc:
                return outcome.failed(
                    "browser_control",
                    f"the search box changed or became unavailable ({type(exc).__name__}); no result is confirmed")

        candidates = [
            ("label",       page.get_by_label(description, exact=False)),
            ("role_textbox", page.get_by_role("textbox", name=description)),
            ("searchbox",   page.get_by_role("searchbox")),
            ("combobox",    page.get_by_role("combobox", name=description)),
            ("placeholder_attr", page.get_by_placeholder(description, exact=False)),
        ]
        for method, loc in candidates:
            try:
                count = await loc.count()
                if count == 0:
                    continue
                if count > 1:
                    visible_matches = []
                    for idx in range(min(count, 6)):
                        cand = loc.nth(idx)
                        if await cand.is_visible() and await cand.is_editable():
                            visible_matches.append(cand)
                    if len(visible_matches) != 1:
                        return f"Multiple inputs match {description!r}; no text was entered."
                    el = visible_matches[0]
                else:
                    el = loc
                if not await el.is_visible() or not await el.is_editable():
                    continue
                meta = await el.evaluate(
                    "el => ({tag: el.tagName, editable: !!el.isContentEditable, placeholder: el.getAttribute('placeholder') || ''})"
                )
                if meta.get("tag") not in ("INPUT", "TEXTAREA") and not meta.get("editable"):
                    continue
                if (await el.get_attribute("type") or "").casefold() == "password":
                    return "Password fields are protected. Enter credentials yourself."
                await el.click(timeout=5_000)
                field_focused = bool(await el.evaluate("el => document.activeElement === el"))
                await el.fill(str(text))
                value = await el.input_value()
                ph = str(meta.get("placeholder") or "")
                text_entered = value == str(text) and not (ph and value == ph and str(text) != ph)
                if text_entered:
                    return outcome.done(
                        "browser_control",
                        field=description,
                        method="dom_editable_verified",
                        field_detected=True,
                        field_focused=field_focused,
                        text_entered=True,
                        entered_text=value,
                        submission_completed=False,
                        verified=True,
                    )
                return outcome.unverified(
                    "browser_control",
                    f"text in {description!r} did not read back exactly",
                    field_detected=True,
                    field_focused=field_focused,
                    text_entered=False,
                    entered_text=value,
                    submission_completed=False,
                )
            except Exception:
                continue
        return f"Could not find editable input: '{description}'"

    async def new_tab(self, url: str = "") -> str:
        page = await self._get_page()
        ctx  = page.context
        new  = await ctx.new_page()
        self._page = new
        if url:
            return await self.go_to(url)
        return outcome.done("browser_control", action="new_tab", verified=True, url=new.url)

    async def switch_tab(self, target: str = "") -> str:
        page = await self._get_page()
        pages = [p for p in page.context.pages if not p.is_closed()]
        if not pages:
            return outcome.failed("browser_control", "no open tabs found")
        q = str(target or "").strip()
        if q.isdigit():
            idx = int(q) - 1
            if 0 <= idx < len(pages):
                self._page = pages[idx]
                await self._page.bring_to_front()
                return outcome.done("browser_control", action="switch_tab", verified=True,
                                    url=self._page.url, title=await self._page.title())
        q_cf = q.casefold()
        for p in pages:
            title = ""
            try:
                title = await p.title()
            except Exception:
                pass
            if not q_cf or q_cf in str(p.url or "").casefold() or q_cf in title.casefold():
                self._page = p
                await self._page.bring_to_front()
                return outcome.done("browser_control", action="switch_tab", verified=True,
                                    url=p.url, title=title)
        return outcome.failed("browser_control", f"no tab matching {q!r} was found")

    async def inspect_page(self) -> str:
        page = await self._get_page()
        url = page.url
        try:
            title = await page.title()
        except Exception:
            title = ""
        try:
            ready = await page.evaluate("() => document.readyState")
        except Exception:
            ready = "unknown"
        try:
            body = (await page.locator("body").inner_text(timeout=1_500))[:1200]
        except Exception:
            body = ""
        state = _classify_page_state(title, body) if ready in ("interactive", "complete") else "loading"
        extra = {}
        try:
            host = (urlsplit(url).hostname or "").casefold()
            if "youtube.com" in host:
                videos = await self._extract_youtube_videos(page, include_shorts=False)
                if videos:
                    extra["video_count"] = len(videos)
                    extra["top_videos"] = "; ".join(f"{v['position']}. {v['title']}" for v in videos[:5])
        except Exception:
            pass
        return outcome.done(
            "browser_control", verified=True, browser=self.browser_name,
            url=url, title=title, page_state=state, ready_state=ready, **extra,
        )

    async def close_tab(self) -> str:
        page = self._page
        if (page is None or page.is_closed()) and self._context is not None:
            try:
                live = [p for p in self._context.pages if not p.is_closed()]
                page = live[-1] if live else None
            except Exception:
                page = None
        if page and not page.is_closed():
            try:
                dirty = await page.evaluate(
                    "() => [...document.querySelectorAll('textarea, input:not([type=hidden])')]"
                    ".some(el => el.type !== 'search' && el.getAttribute('role') !== 'searchbox' "
                    "&& el.name !== 'search_query' && el.id !== 'search' "
                    "&& (el.value || '').trim().length > 0 && el.value !== el.defaultValue)"
                )
                if dirty:
                    return "CONFIRM_REQUIRED: close tab containing unsaved form input"
            except Exception:
                pass
            ctx   = page.context
            before_count = len([p for p in ctx.pages if not p.is_closed()])
            closed_url = getattr(page, "url", "")
            await page.close()
            pages = [p for p in ctx.pages if not p.is_closed()]
            self._page = pages[-1] if pages else None
            verified = bool(page.is_closed() or len(pages) < before_count)
            return outcome.done(
                "browser_control",
                action="close_tab",
                browser=self.browser_name,
                closed_url=closed_url,
                verified=verified,
                remaining_tabs=len(pages),
                active_url=(self._page.url if self._page else ""),
            )
        return outcome.failed("browser_control", "no active tab to close", browser=self.browser_name)

    async def screenshot(self, path: str = None) -> str:
        page = await self._get_page()
        try:
            save_path = path or str(Path.home() / "Desktop" / "rem_screenshot.png")
            await page.screenshot(path=save_path, full_page=False)
            return f"Screenshot saved: {save_path}"
        except Exception as e:
            return f"Screenshot error: {e}"

    async def back(self) -> str:
        page = await self._get_page()
        try:
            await page.go_back(timeout=10_000)
            return f"Navigated back: {page.url}"
        except Exception as e:
            return f"Back error: {e}"

    async def forward(self) -> str:
        page = await self._get_page()
        try:
            await page.go_forward(timeout=10_000)
            return f"Navigated forward: {page.url}"
        except Exception as e:
            return f"Forward error: {e}"

    async def reload(self) -> str:
        page = await self._get_page()
        site_hint = _site_key_from_url_or_title(page.url or "", "")
        try:
            await page.reload(wait_until="domcontentloaded", timeout=15_000)
            await asyncio.sleep(0.25)
            title = await page.title()
            body = ""
            try:
                body = (await page.locator("body").inner_text(timeout=1_000))[:800]
            except Exception:
                pass
            ready = await page.evaluate(
                "() => document.readyState === 'interactive' || document.readyState === 'complete'"
            )
            ident = verify_site_page_identity(
                site_hint, url=page.url or "", title=title, body=body, dom_ready=bool(ready),
            )
            vis = await self._page_visible_for_action(page)
            if ident.get("page_state") == "error":
                return outcome.failed(
                    "browser_control", f"page error after reload ({title or page.url})",
                    action="reload", browser=self.browser_name, url=page.url or "", title=title,
                    page_state="error",
                )
            if not vis or not ident.get("verified"):
                return outcome.unverified(
                    "browser_control", ident.get("reason") or "reload dispatched, but visible loaded page was not confirmed",
                    action="reload", browser=self.browser_name, url=page.url or "", title=title,
                    page_state=ident.get("page_state") or "unverified", foreground_verified=bool(vis),
                    target_matched=bool(ident.get("target_matched")),
                )
            return outcome.done(
                "browser_control", action="reload", browser=self.browser_name,
                url=page.url or "", title=title or "", page_state=ident.get("page_state") or "loaded",
                foreground_verified=True, target_matched=True, verified=True,
            )
        except Exception as e:
            return outcome.failed("browser_control", f"reload failed: {e}", action="reload", browser=self.browser_name)

    async def search_whatsapp_contact(self, query: str) -> str:
        from plugins import _whatsapp_core as _wa
        q = str(query or "").strip()
        if not q:
            return outcome.ask("browser_control", "Which contact or chat should I search for?", browser=self.browser_name, verified=False)
        page = await self._get_page_for_site("whatsapp")
        if "web.whatsapp.com" not in str(getattr(page, "url", "") or "").casefold():
            return outcome.unverified(
                "browser_control", "The active Playwright tab is not WhatsApp Web.",
                action="search_contact", browser=self.browser_name, action_state=ACTION_UNVERIFIED, verified=False,
            )
        sb = await _wa.dom_locate_whatsapp_search_field(page)
        if not sb.get("found") or not sb.get("visible") or not sb.get("enabled"):
            return outcome.unverified(
                "browser_control", "Could not locate WhatsApp search field via DOM.",
                action="search_contact", browser=self.browser_name,
                action_state=ACTION_UNVERIFIED, selection_method="DOM", verified=False,
            )
        loc = page.locator(
            '#side div[contenteditable="true"][data-tab="3"], '
            '#side [role="textbox"][contenteditable="true"], '
            '#side [data-testid="chat-list-search"] [contenteditable="true"], '
            '#side [data-testid="chat-list-search"] input, '
            '#side input[type="search"], '
            '#side input[type="text"], '
            '#side [data-testid="chat-list-search"]'
        ).first
        await loc.click(timeout=5_000)
        if sb.get("editable", True):
            try:
                await loc.fill(q)
            except Exception:
                await page.keyboard.type(q, delay=20)
        else:
            await page.keyboard.type(q, delay=20)
        await asyncio.sleep(0.35)
        rb_sb = await _wa.dom_locate_whatsapp_search_field(page)
        q_confirmed = bool(q.casefold() in str((rb_sb or {}).get("value") or "").casefold())
        contacts = await _wa.dom_extract_whatsapp_contacts(page, query=q)
        matches = [c for c in contacts if _wa.contact_match(q, c["display_name"]) != "none"]
        _log_browser_action_diag(
            requested_site="whatsapp", verified_site="whatsapp",
            active_tab=str(getattr(page, "url", "") or ""), action="search_contact",
            target_class=ELEM_SEARCH_CONTROL, selected_target=q,
            selection_method="DOM", target_bbox=sb.get("rect"),
            click_method="dom_click",
            verification_result=(ACTION_VERIFIED if (matches or q_confirmed) else ACTION_UNVERIFIED),
        )
        return outcome.done(
            "browser_control", action="search_contact", browser=self.browser_name,
            control_mode="cdp_dom", resolved_target=q,
            search_source="dom", search_method="dom_side_search",
            query_verified=(q_confirmed or bool(matches)), unique_contact_count=len(matches),
            matched_contacts="; ".join(c["display_name"] for c in matches[:5]),
            action_state=(ACTION_VERIFIED if (matches or q_confirmed) else ACTION_UNVERIFIED),
            verification_status=("verified" if (matches or q_confirmed) else "unverified"),
            message_sent=False,
            verified=bool(matches or q_confirmed),
        )

    async def select_whatsapp_contact(self, description: str) -> str:
        from plugins import _whatsapp_core as _wa
        desc = str(description or "").strip()
        page = await self._get_page_for_site("whatsapp")
        if "web.whatsapp.com" not in str(getattr(page, "url", "") or "").casefold():
            return outcome.unverified(
                "browser_control", "The active Playwright tab is not WhatsApp Web.",
                action="select_contact", browser=self.browser_name, action_state=ACTION_UNVERIFIED, verified=False,
            )
        ord_c = _parse_ordinal_index(desc)
        cleaned = "" if ord_c is not None else re.sub(
            r"\b(?:open|select|click|chat|contact|the|one)\b", "", desc, flags=re.IGNORECASE,
        ).strip() or desc
        contacts = await _wa.dom_extract_whatsapp_contacts(page, query=cleaned)
        if cleaned:
            contacts = [c for c in contacts if _wa.contact_match(cleaned, c["display_name"]) != "none"]
        if not contacts:
            return outcome.unverified(
                "browser_control", f"No WhatsApp contact matching {desc!r} was found via DOM.",
                action="select_contact", browser=self.browser_name, action_state=ACTION_UNVERIFIED,
                selection_method="DOM", verified=False,
            )
        chosen = None
        if ord_c is not None:
            pick = (len(contacts) - 1) if ord_c == -1 else ord_c
            if 0 <= pick < len(contacts):
                chosen = contacts[pick]
        if chosen is None:
            exact = [c for c in contacts if cleaned and c["display_name"].casefold() == cleaned.casefold()]
            chosen = exact[0] if len(exact) == 1 else (contacts[0] if len(contacts) == 1 else None)
        if chosen is None:
            names = ", ".join(c["display_name"] for c in contacts[:4])
            return outcome.ask(
                "browser_control",
                f"I see {len(contacts)} unique contacts matching {cleaned or desc!r}: {names}. Which one should I open?",
                browser=self.browser_name, unique_contact_count=len(contacts), verified=False,
            )
        pt = chosen.get("point") or (0, 0)
        if pt and pt != (0, 0):
            await page.mouse.click(pt[0], pt[1])
        else:
            await page.get_by_text(chosen["display_name"], exact=False).first.click(timeout=5_000)
        await asyncio.sleep(0.35)
        hdr = await _wa.dom_verify_active_chat(page, expected_contact=chosen["display_name"])
        _log_browser_action_diag(
            requested_site="whatsapp", verified_site="whatsapp",
            active_tab=str(getattr(page, "url", "") or ""), action="select_contact",
            selected_target=chosen["display_name"], selection_method="DOM", target_bbox=chosen.get("rect"),
            verification_result=(ACTION_VERIFIED if hdr.get("verified") else ACTION_UNVERIFIED),
            failure_reason=str(hdr.get("reason") or ""),
        )
        if not hdr.get("verified"):
            return outcome.unverified(
                "browser_control",
                f"Clicked {chosen['display_name']}, but WhatsApp Web header did not confirm that chat opened ({hdr.get('reason') or 'unconfirmed'}).",
                action="select_contact", browser=self.browser_name, control_mode="cdp_dom",
                resolved_target=chosen["display_name"], selected_chat_verified=False,
                action_state=ACTION_UNVERIFIED, verification_status="unverified", verified=False,
            )
        return outcome.done(
            "browser_control", action="select_contact", browser=self.browser_name, control_mode="cdp_dom",
            resolved_target=chosen["display_name"], active_contact=hdr["active_contact"],
            active_chat_id=hdr["chat_id"], selected_chat_verified=True,
            action_state=ACTION_VERIFIED, verification_status="verified", message_sent=False, verified=True,
        )

    async def close_browser(self) -> str:
        await self._async_close()
        return f"{self.browser_name} closed."

class _SessionRegistry:
    """Manages all active browser sessions and active window/page sync state."""

    def __init__(self):
        self._sessions:        dict[str, _BrowserSession] = {}
        self._active_browser:  str                        = ""
        self._task_browser:    str                        = ""
        self._lock             = threading.Lock()
        self._last_native_url: str                        = ""
        self._sync_state:      dict[str, dict]            = {}
        self._target_registry: dict[str, dict]            = {}

    def invalidate_sync(self, browser_name: str | None = None) -> None:
        with self._lock:
            if not browser_name:
                self._sync_state.clear()
            else:
                name = _ALIASES.get(browser_name.lower().strip(), browser_name.lower().strip())
                self._sync_state.pop(name, None)

    def note_site_target(self, browser_name: str, site: str, info: dict) -> None:
        name = _ALIASES.get(str(browser_name or "chrome").lower().strip(), str(browser_name or "chrome").lower().strip())
        site_key = str(site or "").strip().casefold() or "other"
        entry = {
            "browser": name,
            "site": site_key,
            "hwnd": int((info or {}).get("hwnd", 0) or 0),
            "pid": int((info or {}).get("pid", 0) or 0),
            "title": str((info or {}).get("title") or ""),
            "url": str((info or {}).get("url") or ""),
            "foreground": bool((info or {}).get("foreground")),
            "last_verified_at": time.time(),
        }
        with self._lock:
            reg = self._target_registry.setdefault(
                name, {"foreground": {}, "whatsapp": {}, "youtube": {}, "other_tabs": []}
            )
            if entry["foreground"]:
                reg["foreground"] = dict(entry)
            if site_key in ("whatsapp", "youtube"):
                reg[site_key] = dict(entry)
            else:
                others = [
                    t for t in reg.get("other_tabs", [])
                    if (t.get("title"), t.get("url")) != (entry["title"], entry["url"])
                ]
                others.insert(0, dict(entry))
                reg["other_tabs"] = others[:8]

    def get_site_target(self, browser_name: str, site: str) -> dict:
        name = _ALIASES.get(str(browser_name or "chrome").lower().strip(), str(browser_name or "chrome").lower().strip())
        site_key = str(site or "").strip().casefold()
        with self._lock:
            reg = self._target_registry.get(name) or {}
            if site_key in ("whatsapp", "youtube"):
                return dict(reg.get(site_key) or {})
            return dict(reg.get("foreground") or {})

    def clear_site_targets(self, browser_name: str | None = None) -> None:
        with self._lock:
            if not browser_name:
                self._target_registry.clear()
            else:
                name = _ALIASES.get(browser_name.lower().strip(), browser_name.lower().strip())
                self._target_registry.pop(name, None)

    def get_target_registry(self, browser_name: str | None = None) -> dict:
        name = _ALIASES.get(str(browser_name or self._active_browser or "chrome").lower().strip(), str(browser_name or self._active_browser or "chrome").lower().strip())
        with self._lock:
            reg = self._target_registry.get(name) or {"foreground": {}, "whatsapp": {}, "youtube": {}, "other_tabs": []}
            return {
                "foreground": dict(reg.get("foreground") or {}),
                "whatsapp": dict(reg.get("whatsapp") or {}),
                "youtube": dict(reg.get("youtube") or {}),
                "other_tabs": [dict(t) for t in (reg.get("other_tabs") or [])],
            }

    def update_sync(self, browser_name: str, data: dict) -> dict:
        name = _ALIASES.get(str(browser_name or "chrome").lower().strip(), str(browser_name or "chrome").lower().strip())
        snap = dict(data or {})
        snap.setdefault("browser", name)
        snap["timestamp"] = time.time()
        with self._lock:
            self._sync_state[name] = snap
            return dict(snap)

    def get_sync(self, browser_name: str | None = None) -> dict:
        name = _ALIASES.get(str(browser_name or self._active_browser or "chrome").lower().strip(), str(browser_name or self._active_browser or "chrome").lower().strip())
        with self._lock:
            return dict(self._sync_state.get(name) or {})

    def has(self, browser_name: str | None = None) -> bool:
        """Is there an active automation session for this browser (or any)?"""
        with self._lock:
            if not browser_name:
                return bool(self._sessions)
            name = _ALIASES.get(browser_name.lower().strip(), browser_name.lower().strip())
            return name in self._sessions

    def note_native_url(self, url: str) -> None:
        self._last_native_url = url

    def pop_native_url(self) -> str:
        """Returns the last natively-opened URL once (consumed to avoid repeats)."""
        url, self._last_native_url = self._last_native_url, ""
        return url

    def _get_or_create(self, browser_name: str) -> _BrowserSession:
        with self._lock:
            if browser_name not in self._sessions:
                sess = _BrowserSession(browser_name)
                sess.start()
                self._sessions[browser_name] = sess
                print(f"[Registry] New session: {browser_name}")
            return self._sessions[browser_name]

    def note_active_browser(self, browser_name: str | None) -> str:
        if not browser_name:
            return self._active_browser
        name = _ALIASES.get(browser_name.lower().strip(), browser_name.lower().strip())
        with self._lock:
            self._active_browser = name
            self._task_browser = name
        return name

    def get(self, browser_name: str | None = None) -> _BrowserSession:
        browser_name = select_browser(browser_name)
        sess = self._get_or_create(browser_name)
        self._active_browser = browser_name
        return sess

    def switch(self, browser_name: str) -> str:
        browser_name = _ALIASES.get(browser_name.lower().strip(), browser_name.lower().strip())
        self._get_or_create(browser_name)
        self._active_browser = browser_name
        return f"Active browser → {browser_name}"

    def close_one(self, browser_name: str) -> str:
        with self._lock:
            sess = self._sessions.pop(browser_name, None)
        if sess:
            sess.close()
            if self._active_browser == browser_name:
                self._active_browser = ""
            return f"{browser_name} closed."
        return f"No active session for: {browser_name}"

    def close_all(self) -> str:
        with self._lock:
            names    = list(self._sessions.keys())
            sessions = list(self._sessions.values())
            self._sessions.clear()
            self._active_browser = ""
        for s in sessions:
            try:
                s.close()
            except Exception:
                pass
        return "All browsers closed: " + (", ".join(names) if names else "none")

    def list_sessions(self) -> str:
        with self._lock:
            if not self._sessions:
                return "No active browser sessions."
            lines = []
            for name in self._sessions:
                marker = " ◀ active" if name == self._active_browser else ""
                lines.append(f"  • {name}{marker}")
            return "Open browsers:\n" + "\n".join(lines)


_registry = _SessionRegistry()


def _proc_matches_browser(app_name: str, title: str, browser_name: str) -> bool:
    b = _ALIASES.get(str(browser_name or "").lower().strip(), str(browser_name or "").lower().strip())
    a_cf = str(app_name or "").casefold()
    t_cf = str(title or "").casefold()
    if b == "edge":
        return "msedge" in a_cf or "edge" in a_cf or "microsoft edge" in t_cf
    if b == "chrome":
        return "chrome" in a_cf or "google chrome" in t_cf
    return bool(b and (b in a_cf or b in t_cf))


def _has_running_or_visible_browser(browser_name: str) -> bool:
    b = _ALIASES.get(str(browser_name or "").lower().strip(), str(browser_name or "").lower().strip())
    if not b:
        return False
    try:
        if appidentity.running(b):
            return True
    except Exception:
        pass
    try:
        from core import screenview
        for app, title in screenview.all_windows():
            if _proc_matches_browser(app, title, b):
                return True
    except Exception:
        pass
    return False


def _should_use_native_window(browser_name: str | None) -> bool:
    b = select_browser(browser_name)
    if _registry.has(b):
        return False
    if _discover_running_cdp_endpoint(b):
        return False
    return _has_running_or_visible_browser(b)


def get_browser_target_registry(browser_name: str = "chrome") -> dict:
    """Return tracked foreground, WhatsApp, YouTube, and other browser targets."""
    return _registry.get_target_registry(browser_name)


def _find_browser_window(browser_name: str, *, site_hint: str = "") -> dict:
    """Identify the actual visible foreground/open window for ``browser_name``.

    When ``site_hint`` is specified (e.g. 'whatsapp' or 'youtube'), prefer the
    matching Chrome/browser window/tab rather than blindly returning an unrelated
    foreground window from another site.
    """
    b = _ALIASES.get(str(browser_name or "chrome").lower().strip(), str(browser_name or "chrome").lower().strip())
    sh = str(site_hint or "").strip().casefold()
    try:
        from core import screenview
        fg_app, fg_title, fg_pid, fg_hwnd = screenview._foreground()
        fg_matches_browser = _proc_matches_browser(fg_app, fg_title, b)
        fg_site = _site_key_from_url_or_title("", fg_title) if fg_matches_browser else ""
        if fg_matches_browser and (not sh or fg_site == sh):
            info = {
                "found": True,
                "foreground": True,
                "browser": b,
                "app": fg_app,
                "title": fg_title,
                "pid": int(fg_pid or 0),
                "hwnd": int(fg_hwnd or 0),
                "matched_site": fg_site,
                "site_mismatch": False,
                "ambiguous_matches": 0,
            }
            if fg_site:
                _registry.note_site_target(b, fg_site, info)
            return info

        all_browser_wins: list[tuple[str, str]] = []
        site_matches: list[tuple[str, str]] = []
        for app, title in screenview.all_windows():
            if _proc_matches_browser(app, title, b):
                all_browser_wins.append((app, title))
                w_site = _site_key_from_url_or_title("", title)
                if sh and w_site == sh:
                    site_matches.append((app, title))

        if sh and site_matches:
            app, title = site_matches[0]
            whwnd, wpid, wapp, wtitle = _win32_window_identity_for_title(b, title_query=title, site_hint=sh)
            info = {
                "found": True,
                "foreground": bool(fg_matches_browser and fg_hwnd and whwnd and int(fg_hwnd) == int(whwnd)),
                "browser": b,
                "app": wapp or app,
                "title": wtitle or title,
                "pid": int(wpid or 0),
                "hwnd": int(whwnd or 0),
                "matched_site": sh,
                "site_mismatch": False,
                "ambiguous_matches": len(site_matches) if len(site_matches) > 1 else 0,
            }
            _registry.note_site_target(b, sh, info)
            return info

        if sh:
            cached = _registry.get_site_target(b, sh)
            if cached and cached.get("title"):
                for app, title in all_browser_wins:
                    if _site_key_from_url_or_title("", title) == sh:
                        return {**cached, "found": True, "site_mismatch": False}

        if fg_matches_browser:
            return {
                "found": True,
                "foreground": True,
                "browser": b,
                "app": fg_app,
                "title": fg_title,
                "pid": int(fg_pid or 0),
                "hwnd": int(fg_hwnd or 0),
                "matched_site": fg_site,
                "site_mismatch": bool(sh and fg_site and fg_site != sh),
                "ambiguous_matches": 0,
            }
        if all_browser_wins:
            app, title = all_browser_wins[0]
            whwnd, wpid, wapp, wtitle = _win32_window_identity_for_title(b, title_query=title, site_hint=sh)
            actual_site = _site_key_from_url_or_title("", wtitle or title)
            return {
                "found": True,
                "foreground": False,
                "browser": b,
                "app": wapp or app,
                "title": wtitle or title,
                "pid": int(wpid or 0),
                "hwnd": int(whwnd or 0),
                "matched_site": actual_site,
                "site_mismatch": bool(sh and actual_site != sh),
                "ambiguous_matches": 0,
            }
    except Exception:
        pass
    running = bool(appidentity.running(b)) if b else False
    return {"found": running, "foreground": False, "browser": b, "app": b, "title": "", "pid": 0, "hwnd": 0, "matched_site": "", "site_mismatch": False, "ambiguous_matches": 0}


_DURATION_RE = re.compile(r"^\d{1,2}:\d{2}(?::\d{2})?$")
_YT_META_RE = re.compile(
    r"(?:^\d+(?:\.\d+)?[KMB]?\s+views?\b|\bviews?\s*[·•]|\b\d+\s+(?:second|minute|hour|day|week|month|year)s?\s+ago\b|^@[A-Za-z0-9._-]{2,}$)",
    re.IGNORECASE,
)
_YT_NAV_NOISE = frozenset({
    "home", "shorts", "subscriptions", "you", "history", "playlists",
    "watch later", "liked videos", "trending", "music", "gaming", "news",
    "sports", "podcasts", "sign in", "create", "notifications", "search",
    "search with your voice", "skip navigation", "guide", "settings",
    "try searching to get started", "youtube", "youtube home",
    "all", "filters", "filter", "channels", "videos", "posts", "playlist",
    "live", "recently uploaded", "watched", "new to you", "for you",
    "from your search", "people also watched", "previously watched",
    "related to your search", "share", "save", "download", "subscribe",
    "subscribed", "show more", "show less", "4k", "cc",
})


def classify_youtube_elements(view, *, include_shorts: bool = False) -> dict:
    """Classify visible UIA/OCR elements on YouTube into explicit semantic classes.

    Classes: ``VIDEO_CARD``, ``CATEGORY_CHIP``, ``NAVIGATION_ITEM``, ``SHORTS_CARD``,
    ``AD_CARD``, ``SEARCH_CONTROL``, ``PLAYER_CONTROL``, ``OTHER``.
    Any element sharing the horizontal YouTube category-chip bar (anchored by labels such
    as 'All', 'Music', 'Gaming', 'Mixes', etc.) is classified ``CATEGORY_CHIP`` and NEVER
    enters ``VIDEO_CARD`` ordinals even for dynamic multi-word topics like 'Science fiction'.
    """
    elements = list(getattr(view, "elements", ()) or ()) if view is not None else []
    mon = (getattr(view, "monitor", None) if view is not None else None) or {}
    win_rect = getattr(view, "window_rect", None) if view is not None else None
    if isinstance(win_rect, (list, tuple)) and len(win_rect) == 4 and int(win_rect[2] or 0) > 200:
        w_left, w_top, w_w, _w_h = (int(v or 0) for v in win_rect)
    else:
        w_left = int(mon.get("left", 0) or 0)
        w_top = int(mon.get("top", 0) or 0)
        w_w = int(mon.get("width", 1920) or 1920)

    header_floor = w_top + 130
    ocr_floor = w_top + 185
    chip_top_max = w_top + 315
    sidebar_ceil = w_left + (200 if w_w >= 1100 else 60)
    view_source = str(getattr(view, "source", "") or "").casefold() if view is not None else ""

    joined_low = " ".join(
        str(getattr(el, "name", "") or getattr(el, "value", "") or "").casefold()
        for el in elements
    )
    empty_home = "try searching to get started" in joined_low

    # First detect horizontal Category Chip Bar Y bands using compact anchor pills
    compact_rows: list[tuple[int, str, Any]] = []
    for el in elements:
        name = " ".join(str(getattr(el, "name", "") or getattr(el, "label", "") or "").split())
        if not name:
            continue
        top = int(getattr(el, "top", 0) or 0)
        left = int(getattr(el, "left", 0) or 0)
        width = int(getattr(el, "width", 0) or 0)
        height = int(getattr(el, "height", 0) or 0)
        if w_top + 95 <= top <= chip_top_max and left >= sidebar_ceil and 12 <= height <= 56 and width <= 320:
            compact_rows.append((top, name.casefold(), el))

    chip_band_ys: list[int] = []
    for top, low, _el in compact_rows:
        if low in _YT_CHIP_ANCHORS:
            # Confirm at least 2 compact items share this horizontal line (or UIA tab role)
            role_cf = str(getattr(_el, "role", "") or "").casefold()
            siblings = [t2 for t2, _l2, _e2 in compact_rows if abs(t2 - top) <= 22]
            if len(siblings) >= 2 or role_cf in {"tab", "radiobutton"}:
                chip_band_ys.append(top)

    now_m = time.monotonic()
    classified: dict[str, list[dict]] = {
        ELEM_VIDEO_CARD: [],
        ELEM_CATEGORY_CHIP: [],
        ELEM_NAVIGATION_ITEM: [],
        ELEM_SHORTS_CARD: [],
        ELEM_AD_CARD: [],
        ELEM_SEARCH_CONTROL: [],
        ELEM_PLAYER_CONTROL: [],
        ELEM_OTHER: [],
    }
    seen_videos: set[str] = set()
    seen_chips: set[str] = set()

    for el in elements:
        role = str(getattr(el, "role", "") or "").casefold()
        is_ocr = ("ocr" in role) or (view_source == "ocr" and role in {"text", "ocr_text", ""})
        name = " ".join(str(getattr(el, "name", "") or getattr(el, "label", "") or "").split())
        if not name:
            continue
        low = name.casefold()
        top = int(getattr(el, "top", 0) or 0)
        left = int(getattr(el, "left", 0) or 0)
        width = int(getattr(el, "width", 0) or 0)
        height = int(getattr(el, "height", 0) or 0)
        cx, cy = (getattr(el, "center", None) or (left + max(1, width // 2), top + max(1, height // 2)))
        src_kind = "ocr" if is_ocr else "uia"
        item = {
            "title": name[:160],
            "label": name[:160],
            "point": (int(cx), int(cy)),
            "rect": (left, top, width, height),
            "top": top,
            "left": left,
            "source": src_kind,
            "selected": bool(getattr(el, "selected", False)),
            "confidence": ("high" if src_kind == "uia" and "link" in role else "medium"),
            "observed_at": now_m,
            "element": el,
        }

        # 1. Player controls
        if any(rx.search(name) for rules in _YT_BUTTON_RULES.values() for rx, _st in rules):
            item["element_class"] = ELEM_PLAYER_CONTROL
            classified[ELEM_PLAYER_CONTROL].append(item)
            continue

        # 2. Search bar controls
        if role in {"edit", "combobox", "searchbox"} or "search with your voice" in low:
            item["element_class"] = ELEM_SEARCH_CONTROL
            classified[ELEM_SEARCH_CONTROL].append(item)
            continue

        # 3. Ads
        if any(ad in low for ad in ("sponsored", "ad ·", "advertisement", "shop now", "learn more")):
            item["element_class"] = ELEM_AD_CARD
            classified[ELEM_AD_CARD].append(item)
            continue

        # 4. Category chip bar (`All`, `Music`, `Gaming`, `Science fiction`, etc.)
        in_chip_band = bool(
            chip_band_ys and any(abs(top - cy_band) <= 22 for cy_band in chip_band_ys)
            and left >= sidebar_ceil and height <= 56 and width <= 320
        )
        is_uia_chip = role in {"tab", "radiobutton"} and left >= sidebar_ceil and top <= chip_top_max
        if in_chip_band or is_uia_chip:
            item["element_class"] = ELEM_CATEGORY_CHIP
            if low not in seen_chips:
                seen_chips.add(low)
                classified[ELEM_CATEGORY_CHIP].append(item)
            continue

        # 5. Shorts
        if "#shorts" in low or low == "shorts":
            item["element_class"] = ELEM_SHORTS_CARD
            classified[ELEM_SHORTS_CARD].append(item)
            if not include_shorts:
                continue

        # 6. Navigation / metadata / sidebar noise
        if left < sidebar_ceil or low in _YT_NAV_NOISE or _DURATION_RE.match(name) or _YT_META_RE.search(name):
            item["element_class"] = ELEM_NAVIGATION_ITEM
            classified[ELEM_NAVIGATION_ITEM].append(item)
            continue

        # 7. Remaining floor / role / length gates for VIDEO_CARD
        if (
            len(name) < 4
            or (is_ocr and len(name.split()) < 2 and len(name) < 14)
            or top < (ocr_floor if is_ocr else header_floor)
            or width < 90
            or height < 14
            or (
                role not in {"link", "button", "heading", "text", "ocr_text", "group", ""}
                and "link" not in role and "text" not in role and "heading" not in role
            )
        ):
            item["element_class"] = ELEM_OTHER
            classified[ELEM_OTHER].append(item)
            continue

        if low in seen_videos:
            continue
        seen_videos.add(low)
        src_upper = "OCR" if src_kind == "ocr" else "UIA"
        conf_score = (0.90 if src_kind == "uia" and "link" in role else (0.84 if src_kind == "uia" else 0.72))
        ev_stat = ("OBSERVED" if src_kind == "uia" else "ASSOCIATED")
        item["exact_title"] = name[:160]
        item["extraction_source"] = src_upper
        item["association_confidence"] = conf_score
        item["card_ordering"] = "row_major"
        item["evidence_status"] = ev_stat
        item["title_bbox"] = [left, top, width, height]
        item["card_bbox"] = [left, max(w_top, top - 165), max(width, 220), height + 175]
        item["thumbnail_bbox"] = [left, max(w_top, top - 165), max(width, 220), 155]
        item["conflicting_titles"] = []
        item["element_class"] = ELEM_SHORTS_CARD if ("#shorts" in low or low == "shorts") else ELEM_VIDEO_CARD
        classified[ELEM_VIDEO_CARD].append(item)

    # Guard against conflicting OCR titles falling into the same card spatial slot:
    # NEVER merge conflicting fragments and NEVER guess/pick one over another.
    raw_videos = list(classified[ELEM_VIDEO_CARD])
    if any(v.get("extraction_source") == "OCR" for v in raw_videos):
        merged_slots: list[dict] = []
        for cand in raw_videos:
            if cand.get("extraction_source") != "OCR":
                merged_slots.append(cand)
                continue
            cl, ct, cw, _ch = cand["rect"]
            slot_hit = None
            for existing in merged_slots:
                if existing.get("extraction_source") != "OCR":
                    continue
                el_l, el_t, el_w, _el_h = existing["rect"]
                horiz_overlap = min(cl + cw, el_l + el_w) - max(cl, el_l)
                same_col = (horiz_overlap >= min(cw, el_w) * 0.35) or (abs(cl - el_l) <= 115)
                if same_col and abs(ct - el_t) <= 155:
                    slot_hit = existing
                    break
            if slot_hit is None:
                merged_slots.append(cand)
            else:
                prev_titles = list(slot_hit.get("conflicting_titles") or [])
                base_t = str(slot_hit.get("exact_title") or slot_hit.get("title") or "").strip()
                new_t = str(cand.get("exact_title") or cand.get("title") or "").strip()
                if base_t and base_t not in prev_titles:
                    prev_titles.append(base_t)
                if new_t and new_t not in prev_titles:
                    prev_titles.append(new_t)
                if len(prev_titles) > 1:
                    t_a = {w for w in re.findall(r"[a-z0-9]+", base_t.casefold()) if len(w) >= 2}
                    t_b = {w for w in re.findall(r"[a-z0-9]+", new_t.casefold()) if len(w) >= 2}
                    union = t_a | t_b
                    jaccard = (len(t_a & t_b) / len(union)) if union else 0.0
                    # Adjacent subtitle lines with high overlap (<0.5 conflict) mark card UNKNOWN rather than guessing
                    if jaccard < 0.72:
                        slot_hit["conflicting_titles"] = prev_titles
                        slot_hit["evidence_status"] = "UNKNOWN"
                        slot_hit["exact_title"] = ""
                        slot_hit["title"] = "Unreadable video card"
                        slot_hit["label"] = "Unreadable video card"
                        slot_hit["association_confidence"] = 0.0
                        slot_hit["confidence"] = "uncertain"
        classified[ELEM_VIDEO_CARD] = merged_slots

    ordered_videos, _rows, _ambig, _secs = _cluster_youtube_video_rows(classified[ELEM_VIDEO_CARD])
    classified[ELEM_VIDEO_CARD] = ordered_videos
    classified[ELEM_CATEGORY_CHIP].sort(key=lambda c: (c["top"], c["left"]))
    for pos, ch in enumerate(classified[ELEM_CATEGORY_CHIP], 1):
        ch["position"] = pos
    classified["empty_home"] = empty_home
    return classified


def _extract_native_youtube_candidates(view, *, include_shorts: bool = False) -> tuple[list[dict], bool]:
    """Extract ordered non-ad ``VIDEO_CARD`` candidates from UIA/OCR on the visible Chrome window."""
    cls = classify_youtube_elements(view, include_shorts=include_shorts)
    return list(cls[ELEM_VIDEO_CARD]), bool(cls.get("empty_home"))


def _inspect_visible_browser_window(browser_name: str, *, focus_if_needed: bool = False,
                                    allow_ocr: bool = True, site_hint: str = "",
                                    lightweight: bool = False) -> dict:
    """Synchronize foreground window identity, URL, search query, and visible site state."""
    from core import screenact, screenview

    t0 = time.monotonic()
    sh = str(site_hint or "").strip().casefold()
    win = _find_browser_window(browser_name, site_hint=sh)
    window_resolution_ms = round((time.monotonic() - t0) * 1000.0, 1)
    t_fg0 = time.monotonic()
    if focus_if_needed and win.get("found") and not win.get("foreground"):
        try:
            title_hint = ("WhatsApp" if sh == "whatsapp" else ("YouTube" if sh == "youtube" else str(win.get("title") or "")))
            if sh:
                appidentity.focus_window_by_app(win["browser"], title_hint=title_hint)
            else:
                screenact.focus_app(win["browser"])
            time.sleep(0.15)
            win = _find_browser_window(win["browser"], site_hint=sh)
        except Exception:
            pass
    foreground_activation_ms = round((time.monotonic() - t_fg0) * 1000.0, 1)
    hwnd_ms = round((time.monotonic() - t0) * 1000.0, 1)

    t_obs0 = time.monotonic()
    ocr_before = screenview.ocr_stats() if hasattr(screenview, "ocr_stats") else {"init_ms": 0.0, "exec_count": 0, "total_exec_ms": 0.0}
    view = None
    url = ""
    if win.get("foreground") and _proc_matches_browser(win.get("app", ""), win.get("title", ""), win["browser"]):
        try:
            budget = 0.45 if lightweight else (2.6 if allow_ocr else 0.9)
            cand_view = screenview.look(budget_s=budget, allow_ocr=allow_ocr, allow_pixel_digest=not lightweight)
            c_app = str(getattr(cand_view, "app", "") or win.get("app") or "")
            c_title = str(getattr(cand_view, "title", "") or win.get("title") or "")
            c_pid = int(getattr(cand_view, "pid", 0) or win.get("pid", 0) or 0)
            c_src = str(getattr(cand_view, "source", "") or "")
            is_explorer_app = "explorer" in c_app.casefold()
            same_app_or_wa = _proc_matches_browser(c_app, c_title, win["browser"]) or (
                "whatsapp" in c_app.casefold() and "whatsapp" in str(win.get("title") or "").casefold()
            )
            if (
                c_src == "self"
                or (c_pid and screenview.is_own_pid(c_pid))
                or is_explorer_app
                or not same_app_or_wa
            ):
                view = None
                win = {**win, "foreground": False}
            else:
                view = cand_view
        except Exception:
            view = None
    obs_ms = round((time.monotonic() - t_obs0) * 1000.0, 1)
    ocr_after = screenview.ocr_stats() if hasattr(screenview, "ocr_stats") else {"init_ms": 0.0, "exec_count": 0, "total_exec_ms": 0.0}
    ocr_exec_delta_ms = round(max(0.0, float(ocr_after.get("total_exec_ms", 0.0)) - float(ocr_before.get("total_exec_ms", 0.0))), 1)
    uia_lookup_ms = round(max(0.0, obs_ms - ocr_exec_delta_ms), 1)

    uia_count = 0
    ocr_count = 0
    elements = list(getattr(view, "elements", ()) or ()) if view is not None else []
    for el in elements:
        r = str(getattr(el, "role", "") or "").casefold()
        if "ocr" in r:
            ocr_count += 1
        else:
            uia_count += 1
        val = str(getattr(el, "value", "") or getattr(el, "name", "") or "").strip()
        label_low = str(getattr(el, "name", "") or getattr(el, "label", "") or "").casefold()
        if not url and ("address" in label_low or "search bar" in label_low or "omnibox" in label_low) and "." in val:
            url = val if "://" in val else f"https://{val}"
        elif not url and ("youtube.com/" in val.casefold() or "web.whatsapp.com" in val.casefold() or "http://" in val.casefold() or "https://" in val.casefold()):
            url = val

    title = str(win.get("title") or (getattr(view, "title", "") if view else "") or "")
    search_query = ""
    if "search_query=" in url:
        try:
            search_query = next((v for k, v in parse_qsl(urlsplit(url).query) if k == "search_query"), "")
        except Exception:
            search_query = ""
    elif " - youtube" in title.casefold():
        candidate_q = re.sub(r"\s*-\s*YouTube.*$", "", title, flags=re.IGNORECASE).strip()
        if candidate_q and candidate_q.casefold() not in {"youtube", "home", "(1) youtube"}:
            search_query = candidate_q

    detected_site = _site_key_from_url_or_title(url, title) or sh
    if view is not None and not lightweight and detected_site != "whatsapp":
        _yt_cls = classify_youtube_elements(view)
        videos = list(_yt_cls[ELEM_VIDEO_CARD])
        yt_chips = list(_yt_cls[ELEM_CATEGORY_CHIP])
        empty_home = bool(_yt_cls.get("empty_home"))
    else:
        videos, yt_chips, empty_home = [], [], False
    if empty_home:
        videos = []
    src = getattr(view, "source", "none") if view is not None else "window_list"
    if uia_count and ocr_count:
        obs_source = "windows_uia_and_ocr"
    elif ocr_count:
        obs_source = "windows_ocr"
    elif uia_count:
        obs_source = "windows_uia"
    else:
        obs_source = str(src or "window_list")

    extracted_text_count = uia_count + ocr_count
    if videos or extracted_text_count >= 4:
        confidence = "high"
    elif extracted_text_count > 0:
        confidence = "medium"
    else:
        confidence = "uncertain"
    app_exe = str(win.get("app") or f"{win['browser']}.exe")
    window_identity = f"{app_exe}: {title}" if title else app_exe
    is_wa = detected_site == "whatsapp" or "whatsapp" in (title + " " + url).casefold()
    wa_contacts: list[dict] = []
    wa_chat: dict = {}
    if is_wa and view is not None and not lightweight:
        try:
            from plugins import _whatsapp_core as _wa
            wa_contacts = _wa.extract_whatsapp_contacts(view, query=search_query)
            wa_chat = _wa.verify_selected_chat(view)
        except Exception:
            wa_contacts, wa_chat = [], {}
    total_ms = round((time.monotonic() - t0) * 1000.0, 1)
    try:
        from core import desktop_control as _dc
        diag_updates = dict(
            last_obs_source=obs_source,
            ocr_count=ocr_count,
            uia_count=uia_count,
            chrome_detected=bool(win.get("found") and win["browser"] == "chrome"),
            chrome_foreground=bool(win.get("foreground") and win["browser"] == "chrome"),
            foreground_app=(app_exe if win.get("foreground") else _dc.snapshot().get("foreground_app", "—")),
        )
        if is_wa:
            diag_updates["active_application"] = "WhatsApp (Chrome)"
            if not lightweight:
                diag_updates["unique_contact_count"] = len(wa_contacts)
                diag_updates["selected_chat_verified"] = bool(wa_chat.get("verified"))
                if wa_chat.get("active_contact"):
                    diag_updates["active_contact"] = wa_chat["active_contact"]
                    diag_updates["active_chat_id"] = wa_chat.get("chat_id", "")
        _dc.record_diagnostics(**diag_updates)
    except Exception:
        pass

    snap = {
        "browser": win["browser"],
        "hwnd": win.get("hwnd", 0),
        "pid": win.get("pid", 0),
        "title": title,
        "window_identity": window_identity,
        "url": url,
        "foreground": bool(win.get("foreground")),
        "site": detected_site,
        "site_mismatch": bool(win.get("site_mismatch")),
        "search_query": search_query,
        "empty_home": bool(empty_home),
        "videos": videos,
        "yt_chips": yt_chips,
        "wa_contacts": wa_contacts,
        "wa_chat": wa_chat,
        "uia_items": uia_count,
        "ocr_items": ocr_count,
        "extracted_text_count": extracted_text_count,
        "confidence": confidence,
        "target_identified": bool(videos or wa_contacts or wa_chat.get("verified")),
        "timings": {
            "hwnd_ms": hwnd_ms,
            "obs_ms": obs_ms,
            "window_resolution_ms": window_resolution_ms,
            "foreground_activation_ms": foreground_activation_ms,
            "uia_lookup_ms": uia_lookup_ms,
            "ocr_init_ms": float(ocr_after.get("init_ms", 0.0)),
            "ocr_exec_ms": ocr_exec_delta_ms,
            "total_ms": total_ms,
        },
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "source": obs_source,
        "cdp_available": False,
        "view": view,
    }
    try:
        from core import screen_observation as _so
        obs_epoch = round(time.time(), 3)
        ocr_recs, uia_recs = [], []
        for el in elements[:80]:
            t_el = str(getattr(el, "name", "") or getattr(el, "value", "") or "").strip()
            if not t_el:
                continue
            rec_item = {
                "text": t_el,
                "bbox": [
                    int(getattr(el, "left", 0) or 0),
                    int(getattr(el, "top", 0) or 0),
                    int(getattr(el, "width", 0) or 0),
                    int(getattr(el, "height", 0) or 0),
                ],
                "role": str(getattr(el, "role", "") or ""),
            }
            if "ocr" in str(getattr(el, "role", "") or "").casefold():
                ocr_recs.append(rec_item)
            else:
                uia_recs.append(rec_item)
        obs_rec = _so.record_observation_provenance(
            captured_at=obs_epoch,
            hwnd=int(win.get("hwnd", 0) or 0),
            pid=int(win.get("pid", 0) or 0),
            active_app=app_exe,
            active_url=url,
            tab_identity=title,
            source=obs_source,
            ocr_items=ocr_recs,
            uia_items=uia_recs,
            confidence=confidence,
            stale=not bool(win.get("foreground") and not win.get("site_mismatch")),
        )
        obs_id = obs_rec["observation_id"]
        snap["observation_id"] = obs_id
        snap["captured_at"] = obs_epoch
        for v in videos:
            v["observation_id"] = obs_id
            v["screenshot_timestamp"] = obs_epoch
            v["hwnd"] = int(win.get("hwnd", 0) or 0)
            v["pid"] = int(win.get("pid", 0) or 0)
            v["url"] = url
            v["tab_identity"] = title
        if videos:
            _so.update_observation_targets(obs_id, videos[:20], extraction_source=obs_source)
    except Exception:
        pass
    _registry.note_site_target(win["browser"], detected_site or "other", snap)
    _registry.update_sync(win["browser"], snap)
    return snap


def inspect_title_provenance(query: str = "") -> str:
    """Return recorded evidence provenance for a YouTube video title without fabrication."""
    from core import screen_observation as _so
    return _so.explain_title_provenance(query)


def _inspect_browser_sync_safe(browser_name: str, *, allow_ocr: bool = True) -> dict:
    return _inspect_visible_browser_window(browser_name, focus_if_needed=True, allow_ocr=allow_ocr)


def _ensure_browser_foreground(
    bname: str,
    win: dict,
    title_hint: str = "",
    *,
    site_hint: str = "",
) -> tuple[bool, dict, str]:
    """Activate and verify that the requested browser window (not REM's own window) is foreground."""
    from core import appidentity, desktop_control, screenact, screenview

    if not win.get("found"):
        desktop_control.record_diagnostics(chrome_detected=False, chrome_foreground=False)
        return False, win, f"no running {bname} window was found and CDP is unavailable; did not substitute another browser"

    target_hwnd = int(win.get("hwnd", 0) or 0)
    sh = str(site_hint or win.get("matched_site") or "").strip().casefold()
    if target_hwnd <= 0 and sh:
        saved = _registry.get_site_target(bname, sh)
        target_hwnd = int((saved or {}).get("hwnd", 0) or 0)

    def _matches_intended(cand: dict) -> bool:
        if not cand.get("foreground") or not _proc_matches_browser(cand.get("app", ""), cand.get("title", ""), bname):
            return False
        if screenview.is_own_pid(int(cand.get("pid", 0) or 0)):
            return False
        if sh:
            cand_site = _site_key_from_url_or_title("", str(cand.get("title") or ""))
            if cand_site and cand_site != sh:
                return False
            if target_hwnd and int(cand.get("hwnd", 0) or 0) not in (0, target_hwnd) and cand_site != sh:
                return False
        return True

    if _matches_intended(win):
        desktop_control.record_diagnostics(
            chrome_detected=True, chrome_foreground=True,
            foreground_app=str(win.get("app") or bname),
        )
        return True, win, ""

    # Activate exact remembered HWND first (recovers cleanly if Explorer or REM stole focus)
    focus_res: dict = {}
    act = None
    want_title = title_hint or str(win.get("title") or "")
    if target_hwnd > 0 and hasattr(appidentity, "focus_hwnd"):
        focus_res = appidentity.focus_hwnd(target_hwnd)
        if focus_res.get("activated"):
            time.sleep(0.1)
            refreshed = _find_browser_window(bname, site_hint=sh)
            if _matches_intended(refreshed) or (
                focus_res.get("target_hwnd")
                and not screenview.is_own_pid(int(focus_res.get("target_pid", 0) or 0))
            ):
                merged = {
                    **refreshed,
                    "found": True,
                    "foreground": True,
                    "hwnd": int(refreshed.get("hwnd") or focus_res.get("target_hwnd") or target_hwnd),
                    "pid": int(refreshed.get("pid") or focus_res.get("target_pid") or 0),
                    "title": str(refreshed.get("title") or focus_res.get("target_window_title") or win.get("title") or ""),
                }
                desktop_control.record_diagnostics(
                    chrome_detected=True, chrome_foreground=True,
                    foreground_app=str(merged.get("app") or bname),
                )
                return True, merged, ""

    if sh or want_title:
        focus_res = appidentity.focus_window_by_app(bname, title_hint=want_title)
        if focus_res.get("activated"):
            time.sleep(0.1)
            refreshed = _find_browser_window(bname, site_hint=sh)
            if _matches_intended(refreshed) or (
                focus_res.get("target_hwnd")
                and not screenview.is_own_pid(int(focus_res.get("target_pid", 0) or 0))
            ):
                merged = {
                    **refreshed,
                    "found": True,
                    "foreground": True,
                    "hwnd": int(refreshed.get("hwnd") or focus_res.get("target_hwnd") or 0),
                    "pid": int(refreshed.get("pid") or focus_res.get("target_pid") or 0),
                    "title": str(refreshed.get("title") or focus_res.get("target_window_title") or win.get("title") or ""),
                }
                desktop_control.record_diagnostics(
                    chrome_detected=True, chrome_foreground=True,
                    foreground_app=str(merged.get("app") or bname),
                )
                return True, merged, ""

    act = screenact.focus_app(bname)
    time.sleep(0.15)
    refreshed = _find_browser_window(bname, site_hint=sh)
    if _matches_intended(refreshed):
        desktop_control.record_diagnostics(
            chrome_detected=True, chrome_foreground=True,
            foreground_app=str(refreshed.get("app") or bname),
        )
        return True, refreshed, ""

    if not focus_res:
        focus_res = appidentity.focus_window_by_app(bname, title_hint=want_title)
        if focus_res.get("activated"):
            time.sleep(0.1)
            refreshed2 = _find_browser_window(bname, site_hint=sh)
            if (refreshed2.get("foreground") or focus_res.get("target_hwnd")) and not screenview.is_own_pid(
                int(refreshed2.get("pid") or focus_res.get("target_pid") or 0)
            ):
                merged = {
                    **refreshed2,
                    "found": True,
                    "foreground": True,
                    "hwnd": int(refreshed2.get("hwnd") or focus_res.get("target_hwnd") or 0),
                    "pid": int(refreshed2.get("pid") or focus_res.get("target_pid") or 0),
                    "title": str(refreshed2.get("title") or focus_res.get("target_window_title") or win.get("title") or ""),
                }
                desktop_control.record_diagnostics(
                    chrome_detected=True, chrome_foreground=True,
                    foreground_app=str(merged.get("app") or bname),
                )
                return True, merged, ""

    desktop_control.record_diagnostics(
        chrome_detected=bool(win.get("found")), chrome_foreground=False,
        foreground_app=str(refreshed.get("app") or "unknown"),
    )
    err_hint = (getattr(act, "error", "") if act is not None else "") or focus_res.get("reason") or "foreground lock"
    return False, refreshed, f"I couldn't complete that action because {bname} did not become the foreground window ({err_hint})."


# --- native (no-DOM) YouTube control state, read from the player's own buttons ---------
# YouTube labels its transport buttons with their NEXT action: "Pause (k)" while
# playing, "Play (k)" while paused, "Mute (m)" / "Unmute (m)", "Full screen (f)" /
# "Exit full screen (f)". Reading those labels through UI Automation lets the
# native path stay idempotent and verifiable instead of blindly toggling.
_YT_BUTTON_RULES = {
    "playback": ((re.compile(r"^pause\b.*\(k\)", re.I), "playing"), (re.compile(r"^play\b.*\(k\)", re.I), "paused")),
    "mute": ((re.compile(r"^mute\b.*\(m\)", re.I), "unmuted"), (re.compile(r"^unmute\b.*\(m\)", re.I), "muted")),
    "fullscreen": ((re.compile(r"^full ?screen\b.*\(f\)", re.I), "windowed"), (re.compile(r"^exit full ?screen\b.*\(f\)", re.I), "fullscreen")),
}


def _yt_native_state(view, kind: str) -> str | None:
    """'playing'|'paused'|'muted'|... from visible player buttons; None if unreadable."""
    for el in (getattr(view, "elements", ()) or ()):
        label = " ".join(str(getattr(el, "name", "") or getattr(el, "label", "") or "").split())
        if not label:
            continue
        for rx, state in _YT_BUTTON_RULES.get(kind, ()):
            if rx.search(label):
                return state
    return None


def _native_toggle_with_verification(bname: str, kind: str, want: str, key: str, action: str, win: dict) -> str:
    """Press a YouTube toggle key only if needed, then verify via the button label."""
    from core import desktop_control as _dc
    before = _inspect_visible_browser_window(bname, focus_if_needed=False, allow_ocr=False).get("view")
    cur = _yt_native_state(before, kind)
    if cur == want:
        return outcome.done("browser_control", action=action, browser=bname, control_mode="native_windows",
                            already=True, verified=True, verification_status="verified")
    if cur is None:
        return outcome.unverified(
            "browser_control",
            "the player's state is not readable without DOM access, so I did not press a toggle key blindly",
            action=action, browser=bname, control_mode="native_windows", verified=False)
    for el in (getattr(before, "elements", ()) or ()):
        if getattr(el, "focused", False) and str(getattr(el, "role", "") or "").casefold() in {"edit", "combobox", "searchbox"}:
            return outcome.unverified(
                "browser_control", "a text field has keyboard focus, so the player key was not sent",
                action=action, browser=bname, control_mode="native_windows", verified=False)
    r = screenact.press(key)
    _dc.record_diagnostics(last_media_status=f"{action}:{'ok' if r.ok else 'fail'}")
    if not r.ok:
        return outcome.failed("browser_control", f"{action} failed in {bname}: {r.error}", action=action, browser=bname)
    time.sleep(0.3)
    after = _inspect_visible_browser_window(bname, focus_if_needed=False, allow_ocr=False).get("view")
    if _yt_native_state(after, kind) == want:
        return outcome.done("browser_control", action=action, browser=bname, control_mode="native_windows",
                            verified=True, verification_status="verified")
    return outcome.unverified("browser_control", "the key was sent but the player state did not visibly change",
                              action=action, browser=bname, control_mode="native_windows", verified=False)



def _extract_yt_time_seconds(view) -> float | None:
    """Read visible player timestamp (e.g. '1:24 / 12:10' or '1:24') from UIA/OCR if exposed."""
    if view is None:
        return None
    for el in getattr(view, "elements", ()) or ():
        txt = str(getattr(el, "name", "") or getattr(el, "value", "") or "").strip()
        if not txt:
            continue
        m = re.match(r"^(\d{1,2}):(\d{2})(?::(\d{2}))?(?:\s*/|\s*$)", txt)
        if m:
            parts = [int(p) for p in m.groups() if p is not None]
            if len(parts) == 2:
                return float(parts[0] * 60 + parts[1])
            if len(parts) == 3:
                return float(parts[0] * 3600 + parts[1] * 60 + parts[2])
    return None


def _native_browser_action(action: str, params: dict, browser_name: str, player=None) -> str:
    """Operate directly on the user's real visible browser window when CDP is unavailable."""
    from core import screenact, screenview

    bname = _ALIASES.get(str(browser_name or "chrome").lower().strip(), str(browser_name or "chrome").lower().strip())
    site_hint = _infer_requested_site(action, params)
    win = _find_browser_window(bname, site_hint=site_hint)
    if action in ("inspect", "get_url", "get_text"):
        prov_q = str(params.get("provenance_query") or params.get("description") or params.get("text") or "").strip()
        if any(k in prov_q.casefold() for k in ("where did you get", "provenance", "source of title", "how did you know")):
            return outcome.done(
                "browser_control",
                action=action, browser=bname, control_mode="native_windows",
                provenance=inspect_title_provenance(prov_q),
                verified=True,
            )
        is_fast_url = action == "get_url"
        sync = _inspect_visible_browser_window(
            bname,
            focus_if_needed=(not is_fast_url),
            allow_ocr=(not is_fast_url),
            site_hint=site_hint,
            lightweight=is_fast_url,
        )
        timings = sync.get("timings") or {}
        videos = sync.get("videos") or []
        wa_contacts = sync.get("wa_contacts") or []
        wa_chat = sync.get("wa_chat") or {}
        is_wa_surface = (sync.get("site") == "whatsapp") or (
            "whatsapp" in (sync.get("title", "") + " " + sync.get("url", "")).casefold()
        )
        summary_parts = []
        if is_wa_surface:
            if wa_chat.get("verified"):
                summary_parts.append(f"Selected WhatsApp chat: {wa_chat['active_contact']}")
            if wa_contacts:
                summary_parts.append(
                    f"Unique WhatsApp contacts ({len(wa_contacts)}): "
                    + "; ".join(f"{c['position']}. {c['display_name']}" for c in wa_contacts[:6])
                )
        elif sync.get("empty_home"):
            summary_parts.append("YouTube Home ('Try searching to get started', 0 results)")
        elif videos:
            readable_vids = [
                f"{v['position']}. {v['exact_title']}"
                for v in videos[:5]
                if v.get("evidence_status", "OBSERVED") not in ("UNKNOWN", "INFERRED") and (v.get("exact_title") or "").strip()
            ]
            if readable_vids:
                summary_parts.append("Videos: " + "; ".join(readable_vids))
            else:
                summary_parts.append(f"Visible YouTube video cards ({len(videos)}), but titles could not be read reliably")
        if site_hint and sync.get("site_mismatch"):
            return outcome.unverified(
                "browser_control",
                f"{bname} is currently showing {sync.get('title') or sync.get('site') or 'another tab'}, not {site_hint}.",
                action=action, browser=bname, control_mode="native_windows", cdp_available=False,
                requested_site=site_hint, actual_site=sync.get("site", ""),
                window_identity=sync.get("window_identity", ""),
                title=sync.get("title", ""), url=sync.get("url", ""),
                timing_hwnd_ms=timings.get("hwnd_ms", 0.0),
                timing_obs_ms=timings.get("obs_ms", 0.0),
                timing_total_ms=timings.get("total_ms", 0.0),
                verified=False,
            )
        if not win.get("found") and not sync.get("title") and not sync.get("url"):
            return outcome.unverified(
                "browser_control",
                f"no controllable {bname} tab or active browser window title was found",
                browser=bname, control_mode="native_windows", cdp_available=False,
                window_identity=sync.get("window_identity", ""),
                timestamp=sync.get("timestamp", ""),
                extracted_text_count=sync.get("extracted_text_count", 0),
                uia_items=sync.get("uia_items", 0),
                ocr_items=sync.get("ocr_items", 0),
                timing_hwnd_ms=timings.get("hwnd_ms", 0.0),
                timing_obs_ms=timings.get("obs_ms", 0.0),
                timing_total_ms=timings.get("total_ms", 0.0),
                confidence="uncertain",
                target_identified=False,
                page_state="unverified", verification_step="open_browser_window",
            )
        if is_wa_surface:
            try:
                from core import desktop_control as _dc
                _auth_ctx = _dc.current_bound_auth_context()
                if wa_chat.get("verified") and wa_chat.get("active_contact"):
                    _dc.set_active_target(
                        target_type="contact",
                        exact_target_name=wa_chat["active_contact"],
                        browser=bname, tab_title=sync.get("title", ""), tab_url=sync.get("url", ""),
                        confidence="high", extraction_source=sync.get("source", ""), auth_context=_auth_ctx,
                    )
                elif wa_contacts:
                    _dc.set_active_target(
                        target_type="contact",
                        exact_target_name=wa_contacts[0]["display_name"],
                        browser=bname, tab_title=sync.get("title", ""), tab_url=sync.get("url", ""),
                        result_position=1, point=wa_contacts[0]["point"], rect=wa_contacts[0].get("rect"),
                        confidence=wa_contacts[0].get("confidence", "medium"),
                        extraction_source=wa_contacts[0].get("source", ""), auth_context=_auth_ctx,
                    )
            except Exception:
                pass
            if action == "get_url":
                return outcome.done(
                    "browser_control", action="get_url", browser=bname, control_mode="native_windows",
                    active_application="WhatsApp (Chrome)",
                    url=(sync.get("url") or "https://web.whatsapp.com/"),
                    title=sync.get("title", ""),
                    window_identity=sync.get("window_identity", ""),
                    window_hwnd=sync.get("hwnd", 0), window_pid=sync.get("pid", 0),
                    timing_hwnd_ms=timings.get("hwnd_ms", 0.0),
                    timing_obs_ms=timings.get("obs_ms", 0.0),
                    timing_total_ms=timings.get("total_ms", 0.0),
                    verified=bool(sync.get("title") or sync.get("url")),
                )
            return outcome.done(
                "browser_control", action=action, browser=bname, control_mode="native_windows",
                active_application="WhatsApp (Chrome)",
                unique_contact_count=len(wa_contacts),
                active_contact=wa_chat.get("active_contact", wa_contacts[0]["display_name"] if len(wa_contacts) == 1 else ""),
                selected_chat_verified=bool(wa_chat.get("verified")),
                uia_items=sync.get("uia_items", 0), ocr_items=sync.get("ocr_items", 0),
                extracted_text_count=sync.get("extracted_text_count", 0),
                timing_hwnd_ms=timings.get("hwnd_ms", 0.0),
                timing_obs_ms=timings.get("obs_ms", 0.0),
                timing_total_ms=timings.get("total_ms", 0.0),
                source=sync.get("source", "windows_uia"),
                detail=(" | ".join(summary_parts) if summary_parts else "WhatsApp is open; 0 matching contacts visible."),
                verified=bool(wa_contacts or wa_chat.get("verified")),
            )
        if action == "get_url":
            if sync.get("url"):
                return outcome.done(
                    "browser_control", action="get_url", browser=bname,
                    control_mode="native_windows", cdp_available=False,
                    url=sync["url"], title=sync.get("title", ""),
                    window_identity=sync.get("window_identity", ""),
                    window_pid=sync.get("pid", 0), window_hwnd=sync.get("hwnd", 0),
                    timestamp=sync.get("timestamp", ""),
                    extracted_text_count=sync.get("extracted_text_count", 0),
                    uia_items=sync.get("uia_items", 0),
                    ocr_items=sync.get("ocr_items", 0),
                    timing_hwnd_ms=timings.get("hwnd_ms", 0.0),
                    timing_obs_ms=timings.get("obs_ms", 0.0),
                    timing_total_ms=timings.get("total_ms", 0.0),
                    confidence=sync.get("confidence", "medium"),
                    source=sync.get("source", "windows_uia"), verified=True,
                )
            return outcome.unverified(
                "browser_control",
                f"CDP is unavailable on {bname} and UIA did not expose the full URL; active window title is {sync.get('title')!r}",
                browser=bname, control_mode="native_windows", cdp_available=False,
                title=sync.get("title", ""), window_pid=sync.get("pid", 0),
                window_hwnd=sync.get("hwnd", 0),
                window_identity=sync.get("window_identity", ""),
                timestamp=sync.get("timestamp", ""),
                extracted_text_count=sync.get("extracted_text_count", 0),
                uia_items=sync.get("uia_items", 0),
                ocr_items=sync.get("ocr_items", 0),
                timing_hwnd_ms=timings.get("hwnd_ms", 0.0),
                timing_obs_ms=timings.get("obs_ms", 0.0),
                timing_total_ms=timings.get("total_ms", 0.0),
                confidence=sync.get("confidence", "uncertain"),
                source=sync.get("source", "windows_uia"),
            )
        is_youtube = "youtube" in (sync.get("title", "") + " " + sync.get("url", "")).casefold()
        if is_youtube and not videos and not sync.get("empty_home"):
            return outcome.unverified(
                "browser_control",
                "Tarun-kun, I can see YouTube, but I can't reliably read the video titles right now.",
                action=action,
                browser=bname,
                control_mode="native_windows",
                cdp_available=False,
                window_identity=sync.get("window_identity", ""),
                source=sync.get("source", "none"),
                timestamp=sync.get("timestamp", ""),
                window_pid=sync.get("pid", 0),
                window_hwnd=sync.get("hwnd", 0),
                title=sync.get("title", ""),
                extracted_text_count=sync.get("extracted_text_count", 0),
                uia_items=sync.get("uia_items", 0),
                ocr_items=sync.get("ocr_items", 0),
                confidence=sync.get("confidence", "uncertain"),
                target_identified=False,
                page_state="unverified",
            )
        highlighted_pos = None
        highlighted_title = ""
        try:
            from core import desktop_control as _dc
            auth_ctx = _dc.current_bound_auth_context()
            req_text = str(
                params.get("description") or params.get("target")
                or (getattr(auth_ctx, "instruction", "") if auth_ctx else "")
                or ""
            )
            req_ord = _parse_ordinal_index(req_text)
            if videos:
                pick_idx = (len(videos) - 1) if req_ord == -1 else (req_ord if req_ord is not None else 0)
                if 0 <= pick_idx < len(videos):
                    chosen_v = videos[pick_idx]
                    highlighted_pos = chosen_v["position"]
                    highlighted_title = chosen_v["title"]
                    _dc.set_active_target(
                        target_type="video",
                        exact_target_name=highlighted_title,
                        browser=bname,
                        tab_title=sync.get("title", ""),
                        tab_url=sync.get("url", ""),
                        result_position=highlighted_pos,
                        point=chosen_v["point"],
                        rect=chosen_v.get("rect"),
                        confidence=chosen_v.get("confidence", "medium"),
                        extraction_source=sync.get("source", ""),
                        results=videos,
                        auth_context=auth_ctx,
                    )
        except Exception:
            pass
        return outcome.done(
            "browser_control",
            action=action,
            browser=bname,
            control_mode="native_windows",
            cdp_available=False,
            window_identity=sync.get("window_identity", ""),
            source=sync.get("source", "windows_uia"),
            timestamp=sync.get("timestamp", ""),
            window_pid=sync.get("pid", 0),
            window_hwnd=sync.get("hwnd", 0),
            title=sync.get("title", ""),
            url=sync.get("url", ""),
            search_query=sync.get("search_query", ""),
            extracted_text_count=sync.get("extracted_text_count", 0),
            uia_items=sync.get("uia_items", 0),
            ocr_items=sync.get("ocr_items", 0),
            confidence=sync.get("confidence", "medium"),
            target_identified=bool(videos),
            resolved_target=(highlighted_title or sync.get("title", "")),
            highlighted_position=(highlighted_pos or 0),
            highlighted_title=highlighted_title,
            video_count=len(videos),
            empty_home=bool(sync.get("empty_home")),
            detail=(" | ".join(summary_parts) if summary_parts else sync.get("title", "")),
            verified=bool(videos or sync.get("empty_home")),
        )

    if not win.get("found"):
        return outcome.failed(
            "browser_control",
            f"no running {bname} window was found and CDP is unavailable; did not substitute another browser",
            action=action, browser=bname, dispatch_status="completed", input_status="not_executed",
            verification_status="unverified", cdp_available=False,
        )
    ok_fg, win, fg_err = _ensure_browser_foreground(
        bname, win,
        title_hint=(
            "WhatsApp" if site_hint == "whatsapp"
            else ("YouTube" if site_hint == "youtube" else str(params.get("description") or params.get("target") or ""))
        ),
    )
    if not ok_fg:
        return outcome.failed(
            "browser_control", fg_err,
            action=action, browser=bname,
            dispatch_status="completed", input_status="failed",
            verification_status="unverified", failure_reason="foreground_activation_failed",
            cdp_available=False,
        )

    # Always discard stale sync before transitions or reads.
    if action in ("go_to", "search", "new_tab", "switch_tab", "smart_click", "play_video",
                  "replay_video", "back", "forward", "reload", "close_tab"):
        _registry.invalidate_sync(bname)

    if action in ("go_to", "search", "new_tab"):
        if action == "search":
            eng = str(params.get("engine") or "").lower().strip()
            sync = _inspect_visible_browser_window(bname, focus_if_needed=True, allow_ocr=False)
            if not eng:
                hint = (sync.get("title", "") + " " + sync.get("url", "")).casefold()
                eng = "youtube" if "youtube" in hint else "google"
            base = _SEARCH_ENGINES.get(eng, _SEARCH_ENGINES["google"])
            nav_url = base + str(params.get("query") or "").strip().replace(" ", "+")
        else:
            nav_url = str(params.get("url") or "").strip()
        if nav_url:
            try:
                nav_url = _normalize_url(nav_url)
            except ValueError:
                return outcome.ask("browser_control", "Which website did you mean? Please give me its name or web address.", verified=False)
        chord = "ctrl+t" if action == "new_tab" else "ctrl+l"
        key_res = screenact.press(chord)
        if not key_res.ok:
            return outcome.failed("browser_control", f"could not focus {bname} address bar: {key_res.error}", browser=bname)
        time.sleep(0.1)
        if nav_url:
            type_res = screenact.type_text(nav_url, clear_first=True)
            if not type_res.ok:
                return outcome.failed("browser_control", f"could not type URL into {bname}: {type_res.error}", browser=bname)
            time.sleep(0.08)
            enter_res = screenact.press("enter")
            if not enter_res.ok:
                return outcome.failed("browser_control", f"could not submit navigation in {bname}: {enter_res.error}", browser=bname)
            time.sleep(0.8)
            _registry.invalidate_sync(bname)
            refreshed = _inspect_visible_browser_window(bname, focus_if_needed=False, allow_ocr=False)
            return outcome.done(
                "browser_control", action=action, browser=bname,
                control_mode="native_windows", cdp_available=False,
                url=refreshed.get("url") or nav_url, title=refreshed.get("title", ""),
                window_pid=refreshed.get("pid", 0), verified=bool(refreshed.get("title")),
            )
        return outcome.done("browser_control", action="new_tab", browser=bname, control_mode="native_windows", cdp_available=False, verified=True)

    if action in ("compose_message", "update_draft"):
        from core import desktop_control as _dc
        from plugins import _whatsapp_core as _wa

        _auth_ctx = _dc.current_bound_auth_context()
        sync = _inspect_visible_browser_window(bname, focus_if_needed=True, allow_ocr=True)
        view = sync.get("view")
        comp_state = _dc.get_active_composition_state(_auth_ctx) or {}
        expected_contact = str(
            params.get("receiver") or params.get("contact")
            or comp_state.get("selected_contact")
            or _dc.snapshot().get("active_contact") or ""
        ).strip()
        if expected_contact == "—":
            expected_contact = ""
        chat_hdr = _wa.verify_whatsapp_active_chat(view, expected_contact=expected_contact, tab_title=sync.get("title", ""), tab_url=sync.get("url", ""))
        comp_box = _wa.locate_whatsapp_composer(view)
        if not comp_box.get("composer_found"):
            return outcome.failed(
                "browser_control",
                f"I found {chat_hdr.get('active_contact') or expected_contact or 'the chat'}, but I couldn't locate the message box.",
                action=action, browser=bname,
                composer_found=False, active_chat_id=comp_box.get("active_chat_id", ""),
                dispatch_status="completed", input_status="not_executed", verification_status="unverified",
                message_sent=False,
            )
        prev_draft = str(comp_state.get("generated_content") or "")
        inst_text = str(
            params.get("composition_prompt") or params.get("description")
            or (getattr(_auth_ctx, "instruction", "") if _auth_ctx else "")
            or action
        )
        draft_lang = (
            "Hindi" if ("hindi" in inst_text.casefold() or re.search(r"[\u0900-\u097F]", inst_text))
            else ("English" if "english" in inst_text.casefold() else "")
        )
        draft = str(params.get("text") or "").strip() or _wa.generate_autonomous_draft(
            inst_text,
            recipient=str(chat_hdr.get("active_contact") or expected_contact),
            previous_draft=(prev_draft if action == "update_draft" or prev_draft else ""),
            language=draft_lang,
        )
        pt = screenact.Point(comp_box["x"], comp_box["y"], origin="element", label=comp_box["label"])
        mv = screenact.move_cursor(pt)
        clk = screenact.click(pt)
        if not clk.ok:
            return outcome.failed(
                "browser_control", f"clicking WhatsApp composer failed: {clk.error}",
                action=action, browser=bname, dispatch_status="completed",
                input_status="failed", verification_status="unverified", message_sent=False,
            )
        time.sleep(0.08)
        t_res = screenact.type_text(draft, clear_first=True)
        if not t_res.ok:
            return outcome.failed(
                "browser_control", f"typing draft failed: {t_res.error}",
                action=action, browser=bname, dispatch_status="completed",
                input_status="failed", verification_status="unverified", message_sent=False,
            )
        after_v = _inspect_visible_browser_window(bname, focus_if_needed=False, allow_ocr=False)
        after_vals = " ".join(
            str(getattr(e, "value", "") or getattr(e, "name", "") or "")
            for e in (getattr(after_v.get("view"), "elements", ()) or ())
        ).casefold()
        readback_ok = bool(draft and draft.splitlines()[0].strip().casefold() in after_vals)
        contact_name = str(chat_hdr.get("active_contact") or expected_contact)
        _dc.set_active_composition_state(
            composition_requested=True,
            composition_task_id=str(getattr(_auth_ctx, "task_id", "") or ""),
            selected_contact=contact_name,
            generated_content=draft,
            typing_authorized=True,
            message_sent=False,
            language=draft_lang,
            draft_verified=readback_ok,
            auth_context=_auth_ctx,
        )
        _dc.record_diagnostics(
            composition_status=("updated" if action == "update_draft" else "generated"),
            typing_status=("verified" if readback_ok else "typed_unverified"),
            sending_status="not_sent",
            last_keyboard_status=("ok" if t_res.ok else "failed"),
        )
        return outcome.done(
            "browser_control", action=action, browser=bname,
            control_mode="native_windows", resolved_target=comp_box["label"],
            selected_contact=contact_name, selected_chat_verified=bool(chat_hdr.get("verified")),
            generated_content_hash=_wa.draft_hash(draft),
            composition_requested=True, typing_authorized=True, message_sent=False,
            lifecycle_state=(_dc.MSG_DRAFT_VERIFIED if readback_ok else _dc.MSG_COMPOSING),
            dispatch_status="completed", input_status="completed",
            verification_status=("verified" if readback_ok else "input_dispatched"),
            cursor_moved=mv.ok, verified=True,
            detail=f"Drafted and typed message into WhatsApp composer for {contact_name or 'active chat'} without sending.",
        )

    if action == "search_contact":
        from core import desktop_control as _dc
        from plugins import _whatsapp_core as _wa

        q = str(params.get("text") or params.get("query") or params.get("description") or "").strip()
        if not q:
            return outcome.ask("browser_control", "Which contact or chat should I search for?", browser=bname, verified=False)
        sb = None
        sync = {}
        recovery_attempts = 0
        t_resolve0 = time.monotonic()
        while recovery_attempts < 2:
            recovery_attempts += 1
            sync = _inspect_visible_browser_window(bname, focus_if_needed=True, allow_ocr=True, site_hint="whatsapp")
            # Verify active window/tab really is WhatsApp Web before searching
            is_wa_win = (
                sync.get("site") == "whatsapp"
                or "whatsapp" in (sync.get("title", "") + " " + sync.get("url", "")).casefold()
            )
            if not is_wa_win or not sync.get("foreground"):
                _registry.invalidate_sync(bname)
                time.sleep(0.15)
                continue
            fg_h = int(win.get("hwnd", 0) or 0)
            sb = _wa.resolve_whatsapp_search_field(
                sync.get("view"),
                target_hwnd=int(sync.get("hwnd", 0) or fg_h),
                foreground_hwnd=fg_h,
            )
            if sb.get("found") and sb.get("visible") and sb.get("enabled"):
                break
            sb = None
            _registry.invalidate_sync(bname)
            time.sleep(0.15)
        resolve_ms = round((time.monotonic() - t_resolve0) * 1000.0, 1)
        if not sb or not sb.get("found"):
            # Strictly refuse unverified clicks or blind Enter when WhatsApp search field is unresolved.
            return outcome.failed(
                "browser_control",
                f"Could not identify a verified WhatsApp search field inside {bname} ({sync.get('title') or 'unknown window'}) after {recovery_attempts} attempts; no key or click was sent.",
                action="search_contact", browser=bname,
                recovery_attempts=recovery_attempts,
                search_bar_found=False,
                timing_resolve_ms=resolve_ms,
            )
        t_input0 = time.monotonic()
        pt = screenact.Point(sb["x"], sb["y"], origin="element", label=sb["label"])
        screenact.move_cursor(pt)
        clk = screenact.click(pt)
        if not clk.ok:
            return outcome.failed("browser_control", f"clicking WhatsApp search failed: {clk.error}", action="search_contact", browser=bname)
        t_res = screenact.type_text(q, clear_first=True)
        if not t_res.ok:
            return outcome.failed("browser_control", f"typing contact query failed: {t_res.error}", action="search_contact", browser=bname)
        input_ms = round((time.monotonic() - t_input0) * 1000.0, 1)
        time.sleep(0.35)
        t_ver0 = time.monotonic()
        _registry.invalidate_sync(bname)
        after = _inspect_visible_browser_window(bname, focus_if_needed=False, allow_ocr=True, site_hint="whatsapp")
        after_sb = _wa.resolve_whatsapp_search_field(after.get("view"))
        query_verified = bool(after_sb.get("found") and q.casefold() in str(after_sb.get("value") or "").casefold())
        raw_contacts = _wa.extract_whatsapp_contacts(after.get("view"), query=q)
        contacts = [c for c in raw_contacts if _wa.contact_match(q, c["display_name"]) != "none"]
        verify_ms = round((time.monotonic() - t_ver0) * 1000.0, 1)
        _dc.record_diagnostics(unique_contact_count=len(contacts), active_application="WhatsApp (Chrome)")
        if contacts:
            _dc.set_active_target(
                target_type="contact", exact_target_name=contacts[0]["display_name"],
                browser=bname, tab_title=after.get("title", ""), tab_url=after.get("url", ""),
                result_position=1, point=contacts[0]["point"], rect=contacts[0].get("rect"),
                confidence=contacts[0].get("confidence", "medium"), extraction_source=contacts[0].get("source", ""),
                results=contacts,
                auth_context=_dc.current_bound_auth_context(),
            )
        return outcome.done(
            "browser_control", action="search_contact", browser=bname,
            resolved_target=q,
            search_source=sb.get("source", ""),
            search_method=sb.get("method", ""),
            query_verified=query_verified,
            unique_contact_count=len(contacts),
            matched_contacts="; ".join(c["display_name"] for c in contacts[:5]),
            recovery_attempts=recovery_attempts,
            timing_resolve_ms=resolve_ms,
            timing_input_ms=input_ms,
            timing_verify_ms=verify_ms,
            dispatch_status="completed", input_status="completed",
            verification_status=("verified" if (contacts or query_verified) else "unverified"),
            verified=bool(contacts or query_verified),
        )

    def _click_diag(clk_action: str, pt, *, rect_info=None) -> tuple:
        cb = screenact.get_cursor_pos() if hasattr(screenact, "get_cursor_pos") else (0, 0)
        fg_hwnd_b = int(win.get("hwnd", 0) or 0)
        if _OS == "Windows":
            try:
                import ctypes
                live_fg = int(ctypes.windll.user32.GetForegroundWindow() or 0)
                if live_fg:
                    fg_hwnd_b = live_fg if fg_hwnd_b <= 0 else fg_hwnd_b
            except Exception:
                pass
        mv = screenact.move_cursor(pt)
        if clk_action == "right_click":
            clk = screenact.right_click(pt)
        elif clk_action == "double_click":
            clk = screenact.double_click(pt)
        else:
            clk = screenact.click(pt)
        ca = screenact.get_cursor_pos() if hasattr(screenact, "get_cursor_pos") else (getattr(pt, "x", 0), getattr(pt, "y", 0))
        fg_hwnd_a = int(win.get("hwnd", 0) or 0)
        if _OS == "Windows":
            try:
                import ctypes
                live_fg_a = int(ctypes.windll.user32.GetForegroundWindow() or 0)
                if live_fg_a:
                    fg_hwnd_a = live_fg_a
            except Exception:
                pass
        thwnd = int(win.get("hwnd", 0) or 0)
        tpid = int(win.get("pid", 0) or 0)
        fhwnd = int(thwnd or fg_hwnd_a or fg_hwnd_b)
        try:
            from core import desktop_control as _dc_local
            _dc_local.record_diagnostics(
                last_cursor_pos=f"{ca[0]},{ca[1]}",
                last_mouse_status=("ok" if clk.ok else "failed"),
            )
        except Exception:
            pass
        return mv, clk, {
            "target_hwnd": thwnd,
            "target_pid": tpid,
            "foreground_hwnd": fhwnd,
            "fg_hwnd_before": int(fg_hwnd_b or 0),
            "fg_hwnd_after": int(fg_hwnd_a or 0),
            "target_rect": str(rect_info or ""),
            "cursor_before": f"{cb[0]},{cb[1]}",
            "cursor_after": f"{ca[0]},{ca[1]}",
            "dispatch_status": ("completed" if clk.ok else "failed"),
            "input_status": ("completed" if clk.ok else "failed"),
        }

    def _open_wa_contact_bounded(
        raw_desc: str,
        *,
        sync_snap: dict,
        action_label: str,
        resolved_ref: dict | None = None,
        auth_ctx=None,
        task_id: str = "",
    ) -> str:
        from core import desktop_control as _dc, screen_observation as _so
        from plugins import _whatsapp_core as _wa

        desc_str = str(raw_desc or "").strip()
        ord_idx = _parse_ordinal_index(desc_str)
        wa_intent = _wa.parse_whatsapp_intent_target(desc_str)
        if resolved_ref is not None and resolved_ref.get("target_type") == "contact":
            c_query = str(resolved_ref.get("exact_target_name") or "").strip()
        elif ord_idx is not None:
            c_query = ""
        elif wa_intent.get("contact"):
            c_query = str(wa_intent["contact"]).strip()
        else:
            c_query = re.sub(
                r"\b(?:the|first|second|third|fourth|last|1st|2nd|3rd|4th|one|open|select|click|chat|conversation|contact|named|called|with|for|shed|in|whatsapp|now)\b",
                " ", desc_str, flags=re.IGNORECASE,
            )
            c_query = " ".join(c_query.split()).strip(" .,!?'\"-")

        if not c_query and ord_idx is None:
            return outcome.ask(
                "browser_control",
                "Which WhatsApp contact or chat would you like me to open, Tarun-kun?",
                browser=bname, task_id=task_id, verified=False,
            )

        cur_sync = sync_snap
        contacts = _wa.extract_whatsapp_contacts(cur_sync.get("view"), query=c_query)
        if not contacts and resolved_ref and resolved_ref.get("results"):
            contacts = list(resolved_ref.get("results") or ())
        if c_query:
            contacts = [c for c in contacts if _wa.contact_match(c_query, c["display_name"]) != "none"]

        # Dedicated bounded sidebar search when contact is not yet in the visible list
        if not contacts and c_query and ord_idx is None:
            sb = _wa.locate_whatsapp_search_bar(cur_sync.get("view"))
            if sb is None:
                _dc.record_diagnostics(active_application="WhatsApp (Chrome)", unique_contact_count=0, selected_chat_verified=False)
                return outcome.failed(
                    "browser_control",
                    f"Could not locate WhatsApp's sidebar search field to find {c_query!r}.",
                    action=action_label, browser=bname, task_id=task_id,
                    failure_reason="whatsapp_search_field_not_found", verified=False,
                )
            sb_pt = screenact.Point(sb["x"], sb["y"], origin="element", label=sb["label"])
            _mv_sb, clk_sb, cdiag_sb = _click_diag("click", sb_pt, rect_info=sb.get("rect"))
            if not clk_sb.ok:
                return outcome.failed(
                    "browser_control",
                    f"Focusing WhatsApp search field failed: {clk_sb.error}",
                    action=action_label, browser=bname, task_id=task_id, **cdiag_sb,
                )
            t_res = screenact.type_text(c_query, clear_first=True)
            if not t_res.ok:
                return outcome.failed(
                    "browser_control",
                    f"Entering {c_query!r} in WhatsApp search failed: {t_res.error}",
                    action=action_label, browser=bname, task_id=task_id, **cdiag_sb,
                )
            time.sleep(0.45)
            _registry.invalidate_sync(bname)
            _so.invalidate_observation_provenance(reason="whatsapp_search_query", hwnd=int(cur_sync.get("hwnd", 0) or 0))
            cur_sync = _inspect_visible_browser_window(bname, focus_if_needed=False, allow_ocr=True, site_hint="whatsapp")
            contacts = _wa.extract_whatsapp_contacts(cur_sync.get("view"), query=c_query)
            contacts = [c for c in contacts if _wa.contact_match(c_query, c["display_name"]) != "none"]

        _dc.record_diagnostics(unique_contact_count=len(contacts), active_application="WhatsApp (Chrome)")
        if not contacts:
            try:
                _dc.clear_task_targets()
            except Exception:
                pass
            return outcome.unverified(
                "browser_control",
                f"I searched WhatsApp for {c_query or desc_str!r}, Tarun-kun, but couldn't find a matching contact.",
                action=action_label, browser=bname, task_id=task_id,
                unique_contact_count=0, selected_chat_verified=False,
                target_identified=False, verified=False,
            )

        chosen = None
        if ord_idx is not None:
            pick = (len(contacts) - 1) if ord_idx == -1 else ord_idx
            if 0 <= pick < len(contacts):
                chosen = contacts[pick]
        if chosen is None:
            exact = [c for c in contacts if c_query and c["display_name"].casefold() == c_query.casefold()]
            chosen = exact[0] if len(exact) == 1 else (contacts[0] if len(contacts) == 1 else None)
        if chosen is None:
            _dc.set_active_target(
                target_type="contact", exact_target_name=contacts[0]["display_name"],
                browser=bname, tab_title=cur_sync.get("title", ""), tab_url=cur_sync.get("url", ""),
                result_position=1, point=contacts[0]["point"], rect=contacts[0].get("rect"),
                results=contacts, auth_context=auth_ctx,
            )
            names = ", ".join(c["display_name"] for c in contacts[:4])
            return outcome.ask(
                "browser_control",
                f"I found {len(contacts)} contacts matching {c_query or desc_str!r}: {names}. Which one should I open?",
                browser=bname, task_id=task_id, unique_contact_count=len(contacts), verified=False,
            )

        cx, cy = chosen["point"]
        v_rect = getattr(cur_sync.get("view"), "window_rect", None)
        if isinstance(v_rect, (list, tuple)) and len(v_rect) == 4 and int(v_rect[2] or 0) > 100:
            wl, wt, ww, wh = (int(x or 0) for x in v_rect)
            if not (wl <= int(cx) <= wl + ww and wt <= int(cy) <= wt + wh):
                return outcome.failed(
                    "browser_control",
                    f"Contact {chosen['display_name']!r} coordinates ({cx},{cy}) fell outside the visible Chrome window; refused to click.",
                    action=action_label, browser=bname, task_id=task_id,
                    failure_reason="coordinates_outside_window", verified=False,
                )

        pt = screenact.Point(cx, cy, origin="element", label=chosen["display_name"])
        mv, clk, cdiag = _click_diag("click", pt, rect_info=chosen.get("rect"))
        if not clk.ok:
            _dc.clear_task_targets()
            _so.invalidate_observation_provenance(reason="whatsapp_contact_click_failed", hwnd=int(cur_sync.get("hwnd", 0) or 0))
            return outcome.failed(
                "browser_control", f"clicking contact {chosen['display_name']!r} failed: {clk.error}",
                action=action_label, browser=bname, task_id=task_id, **cdiag,
            )
        time.sleep(0.35)
        _registry.invalidate_sync(bname)
        _so.invalidate_observation_provenance(reason="whatsapp_contact_clicked", hwnd=int(cur_sync.get("hwnd", 0) or 0))
        after = _inspect_visible_browser_window(bname, focus_if_needed=False, allow_ocr=True, site_hint="whatsapp")
        hdr = _wa.verify_whatsapp_active_chat(
            after.get("view"), expected_contact=chosen["display_name"],
            tab_title=after.get("title", ""), tab_url=after.get("url", ""),
        )
        if not hdr.get("verified"):
            time.sleep(0.35)
            after = _inspect_visible_browser_window(bname, focus_if_needed=False, allow_ocr=True, site_hint="whatsapp")
            hdr = _wa.verify_whatsapp_active_chat(
                after.get("view"), expected_contact=chosen["display_name"],
                tab_title=after.get("title", ""), tab_url=after.get("url", ""),
            )
        chat_id = str(hdr.get("chat_id") or chosen.get("id") or "")
        chat_ok = bool(hdr.get("verified"))
        if chat_ok:
            _dc.set_active_target(
                target_type="contact", exact_target_name=chosen["display_name"],
                browser=bname, tab_title=after.get("title", ""), tab_url=after.get("url", ""),
                result_position=chosen.get("position", 1), point=chosen["point"], rect=chosen.get("rect"),
                confidence=chosen.get("confidence", "high"), extraction_source=chosen.get("source", ""),
                auth_context=auth_ctx,
            )
            _dc.set_active_composition_state(
                composition_requested=False,
                composition_task_id=str(getattr(auth_ctx, "task_id", "") or ""),
                selected_contact=chosen["display_name"],
                generated_content="",
                typing_authorized=False,
                message_sent=False,
                auth_context=auth_ctx,
            )
        else:
            _dc.clear_task_targets()
        _dc.record_diagnostics(
            active_application="WhatsApp (Chrome)",
            active_contact=(chosen["display_name"] if chat_ok else "—"),
            active_chat_id=chat_id,
            selected_chat_verified=chat_ok,
            unique_contact_count=len(contacts),
        )
        if not chat_ok:
            return outcome.unverified(
                "browser_control",
                f"I clicked {chosen['display_name']}, but I couldn't confirm that the chat opened.",
                action=action_label, browser=bname, task_id=task_id,
                resolved_target=chosen["display_name"], active_chat_id=chat_id,
                unique_contact_count=len(contacts), selected_chat_verified=False,
                verification_status="unverified", cursor_moved=mv.ok, click_executed=clk.ok,
                verified=False, **cdiag,
            )
        return outcome.done(
            "browser_control", action=action_label, browser=bname, task_id=task_id,
            control_mode="native_windows", resolved_target=chosen["display_name"],
            active_contact=chosen["display_name"], active_chat_id=chat_id,
            unique_contact_count=len(contacts), selected_chat_verified=True,
            verification_status="verified", cursor_moved=mv.ok, click_executed=clk.ok,
            verified=True, **cdiag,
        )

    if action == "select_contact":
        from core import desktop_control as _dc

        _auth_ctx = _dc.current_bound_auth_context()
        _cur_task_id = str(getattr(_auth_ctx, "task_id", "") or "") if _auth_ctx is not None else ""
        desc = str(params.get("description") or params.get("text") or params.get("query") or "").strip()
        ref = _dc.get_active_target(_auth_ctx)
        if not desc and ref and ref.get("target_type") == "contact":
            desc = str(ref.get("exact_target_name") or "").strip()
        sync = _inspect_visible_browser_window(bname, focus_if_needed=True, allow_ocr=True, site_hint="whatsapp")
        return _open_wa_contact_bounded(
            desc, sync_snap=sync, action_label="select_contact",
            resolved_ref=ref, auth_ctx=_auth_ctx, task_id=_cur_task_id,
        )

    if action == "clear_field":
        from core import desktop_control as _dc
        from plugins import _whatsapp_core as _wa

        desc = str(params.get("description") or params.get("selector") or "").strip()
        sync = _inspect_visible_browser_window(bname, focus_if_needed=True, allow_ocr=True)
        view = sync.get("view")
        is_wa_page = "whatsapp" in (sync.get("title", "") + " " + sync.get("url", "")).casefold()
        if is_wa_page:
            comp = _wa.locate_whatsapp_composer(view)
            if not comp.get("composer_found"):
                return outcome.failed(
                    "browser_control",
                    "I found the WhatsApp window, but I couldn't locate the message box to clear.",
                    action="clear_field", browser=bname, composer_found=False, verified=False,
                )
            pt = screenact.Point(comp["x"], comp["y"], origin="element", label=comp["label"])
            mv, clk, cdiag = _click_diag("click", pt)
            if not clk.ok:
                return outcome.failed("browser_control", f"focusing WhatsApp composer failed: {clk.error}", action="clear_field", browser=bname, **cdiag)
            time.sleep(0.08)
            screenact.press("ctrl+a")
            screenact.press("backspace")
            screenact.press("delete")
            time.sleep(0.12)
            _registry.invalidate_sync(bname)
            after_v = _inspect_visible_browser_window(bname, focus_if_needed=False, allow_ocr=True)
            rb = _wa.read_composer(after_v.get("view"))
            rb_ok, rb_val = rb["status"] in {"empty", "has_text"}, rb["text"]
            empty = rb["status"] == "empty"      # unreadable is NOT empty
            _dc.set_active_composition_state(
                composition_requested=True,
                selected_contact=str(_dc.snapshot().get("active_contact") or ""),
                generated_content=("" if empty else rb_val),
                typing_authorized=True,
                message_sent=False,
            )
            _dc.record_diagnostics(
                composition_status=("cleared" if empty else "residual_text"),
                typing_status=("empty_verified" if empty else "unverified"),
                sending_status="not_sent",
                last_keyboard_status="ok",
            )
            if empty:
                return outcome.done(
                    "browser_control", action="clear_field", browser=bname,
                    composer_found=True, composer_empty=True, focus_verified=True,
                    active_chat_id=comp.get("active_chat_id", ""),
                    verification_status="verified", verified=True,
                    detail="The message box is empty now.", **cdiag,
                )
            return outcome.unverified(
                "browser_control",
                ("I cleared the message box, but I cannot read it back to confirm it is empty."
                 if rb["status"] == "unreadable" else
                 "I tried to clear the message box, but text is still in it."),
                action="clear_field", browser=bname,
                composer_found=True, composer_empty=False, readback=rb["status"],
                verification_status="unverified", verified=False, **cdiag,
            )
        # Non-WhatsApp generic visible field clear
        target_el = None
        if view is not None:
            for el in getattr(view, "elements", ()) or ():
                if str(getattr(el, "role", "") or "").casefold() in {"edit", "combobox", "searchbox"}:
                    if not desc or desc.casefold() in str(getattr(el, "name", "") or "").casefold() or getattr(el, "focused", False):
                        target_el = el
                        break
        if target_el is None:
            return outcome.failed("browser_control", f"no editable field matching {desc or 'input'!r} was found to clear", action="clear_field", browser=bname)
        pt = screenact.Point.from_element(target_el)
        mv, clk, cdiag = _click_diag("click", pt)
        if not clk.ok:
            return outcome.failed("browser_control", f"focusing the field failed: {clk.error}",
                                  action="clear_field", browser=bname, **cdiag)
        screenact.press("ctrl+a")
        screenact.press("backspace")
        time.sleep(0.12)
        _registry.invalidate_sync(bname)
        after_f = _inspect_visible_browser_window(bname, focus_if_needed=False, allow_ocr=False).get("view")
        same = None
        for el in (getattr(after_f, "elements", ()) or ()):
            if str(getattr(el, "role", "") or "").casefold() in {"edit", "combobox", "searchbox"} and \
                    abs(int(getattr(el, "top", 0)) - int(getattr(target_el, "top", 0))) < 6 and \
                    abs(int(getattr(el, "left", 0)) - int(getattr(target_el, "left", 0))) < 6:
                same = el
                break
        readable = same is not None and getattr(same, "value_read_only", None) is not None
        if readable and not str(getattr(same, "value", "") or "").strip():
            return outcome.done("browser_control", action="clear_field", browser=bname, composer_empty=True,
                                verified=True, verification_status="verified", **cdiag)
        return outcome.unverified(
            "browser_control",
            ("the field still contains text" if readable else "the field was cleared but its content cannot be read back"),
            action="clear_field", browser=bname, composer_empty=False, verified=False, **cdiag)

    if action in ("smart_type", "type"):
        text_val = str(params.get("text") or "")
        desc = str(params.get("description") or params.get("selector") or "").strip()
        sync = _inspect_visible_browser_window(bname, focus_if_needed=True, allow_ocr=True)
        view = sync.get("view")
        is_wa_page = "whatsapp" in (sync.get("title", "") + " " + sync.get("url", "")).casefold()
        target_el = None
        if view is not None and not is_wa_page:
            for el in getattr(view, "elements", ()) or ():
                role_cf = str(getattr(el, "role", "") or "").casefold()
                name_cf = str(getattr(el, "name", "") or "").casefold()
                if desc and role_cf in {"edit", "combobox"} and _is_search_bar_query(desc):
                    if "address" in name_cf or "omnibox" in name_cf or "url" in name_cf:
                        continue
                    if "search" in name_cf or getattr(el, "focused", False):
                        target_el = el
                        break
                elif desc and role_cf in {"edit", "combobox"} and desc.casefold() in name_cf:
                    target_el = el
                    break
                elif not desc and getattr(el, "focused", False) and role_cf in {"edit", "combobox", "searchbox"}:
                    target_el = el
                    break
        target_label = ""
        detection_source = ""
        cdiag = {
            "target_hwnd": int(win.get("hwnd", 0) or 0),
            "target_pid": int(win.get("pid", 0) or 0),
            "foreground_hwnd": int(win.get("hwnd", 0) or 0),
            "target_rect": "",
            "cursor_before": "",
            "cursor_after": "",
            "dispatch_status": "completed",
            "input_status": "completed",
        }
        is_wa_composer = False
        if is_wa_page:
            from plugins import _whatsapp_core as _wa
            desc_cf = desc.casefold()
            if _is_search_bar_query(desc):
                sb = _wa.locate_whatsapp_search_bar(view)
                if sb is None:
                    return outcome.failed("browser_control", "Could not locate WhatsApp search bar.", action=action, browser=bname)
                pt = screenact.Point(sb["x"], sb["y"], origin="element", label=sb["label"])
                mv, clk, cdiag = _click_diag("click", pt)
                if not clk.ok:
                    return outcome.failed("browser_control", f"clicking WhatsApp search failed: {clk.error}", action=action, browser=bname, **cdiag)
                target_label, detection_source = sb["label"], sb["source"]
                time.sleep(0.1)
            elif (
                any(k in desc_cf for k in ("message", "composer", "draft", "reply", "type a message"))
                or not desc
            ):
                comp = _wa.locate_whatsapp_composer(view)
                if not comp.get("composer_found"):
                    return outcome.failed("browser_control", "Could not locate active WhatsApp message composer.", action=action, browser=bname, composer_found=False)
                pt = screenact.Point(comp["x"], comp["y"], origin="element", label=comp["label"])
                mv, clk, cdiag = _click_diag("click", pt)
                if not clk.ok:
                    return outcome.failed("browser_control", f"clicking WhatsApp composer failed: {clk.error}", action=action, browser=bname, **cdiag)
                target_label, detection_source, is_wa_composer = comp["label"], comp["source"], True
                time.sleep(0.08)
            else:
                return outcome.failed("browser_control", f"no WhatsApp field matching {desc!r} was found.", action=action, browser=bname)
        elif target_el is not None:
            pt = screenact.Point.from_element(target_el)
            mv, clk, cdiag = _click_diag("click", pt, rect_info=getattr(target_el, "rect", ""))
            if not clk.ok:
                return outcome.failed("browser_control", f"clicking search field failed in {bname}: {clk.error}", browser=bname, **cdiag)
            target_label = str(getattr(target_el, "name", "") or "input")
            detection_source = "uia"
            time.sleep(0.1)
        elif _is_search_bar_query(desc) or (not desc and "youtube" in (sync.get("title", "") + sync.get("url", "")).casefold()):
            sb = _locate_youtube_search_bar_native(sync, bname, desc or "YouTube search bar")
            if sb is not None:
                pt = screenact.Point(sb["x"], sb["y"], origin="element", label=sb["label"])
                mv, clk, cdiag = _click_diag("click", pt)
                if not clk.ok:
                    return outcome.failed("browser_control", f"clicking YouTube search bar failed in {bname}: {clk.error}", browser=bname, **cdiag)
                target_label = sb["label"]
                detection_source = sb["source"]
                time.sleep(0.1)
            elif not desc:
                screenact.press("/")
                time.sleep(0.15)
                target_label = "YouTube search shortcut"
                detection_source = "shortcut_focus"
            else:
                return outcome.failed(
                    "browser_control",
                    f"no visible search field matching {desc!r} was found in {bname}",
                    browser=bname,
                    window_identity=sync.get("window_identity", ""),
                    uia_items=sync.get("uia_items", 0),
                    ocr_items=sync.get("ocr_items", 0),
                    confidence=sync.get("confidence", "uncertain"),
                    target_identified=False,
                )
        elif not desc and "chrome" in win.get("app", "").casefold():
            # Focused input from prior click ("Click the YouTube search bar" -> "Type short")
            target_label = "focused Chrome input"
            detection_source = "focused_chrome"
        else:
            return outcome.failed("browser_control", f"no editable UIA field matching {desc or 'input'!r} was found in {bname}", browser=bname)
        clear_req = bool(params.get("clear_first", is_wa_composer))
        t_res = screenact.type_text(text_val, clear_first=clear_req)
        if not t_res.ok:
            return outcome.failed("browser_control", f"typing failed in {bname}: {t_res.error}", browser=bname, **cdiag)
        if not is_wa_composer and (params.get("submit") or params.get("press_enter")):
            enter_res = screenact.press("enter")
            if not enter_res.ok:
                return outcome.failed("browser_control", f"Enter after typing failed in {bname}: {enter_res.error}", browser=bname, **cdiag)
        after_type = _inspect_visible_browser_window(bname, focus_if_needed=False, allow_ocr=False)
        after_vals = " ".join(
            str(getattr(e, "value", "") or getattr(e, "name", "") or "")
            for e in (getattr(after_type.get("view"), "elements", ()) or ())
        ).casefold()
        typed_confirmed = bool(text_val and text_val.splitlines()[0].strip().casefold() in after_vals)
        if is_wa_composer:
            from core import desktop_control as _dc
            _dc.record_diagnostics(
                active_application="WhatsApp (Chrome)",
                typing_status=("verified" if typed_confirmed else "typed_unverified"),
                sending_status="not_sent",
                last_keyboard_status="ok",
            )
        return outcome.done(
            "browser_control", action=action, browser=bname,
            control_mode="native_windows",
            requested_action=action,
            target=(target_label or desc or "search bar"),
            detection_source=detection_source,
            execution_status="completed",
            verification_status=("verified" if typed_confirmed else "input_dispatched"),
            text_entered=True, entered_text=text_val,
            message_sent=(False if is_wa_composer else None),
            verified=True, **cdiag,
        )

    if action in ("play_video", "smart_click", "click", "left_click", "right_click", "double_click", "replay_video"):
        desc = str(params.get("description") or params.get("text") or params.get("selector") or "").strip()
        if action == "replay_video" and not _parse_ordinal_index(desc):
            key_res = screenact.press("0")
            if not key_res.ok:
                return outcome.failed("browser_control", f"replay key failed in {bname}: {key_res.error}", browser=bname)
            return outcome.done(
                "browser_control", action="replay_video", browser=bname,
                control_mode="native_windows", cdp_available=False,
                replayed=True, playback_verified=False, verified=False,
                detail="Sent YouTube restart key '0' to active Chrome window; HTML5 video time cannot be read without CDP.",
            )
        sync = _inspect_visible_browser_window(bname, focus_if_needed=True, allow_ocr=True)
        try:
            from core import desktop_control as _dc
            _auth_ctx = _dc.current_bound_auth_context()
            _cur_task_id = str(getattr(_auth_ctx, "task_id", "") or "") if _auth_ctx is not None else ""
        except Exception:
            _dc, _auth_ctx, _cur_task_id = None, None, ""
        desc_cf = desc.casefold()
        _is_pronoun_target = desc_cf in {
            "it", "that", "this", "that one", "this one", "that video", "this video",
            "open it", "open that", "click it", "click that", "play it", "play that",
            "yes click it", "select it", "that field", "that bar",
            "second one", "the second one", "first one", "the first one", "third one", "the third one",
        }
        _resolved_ref = (
            _dc.resolve_target_reference(desc, auth_context=_auth_ctx, request_text=str(getattr(_auth_ctx, "instruction", "") or ""))
            if (_dc is not None and _is_pronoun_target and (_auth_ctx is not None or _dc.get_active_target(None))) else None
        )
        if _resolved_ref is not None and sync.get("empty_home"):
            _resolved_ref = None
        if _resolved_ref is not None:
            ref_tab = str(_resolved_ref.get("tab_title") or "").strip().casefold()
            cur_tab = str(sync.get("title") or "").strip().casefold()
            if ref_tab and cur_tab and ref_tab != cur_tab:
                _resolved_ref = None
        _is_search_target = _is_search_bar_query(desc) or (
            _resolved_ref is not None and _resolved_ref.get("target_type") == "search_bar"
        )
        if _is_search_target:
            is_wa_now = (sync.get("site") == "whatsapp") or ("whatsapp" in (sync.get("title", "") + " " + sync.get("url", "")).casefold())
            sb = None
            if is_wa_now:
                from plugins import _whatsapp_core as _wa
                sb = _wa.locate_whatsapp_search_bar(sync.get("view"))
            else:
                sb = _locate_youtube_search_bar_native(sync, bname, desc or "YouTube search bar")
            if sb is None and _resolved_ref and _resolved_ref.get("point"):
                sx, sy = _resolved_ref["point"]
                sb = {"x": sx, "y": sy, "label": _resolved_ref.get("exact_target_name") or "YouTube search bar", "source": _resolved_ref.get("extraction_source") or "active_target"}
            if sb is None:
                return outcome.failed(
                    "browser_control",
                    f"Could not find the YouTube search bar in the active {bname} window ({sync.get('title', '')})",
                    action=action, browser=bname, task_id=_cur_task_id,
                    execution_status="failed", verification_status="unverified",
                    control_mode="native_windows", cdp_available=False,
                )
            pt = screenact.Point(sb["x"], sb["y"], origin="element", label=sb["label"])
            mv, clk, cdiag = _click_diag(action, pt)
            if not clk.ok:
                return outcome.failed(
                    "browser_control", f"{action} on YouTube search bar failed: {clk.error}",
                    action=action, resolved_target=sb["label"], browser=bname, task_id=_cur_task_id,
                    execution_status="failed", verification_status="unverified", **cdiag,
                )
            if _dc is not None:
                _dc.set_active_target(
                    target_type="search_bar", exact_target_name=sb["label"],
                    browser=bname, tab_title=sync.get("title", ""), tab_url=sync.get("url", ""),
                    point=(sb["x"], sb["y"]), confidence=("high" if "uia" in sb["source"] else "medium"),
                    extraction_source=sb["source"], auth_context=_auth_ctx,
                )
            return outcome.done(
                "browser_control", action=action, browser=bname, task_id=_cur_task_id,
                control_mode="native_windows", cdp_available=False,
                resolved_target=sb["label"], target=sb["label"], detection_source=sb["source"],
                execution_status="completed", verification_status=("verified" if mv.ok and clk.ok else "unverified"),
                x=sb["x"], y=sb["y"], cursor_moved=mv.ok,
                click_executed=clk.ok, verified=True, **cdiag,
            )
        if (
            "whatsapp" in (sync.get("title", "") + " " + sync.get("url", "")).casefold()
            and action in {"smart_click", "click", "left_click"}
            and desc
        ):
            from plugins import _whatsapp_core as _wa_chk
            if (
                (_resolved_ref is not None and _resolved_ref.get("target_type") == "contact")
                or _wa_chk.parse_whatsapp_intent_target(desc).get("is_intent")
                or any(k in desc_cf for k in ("chat", "contact"))
                or desc_cf not in {"message box", "composer"}
            ):
                return _open_wa_contact_bounded(
                    desc, sync_snap=sync, action_label=action,
                    resolved_ref=_resolved_ref, auth_ctx=_auth_ctx, task_id=_cur_task_id,
                )
        videos = sync.get("videos") or []
        if _resolved_ref is not None and _resolved_ref.get("target_type") == "video":
            # Prefer freshly re-observed matching item by title/position when present,
            # falling back cleanly to the fresh resolved target snapshot (<=120s).
            ref_title = str(_resolved_ref.get("exact_target_name") or "").strip()
            ref_pos = _resolved_ref.get("result_position")
            matched_fresh = next(
                (v for v in videos if ref_title and v["title"].casefold() == ref_title.casefold()),
                None,
            )
            if matched_fresh is None and ref_pos and 1 <= int(ref_pos) <= len(videos):
                matched_fresh = videos[int(ref_pos) - 1]
            chosen = matched_fresh or (
                {
                    "position": ref_pos or 1,
                    "title": ref_title or "video",
                    "point": _resolved_ref["point"],
                    "source": _resolved_ref.get("extraction_source") or "active_target",
                }
                if _resolved_ref.get("point") else None
            )
            if chosen is not None:
                cx, cy = chosen["point"]
                pt = screenact.Point(cx, cy, origin="element", label=chosen["title"])
                mv, clk, cdiag = _click_diag(action, pt, rect_info=chosen.get("rect"))
                if not clk.ok:
                    return outcome.failed(
                        "browser_control", f"real click on {chosen['title']!r} failed: {clk.error}",
                        action=action, resolved_target=chosen["title"], browser=bname, task_id=_cur_task_id,
                        execution_status="failed", verification_status="unverified", **cdiag,
                    )
                time.sleep(0.75)
                _registry.invalidate_sync(bname)
                after = _inspect_visible_browser_window(bname, focus_if_needed=False, allow_ocr=False)
                title_changed = bool(after.get("title") and after.get("title") != sync.get("title"))
                if title_changed:
                    return outcome.done(
                        "browser_control",
                        action=action, browser=bname, task_id=_cur_task_id,
                        control_mode="native_windows", cdp_available=False,
                        resolved_target=chosen["title"], target=chosen["title"],
                        execution_status="completed", verification_status="verified",
                        position=chosen["position"], title=chosen["title"],
                        source=chosen["source"], cursor_moved=mv.ok, click_executed=clk.ok,
                        playback_verified=False, resulting_title=after.get("title", ""),
                        detail=f"The video is {chosen['title']!r}. Clicked it in {bname}.",
                        verified=True, **cdiag,
                    )
                return outcome.unverified(
                    "browser_control",
                    "I sent the click, Tarun-kun, but I couldn't confirm that the video opened.",
                    action=action, browser=bname, task_id=_cur_task_id,
                    control_mode="native_windows", cdp_available=False,
                    resolved_target=chosen["title"], target=chosen["title"],
                    execution_status="completed", verification_status="unverified",
                    position=chosen["position"], title=chosen["title"],
                    cursor_moved=mv.ok, click_executed=clk.ok, playback_verified=False,
                    verified=False, **cdiag,
                )
        ordinal_idx = _parse_ordinal_index(desc or ("first video" if action == "play_video" else ""))
        if ordinal_idx is not None:
            t_vid0 = time.monotonic()
            if not videos:
                if sync.get("empty_home"):
                    return (
                        "I see the YouTube home page in Chrome ('Try searching to get started') "
                        "with 0 visible video results; tell me what to search for or name a specific video title."
                    )
                _log_browser_action_diag(
                    task_id=_cur_task_id, browser_hwnd=int(win.get("hwnd", 0) or 0),
                    browser_pid=int(win.get("pid", 0) or 0), requested_site=site_hint or "youtube",
                    verified_site=str(sync.get("site") or ""), active_tab=sync.get("title", ""),
                    action="play_video", selection_method=str(sync.get("source") or "none"),
                    verification_result=ACTION_UNVERIFIED, failure_reason="video_titles_unreadable",
                    elapsed_ms=(time.monotonic() - t_vid0) * 1000.0,
                )
                return outcome.unverified(
                    "browser_control",
                    "Tarun-kun, I can see YouTube, but I can't reliably read the video titles right now.",
                    action=action,
                    browser=bname, task_id=_cur_task_id, cdp_available=False,
                    window_identity=sync.get("window_identity", ""),
                    source=sync.get("source", "none"),
                    timestamp=sync.get("timestamp", ""),
                    extracted_text_count=sync.get("extracted_text_count", 0),
                    uia_items=sync.get("uia_items", 0),
                    ocr_items=sync.get("ocr_items", 0),
                    confidence=sync.get("confidence", "uncertain"),
                    action_state=ACTION_UNVERIFIED,
                    execution_status="not_executed",
                    verification_status="unverified",
                    target_identified=False,
                    page_state="unverified",
                )
            ordered_vids, rows_v, ambig_v, sections_v = _cluster_youtube_video_rows(videos)
            cand_pool = ordered_vids
            if "horizontal" in desc_cf and rows_v:
                horiz_rows = [r for r in rows_v if len(r) >= 2]
                if not horiz_rows:
                    return outcome.unverified(
                        "browser_control",
                        "I do not see a multi-column horizontal video row in Chrome right now.",
                        action="play_video", browser=bname, task_id=_cur_task_id,
                        action_state=ACTION_UNVERIFIED, verified=False,
                    )
                primary_row = horiz_rows[0]
                if ordinal_idx != -1 and ordinal_idx >= len(primary_row):
                    return outcome.unverified(
                        "browser_control",
                        f"The first horizontal row only shows {len(primary_row)} visible videos; requested position is unavailable.",
                        action="play_video", browser=bname, task_id=_cur_task_id,
                        action_state=ACTION_UNVERIFIED, verified=False,
                    )
                cand_pool = primary_row
                ambig_v = False
            elif ambig_v and sections_v:
                return outcome.ask(
                    "browser_control",
                    f"I see multiple video sections on YouTube ({', '.join(sections_v[:3])}). Which section or video title should I click?",
                    browser=bname, task_id=_cur_task_id, action_state=ACTION_UNVERIFIED, verified=False,
                )
            idx = len(cand_pool) - 1 if ordinal_idx == -1 else ordinal_idx
            if idx < 0 or idx >= len(cand_pool):
                return f"I found {len(cand_pool)} eligible non-ad YouTube video results in {bname}; requested position is unavailable, so nothing was clicked."
            chosen = cand_pool[idx]
            ord_word = _INDEX_TO_ORDINAL.get(idx, f"#{idx + 1}")
            ev_stat = str(chosen.get("evidence_status") or "OBSERVED").upper()
            exact_title = str(chosen.get("exact_title") or chosen.get("title") or "").strip()
            if ev_stat in ("UNKNOWN", "INFERRED") or not exact_title or exact_title == "Unreadable video card":
                if _dc is not None:
                    try:
                        _dc.clear_task_targets()
                    except Exception:
                        pass
                try:
                    from core import screen_observation as _so
                    _so.invalidate_observation_provenance(reason="unreadable_conflicting_card_title", hwnd=int(win.get("hwnd", 0) or 0))
                except Exception:
                    pass
                return outcome.unverified(
                    "browser_control",
                    f"I can see the video cards, but I cannot reliably read the {ord_word} video's title.",
                    action="play_video", browser=bname, task_id=_cur_task_id,
                    control_mode="native_windows", cdp_available=False,
                    observation_id=chosen.get("observation_id") or sync.get("observation_id", ""),
                    evidence_status=ev_stat or "UNKNOWN",
                    target_identified=False, playback_verified=False, verified=False,
                )
            cx, cy = chosen["point"]
            v_rect = getattr(sync.get("view"), "window_rect", None)
            if isinstance(v_rect, (list, tuple)) and len(v_rect) == 4 and int(v_rect[2] or 0) > 100:
                wl, wt, ww, wh = (int(x or 0) for x in v_rect)
                if not (wl <= int(cx) <= wl + ww and (wt + 40) <= int(cy) <= wt + wh):
                    return outcome.failed(
                        "browser_control",
                        f"Target coordinates ({cx},{cy}) for {chosen['title']!r} fell outside the visible Chrome window; refused to click.",
                        action="play_video", browser=bname, task_id=_cur_task_id,
                        action_state=ACTION_FAILED, failure_reason="coordinates_outside_window",
                    )
            if _dc is not None:
                _dc.set_active_target(
                    target_type="video", exact_target_name=chosen["title"],
                    browser=bname, tab_title=sync.get("title", ""), tab_url=sync.get("url", ""),
                    result_position=chosen["position"], point=chosen["point"],
                    rect=chosen.get("rect"), confidence=chosen.get("confidence", "medium"),
                    extraction_source=chosen.get("source", ""), results=cand_pool, auth_context=_auth_ctx,
                )
            pt = screenact.Point(cx, cy, origin="element", label=chosen["title"])
            mv, clk, cdiag = _click_diag(action, pt, rect_info=chosen.get("rect"))
            if not clk.ok:
                if _dc is not None:
                    try:
                        _dc.clear_task_targets()
                    except Exception:
                        pass
                try:
                    from core import screen_observation as _so
                    _so.invalidate_observation_provenance(reason="play_video_click_failed", hwnd=int(win.get("hwnd", 0) or 0))
                except Exception:
                    pass
                _log_browser_action_diag(
                    task_id=_cur_task_id, browser_hwnd=int(win.get("hwnd", 0) or 0),
                    browser_pid=int(win.get("pid", 0) or 0), requested_site=site_hint or "youtube",
                    verified_site=str(sync.get("site") or ""), active_tab=sync.get("title", ""),
                    action="play_video", selected_target=chosen["title"],
                    selection_method=str(chosen.get("source") or "native").upper(),
                    target_bbox=chosen.get("rect"), verification_result=ACTION_FAILED,
                    failure_reason=str(clk.error or "click_failed"),
                    elapsed_ms=(time.monotonic() - t_vid0) * 1000.0,
                )
                return outcome.failed(
                    "browser_control", f"real click on {chosen['title']!r} failed: {clk.error}",
                    action="play_video", resolved_target=chosen["title"], browser=bname, task_id=_cur_task_id,
                    action_state=ACTION_FAILED, execution_status="failed", verification_status="unverified", **cdiag,
                )
            _registry.invalidate_sync(bname)
            try:
                from core import screen_observation as _so
                _so.invalidate_observation_provenance(reason="play_video_clicked", hwnd=int(win.get("hwnd", 0) or 0))
            except Exception:
                pass
            if _dc is not None:
                try:
                    _dc.clear_task_targets()
                except Exception:
                    pass
            after = _inspect_visible_browser_window(bname, focus_if_needed=False, allow_ocr=False)
            title_changed = bool(after.get("title") and after.get("title") != sync.get("title"))
            nav_verified = title_changed or ("/watch?v=" in str(after.get("url") or "").casefold())
            pb_state = _yt_native_state(after.get("view"), "playback") if nav_verified else None
            if pb_state == "paused":
                input_focused = any(
                    getattr(el, "focused", False) and str(getattr(el, "role", "") or "").casefold() in {"edit", "combobox", "searchbox"}
                    for el in (getattr(after.get("view"), "elements", ()) or ())
                )
                if not input_focused and screenact.press("k").ok:
                    time.sleep(0.25)
                    after = _inspect_visible_browser_window(bname, focus_if_needed=False, allow_ocr=False)
                    pb_state = _yt_native_state(after.get("view"), "playback")
            pb_verified = (pb_state == "playing")
            _log_browser_action_diag(
                task_id=_cur_task_id, browser_hwnd=int(after.get("hwnd", 0) or win.get("hwnd", 0) or 0),
                browser_pid=int(after.get("pid", 0) or win.get("pid", 0) or 0),
                requested_site=site_hint or "youtube",
                verified_site=str(after.get("site") or ""), active_tab=after.get("title", ""),
                action="play_video", selected_target=chosen["title"],
                selection_method=str(chosen.get("source") or "native").upper(),
                target_bbox=chosen.get("rect"),
                verification_result=(ACTION_VERIFIED if nav_verified else ACTION_UNVERIFIED),
                failure_reason=("" if nav_verified else "navigation_not_confirmed"),
                elapsed_ms=(time.monotonic() - t_vid0) * 1000.0,
            )
            if nav_verified:
                return outcome.done(
                    "browser_control",
                    action="play_video",
                    browser=bname, task_id=_cur_task_id,
                    control_mode="native_windows",
                    resolved_target=chosen["title"], target=chosen["title"],
                    action_state=ACTION_VERIFIED,
                    execution_status="completed", verification_status="verified",
                    cdp_available=False,
                    position=chosen["position"],
                    title=chosen["title"],
                    source=chosen["source"],
                    selection_method=str(chosen.get("source") or "native").upper(),
                    cursor_moved=mv.ok,
                    click_executed=clk.ok,
                    playback_verified=pb_verified,
                    resulting_title=after.get("title", ""),
                    detail=f"The {chosen['position']} video is {chosen['title']!r}. Clicked it in {bname}.",
                    verified=True, **cdiag,
                )
            return outcome.unverified(
                "browser_control",
                "I sent the click, Tarun-kun, but I couldn't confirm that the video opened.",
                action="play_video", browser=bname, task_id=_cur_task_id,
                control_mode="native_windows", cdp_available=False,
                resolved_target=chosen["title"], target=chosen["title"],
                action_state=ACTION_UNVERIFIED,
                execution_status="completed", verification_status="unverified",
                position=chosen["position"], title=chosen["title"],
                source=chosen["source"], cursor_moved=mv.ok, click_executed=clk.ok,
                playback_verified=False, verified=False, **cdiag,
            )
        # Pronoun references with no fresh resolved target ask cleanly instead of guessing
        if desc.casefold() in {"it", "that", "this", "that video", "this video", "open it", "click it", "click that", "second one", "the second one"}:
            return outcome.ask(
                "browser_control",
                "I couldn't match a fresh visible target for that reference; which video or control should I click, Tarun-kun?",
                browser=bname, task_id=_cur_task_id, verified=False,
            )
        # Non-ordinal named click / category chip / title search via UIA/OCR in visible Chrome window
        view = sync.get("view")
        yt_chips = sync.get("yt_chips") or []
        if view is not None and desc:
            needle = desc.casefold()
            chip_clean = re.sub(
                r"\b(?:click|on|the|category|chip|filter|pill|tab|button|select)\b",
                "", desc, flags=re.IGNORECASE,
            ).strip().casefold()
            if yt_chips and chip_clean and "video" not in desc_cf:
                chip_hit = next(
                    (c for c in yt_chips if str(c.get("label") or "").strip().casefold() == chip_clean),
                    None,
                )
                if chip_hit is not None:
                    t_ch0 = time.monotonic()
                    cx, cy = chip_hit["point"]
                    pt = screenact.Point(cx, cy, origin="element", label=chip_hit["label"])
                    mv, clk, cdiag = _click_diag(action, pt, rect_info=chip_hit.get("rect"))
                    _registry.invalidate_sync(bname)
                    if not clk.ok:
                        if _dc is not None:
                            _dc.clear_task_targets()
                        _log_browser_action_diag(
                            task_id=_cur_task_id,
                            intent=(getattr(_auth_ctx, "instruction", "") if _auth_ctx else desc),
                            parsed_target=chip_hit["label"],
                            browser_hwnd=int(win.get("hwnd", 0) or 0), browser_pid=int(win.get("pid", 0) or 0),
                            requested_site="youtube", verified_site=str(sync.get("site") or ""),
                            active_tab=sync.get("title", ""), action=action,
                            target_class=ELEM_CATEGORY_CHIP, selected_target=chip_hit["label"],
                            selection_method=str(chip_hit.get("source") or "native").upper(),
                            target_bbox=chip_hit.get("rect"), click_method="os_mouse",
                            fg_hwnd_before=cdiag.get("fg_hwnd_before", 0),
                            fg_hwnd_after=cdiag.get("fg_hwnd_after", 0),
                            verification_result=ACTION_FAILED, failure_reason=str(clk.error or "click_failed"),
                            elapsed_ms=(time.monotonic() - t_ch0) * 1000.0,
                        )
                        return outcome.failed(
                            "browser_control", f"clicking category chip {chip_hit['label']!r} failed: {clk.error}",
                            action=action, browser=bname, task_id=_cur_task_id,
                            target_class=ELEM_CATEGORY_CHIP, resolved_target=chip_hit["label"],
                            action_state=ACTION_FAILED, verified=False, **cdiag,
                        )
                    time.sleep(0.4)
                    after = _inspect_visible_browser_window(bname, focus_if_needed=False, allow_ocr=True)
                    after_chips = after.get("yt_chips") or []
                    after_chip = next(
                        (c for c in after_chips if str(c.get("label") or "").strip().casefold() == chip_clean),
                        None,
                    )
                    before_vids = [v.get("title") for v in (videos or [])[:4]]
                    after_vids = [v.get("title") for v in (after.get("videos") or [])[:4]]
                    chip_verified = bool(
                        (after_chip and after_chip.get("selected"))
                        or (after_vids and after_vids != before_vids)
                        or (after.get("title") and after.get("title") != sync.get("title"))
                    )
                    _log_browser_action_diag(
                        task_id=_cur_task_id,
                        intent=(getattr(_auth_ctx, "instruction", "") if _auth_ctx else desc),
                        parsed_target=chip_hit["label"],
                        browser_hwnd=int(after.get("hwnd", 0) or win.get("hwnd", 0) or 0),
                        browser_pid=int(after.get("pid", 0) or win.get("pid", 0) or 0),
                        requested_site="youtube", verified_site=str(after.get("site") or ""),
                        active_tab=after.get("title", ""), action=action,
                        target_class=ELEM_CATEGORY_CHIP, selected_target=chip_hit["label"],
                        selection_method=str(chip_hit.get("source") or "native").upper(),
                        target_bbox=chip_hit.get("rect"), click_method="os_mouse",
                        fg_hwnd_before=cdiag.get("fg_hwnd_before", 0),
                        fg_hwnd_after=cdiag.get("fg_hwnd_after", 0),
                        verification_result=(ACTION_VERIFIED if chip_verified else ACTION_UNVERIFIED),
                        failure_reason=("" if chip_verified else "category_chip_selection_unconfirmed"),
                        elapsed_ms=(time.monotonic() - t_ch0) * 1000.0,
                    )
                    if not chip_verified:
                        if _dc is not None:
                            _dc.clear_task_targets()
                        return outcome.unverified(
                            "browser_control",
                            f"I clicked the {chip_hit['label']!r} category chip, Tarun-kun, but couldn't confirm it became active.",
                            action=action, browser=bname, task_id=_cur_task_id,
                            target_class=ELEM_CATEGORY_CHIP, resolved_target=chip_hit["label"],
                            action_state=ACTION_UNVERIFIED, click_executed=True, cursor_moved=mv.ok,
                            verified=False, **cdiag,
                        )
                    return outcome.done(
                        "browser_control", action=action, browser=bname, task_id=_cur_task_id,
                        control_mode="native_windows", cdp_available=False,
                        target_class=ELEM_CATEGORY_CHIP, resolved_target=chip_hit["label"],
                        target=chip_hit["label"], chip_selected=True,
                        action_state=ACTION_VERIFIED, verification_status="verified",
                        cursor_moved=mv.ok, click_executed=True, verified=True, **cdiag,
                    )
            title_m = re.search(r"\b(?:video\s+)?titled\s+[\"']?(.+?)[\"']?$", desc, re.IGNORECASE)
            if title_m:
                needle = title_m.group(1).strip().casefold()
            for v in videos:
                if needle in v["title"].casefold():
                    if _dc is not None:
                        _dc.set_active_target(
                            target_type="video", exact_target_name=v["title"],
                            browser=bname, tab_title=sync.get("title", ""), tab_url=sync.get("url", ""),
                            result_position=v["position"], point=v["point"],
                            rect=v.get("rect"), confidence=v.get("confidence", "medium"),
                            extraction_source=v.get("source", ""), results=videos, auth_context=_auth_ctx,
                        )
                    pt = screenact.Point(v["point"][0], v["point"][1], origin="element", label=v["title"])
                    mv, clk, cdiag = _click_diag(action, pt, rect_info=v.get("rect"))
                    if not clk.ok:
                        if _dc is not None:
                            _dc.clear_task_targets()
                        _registry.invalidate_sync(bname)
                        return outcome.failed(
                            "browser_control", f"real click on {v['title']!r} failed: {clk.error}",
                            action=action, resolved_target=v["title"], browser=bname, task_id=_cur_task_id,
                            target_class=ELEM_VIDEO_CARD, action_state=ACTION_FAILED,
                            execution_status="failed", verification_status="unverified", **cdiag,
                        )
                    time.sleep(0.75)
                    _registry.invalidate_sync(bname)
                    after = _inspect_visible_browser_window(bname, focus_if_needed=False, allow_ocr=False)
                    title_changed = bool(after.get("title") and after.get("title") != sync.get("title"))
                    if title_changed:
                        return outcome.done(
                            "browser_control", action=action, browser=bname, task_id=_cur_task_id,
                            control_mode="native_windows", cdp_available=False,
                            resolved_target=v["title"], target=v["title"],
                            target_class=ELEM_VIDEO_CARD, action_state=ACTION_VERIFIED,
                            execution_status="completed", verification_status="verified",
                            title=v["title"], cursor_moved=mv.ok, click_executed=clk.ok,
                            playback_verified=False, resulting_title=after.get("title", ""), verified=True, **cdiag,
                        )
                    if _dc is not None:
                        _dc.clear_task_targets()
                    return outcome.unverified(
                        "browser_control",
                        "I sent the click, Tarun-kun, but I couldn't confirm that the video opened.",
                        action=action, browser=bname, task_id=_cur_task_id,
                        control_mode="native_windows", cdp_available=False,
                        resolved_target=v["title"], target=v["title"],
                        target_class=ELEM_VIDEO_CARD, action_state=ACTION_UNVERIFIED,
                        execution_status="completed", verification_status="unverified",
                        title=v["title"], cursor_moved=mv.ok, click_executed=clk.ok,
                        playback_verified=False, verified=False, **cdiag,
                    )
            for el in getattr(view, "elements", ()) or ():
                label = str(getattr(el, "name", "") or getattr(el, "label", "") or "")
                if label and needle in label.casefold():
                    pt = screenact.Point.from_element(el)
                    mv, clk, cdiag = _click_diag(action, pt, rect_info=getattr(el, "rect", ""))
                    _registry.invalidate_sync(bname)
                    if not clk.ok:
                        if _dc is not None:
                            _dc.clear_task_targets()
                        return outcome.failed(
                            "browser_control", f"real click on {label!r} failed: {clk.error}",
                            action=action, browser=bname, task_id=_cur_task_id,
                            resolved_target=label, target=label,
                            action_state=ACTION_FAILED, execution_status="failed",
                            verification_status="unverified", click_executed=False,
                            cursor_moved=mv.ok, verified=False, **cdiag,
                        )
                    time.sleep(0.3)
                    after = _inspect_visible_browser_window(bname, focus_if_needed=False, allow_ocr=False)
                    changed = bool(
                        (after.get("title") and after.get("title") != sync.get("title"))
                        or (after.get("url") and after.get("url") != sync.get("url"))
                    )
                    if not changed:
                        if _dc is not None:
                            _dc.clear_task_targets()
                        return outcome.unverified(
                            "browser_control",
                            f"I clicked {label!r}, Tarun-kun, but I couldn't confirm the page changed.",
                            action=action, browser=bname, task_id=_cur_task_id,
                            control_mode="native_windows", cdp_available=False,
                            resolved_target=label, target=label,
                            action_state=ACTION_UNVERIFIED, execution_status="completed",
                            verification_status="unverified", click_executed=True,
                            cursor_moved=mv.ok, verified=False, **cdiag,
                        )
                    return outcome.done(
                        "browser_control", action=action, browser=bname, task_id=_cur_task_id,
                        control_mode="native_windows", cdp_available=False,
                        resolved_target=label, target=label,
                        action_state=ACTION_VERIFIED,
                        execution_status="completed", verification_status="verified",
                        click_executed=True, cursor_moved=mv.ok, verified=True, **cdiag,
                    )
        if _dc is not None:
            try:
                _dc.clear_task_targets()
            except Exception:
                pass
        _registry.invalidate_sync(bname)
        _log_browser_action_diag(
            task_id=_cur_task_id,
            intent=(getattr(_auth_ctx, "instruction", "") if _auth_ctx else desc),
            parsed_target=desc,
            browser_hwnd=int(win.get("hwnd", 0) or 0), browser_pid=int(win.get("pid", 0) or 0),
            requested_site=site_hint or str(sync.get("site") or ""),
            verified_site=str(sync.get("site") or ""), active_tab=sync.get("title", ""),
            action=action, selected_target=desc,
            selection_method=str(sync.get("source") or "none").upper(),
            verification_result=ACTION_FAILED, failure_reason="target_element_not_found",
        )
        return outcome.failed("browser_control", f"Could not find element {desc!r} in the active {bname} window", action=action, browser=bname, task_id=_cur_task_id, action_state=ACTION_FAILED, execution_status="failed", verification_status="unverified", cdp_available=False)

    if action == "focus_search":
        sync = _inspect_visible_browser_window(bname, focus_if_needed=True, allow_ocr=True)
        sb = _locate_youtube_search_bar_native(sync, bname, str(params.get("description") or "YouTube search bar"))
        if sb is None:
            return outcome.failed(
                "browser_control",
                f"Could not locate the YouTube search bar in the active {bname} window",
                action="focus_search", browser=bname, dispatch_status="completed",
                input_status="failed", verification_status="unverified", cdp_available=False,
            )
        pt = screenact.Point(sb["x"], sb["y"], origin="element", label=sb["label"])
        mv = screenact.move_cursor(pt)
        clk = screenact.click(pt)
        if not clk.ok:
            return outcome.failed(
                "browser_control", f"clicking YouTube search bar failed: {clk.error}",
                action="focus_search", browser=bname, resolved_target=sb["label"],
                dispatch_status="completed", input_status="failed", verification_status="unverified",
            )
        return outcome.done(
            "browser_control", action="focus_search", browser=bname,
            control_mode="native_windows", cdp_available=False,
            resolved_target=sb["label"], target=sb["label"], detection_source=sb["source"],
            dispatch_status="completed", input_status="completed",
            verification_status=("verified" if mv.ok and clk.ok else "unverified"),
            cursor_position=f"{sb['x']},{sb['y']}", cursor_moved=mv.ok, click_executed=clk.ok,
            detail="The YouTube search field is focused. What should I search for?",
            verified=bool(mv.ok and clk.ok),
        )

    if action in ("seek_forward", "seek_backward", "set_playback_position"):
        from core import desktop_control as _dc
        forward = (action != "seek_backward")
        raw_val = params.get("seconds") or params.get("value") or params.get("position") or params.get("text")
        secs = 10
        if raw_val is not None:
            m_sec = re.search(r"-?\d+", str(raw_val))
            if m_sec:
                secs = max(1, min(600, abs(int(m_sec.group(0)))))
        before_sync = _inspect_visible_browser_window(bname, focus_if_needed=False, allow_ocr=False)
        before_pos = _extract_yt_time_seconds(before_sync.get("view"))
        # If a search/edit input currently owns focus, press Escape first so player keys reach <video>
        for el in (getattr(before_sync.get("view"), "elements", ()) or ()):
            if getattr(el, "focused", False) and str(getattr(el, "role", "") or "").casefold() in {"edit", "combobox", "searchbox"}:
                screenact.press("escape")
                time.sleep(0.08)
                break
        ten_chunks = secs // 10
        rem = secs % 10
        five_chunks = max(1 if ten_chunks == 0 else 0, round(rem / 5))
        key_big = "l" if forward else "j"
        key_small = "right" if forward else "left"
        sent_ok = True
        for _ in range(ten_chunks):
            r_k = screenact.press(key_big)
            sent_ok = sent_ok and bool(r_k.ok)
            time.sleep(0.05)
        for _ in range(five_chunks):
            r_k = screenact.press(key_small)
            sent_ok = sent_ok and bool(r_k.ok)
            time.sleep(0.05)
        if not sent_ok:
            return outcome.failed(
                "browser_control", f"seek shortcut failed in {bname}",
                action=action, browser=bname, dispatch_status="completed",
                input_status="failed", verification_status="unverified",
            )
        time.sleep(0.25)
        after_sync = _inspect_visible_browser_window(bname, focus_if_needed=False, allow_ocr=False)
        after_pos = _extract_yt_time_seconds(after_sync.get("view"))
        _dc.record_diagnostics(last_media_status=f"{action} {'+' if forward else '-'}{secs}s")
        pos_changed = (
            before_pos is not None and after_pos is not None
            and ((after_pos > before_pos) if forward else (after_pos < before_pos))
        )
        if pos_changed:
            return outcome.done(
                "browser_control", action=action, browser=bname, control_mode="native_windows",
                resolved_target=win.get("title", "YouTube video"),
                dispatch_status="completed", input_status="completed", verification_status="verified",
                seconds=secs, playback_before=before_pos, playback_after=after_pos,
                detail=f"Skipped {'forward' if forward else 'backward'} {secs} seconds, Tarun-kun.",
                verified=True,
            )
        return outcome.unverified(
            "browser_control",
            f"I sent the {secs}-second {'forward' if forward else 'backward'} seek, Tarun-kun, but couldn't confirm the exact timestamp without DOM access.",
            action=action, browser=bname, control_mode="native_windows",
            resolved_target=win.get("title", "YouTube video"),
            dispatch_status="completed", input_status="completed", verification_status="unverified",
            seconds=secs, playback_before=before_pos, playback_after=after_pos, verified=False,
        )

    if action in ("mute", "unmute"):
        return _native_toggle_with_verification(bname, "mute", "muted" if action == "mute" else "unmuted", "m", action, win)

    if action in ("fullscreen", "exit_fullscreen"):
        return _native_toggle_with_verification(
            bname, "fullscreen", "fullscreen" if action == "fullscreen" else "windowed",
            "f" if action == "fullscreen" else "escape", action, win)

    if action in ("volume_up", "volume_down"):
        from core import desktop_control as _dc
        r_v = screenact.press("up" if action == "volume_up" else "down")
        _dc.record_diagnostics(last_media_status=f"{action}:{'ok' if r_v.ok else 'fail'}")
        if not r_v.ok:
            return outcome.failed("browser_control", f"{action} failed in {bname}: {r_v.error}", action=action, browser=bname)
        return outcome.unverified(
            "browser_control", f"Sent {action} key; the level cannot be read without DOM access.",
            action=action, browser=bname, control_mode="native_windows",
            dispatch_status="completed", input_status="completed", verification_status="unverified")

    if action == "set_volume":
        return outcome.failed(
            "browser_control",
            "setting an exact player volume needs DOM access; start the browser with remote debugging enabled",
            action=action, browser=bname, control_mode="native_windows")

    if action in ("pause_video", "resume_video"):
        return _native_toggle_with_verification(
            bname, "playback", "paused" if action == "pause_video" else "playing", "k", action, win)

    if action == "screenshot":
        sync = _inspect_visible_browser_window(bname, focus_if_needed=True, allow_ocr=False)
        return outcome.done(
            "browser_control", action="screenshot", browser=bname,
            control_mode="native_windows", window_identity=sync.get("window_identity", ""),
            target_hwnd=int(win.get("hwnd", 0) or 0), target_pid=int(win.get("pid", 0) or 0),
            dispatch_status="completed", input_status="completed", verification_status="verified",
            verified=bool(win.get("foreground")),
        )

    if action == "scroll":
        from core import desktop_control as _dc
        t_scr0 = time.monotonic()
        direction = str(params.get("direction") or "down").lower().strip()
        if direction not in ("up", "down", "left", "right"):
            direction = "down"
        raw_amt = int(params.get("amount", 4) or 4)
        if raw_amt > 20:
            notches = max(1, min(12, raw_amt // 100))
        else:
            notches = max(1, min(15, raw_amt))
        res = screenact.scroll(direction, amount=notches)
        _dc.record_diagnostics(last_mouse_status=f"scroll:{direction}:{'ok' if res.ok else 'fail'}")
        if not res.ok:
            _log_browser_action_diag(
                browser_hwnd=int(win.get("hwnd", 0) or 0), browser_pid=int(win.get("pid", 0) or 0),
                requested_site=site_hint or str(win.get("matched_site") or ""),
                active_tab=str(win.get("title") or ""), action="scroll",
                selected_target=direction, selection_method="OS_INPUT",
                verification_result=ACTION_FAILED, failure_reason=str(res.error or "scroll_failed"),
                elapsed_ms=(time.monotonic() - t_scr0) * 1000.0,
            )
            return outcome.failed(
                "browser_control", f"scroll failed in {bname}: {res.error}",
                action="scroll", browser=bname, target_hwnd=int(win.get("hwnd", 0) or 0),
                target_pid=int(win.get("pid", 0) or 0),
                action_state=ACTION_FAILED,
                dispatch_status="completed", input_status="failed", verification_status="unverified",
            )
        _registry.invalidate_sync(bname)
        try:
            from core import screen_observation as _so
            _so.invalidate_observation_provenance(reason="scroll", hwnd=int(win.get("hwnd", 0) or 0))
        except Exception:
            pass
        try:
            _dc.clear_task_targets()
        except Exception:
            pass
        _log_browser_action_diag(
            browser_hwnd=int(win.get("hwnd", 0) or 0), browser_pid=int(win.get("pid", 0) or 0),
            requested_site=site_hint or str(win.get("matched_site") or ""),
            verified_site=str(win.get("matched_site") or site_hint or ""),
            active_tab=str(win.get("title") or ""), action="scroll",
            selected_target=direction, selection_method="OS_INPUT",
            verification_result=ACTION_VERIFIED,
            elapsed_ms=(time.monotonic() - t_scr0) * 1000.0,
        )
        return outcome.done(
            "browser_control", action="scroll", browser=bname, control_mode="native_windows",
            cdp_available=False, direction=direction, amount=notches,
            target_hwnd=int(win.get("hwnd", 0) or 0), target_pid=int(win.get("pid", 0) or 0),
            foreground_hwnd=int(win.get("hwnd", 0) or 0),
            action_state=ACTION_VERIFIED,
            dispatch_status="completed", input_status="completed", verification_status="verified",
            verified=True,
        )

    if action in ("back", "forward", "reload", "press", "close_tab"):
        keymap = {"back": "alt+left", "forward": "alt+right", "reload": "ctrl+r", "close_tab": "ctrl+w"}
        key = str(params.get("key") or "enter") if action == "press" else keymap[action]
        is_wa_win = (site_hint == "whatsapp") or ("whatsapp" in (win.get("title", "") + " " + str(params.get("description") or "")).casefold())
        if action == "press" and key.strip().casefold() in {"enter", "return"} and is_wa_win:
            from core import desktop_control as _dc
            from plugins import _whatsapp_core as _wa
            _auth_ctx = _dc.current_bound_auth_context()
            pre_sync = _inspect_visible_browser_window(bname, focus_if_needed=False, allow_ocr=True, site_hint="whatsapp")
            pre_view = pre_sync.get("view")
            comp_state = _dc.get_active_composition_state(_auth_ctx) or {}
            rb_pre = _wa.read_composer(pre_view)
            sb_pre = _wa.resolve_whatsapp_search_field(pre_view)
            # Only bypass send confirmation if the WhatsApp SEARCH field explicitly owns focus
            search_focused = bool(sb_pre.get("found") and sb_pre.get("focus_verified") and rb_pre.get("status") != "has_text")
            if not search_focused:
                draft_txt = str(comp_state.get("generated_content") or rb_pre.get("text") or "").strip()
                expected_c = str(comp_state.get("selected_contact") or _dc.snapshot().get("active_contact") or "").strip()
                if expected_c == "—":
                    expected_c = ""
                dhash = _wa.draft_hash(draft_txt) if draft_txt else str(comp_state.get("generated_content_hash") or "")
                ok_send, deny_reason, _conf = _dc.is_send_authorized(
                    receiver=expected_c,
                    draft_hash=dhash,
                    composition_task_id=str(comp_state.get("composition_task_id") or ""),
                    utterance=str(getattr(_auth_ctx, "instruction", "") if _auth_ctx is not None else ""),
                    auth_context=_auth_ctx,
                )
                if not ok_send:
                    return outcome.failed(
                        "browser_control",
                        deny_reason or "Sending a WhatsApp message requires clear confirmation ('Yes, send it').",
                        action="press", browser=bname, message_sent=False, verified=False,
                    )
                if not draft_txt:
                    return outcome.failed(
                        "browser_control",
                        "No verified WhatsApp draft is present to send.",
                        action="press", browser=bname, message_sent=False, verified=False,
                    )
                _dc.transition_message_lifecycle(
                    _dc.MSG_SEND_ATTEMPTED,
                    receiver=expected_c, draft_hash=dhash, reason="whatsapp_enter_pressed", auth_context=_auth_ctx,
                )
                _dc.record_sent_attempt(receiver=expected_c, draft_hash=dhash, task_id=str(comp_state.get("composition_task_id") or ""))
                res = screenact.press(key)
                if not res.ok:
                    _dc.transition_message_lifecycle(
                        _dc.MSG_SEND_UNVERIFIED,
                        receiver=expected_c, draft_hash=dhash, reason=f"enter_failed:{res.error}", auth_context=_auth_ctx,
                    )
                    return outcome.failed("browser_control", f"press failed in {bname}: {res.error}", browser=bname, message_sent=False)
                time.sleep(0.35)
                _registry.invalidate_sync(bname)
                after_sync = _inspect_visible_browser_window(bname, focus_if_needed=False, allow_ocr=True, site_hint="whatsapp")
                det = _wa.verify_outgoing_message_detailed(
                    after_sync.get("view"),
                    draft_txt,
                    before_view=pre_view,
                    expected_contact=expected_c,
                    tab_title=after_sync.get("title", ""),
                    tab_url=after_sync.get("url", ""),
                )
                if det.get("verified"):
                    _dc.mark_composition_sent(receiver=expected_c, draft_hash=dhash)
                    return outcome.done(
                        "browser_control", action="press", key=key, browser=bname,
                        control_mode="native_windows", cdp_available=False,
                        lifecycle_state=_dc.MSG_SEND_VERIFIED,
                        bubble_verified=True,
                        composer_cleared=bool(det.get("composer_cleared")),
                        message_sent=True, verified=True,
                        title=after_sync.get("title", ""),
                    )
                _dc.transition_message_lifecycle(
                    _dc.MSG_SEND_UNVERIFIED,
                    receiver=expected_c, draft_hash=dhash, reason=str(det.get("reason") or "outgoing_bubble_not_confirmed"), auth_context=_auth_ctx,
                )
                return outcome.unverified(
                    "browser_control",
                    "I attempted to send the message, but WhatsApp has not confirmed that it was sent.",
                    action="press", key=key, browser=bname,
                    control_mode="native_windows", cdp_available=False,
                    lifecycle_state=_dc.MSG_SEND_UNVERIFIED,
                    bubble_verified=False,
                    composer_cleared=bool(det.get("composer_cleared")),
                    message_sent=False, verified=False,
                )
        res = screenact.press(key)
        if not res.ok:
            return outcome.failed("browser_control", f"{action} failed in {bname}: {res.error}", browser=bname)
        if action == "reload":
            time.sleep(0.35)
            _registry.invalidate_sync(bname)
            post_sync = _inspect_visible_browser_window(bname, focus_if_needed=False, allow_ocr=False, site_hint=site_hint)
            post_title = str(post_sync.get("title") or win.get("title") or "")
            post_url = str(post_sync.get("url") or "")
            ident = verify_site_page_identity(site_hint, url=post_url, title=post_title)
            if ident.get("page_state") == "error":
                return outcome.failed(
                    "browser_control", f"page error after reloading ({post_title})",
                    action="reload", browser=bname, title=post_title, page_state="error",
                    foreground_verified=bool(win.get("foreground")),
                )
            if ident.get("page_state") == "loading":
                return outcome.unverified(
                    "browser_control", "reload triggered, and the page is still loading",
                    action="reload", browser=bname, title=post_title, page_state="loading",
                    foreground_verified=bool(win.get("foreground")),
                    target_matched=bool(ident.get("target_matched")),
                )
            if site_hint and not ident.get("verified"):
                return outcome.unverified(
                    "browser_control", ident.get("reason") or "reload finished, but target site identity was not verified",
                    action="reload", browser=bname, title=post_title,
                    page_state=ident.get("page_state") or "wrong_tab",
                    foreground_verified=bool(win.get("foreground")), target_matched=False,
                )
            return outcome.done(
                "browser_control", action="reload", browser=bname,
                control_mode="native_windows", cdp_available=False,
                title=post_title, page_state=ident.get("page_state") or "loaded",
                foreground_verified=True, target_matched=bool(not site_hint or ident.get("verified")),
                verified=True,
            )
        return outcome.done("browser_control", action=action, browser=bname, control_mode="native_windows", cdp_available=False, title=win.get("title", ""), verified=True)

    return _unknown_action(action)


def _is_search_bar_query(desc: str) -> bool:
    cf = str(desc or "").strip().casefold()
    if any(k in cf for k in ("composer", "type a message", "message box", "reply box", "send message")):
        return False
    return cf in {
        "search", "search bar", "search box", "search field", "searchbox",
        "youtube search", "youtube search bar", "youtube search box",
        "youtube search field", "youtube search input",
        "whatsapp search", "whatsapp search bar", "whatsapp search box",
        "whatsapp search field", "whatsapp sidebar search", "sidebar search",
        "chat list search", "search chats", "search or start", "new chat", "new chat search",
    } or (
        "search" in cf
        and any(k in cf for k in ("bar", "box", "field", "input", "youtube", "whatsapp", "sidebar", "chat"))
    )


def _detect_youtube_masthead_search_box(monitor: dict | None = None) -> dict | None:
    """Detect the centered rounded-pill YouTube masthead search box via OpenCV contours.

    Uses DPI scaling ``px`` from ``screenview.capture`` so physical pixels translate
    accurately to logical desktop cursor coordinates across multi-monitor setups.
    """
    try:
        import cv2
        import numpy as np
        from core import screenview

        image, mon, px = screenview.capture(monitor)
        if image is None or mon is None:
            return None
        arr = np.array(image)
        if arr.ndim < 2 or arr.shape[0] < 240 or arr.shape[1] < 400:
            return None
        img_h, img_w = int(arr.shape[0]), int(arr.shape[1])
        px = float(px or 1.0)
        # Band strictly below Chrome tabs/omnibox (~80..210 logical px) and central masthead
        y0 = max(0, min(img_h - 40, int(round(78 * px))))
        y1 = max(y0 + 40, min(img_h, int(round(210 * px))))
        x0 = max(0, int(round(img_w * 0.18)))
        x1 = min(img_w, int(round(img_w * 0.82)))
        crop = arr[y0:y1, x0:x1]
        if crop.size == 0:
            return None
        gray = cv2.cvtColor(crop, cv2.COLOR_RGB2GRAY) if crop.ndim == 3 else crop
        edges = cv2.Canny(gray, 18, 70)
        kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (5, 3))
        closed = cv2.dilate(edges, kernel, iterations=1)
        contours, _ = cv2.findContours(closed, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        best = None
        best_score = -1.0
        min_w = int(round(180 * px))
        max_w = int(round(img_w * 0.58))
        min_h = int(round(24 * px))
        max_h = int(round(62 * px))
        band_mid_x = (x1 - x0) / 2.0
        for cnt in contours:
            bx, by, bw, bh = cv2.boundingRect(cnt)
            if bw < min_w or bw > max_w or bh < min_h or bh > max_h:
                continue
            aspect = bw / max(1.0, float(bh))
            if aspect < 3.5 or aspect > 28.0:
                continue
            cx_local = bx + bw / 2.0
            center_penalty = abs(cx_local - band_mid_x) / max(1.0, band_mid_x)
            if center_penalty > 0.45:
                continue
            score = bw * (1.2 - center_penalty)
            if score > best_score:
                best_score = score
                best = (x0 + bx, y0 + by, bw, bh)
        if best is None:
            return None
        bx, by, bw, bh = best
        # Aim into the text input region slightly left of the trailing magnifying glass button
        lx = int(round(mon["left"] + (bx + bw * 0.42) / px))
        ly = int(round(mon["top"] + (by + bh * 0.50) / px))
        return {
            "x": lx,
            "y": ly,
            "label": "YouTube search bar (visual)",
            "source": "visual_masthead_cv2",
        }
    except Exception:
        return None


def _locate_youtube_search_bar_native(sync: dict, bname: str, desc: str) -> dict | None:
    """Locate the page-level YouTube search input via UIA -> OCR -> '/' probe -> visual masthead.

    Explicitly excludes Chrome's address bar ("Address and search bar" / omnibox).
    """
    from core import screenact, screenview

    title_url_cf = (str(sync.get("title") or "") + " " + str(sync.get("url") or "")).casefold()
    v0 = sync.get("view")
    v0_elts = list(getattr(v0, "elements", ()) or ()) if v0 is not None else []
    is_youtube_surface = (
        "youtube" in title_url_cf
        or any("youtube" in str(getattr(e, "name", "") or "").casefold() for e in v0_elts)
    )
    if "youtube" in str(desc or "").casefold() and title_url_cf and not is_youtube_surface:
        return None

    def _search_in_view(view) -> dict | None:
        if view is None:
            return None
        mon = getattr(view, "monitor", None) or {}
        mon_top = int(mon.get("top", 0) or 0)
        toolbar_floor = mon_top + 85  # Below Chrome tabs + omnibox (~0..82px)
        header_ceil = mon_top + 240
        elements = list(getattr(view, "elements", ()) or ())
        # Pass 1: UIA combobox/edit/searchbox belonging to YouTube page (not Chrome omnibox)
        for el in elements:
            role_cf = str(getattr(el, "role", "") or "").casefold()
            name_cf = str(getattr(el, "name", "") or getattr(el, "label", "") or "").casefold()
            if "address" in name_cf or "omnibox" in name_cf or "url" in name_cf:
                continue
            top = int(getattr(el, "top", 0) or 0)
            width = int(getattr(el, "width", 0) or 0)
            if role_cf in {"edit", "combobox", "searchbox"} and top >= toolbar_floor and width >= 80:
                if "search" in name_cf or getattr(el, "focused", False):
                    cx, cy = getattr(el, "center", (int(el.left + el.width // 2), int(el.top + el.height // 2)))
                    return {"x": int(cx), "y": int(cy), "label": el.name or "YouTube search bar", "source": "uia"}
        # Pass 2: Local OCR "Search" inside YouTube page header band
        view_src_cf = str(getattr(view, "source", "") or "").casefold()
        for el in elements:
            role_cf = str(getattr(el, "role", "") or "").casefold()
            is_ocr = ("ocr" in role_cf) or (view_src_cf == "ocr" and role_cf in {"text", "ocr_text", ""})
            name_cf = str(getattr(el, "name", "") or getattr(el, "label", "") or "").strip().casefold()
            top = int(getattr(el, "top", 0) or 0)
            width = int(getattr(el, "width", 0) or 0)
            if is_ocr and toolbar_floor <= top <= header_ceil and name_cf in {"search", "search youtube"}:
                cx, cy = getattr(el, "center", (int(el.left + max(width, 120) // 2), int(el.top + max(el.height, 24) // 2)))
                return {"x": int(cx), "y": int(cy), "label": "YouTube search bar (OCR)", "source": "ocr"}
        return None

    def _record_sb(sb_dict: dict | None) -> dict | None:
        if sb_dict:
            try:
                from core import desktop_control as _dc
                _dc.set_active_target(
                    target_type="search_bar",
                    exact_target_name=str(sb_dict.get("label") or "YouTube search bar"),
                    browser=bname,
                    tab_title=str(sync.get("title") or ""),
                    tab_url=str(sync.get("url") or ""),
                    point=(int(sb_dict["x"]), int(sb_dict["y"])),
                    confidence=("high" if "uia" in str(sb_dict.get("source") or "") else "medium"),
                    extraction_source=str(sb_dict.get("source") or ""),
                    auth_context=_dc.current_bound_auth_context(),
                )
            except Exception:
                pass
        return sb_dict

    found = _search_in_view(sync.get("view"))
    if found:
        return _record_sb(found)
    # Pass 3: Focus reveal using YouTube's '/' search shortcut on the verified YouTube Chrome window
    if is_youtube_surface:
        try:
            screenact.press("/")
            time.sleep(0.18)
            fresh = screenview.look(budget_s=2.0, allow_ocr=True)
            found = _search_in_view(fresh)
            if found:
                found["source"] = f"{found['source']}_focus_probe"
                return _record_sb(found)
        except Exception:
            pass
    # Pass 4: Visual screenshot masthead pill detector (OpenCV contours, DPI-scaled)
    if is_youtube_surface:
        mon = getattr(v0, "monitor", None) if v0 is not None else None
        vis = _detect_youtube_masthead_search_box(monitor=mon)
        if vis:
            return _record_sb(vis)
    return None


def locate_control_screen_point(description: str, app_hint: str = "") -> dict | None:
    """Resolve desktop screen coordinates for a control in the active browser session."""
    app_cf = str(app_hint or "").casefold()
    browser = None
    for b in ("chrome", "edge", "firefox", "brave", "opera", "vivaldi"):
        if b in app_cf or (b == "edge" and "msedge" in app_cf):
            browser = b
            break
    if not _registry.has(browser):
        bname = browser or _registry._active_browser or "chrome"
        try:
            from core import desktop_control as _dc
            desc = str(description or "").strip()
            if desc.casefold() in {"it", "that", "this", "that video", "this video", "open it", "click it", "click that", "second one", "the second one", "that field", "that bar"}:
                ref = _dc.resolve_target_reference(desc, auth_context=_dc.current_bound_auth_context())
                if ref and ref.get("point"):
                    rx, ry = ref["point"]
                    return {"x": int(rx), "y": int(ry), "label": ref.get("exact_target_name") or desc, "source": ref.get("extraction_source") or "active_target"}
            sync = _inspect_visible_browser_window(bname, focus_if_needed=False, allow_ocr=True)
            if _is_search_bar_query(desc):
                sb = _locate_youtube_search_bar_native(sync, bname, desc)
                if sb is not None:
                    return sb
            ord_idx = _parse_ordinal_index(desc)
            videos = sync.get("videos") or []
            if ord_idx is not None and videos:
                idx = len(videos) - 1 if ord_idx == -1 else ord_idx
                if 0 <= idx < len(videos):
                    px, py = videos[idx]["point"]
                    return {"x": px, "y": py, "label": videos[idx]["title"]}
            view = sync.get("view")
            if view is not None and desc:
                needle = desc.casefold()
                for el in getattr(view, "elements", ()) or ():
                    name = str(getattr(el, "name", "") or getattr(el, "label", "") or "")
                    if name and needle in name.casefold():
                        pt = el.center
                        return {"x": int(pt[0]), "y": int(pt[1]), "label": name}
        except Exception:
            pass
        return None
    try:
        sess = _registry.get(browser)
        return sess.run(sess.locate_element_screen_rect(description), timeout=8)
    except Exception:
        return None


_ACTIONS = (
    "go_to", "search", "focus_search", "search_contact", "select_contact",
    "compose_message", "update_draft", "clear_field",
    "new_tab", "switch_tab", "close_tab", "inspect",
    "click", "left_click", "right_click", "double_click", "smart_click", "type", "smart_type", "fill_form", "press",
    "pause_video", "resume_video", "play_video", "replay_video",
    "seek_forward", "seek_backward", "set_playback_position",
    "volume_up", "volume_down", "mute", "unmute", "set_volume", "fullscreen", "exit_fullscreen",
    "scroll", "get_text", "get_url", "screenshot",
    "back", "forward", "reload",
    "switch", "list_browsers", "close", "close_all",
)


def _unknown_action(action: str) -> str:
    """Refuse an action this tool does not have, and say what it does have.

    THE OLD ANSWER WAS A SENTENCE, AND A SENTENCE IS NOT A FAILURE
        `f"Unknown browser action: '{action}'"` is a plain string. Nothing above
        this file could tell it apart from a success: it does not start with a
        status, so `outcome.is_problem` reads it as fine, the screen agent's
        loop counted the step as performed, and the assistant said the browser
        control function "couldn't understand the action" only because the model
        happened to read the English.

        Worse, it taught nothing. A caller told "unknown action 'open'" and
        nothing else has exactly one move available, which is to guess again.
        Observed doing precisely that: open, then "", then open.

    So this is a real failure with the valid list attached. A planner that
    reads it can correct itself on the next attempt instead of re-rolling.
    """
    return outcome.failed(
        "browser_control",
        f"there is no browser action called {action or '(empty)'!r}",
        valid_actions=", ".join(_ACTIONS))


def _browser_control_impl(
    parameters:    dict = None,
    response=None,
    player=None,
    session_memory=None,
    _approved_internal: bool = False,
) -> str:
    params  = parameters or {}
    action  = params.get("action", "").lower().strip()
    browser = params.get("browser", "").lower().strip() or None
    result  = _unknown_action(action)

    if browser:
        _registry.note_active_browser(browser)

    # A visible label/selector that implies a consequential click is never
    # trusted as permission by itself. The person approves it in the HUD.
    target_text = " ".join(str(params.get(k) or "") for k in
                           ("text", "description", "selector")).casefold()
    sensitive_terms = ("send", "submit", "delete", "remove", "purchase", "buy",
                       "place order", "pay", "transfer", "share", "install",
                       "allow access", "disable security", "login", "log in",
                       "sign in", "authenticate")
    needs_confirm = (
        (action in ("click", "smart_click")
         and any(term in target_text for term in sensitive_terms))
        or action in ("close", "close_all")
    )
    if not _approved_internal and needs_confirm:
        from core import confirm
        if confirm.pending_title():
            return "Another confirmation is already waiting. I have not clicked anything."
        label = (params.get("description") or params.get("text") or
                 params.get("selector") or "the requested control")
        return confirm.request(
            key="browser-consequential-click",
            title=f"Confirm browser action: {str(label)[:70]}?",
            detail="Review the current browser page before approving this action.",
            run=lambda: browser_control(params, player=player,
                                        _approved_internal=True),
        )

    if action == "switch":
        target = browser or params.get("target", "").lower().strip()
        result = _registry.switch(target) if target else "Please specify a browser."
        _log(player, result)
        return result

    if action == "list_browsers":
        result = _registry.list_sessions()
        _log(player, result)
        return result

    if action == "close_all":
        result = _registry.close_all()
        _log(player, result)
        return result

    if action == "close":
        target = browser or _registry._active_browser
        result = _registry.close_one(target) if target else "No browser specified."
        _log(player, result)
        return result

    # ── Navigation is ALWAYS native ──────────────────────────────────────────
    # go_to / search / new_tab open the site in the user's own browser —
    # their own profile, logged-in accounts and start page; exactly as if the
    # user had opened it themselves. A controlled window with about:blank never
    # opens here. The only exception: if an automation flow is already running,
    # navigation continues in that window (so multi-step tasks aren't split).
    if action in ("go_to", "search", "new_tab"):
        _registry.invalidate_sync(browser)
        if _registry.has(browser):
            sess = _registry.get(browser)
            try:
                if action == "search":
                    result = sess.run(sess.search(params.get("query", ""),
                                                  params.get("engine", "")))
                elif action == "new_tab":
                    result = sess.run(sess.new_tab(params.get("url", "")))
                else:
                    result = sess.run(sess.go_to(params.get("url", "")))
            except concurrent.futures.TimeoutError:
                result = f"Browser action '{action}' timed out (60s)."
            except Exception as e:
                result = f"Browser error ({action}): {e}"
            _log(player, result)
            return result

        if action == "search":
            eng = str(params.get("engine") or "").lower().strip()
            if not eng:
                tabs_hint = " ".join(inspect_open_chrome_tabs_local(select_browser(browser))).casefold()
                if "youtube" in tabs_hint or "youtube" in str(_registry._last_native_url or "").casefold():
                    eng = "youtube"
                else:
                    eng = "google"
            base    = _SEARCH_ENGINES.get(eng, _SEARCH_ENGINES["google"])
            nav_url = base + params.get("query", "").replace(" ", "+")
        else:
            nav_url = params.get("url", "").strip()
            if not nav_url:
                try:
                    from core import desktop_control as _dc_nav
                    _ctx_n = _dc_nav.current_bound_auth_context()
                    _inst_n = str(getattr(_ctx_n, "instruction", "") or "") if _ctx_n is not None else ""
                    _toks_n = set(re.findall(r"[a-z0-9]+", _inst_n.casefold()))
                    _noise = {"open", "launch", "start", "the", "a", "an", "in", "on", "now", "please", "browser", "chrome", "edge", "firefox", "rem"}
                    _unclear = _toks_n - _noise
                    if _unclear and not (_toks_n & {"youtube", "whatsapp", "google", "github", "gmail"}):
                        result = outcome.ask(
                            "browser_control",
                            "Which website or application did you mean for me to open, Tarun-kun?",
                            verified=False,
                        )
                        _log(player, result)
                        return result
                except Exception:
                    pass

        try:
            result = _open_native(nav_url, browser)
        except ValueError:
            result = outcome.ask(
                "browser_control",
                "Which website did you mean? Please give me its name or web address.",
                verified=False,
            )
        if nav_url and not result.startswith(f"status={outcome.FAILED} "):
            try:
                _registry.note_native_url(_normalize_url(nav_url))
            except ValueError:
                pass
        _log(player, result)
        return result

    # Native read-only inspection or explicit tab close when no Playwright session is attached yet
    site_hint = _infer_requested_site(action, params)
    if action in ("get_url", "get_text", "inspect") and not _registry.has(browser):
        bname = select_browser(browser)
        if not _discover_running_cdp_endpoint(bname):
            result = _native_browser_action(action, params, bname, player=player)
            _log(player, result)
            return result
        tabs = inspect_open_chrome_tabs_local(bname, site_hint=site_hint)
        if tabs:
            result = outcome.done(
                "browser_control", verified=True, browser=bname,
                title=tabs[0], page_state=_classify_page_state(tabs[0]),
                open_tabs="; ".join(tabs[:5]),
            )
        else:
            result = outcome.unverified(
                "browser_control",
                f"no controllable {bname} tab or active browser window title was found",
                browser=bname, page_state="unverified", verification_step="open_browser_window",
            )
        _log(player, result)
        return result

    if action == "close_tab" and not _registry.has(browser):
        bname = select_browser(browser)
        try:
            from core import screenact, screenview
            act = screenact.focus_app(bname)
            if act.ok:
                fg_app, fg_title, _pid, _hwnd = screenview._foreground()
                if bname in str(fg_app or "").casefold() or (bname == "edge" and "msedge" in str(fg_app or "").casefold()):
                    key_act = screenact.press("ctrl+w")
                    if key_act.ok:
                        _registry.invalidate_sync(bname)
                        result = outcome.done(
                            "browser_control", action="close_tab",
                            browser=bname, closed_title=fg_title, verified=True,
                        )
                        _log(player, result)
                        return result
        except Exception:
            pass
        result = outcome.failed(
            "browser_control",
            f"no open {bname} tab or controllable browser session was found to close",
            browser=bname,
        )
        _log(player, result)
        return result

    # When the user's real Chrome (or selected browser) is already running without CDP,
    # operate on that actual visible window instead of spawning a blank REM profile.
    selected_b = select_browser(browser)
    if _should_use_native_window(selected_b):
        result = _native_browser_action(action, params, selected_b, player=player)
        _log(player, result)
        return result

    # ── Interactive actions (click/type/read…) ───────────────────────────────
    # These require a physically controllable browser; the automation window
    # only opens here, and as soon as it opens it goes to the user's last
    # navigated page — it doesn't sit on a blank page.
    try:
        sess = _registry.get(browser)
    except _SeparateProfileBlockedError:
        result = _native_browser_action(action, params, selected_b, player=player)
        _log(player, result)
        return result
    except Exception as e:
        result = f"Could not start browser session: {e}"
        _log(player, result)
        return result

    try:
        last = _registry.pop_native_url()
        if last:
            try:
                sess.run(sess.go_to(last))
            except Exception as e:
                try:
                    parsed = urlsplit(last)
                    safe_origin = (f"{parsed.scheme}://{parsed.hostname}"
                                   if parsed.scheme and parsed.hostname else "stored page")
                except Exception:
                    safe_origin = "stored page"
                print(f"[Browser] Could not resume {safe_origin} ({type(e).__name__})")

        if action in ("click", "left_click"):
            result = sess.run(sess.click(params.get("selector"), params.get("text")))
            if _needs_visual_fallback(result):
                fb = _try_ocr_click_fallback(browser, params.get("text") or params.get("description") or "")
                if fb:
                    result = fb
        elif action in ("right_click", "double_click"):
            desc = str(params.get("description") or params.get("text") or params.get("selector") or "").strip()
            pt_info = sess.run(sess.locate_element_screen_rect(desc)) if desc else None
            if pt_info and "x" in pt_info and "y" in pt_info:
                from core import screenact
                pt = screenact.Point(int(pt_info["x"]), int(pt_info["y"]), origin="element", label=desc)
                mv = screenact.move_cursor(pt)
                clk = screenact.right_click(pt) if action == "right_click" else screenact.double_click(pt)
                if clk.ok:
                    result = outcome.done(
                        "browser_control", action=action, browser=browser or sess.browser_name,
                        control_mode="cdp_plus_os_input", target=desc,
                        x=int(pt_info["x"]), y=int(pt_info["y"]),
                        cursor_moved=mv.ok, click_executed=True, verified=True,
                    )
                else:
                    result = outcome.failed("browser_control", f"{action} on {desc!r} failed: {clk.error}")
            else:
                result = outcome.failed("browser_control", f"Could not locate {desc!r} for {action} in {browser or sess.browser_name}")
        elif action == "type":
            result = sess.run(sess.type_text(
                params.get("selector"), params.get("text", ""), params.get("clear_first", True)))
        elif action == "scroll":
            result = sess.run(sess.scroll(params.get("direction", "down"), int(params.get("amount", 500))))
        elif action == "fill_form":
            result = sess.run(sess.fill_form(params.get("fields", {})))
        elif action == "smart_click":
            result = sess.run(sess.smart_click(params.get("description", "")))
            if _needs_visual_fallback(result):
                fb = _try_ocr_click_fallback(browser, params.get("description") or params.get("text") or "")
                if fb:
                    result = fb
        elif action == "smart_type":
            result = sess.run(sess.smart_type(params.get("description", ""), params.get("text", "")))
        elif action == "clear_field":
            result = sess.run(sess.clear_field(params.get("description", "")))
        elif action == "get_text":
            result = sess.run(sess.get_text())
        elif action == "get_url":
            result = sess.run(sess.get_url(site_hint))
        elif action == "inspect":
            result = sess.run(sess.inspect_page())
        elif action == "switch_tab":
            result = sess.run(sess.switch_tab(params.get("target") or params.get("text") or ""))
        elif action == "press":
            result = sess.run(sess.press(params.get("key", "Enter")))
        elif action == "focus_search":
            result = sess.run(sess.focus_search_cdp())
        elif action == "pause_video":
            result = sess.run(sess.pause_video())
        elif action == "resume_video":
            result = sess.run(sess.resume_video())
        elif action in ("seek_forward", "seek_backward", "set_playback_position"):
            raw_v = params.get("seconds") or params.get("value") or params.get("position") or params.get("amount") or 10
            try:
                sec_v = float(raw_v)
            except (TypeError, ValueError):
                sec_v = 10.0
            if action == "seek_backward":
                sec_v = -abs(sec_v)
            elif action == "seek_forward":
                sec_v = abs(sec_v)
            result = sess.run(sess.seek_video(sec_v))
        elif action in ("volume_up", "volume_down"):
            step = 10.0 if action == "volume_up" else -10.0
            result = sess.run(sess.volume_step(step))
        elif action in ("mute", "unmute"):
            result = sess.run(sess.set_muted(action == "mute"))
        elif action == "set_volume":
            raw_pct = params.get("value") or params.get("percent") or params.get("amount") or params.get("text") or ""
            m_pct = re.search(r"\d+(?:\.\d+)?", str(raw_pct))
            result = (sess.run(sess.set_volume(float(m_pct.group(0)))) if m_pct else
                      outcome.failed("browser_control", "no volume percentage was given", action="set_volume"))
        elif action in ("fullscreen", "exit_fullscreen"):
            result = sess.run(sess.set_fullscreen(action == "fullscreen"))
        elif action == "play_video":
            desc = params.get("description") or params.get("text") or "first video"
            result = sess.run(sess.smart_click(desc))
            if _needs_visual_fallback(result):
                fb = _try_ocr_click_fallback(browser, desc)
                if fb:
                    result = fb
        elif action == "replay_video":
            result = sess.run(sess.replay_video(params.get("description") or params.get("text") or ""))
        elif action == "close_tab":
            result = sess.run(sess.close_tab())
        elif action == "screenshot":
            result = sess.run(sess.screenshot(params.get("path")))
        elif action == "back":
            result = sess.run(sess.back())
        elif action == "forward":
            result = sess.run(sess.forward())
        elif action == "reload":
            result = sess.run(sess.reload())
        elif action == "search_contact":
            q = str(params.get("text") or params.get("query") or params.get("description") or "").strip()
            result = sess.run(sess.search_whatsapp_contact(q))
            if str(result).startswith(f"status={outcome.UNVERIFIED} "):
                result = _native_browser_action(action, params, select_browser(browser), player=player)
        elif action == "select_contact":
            d = str(params.get("description") or params.get("text") or params.get("query") or "").strip()
            result = sess.run(sess.select_whatsapp_contact(d))
            if str(result).startswith(f"status={outcome.UNVERIFIED} "):
                result = _native_browser_action(action, params, select_browser(browser), player=player)
        elif action in ("compose_message", "update_draft"):
            result = _native_browser_action(action, params, select_browser(browser), player=player)
        else:
            result = _unknown_action(action)

        if str(result).startswith("CONFIRM_REQUIRED:") and not _approved_internal:
            from core import confirm
            if confirm.pending_title():
                result = "Another confirmation is already waiting; no browser action was performed."
            else:
                label = str(result).split(":", 1)[-1].strip()
                result = confirm.request(
                    key="browser-consequential-submit",
                    title=f"Confirm browser action: {label}?",
                    detail="Review the current page and intended effect before approving.",
                    run=lambda: browser_control(params, player=player,
                                                _approved_internal=True),
                )

    except concurrent.futures.TimeoutError:
        result = f"Browser action '{action}' timed out (60s)."
    except Exception as e:
        result = f"Browser error ({action}): {e}"

    _log(player, result)
    return result


def _needs_visual_fallback(result: object) -> bool:
    text = str(result or "").casefold()
    if any(k in text for k in (
        "evidence_status=unknown", "evidence_status=inferred",
        "cannot reliably read", "whatsapp_search_field_not_found",
        "no matching chat", "no whatsapp contact", "selected_chat_verified=false",
    )):
        return False
    return (
        text.startswith("status=failed")
        or "no matching" in text
        or "no visible" in text
        or "could not find" in text
    )


def _try_ocr_click_fallback(browser_name: str, target_desc: str) -> str | None:
    """Level-2 visual OCR/UIA click fallback inside the selected browser window when DOM misses."""
    target = str(target_desc or "").strip()
    if not target:
        return None
    risky = ("send", "submit", "delete", "remove", "purchase", "buy",
             "place order", "pay", "transfer", "install", "allow access")
    if any(r in target.casefold() for r in risky):
        return None
    try:
        from core import screenact, screenview
        from memory import config_manager
        if not config_manager.get_local_screen_observation_enabled():
            return None
        avail = screenact.available()
        if not (avail[0] if isinstance(avail, tuple) else avail):
            return None
        bname = str(browser_name or "chrome").casefold()
        fg_app, _, _, _ = screenview._foreground()
        expected_proc = "msedge" if bname == "edge" else bname
        if expected_proc not in str(fg_app or "").casefold():
            act = screenact.focus_app(bname)
            if not act.ok:
                return None
            fg_app, _, _, _ = screenview._foreground()
            if expected_proc not in str(fg_app or "").casefold():
                return None
        view = screenview.look(allow_ocr=True)
        elements = list(getattr(view, "elements", ()) or ())
        if not elements:
            return None
        needle = target.casefold()
        tokens = [t for t in re.findall(r"[a-z0-9]+", needle) if len(t) >= 2]
        best = None
        for el in elements:
            label = str(getattr(el, "label", "") or "").strip()
            if not label:
                continue
            low = label.casefold()
            if needle == low or needle in low:
                best = el
                break
            if best is None and tokens and all(tok in low for tok in tokens):
                best = el
        if best is None:
            return None
        click_res = screenact.click(best)
        if not click_res.ok:
            return None
        pt = getattr(click_res, "point", None) or getattr(best, "center", (0, 0))
        return outcome.done(
            "browser_control",
            action="click",
            browser=bname,
            target=best.label,
            method="ocr_fallback",
            x=int(pt[0]),
            y=int(pt[1]),
            verified=True,
        )
    except Exception:
        return None


_opening_inflight_lock = threading.Lock()
_opening_inflight: dict[tuple[str, str], float] = {}


def _mark_opening_inflight(browser_name: str, site: str, active: bool) -> None:
    if not site:
        return
    key = (str(browser_name or "chrome").lower(), str(site).lower())
    with _opening_inflight_lock:
        if active:
            _opening_inflight[key] = time.monotonic()
        else:
            _opening_inflight.pop(key, None)


def _is_opening_inflight(browser_name: str, site: str, max_age_s: float = 12.0) -> bool:
    if not site:
        return False
    key = (str(browser_name or "chrome").lower(), str(site).lower())
    now_m = time.monotonic()
    with _opening_inflight_lock:
        ts = _opening_inflight.get(key)
        if ts is None:
            return False
        if (now_m - ts) > max_age_s:
            _opening_inflight.pop(key, None)
            return False
        return True


def _wait_or_inspect_inflight_open(browser_name: str, site: str, *, url: str = "") -> str:
    """Wait briefly or inspect an already-in-progress open operation instead of returning a busy failure."""
    deadline = time.monotonic() + 1.2
    while _is_opening_inflight(browser_name, site) and time.monotonic() < deadline:
        time.sleep(0.15)
    win = _find_browser_window(browser_name, site_hint=site)
    if win.get("found") and not win.get("site_mismatch"):
        ident = verify_site_page_identity(site, url="", title=str(win.get("title") or ""))
        if ident.get("verified"):
            ok_fg, fg_win, _fg_err = _ensure_browser_foreground(
                browser_name, win, title_hint=site, site_hint=site,
            )
            if ok_fg:
                return outcome.done(
                    "browser_control",
                    opened=url or site,
                    browser=browser_name,
                    url=url,
                    active_tab=fg_win.get("title") or win.get("title", ""),
                    window_hwnd=fg_win.get("hwnd") or win.get("hwnd", 0),
                    window_pid=fg_win.get("pid") or win.get("pid", 0),
                    page_state=ident.get("page_state") or "loaded",
                    reused_existing=True,
                    joined_inflight=True,
                    navigation_completed=True,
                    target_matched=True,
                    foreground_verified=True,
                    navigation_lifecycle=NAV_SUCCESS,
                    verified=True,
                )
    return outcome.unverified(
        "browser_control",
        f"{browser_name} is already opening {site}; checked the active window state instead of starting a duplicate launch.",
        browser=browser_name,
        url=url,
        reused_existing=True,
        joined_inflight=True,
        target_matched=False,
        foreground_verified=False,
        page_state="loading",
    )


def browser_control(parameters: dict = None, response=None, player=None,
                    session_memory=None, _approved_internal: bool = False) -> str:
    params = parameters or {}
    action = str(params.get("action") or "").lower().strip()
    # Browser DOM operations share the central action lock with native input,
    # so two voice/tool calls cannot race the foreground application.
    from core import desktop_control
    try:
        with desktop_control.action(f"browser:{action}"):
            if player:
                try:
                    player.set_desktop_control_status(desktop_control.snapshot())
                except Exception:
                    pass
            result = _browser_control_impl(params, response=response, player=player,
                                           session_memory=session_memory,
                                           _approved_internal=_approved_internal)
            desktop_control.report(f"browser:{action}", str(result)[:140])
            if player:
                player.set_desktop_control_status(desktop_control.snapshot())
            return result
    except RuntimeError as exc:
        if "in progress" in str(exc).casefold() and action in {"go_to", "open", "inspect", "get_url"}:
            b_sel = select_browser(params.get("browser"))
            s_hint = _infer_requested_site(action, params)
            return _wait_or_inspect_inflight_open(b_sel, s_hint or "whatsapp", url=str(params.get("url") or ""))
        return str(exc)


def _log(player, text: str):
    # ONE line, once. This printed the same string twice whenever a player was
    # attached - which is always, in the real app - and the second copy read in
    # the terminal like a second navigation. "Opened in operagx: <url>" twice in
    # a row looks exactly like the browser being opened twice, and time was
    # spent looking for a double-open that had never happened.
    print(f"[Browser] {str(text)[:80]}")


# ── Tool declaration (auto-discovered by core/action_loader.py) ──────────────
TOOL = {
    "name": "browser_control",
    "description": (
        "Controls the web browser and everything INSIDE a web page: open an "
        "address, search, click, type, scroll, read the page, manage tabs, go "
        "back and forward. Anything happening on a website is this tool - it "
        "reads the page itself, which nothing else in the assistant can do. "
        "THIS TOOL OPENS THE BROWSER ITSELF. Never call open_app for the "
        "browser first - doing both launches it twice, the second copy on a "
        "different profile with none of the user's sign-ins. 'Open Chrome and "
        "go to YouTube' is ONE call to this tool. "
        "NEVER INVENT AN ADDRESS. Only pass a url you actually know. Anything "
        "belonging to the user personally - their own channel, profile, "
        "account page, inbox or playlist - has an address you do NOT know, and "
        "guessing a path produces an error page that looks like a working one. "
        "When the exact address is not known: open the site's own address "
        "(youtube.com, instagram.com) and get to the rest with smart_click on "
        "what the page shows, or use action='search', or ask the user. A "
        "guessed path is never acceptable. "
        "TO CLICK OR TYPE ON A PAGE, PREFER action='smart_click' or "
        "action='smart_type' with a 'description' of what the thing SAYS on "
        "screen - the visible label, in the page's own language. They search "
        "the page by role, label, placeholder and text and find it. Plain "
        "'click' with a 'selector' needs a CSS selector that is exactly right "
        "for that site's markup, which is not something to guess at; use it "
        "only when the selector is known. "
        "Use screen_agent for native desktop controls. This tool is for web "
        "pages and uses the DOM/accessibility roles instead of screenshot "
        "coordinates. Each click/read/entry is checked against current page "
        "state; ambiguous matches are refused. Consequential buttons and form "
        "submissions pause for on-screen user confirmation. "
        "Read the status it returns. 'done' means it was confirmed - for an "
        "open, the url reported back is the page's OWN address, so if it "
        "differs from what was asked for the site redirected and the user "
        "should be told. 'unverified' means the address was handed to a browser "
        "that does not report back: whether the page loaded, redirected or does "
        "not exist is unknown, so say it was opened but NOT confirmed and never "
        "that the page is up. 'failed' means nothing happened."
    ),
    "brief": (
        "Controls the web browser: open a URL, navigate, click, read the page."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": "go_to | search | click | left_click | type | smart_type | clear_field | scroll | fill_form | smart_click | search_contact | select_contact | compose_message | update_draft | pause_video | resume_video | play_video | replay_video | next_video | seek_forward | seek_backward | volume_up | volume_down | set_volume | mute | unmute | fullscreen | exit_fullscreen | get_text | get_url | inspect | press | new_tab | switch_tab | close_tab | screenshot | back | forward | reload | switch | list_browsers | close | close_all"
            },
            "browser": {
                "type": "STRING",
                "description": "Target browser: chrome | edge | firefox | opera | operagx | brave | vivaldi | safari. Omit to use the currently active browser."
            },
            "url": {
                "type": "STRING",
                "description": "URL for go_to / new_tab action"
            },
            "query": {
                "type": "STRING",
                "description": "Search query for search action"
            },
            "engine": {
                "type": "STRING",
                "description": "Search engine: google | bing | duckduckgo | yandex (default: google)"
            },
            "selector": {
                "type": "STRING",
                "description": "CSS selector for click/type"
            },
            "text": {
                "type": "STRING",
                "description": "Text to click or type"
            },
            "description": {
                "type": "STRING",
                "description": "Element description for smart_click/smart_type"
            },
            "direction": {
                "type": "STRING",
                "description": "up | down for scroll"
            },
            "amount": {
                "type": "INTEGER",
                "description": "Scroll amount in pixels (default: 500)"
            },
            "key": {
                "type": "STRING",
                "description": "Key name for press action (e.g. Enter, Escape, F5)"
            },
            "path": {
                "type": "STRING",
                "description": "Save path for screenshot"
            },
            "incognito": {
                "type": "BOOLEAN",
                "description": "Open in private/incognito mode"
            },
            "clear_first": {
                "type": "BOOLEAN",
                "description": "Clear field before typing (default: true)"
            }
        },
        "required": [
            "action"
        ]
    },
    "handler": browser_control,
}
