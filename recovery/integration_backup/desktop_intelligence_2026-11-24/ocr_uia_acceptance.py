"""Actual OCR/UIA smoke test against the disposable local Tk surface."""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))
from core import screenview
from pywinauto import Desktop

WINDOW_SCRIPT = Path(__file__).with_name("mouse_test_window.py")


def run():
    with tempfile.TemporaryDirectory(prefix="rem-ocr-uia-") as tmp:
        root = Path(tmp)
        state_path, stop_path = root / "state.json", root / "stop"
        window_python = Path(sys.executable).with_name("pythonw.exe")
        if not window_python.is_file():
            raise RuntimeError("pythonw.exe is required for a separate disposable UI process")
        app = subprocess.Popen(
            [str(window_python), str(WINDOW_SCRIPT), str(state_path), str(stop_path)],
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        try:
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                try:
                    state = json.loads(state_path.read_text(encoding="utf-8"))
                    if state.get("ready"):
                        break
                except (OSError, json.JSONDecodeError):
                    pass
                time.sleep(0.05)
            else:
                raise TimeoutError("disposable OCR test window did not become ready")

            desktop = Desktop(backend="uia")
            windows = desktop.windows()
            target = next((w for w in windows
                           if w.window_text() == "REM Disposable Mouse Acceptance"), None)
            uia_report = {"desktop_initialized": True,
                          "visible_windows_count": len(windows),
                          "test_window_found": target is not None,
                          "test_window_title": target.window_text() if target else ""}
            if target:
                try:
                    child_names = [str(c.window_text()) for c in target.descendants()
                                   if c.window_text()]
                    uia_report["test_surface_controls"] = [
                        name for name in child_names
                        if name in {"Left click target", "Right-click target",
                                    "Double-click target", "Hover target"}
                    ]
                except Exception as exc:
                    uia_report["children_error"] = f"{type(exc).__name__}: {exc}"

            # Fresh screenshot is captured inside _scan_ocr; only known test
            # strings and their local rectangles are returned to the console.
            elements = screenview._scan_ocr(
                screenview.monitor_rect(), time.monotonic() + 120)
            expected_phrases = ("REM MOUSE ACCEPTANCE", "Left click target",
                                "Right-click target", "Hover target")
            accepted = []
            for el in elements:
                if any(phrase.casefold() in el.name.casefold()
                       for phrase in expected_phrases):
                    accepted.append({"text": el.name,
                                     "box": [el.left, el.top, el.width, el.height]})
            return {"uia": uia_report,
                    "ocr_backend": "RapidOCR + ONNX Runtime CPU",
                    "ocr_element_count": len(elements),
                    "matching_test_text_and_boxes": accepted,
                    "real_screenshot_used": True,
                    "screenshot_saved": False}
        finally:
            stop_path.write_text("close", encoding="ascii")
            try:
                app.wait(timeout=3)
            except subprocess.TimeoutExpired:
                app.terminate()
                app.wait(timeout=3)


if __name__ == "__main__":
    print(json.dumps(run(), indent=2))
