
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
    m = re.search(
        r"\b(first|1st|second|2nd|third|3rd|fourth|4th|fifth|5th|"
        r"sixth|6th|seventh|7th|eighth|8th|ninth|9th|tenth|10th|last)"
        r"\b(?:\s+\w+){0,2}\s+(?:videos?|results?|thumbnails?|cards?|links?|items?|one)\b",
        str(text or ""),
        re.IGNORECASE,
    )
    if not m:
        m = re.fullmatch(
            r"\s*(?:the\s+)?(first|1st|second|2nd|third|3rd|fourth|4th|fifth|5th|"
            r"sixth|6th|seventh|7th|eighth|8th|ninth|9th|tenth|10th|last)\s*",
            str(text or ""),
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


def inspect_open_chrome_tabs_local(browser_name: str = "chrome") -> list[str]:
    """Return a non-intrusive local summary of open browser windows/tabs."""
    found: list[str] = []
    try:
        from core import screenview
        for app, title in screenview.all_windows():
            app_cf = str(app or "").casefold()
            title_cf = str(title or "").casefold()
            if browser_name in app_cf or "chrome" in title_cf or "edge" in title_cf:
                found.append(f"{app or browser_name}: {title}")
    except Exception:
        pass
    try:
        if _registry.has(browser_name):
            sess = _registry.get(browser_name)
            page = getattr(sess, "_page", None)
            if page is not None and not page.is_closed():
                found.insert(0, f"CDP tab: {page.url}")
    except Exception:
        pass
    return found


def _verify_native_browser(name: str, url: str, *, timeout_s: float | None = None,
                           reused_existing: bool = False) -> str:
    """Verify native browser launch and active window/tab state when possible."""
    import time as _time
    if timeout_s is None:
        try:
            timeout_s = float(os.environ.get("REM_BROWSER_VERIFY_TIMEOUT", "2.5"))
        except ValueError:
            timeout_s = 2.5

    host, hints = _expected_site_hints(url)
    deadline = _time.monotonic() + max(0.0, timeout_s)
    running = bool(appidentity.running(name))
    matched_title = ""
    observed_url = ""
    all_browser_titles: list[str] = []

    while True:
        running = bool(appidentity.running(name))
        all_browser_titles = []
        try:
            from core import screenview
            for app, title in screenview.all_windows():
                app_cf = str(app or "").casefold()
                title_cf = str(title or "").casefold()
                if name in app_cf or (name == "chrome" and "google chrome" in title_cf) or (name == "edge" and "microsoft edge" in title_cf):
                    all_browser_titles.append(str(title or ""))
                    if any(h in title_cf for h in hints):
                        matched_title = str(title or "")
                        break
            if not matched_title:
                fg_app, fg_title, _pid, _hwnd = screenview._foreground()
                if name in str(fg_app or "").casefold():
                    view = screenview.look(budget_s=0.6, allow_ocr=False, allow_pixel_digest=False)
                    for el in view.elements:
                        val = str(getattr(el, "value", "") or getattr(el, "name", "") or "")
                        if host and host in val.casefold():
                            observed_url = val if "://" in val else f"https://{val}"
                            matched_title = str(fg_title or view.title or "")
                            break
        except Exception:
            pass
        if matched_title or _time.monotonic() >= deadline:
            break
        _time.sleep(0.35)

    if not url:
        if running:
            print(f"[Browser] Verified launch browser={name} reused={reused_existing} windows={len(all_browser_titles)}")
            return outcome.done("browser_control", browser=name, opened=True,
                                verified=True, browser_launched=True,
                                reused_existing=reused_existing,
                                page_state="loaded" if all_browser_titles else "open")
        print(f"[Browser] Unverified launch browser={name} (process not detected)")
        return outcome.unverified(
            "browser_control",
            "the browser was launched but has not appeared in the process list yet",
            browser=name, browser_launched=False, reused_existing=reused_existing,
            page_state="unverified", verification_step="process_presence",
        )

    if matched_title:
        page_state = _classify_page_state(matched_title)
        if page_state == "error":
            print(f"[Browser] Navigation error detected browser={name} url={url} title={matched_title!r}")
            return outcome.failed(
                "browser_control",
                f"the page reported an error ({matched_title})",
                browser=name, url=observed_url or url, title=matched_title,
                page_state="error", browser_launched=running,
                reused_existing=reused_existing, navigation_requested=True,
            )
        print(f"[Browser] Verified native navigation browser={name} reused={reused_existing} url={observed_url or url} page_state={page_state}")
        return outcome.done(
            "browser_control",
            verified=True,
            browser=name,
            url=observed_url or url,
            title=matched_title,
            active_tab=matched_title,
            page_state=page_state,
            browser_launched=running,
            reused_existing=reused_existing,
            navigation_requested=True,
            navigation_completed=True,
        )

    print(f"[Browser] Unverified native navigation browser={name} url={url} running={running}")
    return outcome.unverified(
        "browser_control",
        "navigation was requested, but active tab URL and page load state could not be confirmed",
        browser=name,
        url=url,
        browser_launched=running,
        reused_existing=reused_existing,
        navigation_requested=True,
        navigation_completed=False,
        page_state="unverified",
        verification_step="active_tab_url_and_title",
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

    # Specific browser → launch or reuse its own executable, never silently substitute Edge.
    if name:
        try:
            reused_existing = bool(appidentity.running(name))
        except Exception:
            reused_existing = False
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
                self._page = live[-1] if live else await self._context.new_page()
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

    async def _page_visible_for_action(self, page) -> bool:
        """Refuse input to a hidden/background page whose actions are not observable."""
        try:
            return await page.evaluate("document.visibilityState === 'visible'") is True
        except Exception:
            return False

    async def go_to(self, url: str) -> str:

        try:
            url = _normalize_url(url)
        except ValueError:
            return outcome.ask(
                "browser_control",
                "Which website did you mean? Please give me its name or web address.",
                verified=False,
            )
        page     = await self._get_page()
        prev_url = page.url
        navigation_error = None
        if not await self._page_visible_for_action(page):
            return outcome.unverified(
                "browser_control", "browser page is hidden; navigation was not attempted",
                requested=url, actual=prev_url, navigation_confirmed=False)

        async def _do_goto(p: Page) -> str:
            """Attempt navigation and return the resulting URL (may still be blank)."""
            nonlocal navigation_error
            try:
                await p.goto(url, wait_until="domcontentloaded", timeout=30_000)
                await asyncio.sleep(0.3)
            except PlaywrightTimeout:
                # A timeout can happen after the address changed. The URL and
                # document readiness below decide whether navigation occurred.
                navigation_error = "timeout"
            except Exception as e:
                navigation_error = type(e).__name__
            return p.url

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

        if result_url and result_url not in ("about:blank", "", None):
            try:
                page_ready = await page.evaluate(
                    "() => document.readyState === 'interactive' || document.readyState === 'complete'")
            except Exception:
                page_ready = False
            if not page_ready:
                return outcome.unverified(
                    "browser_control",
                    "the browser URL changed, but the page did not confirm DOM readiness",
                    requested=url, actual=result_url, navigation_confirmed=True,
                    page_ready=False, error=navigation_error)
            if not await self._page_visible_for_action(page):
                return outcome.unverified(
                    "browser_control",
                    "the URL loaded, but the browser page is hidden; visible navigation is not confirmed",
                    requested=url, actual=result_url, navigation_confirmed=False,
                    page_ready=True, browser=self.browser_name, page_state="hidden")
            try:
                title = await page.title()
            except Exception:
                title = ""
            try:
                body_sample = (await page.locator("body").inner_text(timeout=1_500))[:1200]
            except Exception:
                body_sample = ""
            page_state = _classify_page_state(title or result_url, body_sample)
            if page_state == "error":
                print(f"[Browser] Navigation error page browser={self.browser_name} url={result_url} title={title!r}")
                return outcome.failed(
                    "browser_control",
                    f"the requested page failed to load ({title or result_url})",
                    browser=self.browser_name, url=result_url, title=title, page_state="error",
                )
            print(f"[Browser] Verified navigation browser={self.browser_name} url={result_url} page_state={page_state}")
            return outcome.done("browser_control", opened=result_url,
                                browser=self.browser_name, url=result_url,
                                title=title or "", page_state=page_state,
                                profile=self.profile_kind or "real",
                                verified=True, navigation_confirmed=True,
                                page_ready=True, requested_url=url,
                                requested_url_match=(result_url.rstrip("/")
                                                     == url.rstrip("/")),
                                profile_notice=(
                                    "separate profile; existing signed-in profile was not used"
                                    if self.profile_kind == "separate" else None))
        if result_url == prev_url and prev_url not in ("about:blank", "", None):
            return outcome.unverified(
                "browser_control",
                "the requested navigation was accepted, but the current page URL did not change",
                requested=url, actual=result_url, navigation_confirmed=False,
                page_ready=False, error=navigation_error)
        return outcome.failed(
            "browser_control",
            "the page stayed blank - the address may be wrong, or the site may "
            "not have answered", url=url)

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
            y = amount if direction == "down" else -amount
            await page.mouse.wheel(0, y)
            return f"Scrolled {direction}."
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

    async def get_url(self) -> str:
        page = await self._get_page()
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

        ordinal_idx = _parse_ordinal_index(desc_str)
        if ordinal_idx is not None:
            if is_youtube:
                return await self._click_youtube_video_ordinal(page, ordinal_idx, desc_str)
            return await self._click_ordinal_result(
                page, _INDEX_TO_ORDINAL.get(ordinal_idx, f"#{ordinal_idx + 1}"), before)

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
            if (!id || !title || seen.has(id)) continue;
            seen.add(id);
            out.push({{
              position: out.length + 1,
              id,
              url: `https://www.youtube.com/watch?v=${{id}}`,
              title: title.slice(0, 160),
              channel: channel.slice(0, 80),
              is_ad: false,
              is_playable: true,
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
        return cards or []

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
        """Open check + HTML5 <video> playback verification on /watch."""
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
        ord_label = _INDEX_TO_ORDINAL.get((position or 1) - 1, f"#{position or 1}")
        summary = f"Opened and verified the {ord_label} YouTube video: {title!r}."
        if not hasattr(page, "evaluate"):
            return outcome.done(
                "browser_control",
                action="play_video",
                browser=getattr(self, "browser_name", "chrome"),
                position=position,
                title=title,
                channel=channel,
                video_id=video_id,
                url=page.url,
                detail=summary,
                verified=True,
            )
        state = {}
        for attempt in range(4):
            try:
                state = await page.evaluate(
                    "() => { const v = document.querySelector('video'); "
                    "if (!v) return {exists:false}; "
                    "return {exists:true, paused:!!v.paused, currentTime:Number(v.currentTime||0), "
                    "readyState:Number(v.readyState||0), duration:Number(v.duration||0)}; }"
                )
            except Exception:
                state = {}
            if state.get("exists") and not state.get("paused") and state.get("readyState", 0) >= 2:
                break
            if attempt == 1 and state.get("exists") and state.get("paused"):
                try:
                    await page.evaluate("() => { const v = document.querySelector('video'); if (v && v.paused) v.play(); }")
                except Exception:
                    pass
            await asyncio.sleep(0.25)
        playing = bool(state.get("exists") and not state.get("paused") and state.get("readyState", 0) >= 2)
        if not state.get("exists"):
            return outcome.unverified(
                "browser_control",
                f"opened video {title!r}, but HTML5 video player did not initialize yet",
                browser=getattr(self, "browser_name", "chrome"), action="play_video", video_id=video_id,
                title=title, channel=channel, position=position, url=page.url, playback_verified=False,
            )
        return outcome.done(
            "browser_control",
            action="play_video",
            browser=getattr(self, "browser_name", "chrome"),
            position=position,
            title=title,
            channel=channel,
            video_id=video_id,
            url=page.url,
            player_state=("playing" if playing else "paused"),
            current_time=round(float(state.get("currentTime") or 0.0), 2),
            playback_verified=playing,
            detail=summary,
            verified=True,
        )

    async def _click_youtube_video_ordinal(self, page, ordinal: int | str, before: tuple | str = ()) -> str:
        """Select a real visible non-ad YouTube video by 0-based ordinal index or word (e.g. 'second')."""
        try:
            raw_label = before if isinstance(before, str) else ""
            cards = await self._extract_youtube_videos(page, include_shorts=("short" in raw_label.casefold()))
            idx = (_ORDINAL_WORDS.get(str(ordinal).lower()) if isinstance(ordinal, str) else int(ordinal))
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
            title = str(selected.get("title", ""))
            channel = str(selected.get("channel", ""))
            if not re.fullmatch(r"[A-Za-z0-9_-]{6,20}", video_id) or not title:
                return "The selected result did not have a verifiable video identity; nothing was clicked."
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

    async def pause_video(self) -> str:
        page = await self._get_page()
        try:
            state = await page.evaluate(
                "() => { const v = document.querySelector('video'); if (!v) return {exists:false}; "
                "if (!v.paused) v.pause(); "
                "return {exists:true, paused:!!v.paused, currentTime:Number(v.currentTime||0)}; }"
            )
            if not state or not state.get("exists"):
                return outcome.failed("browser_control", "no active video player was found on this page", url=page.url)
            paused = bool(state.get("paused"))
            return outcome.done(
                "browser_control", action="pause_video", browser=self.browser_name,
                playback_state=("paused" if paused else "playing"),
                current_time=round(float(state.get("currentTime") or 0.0), 2),
                url=page.url, verified=paused,
            )
        except Exception as exc:
            return outcome.failed("browser_control", f"pause failed: {type(exc).__name__}: {exc}")

    async def resume_video(self) -> str:
        page = await self._get_page()
        try:
            state = await page.evaluate(
                "async () => { const v = document.querySelector('video'); if (!v) return {exists:false}; "
                "if (v.paused) { try { await v.play(); } catch (e) {} } "
                "return {exists:true, paused:!!v.paused, currentTime:Number(v.currentTime||0)}; }"
            )
            if not state or not state.get("exists"):
                return outcome.failed("browser_control", "no active video player was found on this page", url=page.url)
            playing = not bool(state.get("paused"))
            return outcome.done(
                "browser_control", action="resume_video", browser=self.browser_name,
                playback_state=("playing" if playing else "paused"),
                current_time=round(float(state.get("currentTime") or 0.0), 2),
                url=page.url, verified=playing,
            )
        except Exception as exc:
            return outcome.failed("browser_control", f"resume failed: {type(exc).__name__}: {exc}")

    async def seek_video(self, seconds: float = 10.0) -> str:
        page = await self._get_page()
        try:
            delta = float(seconds)
            state = await page.evaluate(
                """(delta) => {
                    const v = document.querySelector('video');
                    if (!v) return {exists: false};
                    const before = Number(v.currentTime || 0);
                    const dur = Number(v.duration || 0);
                    const target = Math.max(0, dur > 0 ? Math.min(dur, before + delta) : before + delta);
                    v.currentTime = target;
                    const after = Number(v.currentTime || target);
                    return {exists: true, before, after, paused: !!v.paused};
                }""",
                delta,
            )
            if not state or not state.get("exists"):
                return outcome.failed("browser_control", "no active video player was found on this page", url=page.url)
            before_t = round(float(state.get("before") or 0.0), 2)
            after_t = round(float(state.get("after") or 0.0), 2)
            moved = (after_t > before_t) if delta >= 0 else (after_t < before_t)
            act_name = "seek_forward" if delta >= 0 else "seek_backward"
            return outcome.done(
                "browser_control", action=act_name, browser=self.browser_name,
                dispatch_status="completed", input_status="completed",
                verification_status=("verified" if moved else "unverified"),
                seconds=round(abs(delta), 2), playback_before=before_t, playback_after=after_t,
                url=page.url, verified=moved,
                detail=f"Skipped {'forward' if delta >= 0 else 'backward'} {abs(round(delta))} seconds, Tarun-kun.",
            )
        except Exception as exc:
            return outcome.failed("browser_control", f"seek failed: {type(exc).__name__}: {exc}")

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

        page = await self._get_page()
        try:
            state = await page.evaluate(
                "async () => { const v = document.querySelector('video'); if (!v) return {exists:false}; "
                "v.currentTime = 0; try { await v.play(); } catch (e) {} "
                "return {exists:true, paused:!!v.paused, currentTime:Number(v.currentTime||0)}; }"
            )
            if not state or not state.get("exists"):
                return outcome.failed("browser_control", "no active video player was found to replay", url=page.url)
            playing = not bool(state.get("paused"))
            return outcome.done(
                "browser_control", action="replay_video", browser=self.browser_name,
                replayed=True, playback_state=("playing" if playing else "paused"),
                current_time=round(float(state.get("currentTime") or 0.0), 2),
                url=page.url, verified=playing,
            )
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
        try:
            await page.reload(timeout=15_000)
            return f"Page reloaded: {page.url}"
        except Exception as e:
            return f"Reload error: {e}"

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

    def invalidate_sync(self, browser_name: str | None = None) -> None:
        with self._lock:
            if not browser_name:
                self._sync_state.clear()
            else:
                name = _ALIASES.get(browser_name.lower().strip(), browser_name.lower().strip())
                self._sync_state.pop(name, None)

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


def _find_browser_window(browser_name: str) -> dict:
    """Identify the actual visible foreground/open window for ``browser_name``."""
    b = _ALIASES.get(str(browser_name or "chrome").lower().strip(), str(browser_name or "chrome").lower().strip())
    try:
        from core import screenview
        fg_app, fg_title, fg_pid, fg_hwnd = screenview._foreground()
        if _proc_matches_browser(fg_app, fg_title, b):
            return {
                "found": True,
                "foreground": True,
                "browser": b,
                "app": fg_app,
                "title": fg_title,
                "pid": int(fg_pid or 0),
                "hwnd": int(fg_hwnd or 0),
            }
        for app, title in screenview.all_windows():
            if _proc_matches_browser(app, title, b):
                return {
                    "found": True,
                    "foreground": False,
                    "browser": b,
                    "app": app,
                    "title": title,
                    "pid": 0,
                    "hwnd": 0,
                }
    except Exception:
        pass
    running = bool(appidentity.running(b)) if b else False
    return {"found": running, "foreground": False, "browser": b, "app": b, "title": "", "pid": 0, "hwnd": 0}


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


def _extract_native_youtube_candidates(view, *, include_shorts: bool = False) -> tuple[list[dict], bool]:
    """Extract ordered non-ad video candidates from UIA/OCR on the visible Chrome window."""
    elements = list(getattr(view, "elements", ()) or ())
    mon = getattr(view, "monitor", None) or {}
    mon_top = int(mon.get("top", 0) or 0)
    mon_left = int(mon.get("left", 0) or 0)
    mon_w = int(mon.get("width", 1920) or 1920)
    header_floor = mon_top + 130
    ocr_floor = mon_top + 185  # Excludes Chrome toolbar + YouTube masthead/filter chips
    sidebar_ceil = mon_left + (200 if mon_w >= 1100 else 60)
    view_source = str(getattr(view, "source", "") or "").casefold()

    joined_low = " ".join(
        str(getattr(el, "name", "") or getattr(el, "value", "") or "").casefold()
        for el in elements
    )
    empty_home = "try searching to get started" in joined_low

    now_m = time.monotonic()
    candidates: list[dict] = []
    seen_titles: set[str] = set()
    for el in elements:
        role = str(getattr(el, "role", "") or "").casefold()
        is_ocr = ("ocr" in role) or (view_source == "ocr" and role in {"text", "ocr_text", ""})
        name = " ".join(str(getattr(el, "name", "") or getattr(el, "label", "") or "").split())
        if not name or len(name) < 4:
            continue
        low = name.casefold()
        if low in _YT_NAV_NOISE or _DURATION_RE.match(name) or _YT_META_RE.search(name):
            continue
        if any(ad in low for ad in ("sponsored", "ad ·", "advertisement", "shop now", "learn more")):
            continue
        if not include_shorts and ("#shorts" in low or low == "shorts"):
            continue
        if is_ocr and len(name.split()) < 2 and len(name) < 14:
            continue
        top = int(getattr(el, "top", 0) or 0)
        left = int(getattr(el, "left", 0) or 0)
        width = int(getattr(el, "width", 0) or 0)
        height = int(getattr(el, "height", 0) or 0)
        if top < (ocr_floor if is_ocr else header_floor) or left < sidebar_ceil:
            continue
        if width < 90 or height < 14:
            continue
        # Only take real interactive/text headings or clickable video titles from UIA/OCR
        if role not in {"link", "button", "heading", "text", "ocr_text", "group", ""}:
            if "link" not in role and "text" not in role and "heading" not in role:
                continue
        if low in seen_titles:
            continue
        seen_titles.add(low)
        cx, cy = (getattr(el, "center", None) or (left + max(1, width // 2), top + max(1, height // 2)))
        src_kind = "ocr" if is_ocr else "uia"
        candidates.append({
            "position": len(candidates) + 1,
            "title": name[:160],
            "point": (int(cx), int(cy)),
            "rect": (left, top, width, height),
            "top": top,
            "left": left,
            "source": src_kind,
            "confidence": ("high" if src_kind == "uia" and "link" in role else "medium"),
            "observed_at": now_m,
            "element": el,
        })
    candidates.sort(key=lambda item: (item["top"] // 60, item["left"]))
    for idx, item in enumerate(candidates, 1):
        item["position"] = idx
    return candidates, empty_home


def _inspect_visible_browser_window(browser_name: str, *, focus_if_needed: bool = False,
                                    allow_ocr: bool = True) -> dict:
    """Synchronize foreground window identity, URL, search query, and visible YouTube state."""
    from core import screenact, screenview

    win = _find_browser_window(browser_name)
    if focus_if_needed and win.get("found") and not win.get("foreground"):
        try:
            screenact.focus_app(win["browser"])
            time.sleep(0.2)
            win = _find_browser_window(win["browser"])
        except Exception:
            pass

    view = None
    url = ""
    if win.get("foreground") and _proc_matches_browser(win.get("app", ""), win.get("title", ""), win["browser"]):
        try:
            view = screenview.look(budget_s=(2.8 if allow_ocr else 1.2), allow_ocr=allow_ocr)
        except Exception:
            view = None

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
        elif not url and ("youtube.com/" in val.casefold() or "http://" in val.casefold() or "https://" in val.casefold()):
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

    videos, empty_home = _extract_native_youtube_candidates(view) if view is not None else ([], False)
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
    is_wa = "whatsapp" in (title + " " + url).casefold()
    wa_contacts: list[dict] = []
    wa_chat: dict = {}
    if is_wa and view is not None:
        try:
            from plugins import _whatsapp_core as _wa
            wa_contacts = _wa.extract_whatsapp_contacts(view, query=search_query)
            wa_chat = _wa.verify_selected_chat(view)
        except Exception:
            wa_contacts, wa_chat = [], {}
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
        "search_query": search_query,
        "empty_home": bool(empty_home),
        "videos": videos,
        "wa_contacts": wa_contacts,
        "wa_chat": wa_chat,
        "uia_items": uia_count,
        "ocr_items": ocr_count,
        "extracted_text_count": extracted_text_count,
        "confidence": confidence,
        "target_identified": bool(videos or wa_contacts or wa_chat.get("verified")),
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "source": obs_source,
        "cdp_available": False,
        "view": view,
    }
    _registry.update_sync(win["browser"], snap)
    return snap


def _inspect_browser_sync_safe(browser_name: str, *, allow_ocr: bool = True) -> dict:
    return _inspect_visible_browser_window(browser_name, focus_if_needed=True, allow_ocr=allow_ocr)


def _ensure_browser_foreground(bname: str, win: dict, title_hint: str = "") -> tuple[bool, dict, str]:
    """Activate and verify that the requested browser window (not REM's own window) is foreground."""
    from core import appidentity, desktop_control, screenact, screenview

    if not win.get("found"):
        desktop_control.record_diagnostics(chrome_detected=False, chrome_foreground=False)
        return False, win, f"no running {bname} window was found and CDP is unavailable; did not substitute another browser"

    if win.get("foreground") and _proc_matches_browser(win.get("app", ""), win.get("title", ""), bname):
        if not screenview.is_own_pid(int(win.get("pid", 0) or 0)):
            desktop_control.record_diagnostics(
                chrome_detected=True, chrome_foreground=True,
                foreground_app=str(win.get("app") or bname),
            )
            return True, win, ""

    # Activate using screenact.focus_app first, then full Win32 multi-process HWND focus fallback
    act = screenact.focus_app(bname)
    time.sleep(0.15)
    refreshed = _find_browser_window(bname)
    if refreshed.get("foreground") and _proc_matches_browser(refreshed.get("app", ""), refreshed.get("title", ""), bname):
        if not screenview.is_own_pid(int(refreshed.get("pid", 0) or 0)):
            desktop_control.record_diagnostics(
                chrome_detected=True, chrome_foreground=True,
                foreground_app=str(refreshed.get("app") or bname),
            )
            return True, refreshed, ""

    focus_res = appidentity.focus_window_by_app(bname, title_hint=title_hint or str(win.get("title") or ""))
    if focus_res.get("activated"):
        time.sleep(0.1)
        refreshed2 = _find_browser_window(bname)
        if refreshed2.get("foreground") or focus_res.get("target_hwnd"):
            merged = {
                **refreshed,
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
    return False, refreshed, f"I couldn't complete that action because {bname} did not become the foreground window ({act.error or focus_res.get('reason') or 'foreground lock'})."


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
    win = _find_browser_window(bname)
    if action in ("inspect", "get_url", "get_text"):
        sync = _inspect_visible_browser_window(bname, focus_if_needed=True, allow_ocr=True)
        videos = sync.get("videos") or []
        wa_contacts = sync.get("wa_contacts") or []
        wa_chat = sync.get("wa_chat") or {}
        is_wa_surface = "whatsapp" in (sync.get("title", "") + " " + sync.get("url", "")).casefold()
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
            summary_parts.append("Videos: " + "; ".join(f"{v['position']}. {v['title']}" for v in videos[:5]))
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
            return outcome.done(
                "browser_control", action=action, browser=bname, control_mode="native_windows",
                active_application="WhatsApp (Chrome)",
                unique_contact_count=len(wa_contacts),
                active_contact=wa_chat.get("active_contact", wa_contacts[0]["display_name"] if len(wa_contacts) == 1 else ""),
                selected_chat_verified=bool(wa_chat.get("verified")),
                uia_items=sync.get("uia_items", 0), ocr_items=sync.get("ocr_items", 0),
                extracted_text_count=sync.get("extracted_text_count", 0),
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
                    confidence=sync.get("confidence", "medium"),
                    source=sync.get("source", "windows_uia"), verified=True,
                )
            return outcome.unverified(
                "browser_control",
                f"CDP is unavailable on {bname} and UIA did not expose the full URL; active window title is {sync.get('title')!r}",
                browser=bname, control_mode="native_windows", cdp_available=False,
                title=sync.get("title", ""), window_pid=sync.get("pid", 0),
                window_identity=sync.get("window_identity", ""),
                timestamp=sync.get("timestamp", ""),
                extracted_text_count=sync.get("extracted_text_count", 0),
                uia_items=sync.get("uia_items", 0),
                ocr_items=sync.get("ocr_items", 0),
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
    ok_fg, win, fg_err = _ensure_browser_foreground(bname, win, title_hint=str(params.get("description") or params.get("target") or ""))
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
        sync = _inspect_visible_browser_window(bname, focus_if_needed=True, allow_ocr=True)
        sb = _wa.locate_whatsapp_search_bar(sync.get("view"))
        if sb is None:
            return outcome.failed("browser_control", "Could not locate WhatsApp's search bar.", action="search_contact", browser=bname)
        pt = screenact.Point(sb["x"], sb["y"], origin="element", label=sb["label"])
        screenact.move_cursor(pt)
        clk = screenact.click(pt)
        if not clk.ok:
            return outcome.failed("browser_control", f"clicking WhatsApp search failed: {clk.error}", action="search_contact", browser=bname)
        t_res = screenact.type_text(q, clear_first=True)
        if not t_res.ok:
            return outcome.failed("browser_control", f"typing contact query failed: {t_res.error}", action="search_contact", browser=bname)
        time.sleep(0.35)
        _registry.invalidate_sync(bname)
        after = _inspect_visible_browser_window(bname, focus_if_needed=False, allow_ocr=True)
        contacts = _wa.extract_whatsapp_contacts(after.get("view"), query=q)
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
            resolved_target=q, unique_contact_count=len(contacts),
            matched_contacts="; ".join(c["display_name"] for c in contacts[:5]),
            dispatch_status="completed", input_status="completed",
            verification_status=("verified" if contacts else "unverified"),
            verified=bool(contacts),
        )

    def _click_diag(clk_action: str, pt, *, rect_info=None) -> tuple:
        cb = screenact.get_cursor_pos() if hasattr(screenact, "get_cursor_pos") else (0, 0)
        mv = screenact.move_cursor(pt)
        if clk_action == "right_click":
            clk = screenact.right_click(pt)
        elif clk_action == "double_click":
            clk = screenact.double_click(pt)
        else:
            clk = screenact.click(pt)
        ca = screenact.get_cursor_pos() if hasattr(screenact, "get_cursor_pos") else (getattr(pt, "x", 0), getattr(pt, "y", 0))
        thwnd = int(win.get("hwnd", 0) or 0)
        tpid = int(win.get("pid", 0) or 0)
        fhwnd = thwnd
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
            "target_rect": str(rect_info or ""),
            "cursor_before": f"{cb[0]},{cb[1]}",
            "cursor_after": f"{ca[0]},{ca[1]}",
            "dispatch_status": ("completed" if clk.ok else "failed"),
            "input_status": ("completed" if clk.ok else "failed"),
        }

    if action == "select_contact":
        from core import desktop_control as _dc
        from plugins import _whatsapp_core as _wa

        _auth_ctx = _dc.current_bound_auth_context()
        desc = str(params.get("description") or params.get("text") or params.get("query") or "").strip()
        sync = _inspect_visible_browser_window(bname, focus_if_needed=True, allow_ocr=True)
        view = sync.get("view")
        ord_idx = _parse_ordinal_index(desc)
        ref = _dc.get_active_target(_auth_ctx)
        if not desc and ref and ref.get("target_type") == "contact":
            desc = str(ref.get("exact_target_name") or "").strip()
        cleaned_query = re.sub(
            r"\b(?:the|first|second|third|fourth|last|1st|2nd|3rd|4th|one|open|select|click|chat|contact)\b",
            "", desc, flags=re.IGNORECASE,
        ).strip()
        contacts = _wa.extract_whatsapp_contacts(view, query=cleaned_query)
        if not contacts and ref and ref.get("results"):
            contacts = list(ref.get("results") or ())
        _dc.record_diagnostics(unique_contact_count=len(contacts), active_application="WhatsApp (Chrome)")
        if not contacts:
            return outcome.unverified(
                "browser_control", f"No WhatsApp contact matching {desc or 'selection'!r} was found on screen.",
                action="select_contact", browser=bname, verified=False,
            )
        chosen = None
        if ord_idx is not None:
            pick = (len(contacts) - 1) if ord_idx == -1 else ord_idx
            if 0 <= pick < len(contacts):
                chosen = contacts[pick]
        if chosen is None:
            exact = [c for c in contacts if cleaned_query and c["display_name"].casefold() == cleaned_query.casefold()]
            chosen = exact[0] if len(exact) == 1 else (contacts[0] if len(contacts) == 1 else None)
        if chosen is None:
            _dc.set_active_target(
                target_type="contact", exact_target_name=contacts[0]["display_name"],
                browser=bname, tab_title=sync.get("title", ""), tab_url=sync.get("url", ""),
                result_position=1, point=contacts[0]["point"], rect=contacts[0].get("rect"),
                results=contacts, auth_context=_auth_ctx,
            )
            names = ", ".join(c["display_name"] for c in contacts[:4])
            return outcome.ask(
                "browser_control",
                f"I found {len(contacts)} contacts matching {cleaned_query or desc!r}: {names}. Which one should I open — first or second?",
                browser=bname, unique_contact_count=len(contacts), verified=False,
            )
        pt = screenact.Point(chosen["point"][0], chosen["point"][1], origin="element", label=chosen["display_name"])
        mv, clk, cdiag = _click_diag("click", pt, rect_info=chosen.get("rect"))
        if not clk.ok:
            return outcome.failed("browser_control", f"clicking contact {chosen['display_name']!r} failed: {clk.error}", action="select_contact", browser=bname, **cdiag)
        time.sleep(0.35)
        _registry.invalidate_sync(bname)
        after = _inspect_visible_browser_window(bname, focus_if_needed=False, allow_ocr=True)
        hdr = _wa.verify_whatsapp_active_chat(after.get("view"), expected_contact=chosen["display_name"], tab_title=after.get("title", ""), tab_url=after.get("url", ""))
        if not hdr.get("verified"):
            time.sleep(0.35)
            after = _inspect_visible_browser_window(bname, focus_if_needed=False, allow_ocr=True)
            hdr = _wa.verify_whatsapp_active_chat(after.get("view"), expected_contact=chosen["display_name"], tab_title=after.get("title", ""), tab_url=after.get("url", ""))
        chat_id = str(hdr.get("chat_id") or chosen.get("id") or "")
        chat_ok = bool(hdr.get("verified"))
        if chat_ok:
            _dc.set_active_target(
                target_type="contact", exact_target_name=chosen["display_name"],
                browser=bname, tab_title=after.get("title", ""), tab_url=after.get("url", ""),
                result_position=chosen.get("position", 1), point=chosen["point"], rect=chosen.get("rect"),
                confidence=chosen.get("confidence", "high"), extraction_source=chosen.get("source", ""),
                auth_context=_auth_ctx,
            )
            _dc.set_active_composition_state(
                composition_requested=False,
                composition_task_id=str(getattr(_auth_ctx, "task_id", "") or ""),
                selected_contact=chosen["display_name"],
                generated_content="",
                typing_authorized=False,
                message_sent=False,
                auth_context=_auth_ctx,
            )
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
                action="select_contact", browser=bname,
                resolved_target=chosen["display_name"], active_chat_id=chat_id,
                unique_contact_count=len(contacts), selected_chat_verified=False,
                verification_status="unverified", cursor_moved=mv.ok, click_executed=clk.ok,
                verified=False, **cdiag,
            )
        return outcome.done(
            "browser_control", action="select_contact", browser=bname,
            resolved_target=chosen["display_name"], active_contact=chosen["display_name"],
            active_chat_id=chat_id, unique_contact_count=len(contacts),
            selected_chat_verified=True,
            verification_status="verified",
            cursor_moved=mv.ok, click_executed=clk.ok, verified=True, **cdiag,
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
            rb_ok, rb_val = _wa.composer_text_value(after_v.get("view"))
            empty = rb_ok and not rb_val.strip()
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
                "I tried to clear the message box, but I couldn't confirm it is completely empty.",
                action="clear_field", browser=bname,
                composer_found=True, composer_empty=False, verification_status="unverified",
                verified=False, **cdiag,
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
        screenact.press("ctrl+a")
        screenact.press("backspace")
        return outcome.done("browser_control", action="clear_field", browser=bname, composer_empty=True, verified=clk.ok, **cdiag)

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
                any(k in desc_cf for k in ("message", "chat", "composer", "draft", "reply", "input"))
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
            sb = _locate_youtube_search_bar_native(sync, bname, desc or "YouTube search bar")
            if sb is None and "whatsapp" in (sync.get("title", "") + " " + sync.get("url", "")).casefold():
                from plugins import _whatsapp_core as _wa
                sb = _wa.locate_whatsapp_search_bar(sync.get("view"))
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
            and (
                (_resolved_ref is not None and _resolved_ref.get("target_type") == "contact")
                or any(k in desc_cf for k in ("chat", "contact"))
                or desc_cf not in {"message box", "composer"}
            )
        ):
            from plugins import _whatsapp_core as _wa
            c_query = (
                str(_resolved_ref.get("exact_target_name") or "")
                if (_resolved_ref is not None and _resolved_ref.get("target_type") == "contact")
                else re.sub(r"\b(?:open|select|click|chat|contact)\b", "", desc, flags=re.IGNORECASE).strip() or desc
            )
            wa_contacts = _wa.extract_whatsapp_contacts(sync.get("view"), query=c_query)
            if wa_contacts:
                exact = [c for c in wa_contacts if c["display_name"].casefold() == c_query.casefold()]
                chosen_c = exact[0] if len(exact) == 1 else (wa_contacts[0] if len(wa_contacts) == 1 else None)
                if chosen_c is None:
                    names = ", ".join(c["display_name"] for c in wa_contacts[:4])
                    return outcome.ask(
                        "browser_control",
                        f"I see {len(wa_contacts)} unique contacts matching {c_query!r}: {names}. Which one should I open?",
                        browser=bname, unique_contact_count=len(wa_contacts), verified=False,
                    )
                pt = screenact.Point(chosen_c["point"][0], chosen_c["point"][1], origin="element", label=chosen_c["display_name"])
                mv, clk, cdiag = _click_diag(action, pt, rect_info=chosen_c.get("rect"))
                if not clk.ok:
                    return outcome.failed("browser_control", f"clicking contact {chosen_c['display_name']!r} failed: {clk.error}", action=action, browser=bname, **cdiag)
                time.sleep(0.35)
                _registry.invalidate_sync(bname)
                after = _inspect_visible_browser_window(bname, focus_if_needed=False, allow_ocr=True)
                hdr = _wa.verify_whatsapp_active_chat(after.get("view"), expected_contact=chosen_c["display_name"], tab_title=after.get("title", ""), tab_url=after.get("url", ""))
                chat_id = str(hdr.get("chat_id") or chosen_c.get("id") or "")
                chat_ok = bool(hdr.get("verified"))
                if _dc is not None:
                    if chat_ok:
                        _dc.set_active_target(
                            target_type="contact", exact_target_name=chosen_c["display_name"],
                            browser=bname, tab_title=after.get("title", ""), tab_url=after.get("url", ""),
                            result_position=chosen_c.get("position", 1), point=chosen_c["point"], rect=chosen_c.get("rect"),
                            confidence=chosen_c.get("confidence", "high"), extraction_source=chosen_c.get("source", ""),
                            auth_context=_auth_ctx,
                        )
                    _dc.record_diagnostics(
                        active_application="WhatsApp (Chrome)",
                        active_contact=(chosen_c["display_name"] if chat_ok else "—"),
                        active_chat_id=chat_id,
                        selected_chat_verified=chat_ok,
                        unique_contact_count=len(wa_contacts),
                    )
                if not chat_ok:
                    return outcome.unverified(
                        "browser_control",
                        f"I clicked {chosen_c['display_name']}, but I couldn't confirm that the chat opened.",
                        action=action, browser=bname, task_id=_cur_task_id,
                        control_mode="native_windows", resolved_target=chosen_c["display_name"],
                        active_chat_id=chat_id, unique_contact_count=len(wa_contacts),
                        selected_chat_verified=False, verification_status="unverified",
                        cursor_moved=mv.ok, click_executed=clk.ok, verified=False, **cdiag,
                    )
                return outcome.done(
                    "browser_control", action=action, browser=bname, task_id=_cur_task_id,
                    control_mode="native_windows", resolved_target=chosen_c["display_name"],
                    active_contact=chosen_c["display_name"], active_chat_id=chat_id,
                    unique_contact_count=len(wa_contacts), selected_chat_verified=True,
                    verification_status="verified",
                    cursor_moved=mv.ok, click_executed=clk.ok, verified=True, **cdiag,
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
            if not videos:
                if sync.get("empty_home"):
                    return (
                        "I see the YouTube home page in Chrome ('Try searching to get started') "
                        "with 0 visible video results; tell me what to search for or name a specific video title."
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
                    execution_status="not_executed",
                    verification_status="unverified",
                    target_identified=False,
                    page_state="unverified",
                )
            idx = len(videos) - 1 if ordinal_idx == -1 else ordinal_idx
            if idx < 0 or idx >= len(videos):
                return f"I found {len(videos)} eligible non-ad YouTube video results in {bname}; requested position is unavailable, so nothing was clicked."
            chosen = videos[idx]
            if _dc is not None:
                _dc.set_active_target(
                    target_type="video", exact_target_name=chosen["title"],
                    browser=bname, tab_title=sync.get("title", ""), tab_url=sync.get("url", ""),
                    result_position=chosen["position"], point=chosen["point"],
                    rect=chosen.get("rect"), confidence=chosen.get("confidence", "medium"),
                    extraction_source=chosen.get("source", ""), results=videos, auth_context=_auth_ctx,
                )
            cx, cy = chosen["point"]
            pt = screenact.Point(cx, cy, origin="element", label=chosen["title"])
            mv, clk, cdiag = _click_diag(action, pt, rect_info=chosen.get("rect"))
            if not clk.ok:
                return outcome.failed(
                    "browser_control", f"real click on {chosen['title']!r} failed: {clk.error}",
                    action="play_video", resolved_target=chosen["title"], browser=bname, task_id=_cur_task_id,
                    execution_status="failed", verification_status="unverified", **cdiag,
                )
            time.sleep(0.75)
            _registry.invalidate_sync(bname)
            after = _inspect_visible_browser_window(bname, focus_if_needed=False, allow_ocr=False)
            title_changed = bool(after.get("title") and after.get("title") != sync.get("title"))
            if title_changed:
                return outcome.done(
                    "browser_control",
                    action="play_video",
                    browser=bname, task_id=_cur_task_id,
                    control_mode="native_windows",
                    resolved_target=chosen["title"], target=chosen["title"],
                    execution_status="completed", verification_status="verified",
                    cdp_available=False,
                    position=chosen["position"],
                    title=chosen["title"],
                    source=chosen["source"],
                    cursor_moved=mv.ok,
                    click_executed=clk.ok,
                    playback_verified=False,
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
        # Non-ordinal named click / title search via UIA/OCR in visible Chrome window
        view = sync.get("view")
        if view is not None and desc:
            needle = desc.casefold()
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
                        return outcome.failed(
                            "browser_control", f"real click on {v['title']!r} failed: {clk.error}",
                            action=action, resolved_target=v["title"], browser=bname, task_id=_cur_task_id,
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
                            execution_status="completed", verification_status="verified",
                            title=v["title"], cursor_moved=mv.ok, click_executed=clk.ok,
                            playback_verified=False, resulting_title=after.get("title", ""), verified=True, **cdiag,
                        )
                    return outcome.unverified(
                        "browser_control",
                        "I sent the click, Tarun-kun, but I couldn't confirm that the video opened.",
                        action=action, browser=bname, task_id=_cur_task_id,
                        control_mode="native_windows", cdp_available=False,
                        resolved_target=v["title"], target=v["title"],
                        execution_status="completed", verification_status="unverified",
                        title=v["title"], cursor_moved=mv.ok, click_executed=clk.ok,
                        playback_verified=False, verified=False, **cdiag,
                    )
            for el in getattr(view, "elements", ()) or ():
                label = str(getattr(el, "name", "") or getattr(el, "label", "") or "")
                if label and needle in label.casefold():
                    pt = screenact.Point.from_element(el)
                    mv, clk, cdiag = _click_diag(action, pt, rect_info=getattr(el, "rect", ""))
                    return outcome.done(
                        "browser_control", action=action, browser=bname, task_id=_cur_task_id,
                        control_mode="native_windows", cdp_available=False,
                        resolved_target=label, target=label,
                        execution_status=("completed" if clk.ok else "failed"),
                        verification_status=("verified" if mv.ok and clk.ok else "unverified"),
                        click_executed=clk.ok, cursor_moved=mv.ok, verified=clk.ok, **cdiag,
                    )
        return outcome.failed("browser_control", f"Could not find element {desc!r} in the active {bname} window", action=action, browser=bname, task_id=_cur_task_id, execution_status="failed", verification_status="unverified", cdp_available=False)

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

    if action in ("volume_up", "volume_down", "mute", "unmute"):
        from core import desktop_control as _dc
        key = {"volume_up": "up", "volume_down": "down", "mute": "m", "unmute": "m"}[action]
        r_v = screenact.press(key)
        _dc.record_diagnostics(last_media_status=f"{action}:{'ok' if r_v.ok else 'fail'}")
        if not r_v.ok:
            return outcome.failed("browser_control", f"{action} failed in {bname}: {r_v.error}", action=action, browser=bname)
        return outcome.unverified(
            "browser_control", f"Sent {action} key to active {bname} window.",
            action=action, browser=bname, control_mode="native_windows",
            dispatch_status="completed", input_status="completed", verification_status="unverified",
        )

    if action in ("pause_video", "resume_video"):
        from core import desktop_control as _dc
        k_res = screenact.press("k")
        _dc.record_diagnostics(last_media_status=f"{action}:{'ok' if k_res.ok else 'fail'}")
        if not k_res.ok:
            return outcome.failed("browser_control", f"media toggle failed in {bname}: {k_res.error}", browser=bname)
        return outcome.done(
            "browser_control", action=action, browser=bname,
            control_mode="native_windows", cdp_available=False,
            dispatch_status="completed", input_status="completed",
            verification_status="unverified", playback_verified=False, verified=False,
            detail=("Done, Tarun-kun. Sent play toggle to resume playback." if action == "resume_video" else "Sent play/pause key 'k' to pause the video."),
        )

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
        direction = str(params.get("direction") or "down").lower().strip()
        notches = max(1, min(15, int(params.get("amount", 4) or 4)))
        if notches > 20:
            notches = max(1, min(12, notches // 100))
        res = screenact.scroll(direction, clicks=notches)
        _dc.record_diagnostics(last_mouse_status=f"scroll:{direction}:{'ok' if res.ok else 'fail'}")
        if not res.ok:
            return outcome.failed(
                "browser_control", f"scroll failed in {bname}: {res.error}",
                action="scroll", browser=bname, target_hwnd=int(win.get("hwnd", 0) or 0),
                target_pid=int(win.get("pid", 0) or 0),
                dispatch_status="completed", input_status="failed", verification_status="unverified",
            )
        _registry.invalidate_sync(bname)
        return outcome.done(
            "browser_control", action="scroll", browser=bname, control_mode="native_windows",
            cdp_available=False, direction=direction,
            target_hwnd=int(win.get("hwnd", 0) or 0), target_pid=int(win.get("pid", 0) or 0),
            foreground_hwnd=int(win.get("hwnd", 0) or 0),
            dispatch_status="completed", input_status="completed", verification_status="verified",
            verified=True,
        )

    if action in ("back", "forward", "reload", "press", "close_tab"):
        keymap = {"back": "alt+left", "forward": "alt+right", "reload": "ctrl+r", "close_tab": "ctrl+w"}
        key = str(params.get("key") or "enter") if action == "press" else keymap[action]
        res = screenact.press(key)
        if not res.ok:
            return outcome.failed("browser_control", f"{action} failed in {bname}: {res.error}", browser=bname)
        return outcome.done("browser_control", action=action, browser=bname, control_mode="native_windows", cdp_available=False, title=win.get("title", ""), verified=True)

    return _unknown_action(action)


def _is_search_bar_query(desc: str) -> bool:
    cf = str(desc or "").strip().casefold()
    return cf in {
        "search", "search bar", "search box", "search field", "searchbox",
        "youtube search", "youtube search bar", "youtube search box",
        "youtube search field", "youtube search input",
    } or ("search" in cf and any(k in cf for k in ("bar", "box", "field", "input", "youtube")))


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
    "volume_up", "volume_down", "mute", "unmute",
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
    if action in ("get_url", "get_text", "inspect") and not _registry.has(browser):
        bname = select_browser(browser)
        if not _discover_running_cdp_endpoint(bname):
            result = _native_browser_action(action, params, bname, player=player)
            _log(player, result)
            return result
        tabs = inspect_open_chrome_tabs_local(bname)
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
            result = sess.run(sess.get_url())
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
            raw_v = params.get("seconds") or params.get("value") or params.get("position") or 10
            try:
                sec_v = float(raw_v)
            except (TypeError, ValueError):
                sec_v = 10.0
            if action == "seek_backward":
                sec_v = -abs(sec_v)
            elif action == "seek_forward":
                sec_v = abs(sec_v)
            result = sess.run(sess.seek_video(sec_v))
        elif action in ("volume_up", "volume_down", "mute", "unmute"):
            key_map = {"volume_up": "ArrowUp", "volume_down": "ArrowDown", "mute": "m", "unmute": "m"}
            result = sess.run(sess.press(key_map[action]))
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
        elif action in ("search_contact", "select_contact", "compose_message", "update_draft"):
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
                "description": "go_to | search | click | left_click | type | smart_type | clear_field | scroll | fill_form | smart_click | search_contact | select_contact | compose_message | update_draft | pause_video | resume_video | play_video | replay_video | get_text | get_url | inspect | press | new_tab | switch_tab | close_tab | screenshot | back | forward | reload | switch | list_browsers | close | close_all"
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
