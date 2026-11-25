"""Real-input acceptance runner for REM against a disposable local Tk window.

This is intentionally not part of the normal unittest suite. It moves/clicks
the real Windows pointer and must be started only with an explicit test run.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import ctypes
import argparse
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))
from core import desktop_control, screenact, screenview
import win32gui

BASE = Path(__file__).resolve().parent
WINDOW_SCRIPT = BASE / "mouse_test_window.py"
_WINDOW_PHYSICAL_ORIGIN = (0, 0)
_WINDOW_LOGICAL_ORIGIN = (0, 0)
_DPI_SCALE_X = 1.0
_DPI_SCALE_Y = 1.0


def wait_state(path: Path, predicate, timeout=5.0, process=None):
    deadline = time.monotonic() + timeout
    latest = {}
    while time.monotonic() < deadline:
        try:
            latest = json.loads(path.read_text(encoding="utf-8"))
            if predicate(latest):
                return latest
        except (OSError, json.JSONDecodeError):
            pass
        if process is not None and process.poll() is not None:
            detail = process.stdout.read() if process.stdout else ""
            raise RuntimeError(
                f"disposable window exited early ({process.returncode}); {detail[-2000:]}"
            )
        time.sleep(0.04)
    raise TimeoutError(f"disposable window did not reach expected state; last={latest}")


def center(state, name):
    r = state["controls"][name]
    # Tk reports DPI-virtualized coordinates on a scaled Windows display;
    # screenact/PyAutoGUI address physical pixels. Convert using the test
    # window's actual DPI so the physical pointer lands on the named control.
    x = r["left"] + r["width"] // 2
    y = r["top"] + r["height"] // 2
    return screenact.Point(round(_WINDOW_PHYSICAL_ORIGIN[0] +
                                 (x - _WINDOW_LOGICAL_ORIGIN[0]) * _DPI_SCALE_X),
                           round(_WINDOW_PHYSICAL_ORIGIN[1] +
                                 (y - _WINDOW_LOGICAL_ORIGIN[1]) * _DPI_SCALE_Y),
                           origin="user", label=name)


def run():
    global _WINDOW_PHYSICAL_ORIGIN, _WINDOW_LOGICAL_ORIGIN, _DPI_SCALE_X, _DPI_SCALE_Y
    try:
        import pyautogui
    except Exception as exc:
        raise RuntimeError(f"PyAutoGUI could not load: {exc}") from exc
    available, reason = screenact.available()
    if not available:
        raise RuntimeError(f"REM input is unavailable: {reason}")

    with tempfile.TemporaryDirectory(prefix="rem-mouse-accept-") as tmp:
        root = Path(tmp)
        state_path, stop_path = root / "state.json", root / "stop"
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        window_python = Path(sys.executable).with_name("pythonw.exe")
        if not window_python.is_file():
            raise RuntimeError("pythonw.exe is required to keep the disposable test app outside REM's self-process guard")
        app = subprocess.Popen(
            [str(window_python), str(WINDOW_SCRIPT), str(state_path), str(stop_path)],
            creationflags=flags,
        )
        summary = {"test_window": "REM Disposable Mouse Acceptance", "checks": {}}
        try:
            state = wait_state(state_path, lambda value: value.get("ready"), process=app)
            hwnd = win32gui.FindWindow(None, "REM Disposable Mouse Acceptance")
            if not hwnd:
                raise RuntimeError("could not locate the disposable mouse-test window handle")
            get_dpi = ctypes.windll.user32.GetDpiForWindow
            get_dpi.argtypes = [ctypes.c_void_p]
            get_dpi.restype = ctypes.c_uint
            window_dpi = get_dpi(hwnd)
            physical_rect = win32gui.GetWindowRect(hwnd)
            logical_window = state["window"]
            client_rect = win32gui.GetClientRect(hwnd)
            client_origin = win32gui.ClientToScreen(hwnd, (0, 0))
            _WINDOW_PHYSICAL_ORIGIN = client_origin
            _WINDOW_LOGICAL_ORIGIN = (logical_window["left"], logical_window["top"])
            # Use the client area, since Tk widget coordinates are relative to
            # that area. Scaling from the outer window rectangle adds the
            # non-client title/frame offsets and was the prior hover miss.
            _DPI_SCALE_X = ((client_rect[2] - client_rect[0]) /
                            logical_window["width"])
            _DPI_SCALE_Y = ((client_rect[3] - client_rect[1]) /
                            logical_window["height"])
            summary["window_dpi"] = window_dpi
            summary["window_rect_physical"] = physical_rect
            summary["client_rect_physical"] = client_rect
            summary["client_origin_physical"] = client_origin
            summary["physical_coordinate_scale"] = [_DPI_SCALE_X, _DPI_SCALE_Y]
            foreground = screenview._foreground()
            summary["foreground_title_at_start"] = str(foreground[1])
            # Start away from the target to ensure the subsequent entry test is
            # a new pointer crossing rather than an already-hovered state.
            pyautogui.moveTo(40, 40)
            time.sleep(0.15)

            # Absolute semantic point movement; verify the actual OS cursor.
            target = center(state, "hover")
            hover_rect = state["controls"]["hover"]
            act = screenact.move_cursor(target)
            position = tuple(map(int, pyautogui.position()))
            expected = (target.x, target.y)
            coordinate_error = (abs(position[0] - expected[0]),
                                abs(position[1] - expected[1]))
            summary["checks"]["absolute_move"] = {
                "act_ok": act.ok, "cursor": position, "expected": expected,
                "coordinate_error_px": coordinate_error,
                "act_point": act.point,
                "hover_widget_state_rect": hover_rect,
                "window_state_rect": logical_window,
                "cursor_win32": tuple(map(int, win32gui.GetCursorPos())),
                "window_at_expected": int(win32gui.GetAncestor(
                    win32gui.WindowFromPoint(expected), 2) or 0),
                "window_at_cursor": int(win32gui.GetAncestor(
                    win32gui.WindowFromPoint(position), 2) or 0),
                # Windows may round a logical coordinate to the adjacent
                # physical pixel when the target window is DPI-scaled.
                "passed": act.ok and max(coordinate_error) <= 1,
            }
            if not summary["checks"]["absolute_move"]["passed"]:
                raise AssertionError("absolute cursor position did not match the requested point")
            under = win32gui.WindowFromPoint(expected)
            under_root = win32gui.GetAncestor(under, 2) if under else 0
            under_title = win32gui.GetWindowText(under_root) if under_root else ""
            summary["checks"]["absolute_move"]["window_under_cursor"] = under_title
            summary["checks"]["absolute_move"]["target_window_handle_match"] = under_root == hwnd
            if under_root != hwnd:
                raise AssertionError(
                    f"cursor is over {under_title!r} rather than the disposable test window; no click was attempted"
                )
            state = wait_state(state_path, lambda s: s.get("events", {}).get("hover", 0) > 0)
            summary["checks"]["hover_delivery"] = {
                "passed": True,
                "event_count": state.get("events", {}).get("hover", 0),
                "physical_cursor": tuple(map(int, pyautogui.position())),
                "window_dpi": window_dpi,
                "client_origin_physical": client_origin,
            }
            if _args.hover_only:
                summary["all_passed"] = True
                summary["mode"] = "hover-only; no buttons or scroll events were sent"
                return summary

            if _args.left_click_only:
                point = center(state, "left")
                moved = screenact.move_cursor(point)
                position_before = tuple(map(int, pyautogui.position()))
                under_before = win32gui.WindowFromPoint(position_before)
                root_before = win32gui.GetAncestor(under_before, 2) if under_before else 0
                if (not moved.ok or position_before != (point.x, point.y)
                        or root_before != hwnd):
                    raise RuntimeError(
                        "left-click diagnostic refused: target point did not resolve "
                        "inside the disposable test window"
                    )
                act = screenact.click(point)
                position = tuple(map(int, pyautogui.position()))
                under = win32gui.WindowFromPoint(position)
                under_root = win32gui.GetAncestor(under, 2) if under else 0
                summary["checks"]["left_click_diagnostic"] = {
                    "act_ok": act.ok, "act_error": act.error,
                    "target_point": [point.x, point.y],
                    "cursor_after": position,
                    "window_under_cursor": win32gui.GetWindowText(under_root)
                    if under_root else "",
                    "target_window_handle_match": under_root == hwnd,
                }
                state = wait_state(state_path,
                    lambda s: s.get("events", {}).get("left_click", 0) > 0)
                summary["checks"]["left_click_diagnostic"]["observed_event"] = (
                    state["events"].get("left_click", 0))
                summary["checks"]["left_click_diagnostic"]["passed"] = bool(act.ok)
                summary["all_passed"] = bool(act.ok)
                summary["mode"] = "single left click on disposable test control"
                return summary

            if _args.mouse_release_only:
                state = wait_state(state_path, lambda _s: True)
                point = center(state, "hold")
                pyautogui.moveTo(point.x, point.y)
                under = win32gui.WindowFromPoint((point.x, point.y))
                under_root = win32gui.GetAncestor(under, 2) if under else 0
                if under_root != hwnd:
                    raise RuntimeError("release test target is outside the disposable test window")
                pyautogui.mouseDown(button="left")
                state = wait_state(state_path, lambda s: s.get("mouse_down") is True)
                down_state = bool(ctypes.windll.user32.GetAsyncKeyState(0x01) & 0x8000)
                screenact.release_all()
                time.sleep(0.2)
                state = json.loads(state_path.read_text(encoding="utf-8"))
                up_state = bool(ctypes.windll.user32.GetAsyncKeyState(0x01) & 0x8000)
                summary["checks"]["mouse_down_release_diagnostic"] = {
                    "down_event_observed": True,
                    "os_button_down_before_release": down_state,
                    "os_button_down_after_release_all": up_state,
                    "app_mouse_down_after_release_all": state.get("mouse_down"),
                    "release_event_count": state.get("events", {}).get("released", 0),
                    "passed": down_state and not up_state and not state.get("mouse_down"),
                }
                summary["all_passed"] = summary["checks"]["mouse_down_release_diagnostic"]["passed"]
                summary["mode"] = "mouse down and release_all diagnostic only"
                return summary

            before = tuple(map(int, pyautogui.position()))
            act = screenact.move_relative(13, 9)
            actual = tuple(map(int, pyautogui.position()))
            expected = (before[0] + 13, before[1] + 9)
            summary["checks"]["relative_move"] = {
                "act_ok": act.ok, "cursor": actual, "expected": expected,
                "passed": act.ok and actual == expected,
            }
            if not summary["checks"]["relative_move"]["passed"]:
                raise AssertionError("relative cursor position did not match the requested offset")

            checks = [
                ("left_click", lambda: screenact.click(center(state, "left")), "left_click"),
                ("right_click", lambda: screenact.right_click(center(state, "right")), "right_click"),
                ("middle_click", lambda: screenact.click(center(state, "middle"), button="middle"), "middle_click"),
                ("double_click", lambda: screenact.double_click(center(state, "double")), "double_click"),
                ("hover", lambda: screenact.hover(center(state, "hover")), "hover"),
            ]
            for name, operation, event_name in checks:
                state = wait_state(state_path, lambda _s: True)
                old = state.get("events", {}).get(event_name, 0)
                act = operation()
                state = wait_state(state_path,
                                   lambda s, key=event_name, count=old: s.get("events", {}).get(key, 0) > count)
                summary["checks"][name] = {"act_ok": act.ok,
                                           "observed_event": state["events"].get(event_name, 0),
                                           "passed": act.ok}
                if not act.ok:
                    raise AssertionError(f"REM {name} did not deliver input: {act}")

            state = wait_state(state_path, lambda _s: True)
            old_scroll = state.get("events", {}).get("scroll", 0)
            act = screenact.scroll("down", 2, target=center(state, "scroll"))
            state = wait_state(state_path,
                               lambda s: s.get("events", {}).get("scroll", 0) > old_scroll)
            summary["checks"]["scroll"] = {"act_ok": act.ok,
                                             "observed_event": state["events"].get("scroll", 0),
                                             "passed": act.ok}
            if not act.ok:
                raise AssertionError(f"REM scroll did not deliver input: {act}")

            state = wait_state(state_path, lambda _s: True)
            old_drop = state.get("events", {}).get("drop_success", 0)
            act = screenact.drag(center(state, "drag_source"),
                                 center(state, "drop_target"), duration=0.55)
            state = wait_state(state_path,
                               lambda s: s.get("events", {}).get("drop_success", 0) > old_drop)
            summary["checks"]["drag_drop"] = {
                "act_ok": act.ok, "drop_success": state.get("drop_success"),
                "passed": act.ok and state.get("drop_success") is True,
            }
            if not summary["checks"]["drag_drop"]["passed"]:
                raise AssertionError("drag did not land in the disposable drop target")

            # Hold a real button, then use REM's emergency-stop cleanup path.
            state = wait_state(state_path, lambda _s: True)
            hold_point = center(state, "hold")
            pyautogui.moveTo(hold_point.x, hold_point.y)
            pyautogui.mouseDown(button="left")
            state = wait_state(state_path, lambda s: s.get("mouse_down") is True)
            screenact.release_all()
            state = wait_state(state_path, lambda s: s.get("mouse_down") is False)
            summary["checks"]["mouse_down_release"] = {
                "observed_down": True, "released": not state.get("mouse_down"),
                "passed": not state.get("mouse_down"),
            }

            # Interrupt an in-progress, segmented drag. Stop releases the held
            # button, the remaining path must be skipped, and the result false.
            state = wait_state(state_path, lambda _s: True)
            stop_fired = threading.Event()
            stop_result = {}
            def stop_during_drag():
                deadline = time.monotonic() + 2.0
                while time.monotonic() < deadline:
                    try:
                        current = json.loads(state_path.read_text(encoding="utf-8"))
                        if current.get("mouse_down") is True:
                            break
                    except (OSError, json.JSONDecodeError):
                        pass
                    time.sleep(0.01)
                else:
                    stop_result["triggered_while_button_down"] = False
                    stop_fired.set()
                    return
                stop_result["triggered_while_button_down"] = True
                stop_result.update(desktop_control.emergency_stop())
                stop_fired.set()
            stop_timer = threading.Timer(0.0, stop_during_drag)
            stop_timer.start()
            act = screenact.drag(center(state, "drag_source"),
                                 center(state, "drop_target"), duration=1.5)
            stop_timer.join(timeout=2.0)
            state = wait_state(state_path, lambda s: s.get("mouse_down") is False)
            summary["checks"]["emergency_stop_during_drag"] = {
                "act_ok": act.ok, "act_error": act.error,
                "stop_callback_fired": stop_fired.is_set(),
                "triggered_while_button_down": stop_result.get("triggered_while_button_down"),
                "stop_latched": bool(stop_result.get("stopped")),
                "cancelled_result": not act.ok and "cancelled" in act.error,
                "button_released": state.get("mouse_down") is False,
                "drop_success": state.get("drop_success"),
                "passed": (stop_fired.is_set()
                           and stop_result.get("triggered_while_button_down") is True
                           and bool(stop_result.get("stopped"))
                           and not act.ok and "cancelled" in act.error
                           and state.get("mouse_down") is False
                           and state.get("drop_success") is False),
            }
            if not summary["checks"]["emergency_stop_during_drag"]["passed"]:
                raise AssertionError("emergency-stop did not cancel the drag and release the button")
            desktop_control.resume()

            summary["all_passed"] = all(row.get("passed") for row in summary["checks"].values())
            return summary
        except Exception as exc:
            print(json.dumps({"all_passed": False, **summary,
                              "error": f"{type(exc).__name__}: {exc}"}, indent=2))
            raise
        finally:
            stop_path.write_text("close", encoding="ascii")
            try:
                app.wait(timeout=3.0)
            except subprocess.TimeoutExpired:
                app.terminate()
                app.wait(timeout=3.0)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--hover-only", action="store_true",
                        help="stop successfully after the disposable hover event; do not click")
    parser.add_argument("--left-click-only", action="store_true",
                        help="after the hover gate, test only the disposable left-click control")
    parser.add_argument("--mouse-release-only", action="store_true",
                        help="after the hover gate, test only mouse down and release_all")
    _args = parser.parse_args()
    print(json.dumps(run(), indent=2))
