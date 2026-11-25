"""Shared state and the single action lock for desktop control."""
from __future__ import annotations

import threading
import re
import unicodedata
from contextlib import contextmanager
from urllib.parse import urlsplit

_lock = threading.RLock()
_action_lock = threading.Lock()
_cancel_lock = threading.Lock()
_active_cancellers: set = set()
_stopped = threading.Event()
_live_stop = threading.Event()
_live_thread: threading.Thread | None = None
_paused = False
_mode = "ON-DEMAND"
_persistent_observation = False
_state = {
    "mode": _mode, "screen_access": False, "controlling": False,
    "active_app": "—", "current_action": "None", "last_action": "None",
    "verification": "Not run", "mouse_keyboard": False,
    "paused": False, "stopped": False, "persistent_observation": False,
}
_listeners: list = []


def _tokens(value) -> set[str]:
    text = unicodedata.normalize("NFKD", str(value or "").casefold())
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return set(re.findall(r"[a-z0-9]+", text))


def _negates_action(request: str, action: str) -> bool:
    """Reject an action the direct request explicitly forbids."""
    aliases = {
        "click": r"(?:left\s+)?click", "left_click": r"(?:left\s+)?click",
        "smart_click": r"(?:left\s+)?click", "right_click": r"right\s+click|click",
        "double_click": r"double\s+click|click", "type": r"type|write|enter",
        "type_text": r"type|write|enter", "smart_type": r"type|write|enter",
        "press": r"press|hit", "press_key": r"press|hit",
        "hotkey": r"press|shortcut|hotkey", "move": r"move|place", "hover": r"hover|move",
        "scroll": r"scroll", "drag": r"drag", "close": r"close|quit|exit",
        "close_app": r"close|quit|exit", "send_message": r"send|message",
        "screen_click": r"click", "screen_find": r"find|locate",
    }
    verbs = aliases.get(action)
    if not verbs:
        return False
    return bool(re.search(
        rf"\b(?:do\s+not|don't|dont|never|not|without|refrain\s+from|stop)"
        rf"(?:\s+\w+){{0,4}}\s+(?:{verbs})\b",
        request, flags=re.IGNORECASE))


def _is_screen_observation_request(value: str) -> bool:
    """Recognize a direct, read-only request to inspect the visible desktop.

    Screen tool arguments are questions to answer, not text to type. This
    classifier deliberately accepts common Hindi (Devanagari and transliterated)
    and natural English verification/observation requests; it grants only
    observation, never actuation.
    """
    text = unicodedata.normalize("NFKC", str(value or "")).casefold().strip()
    english_phrases = (
        "screen", "screenshot", "display", "desktop", "visible", "what is on", "what's on",
        "look at", "check my", "describe", "which application", "which app",
        "what application", "what app", "current window", "current test window",
        "read the current window", "read current window", "read the error",
        "terminal", "editor", "react project", "what is written",
        "can you see", "do you see", "see whether", "see if", "check whether",
        "check if", "what is open", "what's open", "currently open",
        "active window", "active browser", "active tab", "browser tab",
        "loaded", "logged in", "showing on",
    )
    if any(phrase in text for phrase in english_phrases):
        return True
    if re.search(
        r"\b(?:is|are|whether)\b.{1,60}\b(?:open|loaded|running|visible|showing|logged\s+in)\b",
        text,
    ):
        return True
    hindi_phrases = (
        "स्क्रीन", "पर्दे पर", "क्या दिख", "क्या दिखाई", "दिख रहा", "दिखाओ",
        "देखो", "देखिए", "क्या लिखा", "पढ़ो", "पढ़ो", "त्रुटि", "खिड़की",
        "विंडो", "डेस्कटॉप", "कौन सा ऐप", "कौन-सा ऐप", "कौन सा अनुप्रयोग",
        "कौन सा प्रोग्राम", "डिस्प्ले",
        "मेरी स्क्रीन", "कैसा दिख",
    )
    if any(phrase in text for phrase in hindi_phrases):
        return True
    romanized_hindi = (
        "screen par kya", "meri screen dekho", "screen dekho", "kya dikh",
        "kya likha", "kaun sa app", "kaunsa app", "display par kya",
        "error padho", "khidki mein kya", "desktop par kya",
    )
    return any(phrase in text for phrase in romanized_hindi)


def _is_explicit_camera_request(value: str) -> bool:
    text = unicodedata.normalize("NFKC", str(value or "")).casefold().strip()
    negative = (
        "close camera", "stop camera", "turn off camera", "camera off",
        "close webcam", "stop webcam", "turn off webcam", "webcam off",
        "कैमरा बंद", "वेबकैम बंद", "camera band", "webcam band",
    )
    if any(phrase in text for phrase in negative):
        return False
    positive = (
        "look at camera", "check camera", "look at webcam", "look at the webcam",
        "check webcam",
        "look through webcam",
        "show camera", "turn on camera", "open webcam", "camera on",
        "कैमरा देख", "वेबकैम देख", "कैमरा चालू", "वेबकैम चालू",
        "camera dekho", "webcam dekho", "camera chalu",
    )
    return any(phrase in text for phrase in positive)


def subscribe(callback) -> None:
    with _lock:
        if callback not in _listeners:
            _listeners.append(callback)
        snapshot = dict(_state)
    try:
        callback(snapshot)
    except Exception:
        pass


def snapshot() -> dict:
    with _lock:
        return dict(_state)


def authorize_user_request(user_request: str, tool: str, args: dict, *,
                           source: str = "untrusted",
                           auth_context=None) -> tuple[bool, str]:
    """Limit computer actions to the human's latest request, not page text.

    The Live model can see untrusted page contents. Tool output is never used as
    this input; main.py supplies only the input-audio transcript received from
    the user. When an active, task-scoped AutonomousSession is bound to
    ``auth_context``, routine permitted actions within its explicit boundaries
    are evaluated against that session rather than single-turn word matching.
    """
    request = str(user_request or "").casefold().strip()
    trusted_sources = {"voice", "typed", "dashboard", "ui"}
    if source not in trusted_sources:
        return False, "A direct user request from a trusted input channel is required."
    if not request:
        return False, "I need a direct user request before accessing or controlling the desktop."
    words = _tokens(request)
    t = str(tool or "").lower()
    if t == "desktop_ui":
        t = "screen_agent"
    action = str((args or {}).get("action") or "").lower().replace("-", "_")
    if not action and t in ("open_app", "send_message", "screen_process"):
        action = t
    if not action and t == "computer_settings":
        try:
            from actions.computer_settings import _detect_action
            action = str(_detect_action(str((args or {}).get("description") or ""))
                         .get("action") or "")
        except Exception:
            action = ""

    if _negates_action(request, action or t):
        return False, "Your request explicitly forbids that action; nothing was done."

    # Task-scoped trusted authorization (Overnight Autonomous Mode)
    session_id = getattr(auth_context, "autonomous_session_id", "") if auth_context is not None else ""
    if session_id:
        from core import autonomous_mode
        sess = autonomous_mode.get_session(session_id)
        if sess is None:
            return False, "The autonomous task session is missing or no longer valid."
        if _negates_action(sess.objective.casefold(), action or t):
            return False, "Your approved task forbids that action; nothing was done."
        allowed, cat, reason = autonomous_mode.evaluate_tool_access(sess, tool, args)
        if allowed:
            return True, ""
        # Sensitive operations or out-of-scope operations must NEVER fall back to
        # silent bypass; record/refuse cleanly.
        if cat in {
            "external_communication", "destructive_action", "security_or_system_action",
            "software_install", "consequential_desktop_action", "consequential_ui_action",
            "privileged_or_install_command", "consequential_mutation", "outside_task_scope",
            "permission_category_denied", "network_disabled", "camera_not_authorized",
            "expired_or_revoked",
        }:
            return False, reason

    # The existing screen_agent plugin can sequence bounded native steps or
    # route to selected tools. Authorize each concrete step against the same
    # user's transcript before the plugin is allowed to start.
    if t == "screen_agent" and action == "task":
        steps = (args or {}).get("steps")
        if not isinstance(steps, list) or not steps or len(steps) > 8:
            return False, "The desktop task is not a valid bounded sequence."
        for step in steps:
            if not isinstance(step, dict):
                return False, "A desktop task step is invalid; no actions were run."
            allowed, reason = authorize_user_request(
                request, "desktop_ui", step, source=source, auth_context=auth_context)
            if not allowed:
                return False, reason
        return True, ""
    if t == "screen_agent" and action == "tool":
        name = str((args or {}).get("tool_name") or "")
        if name not in {"open_app", "browser_control", "web_search", "file_controller"}:
            return False, "That routed tool is not authorized for desktop tasks."
        if name == "web_search":
            return True, ""
        return authorize_user_request(
            request, name, (args or {}).get("tool_parameters") or {},
            source=source, auth_context=auth_context)

    if t == "screen_process":
        angle = str((args or {}).get("angle") or "screen").strip().casefold()
        if angle == "camera":
            allowed = _is_explicit_camera_request(request)
            print(f"[Auth] {'ALLOW' if allowed else 'DENY'} tool=screen_process action=camera source={source}")
            return (allowed, "A direct camera request is required." if not allowed else "")
        if angle != "screen":
            print(f"[Auth] DENY tool=screen_process action={angle} source={source} cat=invalid_angle")
            return False, "Screen observation only accepts angle='screen' or an explicitly requested camera."
        try:
            from memory.config_manager import get_local_screen_observation_enabled
            if not get_local_screen_observation_enabled():
                print(f"[Auth] DENY tool=screen_process action=screen source={source} cat=revoked_authorization")
                return False, "Local screen observation is disabled or revoked in COMPUTER USE settings."
        except Exception:
            pass
        allowed = _is_screen_observation_request(request)
        print(f"[Auth] {'ALLOW' if allowed else 'DENY'} tool=screen_process action=screen source={source}")
        return (allowed, "A direct screen-observation request is required." if not allowed else "")

    if t == "screen_agent" and action == "observe":
        try:
            from memory.config_manager import get_local_screen_observation_enabled
            if not get_local_screen_observation_enabled():
                print(f"[Auth] DENY tool=desktop_ui action=observe source={source} cat=revoked_authorization")
                return False, "Local screen observation is disabled or revoked in COMPUTER USE settings."
        except Exception:
            pass
        allowed = _is_screen_observation_request(request)
        print(f"[Auth] {'ALLOW' if allowed else 'DENY'} tool=desktop_ui action=observe source={source}")
        return (allowed, "A direct screen-observation request is required." if not allowed else "")

    intents = {
        "screen_agent": {
            "observe": {"screen", "desktop", "window", "application", "look", "see", "visible", "show", "search", "open", "click", "type", "find", "scroll", "switch", "focus", "read", "inspect", "check", "whether", "verify", "confirm", "chrome", "browser", "whatsapp", "youtube", "cursor", "mouse", "put", "place"},
            "find": {"find", "locate", "search", "where", "identify", "look", "open", "see", "check", "whether", "visible", "whatsapp", "youtube", "chrome", "tab", "cursor", "bar"},
            "click": {"click", "select", "choose", "open", "press", "put", "focus", "search", "bar", "tap"},
            "right_click": {"right", "click", "context"},
            "double_click": {"double", "click"},
            "select": {"select", "choose"},
            "toggle": {"toggle", "enable", "disable", "turn", "switch"},
            "expand": {"expand", "open"},
            "collapse": {"collapse", "close"},
            "hover": {"hover", "move", "cursor", "mouse", "point", "put", "place", "click", "focus", "search"},
            "move": {"move", "cursor", "mouse", "point", "put", "place", "control", "position", "click", "focus", "search"},
            "move_relative": {"move", "cursor", "mouse", "left", "right", "up", "down", "point", "nudge"},
            "type": {"type", "enter", "write", "search", "fill", "paste", "put"},
            "press": {"press", "enter", "keyboard", "key", "shortcut", "search", "query", "submit", "close", "tab"},
            "scroll": {"scroll", "page", "down", "up"},
            "focus_window": {"focus", "switch", "bring", "window", "open", "chrome", "youtube"},
            "set_mode": {"mode", "computer", "control", "assist", "cursor"},
            "pause": {"pause", "stop", "halt"},
            "resume": {"resume", "continue", "unpause"},
            "stop": {"stop", "halt", "emergency"},
            "emergency_stop": {"stop", "halt", "emergency"},
        },
        "browser_control": {
            "go_to": {"open", "go", "visit", "navigate", "website", "url", "browser", "chrome", "youtube", "google"},
            "search": {"search", "find", "look", "google", "bing", "web", "youtube", "query"},
            "new_tab": {"tab", "open", "new"},
            "switch_tab": {"tab", "switch", "open", "focus", "whatsapp", "youtube", "chrome", "browser", "current"},
            "inspect": {"inspect", "open", "see", "check", "whether", "verify", "confirm", "chrome", "browser", "whatsapp", "youtube", "page", "site", "website", "tab", "status", "loaded", "login", "url", "read", "search", "cursor"},
            "smart_click": {"click", "open", "select", "choose", "press", "search", "focus", "bar", "put", "cursor"},
            "click": {"click", "open", "select", "choose", "focus", "search", "bar"},
            "smart_type": {"type", "enter", "search", "fill", "write", "put"},
            "type": {"type", "enter", "search", "fill", "write", "put"},
            "fill_form": {"fill", "form", "enter", "complete"},
            "get_text": {"read", "summarize", "page", "website", "screen", "text", "search", "open", "check", "see", "whether", "verify", "confirm", "chrome", "browser", "whatsapp", "youtube", "tab", "loaded"},
            "get_url": {"url", "address", "website", "page", "where", "open", "check", "see", "whether", "verify", "confirm", "chrome", "browser", "whatsapp", "youtube", "tab", "loaded"},
            "list_browsers": {"browser", "chrome", "edge", "open", "see", "check", "whether", "verify", "which"},
            "scroll": {"scroll", "down", "up", "page"},
            "press": {"press", "enter", "keyboard", "key", "search", "submit", "go"},
            "back": {"back", "previous"}, "forward": {"forward", "next"},
            "close_tab": {"close", "tab", "shut", "dismiss"}, "reload": {"reload", "refresh"},
            "switch": {"switch", "use", "chrome", "edge", "firefox", "browser"},
            "close": {"close", "quit", "exit"},
            "close_all": {"close", "quit", "exit", "all"},
        },
        "computer_control": {
            "click": {"click", "tap", "focus"}, "left_click": {"click"}, "double_click": {"double", "click"},
            "right_click": {"right", "click"}, "type": {"type", "write", "enter", "search"},
            "smart_type": {"type", "write", "enter", "search"}, "press": {"press", "key", "enter", "search"},
            "hotkey": {"shortcut", "hotkey", "keyboard", "press", "close", "tab"},
            "scroll": {"scroll", "down", "up"},
            "move": {"move", "cursor", "mouse", "put", "place", "point", "control"},
            "drag": {"drag"}, "screenshot": {"screenshot", "screen", "capture"},
            "screen_find": {"find", "locate", "screen", "where"},
            "screen_click": {"click", "screen"}, "focus_window": {"focus", "switch", "window", "chrome"},
            "close_app": {"close", "quit", "exit"},
        },
        "open_app": {"open_app": {"open", "launch", "start"}},
        "send_message": {"send_message": {"send", "message", "text"}},
        "screen_process": {"screen_process": {"screen", "screenshot", "look", "see", "visible", "what", "camera", "webcam"}},
        "computer_settings": {
            "volume_set": {"volume", "sound", "audio"},
            "volume_up": {"volume", "sound", "louder", "up"},
            "volume_down": {"volume", "sound", "quieter", "down"},
            "mute": {"mute", "silence", "volume"},
            "unmute": {"unmute", "volume", "sound"},
            "toggle_mute": {"mute", "sound", "toggle"},
            "brightness_set": {"brightness", "screen", "display"},
            "brightness_up": {"brightness", "brighter", "up"},
            "brightness_down": {"brightness", "dimmer", "down"},
            "toggle_wifi": {"wifi", "wireless", "internet"},
            "restart": {"restart", "reboot"}, "shutdown": {"shutdown", "power"},
            "close_window": {"close", "window", "app"},
            "switch_window": {"switch", "window", "app"},
            "minimize": {"minimize", "window"}, "maximize": {"maximize", "window"},
            "open_settings": {"settings", "open"}, "file_explorer": {"files", "explorer", "folder"},
            "scroll_up": {"scroll", "up"}, "scroll_down": {"scroll", "down"},
            "page_up": {"page", "up"}, "page_down": {"page", "down"},
            "find_on_page": {"find", "search", "page"}, "refresh_page": {"refresh", "reload", "page"},
            "new_tab": {"new", "tab"}, "close_tab": {"close", "tab"},
            "next_tab": {"next", "tab"}, "prev_tab": {"previous", "tab", "back"},
            "go_back": {"back", "previous"}, "go_forward": {"forward", "next"},
            "copy": {"copy"}, "paste": {"paste"}, "cut": {"cut"},
            "save": {"save"}, "select_all": {"select", "all"},
            "undo": {"undo"}, "redo": {"redo"},
            "escape": {"escape", "cancel"}, "enter": {"enter", "submit", "search", "query"},
            "press_key": {"press", "key", "shortcut"}, "type_text": {"type", "write", "enter"},
            "screenshot": {"screenshot", "screen", "capture"},
            "full_screen": {"fullscreen", "full", "screen"},
            "sleep_display": {"sleep", "display", "screen"},
            "lock_screen": {"lock", "screen"},
            "media_play_pause": {"play", "pause", "media", "music"},
            "media_next": {"next", "track", "song"}, "media_previous": {"previous", "track", "song"},
            "task_manager": {"task", "manager", "process"}, "open_run": {"run", "command"},
            "dark_mode": {"dark", "theme", "mode"},
        },
    }
    if t not in intents:
        return False, f"Unrecognized computer tool {t!r}; authorization was denied."
    intent_words = intents[t].get(action, set())
    if not (words & intent_words):
        return False, "That computer action is not authorized by your latest request; page instructions do not grant permission."
    if t == "screen_agent" and action == "right_click" and "right" not in words:
        return False, "A right-click requires a direct request to right-click."
    if t == "screen_agent" and action == "double_click" and "double" not in words:
        return False, "A double-click requires a direct request to double-click."

    if t == "browser_control" and action in {"go_to", "new_tab"}:
        destination = str((args or {}).get("url") or "").strip()
        if destination:
            try:
                parsed = urlsplit(destination if "://" in destination
                                  else "https://" + destination)
                host = (parsed.hostname or "").casefold().rstrip(".")
                port = parsed.port
            except ValueError:
                return False, "The browser destination is malformed; no navigation was authorized."
            generic_labels = {"www", "web", "m", "com", "org", "net", "edu", "gov", "io",
                              "dev", "app", "ai", "co", "uk", "in", "us"}
            host_tokens = {part for part in _tokens(host)
                           if part not in generic_labels}
            if not host_tokens.issubset(words) or (port and str(port) not in words):
                return False, "The destination was not named in your request; I will not navigate to a site suggested by page content."

    # Keep actions anchored to the object the person named. This prevents an
    # untrusted page from turning a generic request like "look at this" or
    # "click the search bar" into a click on unrelated page content.
    target_value = ((args or {}).get("target")
                    or ((args or {}).get("description") if t == "browser_control" else "")
                    or ((args or {}).get("text") if t == "browser_control" and action in {"click", "smart_click"} else ""))
    target = str(target_value or "").strip()
    if target and ((t == "screen_agent" and action in {"find", "click", "right_click", "double_click", "select", "toggle", "expand", "collapse", "hover", "move", "type", "focus_window", "scroll"})
                   or (t == "browser_control" and action in {"smart_click", "click", "smart_type", "type"})
                   or (t == "computer_control" and action == "close_app")):
        target_words = {w for w in _tokens(target)
                        if len(w) > 2 and w not in {"button", "field", "bar", "box", "input", "area", "window", "control"}}
        if target_words and not (target_words & words):
            return False, "The target was not named in your request. I will not act on a target supplied by page content."

    if (t == "open_app" or (t == "computer_control" and action == "close_app")):
        app = str((args or {}).get("app_name") or "").strip()
        app_words = {w for w in _tokens(app)
                     if len(w) > 2 and w not in {"application", "program"}}
        if app_words and not (app_words & words):
            return False, "The application was not named in your request. I will not open an application suggested by page content."

    if t == "browser_control" and action == "go_to":
        import urllib.parse
        url = str((args or {}).get("url") or (args or {}).get("text") or "").strip()
        host = urllib.parse.urlparse(url if "://" in url else "https://" + url).hostname or ""
        host_words = {w for w in _tokens(host)
                      if len(w) > 2 and w not in {"www", "web", "m", "com", "org", "net", "co"}}
        if host_words and not (host_words & words):
            return False, "The destination was not named in your request. I will not navigate to a site suggested by page content."

    # Never let a page cause arbitrary text to be typed or sent. Every content
    # word must have been supplied by the user, with small language-neutral
    # connector words ignored.
    payload = ""
    payload_keys = ["text", "query", "message_text"]
    if t == "computer_settings" and action in ("type_text", "open_run"):
        payload_keys.append("value")
    for key in payload_keys:
        if (args or {}).get(key):
            payload = str(args[key])
            break
    if payload:
        content_words = {w for w in _tokens(payload)
                         if len(w) > 2 and w not in {"the", "and", "for", "with", "please"}}
        if content_words and not content_words.issubset(words):
            return False, "The text to enter was not provided in your request. I will not copy content from a page into another field."

    fields = (args or {}).get("fields")
    if isinstance(fields, dict):
        for label, value in fields.items():
            label_words = {w for w in _tokens(label) if len(w) > 2}
            value_words = {w for w in _tokens(value) if len(w) > 2}
            if not label_words.issubset(words) or not value_words.issubset(words):
                return False, "Form data or field was not explicitly named in your request. I will not fill it."

    # The recipient is especially important: vague pronouns and page-provided
    # names are not a destination for messages.
    recipient = str((args or {}).get("receiver") or "").strip()
    if recipient:
        rec_words = {w for w in _tokens(recipient) if len(w) > 2}
        if rec_words and not rec_words.issubset(words):
            return False, "The intended recipient was not named in your request. No message was sent."
    return True, ""


def _update(**values) -> dict:
    with _lock:
        _state.update(values)
        result = dict(_state)
        listeners = tuple(_listeners)
    for callback in listeners:
        try:
            callback(result)
        except Exception:
            pass
    return result


def set_mode(mode: str) -> dict:
    global _mode, _live_thread
    value = str(mode or "").strip().upper().replace("_", "-")
    allowed = {"ON-DEMAND", "LIVE ASSIST", "COMPUTER CONTROL"}
    if value not in allowed:
        raise ValueError("mode must be ON-DEMAND, LIVE ASSIST, or COMPUTER CONTROL")
    with _lock:
        _mode = value
        current = _live_thread
        needs_observer = value == "LIVE ASSIST" or _persistent_observation
        if needs_observer and (current is None or not current.is_alive()):
            _live_stop.clear()
            _live_thread = threading.Thread(target=_live_observer, daemon=True,
                                            name="rem-live-desktop-observer")
            _live_thread.start()
        elif not needs_observer:
            _live_stop.set()
    return _update(mode=value,
                   screen_access=needs_observer and not should_stop(),
                   persistent_observation=_persistent_observation,
                   current_action=("Local desktop observation" if needs_observer else "Ready"))


def set_persistent_observation(enabled: bool) -> dict:
    """Enable/disable persistent LOCAL UIA reads after explicit UI consent.

    This never captures screenshots or sends screen data to a model.
    """
    global _persistent_observation, _live_thread
    with _lock:
        _persistent_observation = bool(enabled)
        needs_observer = _persistent_observation or _mode == "LIVE ASSIST"
        current = _live_thread
        if needs_observer and not _paused and not _stopped.is_set() and (
                current is None or not current.is_alive()):
            _live_stop.clear()
            _live_thread = threading.Thread(target=_live_observer, daemon=True,
                                            name="rem-live-desktop-observer")
            _live_thread.start()
        elif not needs_observer:
            _live_stop.set()
        active = needs_observer and not _paused and not _stopped.is_set()
    return _update(persistent_observation=_persistent_observation,
                   screen_access=active,
                   current_action="Local desktop observation" if active else "Ready")


def _live_observer() -> None:
    """Opt-in local accessibility polling with a slower interval when stable."""
    previous = ""
    delay = 2.0
    while not _live_stop.is_set() and not _stopped.is_set():
        with _lock:
            if _paused or (_mode != "LIVE ASSIST" and not _persistent_observation):
                break
        try:
            from core import screenview
            view = screenview.look(budget_s=0.8, allow_ocr=False,
                                   allow_pixel_digest=False)
            digest = view.stable_digest or view.digest
            if digest != previous:
                previous = digest
                delay = 2.0
                _update(active_app=view.app or "unknown",
                        current_action="Live observation",
                        verification=(f"Local UI tree: {view.source}; "
                                      f"{len(view.elements)} controls"))
            else:
                delay = min(8.0, delay * 1.5)
        except Exception:
            delay = min(8.0, delay * 1.5)
        _live_stop.wait(delay)
    if _mode != "LIVE ASSIST" and not _persistent_observation:
        _update(screen_access=False, current_action="Ready")


def pause() -> dict:
    global _paused
    with _lock:
        _paused = True
    _live_stop.set()
    _cancel_inflight()
    try:
        from core import screenact
        screenact.release_all()
    except Exception:
        pass
    return _update(paused=True, controlling=False, screen_access=False,
                   current_action="Paused", mouse_keyboard=False)


def resume() -> dict:
    global _paused
    with _lock:
        _paused = False
        _stopped.clear()
        live = _mode == "LIVE ASSIST" or _persistent_observation
    if live:
        set_persistent_observation(_persistent_observation)
    return _update(paused=False, stopped=False, current_action=(
        "Local live observation" if live else "Ready"), screen_access=live)


def emergency_stop() -> dict:
    """Latch stop first, then best-effort release any held inputs."""
    global _persistent_observation, _mode
    _stopped.set()
    _live_stop.set()
    with _lock:
        _persistent_observation = False
        _mode = "ON-DEMAND"
    try:
        from memory.config_manager import save_persistent_desktop_observation_enabled
        save_persistent_desktop_observation_enabled(False)
    except Exception:
        pass
    _update(mode="ON-DEMAND", stopped=True, paused=True, controlling=False, screen_access=False,
            persistent_observation=False,
            current_action="Emergency stop", verification="Stopped by user",
            mouse_keyboard=False)
    try:
        from core import autonomous_mode
        autonomous_mode.emergency_stop("Desktop emergency stop triggered")
    except Exception:
        pass
    try:
        from core import screenact
        screenact.release_all()
    except Exception:
        pass
    _cancel_inflight()
    return snapshot()


def _cancel_inflight() -> None:
    with _cancel_lock:
        cancellers = tuple(_active_cancellers)
    for cancel in cancellers:
        try:
            cancel()
        except Exception:
            pass


def register_cancel(callback) -> None:
    with _cancel_lock:
        _active_cancellers.add(callback)


def unregister_cancel(callback) -> None:
    with _cancel_lock:
        _active_cancellers.discard(callback)


def should_stop() -> bool:
    with _lock:
        return _paused or _stopped.is_set()


@contextmanager
def action(action_name: str):
    """Serialize pointer/keyboard actions and publish their active state."""
    if should_stop():
        raise RuntimeError("computer control is paused or stopped; resume it first")
    if not _action_lock.acquire(blocking=False):
        raise RuntimeError("another computer-control action is in progress")
    try:
        if should_stop():
            raise RuntimeError("computer control was stopped before the action began")
        _update(controlling=True, screen_access=True, current_action=action_name,
                mouse_keyboard=action_name not in ("observe", "find", "read"))
        yield
    finally:
        stopped = should_stop()
        live = _mode == "LIVE ASSIST" and not stopped
        _update(controlling=False, screen_access=live,
                current_action=("Emergency stop" if _stopped.is_set() else
                                "Paused" if stopped else
                                "Local live observation" if live else "Ready"),
                mouse_keyboard=False)
        _action_lock.release()


def report(action_name: str, verification: str, app: str = "") -> dict:
    return _update(last_action=action_name, verification=verification,
                   active_app=app or _state.get("active_app", "—"))
