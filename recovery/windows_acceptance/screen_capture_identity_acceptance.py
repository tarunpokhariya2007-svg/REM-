"""Real Windows monitor/window capture identity acceptance; stores no images."""
from __future__ import annotations

import io
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

from actions.screen_processor import _capture_screen_with_identity


def _wait_ready(path: Path, process: subprocess.Popen) -> dict:
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError("disposable capture window exited early")
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            if value.get("ready"):
                return value
        except (OSError, json.JSONDecodeError):
            pass
        time.sleep(0.05)
    raise TimeoutError("disposable capture window did not become ready")


def _capture(hwnd: int, pid: int, title: str, mode="monitor") -> tuple[dict, bool]:
    from PIL import Image
    import numpy as np
    import win32gui
    import win32process

    image_bytes, mime_type, identity = _capture_screen_with_identity(
        expected_hwnd=hwnd, expected_pid=pid, expected_title=title,
        capture_mode=mode)
    image = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    # The marker is a vivid magenta patch unique to the disposable fixture.
    pixels = np.asarray(image)
    found = bool(np.any((pixels[:, :, 0] > 190)
                        & (pixels[:, :, 1] < 100)
                        & (pixels[:, :, 2] > 190)))
    actual_pid = int(win32process.GetWindowThreadProcessId(hwnd)[1])
    actual_title = str(win32gui.GetWindowText(hwnd) or "")
    return ({"mode": identity["capture_mode"],
             "identity_validated": identity["identity_validated"],
             "window_specific_pixels": identity["window_specific_pixels"],
             "hwnd_matches": identity["window"]["hwnd"] == hwnd,
             "pid_matches": identity["window"]["pid"] == pid == actual_pid,
             "title_matches": identity["window"]["title"] == title == actual_title,
             "monitor_index": identity["monitor_index"],
             "monitor_bounds": identity["monitor_bounds"],
             "window_bounds": identity["window"]["bounds"],
             "window_capture_api": identity.get("window_capture_api"),
             "window_capture_fallback_error": identity.get("window_capture_fallback_error"),
             "image_size": list(image.size), "mime_type": mime_type}, found)


def run() -> dict:
    import win32gui
    import win32process

    base = Path(__file__).resolve().parent
    window_python = Path(sys.executable).with_name("pythonw.exe")
    if not window_python.is_file():
        raise RuntimeError("pythonw.exe is required for the disposable window")
    report = {"environment": "real Windows; REM MSS + Pillow HWND capture validation",
              "screenshots_saved_or_uploaded": False, "checks": {}}
    with tempfile.TemporaryDirectory(prefix="rem-screen-identity-") as directory:
        state_path, stop_path = Path(directory) / "state.json", Path(directory) / "stop"
        app = subprocess.Popen([str(window_python),
                                str(base / "mouse_test_window.py"),
                                str(state_path), str(stop_path)],
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        try:
            _wait_ready(state_path, app)
            hwnd = int(win32gui.FindWindow(None, "REM Disposable Mouse Acceptance") or 0)
            if not hwnd:
                raise RuntimeError("disposable fixture HWND was not found")
            win32gui.SetForegroundWindow(hwnd)
            time.sleep(0.25)
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            title = win32gui.GetWindowText(hwnd)
            first, marker = _capture(hwnd, int(pid), title)
            report["checks"]["initial_capture"] = {**first, "marker_found": marker,
                                                    "passed": bool(first["identity_validated"]
                                                                   and first["hwnd_matches"]
                                                                   and first["pid_matches"]
                                                                   and first["title_matches"]
                                                                   and marker)}

            window_capture, window_marker = _capture(hwnd, int(pid), title, "window")
            report["checks"]["window_capture"] = {
                **window_capture, "marker_found": window_marker,
                "passed": bool(window_capture["identity_validated"]
                and window_capture["mode"] == "window"
                               and window_capture["window_specific_pixels"]
                               and window_capture["hwnd_matches"]
                               and window_capture["pid_matches"]
                               and window_marker)}

            rect = win32gui.GetWindowRect(hwnd)
            win32gui.SetWindowPos(hwnd, None, rect[0] + 20, rect[1] + 15,
                                  rect[2] - rect[0] + 20,
                                  rect[3] - rect[1] + 10, 0x0004)
            time.sleep(0.2)
            moved, moved_marker = _capture(hwnd, int(pid), title)
            bounds_changed = moved["window_bounds"] != first["window_bounds"]
            report["checks"]["move_resize_capture"] = {
                **moved, "marker_found": moved_marker,
                "bounds_changed": bounds_changed,
                "passed": bool(moved["identity_validated"] and moved_marker
                               and bounds_changed)}

            win32gui.ShowWindow(hwnd, 6)  # SW_MINIMIZE: a test-only window.
            time.sleep(0.3)
            try:
                _capture_screen_with_identity(expected_hwnd=hwnd,
                                              expected_pid=int(pid),
                                              expected_title=title)
                minimized_rejected = False
                reason = "capture unexpectedly returned pixels for minimized/stale target"
            except Exception as exc:
                minimized_rejected = True
                reason = type(exc).__name__
            report["checks"]["minimized_target_rejected"] = {
                "passed": minimized_rejected, "reason": reason}
            return report
        finally:
            stop_path.write_text("close", encoding="ascii")
            try:
                app.wait(timeout=3)
            except subprocess.TimeoutExpired:
                app.terminate()
                app.wait(timeout=3)


if __name__ == "__main__":
    try:
        result = run()
        result["all_passed"] = all(row.get("passed") for row in result["checks"].values())
        print(json.dumps(result, indent=2))
        if not result["all_passed"]:
            raise SystemExit(1)
    except Exception as exc:
        print(json.dumps({"all_passed": False, "error": f"{type(exc).__name__}: {exc}"}, indent=2))
        raise
