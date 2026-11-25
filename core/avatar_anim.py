"""Avatar animation controller — no Qt, no I/O, fully unit-testable.

The renderer (core/rem_avatar.py) owns pixels; this module owns *time*:

* blinking (variable intervals, occasional double blink, smooth envelope),
* mouth openness / width smoothing driven by the real playback schedule,
* conversation-state and expression targets (idle, listening, thinking,
  speaking, happy, concerned, interrupted, error),
* subtle idle motion (breathing, bob, slow head tilt).

Mouth input is whatever the HUD already derives from the PCM that is being
sent to the speaker (see HudCanvas._step): ``openness`` 0..1, ``width``
-1..1 (rounded .. spread) and ``level`` 0..1. This module never invents
mouth movement: with ``speaking=False`` the mouth is forced closed, and
``reset_motion()`` (called on interruption) closes it on the same frame.

Amplitude- and transcript-driven mouth shapes are an approximation of
speech, not phoneme-accurate lip-sync.
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass
from typing import Optional

# ── states ───────────────────────────────────────────────────────────────────
IDLE, LISTENING, THINKING, SPEAKING, INTERRUPTED, ERROR = (
    "IDLE", "LISTENING", "THINKING", "SPEAKING", "INTERRUPTED", "ERROR")
HAPPY, CONCERNED = "HAPPY", "CONCERNED"          # expressions (overlay on a state)

_STATE_ALIASES = {
    "SPEAKING": SPEAKING,
    "LISTENING": LISTENING,
    "THINKING": THINKING, "PROCESSING": THINKING,
    "INTERRUPTED": INTERRUPTED,
    "ERROR": ERROR, "FAILED": ERROR,
}

# blink interval range (seconds) per state
_BLINK_RANGE = {
    IDLE: (3.0, 6.5), LISTENING: (4.0, 8.0), THINKING: (2.2, 4.5),
    SPEAKING: (3.5, 7.0), INTERRUPTED: (3.0, 6.0), ERROR: (5.0, 9.0),
}
# eyelid baseline per expression (0 = open). 0.35 is the first lid frame.
_LID_BASE = {None: 0.0, HAPPY: 0.0, CONCERNED: 0.35}

_HAPPY_WORDS = ("glad", "happy", "wonderful", "great news", "congratulations",
                "delighted", "love that", "well done", "excellent", "pleased")
_CONCERN_WORDS = ("sorry", "unfortunately", "i couldn't", "i could not", "failed",
                  "cannot", "can't", "error", "problem", "worried", "apologi")


def normalise_state(state: Optional[str]) -> str:
    """Map a HUD state string to one of the avatar states."""
    return _STATE_ALIASES.get(str(state or "").strip().upper(), IDLE)


def expression_from_text(text: str) -> Optional[str]:
    """Very small, deliberately conservative sentiment cue for REM's own words.

    Returns HAPPY, CONCERNED or None. Concern wins when both appear.
    """
    t = (text or "").lower()
    if any(w in t for w in _CONCERN_WORDS):
        return CONCERNED
    if any(w in t for w in _HAPPY_WORDS):
        return HAPPY
    return None


def _smoothstep(v: float) -> float:
    v = max(0.0, min(1.0, v))
    return v * v * (3.0 - 2.0 * v)


def _approach(cur: float, target: float, dt: float, tau: float) -> float:
    return cur + (target - cur) * (1.0 - math.exp(-dt / max(1e-4, tau)))


@dataclass
class Pose:
    lid: float = 0.0            # 0 open .. 1 closed
    mouth_open: float = 0.0     # 0 closed .. 1 fully open
    mouth_width: float = 0.0    # -1 rounded (O) .. +1 spread (E)
    smile: float = 0.0          # 0..1, only visible while the mouth is shut
    scale: float = 1.0          # whole-portrait scale (breathing / lean)
    bob: float = 0.0            # vertical offset as a fraction of portrait height
    tilt: float = 0.0           # head tilt in degrees (whole portrait)
    shimmer: float = 0.0        # seconds, drives sparkle phase
    speaking_mix: float = 0.0   # 0..1 speech gate (for debugging/tests)


class AvatarAnimator:
    """Advance the avatar one tick at a time; read the result from ``pose``."""

    def __init__(self, rng: Optional[random.Random] = None) -> None:
        self._rng = rng or random.Random()
        self.state = IDLE
        self.expression: Optional[str] = None
        self._expr_until = 0.0
        self._clock = 0.0
        self._speech_mix = 0.0
        self._mouth_open = 0.0
        self._mouth_width = 0.0
        self._smile = 0.0
        self._lid_base = 0.0
        self._blink = 0.0
        self._blink_phase: Optional[float] = None
        self._blink_queue = 0                    # pending second blink of a double
        self._blink_clock = 0.0
        self._next_blink = self._draw_interval()
        self._scale = 1.0
        self._bob = 0.0
        self._tilt = 0.0
        self._interrupt_hold = 0.0
        self.pose = Pose()

    # ── inputs ───────────────────────────────────────────────────────────────
    def set_state(self, state: Optional[str]) -> None:
        new = normalise_state(state)
        if new == self.state:
            return
        self.state = new
        if new == INTERRUPTED:
            self.reset_motion()
        # a different state gets a fresh, state-appropriate blink interval
        self._next_blink = min(self._next_blink, self._draw_interval())

    def set_expression(self, name: Optional[str], hold: float = 6.0) -> None:
        """Overlay HAPPY / CONCERNED for ``hold`` seconds (None clears it)."""
        name = (name or "").upper() or None
        self.expression = name if name in (HAPPY, CONCERNED) else None
        self._expr_until = self._clock + max(0.0, hold) if self.expression else 0.0

    def reset_motion(self) -> None:
        """Interruption / cancellation: stop the mouth on this very frame."""
        self._speech_mix = 0.0
        self._mouth_open = 0.0
        self._mouth_width = 0.0
        self._interrupt_hold = 0.30
        self.pose.mouth_open = 0.0
        self.pose.mouth_width = 0.0
        self.pose.speaking_mix = 0.0
        # keep an in-flight blink: cutting it mid-way would make the lids pop

    # ── helpers ──────────────────────────────────────────────────────────────
    def _draw_interval(self) -> float:
        lo, hi = _BLINK_RANGE.get(self.state, _BLINK_RANGE[IDLE])
        return self._rng.uniform(lo, hi)

    def _blink_allowed(self) -> bool:
        # No blink while the eyelid baseline is still travelling (expression
        # change) or right after an interruption — avoids stacked lid motion.
        return (abs(self._lid_base - _LID_BASE.get(self.expression, 0.0)) < 0.04
                and self._interrupt_hold <= 0.0)

    # ── tick ─────────────────────────────────────────────────────────────────
    def step(self, dt: float, openness: Optional[float] = None,
             width: Optional[float] = None, level: Optional[float] = None,
             speaking: bool = False, state: Optional[str] = None,
             **_unused) -> Pose:
        dt = max(0.0, min(0.1, float(dt)))
        self._clock += dt
        if state is not None:
            self.set_state(state)
        if self.expression and self._clock >= self._expr_until:
            self.expression = None
        self._interrupt_hold = max(0.0, self._interrupt_hold - dt)

        # ---- mouth: only ever driven by a live playback signal --------------
        try:
            lv = max(0.0, min(1.0, float(level))) if level is not None else 0.0
            raw_open = max(0.0, min(1.0, float(openness))) if openness is not None else None
            raw_w = max(-1.0, min(1.0, float(width))) if width is not None else 0.0
        except (TypeError, ValueError):
            lv, raw_open, raw_w = 0.0, None, 0.0
        active = bool(speaking) and self.state not in (INTERRUPTED,) \
            and self._interrupt_hold <= 0.0
        self._speech_mix = _approach(self._speech_mix, 1.0 if active else 0.0, dt,
                                     0.05 if active else 0.07)
        if raw_open is None:
            raw_open = min(0.82, 0.86 * math.sqrt(lv))
        if lv <= 0.005:
            raw_open = 0.0
        target_open = raw_open * self._speech_mix if active else 0.0
        self._mouth_open = _approach(self._mouth_open, target_open, dt,
                                     0.045 if target_open > self._mouth_open else 0.09)
        self._mouth_width = _approach(self._mouth_width, raw_w if active else 0.0, dt, 0.07)
        if self._mouth_open < 0.015:
            self._mouth_open = 0.0
        if not active and self._speech_mix < 0.01:
            self._speech_mix = 0.0

        # ---- expression targets ------------------------------------------------
        lid_target = _LID_BASE.get(self.expression, 0.0)
        smile_target = 1.0 if self.expression == HAPPY else 0.0
        self._lid_base = _approach(self._lid_base, lid_target, dt, 0.14)
        self._smile = _approach(self._smile, smile_target, dt, 0.22)

        # ---- blinking ----------------------------------------------------------
        self._blink_clock += dt
        if self._blink_phase is None and self._blink_clock >= self._next_blink:
            if self._blink_allowed():
                self._blink_phase = 0.0
                self._blink_clock = 0.0
                self._blink_queue = 1 if self._rng.random() < 0.14 else 0
            else:
                self._next_blink = self._blink_clock + 0.3     # try again shortly
        if self._blink_phase is not None:
            self._blink_phase += dt
            ph = self._blink_phase
            close_t, hold_t, open_t = 0.075, 0.035, 0.105
            if ph < close_t:
                self._blink = _smoothstep(ph / close_t)
            elif ph < close_t + hold_t:
                self._blink = 1.0
            elif ph < close_t + hold_t + open_t:
                self._blink = 1.0 - _smoothstep((ph - close_t - hold_t) / open_t)
            else:
                self._blink = 0.0
                self._blink_phase = None
                self._blink_clock = 0.0
                if self._blink_queue:
                    self._blink_queue = 0
                    self._next_blink = 0.18            # quick second blink
                else:
                    self._next_blink = self._draw_interval()

        # ---- idle motion (calm; whole-portrait only — see readme for limits) ---
        t = self._clock
        breathe = 0.0035 * math.sin(t * 2.0 * math.pi / 4.2)
        scale_t, bob_t, tilt_t = 1.0 + breathe, 0.0025 * math.sin(t * 2.0 * math.pi / 4.2 + 0.6), 0.0
        if self.state == LISTENING:
            scale_t += 0.010            # leans in slightly
            bob_t -= 0.004
        elif self.state == THINKING:
            tilt_t = 0.9 * math.sin(t * 2.0 * math.pi / 6.5)
        elif self.state == SPEAKING:
            bob_t += 0.0025 * self._mouth_open     # tiny nod with emphasis
        elif self.state == ERROR:
            scale_t, bob_t = 1.0, 0.0
        if self.expression == CONCERNED:
            bob_t += 0.003
        self._scale = _approach(self._scale, scale_t, dt, 0.35)
        self._bob = _approach(self._bob, bob_t, dt, 0.35)
        self._tilt = _approach(self._tilt, tilt_t, dt, 0.5)

        lid = self._lid_base + self._blink * (1.0 - self._lid_base)
        p = self.pose
        p.lid = max(0.0, min(1.0, lid))
        p.mouth_open = self._mouth_open
        p.mouth_width = self._mouth_width
        p.smile = self._smile
        p.scale, p.bob, p.tilt = self._scale, self._bob, self._tilt
        p.shimmer = self._clock
        p.speaking_mix = self._speech_mix
        return p
