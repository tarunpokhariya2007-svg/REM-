"""Closed-loop, accessibility-first control of the active desktop window.

One tool call performs one bounded operation. Every call reads a fresh view;
coordinates from previous observations are never retained.
"""
from __future__ import annotations

import threading
import time

from core import desktop_control, screenact, screenview
from core.appidentity import fold

_log_lock = threading.Lock()


def _publish(player=None):
    if player is None:
        return
    try:
        player.set_desktop_control_status(desktop_control.snapshot())
    except Exception:
        pass


def _view():
    return screenview.look(budget_s=1.2, allow_ocr=True)


def _describe(view, cap=65):
    if view.source in ("uia", "ax", "atspi"):
        controls = []
        for el in view.elements[:cap]:
            row = {"id": el.eid, "role": el.role,
                   "name": el.name or el.value or "(unnamed)",
                   "enabled": el.enabled}
            if el.focused:
                row["focused"] = True
            if el.protected:
                row["protected"] = True
            controls.append(row)
    else:
        controls = [{"role": el.role, "text": el.name or el.value}
                    for el in view.elements[:cap]]
    return {
        "app": view.app or "unknown", "title": view.title or "(untitled)",
        "source": view.source, "degraded": view.degraded,
        "reason": view.reason, "scan_ms": round(view.scan_ms, 1),
        "controls": controls,
        "windows": [{"app": a, "title": t} for a, t in view.windows[:35]],
    }


def _resolve(view, target: str, *, roles=(), generic_search=False):
    """Resolve a semantic target; refuse ties instead of selecting .first."""
    text = str(target or "").strip()
    if not text:
        return None, "Tell me which control to use."
    pool = [e for e in view.elements if e.interactable and (not roles or e.role in roles)]
    if generic_search and fold(text) in {"search", "search bar", "search field", "search box"}:
        search = [e for e in pool if e.role in (screenview.ROLE_EDIT, screenview.ROLE_COMBOBOX)
                  and "search" in fold(e.label)]
        candidates = search or [e for e in pool if e.role in
                                (screenview.ROLE_EDIT, screenview.ROLE_COMBOBOX)]
    else:
        candidates = view.find(text, roles=roles, interactable_only=True, limit=20)
    candidates = list(dict.fromkeys(candidates))
    if not candidates:
        return None, f"I could not identify an enabled control matching {text!r}."
    if len(candidates) > 1:
        exact = [e for e in candidates if fold(e.label) == fold(text)]
        if len(exact) == 1:
            return exact[0], ""
        details = [f"{e.role}: {e.label or '(unnamed)'}" for e in candidates[:6]]
        return None, "I found multiple possible controls: " + "; ".join(details) + ". Which one?"
    return candidates[0], ""


def _verified_after(before, *, target=None, typed_text=None):
    # A fresh accessibility read after the action, bounded to avoid hanging on
    # applications whose UI Automation provider is slow.
    time.sleep(0.12)
    after = _view()
    if typed_text is not None:
        if target and after.app == before.app:
            matches = [e for e in after.elements if e.role in
                       (screenview.ROLE_EDIT, screenview.ROLE_COMBOBOX,
                        screenview.ROLE_TEXT)]
            if any(typed_text.casefold() in (e.value or "").casefold() for e in matches):
                return after, "verified: entered text is present in the field"
        return after, "unverified: fresh view did not expose the entered field value"
    if target:
        focused = [e for e in after.elements if e.focused and e.role == target.role
                   and (e.name == target.name or e.rect == target.rect)]
        if focused:
            return after, "verified: intended control has keyboard focus"
    if after.app == before.app and after.digest != before.digest:
        return after, "verified: the interface changed after the action"
    if after.app != before.app:
        return after, "verified: active application changed"
    return after, "unverified: no visible state change was observed"


_CONSEQUENTIAL_LABELS = ("send", "submit", "delete", "remove", "purchase",
                         "buy", "place order", "pay", "transfer", "share",
                         "install", "allow access", "disable security")


def _confirmed_click(target_label: str, app: str, title: str, player=None) -> str:
    """After a human approves, re-read and re-resolve the live target."""
    try:
        with desktop_control.action("confirmed click"):
            view = _view()
            if view.app != app or view.title != title:
                return "The active window changed while confirmation was pending; nothing was clicked."
            target, why = _resolve(view, target_label)
            if why or target is None:
                return why or "The target is no longer available; nothing was clicked."
            result = screenact.click(target, expect_process=view.app)
            after, verification = _verified_after(view, target=target)
            desktop_control.report("confirmed click", verification, after.app)
            _publish(player)
            return f"{result}; {verification}."
    except Exception as exc:
        return f"Confirmed action stopped safely: {exc}"


def _confirmed_press(key: str, app: str, title: str, player=None) -> str:
    try:
        with desktop_control.action("confirmed form submit"):
            view = _view()
            if view.app != app or view.title != title:
                return "The active window changed while confirmation was pending; nothing was submitted."
            focused = next((e for e in view.elements if e.focused), None)
            if not focused or focused.role != screenview.ROLE_EDIT or focused.protected:
                return "The original form field is no longer focused; nothing was submitted."
            result = screenact.press(key)
            after, verification = _verified_after(view)
            desktop_control.report("confirmed form submit", verification, after.app)
            _publish(player)
            return f"{result}; {verification}."
    except Exception as exc:
        return f"Confirmed submission stopped safely: {exc}"


def screen_agent(parameters: dict, player=None, speak=None, response=None,
                 session_memory=None) -> str:
    params = parameters or {}
    action = str(params.get("action", "observe")).strip().lower().replace("-", "_")
    if action in ("stop", "emergency_stop"):
        state = desktop_control.emergency_stop()
        _publish(player)
        return "Computer control stopped immediately. Mouse and keyboard inputs were released."
    if action == "pause":
        desktop_control.pause(); _publish(player)
        return "Computer control is paused."
    if action == "resume":
        desktop_control.resume(); _publish(player)
        return "Computer control resumed."
    if action == "set_mode":
        try:
            desktop_control.set_mode(params.get("mode", "ON-DEMAND"))
        except ValueError as exc:
            return str(exc)
        _publish(player)
        return f"Computer-use mode is now {desktop_control.snapshot()['mode']}."

    if action not in {"observe", "find", "click", "type", "press", "scroll", "focus_window"}:
        return "Supported actions: observe, find, click, type, press, scroll, focus_window, pause, resume, stop, set_mode."

    try:
        with desktop_control.action(action):
            _publish(player)
            before = _view()
            desktop_control.report(action, "observed current screen", before.app)
            _publish(player)
            if action in ("observe", "find"):
                if action == "find":
                    target, why = _resolve(before, params.get("target", ""),
                                           generic_search=True)
                    result = {"screen": _describe(before),
                              "match": ({"role": target.role,
                                         "name": target.label,
                                         "enabled": target.enabled}
                                        if target else None),
                              "clarification": why}
                else:
                    result = _describe(before)
                desktop_control.report(action, "observed" if not before.degraded else
                                       "degraded observation; see source and reason", before.app)
                _publish(player)
                return str(result)

            if action == "focus_window":
                app = str(params.get("target", "")).strip()
                if not app:
                    return "Name the application window to focus."
                result = screenact.focus_app(app)
                after = _view()
                ok = bool(after.title and fold(app) in fold(after.app + " " + after.title))
                verification = ("verified: matching window is active" if ok else
                                "unverified: requested window is not active")
                desktop_control.report(action, verification, after.app)
                _publish(player)
                return f"{result}; {verification}. Active: {after.app} — {after.title}"

            if action in ("click", "type"):
                target_text = str(params.get("target", "")).strip()
                roles = ((screenview.ROLE_EDIT, screenview.ROLE_COMBOBOX)
                         if action == "type" else ())
                target, why = _resolve(before, target_text, roles=roles,
                                       generic_search=(action == "type"))
                if (action == "type" and why and fold(target_text) in
                        {"editor", "document", "text editor", "writing area"}):
                    surfaces = [e for e in before.elements
                                if e.role == screenview.ROLE_TEXT and e.enabled
                                and not e.protected
                                and (e.focused or fold(target_text) in fold(e.label))]
                    if len(surfaces) == 1:
                        target, why = surfaces[0], ""
                    elif len(surfaces) > 1:
                        why = "I found multiple editable-looking text surfaces; name the intended editor."
                if why:
                    desktop_control.report(action, "not executed: target ambiguous or missing", before.app)
                    _publish(player)
                    return why
                if action == "click":
                    if any(word in fold(target.label) for word in _CONSEQUENTIAL_LABELS):
                        from core import confirm
                        if confirm.pending_title():
                            return "Another confirmation is already waiting; no click was made."
                        return confirm.request(
                            key="desktop-consequential-click",
                            title=f"Confirm desktop action: {target.label[:70]}?",
                            detail=f"Review the active {before.app} window before approving.",
                            run=lambda: _confirmed_click(target_text, before.app,
                                                         before.title, player),
                        )
                    outcome = screenact.click(target, expect_process=before.app)
                else:
                    text = str(params.get("text", ""))
                    if not text:
                        return "Tell me what text to enter."
                    if target.protected:
                        return "That field is protected; enter the secret yourself."
                    outcome = screenact.type_text(text, target=target,
                        clear_first=bool(params.get(
                            "clear_first",
                            any(term in fold(target.label) for term in
                                ("search", "query", "address", "url")))))
                if not outcome.ok:
                    desktop_control.report(action, f"failed: {outcome.error}", before.app)
                    _publish(player)
                    return str(outcome)
                after, verification = _verified_after(before, target=target,
                    typed_text=(str(params.get("text", "")) if action == "type" else None))
                desktop_control.report(action, verification, after.app)
                _publish(player)
                return f"{outcome}; {verification}. Current window: {after.title or after.app}."

            if action == "press":
                key = str(params.get("key", "")).strip()
                allowed = {"enter", "tab", "escape", "esc", "up", "down", "left", "right",
                           "home", "end", "pageup", "pagedown", "ctrl+a", "ctrl+c", "ctrl+v",
                           "ctrl+f", "ctrl+l", "ctrl+tab", "ctrl+shift+tab", "alt+tab", "f5"}
                if key.casefold() not in allowed:
                    return "That key or shortcut is not on the safe allowlist for desktop control."
                if key.casefold() == "enter":
                    focused = next((e for e in before.elements if e.focused), None)
                    label = fold(focused.label if focused else "")
                    if any(word in label for word in ("message", "reply", "chat")):
                        return "I will not submit a message through a generic Enter key. Use the message action with its confirmation step."
                    is_search = any(word in label for word in ("search", "query", "address", "url"))
                    if focused and focused.role == screenview.ROLE_EDIT and not is_search:
                        from core import confirm
                        if confirm.pending_title():
                            return "Another confirmation is already waiting; nothing was submitted."
                        return confirm.request(
                            key="desktop-form-submit",
                            title="Confirm submitting this form?",
                            detail=f"Enter is focused in {before.app}: {focused.label or 'an unnamed field'}.",
                            run=lambda: _confirmed_press(key, before.app,
                                                         before.title, player),
                        )
                outcome = screenact.press(key)
                if not outcome.ok:
                    desktop_control.report(action, f"failed: {outcome.error}", before.app)
                    _publish(player)
                    return str(outcome)
                after, verification = _verified_after(before)
                desktop_control.report(action, verification, after.app)
                _publish(player)
                return f"{outcome}; {verification}."

            if action == "scroll":
                direction = str(params.get("direction", "down")).lower()
                if direction not in ("up", "down", "left", "right"):
                    return "Scroll direction must be up, down, left, or right."
                target, why = _resolve(before, params.get("target", "scroll area"),
                                       roles=())
                if why:
                    scrollables = [e for e in before.elements if e.scrollable and e.enabled]
                    if len(scrollables) == 1:
                        target = scrollables[0]
                    elif len(scrollables) > 1:
                        return "Multiple scrollable areas are visible; name which panel to scroll."
                    else:
                        return "No accessible scrollable control is visible. I did not guess a cursor position."
                outcome = screenact.scroll(direction, int(params.get("amount", 3)), target=target)
                if not outcome.ok:
                    desktop_control.report(action, f"failed: {outcome.error}", before.app)
                    _publish(player)
                    return str(outcome)
                after, verification = _verified_after(before)
                desktop_control.report(action, verification, after.app)
                _publish(player)
                return f"{outcome}; {verification}."
    except RuntimeError as exc:
        _publish(player)
        return str(exc)
    except Exception as exc:
        desktop_control.report(action, f"failed: {type(exc).__name__}: {exc}")
        _publish(player)
        return f"Desktop action failed safely: {type(exc).__name__}: {exc}"


TOOL = {
    "name": "desktop_ui",
    "tier": "core",
    "brief": "Observe desktop controls and act on fresh, verified UI elements.",
    "description": (
        "Closed-loop Windows/macOS/Linux desktop control. For native apps, use "
        "screen_process for read-only visual questions about what is on screen, "
        "visible text, errors, or project contents; it includes the screenshot "
        "for the vision model. "
        "Use this tool instead of raw-coordinate computer_control: each action reads "
        "a fresh accessibility tree first, identifies real controls, refuses "
        "ambiguous targets, acts once, then re-reads the interface to verify. "
        "Use observe to inspect the active app and controls; find for a named "
        "control; click/type with target set to the visible label (for 'search "
        "bar' use that exact phrase); press for allowlisted keys; scroll for a "
        "named scroll area; focus_window for an app title. If it reports more "
        "than one match, ask which one. Password fields are never typed into. "
        "For webpage elements and Chrome DOM use browser_control, which uses "
        "Playwright semantic roles/labels. Launch apps with open_app. Never "
        "claim success when the verification says unverified. The user can "
        "always stop/pause control from the visible REM desktop panel."
    ),
    "parameters": {"type": "OBJECT", "properties": {
        "action": {"type": "STRING", "description": "observe | find | click | type | press | scroll | focus_window | set_mode | pause | resume | stop"},
        "target": {"type": "STRING", "description": "Visible control name or app/window title. For search fields say search bar."},
        "text": {"type": "STRING", "description": "Text to type into the named, non-protected field."},
        "key": {"type": "STRING", "description": "Allowlisted key or shortcut (Enter, Escape, Tab, Ctrl+F, Alt+Tab, etc.)."},
        "direction": {"type": "STRING", "description": "up | down | left | right"},
        "amount": {"type": "INTEGER", "description": "Scroll notches, 1–20."},
        "clear_first": {"type": "BOOLEAN", "description": "Clear the named field before typing. Defaults true."},
        "mode": {"type": "STRING", "description": "ON-DEMAND | LIVE ASSIST | COMPUTER CONTROL."},
    }, "required": ["action"]},
    "handler": screen_agent,
}
