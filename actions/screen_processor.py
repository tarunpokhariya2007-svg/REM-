"""
Screen & webcam capture for REM vision.

Provides the two capture entry points main.py uses — `_capture_screen()` and
`_capture_camera()` — plus their helpers (compression, camera auto-detection,
config access). main.py grabs a frame here on demand, then injects it into the
main Gemini Live session; there is no separate vision session here.
"""
from __future__ import annotations

import io
import json
import sys
import time
from pathlib import Path

import numpy as np

from core.lazyimport import LazyModule, installed

# OpenCV is 224 ms and is only ever touched by the webcam path — see
# core/lazyimport.py. Screen capture, which is what this action does on almost
# every call, never reaches it. numpy above stays eager: main.py already
# imports it for audio, so it is in memory before this file is read.
_CV2 = installed("cv2")

cv2 = LazyModule("cv2")

try:
    import mss
    import mss.tools
    _MSS = True
except ImportError:
    _MSS = False

try:
    import PIL.Image
    _PIL = True
except ImportError:
    _PIL = False


def _base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


_BASE        = _base_dir()
_CONFIG_PATH = _BASE / "config" / "api_keys.json"


def _load_config() -> dict:
    try:
        return json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_config_key(key: str, value) -> None:
    try:
        cfg = _load_config()
        cfg[key] = value
        _CONFIG_PATH.write_text(json.dumps(cfg, indent=4), encoding="utf-8")
    except Exception as e:
        print(f"[Vision] ⚠️  Could not save config key '{key}': {e}")


def _get_os() -> str:
    return _load_config().get("os_system", "windows").lower()


_IMG_MAX_W = 1280
_IMG_MAX_H = 720
_JPEG_Q    = 82


def _compress(img_bytes: bytes, source_format: str = "PNG") -> tuple[bytes, str]:
    if not _PIL:
        return img_bytes, f"image/{source_format.lower()}"

    try:
        img = PIL.Image.open(io.BytesIO(img_bytes)).convert("RGB")
        img.thumbnail((_IMG_MAX_W, _IMG_MAX_H), PIL.Image.BILINEAR)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=_JPEG_Q, optimize=False)
        return buf.getvalue(), "image/jpeg"
    except Exception as e:
        print(f"[Vision] ⚠️  Image compress failed: {e}")
        return img_bytes, f"image/{source_format.lower()}"


def _capture_screen() -> tuple[bytes, str]:

    if not _MSS:
        raise RuntimeError("mss is not installed. Run: pip install mss")

    with mss.mss() as sct:
        monitors = sct.monitors          # [0] = all combined, [1..n] = real screens
        target   = monitors[1] if len(monitors) > 1 else monitors[0]
        shot     = sct.grab(target)
        png      = mss.tools.to_png(shot.rgb, shot.size)

    return _compress(png, "PNG")


def _capture_screen_with_identity(*, expected_hwnd: int | None = None,
                                  expected_pid: int | None = None,
                                  expected_title: str | None = None,
                                  capture_mode: str = "monitor"):
    """Capture a stable foreground HWND or its monitor and report the exact mode.

    ``capture_mode="window"`` first asks Pillow's supported Win32 HWND capture
    API for window-specific pixels. If that API fails or returns invalid
    dimensions, the function falls back to the monitor containing the target's
    center and reports ``capture_mode="monitor"``. In both modes the foreground
    HWND/PID/title/class/bounds are validated before and after capture; monitor
    mode never claims that the pixels contain only the selected window.
    """
    requested_mode = str(capture_mode or "monitor").strip().casefold()
    if requested_mode not in {"monitor", "window"}:
        raise ValueError("capture_mode must be 'monitor' or 'window'")
    if not _MSS:
        raise RuntimeError("mss is not installed; identity-bound monitor capture is unavailable")
    if _get_os() != "windows":
        image, mime_type = _capture_screen()
        return image, mime_type, {"capture_mode": "monitor", "identity_validated": False,
                                  "reason": "native HWND validation is Windows-only"}

    import win32gui
    import win32process
    from core import screenview

    def identity():
        app, title, pid, raw_hwnd = screenview._foreground()
        hwnd = int(raw_hwnd or 0)
        if not hwnd or not win32gui.IsWindow(hwnd):
            raise RuntimeError("Foreground window handle is missing or stale.")
        actual_pid = int(win32process.GetWindowThreadProcessId(hwnd)[1])
        actual_title = str(win32gui.GetWindowText(hwnd) or "")
        actual_class = str(win32gui.GetClassName(hwnd) or "")
        if not win32gui.IsWindowVisible(hwnd) or win32gui.IsIconic(hwnd):
            raise RuntimeError("Foreground window is hidden or minimized.")
        rect = tuple(int(v) for v in win32gui.GetWindowRect(hwnd))
        if actual_pid != int(pid or 0) or rect[2] <= rect[0] or rect[3] <= rect[1]:
            raise RuntimeError("Foreground window identity or bounds are invalid.")
        if expected_hwnd is not None and hwnd != int(expected_hwnd):
            raise RuntimeError("Foreground window no longer matches the requested HWND.")
        if expected_pid is not None and actual_pid != int(expected_pid):
            raise RuntimeError("Foreground process no longer matches the requested PID.")
        if expected_title is not None and actual_title != str(expected_title):
            raise RuntimeError("Foreground title no longer matches the requested window.")
        center = ((rect[0] + rect[2]) // 2, (rect[1] + rect[3]) // 2)
        child = int(win32gui.WindowFromPoint(center) or 0)
        root = int(win32gui.GetAncestor(child, 2) or child) if child else 0
        if root != hwnd:
            raise RuntimeError("Foreground window center is occluded by another window.")
        return {"hwnd": hwnd, "pid": actual_pid, "title": actual_title,
                "class_name": actual_class, "bounds": rect,
                "center": center, "app": str(app or "")}

    keys = ("hwnd", "pid", "title", "class_name", "bounds")
    actual_mode = "monitor"
    window_specific_pixels = False
    window_capture_error = None
    for attempt in range(2):
        before = identity()
        with mss.mss() as sct:
            monitor_index = None
            monitor = None
            cx, cy = before["center"]
            for index, candidate in enumerate(sct.monitors[1:], start=1):
                if (candidate["left"] <= cx < candidate["left"] + candidate["width"]
                        and candidate["top"] <= cy < candidate["top"] + candidate["height"]):
                    monitor_index, monitor = index, candidate
                    break
            if monitor is None:
                raise RuntimeError("No monitor contains the foreground window center.")
            if requested_mode == "window":
                try:
                    from PIL import ImageGrab
                    window_image = ImageGrab.grab(window=before["hwnd"])
                    image_size = tuple(int(v) for v in window_image.size)
                    # Pillow's Win32 HWND capture is DPI-virtualized: its
                    # returned pixel dimensions can be logical units while
                    # GetWindowRect is physical. Identity comes from the
                    # HWND-targeted API plus before/after process validation,
                    # not a false equality between those coordinate spaces.
                    if image_size[0] < 2 or image_size[1] < 2:
                        raise RuntimeError("HWND capture returned an empty image")
                    output = io.BytesIO()
                    window_image.save(output, format="PNG")
                    image, mime_type = _compress(output.getvalue(), "PNG")
                    actual_mode = "window"
                    window_specific_pixels = True
                except Exception as exc:
                    # Retain only the exception type; details can contain paths
                    # or other information from the desktop provider.
                    window_capture_error = type(exc).__name__
            if actual_mode != "window":
                shot = sct.grab(monitor)
                png = mss.tools.to_png(shot.rgb, shot.size)
                image, mime_type = _compress(png, "PNG")
                image_size = tuple(int(v) for v in shot.size)

        after = identity()
        changed = [key for key in keys if before[key] != after[key]]
        if not changed:
            break
        if attempt == 0 and changed == ["bounds"]:
            # Windows can finish applying DPI/layout bounds just after a new
            # foreground window is shown. Retry one read-only capture once.
            time.sleep(0.08)
            continue
        # Field names only: window titles and coordinates can disclose user
        # context and do not belong in routine logs or exception text.
        raise RuntimeError("Foreground window identity changed during capture (fields: "
                           + ", ".join(changed) + ").")
    if actual_mode == "monitor":
        expected_size = (int(monitor["width"]), int(monitor["height"]))
        if image_size != expected_size:
            raise RuntimeError("Monitor screenshot dimensions do not match its reported bounds.")
    delivered_size = image_size
    if _PIL:
        try:
            with PIL.Image.open(io.BytesIO(image)) as delivered_image:
                delivered_size = tuple(int(v) for v in delivered_image.size)
        except Exception as exc:
            raise RuntimeError("Compressed screenshot dimensions could not be verified.") from exc
    safe_identity = {key: before[key] for key in keys}
    return image, mime_type, {
        "capture_mode": actual_mode,
        "identity_validated": True,
        "window_specific_pixels": window_specific_pixels,
        "window_capture_api": ("Pillow ImageGrab.grab(window=HWND)"
                               if window_specific_pixels else None),
        "window": safe_identity,
        "monitor_index": monitor_index,
        "monitor_bounds": {key: int(monitor[key])
                           for key in ("left", "top", "width", "height")},
        "source_image_size": image_size,
        "image_size": delivered_size,
        "center_unoccluded": True,
        "window_capture_fallback_error": window_capture_error,
    }


def _cv2_backend() -> int:
    """Return the best OpenCV camera backend for the current OS."""
    if not _CV2:
        return 0
    os_name = _get_os()
    if os_name == "windows":
        return cv2.CAP_DSHOW
    if os_name == "mac":
        return cv2.CAP_AVFOUNDATION
    return cv2.CAP_ANY


def _probe_camera(index: int, backend: int, warmup: int = 5) -> bool:

    if not _CV2:
        return False
    cap = cv2.VideoCapture(index, backend)
    if not cap.isOpened():
        cap.release()
        return False
    for _ in range(warmup):
        cap.read()
    ret, frame = cap.read()
    cap.release()
    if not ret or frame is None:
        return False
    return bool(np.mean(frame) > 8)


def _detect_camera_index() -> int:

    backend = _cv2_backend()
    print("[Vision] 🔍 Auto-detecting camera...")
    for idx in range(6):
        if _probe_camera(idx, backend):
            print(f"[Vision] ✅ Camera found at index {idx}")
            _save_config_key("camera_index", idx)
            return idx
        print(f"[Vision] ⚠️  Camera index {idx}: no usable frame")

    print("[Vision] ⚠️  No camera found — defaulting to index 0")
    _save_config_key("camera_index", 0)
    return 0


def _get_camera_index() -> int:
    cfg = _load_config()
    if "camera_index" in cfg:
        return int(cfg["camera_index"])
    return _detect_camera_index()


def _capture_camera() -> tuple[bytes, str]:
    if not _CV2:
        raise RuntimeError("OpenCV (cv2) is not installed. Run: pip install opencv-python")

    index   = _get_camera_index()
    backend = _cv2_backend()
    cap     = cv2.VideoCapture(index, backend)

    if not cap.isOpened():
        raise RuntimeError(f"Camera index {index} could not be opened.")

    for _ in range(10):
        cap.read()

    ret, frame = cap.read()
    cap.release()

    if not ret or frame is None:
        raise RuntimeError("Camera returned no frame.")

    if _PIL:
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        img = PIL.Image.fromarray(rgb)
        img.thumbnail((_IMG_MAX_W, _IMG_MAX_H), PIL.Image.BILINEAR)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=_JPEG_Q)
        return buf.getvalue(), "image/jpeg"

    _, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, _JPEG_Q])
    return buf.tobytes(), "image/jpeg"
