"""Avatar animation, layered-asset and renderer tests.

Automated checks only. They prove the animation logic, the asset set and (when
PyQt6 is installed) that the renderer paints and reacts; they do NOT replace
looking at the avatar on screen — see recovery/AVATAR_UPGRADE_REPORT.md.
"""
import json
import os
import random
import statistics
import unittest
from pathlib import Path

from core.avatar_anim import (
    ANGRY, CONCERNED, ERROR, EXPRESSIONS, HAPPY, IDLE, INTERRUPTED, LISTENING,
    LOVE, NEUTRAL, SAD, SHY, SLEEPY, SPEAKING, SURPRISED, THINKING,
    AvatarAnimator, detect_expression, expression_from_text, normalise_expression,
    normalise_state,
)

ROOT = Path(__file__).resolve().parent.parent
LAYERS = ROOT / "gui" / "assets" / "rem"
DT = 1.0 / 60.0


def run(anim, seconds, **kw):
    """Step the animator and return the list of poses' (lid, mouth_open)."""
    out = []
    for _ in range(int(seconds / DT)):
        p = anim.step(DT, **kw)
        out.append((p.lid, p.mouth_open))
    return out


class MouthTests(unittest.TestCase):
    def setUp(self):
        self.a = AvatarAnimator(random.Random(1))

    def test_no_mouth_movement_unless_speaking(self):
        # playback-derived values present, but REM is not speaking (e.g. mic
        # listening): the mouth must stay shut.
        poses = run(self.a, 1.0, openness=0.9, width=0.0, level=0.8, speaking=False)
        self.assertEqual(max(m for _l, m in poses), 0.0)

    def test_mouth_tracks_playback_openness_when_speaking(self):
        poses = run(self.a, 0.6, openness=0.7, width=0.0, level=0.6, speaking=True)
        final = poses[-1][1]
        self.assertGreater(final, 0.55)
        self.assertLessEqual(final, 0.7 + 1e-6)

    def test_mouth_closes_after_speech_ends(self):
        run(self.a, 0.5, openness=0.8, level=0.7, speaking=True)
        poses = run(self.a, 0.45, openness=0.8, level=0.7, speaking=False)
        self.assertEqual(poses[-1][1], 0.0)

    def test_reset_motion_closes_mouth_on_the_same_frame_and_holds(self):
        run(self.a, 0.5, openness=0.8, level=0.7, speaking=True)
        self.assertGreater(self.a.pose.mouth_open, 0.4)
        self.a.reset_motion()                       # what an interruption calls
        self.assertEqual(self.a.pose.mouth_open, 0.0)
        # even if stale speaking data is still being delivered for a moment
        poses = run(self.a, 0.25, openness=0.8, level=0.7, speaking=True)
        self.assertEqual(max(m for _l, m in poses), 0.0)

    def test_interrupted_state_forces_closed_mouth(self):
        run(self.a, 0.5, openness=0.8, level=0.7, speaking=True, state="SPEAKING")
        self.a.step(DT, openness=0.8, level=0.7, speaking=True, state="INTERRUPTED")
        self.assertEqual(self.a.pose.mouth_open, 0.0)
        self.assertEqual(self.a.state, INTERRUPTED)

    def test_attack_and_release_are_smooth(self):
        prev, biggest = 0.0, 0.0
        for i in range(120):
            speaking = i < 60
            p = self.a.step(DT, openness=1.0, level=1.0, speaking=speaking)
            biggest = max(biggest, abs(p.mouth_open - prev))
            prev = p.mouth_open
        self.assertLess(biggest, 0.35)

    def test_20ms_flicker_is_damped(self):
        # alternating open/closed 20 ms frames (audio gaps) must not drive the
        # mouth through the full range every frame.
        vals = []
        for i in range(240):
            op = 0.9 if (i // 1) % 2 == 0 else 0.0
            p = self.a.step(1 / 50.0, openness=op, level=op, speaking=True)
            vals.append(p.mouth_open)
        tail = vals[120:]
        self.assertLess(max(tail) - min(tail), 0.6)

    def test_garbage_input_does_not_raise(self):
        p = self.a.step(DT, openness="x", width=None, level=object(), speaking=True)
        self.assertGreaterEqual(p.mouth_open, 0.0)


class BlinkTests(unittest.TestCase):
    def test_blinks_have_variable_intervals_and_full_closure(self):
        a = AvatarAnimator(random.Random(3))
        starts, prev_closed = [], False
        t = 0.0
        max_lid = 0.0
        for _ in range(int(180 / DT)):
            t += DT
            p = a.step(DT)
            max_lid = max(max_lid, p.lid)
            closed = p.lid > 0.5
            if closed and not prev_closed:
                starts.append(t)
            prev_closed = closed
        gaps = [b - a_ for a_, b in zip(starts, starts[1:])]
        self.assertGreater(len(starts), 20)
        self.assertLess(len(starts), 90)
        self.assertGreater(statistics.pstdev(gaps), 0.4)       # not mechanical
        self.assertAlmostEqual(max_lid, 1.0, places=2)         # fully closes

    def test_eyes_open_in_normal_idle(self):
        a = AvatarAnimator(random.Random(5))
        poses = run(a, 60.0)
        open_frames = sum(1 for lid, _m in poses if lid < 0.05)
        self.assertGreater(open_frames / len(poses), 0.9)

    def test_blink_envelope_is_smooth(self):
        a = AvatarAnimator(random.Random(9))
        a._next_blink = 0.0
        prev, biggest = 0.0, 0.0
        for _ in range(60):
            p = a.step(DT)
            biggest = max(biggest, abs(p.lid - prev))
            prev = p.lid
        self.assertLess(biggest, 0.7)

    def test_no_blink_while_expression_is_travelling(self):
        a = AvatarAnimator(random.Random(2))
        a.set_expression(CONCERNED, hold=5.0)
        a._next_blink = 0.0
        p = a.step(DT)                       # lid baseline has barely moved
        self.assertIsNone(a._blink_phase)
        self.assertLess(p.lid, 0.1)


class ExpressionTests(unittest.TestCase):
    def test_concerned_lowers_lids_and_expires(self):
        a = AvatarAnimator(random.Random(4))
        a.set_expression(CONCERNED, hold=1.0)
        run(a, 0.8)
        self.assertGreater(a.pose.lid, 0.25)
        run(a, 1.5)
        self.assertIsNone(a.expression)
        run(a, 1.0)
        self.assertLess(a.pose.lid, 0.15)

    def test_happy_raises_smile_only(self):
        a = AvatarAnimator(random.Random(4))
        a.set_expression(HAPPY, hold=5.0)
        run(a, 1.5)
        self.assertGreater(a.pose.smile, 0.9)
        self.assertLess(a.pose.lid, 0.2)

    def test_state_mapping_and_idle_motion(self):
        self.assertEqual(normalise_state("PROCESSING"), THINKING)
        self.assertEqual(normalise_state("listening"), LISTENING)
        self.assertEqual(normalise_state("INITIALISING"), IDLE)
        self.assertEqual(normalise_state(None), IDLE)
        self.assertEqual(normalise_state("ERROR"), ERROR)
        a = AvatarAnimator(random.Random(6))
        run(a, 2.0, state="THINKING")
        tilts = [a.step(DT, state="THINKING").tilt for _ in range(600)]
        self.assertGreater(max(tilts) - min(tilts), 0.2)
        self.assertLess(max(abs(t) for t in tilts), 1.5)        # stays subtle

    def test_text_cues_are_conservative(self):
        self.assertEqual(expression_from_text("I'm sorry, that failed."), CONCERNED)
        self.assertEqual(expression_from_text("I am so glad to help!"), HAPPY)
        self.assertIsNone(expression_from_text("The weather is mild today."))
        self.assertEqual(expression_from_text("Glad to say sorry it failed"), CONCERNED)

    def test_all_eight_expressions_and_independent_conversation_state(self):
        a = AvatarAnimator(random.Random(7))
        self.assertEqual(a.expression_state, NEUTRAL)
        self.assertEqual(a.state, IDLE)

        for expr in (HAPPY, SAD, ANGRY, SHY, SURPRISED, SLEEPY, LOVE, NEUTRAL):
            with self.subTest(expression=expr):
                a.set_expression(expr, hold=5.0)
                a.set_state(SPEAKING)
                run(a, 0.8, openness=0.6, level=0.5, speaking=True)
                self.assertEqual(a.expression_state, expr)
                self.assertEqual(a.state, SPEAKING)
                self.assertGreater(a.pose.expr_weights[expr], 0.85)
                self.assertGreater(a.pose.mouth_open, 0.2)

                # Changing conversation state to IDLE/LISTENING/THINKING preserves expression
                a.set_state(IDLE)
                run(a, 0.4, speaking=False)
                self.assertEqual(a.expression_state, expr)
                self.assertEqual(a.state, IDLE)
                self.assertEqual(a.pose.mouth_open, 0.0)

        # LOVE / AFFECTION alias normalizes to LOVE
        self.assertEqual(normalise_expression("Love / Affection"), LOVE)
        self.assertIsNone(normalise_expression("Neutral"))

    def test_sleepy_never_stays_permanently_closed_and_blinks_smoothly(self):
        a = AvatarAnimator(random.Random(11))
        a.set_expression(SLEEPY, hold=10.0)
        poses = run(a, 15.0)
        lids = [lid for lid, _ in poses]
        self.assertTrue(any(0.45 <= lid <= 0.65 for lid in lids))
        self.assertTrue(any(lid > 0.9 for lid in lids))
        self.assertLess(lids[-1], 0.7)

    def test_contextual_expression_detection_and_neutral_fallback(self):
        # Mentioning "sad" as a word/story does NOT trigger SAD
        self.assertEqual(detect_expression(user_message="Tell me what the word sad means"), NEUTRAL)
        self.assertEqual(detect_expression(user_message="Recommend a sad movie"), NEUTRAL)
        self.assertEqual(detect_expression(user_message="Don't be angry"), NEUTRAL)
        self.assertEqual(detect_expression(user_message="Run python -m unittest"), NEUTRAL)

        # Real conversational emotional cues
        self.assertEqual(detect_expression(user_message="Good morning REM, all tests passed!"), HAPPY)
        self.assertEqual(detect_expression(user_message="I'm feeling really sad and heartbroken today",
                                           response_text="I'm so sorry to hear that."), SAD)
        self.assertEqual(detect_expression(user_message="This bug is infuriating and unacceptable!"), ANGRY)
        self.assertEqual(detect_expression(user_message="You're so cute, REM!",
                                           response_text="Oh, you're making me blush!"), SHY)
        self.assertEqual(detect_expression(user_message="Wait what?! No way!"), SURPRISED)
        self.assertEqual(detect_expression(user_message="I'm exhausted, going to sleep now, good night."), SLEEPY)
        self.assertEqual(detect_expression(user_message="I love you REM, thank you from the heart.",
                                           response_text="I'll always be here for you."), LOVE)

    def test_speaking_while_sad_and_interruption_stops_mouth(self):
        a = AvatarAnimator(random.Random(12))
        a.set_expression(SAD, hold=6.0)
        run(a, 0.6, openness=0.75, level=0.7, speaking=True, state=SPEAKING)
        self.assertEqual(a.expression_state, SAD)
        self.assertEqual(a.state, SPEAKING)
        self.assertGreater(a.pose.mouth_open, 0.3)

        a.set_state(INTERRUPTED)
        self.assertEqual(a.pose.mouth_open, 0.0)
        self.assertEqual(a.expression_state, SAD)


class AssetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            from PIL import Image
            cls.Image = Image
        except ImportError:                      # pragma: no cover
            raise unittest.SkipTest("Pillow not installed (asset checks only)")

    def _open(self, path):
        with self.Image.open(path) as im:
            im.load()
            return im.copy()

    def test_manifest_matches_files(self):
        m = json.loads((LAYERS / "manifest.json").read_text())
        self.assertEqual(m["version"], 1)
        cw, ch = m["canvas"]["w"], m["canvas"]["h"]
        self.assertEqual(m["lid_levels"], sorted(m["lid_levels"]))
        for name, meta in m["layers"].items():
            im = self._open(LAYERS / meta["file"])
            self.assertEqual(im.mode, "RGBA", name)
            self.assertEqual(im.size, (meta["w"], meta["h"]), name)
            if name == "rem-base":
                self.assertEqual(im.size, (cw, ch))
            else:
                self.assertLessEqual(meta["x"] + meta["w"], cw, name)
                self.assertLessEqual(meta["y"] + meta["h"], ch, name)
        for need in ("rem-base", "rem-mouth-small", "rem-mouth-medium", "rem-mouth-open",
                     "rem-mouth-round", "rem-mouth-wide", "rem-mouth-smile",
                     "rem-eyes-lid035", "rem-eyes-lid070", "rem-eyes-lid100"):
            self.assertIn(need, m["layers"])

    def test_base_is_transparent_outside_the_character(self):
        base = self._open(LAYERS / "rem-base.png")
        w, h = base.size
        for xy in ((2, 2), (w - 3, 2), (2, h // 3), (w - 3, h // 3)):
            self.assertEqual(base.getpixel(xy)[3], 0, xy)
        self.assertGreaterEqual(base.getpixel((w // 2, int(h * 0.55)))[3], 240)   # face is opaque

    def test_closed_mouth_base_differs_from_open_mouth_layer(self):
        m = json.loads((LAYERS / "manifest.json").read_text())
        base = self._open(LAYERS / "rem-base.png").convert("RGB")
        meta = m["layers"]["rem-mouth-open"]
        op = self._open(LAYERS / meta["file"]).convert("RGBA")
        cx, cy = m["mouth_anchor"]
        # pink mouth interior pixel sits in the open layer, not in the closed base
        px_open = op.getpixel((cx - meta["x"], cy - meta["y"] + 6))
        self.assertGreater(px_open[3], 200)
        self.assertGreater(px_open[0] - px_open[1], 40)               # pink
        px_base = base.getpixel((cx, cy + 6))
        self.assertLess(px_base[0] - px_base[1], 40)                  # skin, not mouth


class RendererTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        try:
            from PyQt6.QtGui import QGuiApplication, QImage, QPainter, QColor
        except Exception as exc:                 # pragma: no cover
            raise unittest.SkipTest(f"PyQt6 unavailable: {exc}")
        cls.app = QGuiApplication.instance() or QGuiApplication([])
        cls.QImage, cls.QPainter, cls.QColor = QImage, QPainter, QColor
        from core.rem_avatar import RemLayeredAvatar
        cls.Avatar = RemLayeredAvatar

    def render(self, av, size=360):
        img = self.QImage(size, size, self.QImage.Format.Format_ARGB32_Premultiplied)
        img.fill(self.QColor(4, 14, 22))
        p = self.QPainter(img)
        c = self.QColor(0, 200, 255)
        av.paint(p, size / 2, size / 2 - 10, size * 0.4, c, c, self.QColor(4, 14, 22))
        p.end()
        return img

    def mouth_region_sum(self, img):
        s = 0
        for y in range(int(img.height() * 0.52), int(img.height() * 0.62)):
            for x in range(int(img.width() * 0.42), int(img.width() * 0.58)):
                px = img.pixelColor(x, y)
                s += px.red() * 3 + px.green() + px.blue()
        return s

    def test_paints_nontrivial_image_at_several_sizes(self):
        av = self.Avatar()
        for size in (200, 360, 700, 280):           # includes shrink after growth
            img = self.render(av, size)
            self.assertFalse(img.isNull())
            lit = sum(1 for x in range(0, size, 9) for y in range(0, size, 9)
                      if img.pixelColor(x, y).blue() > 120)
            self.assertGreater(lit, 50, size)

    def test_all_eight_expressions_render_distinctly_in_qt(self):
        av = self.Avatar()
        renders = {}
        for expr in EXPRESSIONS:
            av.set_expression(expr, hold=10.0)
            for _ in range(45):
                av.step(DT, speaking=False, state="IDLE")
            img = self.render(av, 360)
            # Hash downsampled pixel sum across whole canvas to verify distinct artwork
            total = sum(
                img.pixelColor(x, y).red() * 3 + img.pixelColor(x, y).green() * 5 + img.pixelColor(x, y).blue()
                for x in range(0, 360, 6) for y in range(0, 360, 6)
            )
            renders[expr] = total
        self.assertEqual(len(set(renders.values())), len(EXPRESSIONS))

    def test_open_mouth_renders_then_stops_after_reset(self):
        av = self.Avatar()
        for _ in range(60):
            av.step(DT, openness=0.9, width=0.0, level=0.8, speaking=True, state="SPEAKING")
        speaking = self.mouth_region_sum(self.render(av))
        av.reset_motion()
        av.step(DT, openness=0.9, level=0.8, speaking=True, state="SPEAKING")
        reset = self.mouth_region_sum(self.render(av))
        idle_av = self.Avatar()
        idle_av.step(DT, speaking=False, state="IDLE")
        idle = self.mouth_region_sum(self.render(idle_av))
        self.assertNotEqual(speaking, idle)          # open mouth looks different
        self.assertAlmostEqual(reset / idle, 1.0, delta=0.05)   # closed again at once

    def test_factory_falls_back_when_layers_missing(self):
        import core.rem_avatar as ra
        old = ra._LAYER_DIR
        ra._LAYER_DIR = Path("/nonexistent-rem-layers")
        try:
            self.assertEqual(type(ra.RemParticleAvatar()).__name__, "LegacyParticleAvatar")
        finally:
            ra._LAYER_DIR = old


if __name__ == "__main__":
    unittest.main()
