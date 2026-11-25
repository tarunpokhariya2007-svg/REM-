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

TITLE = "REM UIA Disposable Acceptance 2026-11-24"
QUERY = "REM_INTEGRATED_UIA_TEST"
LOG = Path(__file__).with_name("rem_uia_adapter_acceptance.jsonl")


def emit(event, **fields):
    row = {"time_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
           "event": event, "pid": os.getpid(),
           "thread_id": threading.get_native_id(), **fields}
    with LOG.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")


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
        emit("acceptance", passed=bool(ok), edit="Search field", control_type=50004,
             value_pattern=True, is_read_only=False, exact_value_readback=True,
             button="Run test search", button_control_type=50000,
             invoke_pattern=True, invoked=bool(ok),
             status_expected=f"Search completed: {QUERY}",
             status_verification="visually inspect fixture after run")

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
