"""gui/floating_companion.py — Floating Chibi REM Desktop Companion & Settings.

Displays the user-supplied full-body chibi REM artwork in a small, movable,
transparent always-on-top desktop companion window when the main REM dashboard
is minimized, and restores the original MainWindow when clicked.
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable

from PyQt6.QtCore import QPoint, QRect, Qt, pyqtSignal
from PyQt6.QtGui import QAction, QCursor, QFont, QGuiApplication, QMouseEvent, QPixmap
from PyQt6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMenu,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from gui.style import hud_font, px_font
from gui.theme import C

WINDOW_STATE_MAIN_VISIBLE = "MAIN_VISIBLE"
WINDOW_STATE_FLOATING_VISIBLE = "FLOATING_AVATAR_VISIBLE"
WINDOW_STATE_EXITING = "APPLICATION_EXITING"

_ASSETS_DIR = Path(__file__).resolve().parent / "assets" / "rem"
CHIBI_ASSET_PATH = _ASSETS_DIR / "floating_chibi.png"


def compute_companion_size(config_height: int, screen_rect: QRect | None) -> int:
    """Return balanced character height in logical px, scaled softly to screen size."""
    base = max(120, min(320, int(config_height or 190)))
    if screen_rect is None or screen_rect.height() <= 0:
        return base
    # Keep 1080p as 1.0x reference; shrink modestly on 768p laptops, grow slightly on 4K
    scale = max(0.78, min(1.28, screen_rect.height() / 1080.0))
    scaled = int(round(base * scale))
    max_fit = max(120, int(screen_rect.height() * 0.45))
    return max(120, min(max_fit, scaled))


def clamp_to_usable_screens(
    x: int,
    y: int,
    w: int,
    h: int,
    screens_rects: list[QRect],
    primary_rect: QRect,
) -> tuple[int, int]:
    """Clamp `(x, y, w, h)` to the closest usable monitor work-area (no off-screen loss)."""
    rects = [r for r in screens_rects if r.isValid() and r.width() > 0 and r.height() > 0]
    if not rects:
        rects = [primary_rect]
    cx, cy = x + w // 2, y + h // 2
    target = rects[0]
    best_dist = float("inf")
    for r in rects:
        nx = max(r.left(), min(cx, r.right()))
        ny = max(r.top(), min(cy, r.bottom()))
        dist = (cx - nx) ** 2 + (cy - ny) ** 2
        if dist < best_dist:
            best_dist = dist
            target = r
    min_x = target.left() + 4
    max_x = max(min_x, target.right() - w - 4)
    min_y = target.top() + 4
    max_y = max(min_y, target.bottom() - h - 4)
    return max(min_x, min(x, max_x)), max(min_y, min(y, max_y))


def default_bottom_right_position(w: int, h: int, primary_rect: QRect, margin: int = 20) -> tuple[int, int]:
    """Initial anchor near the bottom-right corner of the primary usable desktop area."""
    if not primary_rect.isValid():
        return (100, 100)
    x = primary_rect.right() - w - margin + 1
    y = primary_rect.bottom() - h - margin + 1
    return clamp_to_usable_screens(x, y, w, h, [primary_rect], primary_rect)


class FloatingCompanionWindow(QWidget):
    """Movable transparent frameless widget showing the supplied chibi REM artwork."""

    restore_requested = pyqtSignal()
    temp_hide_requested = pyqtSignal()
    exit_requested = pyqtSignal()
    position_committed = pyqtSignal(int, int)

    def __init__(self, asset_path: Path | str | None = None, parent: QWidget | None = None):
        super().__init__(parent)
        self._asset_path = Path(asset_path) if asset_path else CHIBI_ASSET_PATH
        self._base_pixmap = QPixmap(str(self._asset_path)) if self._asset_path.exists() else QPixmap()
        self._target_height = 190
        self._always_on_top = True
        self._remember_position = True
        self._dragging = False
        self._moved_during_press = False
        self._press_global = QPoint()
        self._drag_offset = QPoint()
        self._expression = "NEUTRAL"

        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        self.setToolTip("REM — Click to restore dashboard, drag to move")
        self._apply_window_flags()

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        self._image_label = QLabel(self)
        self._image_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._image_label.setStyleSheet("background: transparent; border: none;")
        lay.addWidget(self._image_label)

        self._connect_screen_signals()
        self.refresh_pixmap()

    def _connect_screen_signals(self) -> None:
        app = QGuiApplication.instance()
        if app is None:
            return
        try:
            app.screenAdded.connect(lambda _s: self.ensure_on_screen())
            app.screenRemoved.connect(lambda _s: self.ensure_on_screen())
            for scr in app.screens():
                scr.availableGeometryChanged.connect(lambda _g: self.ensure_on_screen())
        except Exception:
            pass

    def _apply_window_flags(self) -> None:
        was_visible = self.isVisible()
        flags = (
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.Tool
        )
        if self._always_on_top:
            flags |= Qt.WindowType.WindowStaysOnTopHint
        self.setWindowFlags(flags)
        if was_visible:
            self.show()

    def _screen_rects(self) -> tuple[list[QRect], QRect]:
        app = QGuiApplication.instance()
        if app is None:
            fallback = QRect(0, 0, 1920, 1080)
            return [fallback], fallback
        primary = app.primaryScreen()
        p_rect = primary.availableGeometry() if primary is not None else QRect(0, 0, 1920, 1080)
        all_rects = [s.availableGeometry() for s in app.screens()] or [p_rect]
        return all_rects, p_rect

    def apply_settings(self, cfg: dict) -> None:
        self._target_height = int(cfg.get("size", 190) or 190)
        self._remember_position = bool(cfg.get("remember_position", True))
        aot = bool(cfg.get("always_on_top", True))
        if aot != self._always_on_top:
            self._always_on_top = aot
            self._apply_window_flags()
        self.refresh_pixmap()
        if self.isVisible():
            self.ensure_on_screen()

    def set_expression_hint(self, expression: str) -> None:
        """Store current REM expression cleanly without altering the supplied artwork."""
        self._expression = str(expression or "NEUTRAL").upper()

    def refresh_pixmap(self) -> None:
        _all, primary = self._screen_rects()
        display_h = compute_companion_size(self._target_height, primary)
        if self._base_pixmap.isNull():
            display_w = max(90, int(display_h * 0.6))
            self._image_label.setText("REM")
            self.setFixedSize(display_w, display_h)
            return
        dpr = max(1.0, float(self.devicePixelRatioF() or 1.0))
        ratio = self._base_pixmap.width() / max(1, self._base_pixmap.height())
        display_w = max(80, int(round(display_h * ratio)))
        scaled = self._base_pixmap.scaled(
            int(round(display_w * dpr)),
            int(round(display_h * dpr)),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        scaled.setDevicePixelRatio(dpr)
        self._image_label.setPixmap(scaled)
        self._image_label.setFixedSize(display_w, display_h)
        self.setFixedSize(display_w, display_h)

    def position_for_show(self, cfg: dict) -> None:
        self.apply_settings(cfg)
        all_rects, primary = self._screen_rects()
        w, h = self.width(), self.height()
        last_x = cfg.get("last_x") if cfg.get("remember_position", True) else None
        last_y = cfg.get("last_y") if cfg.get("remember_position", True) else None
        if last_x is not None and last_y is not None:
            nx, ny = clamp_to_usable_screens(int(last_x), int(last_y), w, h, all_rects, primary)
        else:
            nx, ny = default_bottom_right_position(w, h, primary)
        self.move(nx, ny)

    def ensure_on_screen(self) -> None:
        all_rects, primary = self._screen_rects()
        nx, ny = clamp_to_usable_screens(self.x(), self.y(), self.width(), self.height(), all_rects, primary)
        if (nx, ny) != (self.x(), self.y()):
            self.move(nx, ny)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            gpos = event.globalPosition().toPoint()
            self._dragging = True
            self._moved_during_press = False
            self._press_global = gpos
            self._drag_offset = gpos - self.frameGeometry().topLeft()
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._dragging and (event.buttons() & Qt.MouseButton.LeftButton):
            gpos = event.globalPosition().toPoint()
            if not self._moved_during_press:
                threshold = max(5, QApplication.startDragDistance())
                if (gpos - self._press_global).manhattanLength() >= threshold:
                    self._moved_during_press = True
            if self._moved_during_press:
                target = gpos - self._drag_offset
                all_rects, primary = self._screen_rects()
                nx, ny = clamp_to_usable_screens(
                    target.x(), target.y(), self.width(), self.height(), all_rects, primary
                )
                self.move(nx, ny)
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self._dragging:
            was_drag = self._moved_during_press
            self._dragging = False
            self._moved_during_press = False
            self.setCursor(Qt.CursorShape.OpenHandCursor)
            if was_drag:
                if self._remember_position:
                    self.position_committed.emit(self.x(), self.y())
            else:
                self.restore_requested.emit()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def contextMenuEvent(self, event) -> None:
        menu = QMenu(self)
        menu.setFont(hud_font(9))
        menu.setStyleSheet(
            f"QMenu {{ background: {C.BG}; color: {C.TEXT}; border: 1px solid {C.BORDER_B}; padding: 4px; }}"
            f"QMenu::item {{ padding: 6px 16px; border-radius: 3px; }}"
            f"QMenu::item:selected {{ background: {C.PRI_GHO}; color: {C.PRI}; }}"
        )
        act_restore = QAction("Restore REM", menu)
        act_restore.triggered.connect(self.restore_requested.emit)
        act_hide = QAction("Hide floating avatar temporarily", menu)
        act_hide.triggered.connect(self.temp_hide_requested.emit)
        act_exit = QAction("Exit REM", menu)
        act_exit.triggered.connect(self.exit_requested.emit)
        menu.addAction(act_restore)
        menu.addAction(act_hide)
        menu.addSeparator()
        menu.addAction(act_exit)
        menu.exec(QCursor.pos())


class FloatingCompanionSettingsOverlay(QWidget):
    """Modal overlay inside MainWindow for Floating Companion & Windows Startup settings."""

    saved = pyqtSignal(dict)
    _OW, _OH = 430, 500

    def __init__(
        self,
        cfg: dict,
        autostart_enabled: bool,
        on_toggle_autostart: Callable[[bool], bool] | None = None,
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self._cfg = dict(cfg)
        self._autostart_on = bool(autostart_enabled)
        self._on_toggle_autostart = on_toggle_autostart
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(
            f"FloatingCompanionSettingsOverlay {{ background: rgba(0, 6, 10, 245); "
            f"border: 1px solid {C.BORDER_B}; border-radius: 6px; }}"
        )
        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 18, 24, 18)
        lay.setSpacing(8)

        def _lbl(txt, fs=9, bold=False, color=C.PRI, align=Qt.AlignmentFlag.AlignLeft):
            w = QLabel(txt)
            w.setAlignment(align)
            w.setFont(hud_font(fs, QFont.Weight.Bold if bold else QFont.Weight.Normal))
            w.setStyleSheet(f"color: {color}; background: transparent;")
            return w

        def _sep():
            s = QFrame()
            s.setFrameShape(QFrame.Shape.HLine)
            s.setStyleSheet(f"color: {C.BORDER}; margin: 2px 0;")
            return s

        lay.addWidget(_lbl("FLOATING COMPANION", 12, True, align=Qt.AlignmentFlag.AlignCenter))
        lay.addWidget(_sep())

        self._toggle_btns: dict[str, QPushButton] = {}

        def _make_toggle(key: str, label: str, initial: bool):
            b = QPushButton()
            b.setFixedHeight(30)
            b.setFont(px_font(10, QFont.Weight.DemiBold, spacing=1.0))
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.setProperty("_label", label)
            b.setProperty("_checked", bool(initial))
            b.clicked.connect(lambda _=False, k=key: self._flip_toggle(k))
            self._toggle_btns[key] = b
            self._refresh_toggle(b)
            lay.addWidget(b)

        _make_toggle("enabled", "ENABLE FLOATING AVATAR", self._cfg.get("enabled", True))
        _make_toggle("show_on_minimize", "SHOW AVATAR WHEN MINIMIZED", self._cfg.get("show_on_minimize", True))
        _make_toggle("always_on_top", "ALWAYS ON TOP", self._cfg.get("always_on_top", True))
        _make_toggle("remember_position", "REMEMBER LAST POSITION", self._cfg.get("remember_position", True))

        # Size selector row
        size_row = QHBoxLayout()
        size_row.setSpacing(6)
        size_row.addWidget(_lbl("AVATAR SIZE (PX)", 8, color=C.TEXT_DIM))
        size_row.addStretch()
        self._size_val = int(self._cfg.get("size", 190) or 190)
        self._size_lbl = _lbl(f"{self._size_val} px", 9, True, color=C.TEXT, align=Qt.AlignmentFlag.AlignCenter)
        self._size_lbl.setFixedWidth(56)

        def _sbtn(text: str, delta: int | None = None, exact: int | None = None):
            btn = QPushButton(text)
            btn.setFixedSize(40, 26)
            btn.setFont(hud_font(8, QFont.Weight.Bold))
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setStyleSheet(
                f"QPushButton {{ background: transparent; color: {C.TEXT_MED}; "
                f"border: 1px solid {C.BORDER}; border-radius: 3px; }}"
                f"QPushButton:hover {{ color: {C.PRI}; border-color: {C.PRI}; }}"
            )
            btn.clicked.connect(lambda: self._adjust_size(delta=delta, exact=exact))
            return btn

        size_row.addWidget(_sbtn("-", delta=-10))
        size_row.addWidget(self._size_lbl)
        size_row.addWidget(_sbtn("+", delta=10))
        size_row.addWidget(_sbtn("160", exact=160))
        size_row.addWidget(_sbtn("190", exact=190))
        size_row.addWidget(_sbtn("220", exact=220))
        lay.addLayout(size_row)

        lay.addSpacing(4)
        lay.addWidget(_lbl("WINDOWS STARTUP", 10, True, color=C.PRI))
        lay.addWidget(_sep())
        _make_toggle("autostart", "START REM WITH WINDOWS", self._autostart_on)
        _make_toggle("start_minimized", "RESTORE FLOATING COMPANION ON STARTUP", self._cfg.get("start_minimized", False))

        lay.addStretch()
        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)
        save_btn = QPushButton("▸  APPLY && SAVE")
        save_btn.setFixedHeight(34)
        save_btn.setFont(hud_font(9, QFont.Weight.Bold))
        save_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        save_btn.setStyleSheet(
            f"QPushButton {{ background: transparent; color: {C.PRI}; "
            f"border: 1px solid {C.PRI_DIM}; border-radius: 3px; }}"
            f"QPushButton:hover {{ background: {C.PRI_GHO}; border: 1px solid {C.PRI}; }}"
        )
        save_btn.clicked.connect(self._save)
        btn_row.addWidget(save_btn)

        close_btn = QPushButton("CANCEL")
        close_btn.setFixedHeight(34)
        close_btn.setFont(hud_font(9))
        close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        close_btn.setStyleSheet(
            f"QPushButton {{ background: transparent; color: {C.TEXT_MED}; "
            f"border: 1px solid {C.BORDER}; border-radius: 3px; }}"
            f"QPushButton:hover {{ color: {C.TEXT}; border-color: {C.BORDER_B}; }}"
        )
        close_btn.clicked.connect(self.hide)
        btn_row.addWidget(close_btn)
        lay.addLayout(btn_row)

    def _refresh_toggle(self, btn: QPushButton) -> None:
        label = btn.property("_label")
        checked = bool(btn.property("_checked"))
        btn.setText(f"{label}: {'ON' if checked else 'OFF'}")
        if checked:
            btn.setStyleSheet(
                f"QPushButton {{ background: {C.PRI_GHO}; color: {C.PRI}; "
                f"border: 1px solid {C.PRI}; border-radius: 3px; text-align: left; padding: 0 10px; }}"
            )
        else:
            btn.setStyleSheet(
                f"QPushButton {{ background: transparent; color: {C.TEXT_DIM}; "
                f"border: 1px solid {C.BORDER}; border-radius: 3px; text-align: left; padding: 0 10px; }}"
            )

    def _flip_toggle(self, key: str) -> None:
        btn = self._toggle_btns[key]
        btn.setProperty("_checked", not bool(btn.property("_checked")))
        self._refresh_toggle(btn)

    def _adjust_size(self, delta: int | None = None, exact: int | None = None) -> None:
        if exact is not None:
            self._size_val = max(120, min(320, int(exact)))
        elif delta is not None:
            self._size_val = max(120, min(320, self._size_val + int(delta)))
        self._size_lbl.setText(f"{self._size_val} px")

    def _save(self) -> None:
        want_auto = bool(self._toggle_btns["autostart"].property("_checked"))
        if want_auto != self._autostart_on and self._on_toggle_autostart is not None:
            self._autostart_on = bool(self._on_toggle_autostart(want_auto))
        out = {
            "enabled": bool(self._toggle_btns["enabled"].property("_checked")),
            "show_on_minimize": bool(self._toggle_btns["show_on_minimize"].property("_checked")),
            "always_on_top": bool(self._toggle_btns["always_on_top"].property("_checked")),
            "remember_position": bool(self._toggle_btns["remember_position"].property("_checked")),
            "start_minimized": bool(self._toggle_btns["start_minimized"].property("_checked")),
            "size": self._size_val,
        }
        self.saved.emit(out)
        self.hide()
