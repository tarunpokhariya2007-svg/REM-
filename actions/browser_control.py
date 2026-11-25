
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

    if not exe and _OS == "Windows" and not channel:
        exe = _find_exe_windows(name)

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


_SEARCH_ENGINES: dict[str, str] = {
    "google":     "https://www.google.com/search?q=",
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


def _verify_native_browser(name: str, url: str, *, timeout_s: float | None = None) -> str:
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
            print(f"[Browser] Verified launch browser={name} windows={len(all_browser_titles)}")
            return outcome.done("browser_control", browser=name, opened=True,
                                verified=True, browser_launched=True,
                                page_state="loaded" if all_browser_titles else "open")
        print(f"[Browser] Unverified launch browser={name} (process not detected)")
        return outcome.unverified(
            "browser_control",
            "the browser was launched but has not appeared in the process list yet",
            browser=name, browser_launched=False, page_state="unverified",
            verification_step="process_presence",
        )

    if matched_title:
        page_state = _classify_page_state(matched_title)
        if page_state == "error":
            print(f"[Browser] Navigation error detected browser={name} url={url} title={matched_title!r}")
            return outcome.failed(
                "browser_control",
                f"the page reported an error ({matched_title})",
                browser=name, url=observed_url or url, title=matched_title,
                page_state="error", browser_launched=running, navigation_requested=True,
            )
        print(f"[Browser] Verified native navigation browser={name} url={observed_url or url} page_state={page_state}")
        return outcome.done(
            "browser_control",
            verified=True,
            browser=name,
            url=observed_url or url,
            title=matched_title,
            page_state=page_state,
            browser_launched=running,
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
        navigation_requested=True,
        navigation_completed=False,
        page_state="unverified",
        verification_step="active_tab_url_and_title",
    )


def _native_outcome(name: str, url: str) -> str:
    """Backward-compatible wrapper returning structured browser verification."""
    return _verify_native_browser(name, url)


def _open_native(url: str, browser_name: Optional[str]) -> str:
    """
    Opens the user's REAL browser normally — with their own profile,
    logged-in accounts and extensions. No automation attaches, so an
    about:blank tab or a blank profile NEVER shows up.
    If url is empty the browser starts with no URL (its own start page /
    session restore) — exactly as if the user had opened it themselves.
    Works on all three of Windows / macOS / Linux.
    """
    url = _normalize_url(url) if url and url.strip() else ""
    if url == "about:blank":
        url = ""

    name = None
    if browser_name:
        name = _ALIASES.get(browser_name.lower().strip(), browser_name.lower().strip())
    elif not url:
        # No URL → only a window will open; needs the default browser's exe
        name = _detect_default_browser()

    # Specific browser → launch its own executable, exactly like the user would.
    if name:
        if _OS == "Darwin":
            app = _MAC_APP_NAMES.get(name)
            if app:
                cmd = ["open", "-a", app] + ([url] if url else [])
                try:
                    subprocess.run(cmd, check=True, timeout=10)
                    return _native_outcome(name, url)
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
                subprocess.Popen(
                    [exe] + _launch_flags(name) + ([url] if url else []),
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
                return _native_outcome(name, url)
            except Exception as e:
                print(f"[Browser] Native launch failed for {name}: {e}")
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
        # "real" when the user's own browser profile was opened, "separate"
        # when their browser was already running and a private automation
        # profile had to be used instead. The difference is every sign-in they
        # have, so it is reported rather than only printed.
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
                    print(f"[Browser] Attached to explicitly configured local {self.browser_name.title()} CDP session")
                    return
                except Exception as exc:
                    if browser is not None:
                        try:
                            await browser.close()
                        except Exception:
                            pass
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
        # If somehow page got closed, open a fresh one
        if self._page is None or self._page.is_closed():
            self._page = await self._context.new_page()
            await asyncio.sleep(0.2)
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
        base = _SEARCH_ENGINES.get(engine.lower(), _SEARCH_ENGINES["google"])
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
                    search = (meta.get("type") == "search" or meta.get("role") == "searchbox"
                              or any(x in text for x in ("search", "query", "address")))
                    search_submit = search and meta.get("tag") in ("INPUT", "TEXTAREA")
                    chat = any(x in text for x in ("message", "reply", "chat", "comment"))
                    if chat:
                        return "I will not submit a message or comment through a generic Enter key. Use the explicit send action and confirm it."
                    if ((meta.get("editable") and not search)
                            or (meta.get("tag") in ("INPUT", "TEXTAREA")
                                and not search)):
                        return "Enter could submit this form; use an explicit named button, which will request confirmation when consequential."
            await page.keyboard.press(key)
            if search_submit:
                await asyncio.sleep(0.25)
                actual_url = page.url
                if actual_url != before_url:
                    return outcome.done(
                        "browser_control", action="search_submit", verified=True,
                        navigation_confirmed=True, actual_url=actual_url)
                return outcome.unverified(
                    "browser_control", "Enter was sent to the search field, but no URL change was confirmed",
                    actual_url=actual_url)
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
        ordinal_result = re.search(r"\b(first|second|third|last)\s+results?\b",
                                   str(description or ""), re.IGNORECASE)
        if ordinal_result:
            try:
                host = (urlsplit(page.url).hostname or "").casefold().rstrip(".")
            except Exception:
                host = ""
            if host == "youtube.com" or host.endswith(".youtube.com"):
                return await self._click_youtube_video_ordinal(
                    page, ordinal_result.group(1).lower(), before)
            return await self._click_ordinal_result(
                page, ordinal_result.group(1).lower(), before)
        ordinal = re.search(r"\b(first|second|third|last)\s+videos?\b",
                            str(description or ""), re.IGNORECASE)
        try:
            host = (urlsplit(page.url).hostname or "").casefold().rstrip(".")
        except Exception:
            host = ""
        if ordinal and (host == "youtube.com" or host.endswith(".youtube.com")):
            return await self._click_youtube_video_ordinal(page, ordinal.group(1), before)
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

    async def _click_youtube_video_ordinal(self, page, ordinal: str, before: tuple) -> str:
        """Select a real visible YouTube video card, excluding ads and chrome."""
        try:
            cards = await page.locator("ytd-video-renderer, ytd-rich-item-renderer").evaluate_all("""els => {
              const seen = new Set();
              return els.flatMap(card => {
                const r = card.getBoundingClientRect(), s = getComputedStyle(card);
                if (r.width < 2 || r.height < 2 || s.display === 'none' || s.visibility === 'hidden') return [];
                if (card.hasAttribute('is-ad') || card.querySelector('ytd-ad-slot-renderer, #ad-badge, [id*=ad-badge]')) return [];
                // Prefer the actual result-title anchor. A card's first watch
                // link in DOM order is often its aria-hidden thumbnail, whose
                // text is only a duration overlay such as "Now playing".
                const a = card.querySelector('a#video-title[href*="/watch"]')
                    || card.querySelector('a[href*="/watch?v="]:not([aria-hidden="true"])');
                if (!a) return [];
                const ar = a.getBoundingClientRect();
                if (ar.width < 2 || ar.height < 2) return [];
                const u = new URL(a.href, location.href);
                const id = u.pathname === '/watch' ? u.searchParams.get('v') : null;
                const title = (a.getAttribute('title')
                    || a.querySelector('yt-formatted-string')?.innerText
                    || a.innerText || a.textContent || '').trim();
                if (!id || !title || seen.has(id)) return [];
                seen.add(id);
                return [{id, title: title.slice(0,160)}];
              });
            }""")
            index = {"first": 0, "second": 1, "third": 2}.get(ordinal)
            if ordinal == "last":
                index = len(cards) - 1
            if index is None or index < 0 or index >= len(cards):
                return f"I found {len(cards)} visible non-ad YouTube video results; {ordinal} is unavailable, so nothing was clicked."
            selected = cards[index]
            video_id = str(selected.get("id", ""))
            title = str(selected.get("title", ""))
            if not re.fullmatch(r"[A-Za-z0-9_-]{6,20}", video_id) or not title:
                return "The selected result did not have a verifiable video identity; nothing was clicked."
            # Use the video identity from the inspected result, never a generic
            # page text/role match which could resolve to the YouTube logo.
            loc = page.locator(f'a#video-title[href*="watch?v={video_id}"]')
            count = await loc.count()
            if count != 1 or not await loc.is_visible() or not await loc.is_enabled():
                return "The inspected YouTube video target is no longer unique and visible; nothing was clicked."
            await loc.click(timeout=8_000)
            try:
                await page.wait_for_url(re.compile(r"https?://(?:www\\.)?youtube\\.com/watch\\?.*\\bv=" + re.escape(video_id) + r"(?:&|$)"), timeout=6_000)
            except Exception:
                pass
            current = urlsplit(page.url)
            opened_id = next((value for key, value in parse_qsl(current.query) if key == "v"), "")
            if current.path == "/watch" and opened_id == video_id:
                return f"Opened and verified the {ordinal} YouTube video: {title!r}."
            return f"Clicked the {ordinal} video target {title!r}, but the resulting page did not verify as that video."
        except Exception as exc:
            return outcome.failed("browser_control", f"YouTube video targeting failed safely: {type(exc).__name__}: {exc}")

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

    async def smart_type(self, description: str, text: str) -> str:
        page = await self._get_page()
        if not await self._page_visible_for_action(page):
            return outcome.unverified(
                "browser_control", "browser page is hidden; no text was entered")
        generic_search = str(description or "").strip().casefold() in {
            "search", "search bar", "search box", "search field", "searchbox",
        }
        searchboxes = page.get_by_role("searchbox")
        if generic_search:
            try:
                count = await searchboxes.count()
                if count > 1:
                    return "Multiple search boxes are visible; no text was entered. Name the site or field."
                if count == 1:
                    if not await searchboxes.is_visible() or not await searchboxes.is_enabled():
                        return "The search box is not visible and enabled; no text was entered."
                    await searchboxes.fill(str(text))
                    if await searchboxes.input_value() == str(text):
                        return outcome.done("browser_control", field="search box",
                                            method="role=searchbox", verified=True)
                    return outcome.unverified(
                        "browser_control", "search box text did not read back exactly")
            except Exception as exc:
                return outcome.failed(
                    "browser_control",
                    f"the search box changed or became unavailable ({type(exc).__name__}); no result is confirmed")
        candidates = [
            ("placeholder", page.get_by_placeholder(description, exact=False)),
            ("label",       page.get_by_label(description, exact=False)),
            ("role",        page.get_by_role("textbox", name=description)),
            ("searchbox",   page.get_by_role("searchbox")),
            ("combobox",    page.get_by_role("combobox", name=description)),
        ]
        for method, loc in candidates:
            try:
                count = await loc.count()
                if count == 0:
                    continue
                if count > 1:
                    return f"Multiple inputs match {description!r}; no text was entered."
                el = loc
                if not await el.is_editable():
                    continue
                if (await el.get_attribute("type") or "").casefold() == "password":
                    return "Password fields are protected. Enter credentials yourself."
                await el.fill(str(text))
                value = await el.input_value()
                if value == str(text):
                    return outcome.done("browser_control", field=description,
                                        method=method, verified=True)
                return outcome.unverified("browser_control",
                                          f"text in {description!r} did not read back exactly")
            except Exception:
                continue
        return f"Could not find input: '{description}'"

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
        return outcome.done(
            "browser_control", verified=True, browser=self.browser_name,
            url=url, title=title, page_state=state, ready_state=ready,
        )

    async def close_tab(self) -> str:
        page = self._page
        if page and not page.is_closed():
            ctx   = page.context
            await page.close()
            pages = ctx.pages
            self._page = pages[-1] if pages else None
            return "Tab closed."
        return "No active tab to close."

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
    """Manages all active browser sessions."""

    def __init__(self):
        self._sessions:        dict[str, _BrowserSession] = {}
        self._active_browser:  str                        = ""
        self._lock             = threading.Lock()
        self._last_native_url: str                        = ""

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

    def get(self, browser_name: str | None = None) -> _BrowserSession:
        if not browser_name:
            browser_name = self._active_browser or _detect_default_browser()
        browser_name = _ALIASES.get(browser_name.lower().strip(), browser_name.lower().strip())
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

# Every action this tool answers to. One list, used by the failure message and
# by nothing else — it exists so that being told "that is not an action" also
# tells you what the actions ARE.
_ACTIONS = (
    "go_to", "search", "new_tab", "switch_tab", "close_tab", "inspect",
    "click", "smart_click", "type", "smart_type", "fill_form", "press",
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
        or action in ("close", "close_all", "close_tab")
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
        if _registry.has(browser):
            sess = _registry.get(browser)
            try:
                if action == "search":
                    result = sess.run(sess.search(params.get("query", ""),
                                                  params.get("engine", "google")))
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
            base    = _SEARCH_ENGINES.get(params.get("engine", "google").lower(),
                                          _SEARCH_ENGINES["google"])
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

    # Native read-only inspection fallback when no Playwright/CDP session is attached yet
    if action in ("get_url", "get_text", "inspect") and not _registry.has(browser):
        bname = browser or _detect_default_browser()
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
                "no controllable browser tab or active Chrome window title was found",
                browser=bname, page_state="unverified", verification_step="open_browser_window",
            )
        _log(player, result)
        return result

    # ── Interactive actions (click/type/read…) ───────────────────────────────
    # These require a physically controllable browser; the automation window
    # only opens here, and as soon as it opens it goes to the user's last
    # navigated page — it doesn't sit on a blank page.
    try:
        sess = _registry.get(browser)
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

        if action == "click":
            result = sess.run(sess.click(params.get("selector"), params.get("text")))
        elif action == "type":
            result = sess.run(sess.type_text(
                params.get("selector"), params.get("text", ""), params.get("clear_first", True)))
        elif action == "scroll":
            result = sess.run(sess.scroll(params.get("direction", "down"), int(params.get("amount", 500))))
        elif action == "fill_form":
            result = sess.run(sess.fill_form(params.get("fields", {})))
        elif action == "smart_click":
            result = sess.run(sess.smart_click(params.get("description", "")))
        elif action == "smart_type":
            result = sess.run(sess.smart_type(params.get("description", ""), params.get("text", "")))
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
                "description": "go_to | search | click | type | scroll | fill_form | smart_click | smart_type | get_text | get_url | inspect | press | new_tab | switch_tab | close_tab | screenshot | back | forward | reload | switch | list_browsers | close | close_all"
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
