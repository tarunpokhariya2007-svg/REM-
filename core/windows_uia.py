"""Small CUIAutomation8 adapter used by the existing screenview backend.

Every COM object is created, used, and released on one dedicated MTA thread.
Only plain Python values cross the thread boundary. The adapter is local-only;
it does not capture pixels or start another desktop-control architecture.
"""
from __future__ import annotations

import threading
import time
from typing import Any, Callable

_CLSID_CUIAUTOMATION8 = "{E22AD333-B25F-460C-83D0-0581107395C9}"
_ROOT_LOCK = threading.Lock()
_MAX_ELEMENTS = 900
_MAX_DEPTH = 28
_VALUE_LIMIT = 400

_CONTROL_TYPES = {
    50000: "Button", 50001: "Calendar", 50002: "CheckBox",
    50003: "ComboBox", 50004: "Edit", 50005: "Hyperlink",
    50006: "Image", 50007: "ListItem", 50008: "List",
    50009: "Menu", 50010: "MenuBar", 50011: "MenuItem",
    50012: "ProgressBar", 50013: "RadioButton", 50014: "ScrollBar",
    50015: "Slider", 50016: "Spinner", 50017: "StatusBar",
    50018: "Tab", 50019: "TabItem", 50020: "Text",
    50021: "ToolBar", 50022: "ToolTip", 50023: "Tree",
    50024: "TreeItem", 50025: "Custom", 50026: "Group",
    50027: "Thumb", 50028: "DataGrid", 50029: "DataItem",
    50030: "Document", 50031: "SplitButton", 50032: "Window",
    50033: "Pane", 50034: "Header", 50035: "HeaderItem",
    50036: "Table", 50037: "TitleBar", 50038: "Separator",
    50039: "SemanticZoom", 50040: "AppBar",
}

_PATTERNS_BY_ROLE = {
    "button": ("invoke",), "menuitem": ("invoke",),
    "checkbox": ("toggle",), "radio": ("selection_item",),
    "tab": ("selection_item",), "listitem": ("selection_item",),
    "tree": ("selection", "scroll"), "treeitem": ("selection_item",),
    "row": ("selection_item",), "cell": ("selection_item",),
    "combobox": ("value", "expand_collapse"), "edit": ("value",),
    "slider": ("range_value",), "list": ("selection", "scroll"),
    "table": ("selection", "scroll"), "group": ("scroll",),
    "text": ("text",), "document": ("text",),
    "window": ("window",),
}
_PATTERN_IDS = {
    "invoke": 10000, "selection": 10001, "value": 10002,
    "range_value": 10003, "scroll": 10004,
    "expand_collapse": 10005, "selection_item": 10010,
    "toggle": 10015,
    "text": 10014,
    "window": 10009,
}
_PATTERN_INTERFACES = {
    "invoke": "IUIAutomationInvokePattern",
    "selection": "IUIAutomationSelectionPattern",
    "value": "IUIAutomationValuePattern",
    "range_value": "IUIAutomationRangeValuePattern",
    "scroll": "IUIAutomationScrollPattern",
    "expand_collapse": "IUIAutomationExpandCollapsePattern",
    "selection_item": "IUIAutomationSelectionItemPattern",
    "toggle": "IUIAutomationTogglePattern",
    "text": "IUIAutomationTextPattern",
    "window": "IUIAutomationWindowPattern",
}


def _on_mta(work: Callable[[Any, Any], Any], timeout: float = 8.0) -> Any:
    """Run one complete UIA transaction on a fresh, explicitly MTA thread."""
    # Importing comtypes initializes the importing thread as STA. Import it
    # before the worker, then initialize the worker as MTA before any COM call.
    import comtypes.client
    from comtypes.gen import UIAutomationClient as uia

    result: list[Any] = []
    failure: list[BaseException] = []

    def invoke() -> None:
        import pythoncom
        initialized = False
        automation = None
        try:
            pythoncom.CoInitializeEx(pythoncom.COINIT_MULTITHREADED)
            initialized = True
            automation = comtypes.client.CreateObject(
                _CLSID_CUIAUTOMATION8, interface=uia.IUIAutomation)
            result.append(work(automation, uia))
        except BaseException as exc:
            failure.append(exc)
        finally:
            # All element and pattern pointers are locals of work() and are
            # released before this worker uninitializes COM.
            automation = None
            if initialized:
                pythoncom.CoUninitialize()

    thread = threading.Thread(target=invoke, name="REM-CUIAutomation8-MTA",
                              daemon=True)
    thread.start()
    thread.join(max(0.1, timeout))
    if thread.is_alive():
        raise TimeoutError("CUIAutomation8 worker exceeded its time budget")
    if failure:
        raise failure[0]
    return result[0] if result else None


def _siblings(walker: Any, parent: Any, deadline: float) -> list[Any]:
    first = walker.GetFirstChildElement(parent)
    if first is None or not bool(first):
        return []
    values = []
    seen: set[tuple[int, ...]] = set()
    current = first
    while current is not None and bool(current) and len(values) < _MAX_ELEMENTS:
        if time.monotonic() >= deadline:
            raise TimeoutError("CUIAutomation8 tree walk exceeded its time budget")
        try:
            rid = tuple(int(x) for x in current.GetRuntimeId())
        except Exception:
            rid = ()
        if rid and rid in seen:
            break
        if rid:
            seen.add(rid)
        values.append(current)
        following = walker.GetNextSiblingElement(current)
        if following is None or not bool(following):
            break
        current = following
    return values


def _process_roots(automation: Any, uia: Any, hwnd: int, pid: int,
                   deadline: float) -> tuple[Any, Any]:
    direct = automation.ElementFromHandle(int(hwnd))
    if direct is None or not bool(direct):
        raise RuntimeError("CUIAutomation8 returned no element for the target HWND")
    direct_pid = int(direct.CurrentProcessId)
    direct_hwnd = int(direct.CurrentNativeWindowHandle or 0)
    if direct_pid != int(pid) or direct_hwnd != int(hwnd):
        raise PermissionError("CUIAutomation8 target identity changed during scan")

    with _ROOT_LOCK:
        desktop = automation.GetRootElement()
    walker = automation.ControlViewWalker
    first_level = _siblings(walker, desktop, deadline)
    matches = [e for e in first_level
               if int(e.CurrentProcessId) == int(pid)
               and int(e.CurrentNativeWindowHandle or 0) == int(hwnd)]
    if not matches:
        for parent in first_level:
            second_level = _siblings(walker, parent, deadline)
            matches.extend(e for e in second_level
                           if int(e.CurrentProcessId) == int(pid)
                           and int(e.CurrentNativeWindowHandle or 0) == int(hwnd))
            if matches:
                break
    if matches:
        matched = matches[0]
        if tuple(int(x) for x in matched.GetRuntimeId()) != tuple(
                int(x) for x in direct.GetRuntimeId()):
            raise RuntimeError("desktop and HWND UIA routes returned different window identities")
        return matched, walker
    # Some providers omit HWND roots from the desktop ControlView. The verified
    # ElementFromHandle result remains a bounded, identity-checked fallback.
    return direct, walker


def _role(control_type: str) -> str:
    from core.screenview import _UIA_ROLES
    return _UIA_ROLES.get(control_type.replace(" ", "").lower(), "other")


def _element(raw: Any, eid: int, depth: int, parent_name: str,
             scale: float, pid: int, title: str):
    from core.screenview import Element, MIN_SIZE_PX

    try:
        control_id = int(raw.CurrentControlType)
        control_type = _CONTROL_TYPES.get(control_id, "Custom")
        protected = bool(raw.CurrentIsPassword)
        name = "[REDACTED]" if protected else str(raw.CurrentName or "")
        rect = raw.CurrentBoundingRectangle
        left, top, right, bottom = (int(rect.left), int(rect.top),
                                    int(rect.right), int(rect.bottom))
        width, height = right - left, bottom - top
        if width < MIN_SIZE_PX or height < MIN_SIZE_PX or bool(raw.CurrentIsOffscreen):
            return None
        role = _role(control_type)
        runtime_id = tuple(int(x) for x in raw.GetRuntimeId())
        automation_id = str(raw.CurrentAutomationId or "")
        enabled = bool(raw.CurrentIsEnabled)
        focused = bool(raw.CurrentHasKeyboardFocus)
        patterns = []
        values = {}
        value_read_only = None
        for pattern_name in _PATTERNS_BY_ROLE.get(role, ()):
            try:
                pattern = raw.GetCurrentPattern(_PATTERN_IDS[pattern_name])
                if pattern is not None and bool(pattern):
                    patterns.append(pattern_name)
                    if pattern_name == "value" and not protected:
                        from comtypes.gen import UIAutomationClient as uia
                        value_pattern = pattern.QueryInterface(
                            getattr(uia, _PATTERN_INTERFACES[pattern_name]))
                        values["value"] = str(value_pattern.CurrentValue or "")[:_VALUE_LIMIT]
                        value_read_only = bool(value_pattern.CurrentIsReadOnly)
                    elif pattern_name == "text" and not protected:
                        from comtypes.gen import UIAutomationClient as uia
                        text_pattern = pattern.QueryInterface(
                            getattr(uia, _PATTERN_INTERFACES[pattern_name]))
                        text_range = text_pattern.DocumentRange
                        values["value"] = str(text_range.GetText(_VALUE_LIMIT) or "")[:_VALUE_LIMIT]
            except Exception:
                # An unsupported or stale pattern does not discard the element.
                continue
        if protected:
            values["value"] = ""
        return Element(
            eid=eid, role=role, name=name, value=values.get("value", ""),
            left=int(round(left * scale)), top=int(round(top * scale)),
            width=int(round(width * scale)), height=int(round(height * scale)),
            enabled=enabled, focused=focused,
            scrollable=role in ("list", "table", "tree", "group"),
            depth=depth, protected=protected, automation_id=automation_id,
            value_read_only=value_read_only,
            control_type=control_type, runtime_id=runtime_id,
            parent_name=parent_name, patterns=tuple(patterns),
            window_pid=int(pid), window_title=title,
        )
    except Exception:
        return None


def scan(hwnd: int, pid: int, title: str, scale: float,
         deadline: float) -> list[Any]:
    """Enumerate the exact foreground window through CUIAutomation8 ControlView."""
    def work(automation: Any, uia: Any) -> list[Any]:
        root, walker = _process_roots(automation, uia, hwnd, pid, deadline)
        output = []
        frontier = [(root, 0, "")]
        visited = 0
        while frontier and len(output) < _MAX_ELEMENTS:
            if time.monotonic() >= deadline:
                raise TimeoutError("CUIAutomation8 tree walk exceeded its time budget")
            node, depth, parent_name = frontier.pop(0)
            visited += 1
            current_name = ""
            try:
                current_name = "[REDACTED]" if node.CurrentIsPassword else str(node.CurrentName or "")
            except Exception:
                pass
            element = _element(node, len(output), depth, parent_name,
                               scale, pid, title)
            if element is not None:
                output.append(element)
            if depth < _MAX_DEPTH:
                for child in _siblings(walker, node, deadline):
                    frontier.append((child, depth + 1, current_name))
        return output

    return _on_mta(work, timeout=max(0.2, deadline - time.monotonic() + 1.0))


def perform(hwnd: int, pid: int, title: str, runtime_id: tuple[int, ...],
            automation_id: str, name: str, control_type: str,
            protected: bool, pattern_name: str, value: Any = None,
            stop: Callable[[], bool] = lambda: False) -> tuple[bool, str]:
    """Re-resolve a control and run one supported UIA action on the MTA worker."""
    requested = str(pattern_name or "").lower()
    if requested == "collapse":
        requested = "expand_collapse"
        value = "collapse"
    if requested != "focus" and requested not in _PATTERN_IDS:
        return False, f"unsupported UIA pattern: {requested}"
    if requested in ("value", "range_value") and protected:
        return False, "protected fields cannot be changed by REM"
    if stop():
        return False, "desktop control is paused or emergency-stopped"

    def work(automation: Any, uia: Any) -> tuple[bool, str]:
        direct = automation.ElementFromHandle(int(hwnd))
        if direct is None or not bool(direct):
            return False, "UIA target window disappeared"
        if int(direct.CurrentProcessId) != int(pid) or int(
                direct.CurrentNativeWindowHandle or 0) != int(hwnd):
            return False, "UIA target window identity changed"
        walker = automation.ControlViewWalker
        queue = [(direct, 0)]
        found = None
        visited = 0
        deadline = time.monotonic() + 4.0
        while queue and visited < _MAX_ELEMENTS and time.monotonic() < deadline:
            node, depth = queue.pop(0)
            visited += 1
            try:
                current_id = tuple(int(x) for x in node.GetRuntimeId())
                current_automation_id = str(node.CurrentAutomationId or "")
                current_name = str(node.CurrentName or "")
                current_type = _CONTROL_TYPES.get(int(node.CurrentControlType), "Custom")
                if (current_id == tuple(runtime_id)
                        and (not automation_id or current_automation_id == automation_id)
                        and (not name or current_name == name)
                        and (not control_type or current_type == control_type)):
                    found = node
                    break
            except Exception:
                pass
            if depth < _MAX_DEPTH:
                for child in _siblings(walker, node, deadline):
                    queue.append((child, depth + 1))
        if found is None:
            return False, "UIA control disappeared or changed identity; observe again"
        if stop():
            return False, "desktop control was stopped before the UIA action"
        if requested == "focus":
            found.SetFocus()
        else:
            try:
                raw_pattern = found.GetCurrentPattern(_PATTERN_IDS[requested])
            except Exception as exc:
                return False, _failure("GetCurrentPattern", exc)
            if raw_pattern is None or not bool(raw_pattern):
                return False, f"the control no longer exposes {requested}"
            pattern = raw_pattern.QueryInterface(
                getattr(uia, _PATTERN_INTERFACES[requested]))
            if requested in ("value", "range_value"):
                if requested == "value" and bool(found.CurrentIsPassword):
                    return False, "protected fields cannot be changed by REM"
                expected = str(value if value is not None else "")
                pattern.SetValue(expected)
                if requested == "value":
                    actual = str(pattern.CurrentValue or "")
                    if actual != expected:
                        return False, "ValuePattern read-back did not match the requested value"
            elif requested == "invoke":
                pattern.Invoke()
            elif requested == "toggle":
                pattern.Toggle()
            elif requested == "selection_item":
                pattern.Select()
            elif requested == "selection":
                selection = pattern.GetCurrentSelection()
                count = int(selection.Length)
                return True, f"SelectionPattern read {count} selected item(s)"
            elif requested == "expand_collapse":
                if str(value or "expand").lower() == "collapse":
                    pattern.Collapse()
                else:
                    pattern.Expand()
            elif requested == "collapse":
                pattern.Collapse()
            elif requested == "scroll":
                horizontal, vertical = value or (0, 1)
                pattern.Scroll(int(horizontal), int(vertical))
            elif requested == "window":
                states = {"normal": 0, "restore": 0, "maximize": 1,
                          "minimize": 2, "maximized": 1, "minimized": 2}
                state = states.get(str(value or "").lower())
                if state is None:
                    return False, "WindowPattern requires normal, restore, maximize, or minimize"
                pattern.SetWindowVisualState(state)
        if stop():
            return False, "desktop control was stopped after the UIA action"
        return True, "UI Automation pattern completed"

    try:
        return _on_mta(work, timeout=6.0)
    except Exception as exc:
        return False, _failure("CUIAutomation8", exc)


def _failure(operation: str, exc: BaseException) -> str:
    result = f"{operation} failed: {type(exc).__name__}"
    value = getattr(exc, "hresult", None)
    if isinstance(value, int):
        result += f" (HRESULT 0x{value & 0xFFFFFFFF:08X})"
    return result
