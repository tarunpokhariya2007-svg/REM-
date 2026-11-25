"""Read-only CUIAutomation8/Axe.Windows route probe for the disposable fixture.

Run with the REM virtual environment's Python on Windows. No screen values are
read except the fixture's disposable search field after a ValuePattern is
confirmed. This script never sets a value, focuses a control, or invokes a
button; it is a discovery diagnostic, not the semantic acceptance runner.
"""
from __future__ import annotations

import ctypes
import datetime as dt
import json
import os
import platform
import sys
import threading
import traceback

TITLE = "REM UIA Disposable Acceptance 2026-11-24"
CLSID_CUIAUTOMATION8 = "{E22AD333-B25F-460C-83D0-0581107395C9}"
UIA_VALUE_PATTERN_ID = 10002
UIA_INVOKE_PATTERN_ID = 10000
UIA_LEGACY_PATTERN_ID = 10018


def emit(event: str, **fields: object) -> None:
    row = {
        "time_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "event": event,
        "pid": os.getpid(),
        "thread_id": threading.get_native_id(),
        **fields,
    }
    print(json.dumps(row, ensure_ascii=False, default=str), flush=True)


def hresult(exc: BaseException) -> str | None:
    value = getattr(exc, "hresult", None)
    return f"0x{value & 0xFFFFFFFF:08X}" if isinstance(value, int) else None


def attempt(name: str, func, *, hwnd: int | None = None,
            target_pid: int | None = None):
    try:
        value = func()
        emit("uia_call", operation=name, ok=True, hwnd=hwnd,
             target_pid=target_pid)
        return value
    except Exception as exc:  # diagnostic: preserve the real exception
        emit("uia_call", operation=name, ok=False, hwnd=hwnd,
             target_pid=target_pid, hresult=hresult(exc),
             exception=type(exc).__name__, message=str(exc)[:300])
        return None


def run() -> int:
    import ctypes
    import win32api
    import win32gui
    import win32process
    import pythoncom
    import win32con
    import win32security
    # comtypes initializes the importing thread as STA. Import its generated
    # interfaces here, before creating the dedicated MTA UIA worker, so its
    # module initialization cannot attempt to change that worker's apartment.
    import comtypes.client
    from comtypes.gen import UIAutomationClient as uia

    hwnds: list[int] = []

    def enum(hwnd: int, _unused) -> bool:
        if (win32gui.IsWindowVisible(hwnd)
                and win32gui.GetWindowText(hwnd) == TITLE):
            hwnds.append(hwnd)
        return True

    win32gui.EnumWindows(enum, None)
    candidates = []
    for hwnd in hwnds:
        pid = win32process.GetWindowThreadProcessId(hwnd)[1]
        rect = win32gui.GetWindowRect(hwnd)
        candidates.append({
            "hwnd": f"0x{hwnd:X}", "pid": pid, "title": TITLE,
            "visible": bool(win32gui.IsWindowVisible(hwnd)),
            "minimized": bool(win32gui.IsIconic(hwnd)), "rect": rect,
        })
    emit("environment", python=sys.version, architecture=platform.architecture()[0],
         os=platform.platform(), apartment="pending", candidates=candidates)

    def security_context(pid: int, current: bool = False) -> dict[str, object]:
        result: dict[str, object] = {"pid": pid}
        try:
            process = (win32api.GetCurrentProcess() if current else
                       win32api.OpenProcess(
                           win32con.PROCESS_QUERY_LIMITED_INFORMATION, False, pid))
            token = win32security.OpenProcessToken(process, win32con.TOKEN_QUERY)
            result["user"] = win32security.LookupAccountSid(
                None, win32security.GetTokenInformation(
                    token, win32security.TokenUser)[0])
            result["integrity_sid"] = win32security.ConvertSidToStringSid(
                win32security.GetTokenInformation(
                    token, win32security.TokenIntegrityLevel)[0])
            result["elevation"] = win32security.GetTokenInformation(
                token, win32security.TokenElevation)
            result["uiaccess"] = win32security.GetTokenInformation(
                token, win32security.TokenUIAccess)
            appcontainer = ctypes.c_int()
            returned = ctypes.c_ulong()
            ok = ctypes.windll.advapi32.GetTokenInformation(
                int(token), 29, ctypes.byref(appcontainer),
                ctypes.sizeof(appcontainer), ctypes.byref(returned))
            result["appcontainer"] = bool(appcontainer.value) if ok else None
        except Exception as exc:
            result["query_error"] = type(exc).__name__
            result["winerror"] = getattr(exc, "winerror", None)
            result["message"] = str(exc)[:200]
        session = ctypes.c_ulong()
        ok = ctypes.windll.kernel32.ProcessIdToSessionId(
            pid, ctypes.byref(session))
        result["session_id"] = session.value if ok else None
        result["session_query_error"] = (
            None if ok else ctypes.get_last_error())
        return result

    security_rows = [security_context(os.getpid(), current=True)]
    security_rows.extend(security_context(int(row["pid"])) for row in candidates)
    emit("process_security", rows=security_rows)
    visible = [row for row in candidates if not row["minimized"]]
    if not visible and os.environ.get("REM_UIA_ACCEPTANCE") != "1":
        emit("stop", reason="no visible, non-minimized disposable fixture window")
        return 2
    # EnumWindows reports top-to-bottom; when all copies are minimized, the
    # newest fixture is normally first. Acceptance mode may restore only this
    # named disposable window through its UIA WindowPattern.
    selected = visible[-1] if visible else candidates[0]
    hwnd = int(selected["hwnd"], 16)
    target_pid = int(selected["pid"])

    # UIA clients are isolated on a fresh MTA worker, matching Microsoft's
    # recommended client-side COM usage. GetRootElement is serialized, as in
    # Axe.Windows, to make its synchronization workaround testable.
    def com_worker() -> None:
        try:
            pythoncom.CoInitializeEx(pythoncom.COINIT_MULTITHREADED)
        except Exception as exc:
            emit("com_initialize", ok=False, hresult=hresult(exc),
                 exception=type(exc).__name__, message=str(exc)[:300])
            return
        apartment = ctypes.c_int()
        qualifier = ctypes.c_int()
        hr = ctypes.windll.ole32.CoGetApartmentType(
            ctypes.byref(apartment), ctypes.byref(qualifier))
        emit("com_initialize", ok=(hr == 0), hresult=f"0x{hr & 0xFFFFFFFF:08X}",
             apartment_type=apartment.value, apartment_qualifier=qualifier.value,
             target_hwnd=f"0x{hwnd:X}", target_pid=target_pid)
        try:
            automation = attempt(
                "CoCreateInstance(CUIAutomation8)",
                lambda: comtypes.client.CreateObject(
                    CLSID_CUIAUTOMATION8, interface=uia.IUIAutomation),
                hwnd=hwnd, target_pid=target_pid)
            if automation is None:
                return

            if selected["minimized"] and os.environ.get("REM_UIA_ACCEPTANCE") == "1":
                restore_root = attempt(
                    "IUIAutomation.ElementFromHandle(fixture HWND for restore)",
                    lambda: automation.ElementFromHandle(hwnd), hwnd=hwnd,
                    target_pid=target_pid)
                window_raw = (attempt(
                    "Window.GetCurrentPattern(WindowPattern)",
                    lambda: restore_root.GetCurrentPattern(10009), hwnd=hwnd,
                    target_pid=target_pid) if restore_root is not None else None)
                if window_raw is not None and bool(window_raw):
                    window_pattern = attempt(
                        "QueryInterface(WindowPattern)",
                        lambda: window_raw.QueryInterface(
                            uia.IUIAutomationWindowPattern), hwnd=hwnd,
                        target_pid=target_pid)
                    if window_pattern is not None:
                        attempt("WindowPattern.SetWindowVisualState(Normal)",
                                lambda: (window_pattern.SetWindowVisualState(0), True)[1],
                                hwnd=hwnd, target_pid=target_pid)
                        attempt("WindowPattern.WaitForInputIdle(5000)",
                                lambda: window_pattern.WaitForInputIdle(5000),
                                hwnd=hwnd, target_pid=target_pid)
                        import win32gui
                        selected["rect"] = win32gui.GetWindowRect(hwnd)
                        selected["minimized"] = bool(win32gui.IsIconic(hwnd))
                        emit("fixture_restore", minimized=selected["minimized"],
                             rect=selected["rect"], hwnd=f"0x{hwnd:X}",
                             target_pid=target_pid)
                if selected["minimized"]:
                    emit("acceptance", passed=False, step="restore fixture",
                         reason="UIA WindowPattern did not restore the fixture",
                         hwnd=f"0x{hwnd:X}", target_pid=target_pid)
                    return

            root_lock = threading.Lock()
            with root_lock:
                desktop = attempt(
                    "IUIAutomation.GetRootElement [Axe.Windows lock]",
                    automation.GetRootElement, hwnd=hwnd, target_pid=target_pid)
            walker = attempt(
                "IUIAutomation.get_ControlViewWalker",
                lambda: automation.ControlViewWalker,
                hwnd=hwnd, target_pid=target_pid)
            if desktop is None or walker is None:
                return

            def children(parent, path: str):
                first = attempt(path + ".GetFirstChildElement",
                                 lambda: walker.GetFirstChildElement(parent),
                                 hwnd=hwnd, target_pid=target_pid)
                result = []
                if first is None or not first:
                    emit("walker_children", path=path, count=0,
                         hwnd=f"0x{hwnd:X}", target_pid=target_pid)
                    return result
                seen = set()
                current = first
                while current is not None and bool(current) and len(result) < 1000:
                    try:
                        rid = tuple(current.GetRuntimeId())
                    except Exception:
                        rid = (id(current),)
                    if rid in seen:
                        emit("walker_cycle", path=path, runtime_id=rid,
                             hwnd=f"0x{hwnd:X}", target_pid=target_pid)
                        break
                    seen.add(rid)
                    result.append(current)
                    following = attempt(path + ".GetNextSiblingElement",
                                        lambda current=current:
                                        walker.GetNextSiblingElement(current),
                                        hwnd=hwnd, target_pid=target_pid)
                    if following is None or not following:
                        break
                    current = following
                emit("walker_children", path=path, count=len(result),
                     hwnd=f"0x{hwnd:X}", target_pid=target_pid)
                return result

            root_children = children(desktop, "ControlViewWalker.desktop")
            process_matches = [e for e in root_children
                               if attempt("element.CurrentProcessId",
                                          lambda e=e: e.CurrentProcessId,
                                          hwnd=hwnd, target_pid=target_pid)
                               == target_pid]
            if not process_matches:
                for parent in root_children:
                    descendants = children(parent, "ControlViewWalker.desktop_child")
                    process_matches.extend(
                        e for e in descendants
                        if attempt("element.CurrentProcessId",
                                   lambda e=e: e.CurrentProcessId,
                                   hwnd=hwnd, target_pid=target_pid) == target_pid)
            emit("axe_process_matches", count=len(process_matches),
                 hwnd=f"0x{hwnd:X}", target_pid=target_pid)

            direct = attempt("IUIAutomation.ElementFromHandle(fixture HWND)",
                             lambda: automation.ElementFromHandle(hwnd),
                             hwnd=hwnd, target_pid=target_pid)
            if direct is not None:
                emit_element(direct, "ElementFromHandle", hwnd, target_pid,
                             uia, attempt)
                children(direct, "ControlViewWalker.fixture_root")
                named_elements = {}
                for label, control_type in (("Search field", 50004),
                                            ("Run test search", 50000)):
                    cond = attempt(
                        f"CreateAndCondition(Name={label}, ControlType={control_type})",
                        lambda label=label, control_type=control_type:
                        automation.CreateAndCondition(
                            automation.CreatePropertyCondition(
                                uia.UIA_NamePropertyId, label),
                            automation.CreatePropertyCondition(
                                uia.UIA_ControlTypePropertyId, control_type)),
                        hwnd=hwnd, target_pid=target_pid)
                    if cond is not None:
                        found = attempt(
                            f"fixture_root.FindFirst(Descendants, {label})",
                            lambda cond=cond: direct.FindFirst(
                                uia.TreeScope_Descendants, cond),
                            hwnd=hwnd, target_pid=target_pid)
                        if found is not None and bool(found):
                            named_elements[label] = found
                            emit_element(found, f"FindFirst.{label}", hwnd,
                                         target_pid, uia, attempt)
                for scope_name, scope in (("Children", uia.TreeScope_Children),
                                          ("Descendants", uia.TreeScope_Descendants)):
                    arr = attempt(
                        f"fixture_root.FindAll({scope_name}, TrueCondition)",
                        lambda scope=scope: direct.FindAll(
                            scope, automation.CreateTrueCondition()),
                        hwnd=hwnd, target_pid=target_pid)
                    if arr is not None:
                        length = attempt("IUIAutomationElementArray.Length",
                                         lambda: arr.Length, hwnd=hwnd,
                                         target_pid=target_pid)
                        emit("findall_count", scope=scope_name, count=length,
                             hwnd=f"0x{hwnd:X}", target_pid=target_pid)
                        for index in range(min(int(length or 0), 1000)):
                            e = attempt("IUIAutomationElementArray.GetElement",
                                        lambda index=index: arr.GetElement(index),
                                        hwnd=hwnd, target_pid=target_pid)
                            if e is not None:
                                emit_element(e, f"FindAll.{scope_name}[{index}]",
                                             hwnd, target_pid, uia, attempt)

                if os.environ.get("REM_UIA_ACCEPTANCE") == "1":
                    run_semantic_acceptance(
                        direct, automation, uia, named_elements, hwnd,
                        target_pid, attempt)

            # Read-only route comparison only. The test fixture's controls are
            # laid out in its own source; these points are never passed to an
            # input API and are not used for REM targeting.
            left, top, right, bottom = selected["rect"]
            from ctypes import wintypes
            for label, point in (
                ("SearchBox", (left + 204, top + 66)),
                ("SearchButton", (left + 475, top + 66)),
            ):
                found = attempt(
                    f"IUIAutomation.ElementFromPoint({label})",
                    lambda point=point: automation.ElementFromPoint(
                        uia.tagPOINT(point[0], point[1])),
                    hwnd=hwnd, target_pid=target_pid)
                if found is not None and bool(found):
                    emit_element(found, f"FromPoint.{label}", hwnd,
                                 target_pid, uia, attempt)

            for root in process_matches:
                emit_element(root, "Axe.Windows PID match", hwnd, target_pid,
                             uia, attempt)
                for index, element in enumerate(children(
                        root, "ControlViewWalker.matched_process_root")):
                    emit_element(element, f"Axe.Windows match child {index}",
                                 hwnd, target_pid, uia, attempt)
        except Exception as exc:
            emit("probe_fatal", exception=type(exc).__name__,
                 hresult=hresult(exc), message=str(exc)[:300],
                 target_hwnd=f"0x{hwnd:X}", target_pid=target_pid)
            traceback.print_exc()
        finally:
            try:
                pythoncom.CoUninitialize()
            except Exception:
                pass

    thread = threading.Thread(target=com_worker, name="REM-UIA-MTA", daemon=False)
    thread.start()
    thread.join(timeout=20)
    if thread.is_alive():
        emit("stop", reason="UIA worker exceeded 20 seconds",
             target_hwnd=f"0x{hwnd:X}", target_pid=target_pid)
        return 3
    return 0


def emit_element(element, route: str, hwnd: int, target_pid: int, uia, attempt) -> None:
    names = ("CurrentControlType", "CurrentName", "CurrentAutomationId",
             "CurrentNativeWindowHandle", "CurrentProcessId", "CurrentFrameworkId",
             "CurrentBoundingRectangle", "CurrentIsOffscreen")
    identity = {name: attempt(f"element.{name}",
                              lambda name=name: getattr(element, name),
                              hwnd=hwnd, target_pid=target_pid)
                for name in names}
    rid = attempt("element.GetRuntimeId", element.GetRuntimeId,
                  hwnd=hwnd, target_pid=target_pid)
    emit("element", route=route, runtime_id=rid, properties=identity,
         hwnd=f"0x{hwnd:X}", target_pid=target_pid)
    for label, pattern_id, iface in (
        ("ValuePattern", UIA_VALUE_PATTERN_ID, uia.IUIAutomationValuePattern),
        ("InvokePattern", UIA_INVOKE_PATTERN_ID, uia.IUIAutomationInvokePattern),
        ("LegacyIAccessiblePattern", UIA_LEGACY_PATTERN_ID,
         uia.IUIAutomationLegacyIAccessiblePattern),
    ):
        raw = attempt(f"element.GetCurrentPattern({label})",
                      lambda pattern_id=pattern_id:
                      element.GetCurrentPattern(pattern_id),
                      hwnd=hwnd, target_pid=target_pid)
        if raw is not None and bool(raw):
            pattern = attempt(f"QueryInterface({label})",
                              lambda raw=raw, iface=iface: raw.QueryInterface(iface),
                              hwnd=hwnd, target_pid=target_pid)
            if pattern is not None and label == "ValuePattern":
                readonly = attempt("ValuePattern.CurrentIsReadOnly",
                                   lambda: pattern.CurrentIsReadOnly,
                                   hwnd=hwnd, target_pid=target_pid)
                if not readonly:
                    # This is a disposable fixture control, never a user field.
                    value = attempt("ValuePattern.CurrentValue",
                                    lambda: pattern.CurrentValue,
                                    hwnd=hwnd, target_pid=target_pid)
                    emit("fixture_value", value=value, readonly=readonly,
                         hwnd=f"0x{hwnd:X}", target_pid=target_pid)
        else:
            emit("pattern_unavailable", pattern=label,
                 hwnd=f"0x{hwnd:X}", target_pid=target_pid)


def run_semantic_acceptance(root, automation, uia, named, hwnd, target_pid,
                            attempt) -> None:
    """Exercise only the named disposable fixture through UIA patterns."""
    test_value = "REM_UIA_TEST_123"
    edit = named.get("Search field")
    button = named.get("Run test search")
    if edit is None or button is None:
        emit("acceptance", passed=False, step="find controls",
             reason="named Edit or Button was not found")
        return
    edit_type = attempt("acceptance.Edit.CurrentControlType",
                        lambda: edit.CurrentControlType, hwnd=hwnd,
                        target_pid=target_pid)
    edit_name = attempt("acceptance.Edit.CurrentName",
                        lambda: edit.CurrentName, hwnd=hwnd,
                        target_pid=target_pid)
    raw_value = attempt("acceptance.Edit.GetCurrentPattern(ValuePattern)",
                        lambda: edit.GetCurrentPattern(UIA_VALUE_PATTERN_ID),
                        hwnd=hwnd, target_pid=target_pid)
    value_pattern = (attempt(
        "acceptance.Edit.QueryInterface(ValuePattern)",
        lambda: raw_value.QueryInterface(uia.IUIAutomationValuePattern),
        hwnd=hwnd, target_pid=target_pid)
        if raw_value is not None and bool(raw_value) else None)
    readonly = (attempt("acceptance.ValuePattern.CurrentIsReadOnly",
                        lambda: value_pattern.CurrentIsReadOnly, hwnd=hwnd,
                        target_pid=target_pid)
                if value_pattern is not None else None)
    if edit_type != 50004 or edit_name != "Search field" or not value_pattern or readonly:
        emit("acceptance", passed=False, step="verify writable Edit pattern",
             control_type=edit_type, name=edit_name, readonly=readonly,
             hwnd=f"0x{hwnd:X}", target_pid=target_pid)
        return

    before_names, before_count = _list_state(root, automation, uia, attempt,
                                             hwnd, target_pid)
    set_result = attempt("acceptance.ValuePattern.SetValue",
                         lambda: (value_pattern.SetValue(test_value), True)[1], hwnd=hwnd,
                         target_pid=target_pid)
    readback = attempt("acceptance.ValuePattern.CurrentValue read-back",
                       lambda: value_pattern.CurrentValue, hwnd=hwnd,
                       target_pid=target_pid)
    emit("acceptance_step", step="set and read back value",
         ok=(readback == test_value), readback=readback,
         hwnd=f"0x{hwnd:X}", target_pid=target_pid)
    if readback != test_value:
        emit("acceptance", passed=False, step="value read-back",
             set_call_completed=(set_result is not None), readback=readback,
             hwnd=f"0x{hwnd:X}", target_pid=target_pid)
        return

    button_type = attempt("acceptance.Button.CurrentControlType",
                          lambda: button.CurrentControlType, hwnd=hwnd,
                          target_pid=target_pid)
    button_name = attempt("acceptance.Button.CurrentName",
                          lambda: button.CurrentName, hwnd=hwnd,
                          target_pid=target_pid)
    raw_invoke = attempt("acceptance.Button.GetCurrentPattern(InvokePattern)",
                         lambda: button.GetCurrentPattern(UIA_INVOKE_PATTERN_ID),
                         hwnd=hwnd, target_pid=target_pid)
    invoke_pattern = (attempt(
        "acceptance.Button.QueryInterface(InvokePattern)",
        lambda: raw_invoke.QueryInterface(uia.IUIAutomationInvokePattern),
        hwnd=hwnd, target_pid=target_pid)
        if raw_invoke is not None and bool(raw_invoke) else None)
    if (button_type != 50000 or button_name != "Run test search"
            or invoke_pattern is None):
        emit("acceptance", passed=False, step="verify Button InvokePattern",
             control_type=button_type, name=button_name,
             invoke_available=invoke_pattern is not None,
             hwnd=f"0x{hwnd:X}", target_pid=target_pid)
        return

    status = _find_named(root, automation, uia, "Search result status", 50020,
                         attempt, hwnd, target_pid)
    status_before = _read_text(status, uia, attempt, hwnd, target_pid)
    invoke_result = attempt("acceptance.InvokePattern.Invoke",
                            lambda: (invoke_pattern.Invoke(), True)[1], hwnd=hwnd,
                            target_pid=target_pid)
    status_after = _read_text(status, uia, attempt, hwnd, target_pid)
    after_names, after_count = _list_state(root, automation, uia, attempt,
                                           hwnd, target_pid)
    status_changed = bool(status_after and status_after != status_before
                          and test_value in status_after)
    list_updated = (after_names != before_names or after_count != before_count)
    emit("acceptance_step", step="invoke Button and verify status/list",
         invoke_call_completed=(invoke_result is not None),
         status_before=status_before, status_after=status_after,
         status_changed=status_changed, list_before=before_names,
         list_after=after_names, list_updated=list_updated,
         hwnd=f"0x{hwnd:X}", target_pid=target_pid)

    # Leave the disposable fixture's input empty after the test. The status and
    # list outcomes above remain recorded before cleanup.
    cleanup = attempt("acceptance.cleanup.ValuePattern.SetValue(empty)",
                      lambda: (value_pattern.SetValue(""), True)[1], hwnd=hwnd,
                      target_pid=target_pid)
    cleanup_readback = attempt("acceptance.cleanup.ValuePattern.CurrentValue",
                               lambda: value_pattern.CurrentValue, hwnd=hwnd,
                               target_pid=target_pid)
    emit("acceptance", passed=(status_changed and list_updated),
         edit_set_readback=True, button_invoke=(invoke_result is not None),
         status_changed=status_changed, list_updated=list_updated,
         cleanup=(cleanup is not None and cleanup_readback == ""),
         hwnd=f"0x{hwnd:X}", target_pid=target_pid)


def _find_named(root, automation, uia, name, control_type, attempt, hwnd,
                target_pid):
    condition = automation.CreateAndCondition(
        automation.CreatePropertyCondition(uia.UIA_NamePropertyId, name),
        automation.CreatePropertyCondition(uia.UIA_ControlTypePropertyId,
                                           control_type))
    found = attempt(f"acceptance.FindFirst({name})",
                    lambda: root.FindFirst(uia.TreeScope_Descendants,
                                           condition),
                    hwnd=hwnd, target_pid=target_pid)
    return found if found is not None and bool(found) else None


def _read_text(element, uia, attempt, hwnd, target_pid):
    if element is None:
        return ""
    for label, pattern_id, interface in (
        ("LegacyIAccessible", UIA_LEGACY_PATTERN_ID,
         uia.IUIAutomationLegacyIAccessiblePattern),
        ("Text", 10014, uia.IUIAutomationTextPattern),
    ):
        raw = attempt(f"acceptance.status.GetCurrentPattern({label})",
                      lambda pattern_id=pattern_id:
                      element.GetCurrentPattern(pattern_id),
                      hwnd=hwnd, target_pid=target_pid)
        if raw is None or not bool(raw):
            continue
        pattern = attempt(f"acceptance.status.QueryInterface({label})",
                          lambda raw=raw, interface=interface:
                          raw.QueryInterface(interface),
                          hwnd=hwnd, target_pid=target_pid)
        if pattern is None:
            continue
        if label == "LegacyIAccessible":
            value = attempt("acceptance.status.LegacyValue",
                            lambda: pattern.CurrentValue,
                            hwnd=hwnd, target_pid=target_pid)
            if value:
                return str(value)[:300]
            name = attempt("acceptance.status.LegacyName",
                           lambda: pattern.CurrentName,
                           hwnd=hwnd, target_pid=target_pid)
            if name:
                return str(name)[:300]
        else:
            text_range = attempt("acceptance.status.TextPattern.DocumentRange",
                                 lambda: pattern.DocumentRange,
                                 hwnd=hwnd, target_pid=target_pid)
            if text_range is not None:
                value = attempt("acceptance.status.TextRange.GetText",
                                lambda: text_range.GetText(300),
                                hwnd=hwnd, target_pid=target_pid)
                if value:
                    return str(value)[:300]
    return str(attempt("acceptance.status.CurrentName",
                       lambda: element.CurrentName, hwnd=hwnd,
                       target_pid=target_pid) or "")[:300]


def _list_state(root, automation, uia, attempt, hwnd, target_pid):
    listing = _find_named(root, automation, uia, "Test results", 50008,
                          attempt, hwnd, target_pid)
    if listing is None:
        return [], 0
    children = attempt("acceptance.list.FindAll(Children)",
                       lambda: listing.FindAll(uia.TreeScope_Children,
                                               automation.CreateTrueCondition()),
                       hwnd=hwnd, target_pid=target_pid)
    if children is None:
        return [], 0
    count = int(attempt("acceptance.list.Length", lambda: children.Length,
                        hwnd=hwnd, target_pid=target_pid) or 0)
    names = []
    for index in range(min(count, 100)):
        child = attempt("acceptance.list.GetElement",
                        lambda index=index: children.GetElement(index),
                        hwnd=hwnd, target_pid=target_pid)
        if child is not None and bool(child):
            value = attempt("acceptance.listitem.CurrentName",
                            lambda child=child: child.CurrentName,
                            hwnd=hwnd, target_pid=target_pid)
            names.append(str(value or "")[:100])
    return names, count


if __name__ == "__main__":
    raise SystemExit(run())
