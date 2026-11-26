import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PyQt6.QtCore import QPointF, QRect, Qt
from PyQt6.QtGui import QMouseEvent
from PyQt6.QtWidgets import QApplication

from gui.floating_companion import (
    CHIBI_ASSET_PATH,
    FloatingCompanionSettingsOverlay,
    FloatingCompanionWindow,
    WINDOW_STATE_EXITING,
    WINDOW_STATE_FLOATING_VISIBLE,
    WINDOW_STATE_MAIN_VISIBLE,
    clamp_to_usable_screens,
    compute_companion_size,
    default_bottom_right_position,
)
from memory import config_manager


class FloatingCompanionAssetAndGeometryTests(unittest.TestCase):
    def test_chibi_asset_is_transparent_rgba_png_without_clipping(self):
        from PIL import Image

        self.assertTrue(CHIBI_ASSET_PATH.exists(), f"Missing asset: {CHIBI_ASSET_PATH}")
        with Image.open(CHIBI_ASSET_PATH) as im:
            self.assertEqual(im.mode, "RGBA")
            self.assertGreaterEqual(im.width, 400)
            self.assertGreaterEqual(im.height, 700)
            # Corner pixels must be transparent (exterior background removed)
            self.assertEqual(im.getpixel((0, 0))[3], 0)
            self.assertEqual(im.getpixel((im.width - 1, 0))[3], 0)
            self.assertEqual(im.getpixel((0, im.height - 1))[3], 0)
            # Center chest/face pixel must remain opaque
            self.assertEqual(im.getpixel((im.width // 2, im.height // 2))[3], 255)

    def test_config_persistence_preserves_other_settings(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "api_keys.json"
            cfg.write_text(json.dumps({"user_name": "Tarun-kun"}), encoding="utf-8")
            with patch.object(config_manager, "CONFIG_FILE", cfg):
                dflt = config_manager.get_floating_companion_settings()
                self.assertTrue(dflt["enabled"])
                self.assertTrue(dflt["show_on_minimize"])
                self.assertTrue(dflt["always_on_top"])
                self.assertTrue(dflt["remember_position"])
                self.assertEqual(dflt["size"], 190)

                saved = config_manager.save_floating_companion_settings(
                    size=210, last_x=1400, last_y=600, start_minimized=True
                )
                self.assertEqual(saved["size"], 210)
                self.assertEqual(saved["last_x"], 1400)
                self.assertEqual(saved["last_y"], 600)
                self.assertTrue(saved["start_minimized"])

                raw = json.loads(cfg.read_text(encoding="utf-8"))
                self.assertEqual(raw["user_name"], "Tarun-kun")

    def test_size_scaling_default_corner_and_multi_monitor_clamping(self):
        r1080 = QRect(0, 0, 1920, 1040)
        r768 = QRect(0, 0, 1366, 728)
        r4k = QRect(0, 0, 3840, 2100)
        self.assertGreaterEqual(compute_companion_size(190, r1080), 175)
        self.assertLessEqual(compute_companion_size(190, r768), 160)
        self.assertGreater(compute_companion_size(190, r4k), compute_companion_size(190, r1080))

        bx, by = default_bottom_right_position(110, 190, r1080, margin=20)
        self.assertGreater(bx, 1700)
        self.assertGreater(by, 800)

        # Clamps back inside second monitor negative coordinates cleanly
        left_mon = QRect(-1920, 0, 1920, 1040)
        cx, cy = clamp_to_usable_screens(-2500, -100, 110, 190, [left_mon, r1080], r1080)
        self.assertGreaterEqual(cx, -1916)
        self.assertGreaterEqual(cy, 4)

        # Resolution shrink pulls far-right coordinates back on screen
        sx, sy = clamp_to_usable_screens(1850, 950, 110, 190, [QRect(0, 0, 1280, 720)], QRect(0, 0, 1280, 720))
        self.assertLessEqual(sx + 110, 1280)
        self.assertLessEqual(sy + 190, 720)


class FloatingCompanionWidgetGUITests(unittest.TestCase):
    def test_drag_click_cycle_settings_and_startup_minimized_in_offscreen_qt(self):
        repo_root = str(Path(__file__).resolve().parent.parent)
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "api_keys.json"
            cfg.write_text(json.dumps({
                "os_system": "windows",
                "local_only": True,
                "assistant_name": "REM",
            }), encoding="utf-8")
            script = f"""
import sys
from pathlib import Path
from unittest.mock import patch
from PyQt6.QtCore import QPointF, Qt
from PyQt6.QtGui import QMouseEvent
from PyQt6.QtWidgets import QApplication
from memory import config_manager
from gui.window import MainWindow
from gui.floating_companion import (
    FloatingCompanionSettingsOverlay,
    WINDOW_STATE_MAIN_VISIBLE,
    WINDOW_STATE_FLOATING_VISIBLE,
    WINDOW_STATE_EXITING,
)

app = QApplication([])
cfg = Path({str(cfg)!r})
face = str(Path({repo_root!r}) / "gui" / "assets" / "rem" / "rem-base.png")
with patch.object(config_manager, "CONFIG_FILE", cfg):
    win = MainWindow(face)
    win.show()
    app.processEvents()
    assert win._app_window_state == WINDOW_STATE_MAIN_VISIBLE
    assert not win._floating_companion.isVisible()

    # Cycle 1: minimize -> shows floating companion, hides dashboard
    win._enter_floating_companion_mode()
    app.processEvents()
    assert win._app_window_state == WINDOW_STATE_FLOATING_VISIBLE
    assert win._floating_companion.isVisible()
    assert not win.isVisible()

    # Drag >= 10px -> moves floating companion and commits position WITHOUT restoring dashboard
    fc = win._floating_companion
    x0, y0 = fc.x(), fc.y()
    g0 = fc.frameGeometry().topLeft()
    p0 = QPointF(g0.x() + 20, g0.y() + 20)
    fc.mousePressEvent(QMouseEvent(QMouseEvent.Type.MouseButtonPress, QPointF(20, 20), p0, Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier))
    p1 = QPointF(p0.x() - 80, p0.y() - 60)
    fc.mouseMoveEvent(QMouseEvent(QMouseEvent.Type.MouseMove, QPointF(20, 20), p1, Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier))
    fc.mouseReleaseEvent(QMouseEvent(QMouseEvent.Type.MouseButtonRelease, QPointF(20, 20), p1, Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier))
    app.processEvents()
    assert win._app_window_state == WINDOW_STATE_FLOATING_VISIBLE
    assert fc.isVisible()
    assert (fc.x(), fc.y()) != (x0, y0)
    saved_x, saved_y = fc.x(), fc.y()

    # Expression forwarding keeps working while minimized
    win._expression_sig.emit("HAPPY", 4.0, "test")
    app.processEvents()
    assert fc._expression == "HAPPY"

    # True click (< 5px movement) -> hides companion and restores original MainWindow
    g1 = fc.frameGeometry().topLeft()
    cp = QPointF(g1.x() + 20, g1.y() + 20)
    fc.mousePressEvent(QMouseEvent(QMouseEvent.Type.MouseButtonPress, QPointF(20, 20), cp, Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier))
    fc.mouseReleaseEvent(QMouseEvent(QMouseEvent.Type.MouseButtonRelease, QPointF(20, 20), cp, Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier))
    app.processEvents()
    assert win._app_window_state == WINDOW_STATE_MAIN_VISIBLE
    assert not fc.isVisible()
    assert win.isVisible()

    # Cycle 2: minimize again -> restores at remembered dragged position
    win._enter_floating_companion_mode()
    app.processEvents()
    assert fc.isVisible()
    assert abs(fc.x() - saved_x) <= 2 and abs(fc.y() - saved_y) <= 2

    # Settings overlay updates size & flags
    win._restore_from_floating_companion()
    win._open_floating_settings()
    app.processEvents()
    ov = win._floating_settings_overlay
    assert isinstance(ov, FloatingCompanionSettingsOverlay) and ov.isVisible()
    ov._adjust_size(exact=220)
    ov._flip_toggle("start_minimized")
    ov._save()
    app.processEvents()
    persisted = config_manager.get_floating_companion_settings()
    assert persisted["size"] == 220 and persisted["start_minimized"] is True

    # Close cleanly transitions to APPLICATION_EXITING
    win.close()
    app.processEvents()
    assert win._app_window_state == WINDOW_STATE_EXITING
    assert not fc.isVisible()
"""
            env = dict(os.environ)
            env["QT_QPA_PLATFORM"] = "offscreen"
            proc = subprocess.run(
                [sys.executable, "-c", script],
                cwd=repo_root,
                env=env,
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr or proc.stdout)


if __name__ == "__main__":
    unittest.main()
