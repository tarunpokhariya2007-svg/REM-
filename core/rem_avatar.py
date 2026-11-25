"""REM portrait renderers.

``RemParticleAvatar()`` is a factory returning, in order of preference:

1. ``RemLayeredAvatar`` — pre-built transparent layers from
   ``gui/assets/rem/`` (base, eyelid frames, mouth keyframes, manifest.json),
   pre-filtered to the display size so the particle art is not aliased, and
   animated by ``core.avatar_anim.AvatarAnimator``.
2. ``LegacyParticleAvatar`` — the original single-image renderer, used only if
   the layered assets are missing/unreadable or ``REM_AVATAR_STYLE=legacy``.
"""
from __future__ import annotations

import math
import os
import random
from pathlib import Path

import numpy as np

import json
import time

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


class LegacyParticleAvatar:
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


# ═════════════════════════════════════════════════════════════════════════════
#  Layered renderer
# ═════════════════════════════════════════════════════════════════════════════
_LAYER_DIR = Path(__file__).resolve().parent.parent / "gui" / "assets" / "rem"


def _clamp01(v: float) -> float:
    return 0.0 if v < 0.0 else 1.0 if v > 1.0 else v


class RemLayeredAvatar:
    """Transparent layered portrait: base + eyelid frames + mouth keyframes.

    Honest limits: the head, hair and ribbon are one raster, so "head
    movement" is a whole-portrait tilt/bob of a fraction of a degree/pixel.
    Mouth shapes between the closed line and the open mouth are derived from
    the original open mouth; they are not hand-drawn phoneme art.
    """

    def __init__(self, layer_dir: Path | None = None) -> None:
        from core.avatar_anim import AvatarAnimator
        self._dir = Path(layer_dir) if layer_dir else _LAYER_DIR
        manifest_path = self._dir / "manifest.json"
        try:
            self._manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise RuntimeError(f"REM layer manifest unreadable: {manifest_path}: {exc}")
        canvas = self._manifest["canvas"]
        self._cw, self._ch = int(canvas["w"]), int(canvas["h"])
        self.SPAN = 2.0 * self._ch / max(self._cw, self._ch)
        self._src: dict[str, tuple[QImage, int, int]] = {}
        for name, meta in self._manifest["layers"].items():
            img = QImage(str(self._dir / meta["file"]))
            if img.isNull():
                raise RuntimeError(f"REM layer missing or unreadable: {meta['file']}")
            self._src[name] = (img.convertToFormat(QImage.Format.Format_ARGB32_Premultiplied),
                               int(meta["x"]), int(meta["y"]))
        for needed in ("rem-base", "rem-mouth-open", "rem-eyes-lid100"):
            if needed not in self._src:
                raise RuntimeError(f"REM layer set incomplete: {needed}")
        self._lid_levels = [float(v) for v in self._manifest.get("lid_levels", (0.35, 0.7, 1.0))]
        self._anim = AvatarAnimator()
        self._cache: dict[str, tuple[QImage, int, int]] = {}
        self._cache_w = 0
        self._cache_dpr = 1.0
        self._cache_t = 0.0
        self._sparkles = self._make_sparkles()
        self._debug_pose = ""

    # ── inputs from the HUD ──────────────────────────────────────────────────
    def glance(self, *_a, **_k) -> None:
        """Gaze cannot move without separate pupil art; kept for API parity."""

    def set_state(self, state: str) -> None:
        self._anim.set_state(state)

    def set_expression(self, name, hold: float = 6.0) -> None:
        self._anim.set_expression(name, hold)

    def reset_motion(self) -> None:
        self._anim.reset_motion()

    def step(self, dt: float, openness=None, width=None, level=None,
             speaking: bool = False, muted: bool = False, state=None, **_unused) -> None:
        # ``muted`` is the *microphone*; REM can still be speaking. Mouth is
        # gated by the playback-derived ``speaking`` flag only.
        self._anim.step(dt, openness=openness, width=width, level=level,
                        speaking=speaking, state=state)
        if _AVATAR_DEBUG:
            p = self._anim.pose
            tag = f"{self._anim.state}/{self._anim.expression}"
            if tag != self._debug_pose:
                self._debug_pose = tag
                print(f"[Avatar] state={tag} open={p.mouth_open:.2f} lid={p.lid:.2f}")

    # ── scaling cache ────────────────────────────────────────────────────────
    def _make_sparkles(self):
        base, _x, _y = self._src["rem-base"]
        rng = random.Random(7)
        pts = []
        w, h = base.width(), base.height()
        tries = 0
        while len(pts) < 80 and tries < 6000:
            tries += 1
            x, y = rng.randrange(w), rng.randrange(h)
            # keep the face clear: sparkles live on hair/ruffles, not on skin
            if ((x / w - 0.5) / 0.22) ** 2 + ((y / h - 0.52) / 0.24) ** 2 < 1.0:
                continue
            c = base.pixelColor(x, y)
            if c.alpha() < 240 or c.blue() < 200 or c.red() > 170:
                continue
            pts.append((x, y, c.red(), c.green(), c.blue(),
                        rng.random() * math.tau, 0.5 + rng.random() * 0.8))
        return pts

    def _ensure_cache(self, width_px: int, dpr: float) -> None:
        """(Re)build pre-filtered layers for the current device size.

        Downscaling the 1277 px dotted art with the painter's bilinear
        transform aliases badly (the noise visible in the old HUD).
        QImage.scaled(SmoothTransformation) averages properly. Rebuilds are
        rate-limited so dragging the window edge stays smooth.
        """
        width_px = max(64, int(width_px))
        if self._cache:
            drift = abs(width_px - self._cache_w) / float(self._cache_w)
            if drift < 0.04 and dpr == self._cache_dpr:
                return
            if time.monotonic() - self._cache_t < 0.12 and dpr == self._cache_dpr:
                return                       # reuse stale cache, scaled smoothly
        sx = width_px / float(self._cw)
        out: dict[str, tuple[QImage, int, int]] = {}
        for name, (img, x, y) in self._src.items():
            w = max(1, int(round(img.width() * sx)))
            h = max(1, int(round(img.height() * sx)))
            scaled = img.scaled(w, h, Qt.AspectRatioMode.IgnoreAspectRatio,
                                Qt.TransformationMode.SmoothTransformation)
            scaled = scaled.convertToFormat(QImage.Format.Format_ARGB32_Premultiplied)
            scaled.setDevicePixelRatio(dpr)
            out[name] = (scaled, int(round(x * sx)), int(round(y * sx)))
        self._cache, self._cache_w, self._cache_dpr = out, width_px, dpr
        self._cache_t = time.monotonic()

    # ── drawing ──────────────────────────────────────────────────────────────
    def _blit(self, p: QPainter, name: str, left: float, top: float,
              unit: float, opacity: float = 1.0) -> None:
        if opacity <= 0.004 or name not in self._cache:
            return
        img, x, y = self._cache[name]
        # ``unit`` converts cache device-pixels to logical pixels (1/dpr, and
        # the ratio between the cached width and the current width).
        p.setOpacity(opacity)
        p.drawImage(QRectF(left + x * unit, top + y * unit,
                           img.width() * unit, img.height() * unit), img)

    def _mouth_layers(self, o: float, w: float, smile: float):
        """[(layer name, opacity)] for the current mouth pose."""
        if o < 0.035:
            return [("rem-mouth-smile", smile)] if smile > 0.01 else []
        chain = [(0.0, None), (0.25, "rem-mouth-small"), (0.55, "rem-mouth-medium"), (1.0, "rem-mouth-open")]
        layers: list[tuple[str, float]] = []
        for (lo, a), (hi, b) in zip(chain, chain[1:]):
            if o <= hi:
                t = (o - lo) / (hi - lo)
                if a:
                    layers.append((a, 1.0))
                layers.append((b, t if a else _smooth(t)))
                break
        shape = 0.0
        shape_name = ""
        if w < -0.25:
            shape, shape_name = _clamp01((-w - 0.25) / 0.5), "rem-mouth-round"
        elif w > 0.25:
            shape, shape_name = _clamp01((w - 0.25) / 0.5), "rem-mouth-wide"
        shape *= _clamp01(o * 1.6)
        if shape_name and shape_name in self._cache and shape > 0.01:
            layers = [(n, a * (1.0 - shape)) for n, a in layers] + [(shape_name, shape)]
        return layers

    def paint(self, painter: QPainter, cx: float, cy: float, radius: float,
              primary: QColor, accent: QColor, bg: QColor) -> None:
        dpr = 1.0
        try:
            dpr = float(painter.device().devicePixelRatioF())
        except Exception:
            pass
        width_logical = 2.0 * radius
        self._ensure_cache(int(round(width_logical * dpr)), dpr)
        if not self._cache:
            return
        pose = self._anim.pose
        base_img, _bx, _by = self._cache["rem-base"]
        # logical size of one cached pixel, derived from the base layer
        unit = width_logical / float(base_img.width())
        left, top = cx - radius, cy - radius
        height = base_img.height() * unit

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

        # whole-portrait idle motion, anchored at the bottom centre (the neck/
        # shoulders stay planted; the head moves most)
        ax, ay = cx, top + height
        painter.translate(ax, ay + pose.bob * height)
        painter.rotate(pose.tilt)
        painter.scale(pose.scale, pose.scale)
        painter.translate(-ax, -ay)

        self._blit(painter, "rem-base", left, top, unit)

        # eyelids: 0 = open (no layer), then lid035 -> lid070 -> lid100
        lid = pose.lid
        if lid > 0.02:
            names = ["rem-eyes-lid%03d" % int(round(v * 100)) for v in self._lid_levels]
            levels = [0.0] + self._lid_levels
            for i in range(1, len(levels)):
                if lid <= levels[i]:
                    t = (lid - levels[i - 1]) / (levels[i] - levels[i - 1])
                    if i > 1:
                        self._blit(painter, names[i - 2], left, top, unit, 1.0)
                        self._blit(painter, names[i - 1], left, top, unit, t)
                    else:
                        self._blit(painter, names[0], left, top, unit, t)
                    break

        for name, alpha in self._mouth_layers(pose.mouth_open, pose.mouth_width, pose.smile):
            self._blit(painter, name, left, top, unit, alpha)
        painter.setOpacity(1.0)

        # sparse shimmer on hair/ruffles only — never on the face
        sx = width_logical / float(self._cw)
        for x, y, r, g, b, phase, speed in self._sparkles:
            shimmer = 0.5 + 0.5 * math.sin(pose.shimmer * 1.6 * speed + phase)
            if shimmer < 0.25:
                continue
            painter.setPen(QPen(QColor(min(255, r + 70), min(255, g + 70), 255, int(60 + 160 * shimmer)),
                                max(0.8, sx * 2.6), Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            painter.drawPoint(QPointF(left + x * sx, top + y * sx))
        painter.restore()


def _smooth(v: float) -> float:
    v = _clamp01(v)
    return v * v * (3.0 - 2.0 * v)


def RemParticleAvatar():
    """Factory kept under the historical name used by gui/theme.py and hud.py."""
    if os.environ.get("REM_AVATAR_STYLE", "").lower() != "legacy":
        try:
            return RemLayeredAvatar()
        except Exception as exc:     # cosmetics must never take the HUD down
            print(f"[Avatar] layered renderer unavailable ({exc}); using legacy particle portrait")
    return LegacyParticleAvatar()
