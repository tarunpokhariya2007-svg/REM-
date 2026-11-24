"""Particle portrait renderer with local audio-synchronised facial layers."""
from __future__ import annotations

import math
import os
import random
from pathlib import Path

import numpy as np

from PyQt6.QtCore import QPointF, QRectF, Qt
from PyQt6.QtGui import (
    QColor, QImage, QPainter, QPainterPath, QPen, QPolygonF, QRadialGradient,
)


_ASSET = Path(__file__).resolve().parent.parent / "gui" / "assets" / "rem_particle_portrait.png"
_EYES = ((0.397, 0.466, 0.038, 0.023), (0.617, 0.466, 0.038, 0.023))
_MOUTH = (0.502, 0.595, 0.078, 0.027)
_MOUTH_RESTORE = (0.502, 0.595, 0.071, 0.023)
_AVATAR_DEBUG = os.environ.get("REM_AVATAR_DEBUG", "").lower() in {
    "1", "true", "yes", "on",
}


def _ellipse_mask(width: int, height: int,
                  region: tuple[float, float, float, float]) -> np.ndarray:
    cx, cy, rx, ry = region
    yy, xx = np.ogrid[:height, :width]
    return (((xx / width - cx) / rx) ** 2 + ((yy / height - cy) / ry) ** 2) <= 1.0


def _qimage_from_rgba(pixels: np.ndarray) -> QImage:
    h, w = pixels.shape[:2]
    return QImage(pixels.data, w, h, w * 4,
                  QImage.Format.Format_RGBA8888).copy()


class RemParticleAvatar:
    """Keep the supplied portrait as the base and animate only aligned layers.

    ``openness`` and ``width`` come from 20 ms analysis of the PCM being sent
    to the speaker, fused with the generated transcript when available. The
    face remains visible throughout; there is no scene or video replacement.
    """

    SPAN = 2.0

    def __init__(self) -> None:
        source = QImage(str(_ASSET))
        if source.isNull():
            raise RuntimeError(f"REM portrait asset is missing or unreadable: {_ASSET}")
        self._source = source.convertToFormat(QImage.Format.Format_ARGB32)
        self._w, self._h = self._source.width(), self._source.height()
        self._base, self._mouth_art, self._eye_cover, self._sparkles = self._build_layers()
        self._time = 0.0
        self._mouth_open = 0.0
        self._mouth_width = 0.0
        self._speech_mix = 0.0
        self._blink_clock = 0.0
        self._blink_phase: float | None = None
        self._blink = 0.0
        self._next_blink = random.uniform(3.2, 6.4)
        self._debug_pose = "REST"

    def _build_layers(self):
        rgba_source = self._source.convertToFormat(QImage.Format.Format_RGBA8888)
        bits = rgba_source.bits()
        bits.setsize(rgba_source.sizeInBytes())
        pixels = np.frombuffer(bits, dtype=np.uint8).reshape(
            self._h, rgba_source.bytesPerLine())[:, :self._w * 4]
        pixels = pixels.reshape(self._h, self._w, 4).copy()
        rgb = pixels[:, :, :3].astype(np.uint16)
        chroma = rgb.max(axis=2) - rgb.min(axis=2)
        background = ((255 - rgb.min(axis=2)) < 12) & (chroma < 18)
        pixels[background, 3] = 0
        pixels[:, :, :3] = np.minimum(255, rgb * 110 // 100 + 8).astype(np.uint8)

        yy, xx = np.indices((self._h, self._w))
        face_region = ((xx / self._w > 0.37) & (xx / self._w < 0.63)
                       & (yy / self._h > 0.53) & (yy / self._h < 0.67))
        skin_samples = pixels[face_region & (pixels[:, :, 3] > 0), :3]
        if len(skin_samples):
            skin_samples = skin_samples[(skin_samples.min(axis=1) > 90)
                                        & (skin_samples.max(axis=1) < 255)]
        if not len(skin_samples):
            skin_samples = np.array([[190, 218, 250], [245, 250, 255],
                                     [228, 172, 211]], dtype=np.uint8)
        palette = [tuple(int(v) for v in row) for row in skin_samples[::max(1, len(skin_samples) // 96)][:96]]
        # The source is strongly blue-lit; a fixed local lavender skin tint
        # avoids a blue-black cavity when the open mouth is covered at rest.
        skin_color = QColor(190, 166, 218)

        # Keep the source mouth as the wide-open pose. Restore its area with
        # neighboring particle skin so the idle pose can be closed cleanly.
        mouth_mask = _ellipse_mask(self._w, self._h, _MOUTH)
        mouth_art = pixels.copy()
        mouth_art[~mouth_mask] = 0
        mx, my, mrx, mry = _MOUTH
        x0, x1 = max(0, int((mx - mrx) * self._w)), min(self._w, int((mx + mrx) * self._w) + 1)
        y0, y1 = max(0, int((my - mry) * self._h)), min(self._h, int((my + mry) * self._h) + 1)
        mouth_art = mouth_art[y0:y1, x0:x1].copy()
        restore_mask = _ellipse_mask(self._w, self._h, _MOUTH_RESTORE)
        pixels[restore_mask] = (0, 0, 0, 0)
        base = _qimage_from_rgba(pixels)
        base_painter = QPainter(base)
        mouth_skin = QColor(skin_color)
        mouth_skin.setAlpha(230)
        self._draw_particle_patch(base_painter, _MOUTH_RESTORE, palette,
                                  mouth_skin, feather=True)
        base_painter.end()

        # Blink overlay only covers the two eye ovals, using matching skin tint
        # and fine particles sampled from the portrait's face palette.
        eye_cover = QImage(self._w, self._h, QImage.Format.Format_ARGB32_Premultiplied)
        eye_cover.fill(Qt.GlobalColor.transparent)
        eye_painter = QPainter(eye_cover)
        for eye in _EYES:
            self._draw_particle_patch(eye_painter, eye, palette, skin_color)
        eye_painter.end()

        # Full-resolution source pixels preserve the fine particle field. A
        # small subset outside the face receives the original demo's drift.
        face = (((xx / self._w - 0.5) / 0.205) ** 2
                + ((yy / self._h - 0.525) / 0.225) ** 2) < 1.0
        cy, cx = np.nonzero((~background) & (chroma > 24) & (~face)
                            & ((xx * 7 + yy * 13) % 11 == 0))
        chosen = random.sample(range(len(cx)), min(360, len(cx)))
        sparkles = []
        for index in chosen:
            x, y = int(cx[index]), int(cy[index])
            r, g, b = (int(value) for value in rgb[y, x])
            sparkles.append((x, y, r, g, b, random.random() * math.tau,
                             0.65 + random.random() * 0.7))
        return (base, _qimage_from_rgba(mouth_art), eye_cover, sparkles)

    def _draw_particle_patch(self, painter: QPainter, region: tuple[float, ...],
                             palette: list[tuple[int, int, int]],
                             fill: QColor | None = None,
                             feather: bool = False) -> None:
        cx, cy, rx, ry = region
        x0, x1 = int((cx - rx) * self._w), int((cx + rx) * self._w)
        y0, y1 = int((cy - ry) * self._h), int((cy + ry) * self._h)
        rect = QRectF(x0, y0, x1 - x0, y1 - y0)
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        path = QPainterPath()
        path.addEllipse(rect)
        painter.setClipPath(path)
        if fill is not None:
            if feather:
                painter.save()
                painter.translate(rect.center())
                painter.scale(rect.width() / 2.0, rect.height() / 2.0)
                gradient = QRadialGradient(QPointF(0, 0), 1.0)
                gradient.setColorAt(0.0, fill)
                edge = QColor(fill)
                edge.setAlpha(0)
                gradient.setColorAt(1.0, edge)
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(gradient)
                painter.drawEllipse(QRectF(-1, -1, 2, 2))
                painter.restore()
            else:
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(fill)
                painter.drawEllipse(rect)
        radius = 2.2
        for y in range(y0 + 3, y1, 5):
            for x in range(x0 + 3, x1, 5):
                r, g, b = palette[(x * 17 + y * 31) % len(palette)]
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(QColor(r, g, b, 235))
                painter.drawEllipse(QPointF(x, y), radius, radius)
        painter.restore()

    def glance(self, *_args, **_kwargs) -> None:
        """Compatibility hook for callers; gaze remains aligned to the art."""

    def reset_motion(self) -> None:
        self._mouth_open = 0.0
        self._mouth_width = 0.0
        self._speech_mix = 0.0
        self._blink = 0.0
        self._blink_phase = None

    def step(self, dt: float, openness: float | None = None,
             width: float | None = None, level: float | None = None,
             speaking: bool = False, muted: bool = False, **_unused) -> None:
        dt = max(0.0, min(0.1, float(dt)))
        self._time += dt
        try:
            level = max(0.0, min(1.0, float(level))) if level is not None else 0.0
            raw_open = max(0.0, min(1.0, float(openness))) if openness is not None else None
            raw_width = max(-1.0, min(1.0, float(width))) if width is not None else 0.0
        except (TypeError, ValueError):
            level, raw_open, raw_width = 0.0, None, 0.0

        active = bool(speaking and not muted)
        mix_tau = 0.115 if active else 0.145  # ~350 ms in, ~440 ms out to 95%
        previous_mix = self._speech_mix
        self._speech_mix += (float(active) - self._speech_mix) * (1.0 - math.exp(-dt / mix_tau))
        if _AVATAR_DEBUG:
            if active and previous_mix <= 0.01 and self._speech_mix > 0.01:
                print("[Avatar] transition=IDLE->SPEAKING started")
            elif active and previous_mix < 0.99 <= self._speech_mix:
                print("[Avatar] transition=IDLE->SPEAKING complete")
            elif not active and previous_mix > 0.99 >= self._speech_mix:
                print("[Avatar] transition=SPEAKING->IDLE complete")
        if raw_open is None:
            raw_open = min(0.82, 0.86 * math.sqrt(level))
        if level <= 0.005:
            raw_open = 0.0
        target_open = raw_open * self._speech_mix
        open_tau = 0.055 if target_open > self._mouth_open else 0.095
        self._mouth_open += (target_open - self._mouth_open) * (1.0 - math.exp(-dt / open_tau))
        self._mouth_width += (raw_width - self._mouth_width) * (1.0 - math.exp(-dt / 0.07))
        if self._mouth_open < 0.015:
            self._mouth_open = 0.0

        # Independent, variable-interval blinks. The portrait is loaded before
        # the timer starts, so this layer is only evaluated with valid artwork.
        self._blink_clock += dt
        if self._blink_phase is None and self._blink_clock >= self._next_blink:
            self._blink_phase = 0.0
            self._blink_clock = 0.0
        if self._blink_phase is not None:
            self._blink_phase += dt
            phase = self._blink_phase
            close_t, hold_t, open_t = 0.075, 0.035, 0.105
            if phase < close_t:
                self._blink = self._smoothstep(phase / close_t)
            elif phase < close_t + hold_t:
                self._blink = 1.0
            elif phase < close_t + hold_t + open_t:
                self._blink = 1.0 - self._smoothstep((phase - close_t - hold_t) / open_t)
            else:
                self._blink = 0.0
                self._blink_phase = None
                self._next_blink = random.uniform(3.4, 6.8) if not active else random.uniform(4.5, 8.5)

        if _AVATAR_DEBUG:
            pose = self._pose_name()
            if pose != self._debug_pose:
                self._debug_pose = pose
                print(f"[Avatar] viseme={pose} openness={self._mouth_open:.2f} "
                      f"width={self._mouth_width:.2f} blink={self._blink:.2f}")

    @staticmethod
    def _smoothstep(value: float) -> float:
        value = max(0.0, min(1.0, value))
        return value * value * (3.0 - 2.0 * value)

    def _pose_name(self) -> str:
        if self._mouth_open < 0.08:
            return "MBP/REST"
        if self._mouth_width < -0.3:
            return "ROUNDED"
        if self._mouth_width > 0.3:
            return "SPREAD"
        if self._mouth_open > 0.66:
            return "OPEN"
        return "TEETH/MEDIUM"

    def paint(self, painter: QPainter, cx: float, cy: float, radius: float,
              primary: QColor, accent: QColor, bg: QColor) -> None:
        scale = (2.0 * radius) / max(self._w, self._h)
        dest = QRectF(cx - radius, cy - radius,
                      self._w * scale, self._h * scale)
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        halo = QRadialGradient(QPointF(cx, cy), radius * 0.95)
        glow = QColor(primary)
        glow.setAlpha(10)
        halo.setColorAt(0.0, glow)
        glow.setAlpha(0)
        halo.setColorAt(1.0, glow)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(halo)
        painter.drawEllipse(QPointF(cx, cy), radius * 1.08, radius * 1.08)
        painter.drawImage(dest, self._base)

        blink = self._blink
        if blink > 0.001:
            painter.save()
            painter.setOpacity(blink)
            painter.drawImage(dest, self._eye_cover)
            painter.restore()
            for ex, ey, erx, _ery in _EYES:
                x = dest.left() + ex * self._w * scale
                y = dest.top() + ey * self._h * scale
                half = erx * self._w * scale * 0.80
                lid = QPainterPath(QPointF(x - half, y))
                lid.cubicTo(QPointF(x - half * 0.45, y + 3.0 * scale),
                            QPointF(x + half * 0.45, y + 3.0 * scale),
                            QPointF(x + half, y))
                color = QColor(78, 190, 246, int(235 * blink))
                painter.setPen(QPen(color, max(0.8, scale * 3.0),
                                    Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawPath(lid)

        amount = self._mouth_open
        spread = self._mouth_width
        mx, my, rx, ry = _MOUTH
        center_x = dest.left() + mx * self._w * scale
        center_y = dest.top() + my * self._h * scale
        base_w, base_h = 2.0 * rx * self._w * scale, 2.0 * ry * self._h * scale
        if amount < 0.09:
            half = base_w * (0.31 + 0.05 * spread)
            smile = QPainterPath(QPointF(center_x - half, center_y))
            smile.cubicTo(QPointF(center_x - half * 0.42, center_y + max(1.0, base_h * 0.13)),
                          QPointF(center_x + half * 0.42, center_y + max(1.0, base_h * 0.13)),
                          QPointF(center_x + half, center_y))
            painter.setPen(QPen(QColor(232, 157, 203, 225),
                                max(1.2, scale * 3.0), Qt.PenStyle.SolidLine,
                                Qt.PenCapStyle.RoundCap))
            painter.drawPath(smile)
            for dot in range(7):
                x = center_x + (dot - 3) * half / 3.2
                y = center_y + max(1.0, base_h * 0.13) * (1.0 - abs(dot - 3) / 3.0)
                painter.setPen(QPen(QColor(248, 193, 225, 210),
                                    max(0.7, scale * 1.7),
                                    Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
                painter.drawPoint(QPointF(x, y))
        else:
            width_scale = max(0.76, min(1.26, 1.0 + spread * 0.25))
            height_scale = 0.18 + 0.82 * amount
            target = QRectF(center_x - base_w * width_scale / 2.0,
                            center_y - base_h * height_scale / 2.0,
                            base_w * width_scale, base_h * height_scale)
            painter.drawImage(target, self._mouth_art)
            # The original pink/dark mouth layer supplies the shape; a fine
            # particle-like tooth glint differentiates mid-open articulation.
            if 0.24 <= amount <= 0.66 and spread > -0.35:
                points = QPolygonF()
                for i in range(7):
                    x = center_x + (i - 3) * max(0.7, base_w * width_scale / 11.0)
                    y = center_y - base_h * height_scale * 0.13
                    points.append(QPointF(x, y))
                painter.setPen(QPen(QColor(224, 250, 255, 220),
                                    max(0.8, scale * 1.8),
                                    Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
                painter.drawPoints(points)

        for x, y, r, g, b, phase, speed in self._sparkles:
            t = self._time * speed + phase
            px = dest.left() + (x + math.sin(t) * 1.15) * scale
            py = dest.top() + (y + math.cos(t * 0.8) * 1.15) * scale
            shimmer = 0.55 + 0.45 * (math.sin(self._time * 2.0 + phase) + 1.0) * 0.5
            painter.setPen(QPen(QColor(r, g, b, int(255 * shimmer)),
                                max(0.7, scale * 2.4), Qt.PenStyle.SolidLine,
                                Qt.PenCapStyle.RoundCap))
            painter.drawPoint(QPointF(px, py))
        painter.restore()
