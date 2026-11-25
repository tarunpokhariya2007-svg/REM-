"""Exercise REM's integrated screenview/UIA action path on the disposable fixture."""
from __future__ import annotations

import datetime as dt
import json
import os
import platform
import threading
import time
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

TITLE = "REM UIA Disposable Acceptance 2026-11-24"
QUERY = "REM_INTEGRATED_UIA_TEST"
LOG = Path(__file__).with_name("rem_uia_adapter_acceptance.jsonl")


def emit(event, **fields):
    row = {"time_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
           "event": event, "pid": os.getpid(),
           "thread_id": threading.get_native_id(), **fields}
    with LOG.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")


def wait_for_element_value(screenview, hwnd, pid, title, *, name=None,
                           automation_id=None, control_type=None,
                           expected, timeout=8.0):
    """Wait for the UIA provider to publish a freshly entered disposable value."""
    deadline = time.monotonic() + timeout
    latest = []
    while time.monotonic() < deadline:
        latest = screenview._scan_windows(
            hwnd, 1.0, time.monotonic() + 5.0, pid=pid, title=title)
        values = [e.value for e in latest
                  if (name is None or e.name == name)
                  and (automation_id is None or e.automation_id == automation_id)
                  and (control_type is None or e.control_type == control_type)]
        if values == [expected]:
            return latest, values, True
        time.sleep(0.1)
    return latest, values if 'values' in locals() else [], False


def main():
    import ctypes
    import pythoncom
    import psutil
    import win32api
    import win32con
    import win32security
    import win32gui
    import win32process
    from core import screenview

    pythoncom.CoInitializeEx(pythoncom.COINIT_APARTMENTTHREADED)
    try:
        windows = []
        def enum(hwnd, _):
            if win32gui.IsWindowVisible(hwnd) and win32gui.GetWindowText(hwnd) == TITLE:
                pid = int(win32process.GetWindowThreadProcessId(hwnd)[1])
                windows.append((int(hwnd), pid))
            return True

        win32gui.EnumWindows(enum, None)
        if not windows:
            emit("blocked", reason="no visible disposable fixture window")
            return
        hwnd, pid = sorted(windows)[-1]
        ctypes.windll.user32.ShowWindow(hwnd, 9)  # SW_RESTORE for the disposable fixture
        ctypes.windll.user32.SetForegroundWindow(hwnd)
        time.sleep(0.2)
        _, title, foreground_pid, foreground_hwnd = screenview._foreground()
        try:
            executable = psutil.Process(pid).exe()
        except Exception:
            executable = "unavailable"
        token = win32security.OpenProcessToken(
            win32api.GetCurrentProcess(), win32con.TOKEN_QUERY)
        user = win32security.LookupAccountSid(
            None, win32security.GetTokenInformation(token, win32security.TokenUser)[0])
        integrity_sid = win32security.ConvertSidToStringSid(
            win32security.GetTokenInformation(token, win32security.TokenIntegrityLevel)[0])
        try:
            elevation = win32security.GetTokenInformation(token, win32security.TokenElevation)
        except Exception:
            elevation = None
        try:
            uiaccess = win32security.GetTokenInformation(token, win32security.TokenUIAccess)
        except Exception:
            uiaccess = None
        appcontainer_value = ctypes.c_int()
        returned = ctypes.c_ulong()
        appcontainer_ok = ctypes.windll.advapi32.GetTokenInformation(
            int(token), 29, ctypes.byref(appcontainer_value),
            ctypes.sizeof(appcontainer_value), ctypes.byref(returned))
        session_id = ctypes.c_ulong()
        session_ok = ctypes.windll.kernel32.ProcessIdToSessionId(
            os.getpid(), ctypes.byref(session_id))
        context = {"hwnd": hex(hwnd), "pid": pid, "title": title,
                   "foreground_hwnd": hex(int(foreground_hwnd or 0)),
                   "foreground_pid": foreground_pid,
                   "executable": executable,
                   "runner_executable": sys.executable,
                   "user": user, "integrity_sid": integrity_sid,
                   "elevation": elevation, "uiaccess": uiaccess,
                   "appcontainer": (bool(appcontainer_value.value)
                                    if appcontainer_ok else None),
                   "session_id": int(session_id.value) if session_ok else None,
                   "python": platform.python_version(),
                   "architecture": platform.architecture()[0],
                   "com_initialization": "CoInitializeEx(COINIT_APARTMENTTHREADED) succeeded",
                   "uia_initialization": "CUIAutomation8 created by adapter MTA worker after scan"}
        emit("context", **context)
        if int(foreground_hwnd or 0) != hwnd or int(foreground_pid) != pid:
            emit("blocked", reason="fixture did not become foreground", **context)
            return

        elements = screenview._scan_windows(hwnd, 1.0, time.monotonic() + 5.0,
                                            pid=pid, title=title)
        field = [e for e in elements if e.name == "Search field" and e.control_type == "Edit"]
        button = [e for e in elements if e.name == "Run test search" and e.control_type == "Button"]
        emit("discovery", count=len(elements), controls=[{
            "name": e.name, "control_type": e.control_type,
            "automation_id": e.automation_id, "runtime_id": e.runtime_id,
            "patterns": e.patterns, "value_read_only": e.value_read_only,
        } for e in elements], field_matches=len(field), button_matches=len(button))
        if len(field) != 1 or len(button) != 1:
            emit("acceptance", passed=False, reason="target was missing or ambiguous")
            return
        target = field[0]
        from core import screenact
        import win32api
        import win32gui
        element_moves = []
        for label, element in (("search field", target), ("search button", button[0])):
            move = screenact.move_cursor(element)
            expected_point = element.center
            actual_point = tuple(map(int, win32api.GetCursorPos()))
            child_hwnd = int(win32gui.WindowFromPoint(actual_point) or 0)
            root_hwnd = int(win32gui.GetAncestor(child_hwnd, 2) or child_hwnd) if child_hwnd else 0
            element_moves.append({"target": label, "move_ok": move.ok,
                                  "position_matches": actual_point == expected_point,
                                  "window_matches": root_hwnd == hwnd,
                                  "passed": move.ok and actual_point == expected_point
                                  and root_hwnd == hwnd})
        emit("physical_cursor_to_uia_targets", checks=element_moves,
             passed=all(row["passed"] for row in element_moves))
        if "value" not in target.patterns or target.value_read_only is not False:
            emit("acceptance", passed=False, reason="ValuePattern missing or read-only",
                 patterns=target.patterns, value_read_only=target.value_read_only)
            return
        ok, detail = screenview.perform_uia_pattern(target, "value", QUERY)
        emit("set_value", ok=ok, detail=detail)
        if not ok:
            emit("acceptance", passed=False, failed_step="screenview.perform_uia_pattern(value)")
            return
        refreshed = screenview._scan_windows(hwnd, 1.0, time.monotonic() + 5.0,
                                             pid=pid, title=title)
        values = [e.value for e in refreshed if e.name == "Search field"
                  and e.control_type == "Edit" and e.automation_id == target.automation_id]
        emit("readback", values=values, exact=(values == [QUERY]))
        if values != [QUERY]:
            emit("acceptance", passed=False, failed_step="ValuePattern readback")
            return
        button_target = button[0]
        if "invoke" not in button_target.patterns:
            emit("acceptance", passed=False, reason="InvokePattern unavailable")
            return
        ok, detail = screenview.perform_uia_pattern(button_target, "invoke")
        emit("invoke", ok=ok, detail=detail)
        after_invoke = screenview._scan_windows(hwnd, 1.0, time.monotonic() + 5.0,
                                                pid=pid, title=title)
        expected_status = f"Search completed: {QUERY}"
        status_values = [{"name": e.name, "value": e.value}
                         for e in after_invoke if e.automation_id == "ResultLabel"]
        status_verified = any(expected_status in (row["name"] or row["value"])
                              for row in status_values)
        list_items = [e.name for e in after_invoke
                      if e.control_type == "ListItem" and e.name]
        expected_result = f"Result for {QUERY}"
        emit("post_invoke_verification", status_values=status_values,
             status_verified=status_verified, list_items=list_items,
             list_discovered=any(e.control_type == "List" and
                                 e.automation_id == "ResultList" for e in after_invoke))
        emit("acceptance", passed=bool(ok) and status_verified
             and expected_result in list_items,
             edit="Search field", control_type=50004,
             value_pattern=True, is_read_only=False, exact_value_readback=True,
             button="Run test search", button_control_type=50000,
             invoke_pattern=True, invoked=bool(ok),
             status_expected=expected_status, status_verified=status_verified,
             list_items=list_items, list_updated=expected_result in list_items)

        combo_value = next((e for e in after_invoke
                            if e.name == "Test choices" and e.control_type == "ComboBox"
                            and "value" in e.patterns and e.value_read_only is False
                            and e.runtime_id), None)
        if combo_value:
            set_combo, combo_detail = screenview.perform_uia_pattern(
                combo_value, "value", "Choice Two")
            combo_after = screenview._scan_windows(
                hwnd, 1.0, time.monotonic() + 5.0, pid=pid, title=title)
            combo_readback = [e.value for e in combo_after
                              if e.automation_id == combo_value.automation_id
                              and e.control_type == "ComboBox"]
            combo_verified = set_combo and combo_readback == ["Choice Two"]
            restore_combo = False
            if combo_verified:
                restore_combo, _ = screenview.perform_uia_pattern(
                    combo_value, "value", "Choice One")
            emit("combo_value_pattern", tested=True, set_ok=set_combo,
                 exact_readback=combo_verified, restored=restore_combo,
                 detail=combo_detail)
        else:
            emit("combo_value_pattern", tested=False,
                 reason="no writable uniquely identified ValuePattern")

        selected_item = next((e for e in after_invoke
                              if e.name == expected_result and e.control_type == "ListItem"
                              and "selection_item" in e.patterns and e.runtime_id), None)
        if selected_item:
            list_selected, list_detail = screenview.perform_uia_pattern(
                selected_item, "selection_item")
            emit("list_item_selection", tested=True, ok=list_selected,
                 detail=list_detail)
        else:
            emit("list_item_selection", tested=False,
                 reason="list item has no unique stable SelectionItemPattern identity")

        # Real keyboard input is exercised only in the disposable fixture.
        # Focus is verified through UIA before sending any characters.
        from core import screenact
        focus_ok, focus_detail = screenview.perform_uia_pattern(target, "focus")
        keyboard_sample = (
            "REM_KEYBOARD_TEST_123 ✓ Ω — punctuation !? #$% "
            + "long keyboard payload 0123456789 " * 9
        )
        initial_select = screenact.press("ctrl+a") if focus_ok else None
        type_act = (screenact.type_text(keyboard_sample)
                    if initial_select and initial_select.ok else None)
        after_type, typed_values, typed_verified = wait_for_element_value(
            screenview, hwnd, pid, title, name="Search field", control_type="Edit",
            expected=keyboard_sample)
        emit("keyboard_unicode_type", focus_ok=focus_ok, focus_detail=focus_detail,
             select_all_ok=bool(initial_select and initial_select.ok),
             type_ok=bool(type_act and type_act.ok),
             exact_readback=typed_verified, character_count=len(keyboard_sample))

        select_all = screenact.press("ctrl+a") if typed_verified else None
        replacement = "REM_KEYBOARD_REPLACED ✓"
        replace_act = screenact.type_text(replacement) if select_all and select_all.ok else None
        after_replace, replacement_values, replacement_verified = wait_for_element_value(
            screenview, hwnd, pid, title, name="Search field", control_type="Edit",
            expected=replacement)
        emit("keyboard_select_all_replace", select_all_ok=bool(select_all and select_all.ok),
             type_ok=bool(replace_act and replace_act.ok),
             exact_readback=replacement_verified)

        end_key = screenact.press("end") if replacement_verified else None
        append_act = screenact.type_text(" Z") if end_key and end_key.ok else None
        after_append, append_values, append_verified = wait_for_element_value(
            screenview, hwnd, pid, title, name="Search field", control_type="Edit",
            expected=replacement + " Z")
        escape_key = screenact.press("esc") if append_act and append_act.ok else None
        tab_key = screenact.press("tab") if escape_key and escape_key.ok else None
        time.sleep(0.15)
        after_tab = screenview._scan_windows(hwnd, 1.0, time.monotonic() + 5.0,
                                             pid=pid, title=title)
        focused_controls = [e.name for e in after_tab if e.focused]
        focus_return_ok, focus_return_detail = screenview.perform_uia_pattern(
            target, "focus")
        save_key = screenact.press("ctrl+s") if focus_return_ok else None
        screenact.release_all()
        import ctypes
        modifiers_released = all(not (ctypes.windll.user32.GetAsyncKeyState(code) & 0x8000)
                                 for code in (0x10, 0x11, 0x12))
        emit("keyboard_keys_focus_and_cleanup", end_ok=bool(end_key and end_key.ok),
             append_ok=bool(append_act and append_act.ok),
             append_verified=append_verified,
             escape_ok=bool(escape_key and escape_key.ok),
             tab_ok=bool(tab_key and tab_key.ok), focused_controls=focused_controls,
             focus_return_ok=focus_return_ok, focus_return_detail=focus_return_detail,
             ctrl_s_ok=bool(save_key and save_key.ok),
             modifiers_released=modifiers_released)

        multiline = next((e for e in after_tab
                          if e.automation_id == "MultilineKeyboardBox"
                          and e.control_type in {"Edit", "Document"}), None)
        multiline_text = "REM_MULTILINE_LINE_1 ✓ Ω\nREM_MULTILINE_LINE_2 !?"
        multiline_focus = (screenview.perform_uia_pattern(multiline, "focus")
                           if multiline else (False, "multiline edit control not discovered"))
        multiline_select = (screenact.press("ctrl+a")
                            if multiline_focus[0] else None)
        multiline_type = (screenact.type_text(multiline_text)
                          if multiline_select and multiline_select.ok else None)
        after_multiline, multiline_values, multiline_exact = wait_for_element_value(
            screenview, hwnd, pid, title,
            automation_id="MultilineKeyboardBox", expected=multiline_text,
            timeout=3.0)
        normalized_multiline = [value.rstrip("\r").replace("\r\n", "\n").replace("\r", "\n")
                                for value in multiline_values]
        multiline_verified = normalized_multiline == [multiline_text]
        emit("keyboard_multiline_unicode", discovered=multiline is not None,
             patterns=list(multiline.patterns) if multiline else [],
             focus_ok=multiline_focus[0],
             select_all_ok=bool(multiline_select and multiline_select.ok),
             type_ok=bool(multiline_type and multiline_type.ok),
             exact_readback=multiline_verified,
             provider_exact_readback=multiline_exact,
             line_count=multiline_text.count("\n") + 1)

        # Exercise other advertised patterns only on this disposable fixture.
        by_name = {}
        for element in refreshed:
            by_name.setdefault(element.name, []).append(element)
        checkbox = next((e for e in by_name.get("Enable test option", [])
                         if "toggle" in e.patterns and e.runtime_id), None)
        combo = next((e for e in by_name.get("Test choices", [])
                      if e.control_type == "ComboBox" and "expand_collapse" in e.patterns
                      and e.runtime_id), None)
        alpha = next((e for e in by_name.get("Alpha result", [])
                      if e.control_type == "ListItem" and "selection_item" in e.patterns
                      and e.runtime_id), None)
        for label, element, pattern, value in (
                ("checkbox", checkbox, "toggle", None),
                ("combo_expand", combo, "expand_collapse", "expand"),
                ("combo_collapse", combo, "expand_collapse", "collapse"),
                ("listitem_select", alpha, "selection_item", None)):
            if element is None:
                emit("control_pattern", target=label, tested=False,
                     reason="no uniquely identified element with advertised pattern")
                continue
            action_ok, action_detail = screenview.perform_uia_pattern(
                element, pattern, value)
            emit("control_pattern", target=label, tested=True,
                 runtime_id=element.runtime_id, pattern=pattern,
                 value=value, ok=action_ok, detail=action_detail)
            if label == "checkbox" and action_ok:
                restore_ok, restore_detail = screenview.perform_uia_pattern(
                    element, pattern, None)
                emit("control_pattern_restore", target=label, ok=restore_ok,
                     detail=restore_detail)
    except Exception as exc:
        emit("exception", type=type(exc).__name__, message=str(exc)[:400],
             hresult=(f"0x{exc.hresult & 0xffffffff:08X}"
                      if isinstance(getattr(exc, "hresult", None), int) else None))
    finally:
        pythoncom.CoUninitialize()


if __name__ == "__main__":
    main()
