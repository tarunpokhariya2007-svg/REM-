"""Build the layered REM avatar assets used by core/rem_avatar.py.

Run from the repository root (needs Pillow + numpy; NOT needed at runtime):

    python tools/build_rem_layers.py \
        --open   path/to/rem-source-open.png   \
        --closed path/to/rem-source-closed.png

Inputs are the two supplied RGBA artworks (same canvas size, transparent
background): ``open`` (eyes open, mouth open) and ``closed`` (eyes closed,
mouth closed). They are two separate drawings, not an aligned layer pair, so
this tool never pastes the closed-eye drawing over the open one. It:

  * keeps the open artwork as the base and swaps its open mouth for the
    closed-mouth pixels of the second drawing (feathered),
  * extracts the dark eyelash strokes of the second drawing and re-places
    them on the open drawing's eye positions,
  * builds eyelid frames (skin cover + lash line, hair strands kept on top),
  * builds mouth keyframes from the original open mouth (vertical / width
    scaling) — these are *derived* shapes, not hand-drawn phonemes,
  * writes manifest.json describing every layer and its canvas position.

Coordinates below are pixel positions on the 1277x1232 source canvas.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

# --- geometry measured on the supplied 1277x1232 artwork ---------------------
CANVAS = (1277, 1232)
# open-eye almond: centre x, centre y, half-width, half-height, tilt (deg)
EYES = {
    "L": dict(c=(511, 561), r=(88, 50), tilt=4.0),
    "R": dict(c=(832, 587), r=(84, 49), tilt=-3.0),
}
# closed-lash centre in the CLOSED drawing  ->  target lash centre on the OPEN one
LASH_SRC = {"L": (518, 598, 92, 34), "R": (826, 596, 92, 34)}   # cx, cy, hw, hh
LASH_DST = {"L": (510, 586), "R": (833, 606)}
# hair strands that hang in front of the eyes on the open drawing (polygons)
# (polygon, source rows above the eye to continue downwards, centre-line slope px/row)
STRANDS = {
    "L": dict(poly=[(495, 478), (557, 478), (556, 505), (544, 560), (527, 622), (505, 622), (498, 560), (495, 505)],
              src=(480, 505), slope=-0.065),
    "R": dict(poly=[(814, 500), (831, 500), (829, 540), (816, 600), (812, 640), (796, 640), (800, 600), (808, 540)],
              src=(500, 535), slope=-0.143),
}
MOUTH_OPEN_C = (655, 768)          # centre of the open mouth, open drawing
MOUTH_OPEN_R = (112, 44)           # half-width / half-height of its ellipse
MOUTH_TILT = 8.0                   # tilt in image coordinates (y down): left end of the mouth sits higher
MOUTH_CLOSED_C = (655, 752)        # closed-mouth line centre, closed drawing
LID_LEVELS = (0.35, 0.7, 1.0)
MOUTH_KEYS = {                     # name: (height scale, width scale)
    "small": (0.30, 0.86),
    "medium": (0.62, 0.96),
    "open": (1.00, 1.00),
    "round": (0.82, 0.62),
    "wide": (0.52, 1.14),
}


def ell_mask(size, c, r, tilt=0.0, grow=0.0, feather=3.0):
    m = Image.new("L", size, 0)
    d = ImageDraw.Draw(m)
    rx, ry = r[0] + grow, r[1] + grow
    pts = []
    for i in range(96):
        a = i / 96 * 2 * np.pi
        x, y = rx * np.cos(a), ry * np.sin(a)
        t = np.radians(tilt)
        pts.append((c[0] + x * np.cos(t) - y * np.sin(t),
                    c[1] + x * np.sin(t) + y * np.cos(t)))
    d.polygon(pts, fill=255)
    return m.filter(ImageFilter.GaussianBlur(feather)) if feather else m


def poly_mask(size, pts, feather=2.0):
    m = Image.new("L", size, 0)
    ImageDraw.Draw(m).polygon(pts, fill=255)
    return m.filter(ImageFilter.GaussianBlur(feather))


def strand_patch(arr: np.ndarray, spec: dict) -> np.ndarray:
    """RGBA float patch: the hair strand continued downwards through the eye.

    The open drawing shows the iris through the strand, which would stay
    visible when the eye closes. Rows just above the eye are tiled downwards
    (shifted along the strand's slope so it keeps its direction)."""
    h, w = arr.shape[:2]
    patch = np.zeros((h, w, 4), np.float32)
    y0, y1 = spec["src"]
    hs = y1 - y0
    xs = np.arange(w)
    ymin = int(min(p[1] for p in spec["poly"]))
    ymax = int(max(p[1] for p in spec["poly"]))
    for y in range(y1, ymax + 1):
        ys = y0 + ((y - y0) % hs)
        shift = int(round(spec["slope"] * (y - ys)))
        src = np.clip(xs - shift, 0, w - 1)
        patch[y, :, :3] = arr[ys, src, :3]
        patch[y, :, 3] = 255.0
    return patch


def crop_layer(img: Image.Image, pad=2):
    a = np.array(img)[:, :, 3]
    ys, xs = np.nonzero(a > 2)
    if not len(xs):
        return img.crop((0, 0, 1, 1)), (0, 0)
    box = (int(max(0, xs.min() - pad)), int(max(0, ys.min() - pad)),
           int(min(img.width, xs.max() + pad + 1)), int(min(img.height, ys.max() + pad + 1)))
    return img.crop(box), (box[0], box[1])


def skin_colour(arr: np.ndarray, region: np.ndarray) -> tuple[int, int, int]:
    rgb = arr[:, :, :3].astype(int)
    skin = (region & (rgb[:, :, 0] > 150) & (rgb[:, :, 2] > 215)
            & (rgb[:, :, 2] - rgb[:, :, 0] < 95) & (rgb[:, :, 1] > 150))
    px = rgb[skin]
    return tuple(int(v) for v in np.median(px, axis=0)) if len(px) else (196, 190, 238)


def dotted_fill(size, mask: Image.Image, colour, period=5) -> Image.Image:
    """Solid skin colour with the artwork's fine lighter dot lattice."""
    base = Image.new("RGBA", size, colour + (255,))
    dots = Image.new("L", size, 0)
    d = ImageDraw.Draw(dots)
    for y in range(0, size[1], period):
        for x in range(0, size[0], period):
            d.ellipse((x, y, x + 2, y + 2), fill=255)
    light = Image.new("RGBA", size, tuple(min(255, c + 26) for c in colour) + (255,))
    base.paste(light, (0, 0), dots.filter(ImageFilter.GaussianBlur(0.4)))
    base.putalpha(mask)
    return base


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--open", required=True)
    ap.add_argument("--closed", required=True)
    ap.add_argument("--out", default=str(Path(__file__).resolve().parent.parent
                                         / "gui" / "assets" / "rem"))
    ap.add_argument("--debug", default="", help="folder for debug composites")
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    A = Image.open(args.open).convert("RGBA")
    B = Image.open(args.closed).convert("RGBA")
    if A.size != CANVAS or B.size != CANVAS:
        raise SystemExit(f"expected {CANVAS} artwork, got {A.size} and {B.size}")
    Aarr = np.array(A)
    layers: dict[str, dict] = {}

    def save(name: str, img: Image.Image, crop=True, **meta):
        if crop:
            img, (x, y) = crop_layer(img)
        else:
            x = y = 0
        fn = f"{name}.png"
        img.save(out / fn, optimize=True)
        layers[name] = dict(file=fn, x=x, y=y, w=img.width, h=img.height, **meta)

    # ---- base = open artwork with the open mouth swapped for the closed one ----
    # Straight per-pixel blend: rgb = open*(1-m) + closed*m, alpha untouched
    # (the face interior is fully opaque in the source, so no holes appear).
    cmask = ell_mask(CANVAS, MOUTH_OPEN_C, (MOUTH_OPEN_R[0] + 8, MOUTH_OPEN_R[1] + 12), MOUTH_TILT, feather=7)
    shift = (MOUTH_OPEN_C[0] - MOUTH_CLOSED_C[0], MOUTH_OPEN_C[1] - MOUTH_CLOSED_C[1])
    Bs = Image.new("RGBA", CANVAS, (0, 0, 0, 0))
    Bs.paste(B, shift)
    m = (np.array(cmask).astype(np.float32) / 255.0)[:, :, None]
    ar = np.array(A).astype(np.float32)
    br = np.array(Bs).astype(np.float32)
    m = m * (br[:, :, 3:4] > 250)          # only where the closed drawing is opaque
    out_rgb = ar[:, :, :3] * (1 - m) + br[:, :, :3] * m
    base = Image.fromarray(np.dstack([out_rgb, ar[:, :, 3:4]]).clip(0, 255).astype(np.uint8), "RGBA")
    # The source's open mouth has a tiny tail at its left corner that survives the
    # swap; clone skin from 20 px to the right (a multiple of the dot period).
    SPECK = (555, 748, 12)
    arr = np.array(base)
    sx, sy, sr = SPECK
    yy0, xx0 = np.ogrid[:arr.shape[0], :arr.shape[1]]
    d = np.hypot(xx0 - sx, yy0 - sy)
    w = np.clip((sr - d) / 4.0, 0, 1)[:, :, None]
    src = np.roll(arr, -20, axis=1).astype(np.float32)
    arr[:, :, :3] = (arr[:, :, :3] * (1 - w) + src[:, :, :3] * w).astype(np.uint8)
    base = Image.fromarray(arr, "RGBA")
    save("rem-base", base, crop=False, role="base")

    # ---- open mouth art ------------------------------------------------------
    omask = ell_mask(CANVAS, MOUTH_OPEN_C, MOUTH_OPEN_R, MOUTH_TILT, feather=2.5)
    open_mouth = A.copy()
    open_mouth.putalpha(Image.fromarray((np.array(omask).astype(float) * np.array(A)[:, :, 3] / 255).astype(np.uint8)))
    om, (omx, omy) = crop_layer(open_mouth, pad=3)
    anchor = (MOUTH_OPEN_C[0] - omx, MOUTH_OPEN_C[1] - omy)

    def mouth_variant(h, w):
        W, H = max(4, int(om.width * w)), max(3, int(om.height * h))
        v = om.resize((W, H), Image.LANCZOS)
        canvas = Image.new("RGBA", CANVAS, (0, 0, 0, 0))
        canvas.alpha_composite(v, (int(MOUTH_OPEN_C[0] - anchor[0] * w), int(MOUTH_OPEN_C[1] - anchor[1] * h)))
        return canvas

    for key, (h, w) in MOUTH_KEYS.items():
        save(f"rem-mouth-{key}", mouth_variant(h, w), role="mouth", shape=key)
    # gentle smile: neutral line bowed up at the corners, in the closed drawing's own line colour
    seg = np.array(B)[MOUTH_CLOSED_C[1] - 3:MOUTH_CLOSED_C[1] + 4, MOUTH_CLOSED_C[0] - 40:MOUTH_CLOSED_C[0] + 40, :3].reshape(-1, 3)
    lc = tuple(int(v) for v in seg[seg.sum(1).argmin()]) if len(seg) else (150, 120, 200)
    sm = Image.new("RGBA", CANVAS, (0, 0, 0, 0))
    pts = [(MOUTH_OPEN_C[0] + t, MOUTH_OPEN_C[1] + 8 - 5 * (1 - (t / 26) ** 2)) for t in range(-26, 27)]
    ImageDraw.Draw(sm).line(pts, fill=lc + (235,), width=2, joint="curve")
    sm = sm.filter(ImageFilter.GaussianBlur(0.7))
    # opaque skin pad hides the neutral line; colour sampled right beside the mouth
    near = np.zeros(Aarr.shape[:2], bool)
    near[735:812, 560:600] = True
    near[735:812, 705:745] = True
    pad_colour = skin_colour(Aarr, near)
    smile = dotted_fill(CANVAS, ell_mask(CANVAS, (MOUTH_OPEN_C[0], MOUTH_OPEN_C[1] + 6), (40, 12), feather=5), pad_colour)
    smile.alpha_composite(sm)
    save("rem-mouth-smile", smile, role="mouth", shape="smile")

    # ---- eyelid frames ----------------------------------------------------------
    yy, xx = np.indices((CANVAS[1], CANVAS[0]))
    lash_alpha_all = np.zeros(CANVAS[::-1], np.float32)
    lashes = {}
    Barr = np.array(B).astype(int)
    for side, (cx, cy, hw, hh) in LASH_SRC.items():
        box = (cx - hw, cy - hh, cx + hw, cy + hh)
        reg = Barr[box[1]:box[3], box[0]:box[2], :3]
        dark = reg.max(axis=2)
        a = np.clip((125 - dark) / 45.0, 0, 1)
        im = Image.fromarray(np.dstack([reg.astype(np.uint8), (a * 255).astype(np.uint8)]), "RGBA")
        lashes[side] = im.filter(ImageFilter.GaussianBlur(0.6))

    for level in LID_LEVELS:
        frame = Image.new("RGBA", CANVAS, (0, 0, 0, 0))
        for side, e in EYES.items():
            cx, cy = e["c"]; rx, ry = e["r"]
            top, bottom = cy - ry - 6, cy + ry + 6
            edge = top + level * (bottom - top)
            em = np.array(ell_mask(CANVAS, e["c"], e["r"], e["tilt"], grow=3, feather=2.0)).astype(np.float32) / 255.0
            if level < 1.0:
                cover = np.array(Image.fromarray(((yy <= edge) * 255).astype(np.uint8))
                                 .filter(ImageFilter.GaussianBlur(2.0))).astype(np.float32) / 255.0
            else:
                cover = np.ones_like(em)
            sp = STRANDS[side]
            strands = np.array(poly_mask(CANVAS, sp["poly"], feather=1.5)).astype(np.float32) / 255.0
            alpha = em * cover * (1.0 - strands)
            ring = (np.array(ell_mask(CANVAS, e["c"], e["r"], e["tilt"], grow=28, feather=0)) > 128) & (em < 0.05)
            colour = tuple(int(c * 0.95) for c in skin_colour(Aarr, ring))
            frame.alpha_composite(dotted_fill(CANVAS, Image.fromarray((alpha * 255).astype(np.uint8)), colour))
            # lash line rides on the lid edge; hidden where a hair strand hangs in front
            lash = lashes[side]
            lx, ly = LASH_DST[side]
            ly = int(round(edge - level * (bottom - ly)))
            lashed = Image.new("RGBA", CANVAS, (0, 0, 0, 0))
            lashed.alpha_composite(lash, (lx - lash.width // 2, ly - lash.height // 2))
            la = np.array(lashed)[:, :, 3].astype(np.float32) / 255.0
            zone = np.array(ell_mask(CANVAS, e["c"], (rx + 26, ry + 24), e["tilt"], feather=3)).astype(np.float32) / 255.0
            la = la * zone * (1.0 - strands)
            arr = np.array(lashed)
            arr[:, :, 3] = (la * 255).astype(np.uint8)
            frame.alpha_composite(Image.fromarray(arr, "RGBA"))
            # strand continuation sits in front of lid and lashes, only inside the eye zone
            patch = strand_patch(Aarr, sp)
            patch[:, :, 3] *= strands * np.clip(np.array(ell_mask(CANVAS, e["c"], (rx + 10, ry + 10), e["tilt"], feather=2)).astype(np.float32) / 255.0, 0, 1)
            patch[:, :, 3] *= (yy >= edge - 40) if level < 1.0 else 1.0
            frame.alpha_composite(Image.fromarray(patch.clip(0, 255).astype(np.uint8), "RGBA"))
        save(f"rem-eyes-lid{int(round(level * 100)):03d}", frame, role="eyes", level=level)

    manifest = dict(
        version=1, canvas=dict(w=CANVAS[0], h=CANVAS[1]),
        mouth_anchor=list(MOUTH_OPEN_C), eyes={k: dict(c=list(v["c"]), r=list(v["r"])) for k, v in EYES.items()},
        lid_levels=list(LID_LEVELS), mouth_keys=list(MOUTH_KEYS) + ["smile"], layers=layers,
        notes="Generated by tools/build_rem_layers.py. Mouth keyframes other than closed/open are derived by "
              "scaling the original open mouth; they are not hand-drawn phoneme art.")
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2))
    print(f"wrote {len(layers)} layers to {out}")

    if args.debug:
        dbg = Path(args.debug); dbg.mkdir(parents=True, exist_ok=True)
        def comp(*names):
            im = Image.new("RGBA", CANVAS, (6, 16, 24, 255))
            for n in names:
                L = layers[n]; im.alpha_composite(Image.open(out / L["file"]), (L["x"], L["y"]))
            return im.convert("RGB")
        for n in ["rem-eyes-lid035", "rem-eyes-lid070", "rem-eyes-lid100"]:
            comp("rem-base", n).crop((380, 470, 960, 680)).save(dbg / f"{n}.png")
        for k in list(MOUTH_KEYS) + ["smile"]:
            comp("rem-base", f"rem-mouth-{k}").crop((480, 690, 840, 850)).save(dbg / f"mouth_{k}.png")
        comp("rem-base").crop((380, 470, 960, 850)).save(dbg / "base_face.png")


if __name__ == "__main__":
    main()
