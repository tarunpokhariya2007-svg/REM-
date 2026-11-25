"""Build aligned transparent RGBA expression layers for core/rem_avatar.py.

Uses the exact 1277x1232 canvas geometry, sampled skin/line palette, and
hair-strand masking from ``tools/build_rem_layers.py`` so every eye, eyebrow,
mouth, blush, tear, and decorative effect layer aligns cleanly onto
``gui/assets/rem/rem-base.png`` without altering the original artwork.

Run from the repository root (Pillow + numpy):
    python tools/build_rem_expression_layers.py
"""
from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

from tools.build_rem_layers import (
    CANVAS,
    EYES,
    LASH_DST,
    LASH_SRC,
    MOUTH_CLOSED_C,
    MOUTH_OPEN_C,
    STRANDS,
    crop_layer,
    dotted_fill,
    ell_mask,
    poly_mask,
    skin_colour,
    strand_patch,
)

OUT_DIR = Path(__file__).resolve().parent.parent / "gui" / "assets" / "rem"
SRC_OPEN = OUT_DIR / "source" / "rem-source-open.png"
SRC_CLOSED = OUT_DIR / "source" / "rem-source-closed.png"


def _sample_palettes(open_arr: np.ndarray, closed_arr: np.ndarray):
    """Sample skin and line colours from the original REM drawings."""
    near = np.zeros(open_arr.shape[:2], bool)
    near[735:812, 560:600] = True
    near[735:812, 705:745] = True
    mouth_skin = skin_colour(open_arr, near)

    seg = closed_arr[
        MOUTH_CLOSED_C[1] - 3:MOUTH_CLOSED_C[1] + 4,
        MOUTH_CLOSED_C[0] - 40:MOUTH_CLOSED_C[0] + 40,
        :3,
    ].reshape(-1, 3)
    line_col = tuple(int(v) for v in seg[seg.sum(1).argmin()]) if len(seg) else (45, 55, 130)

    eye_colours = {}
    for side, e in EYES.items():
        em = np.array(ell_mask(CANVAS, e["c"], e["r"], e["tilt"], grow=3, feather=0)) > 128
        ring = (np.array(ell_mask(CANVAS, e["c"], e["r"], e["tilt"], grow=28, feather=0)) > 128) & (~em)
        eye_colours[side] = tuple(int(c * 0.95) for c in skin_colour(open_arr, ring))
    return mouth_skin, line_col, eye_colours


def _extract_lashes(closed_arr: np.ndarray) -> dict[str, Image.Image]:
    lashes = {}
    arr = closed_arr.astype(int)
    for side, (cx, cy, hw, hh) in LASH_SRC.items():
        box = (cx - hw, cy - hh, cx + hw, cy + hh)
        reg = arr[box[1]:box[3], box[0]:box[2], :3]
        dark = reg.max(axis=2)
        a = np.clip((125 - dark) / 45.0, 0, 1)
        im = Image.fromarray(np.dstack([reg.astype(np.uint8), (a * 255).astype(np.uint8)]), "RGBA")
        lashes[side] = im.filter(ImageFilter.GaussianBlur(0.6))
    return lashes


def _apply_strands_over_eye(frame: Image.Image, open_arr: np.ndarray, side: str,
                            cover_fn=None) -> None:
    """Keep foreground hair strands above eyelids inside the eye zone."""
    e = EYES[side]
    rx, ry = e["r"]
    sp = STRANDS[side]
    strands = np.array(poly_mask(CANVAS, sp["poly"], feather=1.5)).astype(np.float32) / 255.0
    zone = np.array(ell_mask(CANVAS, e["c"], (rx + 10, ry + 10), e["tilt"], feather=2)).astype(np.float32) / 255.0
    patch = strand_patch(open_arr, sp)
    patch[:, :, 3] *= strands * zone
    if cover_fn is not None:
        yy, _xx = np.indices((CANVAS[1], CANVAS[0]))
        patch[:, :, 3] *= cover_fn(yy)
    frame.alpha_composite(Image.fromarray(patch.clip(0, 255).astype(np.uint8), "RGBA"))


def _draw_glow_stroke(canvas: Image.Image, pts: list[tuple[float, float]],
                      core_col: tuple[int, int, int, int],
                      glow_col: tuple[int, int, int, int],
                      width: int = 8, glow_width: int = 16,
                      blur: float = 4.0) -> None:
    glow = Image.new("RGBA", CANVAS, (0, 0, 0, 0))
    ImageDraw.Draw(glow).line(pts, fill=glow_col, width=glow_width, joint="curve")
    glow = glow.filter(ImageFilter.GaussianBlur(blur))
    canvas.alpha_composite(glow)
    core = Image.new("RGBA", CANVAS, (0, 0, 0, 0))
    ImageDraw.Draw(core).line(pts, fill=core_col, width=width, joint="curve")
    core = core.filter(ImageFilter.GaussianBlur(0.6))
    canvas.alpha_composite(core)


def _quad_bezier(p0, p1, p2, steps=36):
    pts = []
    for i in range(steps + 1):
        t = i / steps
        u = 1.0 - t
        x = u * u * p0[0] + 2 * u * t * p1[0] + t * t * p2[0]
        y = u * u * p0[1] + 2 * u * t * p1[1] + t * t * p2[1]
        pts.append((x, y))
    return pts


def _build_eye_layers(open_arr: np.ndarray, closed_arr: np.ndarray,
                      eye_colours: dict[str, tuple[int, int, int]]) -> dict[str, Image.Image]:
    """Build expression-specific eye open/closed layers aligned to EYES."""
    yy, xx = np.indices((CANVAS[1], CANVAS[0]))
    lashes = _extract_lashes(closed_arr)
    layers: dict[str, Image.Image] = {}

    def make_lid_frame(cover_mask_fn, *, tint=None, extra_draw_fn=None) -> Image.Image:
        frame = Image.new("RGBA", CANVAS, (0, 0, 0, 0))
        for side, e in EYES.items():
            em = np.array(ell_mask(CANVAS, e["c"], e["r"], e["tilt"], grow=5, feather=2.2)).astype(np.float32) / 255.0
            sp = STRANDS[side]
            strands = np.array(poly_mask(CANVAS, sp["poly"], feather=1.5)).astype(np.float32) / 255.0
            cov = cover_mask_fn(side, e, yy, xx)
            alpha = np.clip(em * cov * (1.0 - strands), 0.0, 1.0)
            col = tint or eye_colours[side]
            frame.alpha_composite(dotted_fill(CANVAS, Image.fromarray((alpha * 255).astype(np.uint8)), col))
            if extra_draw_fn is not None:
                extra_draw_fn(frame, side, e, strands)
            _apply_strands_over_eye(frame, open_arr, side)
        return frame

    # 1. Happy / Love closed-eye smile: upward curved dark-blue/cyan lashes over full skin cover
    def happy_closed_draw(frame, side, e, strands):
        cx, cy = e["c"]
        rx, ry = e["r"]
        sgn = 1 if side == "L" else -1
        p0 = (cx - rx * 0.78, cy + 8 + sgn * 3)
        p1 = (cx, cy - ry * 0.78)
        p2 = (cx + rx * 0.78, cy + 8 - sgn * 3)
        pts = _quad_bezier(p0, p1, p2)
        arc = Image.new("RGBA", CANVAS, (0, 0, 0, 0))
        _draw_glow_stroke(arc, pts, (18, 28, 105, 245), (120, 220, 255, 190), width=11, glow_width=19, blur=3.2)
        arr = np.array(arc)
        arr[:, :, 3] = (arr[:, :, 3].astype(np.float32) * (1.0 - strands)).astype(np.uint8)
        frame.alpha_composite(Image.fromarray(arr, "RGBA"))

    layers["rem-eyes-happy-closed"] = make_lid_frame(
        lambda _s, _e, _yy, _xx: 1.0,
        extra_draw_fn=happy_closed_draw,
    )

    # 2. Angry open: angled upper lid lowering sharply toward nose bridge
    def angry_open_cover(side, e, y_grid, x_grid):
        cx, cy = e["c"]
        rx, ry = e["r"]
        # Inner corner is closer to x=670
        inner_dir = 1.0 if side == "L" else -1.0
        dx = (x_grid - cx) / max(1.0, rx)
        # Slope down toward inner side
        edge = (cy - ry * 0.35) + inner_dir * dx * (ry * 0.48)
        mask = np.clip((edge - y_grid + 4.0) / 5.0, 0.0, 1.0)
        return mask

    def angry_open_lash(frame, side, e, strands):
        cx, cy = e["c"]
        rx, ry = e["r"]
        inner_dir = 1.0 if side == "L" else -1.0
        p0 = (cx - rx * 0.85, (cy - ry * 0.35) - inner_dir * rx * 0.85 / rx * (ry * 0.48))
        p2 = (cx + rx * 0.85, (cy - ry * 0.35) + inner_dir * rx * 0.85 / rx * (ry * 0.48))
        p1 = ((p0[0] + p2[0]) / 2, (p0[1] + p2[1]) / 2 + 4)
        pts = _quad_bezier(p0, p1, p2)
        arc = Image.new("RGBA", CANVAS, (0, 0, 0, 0))
        _draw_glow_stroke(arc, pts, (16, 26, 98, 245), (100, 210, 255, 160), width=9, glow_width=15, blur=2.5)
        arr = np.array(arc)
        arr[:, :, 3] = (arr[:, :, 3].astype(np.float32) * (1.0 - strands)).astype(np.uint8)
        frame.alpha_composite(Image.fromarray(arr, "RGBA"))

    layers["rem-eyes-angry-open"] = make_lid_frame(angry_open_cover, extra_draw_fn=angry_open_lash)

    # 3. Angry closed: sharp downward inner V lash lines (> <)
    def angry_closed_draw(frame, side, e, strands):
        cx, cy = e["c"]
        rx, ry = e["r"]
        inner_dir = 1.0 if side == "L" else -1.0
        p0 = (cx - rx * 0.78, cy - 10 * inner_dir + 6)
        p1 = (cx + 4 * inner_dir, cy + 12)
        p2 = (cx + rx * 0.78, cy + 10 * inner_dir + 6)
        pts = _quad_bezier(p0, p1, p2)
        arc = Image.new("RGBA", CANVAS, (0, 0, 0, 0))
        _draw_glow_stroke(arc, pts, (16, 24, 96, 248), (110, 210, 255, 175), width=11, glow_width=18, blur=3.0)
        arr = np.array(arc)
        arr[:, :, 3] = (arr[:, :, 3].astype(np.float32) * (1.0 - strands)).astype(np.uint8)
        frame.alpha_composite(Image.fromarray(arr, "RGBA"))

    layers["rem-eyes-angry-closed"] = make_lid_frame(
        lambda _s, _e, _yy, _xx: 1.0,
        extra_draw_fn=angry_closed_draw,
    )

    # 4. Sad open: slightly lowered outer-droopy upper lid + glossy watery bottom reflection
    def sad_open_cover(side, e, y_grid, x_grid):
        cx, cy = e["c"]
        rx, ry = e["r"]
        outer_dir = -1.0 if side == "L" else 1.0
        dx = (x_grid - cx) / max(1.0, rx)
        edge = (cy - ry * 0.42) + outer_dir * dx * (ry * 0.25)
        return np.clip((edge - y_grid + 4.0) / 5.0, 0.0, 1.0)

    def sad_open_gloss(frame, side, e, strands):
        cx, cy = e["c"]
        rx, ry = e["r"]
        gloss = Image.new("RGBA", CANVAS, (0, 0, 0, 0))
        d = ImageDraw.Draw(gloss)
        # Watery highlight pool near lower rim of each iris
        d.ellipse((cx - rx * 0.50, cy + ry * 0.18, cx + rx * 0.46, cy + ry * 0.74), fill=(215, 248, 255, 130))
        d.ellipse((cx - rx * 0.26, cy + ry * 0.30, cx + rx * 0.22, cy + ry * 0.62), fill=(255, 255, 255, 185))
        gloss = gloss.filter(ImageFilter.GaussianBlur(3.0))
        arr = np.array(gloss)
        arr[:, :, 3] = (arr[:, :, 3].astype(np.float32) * (1.0 - strands)).astype(np.uint8)
        frame.alpha_composite(Image.fromarray(arr, "RGBA"))

    layers["rem-eyes-sad-open"] = make_lid_frame(sad_open_cover, extra_draw_fn=sad_open_gloss)

    # 5. Sad closed: gentle sad drooping closed lash lines
    def sad_closed_draw(frame, side, e, strands):
        cx, cy = e["c"]
        rx, ry = e["r"]
        outer_dir = -1.0 if side == "L" else 1.0
        p0 = (cx - rx * 0.75, cy + 4 + 8 * outer_dir)
        p1 = (cx, cy + ry * 0.48)
        p2 = (cx + rx * 0.75, cy + 4 - 8 * outer_dir)
        pts = _quad_bezier(p0, p1, p2)
        arc = Image.new("RGBA", CANVAS, (0, 0, 0, 0))
        _draw_glow_stroke(arc, pts, (18, 30, 105, 245), (140, 230, 255, 185), width=10, glow_width=17, blur=3.0)
        arr = np.array(arc)
        arr[:, :, 3] = (arr[:, :, 3].astype(np.float32) * (1.0 - strands)).astype(np.uint8)
        frame.alpha_composite(Image.fromarray(arr, "RGBA"))

    layers["rem-eyes-sad-closed"] = make_lid_frame(
        lambda _s, _e, _yy, _xx: 1.0,
        extra_draw_fn=sad_closed_draw,
    )

    # 6. Shy: slightly lowered soft bashful upper lid + extra catchlight shimmer
    def shy_cover(side, e, y_grid, x_grid):
        cx, cy = e["c"]
        ry = e["r"][1]
        edge = cy - ry * 0.36
        return np.clip((edge - y_grid + 4.0) / 5.0, 0.0, 1.0)

    layers["rem-eyes-shy"] = make_lid_frame(shy_cover)

    # 7. Sleepy: heavy half-closed droopy upper lid (~0.56 down) with relaxed lash line
    def sleepy_cover(side, e, y_grid, x_grid):
        cx, cy = e["c"]
        ry = e["r"][1]
        edge = cy + ry * 0.08
        return np.clip((edge - y_grid + 4.0) / 5.0, 0.0, 1.0)

    def sleepy_lash(frame, side, e, strands):
        cx, cy = e["c"]
        rx, ry = e["r"]
        p0 = (cx - rx * 0.78, cy + ry * 0.04)
        p1 = (cx, cy + ry * 0.20)
        p2 = (cx + rx * 0.78, cy + ry * 0.06)
        pts = _quad_bezier(p0, p1, p2)
        arc = Image.new("RGBA", CANVAS, (0, 0, 0, 0))
        _draw_glow_stroke(arc, pts, (16, 26, 98, 240), (110, 215, 255, 165), width=10, glow_width=16, blur=2.6)
        arr = np.array(arc)
        arr[:, :, 3] = (arr[:, :, 3].astype(np.float32) * (1.0 - strands)).astype(np.uint8)
        frame.alpha_composite(Image.fromarray(arr, "RGBA"))

    layers["rem-eyes-sleepy"] = make_lid_frame(sleepy_cover, extra_draw_fn=sleepy_lash)

    # 8. Surprised wide eyes: subtle bright sclera & catchlight lift around upper/lower eye rim
    wide_frame = Image.new("RGBA", CANVAS, (0, 0, 0, 0))
    for side, e in EYES.items():
        cx, cy = e["c"]
        rx, ry = e["r"]
        sp = STRANDS[side]
        strands = np.array(poly_mask(CANVAS, sp["poly"], feather=1.5)).astype(np.float32) / 255.0
        ring = Image.new("RGBA", CANVAS, (0, 0, 0, 0))
        d = ImageDraw.Draw(ring)
        d.ellipse((cx - rx * 0.88, cy - ry * 0.96, cx + rx * 0.88, cy + ry * 0.92),
                  outline=(235, 250, 255, 150), width=7)
        d.ellipse((cx - rx * 0.26, cy - ry * 0.56, cx - rx * 0.02, cy - ry * 0.24),
                  fill=(255, 255, 255, 190))
        ring = ring.filter(ImageFilter.GaussianBlur(2.0))
        arr = np.array(ring)
        arr[:, :, 3] = (arr[:, :, 3].astype(np.float32) * (1.0 - strands)).astype(np.uint8)
        wide_frame.alpha_composite(Image.fromarray(arr, "RGBA"))
        _apply_strands_over_eye(wide_frame, open_arr, side)
    layers["rem-eyes-wide"] = wide_frame

    return layers


def _build_brow_layers() -> dict[str, Image.Image]:
    """Build luminous particle-style eyebrow layers above EYES."""
    layers = {}
    # Specs per brow expression: (outer_dy, mid_dy, inner_dy) relative to eye top
    specs = {
        "neutral":   (-14, -26, -12),
        "happy":     (-20, -36, -18),
        "sad":       (-4, -20, -36),
        "angry":     (-24, -10, +12),
        "surprised": (-28, -48, -30),
        "shy":       (-8, -22, -32),
        "sleepy":    (-10, -18, -14),
    }
    for name, (ody, mdy, idy) in specs.items():
        canvas = Image.new("RGBA", CANVAS, (0, 0, 0, 0))
        for side, e in EYES.items():
            cx, cy = e["c"]
            rx, ry = e["r"]
            top_y = cy - ry
            if side == "L":
                p0 = (cx - rx * 0.72, top_y + ody)
                p1 = (cx, top_y + mdy)
                p2 = (cx + rx * 0.72, top_y + idy)
            else:
                p0 = (cx - rx * 0.72, top_y + idy)
                p1 = (cx, top_y + mdy)
                p2 = (cx + rx * 0.72, top_y + ody)
            pts = _quad_bezier(p0, p1, p2)
            _draw_glow_stroke(
                canvas, pts,
                core_col=(18, 38, 128, 235),
                glow_col=(135, 225, 255, 210),
                width=7, glow_width=14, blur=2.6,
            )
        layers[f"rem-brows-{name}"] = canvas
    return layers


def _build_mouth_layers(mouth_skin: tuple[int, int, int],
                        line_col: tuple[int, int, int],
                        open_img: Image.Image) -> dict[str, Image.Image]:
    """Build expression mouth layers anchored at MOUTH_OPEN_C with matching skin pads."""
    layers = {}
    mx, my = MOUTH_OPEN_C

    def skin_pad(rx=44, ry=16, dy=6) -> Image.Image:
        return dotted_fill(CANVAS, ell_mask(CANVAS, (mx, my + dy), (rx, ry), feather=5), mouth_skin)

    # Sad downturned frown (corners lower, center higher in image y-down coords)
    sad = skin_pad()
    sm = Image.new("RGBA", CANVAS, (0, 0, 0, 0))
    pts = [(mx + t, my + 12 - 8 * (1 - (t / 24) ** 2)) for t in range(-24, 25)]
    ImageDraw.Draw(sm).line(pts, fill=line_col + (235,), width=3, joint="curve")
    sad.alpha_composite(sm.filter(ImageFilter.GaussianBlur(0.6)))
    layers["rem-mouth-sad"] = sad

    # Angry closed grimace (sharp downturned frown)
    angry_c = skin_pad()
    am = Image.new("RGBA", CANVAS, (0, 0, 0, 0))
    pts = [(mx + t, my + 13 - 10 * (1 - (t / 28) ** 2)) for t in range(-28, 29)]
    ImageDraw.Draw(am).line(pts, fill=line_col + (245,), width=4, joint="curve")
    angry_c.alpha_composite(am.filter(ImageFilter.GaussianBlur(0.6)))
    layers["rem-mouth-angry-closed"] = angry_c

    # Angry open mouth (tense wide shout)
    angry_o = skin_pad(rx=56, ry=28, dy=2)
    ao = Image.new("RGBA", CANVAS, (0, 0, 0, 0))
    d = ImageDraw.Draw(ao)
    poly = [(mx - 42, my + 12), (mx - 28, my - 12), (mx, my - 18), (mx + 28, my - 12),
            (mx + 42, my + 12), (mx + 22, my + 24), (mx, my + 20), (mx - 22, my + 24)]
    d.polygon(poly, fill=(135, 62, 118, 240), outline=(230, 175, 225, 245))
    d.polygon([(mx - 24, my - 10), (mx + 24, my - 10), (mx + 18, my - 2), (mx - 18, my - 2)],
              fill=(242, 248, 255, 225))
    angry_o.alpha_composite(ao.filter(ImageFilter.GaussianBlur(0.6)))
    layers["rem-mouth-angry"] = angry_o

    # Shy tiny embarrassed mouth
    shy = skin_pad()
    sh = Image.new("RGBA", CANVAS, (0, 0, 0, 0))
    d = ImageDraw.Draw(sh)
    d.ellipse((mx - 12, my + 1, mx + 12, my + 17), fill=(185, 108, 158, 225), outline=line_col + (225,), width=2)
    shy.alpha_composite(sh.filter(ImageFilter.GaussianBlur(0.6)))
    layers["rem-mouth-shy"] = shy

    # Surprised rounded O mouth
    surprised = skin_pad(rx=44, ry=24, dy=4)
    su = Image.new("RGBA", CANVAS, (0, 0, 0, 0))
    d = ImageDraw.Draw(su)
    d.ellipse((mx - 20, my - 12, mx + 20, my + 26), fill=(142, 72, 128, 238), outline=(235, 185, 225, 240), width=3)
    surprised.alpha_composite(su.filter(ImageFilter.GaussianBlur(0.6)))
    layers["rem-mouth-surprised"] = surprised

    # Sleepy soft small drowsy mouth
    sleepy = skin_pad(rx=44, ry=20, dy=5)
    sl = Image.new("RGBA", CANVAS, (0, 0, 0, 0))
    d = ImageDraw.Draw(sl)
    d.ellipse((mx - 16, my - 2, mx + 16, my + 20), fill=(168, 92, 145, 228), outline=line_col + (225,), width=2)
    sleepy.alpha_composite(sl.filter(ImageFilter.GaussianBlur(0.7)))
    layers["rem-mouth-sleepy"] = sleepy

    # Love warm closed smile (corners higher, center lower in image y-down coords)
    love = skin_pad()
    lv = Image.new("RGBA", CANVAS, (0, 0, 0, 0))
    pts = [(mx + t, my + 2 + 9 * (1 - (t / 30) ** 2)) for t in range(-30, 31)]
    ImageDraw.Draw(lv).line(pts, fill=(135, 72, 142, 240), width=4, joint="curve")
    love.alpha_composite(lv.filter(ImageFilter.GaussianBlur(0.6)))
    layers["rem-mouth-love"] = love

    # Happy open smile (derived cleanly from original open mouth lifted gently)
    happy = skin_pad(rx=62, ry=28, dy=2)
    hm = Image.new("RGBA", CANVAS, (0, 0, 0, 0))
    d = ImageDraw.Draw(hm)
    d.polygon([(mx - 48, my - 4), (mx, my - 10), (mx + 48, my - 4),
               (mx + 34, my + 22), (mx, my + 30), (mx - 34, my + 22)],
              fill=(178, 88, 146, 238), outline=(238, 192, 228, 245))
    d.polygon([(mx - 38, my - 4), (mx + 38, my - 4), (mx + 30, my + 4), (mx - 30, my + 4)],
              fill=(248, 250, 255, 220))
    happy.alpha_composite(hm.filter(ImageFilter.GaussianBlur(0.6)))
    layers["rem-mouth-happy"] = happy

    return layers


def _build_blush_and_tears() -> dict[str, Image.Image]:
    layers = {}
    # Soft blush on both cheeks
    soft = Image.new("RGBA", CANVAS, (0, 0, 0, 0))
    d = ImageDraw.Draw(soft)
    for cx, cy, rx, ry in ((482, 660, 74, 34), (835, 682, 72, 34)):
        d.ellipse((cx - rx, cy - ry, cx + rx, cy + ry), fill=(255, 118, 178, 125))
    layers["rem-blush-soft"] = soft.filter(ImageFilter.GaussianBlur(18))

    # Intense Shy blush (across cheeks + nose bridge + diagonal hatch strokes)
    intense = Image.new("RGBA", CANVAS, (0, 0, 0, 0))
    d = ImageDraw.Draw(intense)
    for cx, cy, rx, ry in ((485, 662, 92, 42), (832, 682, 90, 42), (660, 666, 130, 32)):
        d.ellipse((cx - rx, cy - ry, cx + rx, cy + ry), fill=(255, 95, 165, 165))
    intense = intense.filter(ImageFilter.GaussianBlur(16))
    hatch = Image.new("RGBA", CANVAS, (0, 0, 0, 0))
    hd = ImageDraw.Draw(hatch)
    for base_x, base_y in ((435, 668), (785, 688)):
        for i in range(6):
            x0 = base_x + i * 20
            hd.line([(x0, base_y + 18), (x0 + 18, base_y - 18)], fill=(255, 185, 220, 175), width=4)
    hatch = hatch.filter(ImageFilter.GaussianBlur(1.4))
    intense.alpha_composite(hatch)
    layers["rem-blush-intense"] = intense

    # Glowing cyan-white tears below both eyes
    tears = Image.new("RGBA", CANVAS, (0, 0, 0, 0))
    td = ImageDraw.Draw(tears)
    for side, e in EYES.items():
        cx, cy = e["c"]
        rx, ry = e["r"]
        # Welling tear pool along lower lid
        td.ellipse((cx - rx * 0.62, cy + ry * 0.56, cx + rx * 0.60, cy + ry * 1.05),
                   fill=(170, 240, 255, 165))
        # Tear stream down cheek
        sx = cx + (-rx * 0.35 if side == "L" else rx * 0.35)
        stream = _quad_bezier((sx, cy + ry * 0.85), (sx - 4, cy + ry * 2.1), (sx + 2, cy + ry * 3.1))
        td.line(stream, fill=(195, 246, 255, 180), width=12, joint="curve")
        td.ellipse((sx - 10, cy + ry * 2.95, sx + 12, cy + ry * 3.35), fill=(225, 252, 255, 200))
    layers["rem-tears"] = tears.filter(ImageFilter.GaussianBlur(2.5))
    return layers


def _draw_heart(draw: ImageDraw.ImageDraw, cx: float, cy: float, size: float,
                outline_col, width: int = 6) -> None:
    pts = []
    for i in range(64):
        t = i / 64.0 * 2.0 * math.pi
        x = 16.0 * (math.sin(t) ** 3)
        y = -(13.0 * math.cos(t) - 5.0 * math.cos(2 * t) - 2.0 * math.cos(3 * t) - math.cos(4 * t))
        pts.append((cx + x * size / 16.0, cy + y * size / 16.0))
    draw.polygon(pts, outline=outline_col, width=width)


def _build_fx_layers() -> dict[str, Image.Image]:
    """Build decorative expression effects matching the reference expression grids."""
    layers = {}

    # Happy: 3 yellow radial burst lines top-left
    happy = Image.new("RGBA", CANVAS, (0, 0, 0, 0))
    for seg in ([(215, 205), (275, 232)], [(238, 145), (296, 195)], [(305, 118), (332, 178)]):
        _draw_glow_stroke(happy, seg, (255, 238, 120, 245), (255, 198, 40, 200), width=10, glow_width=20, blur=4.0)
    layers["rem-fx-happy"] = happy

    # Angry: neon red anime anger vein mark on upper-right forehead (clear of pink clips)
    angry = Image.new("RGBA", CANVAS, (0, 0, 0, 0))
    ax, ay = 768, 415
    arcs = [
        _quad_bezier((ax - 30, ay - 6), (ax - 12, ay - 12), (ax - 6, ay - 30)),
        _quad_bezier((ax + 6, ay - 30), (ax + 12, ay - 12), (ax + 30, ay - 6)),
        _quad_bezier((ax + 30, ay + 6), (ax + 12, ay + 12), (ax + 6, ay + 30)),
        _quad_bezier((ax - 6, ay + 30), (ax - 12, ay + 12), (ax - 30, ay + 6)),
    ]
    for arc in arcs:
        _draw_glow_stroke(angry, arc, (255, 65, 95, 250), (255, 20, 55, 215), width=9, glow_width=18, blur=4.0)
    layers["rem-fx-angry"] = angry

    # Shy: pink diagonal slash marks near lower-left cheek/collar
    shy = Image.new("RGBA", CANVAS, (0, 0, 0, 0))
    for i in range(4):
        x0 = 235 + i * 24
        _draw_glow_stroke(shy, [(x0, 865), (x0 + 22, 818)],
                          (255, 145, 195, 240), (255, 85, 165, 190), width=8, glow_width=16, blur=3.0)
    layers["rem-fx-shy"] = shy

    # Surprised: glowing yellow "!?" top-left
    surp = Image.new("RGBA", CANVAS, (0, 0, 0, 0))
    _draw_glow_stroke(surp, [(240, 125), (252, 195)], (255, 238, 115, 250), (255, 190, 35, 205), width=11, glow_width=22, blur=4.0)
    _draw_glow_stroke(surp, [(256, 214), (258, 224)], (255, 238, 115, 250), (255, 190, 35, 205), width=12, glow_width=22, blur=4.0)
    q_pts = _quad_bezier((286, 142), (314, 110), (332, 138)) + _quad_bezier((332, 138), (338, 160), (308, 188))
    _draw_glow_stroke(surp, q_pts, (255, 238, 115, 250), (255, 190, 35, 205), width=10, glow_width=20, blur=4.0)
    _draw_glow_stroke(surp, [(307, 208), (308, 218)], (255, 238, 115, 250), (255, 190, 35, 205), width=12, glow_width=22, blur=4.0)
    layers["rem-fx-surprised"] = surp

    # Sleepy: glowing cyan "zZz" top-left + subtle sleep bubble near cheek
    sleepy = Image.new("RGBA", CANVAS, (0, 0, 0, 0))
    z1 = [(215, 182), (252, 182), (215, 222), (252, 222)]
    z2 = [(266, 142), (312, 142), (266, 192), (312, 192)]
    z3 = [(322, 102), (376, 102), (322, 160), (376, 160)]
    for z in (z1, z2, z3):
        _draw_glow_stroke(sleepy, z, (120, 230, 255, 245), (40, 175, 255, 205), width=9, glow_width=18, blur=3.5)
    bub = Image.new("RGBA", CANVAS, (0, 0, 0, 0))
    bd = ImageDraw.Draw(bub)
    bd.ellipse((345, 675, 405, 735), fill=(150, 225, 255, 55), outline=(190, 242, 255, 185), width=4)
    bd.ellipse((360, 690, 376, 706), fill=(235, 250, 255, 170))
    sleepy.alpha_composite(bub.filter(ImageFilter.GaussianBlur(1.2)))
    layers["rem-fx-sleepy"] = sleepy

    # Love: neon pink outlined hearts top-left + small heart lower-right
    love = Image.new("RGBA", CANVAS, (0, 0, 0, 0))
    glow = Image.new("RGBA", CANVAS, (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    for hx, hy, hs in ((295, 165, 42), (235, 238, 28), (975, 830, 24)):
        _draw_heart(gd, hx, hy, hs, (255, 60, 165, 210), width=14)
    love.alpha_composite(glow.filter(ImageFilter.GaussianBlur(5.0)))
    core = Image.new("RGBA", CANVAS, (0, 0, 0, 0))
    cd = ImageDraw.Draw(core)
    for hx, hy, hs in ((295, 165, 42), (235, 238, 28), (975, 830, 24)):
        _draw_heart(cd, hx, hy, hs, (255, 150, 215, 250), width=6)
    love.alpha_composite(core.filter(ImageFilter.GaussianBlur(0.5)))
    layers["rem-fx-love"] = love

    return layers


def build_expression_layers(out_dir: Path = OUT_DIR) -> dict:
    """Generate all expression layers and update manifest.json in place."""
    if not SRC_OPEN.exists() or not SRC_CLOSED.exists():
        raise FileNotFoundError(f"Source REM images missing under {OUT_DIR / 'source'}")
    open_img = Image.open(SRC_OPEN).convert("RGBA")
    closed_img = Image.open(SRC_CLOSED).convert("RGBA")
    open_arr = np.array(open_img)
    closed_arr = np.array(closed_img)
    mouth_skin, line_col, eye_colours = _sample_palettes(open_arr, closed_arr)

    manifest_path = out_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    layers_meta: dict[str, dict] = dict(manifest.get("layers") or {})

    all_new: dict[str, tuple[Image.Image, dict]] = {}
    for name, img in _build_eye_layers(open_arr, closed_arr, eye_colours).items():
        all_new[name] = (img, {"role": "eyes_expr"})
    for name, img in _build_brow_layers().items():
        all_new[name] = (img, {"role": "brows"})
    for name, img in _build_mouth_layers(mouth_skin, line_col, open_img).items():
        all_new[name] = (img, {"role": "mouth_expr"})
    for name, img in _build_blush_and_tears().items():
        all_new[name] = (img, {"role": "cheek_fx"})
    for name, img in _build_fx_layers().items():
        all_new[name] = (img, {"role": "fx"})

    for name, (img, meta) in all_new.items():
        cropped, (x, y) = crop_layer(img, pad=2)
        fn = f"{name}.png"
        cropped.save(out_dir / fn, optimize=True)
        layers_meta[name] = dict(file=fn, x=x, y=y, w=cropped.width, h=cropped.height, **meta)

    manifest["layers"] = layers_meta
    manifest["expressions"] = [
        "NEUTRAL", "HAPPY", "SAD", "ANGRY", "SHY", "SURPRISED", "SLEEPY", "LOVE"
    ]
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest


if __name__ == "__main__":
    m = build_expression_layers()
    print(f"Updated {OUT_DIR / 'manifest.json'} with {len(m['layers'])} total layers.")
