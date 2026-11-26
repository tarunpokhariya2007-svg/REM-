"""Shared state and the single action lock for desktop control."""
from __future__ import annotations

import contextvars
import threading
import re
import time
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
    "local_autonomy_profile": "trusted",
    "revoked": False,
    "foreground_app": "—",
    "active_application": "—",
    "root_task_id": "",
    "active_task_id": "",
    "active_contact": "—",
    "active_chat_id": "",
    "selected_target": "—",
    "chrome_detected": False,
    "chrome_foreground": False,
    "browser_task_id": "",
    "active_target": "—",
    "last_obs_source": "none",
    "ocr_count": 0,
    "uia_count": 0,
    "unique_contact_count": 0,
    "selected_chat_verified": False,
    "composition_status": "idle",
    "typing_status": "idle",
    "sending_status": "not_sent",
    "cursor_pos": "(0, 0)",
    "last_mouse_status": "idle",
    "last_keyboard_status": "idle",
    "last_media_status": "idle",
    "last_auth_decision": "—",
    "voice_turn_id": "",
    "recognized_transcript": "",
    "detected_intent": "",
    "auth_source": "",
    "composition_id": "",
    "draft_hash": "",
    "call_status": "idle",
    "gui_confirm_required": False,
    "gui_confirm_reason": "",
    "auth_expires_at": "",
    "tool_name": "",
    "exec_status": "idle",
    "verify_status": "idle",
    "error_reason": "",
    "obs_id": "",
    "gemini_live_status": "disconnected",
    "gemini_last_error_category": "none",
    "gemini_reconnect_count": 0,
    "live_retry_delay": 0.0,
    "live_session_count": 0,
    "audio_stream_status": "idle",
}
_listeners: list = []

ACTIVE_TARGET_TTL = 120.0
_active_target_lock = threading.RLock()
_active_targets_by_task: dict[str, dict] = {}
_active_typing_by_task: dict[str, dict] = {}
_bound_auth_ctx_var: contextvars.ContextVar = contextvars.ContextVar("rem_desktop_auth_ctx", default=None)


def bind_auth_context(auth_context):
    """Bind the current worker dispatch RequestContext so nested tools share task target state."""
    return _bound_auth_ctx_var.set(auth_context)


def reset_auth_context(token) -> None:
    _bound_auth_ctx_var.reset(token)


def current_bound_auth_context():
    return _bound_auth_ctx_var.get()


def _task_keys(auth_context=None, task_id: str = "", browser_task_id: str = "") -> list[str]:
    ctx = auth_context if auth_context is not None else _bound_auth_ctx_var.get()
    keys: list[str] = []
    for k in (
        browser_task_id,
        task_id,
        str(getattr(ctx, "browser_task_id", "") or "").strip() if ctx is not None else "",
        str(getattr(ctx, "root_task_id", "") or "").strip() if ctx is not None else "",
        str(getattr(ctx, "task_id", "") or "").strip() if ctx is not None else "",
    ):
        if k and k not in keys:
            keys.append(k)
    return keys


def set_active_target(*, target_type: str, exact_target_name: str,
                      browser: str = "", tab_title: str = "", tab_url: str = "",
                      source_observation_id: str = "",
                      result_position: int | None = None,
                      originating_instruction: str = "",
                      task_id: str = "", browser_task_id: str = "",
                      point: tuple[int, int] | None = None,
                      rect: tuple[int, int, int, int] | None = None,
                      confidence: str = "medium",
                      extraction_source: str = "",
                      results: list[dict] | None = None,
                      auth_context=None) -> dict:
    """Record a grounded active UI/browser target scoped cleanly to the current task."""
    keys = _task_keys(auth_context, task_id, browser_task_id)
    now_m = time.monotonic()
    ctx = auth_context if auth_context is not None else _bound_auth_ctx_var.get()
    rec = {
        "target_type": str(target_type or "element").strip().lower(),
        "exact_target_name": str(exact_target_name or "").strip(),
        "browser": str(browser or "").strip().lower(),
        "tab_title": str(tab_title or "").strip(),
        "tab_url": str(tab_url or "").strip(),
        "source_observation_id": str(source_observation_id or f"obs_{int(now_m * 1000)}"),
        "result_position": (int(result_position) if result_position is not None else None),
        "observed_at": now_m,
        "originating_instruction": str(originating_instruction or (getattr(ctx, "instruction", "") if ctx else "")).strip(),
        "task_id": str(task_id or (getattr(ctx, "task_id", "") if ctx else "") or "").strip(),
        "browser_task_id": str(browser_task_id or (getattr(ctx, "browser_task_id", "") if ctx else "") or "").strip(),
        "point": (tuple(int(v) for v in point) if point and len(point) == 2 else None),
        "rect": (tuple(int(v) for v in rect) if rect and len(rect) == 4 else None),
        "confidence": str(confidence or "medium"),
        "extraction_source": str(extraction_source or "").strip(),
        "results": list(results or ()),
    }
    if not keys:
        keys = ["_default_session_target"]
    with _active_target_lock:
        for k in keys:
            _active_targets_by_task[k] = rec
    return rec


def get_active_target(auth_context=None, *, max_age_s: float = ACTIVE_TARGET_TTL) -> dict | None:
    if should_stop():
        return None
    keys = _task_keys(auth_context)
    if not keys and auth_context is None:
        keys = ["_default_session_target"]
    now_m = time.monotonic()
    with _active_target_lock:
        for k in keys:
            rec = _active_targets_by_task.get(k)
            if not rec:
                continue
            if (now_m - float(rec.get("observed_at", 0.0))) <= max_age_s:
                return rec
    return None


def _ordinal_from_ref(text: str) -> int | None:
    cf = str(text or "").casefold()
    mapping = {
        "first": 1, "1st": 1, "second": 2, "2nd": 2, "third": 3, "3rd": 3,
        "fourth": 4, "4th": 4, "fifth": 5, "5th": 5, "sixth": 6, "6th": 6,
    }
    for word, pos in mapping.items():
        if re.search(rf"\b{word}\b", cf):
            return pos
    if re.search(r"\blast\b", cf):
        return -1
    return None


def resolve_target_reference(description: str, *, auth_context=None,
                             request_text: str = "",
                             max_age_s: float = ACTIVE_TARGET_TTL) -> dict | None:
    """Resolve pronoun ('it', 'that', 'second one') or title against fresh active target state."""
    rec = get_active_target(auth_context, max_age_s=max_age_s)
    if not rec:
        return None
    desc = str(description or "").strip()
    desc_cf = desc.casefold()
    req_cf = str(request_text or "").casefold()
    results = rec.get("results") or []

    # 1. Explicit ordinal inside description or request ("the first one", "the second one", "first rio")
    pos = _ordinal_from_ref(desc_cf) or (_ordinal_from_ref(req_cf) if ({"one", "video", "contact", "chat", "rio"} & _tokens(req_cf)) else None)
    if pos is not None and results:
        idx = (len(results) - 1) if pos == -1 else (pos - 1)
        if 0 <= idx < len(results):
            item = results[idx]
            item_name = str(item.get("display_name") or item.get("title") or rec.get("exact_target_name") or "")
            item_type = "contact" if item.get("display_name") else "video"
            return {
                **rec,
                "target_type": item_type,
                "exact_target_name": item_name,
                "result_position": int(item.get("position") or (idx + 1)),
                "point": item.get("point") or rec.get("point"),
                "rect": item.get("rect") or rec.get("rect"),
                "confidence": item.get("confidence") or rec.get("confidence", "medium"),
                "extraction_source": item.get("source") or rec.get("extraction_source", ""),
            }

    # 2. Pronoun reference ("it", "that", "him", "her", "second one", "open it", "call him")
    pronoun_phrases = {
        "it", "that", "this", "him", "her", "them", "that one", "this one", "that video", "this video",
        "click it", "click that", "open it", "open that", "play it", "play that",
        "call him", "call her", "call them", "yes click it", "select it", "that result", "that field", "that bar",
    }
    desc_tokens = _tokens(desc_cf)
    is_pronoun_desc = (desc_cf in pronoun_phrases) or (bool(desc_tokens) and desc_tokens.issubset({"yes", "click", "open", "play", "select", "call", "it", "that", "this", "one", "him", "her", "them", "video", "result", "chat"}))
    is_pronoun_req = bool(_tokens(req_cf) & {"it", "that", "this", "one", "him", "her"})
    if is_pronoun_desc or (not desc and is_pronoun_req):
        if rec.get("exact_target_name") or rec.get("point"):
            return rec

    # 3. Exact or strong substring match against currently tracked active target / observed results
    if desc_cf:
        exact_cf = str(rec.get("exact_target_name") or "").casefold()
        if exact_cf and (desc_cf == exact_cf or (len(desc_cf) >= 4 and (desc_cf in exact_cf or exact_cf in desc_cf))):
            return rec
        for item in results:
            item_label = str(item.get("display_name") or item.get("title") or "")
            t_cf = item_label.casefold()
            if t_cf and (desc_cf == t_cf or (len(desc_cf) >= 3 and (desc_cf in t_cf or t_cf in desc_cf))):
                return {
                    **rec,
                    "target_type": ("contact" if item.get("display_name") else "video"),
                    "exact_target_name": item_label,
                    "result_position": int(item.get("position") or 0) or None,
                    "point": item.get("point"),
                    "rect": item.get("rect"),
                    "confidence": item.get("confidence") or "medium",
                    "extraction_source": item.get("source") or "",
                }
    return None


def set_active_typing_payload(text: str, *, auth_context=None, task_id: str = "", browser_task_id: str = "") -> None:
    cleaned = str(text or "").strip()
    if not cleaned:
        return
    keys = _task_keys(auth_context, task_id, browser_task_id) or ["_default_session_target"]
    rec = {"text": cleaned, "observed_at": time.monotonic()}
    with _active_target_lock:
        for k in keys:
            _active_typing_by_task[k] = rec


_active_composition_by_task: dict[str, dict] = {}
_sent_attempts_lock = threading.RLock()
_sent_attempt_records: dict[tuple[str, str], float] = {}


def has_sent_attempt(*, receiver: str, draft_hash: str, task_id: str = "", window_s: float = 120.0) -> bool:
    """Idempotency check preventing duplicate sends of the same draft to the same contact."""
    rec_key = (str(receiver or "").strip().casefold(), str(draft_hash or "").strip())
    if not rec_key[0] or not rec_key[1]:
        return False
    now_m = time.monotonic()
    with _sent_attempts_lock:
        ts = _sent_attempt_records.get(rec_key)
        if ts is not None and (now_m - ts) <= window_s:
            return True
    return False


def record_sent_attempt(*, receiver: str, draft_hash: str, task_id: str = "") -> None:
    rec_key = (str(receiver or "").strip().casefold(), str(draft_hash or "").strip())
    if not rec_key[0] or not rec_key[1]:
        return
    with _sent_attempts_lock:
        _sent_attempt_records[rec_key] = time.monotonic()


def mark_composition_sent(*, receiver: str = "", draft_hash: str = "") -> None:
    with _active_target_lock:
        for rec in _active_composition_by_task.values():
            if not draft_hash or rec.get("generated_content_hash") == draft_hash:
                rec["message_sent"] = True
    record_diagnostics(sending_status=(f"sent:{receiver[:20]}" if receiver else "sent"))


def set_active_composition_state(*, composition_requested: bool, composition_task_id: str = "",
                                 selected_contact: str = "", generated_content: str = "",
                                 typing_authorized: bool = True, message_sent: bool = False,
                                 language: str = "",
                                 auth_context=None, task_id: str = "", browser_task_id: str = "") -> dict:
    """Record explicit autonomous draft authorization scoped to an active task without authorizing send."""
    import hashlib
    keys = _task_keys(auth_context, task_id, browser_task_id) or ["_default_session_target"]
    content_str = str(generated_content or "")
    d_hash = hashlib.sha256(content_str.encode("utf-8", errors="ignore")).hexdigest()[:16] if content_str else ""
    comp_id = f"cmp_{d_hash[:8]}" if d_hash else f"cmp_{int(time.monotonic())}"
    rec = {
        "composition_id": comp_id,
        "composition_requested": bool(composition_requested),
        "composition_task_id": str(composition_task_id or (keys[0] if keys else "")),
        "selected_contact": str(selected_contact or "").strip(),
        "language": str(language or "").strip(),
        "generated_content": content_str,
        "generated_content_hash": d_hash,
        "typing_authorized": bool(typing_authorized),
        "message_sent": bool(message_sent),
        "observed_at": time.monotonic(),
    }
    with _active_target_lock:
        for k in keys:
            _active_composition_by_task[k] = rec
    record_diagnostics(
        composition_id=comp_id,
        draft_hash=d_hash,
        composition_status=("ready" if content_str else "authorized"),
        typing_status=("authorized" if typing_authorized else "idle"),
        sending_status=("sent" if message_sent else "not_sent"),
        active_contact=(rec["selected_contact"] or "—")[:32],
    )
    return rec


def get_active_composition_state(auth_context=None, *, max_age_s: float = ACTIVE_TARGET_TTL) -> dict | None:
    if should_stop():
        return None
    keys = _task_keys(auth_context) or ["_default_session_target"]
    now_m = time.monotonic()
    with _active_target_lock:
        for k in keys:
            rec = _active_composition_by_task.get(k)
            if rec and (now_m - float(rec.get("observed_at", 0.0))) <= max_age_s:
                return rec
    return None


def _is_composition_request(request: str, spoken_hist: str = "") -> bool:
    combined = f"{request} {spoken_hist}".casefold()
    phrases = (
        "write something", "write by yourself", "by yourself", "write a message",
        "write a hindi", "write an english", "in hindi", "in english", "into english", "into hindi",
        "translate", "translation", "replace", "remove the previous", "remove previous", "remove the hindi",
        "write this one", "type this one", "replace it", "his day", "her day", "own wording",
        "write about", "how your day", "compose", "draft a message", "draft",
        "create a friendly", "say anything nice", "5 to 10 lines", "5-10 lines",
        "make it more natural", "more natural", "make it shorter", "shorten it",
        "rewrite it", "rewrite the", "change it", "apne aap likho", "message likho",
        "generate a message", "write a friendly",
    )
    return any(p in combined for p in phrases)


def get_active_typing_payload(auth_context=None, *, max_age_s: float = ACTIVE_TARGET_TTL) -> str:
    if should_stop():
        return ""
    keys = _task_keys(auth_context) or ["_default_session_target"]
    now_m = time.monotonic()
    with _active_target_lock:
        for k in keys:
            rec = _active_typing_by_task.get(k)
            if rec and (now_m - float(rec.get("observed_at", 0.0))) <= max_age_s:
                return str(rec.get("text") or "")
    return ""


def clear_task_targets(*, task_id: str = "", browser_task_id: str = "") -> None:
    keys = [k for k in (task_id, browser_task_id) if k]
    with _active_target_lock:
        if not keys:
            _active_targets_by_task.clear()
            _active_typing_by_task.clear()
            return
        for k in keys:
            _active_targets_by_task.pop(k, None)
            _active_typing_by_task.pop(k, None)


def _tokens(value) -> set[str]:
    text = unicodedata.normalize("NFKD", str(value or "").casefold())
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return set(re.findall(r"[a-z0-9]+", text))


_BROWSER_NAMES = ("chrome", "edge", "firefox", "brave", "opera", "vivaldi")


def extract_browser_name(value: str) -> str:
    """Return canonical browser name if explicitly requested in user text."""
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    if re.search(r"\b(?:google\s+)?chrome\b", text):
        return "chrome"
    if re.search(r"\b(?:microsoft\s+edge|ms\s*edge|msedge|edge)\b", text):
        return "edge"
    if re.search(r"\b(?:mozilla\s+)?firefox\b", text):
        return "firefox"
    if re.search(r"\bbrave\b", text):
        return "brave"
    if re.search(r"\bopera(?:\s*gx)?\b", text):
        return "operagx" if "gx" in text else "opera"
    if re.search(r"\bvivaldi\b", text):
        return "vivaldi"
    return ""


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


def _get_local_autonomy_profile() -> str:
    try:
        from memory.config_manager import get_local_autonomy_mode
        return get_local_autonomy_mode()
    except Exception:
        return "trusted"


def classify_action(tool: str, args: dict | None) -> dict:
    """Normalize any desktop/browser/media tool invocation into a canonical semantic category."""
    t = str(tool or "").lower().strip()
    if t == "desktop_ui":
        t = "screen_agent"
    raw_action = str((args or {}).get("action") or "").lower().replace("-", "_").strip()
    if not raw_action and t in ("open_app", "send_message", "screen_process"):
        raw_action = t
    if not raw_action and t == "computer_settings":
        try:
            from actions.computer_settings import _detect_action
            raw_action = str(_detect_action(str((args or {}).get("description") or "")).get("action") or "")
        except Exception:
            raw_action = ""

    category = "unknown"
    routine = True
    sensitive = False

    if t == "screen_process":
        angle = str((args or {}).get("angle") or "screen").strip().casefold()
        category = "screen.camera" if angle == "camera" else "screen.observe"
    elif t == "screen_agent":
        if raw_action in ("observe", "find"):
            category = "screen.observe"
        elif raw_action == "focus_window":
            category = "browser.focus"
        elif raw_action in ("move", "hover", "move_relative"):
            category = "mouse.move"
        elif raw_action in ("click", "left_click", "right_click", "double_click", "mouse_down", "mouse_up", "scroll", "select", "toggle", "expand", "collapse"):
            category = "mouse.click"
        elif raw_action == "type":
            category = "keyboard.type"
        elif raw_action == "press":
            category = "keyboard.press"
        elif raw_action in ("pause", "resume", "stop", "emergency_stop", "set_mode"):
            category = "control.state"
    elif t == "browser_control":
        if raw_action in ("inspect", "get_text", "get_url", "list_browsers"):
            category = "browser.inspect"
        elif raw_action == "screenshot":
            category = "screen.observe"
        elif raw_action in ("go_to", "search", "new_tab", "switch_tab", "close_tab", "back", "forward", "reload", "switch"):
            category = "browser.navigate"
        elif raw_action in ("focus_search",):
            category = "browser.focus"
        elif raw_action == "search_contact":
            category = "contact.search"
        elif raw_action == "select_contact":
            category = "contact.select"
        elif raw_action in ("compose_message", "compose_draft", "update_draft", "edit_draft"):
            category = "message.compose"
        elif raw_action in ("smart_click", "click", "left_click", "right_click", "double_click"):
            category = "browser.click"
        elif raw_action in ("smart_type", "type"):
            category = "browser.type"
        elif raw_action in ("press", "scroll"):
            category = "browser.keypress"
        elif raw_action in ("play_video", "replay_video"):
            category = "media.play"
        elif raw_action == "pause_video":
            category = "media.pause"
        elif raw_action == "resume_video":
            category = "media.resume"
        elif raw_action in ("seek_forward", "seek_backward", "set_playback_position"):
            category = "media.seek"
        elif raw_action in ("volume_up", "volume_down", "mute", "unmute"):
            category = "media.volume"
        elif raw_action in ("fill_form", "close", "close_all"):
            category = "sensitive.form"
            routine = False
            sensitive = True
    elif t == "computer_control":
        if raw_action in ("screenshot", "screen_find"):
            category = "screen.observe"
        elif raw_action == "focus_window":
            category = "browser.focus"
        elif raw_action == "move":
            category = "mouse.move"
        elif raw_action in ("click", "left_click", "right_click", "double_click", "mouse_down", "mouse_up", "scroll", "drag", "screen_click"):
            category = "mouse.click"
        elif raw_action in ("type", "smart_type"):
            category = "keyboard.type"
        elif raw_action in ("press", "hotkey"):
            category = "keyboard.press"
        elif raw_action == "close_app":
            category = "sensitive.app_lifecycle"
            routine = False
            sensitive = True
    elif t == "computer_settings":
        if raw_action == "focus_search":
            category = "browser.focus"
        elif raw_action in ("seek_forward", "seek_backward", "fast_forward", "rewind"):
            category = "media.seek"
        elif raw_action == "pause_video":
            category = "media.pause"
        elif raw_action in ("media_play_pause", "media_next", "media_previous"):
            category = "media.resume"
        elif raw_action in ("volume_set", "volume_up", "volume_down", "mute", "unmute", "toggle_mute"):
            category = "media.volume"
        elif raw_action == "type_text":
            category = "keyboard.type"
        elif raw_action in ("restart", "shutdown", "toggle_wifi", "lock_screen", "sleep_display", "open_run"):
            category = "sensitive.system"
            routine = False
            sensitive = True
        else:
            category = "keyboard.press"
    elif t == "video_player":
        if raw_action in ("seek", "forward", "back", "rewind", "seek_to"):
            category = "media.seek"
        elif raw_action == "pause":
            category = "media.pause"
        elif raw_action in ("resume", "play"):
            category = "media.resume"
        else:
            category = "media.play"
    elif t == "send_message":
        if (args or {}).get("draft_only"):
            category = "message.type"
        else:
            category = "message.send"
            routine = False
            sensitive = True
    elif t == "whatsapp_call":
        if raw_action == "inspect":
            category = "screen.observe"
        elif raw_action == "end_call":
            category = "call.end"
        elif (args or {}).get("video"):
            category = "call.video"
            routine = False
            sensitive = True
        else:
            category = "call.voice"
            routine = False
            sensitive = True
    elif t == "open_app":
        category = "application.open"

    return {
        "tool": t,
        "action": raw_action,
        "category": category,
        "routine": routine,
        "sensitive": sensitive,
    }


def _authorize_screen_observation(request: str, words: set[str], *, tool_label: str, action_label: str,
                                  source: str, has_live_task_chain: bool,
                                  profile: str, auth_context=None) -> dict:
    """Single shared gate for screen_process(screen) and desktop_ui/screen_agent(observe)."""
    try:
        from memory.config_manager import (
            get_local_screen_observation_enabled,
            is_screen_access_revoked,
        )
        local_enabled = bool(get_local_screen_observation_enabled())
        revoked = bool(is_screen_access_revoked())
    except Exception:
        local_enabled, revoked = True, False

    if not local_enabled or revoked:
        if revoked:
            print(f"[Auth] DENY tool={tool_label} action={action_label} source={source} cat=revoked_authorization")
            return {
                "allowed": False,
                "cat": "revoked_authorization",
                "reason": "Local screen observation was revoked by the user. Enable LOCAL SCREEN OBSERVATION in the COMPUTER USE panel to restore it.",
                "revoked": True,
            }
        print(f"[Auth] DENY tool={tool_label} action={action_label} source={source} cat=observation_disabled")
        return {
            "allowed": False,
            "cat": "observation_disabled",
            "reason": "Local screen observation is disabled in COMPUTER USE settings.",
            "revoked": False,
        }

    routine_desktop_hints = {
        "youtube", "video", "videos", "title", "titles", "search", "bar", "tab",
        "chrome", "edge", "firefox", "browser", "whatsapp", "cursor", "mouse",
        "button", "link", "karo", "batao", "chala", "chalao", "kholo", "roko",
        "first", "second", "third", "fourth", "last", "play", "pause", "resume",
        "seek", "forward", "backward", "click", "type", "focus",
    }
    allowed = _is_screen_observation_request(request) or (
        profile == "trusted" and (has_live_task_chain or bool(words & routine_desktop_hints))
    )
    print(f"[Auth] {'ALLOW' if allowed else 'DENY'} tool={tool_label} action={action_label} source={source}")
    return {
        "allowed": allowed,
        "cat": ("" if allowed else "request_not_authorized"),
        "reason": ("" if allowed else "A direct screen-observation request is required."),
        "revoked": False,
    }


def _log_auth_decision(decision: str, *, tool: str, action: str, source: str,
                       auth_context=None, reason: str = "", category: str = "") -> None:
    """Emit structured authorization diagnostics without leaking screen or payload text."""
    tid = str(getattr(auth_context, "task_id", "") or "none") if auth_context is not None else "none"
    btid = str(getattr(auth_context, "browser_task_id", "") or "none") if auth_context is not None else "none"
    rtid = str(getattr(auth_context, "root_task_id", "") or tid) if auth_context is not None else "none"
    created = getattr(auth_context, "created_at", None) if auth_context is not None else None
    expires = getattr(auth_context, "expires_at", None) if auth_context is not None else None
    c_str = f"{created:.2f}" if isinstance(created, (int, float)) else "none"
    e_str = f"{expires:.2f}" if isinstance(expires, (int, float)) else "none"
    cat_part = f" cat={category}" if category else ""
    reason_part = f" reason={reason}" if reason else ""
    print(
        f"[Auth] {decision} tool={tool} action={action or 'none'} source={source}"
        f" expected_task={rtid} received_task={tid} browser_task={btid}"
        f" created_at={c_str} expires_at={e_str}{cat_part}{reason_part}"
    )


def evaluate_authorization(user_request: str, tool: str, args: dict, *,
                           source: str = "untrusted",
                           auth_context=None) -> dict:
    """Unified semantic authorization evaluation returning structured audit & task metadata."""
    info = classify_action(tool, args)
    profile = _get_local_autonomy_profile()
    perm = get_screen_permission_status()
    revoked = bool(perm.get("explicitly_revoked"))
    rev_status = "REVOKED" if revoked else "GRANTED"
    active_tid = str(getattr(auth_context, "task_id", "") or "") if auth_context is not None else ""
    root_tid = str(getattr(auth_context, "root_task_id", "") or active_tid) if auth_context is not None else ""
    browser_tid = str(getattr(auth_context, "browser_task_id", "") or "") if auth_context is not None else ""

    raw_target = str(
        (args or {}).get("target")
        or (args or {}).get("description")
        or (args or {}).get("text")
        or (args or {}).get("app_name")
        or (args or {}).get("receiver")
        or ""
    ).strip()
    resolved = resolve_target_reference(raw_target, auth_context=auth_context, request_text=user_request) if raw_target else get_active_target(auth_context)
    resolved_name = str((resolved or {}).get("exact_target_name") or raw_target or "")

    allowed, reason = _authorize_user_request_core(
        user_request, tool, args, source=source, auth_context=auth_context,
    )
    exp_str = f"{max(0.0, float(auth_context.expires_at) - time.monotonic()):.0f}s" if auth_context is not None and not auth_context.is_expired() else ("expired" if auth_context is not None else "none")
    decision_str = f"{'ALLOW' if allowed else 'DENY'} {info['category']} via {info['tool']}"
    record_diagnostics(
        local_autonomy_profile=profile,
        revoked=revoked,
        active_task_id=active_tid,
        root_task_id=root_tid,
        browser_task_id=browser_tid,
        active_target=(resolved_name or "—")[:48],
        selected_target=(resolved_name or "—")[:48],
        last_auth_decision=decision_str[:64],
        auth_source=source,
        detected_intent=info["category"],
        tool_name=info["tool"],
        auth_expires_at=exp_str,
    )
    return {
        "allowed": bool(allowed),
        "reason": str(reason or ""),
        "root_task_id": root_tid,
        "active_task_id": active_tid,
        "browser_task_id": browser_tid,
        "resolved_target": resolved_name,
        "action_category": info["category"],
        "authorization_profile": profile,
        "permission_source": source,
        "revocation_status": rev_status,
    }


def authorize_user_request(user_request: str, tool: str, args: dict, *,
                           source: str = "untrusted",
                           auth_context=None) -> tuple[bool, str]:
    """Limit computer actions to the human's latest request, not page text."""
    res = evaluate_authorization(user_request, tool, args, source=source, auth_context=auth_context)
    return bool(res["allowed"]), str(res["reason"])


def _authorize_user_request_core(user_request: str, tool: str, args: dict, *,
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
    t = str(tool or "").lower()
    if t == "desktop_ui":
        t = "screen_agent"
    action = str((args or {}).get("action") or "").lower().replace("-", "_")
    if not action and t in ("open_app", "send_message", "screen_process"):
        action = t
    if source not in trusted_sources:
        _log_auth_decision("DENY", tool=t, action=action, source=source, auth_context=auth_context, category="untrusted_source", reason="source_not_trusted")
        return False, "A direct user request from a trusted input channel is required."
    if not request:
        _log_auth_decision("DENY", tool=t, action=action, source=source, auth_context=auth_context, category="empty_request", reason="empty_user_request")
        return False, "I need a direct user request before accessing or controlling the desktop."
    words = _tokens(request)
    parent_request = str(getattr(auth_context, "parent_instruction", "") or "").casefold().strip() if auth_context is not None else ""
    spoken_hist = " ".join(getattr(auth_context, "spoken_inputs", ()) or ()) if auth_context is not None else ""
    has_live_task_chain = (
        auth_context is not None
        and not auth_context.is_expired()
        and not should_stop()
        and bool(str(getattr(auth_context, "browser_task_id", "") or "").strip() or parent_request)
    )
    clarification_words = (_tokens(parent_request) | _tokens(spoken_hist)) if has_live_task_chain else set()
    context_words = (words | clarification_words) if has_live_task_chain else words
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
        res_obs = _authorize_screen_observation(
            request, words, tool_label="screen_process", action_label="screen",
            source=source, has_live_task_chain=has_live_task_chain,
            profile=_get_local_autonomy_profile(), auth_context=auth_context,
        )
        return bool(res_obs["allowed"]), str(res_obs["reason"])

    if t == "screen_agent" and action == "observe":
        res_obs = _authorize_screen_observation(
            request, words, tool_label="desktop_ui", action_label="observe",
            source=source, has_live_task_chain=has_live_task_chain,
            profile=_get_local_autonomy_profile(), auth_context=auth_context,
        )
        return bool(res_obs["allowed"]), str(res_obs["reason"])

    intents = {
        "screen_agent": {
            "observe": {"screen", "desktop", "window", "application", "look", "see", "visible", "show", "search", "open", "click", "type", "find", "scroll", "switch", "focus", "read", "inspect", "check", "whether", "verify", "confirm", "chrome", "browser", "whatsapp", "youtube", "cursor", "mouse", "put", "place"},
            "find": {"find", "locate", "search", "where", "identify", "look", "open", "see", "check", "whether", "visible", "whatsapp", "youtube", "chrome", "tab", "cursor", "bar"},
            "click": {"click", "left", "tap", "here", "there", "this", "that", "it", "select", "choose", "open", "press", "put", "focus", "search", "bar", "box", "video", "thumbnail", "result", "play", "pause", "resume", "button", "mouse", "cursor", "first", "second", "third", "fourth", "last"},
            "left_click": {"click", "left", "tap", "here", "there", "this", "that", "it", "mouse", "button", "cursor", "bar", "box", "video", "thumbnail", "result", "play", "open", "select", "first", "second", "third", "fourth", "last", "search", "youtube"},
            "right_click": {"right", "click", "context", "here", "mouse", "search", "bar", "box", "youtube"},
            "double_click": {"double", "click", "here", "mouse", "search", "bar"},
            "mouse_down": {"hold", "down", "press", "mouse", "button", "left"},
            "mouse_up": {"release", "let", "up", "mouse", "button", "left"},
            "select": {"select", "choose"},
            "toggle": {"toggle", "enable", "disable", "turn", "switch"},
            "expand": {"expand", "open"},
            "collapse": {"collapse", "close"},
            "hover": {"hover", "move", "cursor", "mouse", "point", "put", "place", "click", "focus", "search", "video", "second", "third", "first"},
            "move": {"move", "go", "cursor", "mouse", "pointer", "point", "put", "place", "control", "position", "coordinates", "click", "focus", "search", "box", "bar", "video", "result", "here", "center", "middle", "corner", "top", "bottom", "left", "right", "screen"},
            "move_relative": {"move", "cursor", "mouse", "pointer", "left", "right", "up", "down", "point", "nudge"},
            "type": {"type", "enter", "write", "search", "fill", "paste", "put"},
            "press": {"press", "enter", "return", "escape", "esc", "tab", "backspace", "delete", "arrow", "up", "down", "left", "right", "home", "end", "ctrl", "select", "all", "copy", "paste", "undo", "keyboard", "key", "shortcut", "hotkey", "search", "query", "submit", "close", "tab", "play", "pause", "replay"},
            "scroll": {"scroll", "page", "down", "up", "left", "right"},
            "focus_window": {"focus", "switch", "bring", "window", "open", "chrome", "youtube"},
            "set_mode": {"mode", "computer", "control", "assist", "cursor"},
            "pause": {"pause", "stop", "halt"},
            "resume": {"resume", "continue", "unpause"},
            "stop": {"stop", "halt", "emergency"},
            "emergency_stop": {"stop", "halt", "emergency"},
        },
        "browser_control": {
            "go_to": {"open", "go", "visit", "navigate", "website", "url", "browser", "chrome", "edge", "firefox", "youtube", "google"},
            "search": {"search", "find", "look", "google", "bing", "web", "youtube", "query", "play"},
            "focus_search": {"search", "bar", "box", "field", "focus", "open", "click", "youtube", "ab", "karo"},
            "new_tab": {"tab", "open", "new"},
            "switch_tab": {"tab", "switch", "open", "focus", "whatsapp", "youtube", "chrome", "edge", "browser", "current"},
            "inspect": {"inspect", "open", "see", "check", "whether", "verify", "confirm", "chrome", "browser", "whatsapp", "youtube", "page", "site", "website", "tab", "status", "loaded", "login", "url", "read", "search", "cursor", "play", "pause", "resume", "replay", "video", "videos", "result", "results", "first", "second", "third", "fourth", "fifth", "titled", "title"},
            "smart_click": {"click", "left", "open", "select", "choose", "press", "search", "focus", "bar", "put", "cursor", "play", "replay", "again", "restart", "pause", "resume", "video", "videos", "result", "results", "recommended", "first", "second", "third", "fourth", "fifth", "sixth", "seventh", "eighth", "ninth", "tenth", "last", "next", "previous", "titled", "title", "tab", "youtube", "1st", "2nd", "3rd", "4th", "5th", "it", "that", "this", "one", "yes"},
            "click": {"click", "left", "open", "select", "choose", "focus", "search", "bar", "play", "pause", "resume", "replay", "it", "that", "this", "one", "yes"},
            "left_click": {"click", "left", "open", "select", "focus", "bar", "video", "result", "play", "it", "that", "this", "one", "yes"},
            "smart_type": {"type", "enter", "search", "fill", "write", "put", "that", "it", "yes", "compose", "write", "message", "draft", "natural", "shorter", "rewrite", "rio"},
            "type": {"type", "enter", "search", "fill", "write", "put", "that", "it", "yes", "compose", "write", "message", "draft", "natural", "shorter", "rewrite"},
            "compose_message": {"compose", "write", "message", "draft", "yourself", "friendly", "nice", "natural", "shorter", "rewrite", "reply", "say"},
            "update_draft": {"shorter", "shorten", "natural", "rewrite", "change", "edit", "update", "draft", "make", "more"},
            "search_contact": {"search", "find", "look", "rio", "contact", "chat", "whatsapp", "for"},
            "select_contact": {"open", "click", "select", "chat", "contact", "rio", "it", "that", "whatsapp"},
            "screenshot": {"screenshot", "screen", "capture", "see", "look", "check", "inspect", "what", "visible", "youtube", "whatsapp"},
            "fill_form": {"fill", "form", "enter", "complete"},
            "pause_video": {"pause", "stop", "halt", "video", "playback", "roko"},
            "resume_video": {"resume", "play", "continue", "unpause", "video", "playback", "chalao", "chala", "karo"},
            "play_video": {"play", "watch", "open", "next", "video", "result", "first", "second", "third", "fourth", "fifth", "last", "titled", "it", "that", "this", "one", "yes", "chalao"},
            "replay_video": {"replay", "again", "restart", "rewatch", "video", "play", "first", "second", "third", "fourth", "fifth", "this", "last"},
            "seek_forward": {"forward", "skip", "seek", "ahead", "fast", "seconds", "second", "video", "aage"},
            "seek_backward": {"back", "backward", "rewind", "seek", "seconds", "second", "video", "peeche"},
            "set_playback_position": {"seek", "jump", "skip", "position", "minute", "minutes", "seconds", "video"},
            "volume_up": {"volume", "louder", "up", "sound"},
            "volume_down": {"volume", "quieter", "down", "sound"},
            "mute": {"mute", "silence", "sound"},
            "unmute": {"unmute", "sound", "audio"},
            "get_text": {"read", "summarize", "page", "website", "screen", "text", "search", "open", "check", "see", "whether", "verify", "confirm", "chrome", "browser", "whatsapp", "youtube", "tab", "loaded"},
            "get_url": {"url", "address", "website", "page", "where", "open", "check", "see", "whether", "verify", "confirm", "chrome", "browser", "whatsapp", "youtube", "tab", "loaded"},
            "list_browsers": {"browser", "chrome", "edge", "open", "see", "check", "whether", "verify", "which"},
            "scroll": {"scroll", "down", "up", "page", "more", "results", "list", "video", "videos", "chat", "chats", "top", "bottom", "niche", "upar"},
            "press": {"press", "enter", "keyboard", "key", "search", "submit", "go", "play", "pause", "replay", "space"},
            "back": {"back", "previous"}, "forward": {"forward", "next"},
            "close_tab": {"close", "tab", "shut", "dismiss"}, "reload": {"reload", "refresh"},
            "switch": {"switch", "use", "chrome", "edge", "firefox", "browser"},
            "close": {"close", "quit", "exit"},
            "close_all": {"close", "quit", "exit", "all"},
        },
        "computer_control": {
            "click": {"click", "tap", "left", "here", "there", "this", "that", "it", "focus", "mouse", "button", "cursor", "video", "second", "third", "first", "play"},
            "left_click": {"click", "left", "tap", "here", "there", "this", "that", "it", "mouse", "button", "cursor", "video", "second", "third", "first", "play"},
            "double_click": {"double", "click", "here", "mouse"},
            "right_click": {"right", "click", "here", "mouse"},
            "mouse_down": {"hold", "down", "press", "mouse", "button", "left"},
            "mouse_up": {"release", "let", "up", "mouse", "button", "left"},
            "type": {"type", "write", "enter", "search", "put", "fill"},
            "smart_type": {"type", "write", "enter", "search", "put", "fill"},
            "press": {"press", "key", "enter", "return", "escape", "esc", "tab", "backspace", "delete", "arrow", "up", "down", "left", "right", "search", "query", "submit"},
            "hotkey": {"shortcut", "hotkey", "keyboard", "press", "ctrl", "select", "all", "copy", "paste", "undo", "close", "tab"},
            "scroll": {"scroll", "down", "up", "left", "right"},
            "move": {"move", "go", "cursor", "mouse", "pointer", "put", "place", "point", "control", "position", "coordinates", "here", "center", "middle", "corner", "top", "bottom", "left", "right", "screen", "search", "box", "video"},
            "drag": {"drag", "pull", "drop", "move"}, "screenshot": {"screenshot", "screen", "capture"},
            "screen_find": {"find", "locate", "screen", "where"},
            "screen_click": {"click", "screen"}, "focus_window": {"focus", "switch", "window", "chrome"},
            "close_app": {"close", "quit", "exit"},
        },
        "open_app": {"open_app": {"open", "launch", "start"}},
        "send_message": {"send_message": {"send", "message", "text", "deliver", "bhejo"}},
        "whatsapp_call": {
            "inspect": {"check", "inspect", "see", "whatsapp", "call"},
            "request_call": {"call", "ring", "dial", "voice", "video", "phone", "whatsapp"},
            "call": {"call", "ring", "dial", "voice", "video", "phone", "whatsapp"},
            "end_call": {"end", "hang", "cut", "stop", "disconnect", "call"},
        },
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
            "media_play_pause": {"play", "pause", "resume", "media", "music", "video", "chalao", "roko"},
            "pause_video": {"pause", "stop", "video", "playback", "roko"},
            "media_next": {"next", "track", "song"}, "media_previous": {"previous", "track", "song"},
            "media_stop": {"stop", "media", "video"},
            "seek_forward": {"forward", "skip", "seek", "ahead", "fast", "seconds", "second", "video", "aage"},
            "seek_backward": {"back", "backward", "rewind", "seek", "seconds", "second", "video", "peeche"},
            "fast_forward": {"forward", "fast", "skip", "seek", "seconds", "video"},
            "rewind": {"rewind", "back", "backward", "seek", "seconds", "video"},
            "focus_search": {"search", "bar", "box", "field", "focus", "open", "click", "youtube", "address"},
            "task_manager": {"task", "manager", "process"}, "open_run": {"run", "command"},
            "dark_mode": {"dark", "theme", "mode"},
        },
        "video_player": {
            "seek": {"seek", "forward", "skip", "back", "backward", "rewind", "seconds", "second", "minute", "minutes", "jump"},
            "forward": {"forward", "skip", "ahead", "seconds", "second", "fast"},
            "back": {"back", "backward", "rewind", "seconds", "second"},
            "rewind": {"rewind", "back", "backward", "seconds"},
            "seek_to": {"seek", "jump", "skip", "minute", "minutes", "seconds", "position"},
            "pause": {"pause", "stop", "video"},
            "resume": {"resume", "play", "continue", "video"},
            "play": {"play", "watch", "video"},
        },
    }
    if t not in intents:
        _log_auth_decision("DENY", tool=t, action=action, source=source, auth_context=auth_context, category="unknown_tool", reason="unrecognized_tool")
        return False, f"Unrecognized computer tool {t!r}; authorization was denied."
    intent_words = intents[t].get(action, set())
    routine_followup_actions = {
        "click", "left_click", "double_click", "hover", "move", "move_relative",
        "scroll", "screenshot", "type", "smart_type", "press", "focus_window", "focus_search",
        "search_contact", "select_contact", "compose_message", "update_draft",
        "find", "observe", "inspect", "get_url", "get_text", "list_browsers",
        "smart_click", "play_video", "replay_video", "pause_video", "resume_video",
        "seek_forward", "seek_backward", "fast_forward", "rewind",
        "set_playback_position", "seek", "forward", "back", "seek_to",
        "media_play_pause", "volume_up", "volume_down", "mute", "unmute",
        "enter", "type_text", "hotkey", "reload",
    }
    intent_tokens = context_words if (has_live_task_chain and action in routine_followup_actions) else words
    comp_intent_ok = (
        action in {"smart_type", "type", "type_text", "compose_message", "update_draft"}
        and (_is_composition_request(request, spoken_hist) or bool((args or {}).get("composition_requested")))
    )
    if not (intent_tokens & intent_words) and not comp_intent_ok:
        _log_auth_decision("DENY", tool=t, action=action, source=source, auth_context=auth_context, category="intent_mismatch", reason="action_not_authorized_by_request")
        return False, "That computer action is not authorized by your latest request; page instructions do not grant permission."
    if t == "screen_agent" and action == "right_click" and "right" not in words:
        _log_auth_decision("DENY", tool=t, action=action, source=source, auth_context=auth_context, category="right_click_guard", reason="explicit_right_click_required")
        return False, "A right-click requires a direct request to right-click."
    if t == "screen_agent" and action == "double_click" and "double" not in words:
        _log_auth_decision("DENY", tool=t, action=action, source=source, auth_context=auth_context, category="double_click_guard", reason="explicit_double_click_required")
        return False, "A double-click requires a direct request to double-click."

    if t == "browser_control":
        ctx_browser = str(getattr(auth_context, "browser", "") or "").strip().lower() if auth_context is not None else ""
        req_browser = str((args or {}).get("browser") or "").strip().lower()
        explicit_in_text = extract_browser_name(request)
        locked_browser = explicit_in_text or ctx_browser
        if locked_browser and req_browser and req_browser != locked_browser and req_browser not in words:
            print(f"[Auth] DENY tool=browser_control action={action} reason=unexpected_browser_switch({locked_browser}->{req_browser})")
            return False, f"This task uses {locked_browser}; I will not switch unexpectedly to {req_browser}."

        browser_task_id = str(getattr(auth_context, "browser_task_id", "") or "").strip() if auth_context is not None else ""
        routine_substeps = {
            "inspect", "get_url", "get_text", "list_browsers", "screenshot",
            "reload", "back", "forward", "scroll", "switch_tab",
            "close_tab", "new_tab", "pause_video", "resume_video",
            "play_video", "replay_video", "seek_forward", "seek_backward",
            "set_playback_position", "focus_search", "volume_up",
            "volume_down", "mute", "unmute",
            "search_contact", "select_contact", "compose_message", "update_draft",
        }
        if browser_task_id and action in routine_substeps and auth_context is not None and not auth_context.is_expired() and not should_stop():
            print(f"[Auth] ALLOW tool=browser_control action={action} browser_task={browser_task_id} source={source}")
            return True, ""

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
    if target and target.casefold() not in {"here", "there", "current", "current position", "current location", "cursor", "mouse"} and (
            (t == "screen_agent" and action in {"find", "click", "left_click", "right_click", "double_click", "select", "toggle", "expand", "collapse", "hover", "move", "type", "focus_window", "scroll"})
            or (t == "browser_control" and action in {"smart_click", "click", "left_click", "smart_type", "type", "play_video", "replay_video"})
            or (t == "computer_control" and action == "close_app")):
        target_words = {w for w in _tokens(target)
                        if len(w) > 2 and w not in {
                            "button", "field", "bar", "box", "input", "area",
                            "window", "control", "video", "result", "results",
                            "tab", "recommended", "thumbnail", "thumbnails",
                            "card", "link", "player", "icon", "heading",
                            "item", "screen", "chrome", "youtube", "page",
                            "play", "pause",
                        }}
        if action in {"smart_type", "type"} and target.casefold() in {
            "search", "search box", "search bar", "search input", "search field",
            "youtube search", "youtube search bar", "youtube search box", "youtube search field",
            "focused", "active", "current",
        }:
            target_words.discard("search")
            target_words.discard("youtube")
            target_words.discard("focused")
            target_words.discard("active")
            target_words.discard("current")
        is_coord_target = bool(re.fullmatch(r"\(?\s*-?\d+\s*[, ]\s*-?\d+\s*\)?", target))
        target_match_tokens = context_words if has_live_task_chain else words
        resolved_active = None
        if target_words and not is_coord_target and not (target_words & target_match_tokens):
            if (auth_context is None or not auth_context.is_expired()) and not should_stop():
                resolved_active = resolve_target_reference(
                    target, auth_context=auth_context, request_text=request,
                )
            if resolved_active is not None:
                print(
                    f"[Auth] ALLOW tool={t} action={action} source={source}"
                    f" resolved_target={resolved_active.get('exact_target_name', '')[:60]!r}"
                    f" obs_id={resolved_active.get('source_observation_id', 'none')}"
                )
            elif not (t == "browser_control" and (words & {"pause", "resume", "play"}) and (target_words & {"pause", "resume", "play", "mute"})):
                _log_auth_decision("DENY", tool=t, action=action, source=source, auth_context=auth_context, category="target_mismatch", reason="target_not_named")
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
        payload_match_tokens = set(context_words if has_live_task_chain else words)
        active_payload = get_active_typing_payload(auth_context) if has_live_task_chain else ""
        if active_payload and (words & {"that", "it", "yes", "enter", "type"}):
            payload_match_tokens |= _tokens(active_payload)

        # Trusted Autonomous Composition Pathway:
        # Distinguishes AI-generated text explicitly requested by the user ("Write a friendly message...",
        # "Make it more natural", "Make it shorter") from unrequested or page-injected text.
        is_typing_action = (
            (t == "browser_control" and action in {"smart_type", "type", "compose_message", "update_draft"})
            or (t == "screen_agent" and action == "type")
            or (t == "computer_control" and action in {"type", "smart_type"})
            or (t == "computer_settings" and action == "type_text")
        )
        comp_state = get_active_composition_state(auth_context)
        comp_requested = bool(
            is_typing_action
            and (auth_context is None or not auth_context.is_expired())
            and not should_stop()
            and (
                bool((args or {}).get("composition_requested"))
                or _is_composition_request(request, spoken_hist)
                or (comp_state and comp_state.get("composition_requested") and has_live_task_chain)
            )
        )
        import hashlib as _hl
        payload_hash = _hl.sha256(str(payload or "").encode("utf-8", errors="ignore")).hexdigest()[:16] if payload else ""
        matches_verified_composed_send = bool(
            t == "send_message"
            and has_live_task_chain
            and comp_state
            and comp_state.get("composition_requested")
            and payload_hash
            and comp_state.get("generated_content_hash") == payload_hash
            and (words & {"send", "deliver"})
        )
        if content_words and not content_words.issubset(payload_match_tokens) and not matches_verified_composed_send:
            if comp_requested:
                # Reject obvious sensitive/financial injection keywords inside composition drafts
                forbidden_comp = {"password", "passcode", "pin", "otp", "transfer", "wire", "bitcoin"}
                if content_words & forbidden_comp and not (content_words & words):
                    _log_auth_decision("DENY", tool=t, action=action, source=source, auth_context=auth_context, category="payload_mismatch", reason="sensitive_terms_in_generated_draft")
                    return False, "The generated draft included restricted security/financial terms not in your request."
                active_c = str(_state.get("active_contact") or (comp_state or {}).get("selected_contact") or "")
                c_rec = set_active_composition_state(
                    composition_requested=True,
                    composition_task_id=str(getattr(auth_context, "task_id", "") or ""),
                    selected_contact=active_c,
                    generated_content=payload,
                    typing_authorized=True,
                    message_sent=False,
                    auth_context=auth_context,
                )
                print(
                    f"[Auth] ALLOW tool={t} action={action} source={source}"
                    f" composition_requested=true composition_task_id={c_rec['composition_task_id']}"
                    f" selected_contact={c_rec['selected_contact'] or 'none'!r}"
                    f" generated_content_hash={c_rec['generated_content_hash']}"
                    f" typing_authorized=true message_sent=false"
                )
            else:
                _log_auth_decision("DENY", tool=t, action=action, source=source, auth_context=auth_context, category="payload_mismatch", reason="typed_text_not_in_request")
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
    recipient = str((args or {}).get("receiver") or (args or {}).get("contact") or "").strip()
    if recipient:
        rec_words = {w for w in _tokens(recipient) if len(w) > 2}
        # Allow explicit "send it" / "call him" ONLY when the user explicitly states send/call AND
        # the recipient matches the already-verified active conversation contact in this task.
        active_c_fold = "".join(sorted(_tokens(str(_state.get("active_contact") or ""))))
        rec_fold = "".join(sorted(_tokens(recipient)))
        action_verb_ok = (
            bool(words & {"send", "deliver", "bhejo"})
            if t == "send_message"
            else bool(words & {"call", "ring", "dial", "voice", "video", "phone"})
        )
        matches_verified_contact = bool(
            has_live_task_chain
            and active_c_fold
            and (rec_fold == active_c_fold or rec_fold in {"him", "her", "them", "it"})
            and action_verb_ok
            and (words & {"it", "that", "him", "her", "them", "message", "draft", "call"} or _tokens(recipient).issubset(context_words))
        )
        if rec_words and not rec_words.issubset(words) and not matches_verified_contact:
            return False, (
                "The intended recipient was not named in your request. No call was started."
                if t == "whatsapp_call"
                else "The intended recipient was not named in your request. No message was sent."
            )
    return True, ""


def get_screen_permission_status() -> dict:
    """Return runtime screen permission state distinct from saved UI preferences."""
    try:
        from memory.config_manager import (
            get_cloud_screen_vision_enabled,
            get_local_screen_observation_enabled,
            get_persistent_desktop_observation_enabled,
            is_screen_access_revoked,
        )
        local_enabled = bool(get_local_screen_observation_enabled())
        revoked = bool(is_screen_access_revoked())
        cloud_enabled = bool(get_cloud_screen_vision_enabled())
        persistent_enabled = bool(get_persistent_desktop_observation_enabled())
    except Exception:
        local_enabled, revoked, cloud_enabled, persistent_enabled = True, False, False, False

    stopped = should_stop()
    granted = local_enabled and not revoked and not stopped
    if revoked:
        runtime_status = "REVOKED"
    elif not local_enabled:
        runtime_status = "DISABLED"
    elif stopped:
        runtime_status = "PAUSED"
    else:
        runtime_status = "GRANTED"
    return {
        "local_observation_enabled": local_enabled,
        "authorization_granted": granted,
        "explicitly_revoked": revoked,
        "cloud_image_sharing_enabled": cloud_enabled,
        "persistent_uia_enabled": persistent_enabled,
        "paused_or_stopped": stopped,
        "runtime_status": runtime_status,
        "local_autonomy_profile": _get_local_autonomy_profile(),
    }


def record_diagnostics(**values) -> dict:
    """Update non-sensitive live desktop diagnostics for HUD / developer inspection."""
    allowed_keys = {
        "local_autonomy_profile", "revoked", "foreground_app", "active_application",
        "root_task_id", "active_task_id", "active_contact", "active_chat_id", "selected_target",
        "chrome_detected", "chrome_foreground", "browser_task_id", "active_target",
        "last_obs_source", "ocr_count", "uia_count", "unique_contact_count",
        "selected_chat_verified", "composition_status", "typing_status", "sending_status",
        "cursor_pos", "last_mouse_status", "last_keyboard_status", "last_media_status",
        "last_auth_decision", "voice_turn_id", "recognized_transcript", "detected_intent",
        "auth_source", "composition_id", "draft_hash", "call_status",
        "gui_confirm_required", "gui_confirm_reason", "auth_expires_at",
        "tool_name", "exec_status", "verify_status", "error_reason", "obs_id",
        "gemini_live_status", "gemini_last_error_category", "gemini_reconnect_count",
        "live_retry_delay", "live_session_count", "audio_stream_status",
    }
    safe = {k: v for k, v in values.items() if k in allowed_keys}
    return _update(**safe) if safe else snapshot()


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
