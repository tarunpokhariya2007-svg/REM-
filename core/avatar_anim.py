"""Avatar animation controller — no Qt, no I/O, fully unit-testable.

The renderer (core/rem_avatar.py) owns pixels; this module owns *time*:

* blinking (variable intervals, occasional double blink, smooth envelope),
* mouth openness / width smoothing driven by the real playback schedule,
* independent conversation-state and 8-expression targets
  (NEUTRAL, HAPPY, SAD, ANGRY, SHY, SURPRISED, SLEEPY, LOVE),
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
import re
from dataclasses import dataclass, field
from typing import Iterable, Optional

# ── conversation states ──────────────────────────────────────────────────────
IDLE, LISTENING, THINKING, SPEAKING, INTERRUPTED, ERROR = (
    "IDLE", "LISTENING", "THINKING", "SPEAKING", "INTERRUPTED", "ERROR")

# ── emotional expressions (independent of conversation state) ────────────────
NEUTRAL = "NEUTRAL"
HAPPY = "HAPPY"
SAD = "SAD"
ANGRY = "ANGRY"
SHY = "SHY"
SURPRISED = "SURPRISED"
SLEEPY = "SLEEPY"
LOVE = "LOVE"
CONCERNED = "CONCERNED"  # legacy alias kept for existing callers/tests

EXPRESSIONS = (NEUTRAL, HAPPY, SAD, ANGRY, SHY, SURPRISED, SLEEPY, LOVE)
NON_NEUTRAL_EXPRESSIONS = (HAPPY, SAD, ANGRY, SHY, SURPRISED, SLEEPY, LOVE)

EXPLICIT_USER_COMMAND = "EXPLICIT_USER_COMMAND"
CONTEXTUAL_EMOTION_DETECTION = "CONTEXTUAL_EMOTION_DETECTION"
DEFAULT_NEUTRAL = "DEFAULT_NEUTRAL"

_EXPR_SOURCE_PRIORITY = {
    EXPLICIT_USER_COMMAND: 3,
    CONTEXTUAL_EMOTION_DETECTION: 2,
    DEFAULT_NEUTRAL: 1,
}

_EXPRESSION_ALIASES = {
    "NEUTRAL": NEUTRAL, "NONE": NEUTRAL, "DEFAULT": NEUTRAL, "CALM": NEUTRAL,
    "HAPPY": HAPPY, "JOY": HAPPY, "EXCITED": HAPPY, "CHEERFUL": HAPPY,
    "SAD": SAD, "SORRY": SAD, "UPSET": SAD, "DISAPPOINTED": SAD,
    "ANGRY": ANGRY, "MAD": ANGRY, "FRUSTRATED": ANGRY, "ANNOYED": ANGRY,
    "SHY": SHY, "EMBARRASSED": SHY, "BASHFUL": SHY, "BLUSH": SHY, "BLUSHING": SHY,
    "SURPRISED": SURPRISED, "SHOCKED": SURPRISED, "ASTONISHED": SURPRISED, "AMAZED": SURPRISED,
    "SLEEPY": SLEEPY, "TIRED": SLEEPY, "DROWSY": SLEEPY,
    "LOVE": LOVE, "AFFECTION": LOVE, "LOVE / AFFECTION": LOVE, "LOVE/AFFECTION": LOVE,
    "LOVE_AFFECTION": LOVE, "WARM": LOVE, "CARING": LOVE,
    "CONCERNED": CONCERNED,
}

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
_LID_BASE = {
    None: 0.0,
    NEUTRAL: 0.0,
    HAPPY: 0.0,
    SAD: 0.16,
    ANGRY: 0.0,
    SHY: 0.18,
    SURPRISED: 0.0,
    SLEEPY: 0.52,
    LOVE: 0.0,
    CONCERNED: 0.35,
}

_HAPPY_WORDS = ("glad", "happy", "wonderful", "great news", "congratulations",
                "delighted", "love that", "well done", "excellent", "pleased")
_CONCERN_WORDS = ("sorry", "unfortunately", "i couldn't", "i could not", "failed",
                  "cannot", "can't", "error", "problem", "worried", "apologi")

_META_MENTION_RE = re.compile(
    r"\b(?:word|meaning|definition|define|story|movie|film|song|poem|book|"
    r"essay|example|translate|translation|spell|character|anime)\b",
    re.IGNORECASE,
)


def normalise_state(state: Optional[str]) -> str:
    """Map a HUD state string to one of the avatar states."""
    return _STATE_ALIASES.get(str(state or "").strip().upper(), IDLE)


def normalise_expression(name: Optional[str]) -> Optional[str]:
    """Map an expression label/alias to a canonical expression, or None for NEUTRAL/unknown."""
    key = " ".join(str(name or "").strip().upper().split())
    if not key:
        return None
    mapped = _EXPRESSION_ALIASES.get(key)
    if mapped == NEUTRAL:
        return None
    return mapped


_EXPLICIT_CMD_NEUTRAL = re.compile(
    r"\b(?:return\s+to\s+normal|go\s+back\s+to\s+normal|back\s+to\s+normal|"
    r"normal\s+(?:face|expression|look|mood)|neutral\s+(?:face|expression|look)|"
    r"(?:reset|clear)\s+(?:your\s+)?(?:face|expression)|"
    r"(?:be|look|act|stay)\s+(?:normal|neutral))\b",
    re.IGNORECASE,
)

_EXPLICIT_CMD_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (HAPPY, re.compile(
        r"^(?:please\s+|hey\s+rem[\s,]+|rem[\s,]+)?(?:smile|grin)(?:\s+(?:please|for\s+me|now))?[.!?]*$"
        r"|\b(?:give\s+me\s+a\s+smile|show\s+(?:me\s+)?(?:a\s+)?(?:happy|smiling)\s+(?:face|expression)|"
        r"can\s+you\s+smile|(?:please|now)\s+smile|smile\s+for\s+me|"
        r"(?:show|make)\s+(?:me\s+)?(?:a\s+|an\s+|your\s+)?happy\s+(?:face|expression)|"
        r"(?:look|be|act)\s+happy)\b",
        re.IGNORECASE,
    )),
    (SAD, re.compile(
        r"\b(?:can\s+you\s+cry|start\s+crying|cry\s+for\s+me|"
        r"(?:show|make)\s+(?:me\s+)?(?:a\s+|an\s+|your\s+)?(?:sad|crying|tearful)\s+(?:face|expression)|"
        r"(?:look|be|act)\s+(?:sad|tearful)|show\s+(?:me\s+)?tears)\b"
        r"|^(?:please\s+|hey\s+rem[\s,]+|rem[\s,]+)?cry(?:\s+(?:please|for\s+me|now))?[.!?]*$",
        re.IGNORECASE,
    )),
    (ANGRY, re.compile(
        r"\b(?:(?:show|make)\s+(?:me\s+)?(?:a\s+|an\s+|your\s+)?(?:angry|mad|annoyed|upset)\s+(?:face|expression)|"
        r"(?:look|be|act)\s+(?:angry|mad)|pout\s+for\s+me|can\s+you\s+(?:look|act|be)\s+angry)\b",
        re.IGNORECASE,
    )),
    (SHY, re.compile(
        r"\b(?:(?:show|make)\s+(?:me\s+)?(?:a\s+|an\s+|your\s+)?(?:shy|blushing|bashful|embarrassed)\s+(?:face|expression)|"
        r"(?:look|be|act)\s+(?:shy|bashful|embarrassed)|"
        r"can\s+you\s+blush|(?:please\s+)?blush(?:\s+for\s+me)?)\b",
        re.IGNORECASE,
    )),
    (SURPRISED, re.compile(
        r"\b(?:(?:show|make)\s+(?:me\s+)?(?:a\s+|an\s+|your\s+)?(?:surprised|shocked|amazed)\s+(?:face|expression)|"
        r"(?:look|be|act)\s+(?:surprised|shocked))\b",
        re.IGNORECASE,
    )),
    (SLEEPY, re.compile(
        r"\b(?:(?:show|make)\s+(?:me\s+)?(?:a\s+|an\s+|your\s+)?(?:sleepy|tired|drowsy)\s+(?:face|expression)|"
        r"(?:look|be|act)\s+(?:sleepy|drowsy|tired)|can\s+you\s+(?:look\s+sleepy|yawn))\b",
        re.IGNORECASE,
    )),
    (LOVE, re.compile(
        r"\b(?:(?:show|make)\s+(?:me\s+)?(?:a\s+|an\s+|your\s+)?(?:love|affection|affectionate|loving)\s+(?:face|expression)|"
        r"show\s+(?:me\s+)?(?:love|affection)|(?:look|be|act)\s+(?:affectionate|loving))\b",
        re.IGNORECASE,
    )),
)


def parse_explicit_expression_command(text: str) -> Optional[str]:
    """Return canonical expression if user explicitly asked REM to show/reset one."""
    cleaned = " ".join(str(text or "").split()).strip()
    if not cleaned:
        return None
    if _EXPLICIT_CMD_NEUTRAL.search(cleaned):
        return NEUTRAL
    for expr, pattern in _EXPLICIT_CMD_PATTERNS:
        m = pattern.search(cleaned)
        if m and not _is_negated_or_meta(cleaned, m.group(0).strip().split()[-1]):
            # Also ignore explicit request negation like "don't smile" / "stop smiling"
            if re.search(rf"\b(?:don't|do\s+not|dont|never|stop)\s+(?:\w+\s+){{0,2}}{re.escape(m.group(0).strip().split()[0])}\b", cleaned, re.IGNORECASE):
                continue
            return expr
    return None


def expression_from_text(text: str) -> Optional[str]:
    """Conservative sentiment cue for single-string callers.

    Returns HAPPY, CONCERNED or None. Concern wins when both appear.
    """
    t = (text or "").lower()
    if any(w in t for w in _CONCERN_WORDS):
        return CONCERNED
    if any(w in t for w in _HAPPY_WORDS):
        return HAPPY
    return None


def _is_negated_or_meta(text: str, term: str) -> bool:
    """Reject isolated keyword hits that are negated or discussed as words/fiction."""
    low = text.casefold()
    if _META_MENTION_RE.search(low):
        return True
    escaped = re.escape(term)
    if re.search(rf"\b(?:not|never|don't|dont|do not|isn't|isnt|aren't|no|without)\s+(?:\w+\s+){{0,2}}{escaped}\b", low):
        return True
    if re.search(rf"['\"“”]{escaped}['\"“”]", low):
        return True
    return False


def detect_expression(
    *,
    user_message: str = "",
    response_text: str = "",
    recent_turns: Iterable[str] = (),
    conversation_state: Optional[str] = None,
    current_expression: Optional[str] = None,
    personality: Optional[object] = None,
) -> str:
    """Select one of the eight canonical expressions from conversational context.

    Considers the user's message, recent turns, REM's intended response,
    conversation state, and personality without relying blindly on isolated
    keywords or time of day. Falls back to NEUTRAL when uncertain.
    """
    user = " ".join(str(user_message or "").split())
    reply = " ".join(str(response_text or "").split())
    recent = " ".join(str(x or "") for x in list(recent_turns or ())[-4:]).strip()
    state = normalise_state(conversation_state)
    current = normalise_expression(current_expression) or NEUTRAL

    if not user and not reply:
        return current if current != NEUTRAL else NEUTRAL

    # Purely technical/command phrasing with no emotional intent stays NEUTRAL.
    combined = f"{user}\n{reply}".strip()
    if not combined:
        return NEUTRAL

    scores: dict[str, float] = {e: 0.0 for e in NON_NEUTRAL_EXPRESSIONS}

    def add_hits(text: str, weight: float, table: dict[str, tuple[tuple[str, float], ...]]) -> None:
        if not text:
            return
        low = text.casefold()
        for expr, cues in table.items():
            for phrase, pts in cues:
                if " " in phrase:
                    hit = phrase in low
                else:
                    hit = bool(re.search(rf"\b{re.escape(phrase)}\b", low))
                if hit and not _is_negated_or_meta(text, phrase):
                    scores[expr] += pts * weight

    user_cues = {
        HAPPY: (
            ("good morning", 1.8), ("good afternoon", 1.6), ("good evening", 1.6),
            ("welcome back", 2.0), ("great job", 2.2), ("well done", 2.2),
            ("all tests passed", 2.4), ("awesome", 1.6), ("yay", 1.8),
            ("congratulations", 2.2), ("i'm so happy", 2.4), ("we did it", 2.2),
            ("hello rem", 1.4), ("hi rem", 1.4), ("hey rem", 1.2),
        ),
        SAD: (
            ("i'm sad", 2.4), ("i feel sad", 2.5), ("feeling down", 2.2),
            ("bad news", 2.0), ("i failed", 2.0), ("heartbroken", 2.6),
            ("i miss you", 1.8), ("feel terrible", 2.3), ("lost my", 1.7),
            ("disappointed", 2.0), ("feeling lonely", 2.4), ("crying", 2.2),
        ),
        ANGRY: (
            ("i'm angry", 2.4), ("makes me angry", 2.5), ("infuriating", 2.4),
            ("so frustrated", 2.2), ("annoying", 1.6), ("unacceptable", 2.2),
            ("stop doing that", 2.0), ("this is ridiculous", 2.0), ("pissed off", 2.4),
            ("how dare", 2.3),
        ),
        SHY: (
            ("you're cute", 2.8), ("you are cute", 2.8), ("you look cute", 2.8),
            ("you're pretty", 2.7), ("you look pretty", 2.7), ("you're adorable", 2.8),
            ("you're the best", 2.3), ("you're amazing", 2.1), ("good girl", 2.7),
            ("blushing", 2.4), ("embarrassing", 1.8), ("you're gorgeous", 2.8),
        ),
        SURPRISED: (
            ("wait what", 2.3), ("no way", 2.2), ("what?!", 2.4), ("really?!", 2.3),
            ("are you serious", 2.1), ("didn't expect", 2.2), ("unexpected", 1.9),
            ("whoa", 2.0), ("surprise", 2.0), ("shocked", 2.1),
        ),
        SLEEPY: (
            ("going to sleep", 2.6), ("going to bed", 2.6), ("i'm sleepy", 2.7),
            ("so sleepy", 2.7), ("i'm exhausted", 2.4), ("need a nap", 2.4),
            ("good night", 2.4), ("goodnight", 2.4), ("feeling drowsy", 2.5),
            ("i'm tired", 2.0),
        ),
        LOVE: (
            ("i love you", 3.0), ("love you", 2.8), ("adore you", 2.8),
            ("you mean so much", 2.6), ("grateful to have you", 2.4),
            ("always care", 2.2), ("you're so sweet", 2.4), ("cherish", 2.4),
            ("thank you from the heart", 2.4), ("my dear", 2.2),
        ),
    }

    reply_cues = {
        HAPPY: (
            ("glad to help", 2.0), ("happy to help", 2.0), ("wonderful", 1.8),
            ("delighted", 2.2), ("great news", 2.2), ("congratulations", 2.3),
            ("well done", 2.0), ("welcome back", 2.0), ("cheerfully", 1.8),
            ("pleased to", 1.7),
        ),
        SAD: (
            ("so sorry to hear", 2.6), ("i'm sorry for", 2.2), ("that sounds painful", 2.4),
            ("that is heartbreaking", 2.6), ("unfortunately", 1.6),
            ("i wish i could take that away", 2.4), ("disappointing", 1.8),
        ),
        ANGRY: (
            ("unacceptable", 2.2), ("how infuriating", 2.4), ("that is frustrating", 2.0),
            ("i will not tolerate", 2.4),
        ),
        SHY: (
            ("you're making me blush", 3.0), ("oh, tarun-kun", 1.9), ("oh, master", 1.9), ("that's very flattering", 2.6),
            ("you're too kind", 2.4), ("flustered", 2.6), ("embarrassed", 2.3),
            ("oh my", 1.4),
        ),
        SURPRISED: (
            ("oh!", 1.5), ("my goodness", 2.2), ("i didn't expect", 2.4),
            ("what a surprise", 2.5), ("astonishing", 2.2), ("remarkable", 1.5),
        ),
        SLEEPY: (
            ("sweet dreams", 2.5), ("restful sleep", 2.5), ("sleep well", 2.4),
            ("good night", 2.2), ("get some rest", 2.0), ("yawn", 2.4),
        ),
        LOVE: (
            ("always here for you", 2.6), ("with all my heart", 2.8),
            ("care deeply", 2.6), ("cherish", 2.5), ("dearly", 2.3),
            ("warmly", 1.8), ("affection", 2.4), ("take good care of yourself", 2.0),
            ("thank you, tarun-kun", 1.6), ("thank you, master", 1.6),
        ),
    }

    add_hits(user, 1.0, user_cues)
    add_hits(reply, 1.15, reply_cues)
    if recent:
        add_hits(recent, 0.35, user_cues)

    # Personality weighting: REM is warm/gentle/polite, rarely angry unless provoked.
    p_text = str(personality or "").casefold()
    if "rem" in p_text or "gentle" in p_text or not p_text:
        scores[ANGRY] *= 0.85
        scores[LOVE] *= 1.05

    # State guard: while thinking through a routine task, avoid jumping to extreme
    # emotions from weak signals.
    threshold = 1.9 if state == THINKING else 1.55

    best_expr = max(scores, key=lambda k: scores[k])
    best_score = scores[best_expr]
    if best_score < threshold:
        return NEUTRAL

    # Anti-flicker hysteresis: keep current expression if the new contender is
    # only marginally higher.
    if current in scores and current != best_expr and scores[current] >= 1.2:
        if best_score < scores[current] + 0.6:
            return current

    return best_expr


def _smoothstep(v: float) -> float:
    v = max(0.0, min(1.0, v))
    return v * v * (3.0 - 2.0 * v)


def _approach(cur: float, target: float, dt: float, tau: float) -> float:
    return cur + (target - cur) * (1.0 - math.exp(-dt / max(1e-4, tau)))


@dataclass
class Pose:
    lid: float = 0.0            # 0 open .. 1 closed
    blink: float = 0.0          # 0 .. 1 blink pulse envelope
    mouth_open: float = 0.0     # 0 closed .. 1 fully open
    mouth_width: float = 0.0    # -1 rounded (O) .. +1 spread (E)
    smile: float = 0.0          # 0..1, only visible while the mouth is shut
    scale: float = 1.0          # whole-portrait scale (breathing / lean)
    bob: float = 0.0            # vertical offset as a fraction of portrait height
    tilt: float = 0.0           # head tilt in degrees (whole portrait)
    shimmer: float = 0.0        # seconds, drives sparkle / fx phase
    speaking_mix: float = 0.0   # 0..1 speech gate (for debugging/tests)
    expression: str = NEUTRAL   # canonical expression state
    conversation_state: str = IDLE
    expr_weights: dict[str, float] = field(default_factory=lambda: {
        e: (1.0 if e == NEUTRAL else 0.0) for e in EXPRESSIONS
    })


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
        self._expr_source = DEFAULT_NEUTRAL
        self._expr_weights = {e: (1.0 if e == NEUTRAL else 0.0) for e in EXPRESSIONS}
        self.pose = Pose()

    @property
    def expression_state(self) -> str:
        """Canonical active expression name (returns NEUTRAL when cleared)."""
        if self.expression == CONCERNED:
            return SAD
        return self.expression or NEUTRAL

    @property
    def expression_source(self) -> str:
        """Priority source of the currently active expression."""
        return self._expr_source

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

    def set_expression(self, name: Optional[str], hold: Optional[float] = 6.0,
                       source: str = EXPLICIT_USER_COMMAND) -> None:
        """Set one of the 8 emotional expressions (or None / NEUTRAL to clear).

        Priority rule:
          EXPLICIT_USER_COMMAND > CONTEXTUAL_EMOTION_DETECTION > DEFAULT_NEUTRAL.
        Contextual emotion detection must never overwrite an active explicit
        user command until that command expires or is explicitly cleared.
        """
        src = source if source in _EXPR_SOURCE_PRIORITY else EXPLICIT_USER_COMMAND
        if (src == CONTEXTUAL_EMOTION_DETECTION
                and self._expr_source == EXPLICIT_USER_COMMAND
                and self.expression is not None
                and self._clock < self._expr_until):
            return
        mapped = normalise_expression(name)
        self.expression = mapped
        if not self.expression:
            self._expr_until = 0.0
            self._expr_source = DEFAULT_NEUTRAL
        else:
            self._expr_source = src
            if hold is None or (isinstance(hold, float) and math.isinf(hold)):
                self._expr_until = float("inf")
            else:
                self._expr_until = self._clock + max(0.0, float(hold))

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
        if self.expression == SLEEPY:
            lo, hi = lo * 0.75, hi * 0.75
        elif self.expression == SURPRISED:
            lo, hi = lo * 1.25, hi * 1.25
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
            self._expr_source = DEFAULT_NEUTRAL
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

        # ---- expression targets (independent of conversation state) -----------
        expr_key = self.expression_state
        for e in EXPRESSIONS:
            target_w = 1.0 if e == expr_key else 0.0
            self._expr_weights[e] = _approach(self._expr_weights[e], target_w, dt, 0.16)
            if self._expr_weights[e] < 0.005:
                self._expr_weights[e] = 0.0

        lid_target = _LID_BASE.get(self.expression, 0.0)
        smile_target = 1.0 if self.expression in (HAPPY, LOVE) else 0.0
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

        # ---- idle motion (calm; whole-portrait only) --------------------------
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

        # Subtle expression posture nudges (never overrides conversation state)
        if expr_key == HAPPY or expr_key == LOVE:
            bob_t -= 0.0025 * self._expr_weights.get(expr_key, 0.0)
        elif expr_key == SAD or self.expression == CONCERNED:
            bob_t += 0.0035 * max(self._expr_weights.get(SAD, 0.0), 0.7 if self.expression == CONCERNED else 0.0)
        elif expr_key == SHY:
            tilt_t += 0.6 * self._expr_weights.get(SHY, 0.0)
        elif expr_key == SLEEPY:
            bob_t += 0.003 * self._expr_weights.get(SLEEPY, 0.0)
            tilt_t += 0.7 * math.sin(t * 0.9) * self._expr_weights.get(SLEEPY, 0.0)
        elif expr_key == SURPRISED:
            scale_t += 0.008 * self._expr_weights.get(SURPRISED, 0.0)

        self._scale = _approach(self._scale, scale_t, dt, 0.35)
        self._bob = _approach(self._bob, bob_t, dt, 0.35)
        self._tilt = _approach(self._tilt, tilt_t, dt, 0.5)

        lid = self._lid_base + self._blink * (1.0 - self._lid_base)
        p = self.pose
        p.lid = max(0.0, min(1.0, lid))
        p.blink = max(0.0, min(1.0, self._blink))
        p.mouth_open = self._mouth_open
        p.mouth_width = self._mouth_width
        p.smile = self._smile
        p.scale, p.bob, p.tilt = self._scale, self._bob, self._tilt
        p.shimmer = self._clock
        p.speaking_mix = self._speech_mix
        p.expression = expr_key
        p.conversation_state = self.state
        p.expr_weights = dict(self._expr_weights)
        return p
