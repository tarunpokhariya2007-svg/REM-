"""Small synchronized state tracker for the live voice conversation lifecycle."""
from __future__ import annotations

import threading
from enum import Enum


class VoicePhase(str, Enum):
    IDLE = "IDLE"
    LISTENING = "LISTENING"
    THINKING = "THINKING"
    GENERATING_AUDIO = "GENERATING_AUDIO"
    SPEAKING = "SPEAKING"
    USER_INTERRUPTION = "USER_INTERRUPTION"
    CANCELLING = "CANCELLING"
    STOPPED = "STOPPED"


class VoiceState:
    def __init__(self):
        self._lock = threading.RLock()
        self._phase = VoicePhase.IDLE
        self._transitions = 0

    def set(self, phase: VoicePhase | str) -> VoicePhase:
        phase = VoicePhase(phase)
        with self._lock:
            if self._phase != phase:
                self._phase = phase
                self._transitions += 1
            return self._phase

    @property
    def phase(self) -> VoicePhase:
        with self._lock:
            return self._phase

    @property
    def transitions(self) -> int:
        with self._lock:
            return self._transitions


class BargeInGate:
    """Debounce EchoGuard's per-frame decisions before interrupting playback."""

    def __init__(self):
        self._lock = threading.Lock()
        self._positive = 0

    def observe(self, is_user_speech: bool, required_blocks: int) -> bool:
        with self._lock:
            self._positive = self._positive + 1 if is_user_speech else 0
            if self._positive < max(1, int(required_blocks)):
                return False
            self._positive = 0
            return True

    def reset(self) -> None:
        with self._lock:
            self._positive = 0
