"""Closed-loop, accessibility-first control of the active desktop window.

One tool call performs one bounded operation. Every call reads a fresh view;
coordinates from previous observations are never retained.
"""
from __future__ import annotations

import re
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
                   "enabled": el.enabled, "bounds": list(el.rect),
                   "depth": el.depth}
            if el.value and not el.protected:
                row["value"] = el.value
            if el.automation_id:
                row["automation_id"] = el.automation_id
            if el.control_type:
                row["control_type"] = el.control_type
            if el.parent_name:
                row["parent"] = el.parent_name
            if el.patterns:
                row["patterns"] = list(el.patterns)
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

_SINGLE_KEYS = frozenset({
    "enter", "tab", "esc", "space", "backspace", "delete", "insert",
    "up", "down", "left", "right", "home", "end", "pageup", "pagedown",
    "ctrl", "alt", "shift", "winleft",
    *(f"f{i}" for i in range(1, 13)),
})
_SAFE_SHORTCUTS = frozenset({
    "ctrl+a", "ctrl+c", "ctrl+v", "ctrl+x", "ctrl+z", "ctrl+y",
    "ctrl+s", "ctrl+f", "ctrl+n", "ctrl+t", "ctrl+w", "ctrl+l",
    "ctrl+shift+t", "ctrl+tab", "ctrl+shift+tab", "alt+tab",
    "alt+f4", "f5", "shift+tab",
    "winleft+d", "winleft+e", "winleft+r", "winleft+l", "winleft+tab",
})


def _normalize_key_request(value: str) -> str:
    aliases = {"return": "enter", "escape": "esc", "control": "ctrl",
               "windows": "winleft", "win": "winleft", "start": "winleft",
               "pgup": "pageup", "pgdn": "pagedown", "del": "delete",
               "ins": "insert", "spacebar": "space", "bksp": "backspace"}
    text = str(value or "").strip().casefold()
    phrase_map = {
        "select all": "ctrl+a",
        "copy": "ctrl+c",
        "paste": "ctrl+v",
        "cut": "ctrl+x",
        "undo": "ctrl+z",
        "redo": "ctrl+y",
    }
    if text in phrase_map:
        return phrase_map[text]
    text = re.sub(r"\bpage\s+(up|down)\b", r"page\1", text)
    parts = [part for part in re.split(r"[+\s]+", text)
             if part]
    return "+".join(aliases.get(part, part) for part in parts)


def _key_request_allowed(value: str) -> bool:
    normalized = _normalize_key_request(value)
    return normalized in _SINGLE_KEYS or normalized in _SAFE_SHORTCUTS


def _confirmed_click(target_label: str, app: str, title: str, player=None,
                     click_action: str = "click") -> str:
    """After a human approves, re-read and re-resolve the live target."""
    try:
        with desktop_control.action("confirmed click"):
            view = _view()
            if view.app != app or view.title != title:
                return "The active window changed while confirmation was pending; nothing was clicked."
            target, why = _resolve(view, target_label)
            if why or target is None:
                return why or "The target is no longer available; nothing was clicked."
            if click_action == "right_click":
                result = screenact.right_click(target, expect_process=view.app)
            elif click_action == "double_click":
                result = screenact.double_click(target, expect_process=view.app)
            else:
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


def _confirmed_shortcut(key: str, app: str, title: str, player=None) -> str:
    """Revalidate the active window after approval before a close shortcut."""
    try:
        with desktop_control.action("confirmed window close"):
            view = _view()
            if view.app != app or view.title != title:
                return "The active window changed while confirmation was pending; nothing was closed."
            result = screenact.press(key)
            if not result.ok:
                return str(result)
            after, verification = _verified_after(view)
            desktop_control.report("confirmed window close", verification, after.app)
            _publish(player)
            return f"{result}; {verification}."
    except Exception as exc:
        return f"Confirmed window close stopped safely: {exc}"


def _try_browser_dom_point(before, target_text: str):
    """If a browser window is active and UIA lacked page DOM controls, resolve
    verified desktop coordinates from an attached browser session."""
    try:
        from actions import browser_control
        pt = browser_control.locate_control_screen_point(target_text, app_hint=before.app)
        if pt and isinstance(pt, dict) and "x" in pt and "y" in pt:
            return screenact.Point(
                int(pt["x"]), int(pt["y"]),
                origin="element",
                label=str(target_text or pt.get("url") or "browser control"),
            )
    except Exception:
        pass
    return None


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

    if action not in {"observe", "find", "click", "left_click", "right_click", "double_click", "mouse_down", "mouse_up", "select", "toggle", "expand", "collapse", "hover", "move", "move_relative", "type", "press", "scroll", "focus_window"}:
        return "Supported actions: observe, find, click, left_click, right_click, double_click, mouse_down, mouse_up, select, toggle, expand, collapse, hover, move, move_relative, type, press, scroll, focus_window, pause, resume, stop, set_mode."

    try:
        if action in ("observe", "find"):
            from memory.config_manager import (
                get_local_screen_observation_enabled,
                is_screen_access_revoked,
            )
            if not get_local_screen_observation_enabled():
                revoked = is_screen_access_revoked()
                desktop_control.report(
                    action,
                    "refused: screen access revoked" if revoked else "refused: local screen observation disabled",
                )
                _publish(player)
                if revoked:
                    return ("status=failed action=desktop_ui reason=\"Local screen observation was "
                            "explicitly revoked by the user. Enable LOCAL SCREEN OBSERVATION in the "
                            "COMPUTER USE panel.\"")
                return ("status=failed action=desktop_ui reason=\"Local screen observation is "
                        "currently disabled. Enable LOCAL SCREEN OBSERVATION in the "
                        "COMPUTER USE panel.\"")
        with desktop_control.action(action):
            _publish(player)
            if action == "mouse_down":
                outcome = screenact.mouse_down(button=str(params.get("button") or "left"))
                desktop_control.report(action, str(outcome))
                _publish(player)
                return str(outcome)
            if action == "mouse_up":
                outcome = screenact.mouse_up(button=str(params.get("button") or "left"))
                desktop_control.report(action, str(outcome))
                _publish(player)
                return str(outcome)

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
                    desc = _describe(before)
                    try:
                        from core.screen_observation import inspect_local_screen
                        local_detail = inspect_local_screen(str(params.get("text") or ""))
                        if "Local OCR text:" in local_detail or "Browser windows/tabs" in local_detail:
                            desc = {"uia_view": desc, "local_screen": local_detail}
                    except Exception:
                        pass
                    result = desc
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

            if action == "move_relative":
                dx, dy = int(params.get("dx", 0)), int(params.get("dy", 0))
                outcome = screenact.move_relative(dx, dy)
                verification = "cursor moved" if outcome.ok else f"failed: {outcome.error}"
                desktop_control.report(action, verification, before.app)
                _publish(player)
                return str(outcome)

            if action == "move" and not str(params.get("target", "")).strip():
                if params.get("x") is not None and params.get("y") is not None:
                    pt = screenact.Point(int(params["x"]), int(params["y"]), origin="user", label="coordinates")
                    outcome = screenact.move_cursor(pt)
                else:
                    outcome = screenact.move_to_screen_position(params.get("position", ""))
                desktop_control.report(action, "cursor moved" if outcome.ok else
                                       f"failed: {outcome.error}", before.app)
                _publish(player)
                return str(outcome)

            if action in {"click", "left_click", "right_click", "double_click", "select", "toggle", "expand", "collapse", "hover", "move", "type"}:
                click_kind = "click" if action == "left_click" else action
                target_text = str(params.get("target", "")).strip()
                target_cf = target_text.casefold()
                named_pos = target_cf.replace(" ", "_")
                if click_kind in {"move", "hover"} and (
                    params.get("position")
                    or named_pos in {"center", "middle", "top_left", "top_right", "bottom_left", "bottom_right"}
                ):
                    outcome = screenact.move_to_screen_position(params.get("position") or named_pos)
                    desktop_control.report(action, "cursor moved" if outcome.ok else f"failed: {outcome.error}", before.app)
                    _publish(player)
                    return str(outcome)
                coord_match = re.fullmatch(r"\(?\s*(-?\d+)\s*[, ]\s*(-?\d+)\s*\)?", target_text)
                coord_x = params.get("x") if params.get("x") is not None else (int(coord_match.group(1)) if coord_match else None)
                coord_y = params.get("y") if params.get("y") is not None else (int(coord_match.group(2)) if coord_match else None)
                if coord_x is not None and coord_y is not None and click_kind in {"move", "hover", "click", "right_click", "double_click"}:
                    pt = screenact.Point(int(coord_x), int(coord_y), origin="user", label=target_text or "coordinates")
                    if click_kind in {"move", "hover"}:
                        outcome = screenact.move_cursor(pt)
                    elif click_kind == "right_click":
                        outcome = screenact.right_click(pt)
                    elif click_kind == "double_click":
                        outcome = screenact.double_click(pt)
                    else:
                        outcome = screenact.click(pt)
                    desktop_control.report(action, str(outcome), before.app)
                    _publish(player)
                    return str(outcome)
                # Level 3 direct click at current cursor position
                if click_kind in {"click", "right_click", "double_click"}:
                    if target_cf in {"", "here", "there", "current position", "current location", "cursor", "mouse"}:
                        outcome = screenact.click_current(
                            button=("right" if click_kind == "right_click" else "left"),
                            clicks=(2 if click_kind == "double_click" else 1),
                        )
                        desktop_control.report(action, str(outcome), before.app)
                        _publish(player)
                        return str(outcome)

                if click_kind in {"hover", "move"} and not target_text:
                    return "Name a visible control to move the cursor to."
                if click_kind == "type" and target_cf in {"", "here", "focused", "current", "active"}:
                    text = str(params.get("text", ""))
                    if not text:
                        return "Tell me what text to enter."
                    focused = next((e for e in before.elements if e.focused), None)
                    if focused and focused.protected:
                        return "That field is protected; enter the secret yourself."
                    outcome = screenact.type_text(
                        text,
                        clear_first=bool(params.get("clear_first", False)),
                    )
                    if not outcome.ok:
                        desktop_control.report(action, f"failed: {outcome.error}", before.app)
                        _publish(player)
                        return str(outcome)
                    after, verification = _verified_after(before, target=focused, typed_text=text)
                    desktop_control.report(action, verification, after.app)
                    _publish(player)
                    return f"{outcome}; {verification}. Current window: {after.title or after.app}."
                is_whatsapp_app = "whatsapp" in fold(f"{before.app} {before.title}")
                if is_whatsapp_app and click_kind in {"click", "left_click", "select", "type"}:
                    try:
                        from plugins import _whatsapp_core as _wa
                        is_search_hint = target_cf in {
                            "search", "search bar", "search box", "search field",
                            "contact search", "whatsapp search",
                        }
                        is_composer_hint = (
                            click_kind == "type"
                            and not is_search_hint
                            and target_cf in {"", "here", "focused", "current", "active", "message", "message box", "chat", "chat box", "composer", "input", "reply"}
                        )
                        if click_kind == "type" and is_search_hint:
                            sb = _wa.locate_whatsapp_search_bar(before)
                            text = str(params.get("text", ""))
                            if sb is not None and text:
                                pt = screenact.Point(sb["x"], sb["y"], origin="element", label=sb["label"])
                                screenact.move_cursor(pt)
                                clk = screenact.click(pt, expect_process=before.app)
                                if clk.ok:
                                    t_res = screenact.type_text(text, clear_first=True)
                                    after, verification = _verified_after(before, typed_text=text)
                                    contacts = _wa.extract_whatsapp_contacts(after, query=text)
                                    desktop_control.record_diagnostics(
                                        active_application="WhatsApp Desktop",
                                        unique_contact_count=len(contacts),
                                        last_keyboard_status=("ok" if t_res.ok else "failed"),
                                    )
                                    desktop_control.report(action, verification, after.app)
                                    _publish(player)
                                    return f"Searched WhatsApp for {text!r} ({len(contacts)} unique result(s)); {verification}."
                        elif is_composer_hint:
                            comp_box = _wa.locate_whatsapp_composer(before)
                            text = str(params.get("text", ""))
                            if not text:
                                return "Tell me what text to enter."
                            if comp_box.get("composer_found"):
                                pt = screenact.Point(comp_box["x"], comp_box["y"], origin="element", label=comp_box["label"])
                                mv = screenact.move_cursor(pt)
                                clk = screenact.click(pt, expect_process=before.app)
                                if not clk.ok:
                                    return str(clk)
                                t_res = screenact.type_text(text, clear_first=bool(params.get("clear_first", True)))
                                if not t_res.ok:
                                    return str(t_res)
                                after, verification = _verified_after(before, typed_text=text)
                                hdr = _wa.verify_selected_chat(after)
                                active_c = str(hdr.get("active_contact") or desktop_control.snapshot().get("active_contact") or "")
                                desktop_control.set_active_composition_state(
                                    composition_requested=True,
                                    selected_contact=active_c,
                                    generated_content=text,
                                    typing_authorized=True,
                                    message_sent=False,
                                )
                                desktop_control.record_diagnostics(
                                    active_application="WhatsApp Desktop",
                                    typing_status="verified",
                                    sending_status="not_sent",
                                    last_keyboard_status="ok",
                                )
                                desktop_control.report(action, verification, after.app)
                                _publish(player)
                                return f"Typed draft into WhatsApp composer ({'cursor moved, ' if mv.ok else ''}not sent); {verification}."
                        elif click_kind in {"click", "left_click", "select"} and target_text and not is_search_hint:
                            contacts = _wa.extract_whatsapp_contacts(before, query=target_text)
                            if contacts:
                                desktop_control.record_diagnostics(
                                    active_application="WhatsApp Desktop",
                                    unique_contact_count=len(contacts),
                                )
                                exact = [c for c in contacts if fold(c["display_name"]) == fold(target_text)]
                                chosen = exact[0] if len(exact) == 1 else (contacts[0] if len(contacts) == 1 else None)
                                if chosen is None:
                                    names = ", ".join(c["display_name"] for c in contacts[:4])
                                    return f"I see {len(contacts)} distinct WhatsApp contacts matching {target_text!r}: {names}. Which one should I open?"
                                pt = screenact.Point(chosen["point"][0], chosen["point"][1], origin="element", label=chosen["display_name"])
                                screenact.move_cursor(pt)
                                clk = screenact.click(pt, expect_process=before.app)
                                if not clk.ok:
                                    return str(clk)
                                after, verification = _verified_after(before)
                                hdr = _wa.verify_selected_chat(after, expected_contact=chosen["display_name"])
                                desktop_control.set_active_target(
                                    target_type="contact", exact_target_name=chosen["display_name"],
                                    browser="whatsapp", tab_title=after.title,
                                    point=chosen["point"], rect=chosen.get("rect"),
                                    confidence=chosen.get("confidence", "high"),
                                    extraction_source=chosen.get("source", "uia"),
                                )
                                desktop_control.record_diagnostics(
                                    active_application="WhatsApp Desktop",
                                    active_contact=chosen["display_name"],
                                    active_chat_id=chosen.get("id") or _wa.draft_hash(chosen["display_name"]),
                                    selected_chat_verified=bool(hdr.get("verified")),
                                    unique_contact_count=len(contacts),
                                )
                                desktop_control.report(action, verification, after.app)
                                _publish(player)
                                return f"Opened WhatsApp chat for {chosen['display_name']} ({'verified' if hdr.get('verified') else 'clicked'}); {verification}."
                    except Exception:
                        pass

                is_page_search = target_cf in {
                    "search", "search bar", "search box", "search field",
                    "youtube search", "youtube search bar", "youtube search box", "youtube search field",
                }
                is_browser_app = any(b in fold(before.app) for b in ("chrome", "msedge", "edge", "firefox", "brave", "opera", "vivaldi"))
                if is_page_search and is_browser_app and click_kind in {"hover", "move", "click", "right_click", "double_click", "type"}:
                    dom_point = _try_browser_dom_point(before, target_text)
                    if dom_point is not None:
                        if click_kind == "hover":
                            outcome = screenact.hover(dom_point)
                        elif click_kind == "move":
                            outcome = screenact.move_cursor(dom_point)
                        elif click_kind == "right_click":
                            outcome = screenact.right_click(dom_point, expect_process=before.app)
                        elif click_kind == "double_click":
                            outcome = screenact.double_click(dom_point, expect_process=before.app)
                        elif click_kind == "click":
                            outcome = screenact.click(dom_point, expect_process=before.app)
                        else:
                            text = str(params.get("text", ""))
                            if not text:
                                return "Tell me what text to enter."
                            outcome = screenact.type_text(
                                text, target=dom_point,
                                clear_first=bool(params.get("clear_first", True)),
                            )
                        desktop_control.report(action, str(outcome), before.app)
                        _publish(player)
                        return str(outcome)
                roles = ((screenview.ROLE_EDIT, screenview.ROLE_COMBOBOX)
                         if (click_kind == "type" or is_page_search) else ())
                target, why = _resolve(before, target_text, roles=roles,
                                       generic_search=(click_kind == "type" or is_page_search))
                # Never confuse Chrome's browser address bar for a YouTube/page search bar
                if target is not None and is_page_search and "address" in fold(getattr(target, "label", "") or ""):
                    target = None
                    why = f"Only the browser address bar was visible; could not locate {target_text!r}."
                if (click_kind == "type" and why and fold(target_text) in
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
                    # Level 1 -> Level 2 fallback: Browser DOM screen coordinates, then local OCR visual bounds
                    dom_point = _try_browser_dom_point(before, target_text)
                    if dom_point is None and click_kind in {"hover", "move", "click", "right_click", "double_click"}:
                        try:
                            if getattr(screenview, "_HAS_OCR", False):
                                ocr_els = screenview._scan_ocr(screenview.monitor_rect(before), time.monotonic() + 2.0)
                                fold_t = fold(target_text)
                                matches = [e for e in ocr_els if fold_t and fold_t in fold(getattr(e, "name", "") or "")]
                                if len(matches) >= 1:
                                    dom_point = screenact.Point.from_element(matches[0])
                        except Exception:
                            pass
                    if dom_point is not None and click_kind in {"hover", "move", "click", "right_click", "double_click", "type"}:
                        if click_kind == "hover":
                            outcome = screenact.hover(dom_point)
                        elif click_kind == "move":
                            outcome = screenact.move_cursor(dom_point)
                        elif click_kind == "right_click":
                            outcome = screenact.right_click(dom_point, expect_process=before.app)
                        elif click_kind == "double_click":
                            outcome = screenact.double_click(dom_point, expect_process=before.app)
                        elif click_kind == "click":
                            outcome = screenact.click(dom_point, expect_process=before.app)
                        else:
                            text = str(params.get("text", ""))
                            if not text:
                                return "Tell me what text to enter."
                            outcome = screenact.type_text(
                                text, target=dom_point,
                                clear_first=bool(params.get("clear_first", True)),
                            )
                        desktop_control.report(action, str(outcome), before.app)
                        _publish(player)
                        return str(outcome)
                    desktop_control.report(action, "not executed: target ambiguous or missing", before.app)
                    _publish(player)
                    return why
                action = click_kind
                if action in {"select", "toggle", "expand", "collapse"}:
                    patterns = {"select": ("selection_item", None),
                                "toggle": ("toggle", None),
                                "expand": ("expand_collapse", "expand"),
                                "collapse": ("expand_collapse", "collapse")}
                    pattern, value = patterns[action]
                    ok, detail = screenview.perform_uia_pattern(target, pattern, value)
                    if not ok:
                        desktop_control.report(action, f"not performed: {detail}", before.app)
                        _publish(player)
                        return f"I could not use the target's fresh UI Automation {pattern} pattern: {detail}. No coordinate action was attempted."
                    after, verification = _verified_after(before, target=target)
                    desktop_control.report(action, verification, after.app)
                    _publish(player)
                    return f"{action.title()} {target.label}; {verification}."
                if action == "type":
                    text = str(params.get("text", ""))
                    if not text:
                        return "Tell me what text to enter."
                    if target.protected:
                        return "That field is protected; enter the secret yourself."
                    if "value" in target.patterns:
                        ok, detail = screenview.perform_uia_pattern(
                            target, "value", text)
                        if not ok:
                            desktop_control.report(action, f"failed: {detail}", before.app)
                            _publish(player)
                            return (f"I could not set the target through its fresh UI Automation "
                                    f"ValuePattern: {detail}. No keyboard fallback was attempted.")
                        after, verification = _verified_after(
                            before, target=target, typed_text=text)
                        desktop_control.report(action, verification, after.app)
                        _publish(player)
                        return f"Entered text in {target.label}; {verification}."
                if action == "click":
                    semantic_pattern = next((pattern for pattern in (
                        "invoke", "toggle", "selection_item")
                        if pattern in target.patterns), None)
                    if target.role in (screenview.ROLE_EDIT, screenview.ROLE_COMBOBOX):
                        semantic_pattern = "focus"
                    if semantic_pattern:
                        semantic_value = None
                        if semantic_pattern == "expand_collapse":
                            semantic_value = "expand"
                        ok, detail = screenview.perform_uia_pattern(
                            target, semantic_pattern, semantic_value)
                        if not ok:
                            desktop_control.report(action, f"failed: {detail}", before.app)
                            _publish(player)
                            return (f"I could not activate the freshly observed control through "
                                    f"UI Automation: {detail}. No physical click was attempted.")
                        after, verification = _verified_after(before, target=target)
                        desktop_control.report(action, verification, after.app)
                        _publish(player)
                        return f"Activated {target.label} semantically; {verification}."
                if action in {"click", "right_click", "double_click"} and any(
                        word in fold(target.label) for word in _CONSEQUENTIAL_LABELS):
                    from core import confirm
                    if confirm.pending_title():
                        return "Another confirmation is already waiting; no click was made."
                    return confirm.request(
                        key="desktop-consequential-click",
                        title=f"Confirm desktop action: {target.label[:70]}?",
                        detail=f"Review the active {before.app} window before approving.",
                        run=lambda: _confirmed_click(target_text, before.app,
                                                     before.title, player, action),
                    )
                if action == "click":
                    outcome = screenact.click(target, expect_process=before.app)
                elif action == "right_click":
                    outcome = screenact.right_click(target, expect_process=before.app)
                elif action == "double_click":
                    outcome = screenact.double_click(target, expect_process=before.app)
                elif action == "hover":
                    outcome = screenact.hover(target)
                elif action == "move":
                    outcome = screenact.move_cursor(target)
                else:
                    text = str(params.get("text", ""))
                    if not text:
                        return "Tell me what text to enter."
                    if target.protected:
                        return "That field is protected; enter the secret yourself."
                    outcome = screenact.type_text(text, target=target,
                        clear_first=bool(params.get("clear_first",
                            any(term in fold(target.label) for term in
                                ("search", "query", "address", "url")))))
                if not outcome.ok:
                    desktop_control.report(action, f"failed: {outcome.error}", before.app)
                    _publish(player)
                    return str(outcome)
                if action in {"hover", "move"}:
                    desktop_control.report(action, "cursor moved to resolved control", before.app)
                    _publish(player)
                    return str(outcome)
                after, verification = _verified_after(before, target=target,
                    typed_text=(str(params.get("text", "")) if action == "type" else None))
                desktop_control.report(action, verification, after.app)
                _publish(player)
                return f"{outcome}; {verification}. Current window: {after.title or after.app}."

            if action == "press":
                key = str(params.get("key", "")).strip()
                normalized_key = _normalize_key_request(key)
                if not _key_request_allowed(key):
                    return ("That key or shortcut is not on the safe allowlist for desktop "
                            "control. Use a supported key or an explicitly supported shortcut.")
                try:
                    presses = int(params.get("presses", 1))
                except (TypeError, ValueError):
                    return "Key repeat count must be an integer from 1 to 10."
                if not 1 <= presses <= 10:
                    return "Key repeat count must be from 1 to 10."
                if normalized_key == "enter":
                    focused = next((e for e in before.elements if e.focused), None)
                    label = fold(focused.label if focused else "")
                    if any(word in label for word in ("message", "reply", "chat")):
                        return "I will not submit a message through a generic Enter key. Use the message action with its confirmation step."
                    is_search = (
                        any(word in label for word in ("search", "query", "address", "url"))
                        or (focused is not None and focused.role == screenview.ROLE_COMBOBOX)
                        or ("youtube" in fold(before.title) and not any(
                            word in label for word in ("comment", "post", "email", "pay", "checkout", "transfer", "password")
                        ))
                    )
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
                if normalized_key == "alt+f4":
                    from core import confirm
                    if confirm.pending_title():
                        return "Another confirmation is already waiting; no window was closed."
                    return confirm.request(
                        key="desktop-close-active-window",
                        title=f"Confirm closing {before.title or before.app}?",
                        detail="Alt+F4 closes the active window and may expose an unsaved-changes prompt.",
                        run=lambda: _confirmed_shortcut(normalized_key, before.app,
                                                        before.title, player),
                    )
                outcome = screenact.press(normalized_key, presses=presses)
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
        "bar' use that exact phrase); press for allowlisted keys and shortcuts "
        "such as Ctrl+A/C/V/X/Z/Y/S/F/N/T/W, Ctrl+Shift+T, Alt+Tab and Windows "
        "shortcuts; use presses for bounded repeats such as 'Tab twice'. Alt+F4 "
        "requires confirmation. Scroll for a "
        "named scroll area; focus_window for an app title. If it reports more "
        "than one match, ask which one. Password fields are never typed into. "
        "For webpage elements and Chrome DOM use browser_control, which uses "
        "Playwright semantic roles/labels. Launch apps with open_app. Never "
        "claim success when the verification says unverified. The user can "
        "always stop/pause control from the visible REM desktop panel."
    ),
    "parameters": {"type": "OBJECT", "properties": {
        "action": {"type": "STRING", "description": "observe | find | click | left_click | right_click | double_click | mouse_down | mouse_up | select | toggle | expand | collapse | hover | move | move_relative | type | press | scroll | focus_window | set_mode | pause | resume | stop"},
        "target": {"type": "STRING", "description": "Visible control name or app/window title (or 'here' for current cursor position). For search fields say search bar."},
        "text": {"type": "STRING", "description": "Text to type into the named, non-protected field."},
        "key": {"type": "STRING", "description": "Allowlisted key or shortcut (Enter, Escape, Tab, Ctrl+F, Alt+Tab, etc.)."},
        "presses": {"type": "INTEGER", "description": "Repeat a key press 1–10 times; useful for commands such as 'Tab twice'."},
        "direction": {"type": "STRING", "description": "up | down | left | right"},
        "amount": {"type": "INTEGER", "description": "Scroll notches, 1–20."},
        "x": {"type": "INTEGER", "description": "Optional virtual-screen X coordinate for move/click."},
        "y": {"type": "INTEGER", "description": "Optional virtual-screen Y coordinate for move/click."},
        "dx": {"type": "INTEGER", "description": "Bounded relative cursor offset, up to 200 pixels."},
        "dy": {"type": "INTEGER", "description": "Bounded relative cursor offset, up to 200 pixels."},
        "position": {"type": "STRING", "description": "Named position: center, top_left, top_right, bottom_left, bottom_right."},
        "clear_first": {"type": "BOOLEAN", "description": "Clear the named field before typing. Defaults true."},
        "mode": {"type": "STRING", "description": "ON-DEMAND | LIVE ASSIST | COMPUTER CONTROL."},
    }, "required": ["action"]},
    "handler": screen_agent,
}
