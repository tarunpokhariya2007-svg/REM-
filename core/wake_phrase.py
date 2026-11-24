"""Local, phrase-constrained wake detection for REM.

Audio reaches this detector only while the assistant is asleep. It is never
saved or sent over the network. A short energy gate buffers only speech-sized
segments, and Vosk is constrained to the one activation phrase.
"""
from __future__ import annotations

import importlib.util
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path
from typing import Callable

WAKE_PHRASE = "REM, where are you?"
_MODEL_NAME = "vosk-model-small-en-us-0.15"
_SAMPLE_RATE = 16000
_ENERGY_THRESHOLD = 300
_SILENCE_FRAMES = 8              # about half a second with the normal mic block size
_MAX_SEGMENT_SECONDS = 4.0
_COOLDOWN_SECONDS = 3.0
_PRE_ROLL_FRAMES = 5


def _model_directories() -> list[Path]:
    result = []
    env_path = os.environ.get("VOSK_MODEL_PATH")
    if env_path:
        result.append(Path(env_path))
    result.extend((Path.home() / "AppData/Local/vosk", Path.home() / ".cache/vosk",
                   Path("/usr/share/vosk")))
    return result


def _model_path() -> Path | None:
    for directory in _model_directories():
        candidate = directory / _MODEL_NAME
        if candidate.is_dir():
            return candidate
    return None


def is_installed() -> bool:
    try:
        return importlib.util.find_spec("vosk") is not None
    except (ImportError, ValueError):
        return False


def is_ready() -> bool:
    """Cheap readiness check; model loading and vocabulary checks happen on start."""
    return is_installed() and _model_path() is not None


def install_and_download(logger: Callable[[str], None] = print,
                         notify: Callable[[str], None] | None = None) -> tuple[bool, str]:
    """Install Vosk and its small local English model after explicit UI action."""
    tell = notify or (lambda _message: None)
    try:
        if not is_installed():
            message = "Installing the local REM phrase recognizer…"
            logger(message)
            tell(message)
            result = subprocess.run(
                [sys.executable, "-m", "pip", "install", "vosk"],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
            )
            if result.returncode:
                detail = (result.stderr or result.stdout or "").strip().splitlines()
                return False, f"Vosk installation failed: {(detail[-1] if detail else '')[:160]}"
        message = "Downloading the local English phrase model…"
        logger(message)
        tell(message)
        from vosk import Model
        # Vosk downloads this named public model into its normal user model cache.
        model = Model(model_name=_MODEL_NAME)
        if model.vosk_model_find_word("rem") < 0:
            return False, "The downloaded model vocabulary does not contain ‘REM’; phrase detection is unavailable."
        del model
        return True, "Local phrase model is ready. Microphone phrase detection still needs testing on this device."
    except Exception as exc:
        return False, f"Phrase detector setup failed: {exc}"


def _normalize(text: str) -> str:
    return " ".join(re.findall(r"[a-z]+", text.lower()))


def _is_exact_phrase(text: str) -> bool:
    return _normalize(text) == "rem where are you"


class WakePhraseDetector:
    """Phrase-only local recognizer running off the real-time microphone thread."""

    def __init__(self, on_detect: Callable[[], None],
                 logger: Callable[[str], None] = print,
                 notify: Callable[[str], None] | None = None):
        self._on_detect = on_detect
        self._logger = logger
        self._notify = notify or (lambda _message: None)
        self._queue: queue.Queue = queue.Queue(maxsize=64)
        self._thread: threading.Thread | None = None
        self._running = False
        self._ready = False
        self._reset_requested = threading.Event()
        self._recognizer = None
        self._model = None
        self._last_trigger = 0.0

    @property
    def ready(self) -> bool:
        return self._ready

    def start(self) -> bool:
        if self._running:
            return True
        try:
            from vosk import KaldiRecognizer, Model, SetLogLevel
            model_path = _model_path()
            if not model_path:
                raise RuntimeError("Local phrase model has not been downloaded")
            SetLogLevel(-1)
            self._model = Model(str(model_path))
            if self._model.vosk_model_find_word("rem") < 0:
                raise RuntimeError("The local model does not contain the word REM")
            grammar = json.dumps(["rem where are you"], ensure_ascii=False)
            self._recognizer = KaldiRecognizer(self._model, _SAMPLE_RATE, grammar)
            self._running = self._ready = True
            self._thread = threading.Thread(target=self._loop, daemon=True,
                                            name="RemWakePhraseThread")
            self._thread.start()
            self._logger(f"Local detector is listening for ‘{WAKE_PHRASE}’ while REM sleeps.")
            return True
        except Exception as exc:
            self._model = self._recognizer = None
            self._running = self._ready = False
            self._logger(f"Local phrase detector could not start: {exc}")
            self._notify("Local phrase detection is unavailable; use WAKE NOW or install its model.")
            return False

    def stop(self) -> None:
        self._running = self._ready = False
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            pass
        self._recognizer = self._model = None

    def reset(self) -> None:
        """Drop buffered audio and recognizer state when REM changes sleep state."""
        self._reset_requested.set()

    def feed(self, frame_int16) -> None:
        """Non-blocking frame copy from the sound-device callback; never stores to disk."""
        if not self._running:
            return
        try:
            frame = (frame_int16[:, 0] if getattr(frame_int16, "ndim", 1) > 1
                     else frame_int16).copy()
            self._queue.put_nowait(frame)
        except Exception:
            # Drop under load rather than blocking the microphone callback.
            return

    def _loop(self) -> None:
        import numpy as np
        pre_roll: deque[bytes] = deque(maxlen=_PRE_ROLL_FRAMES)
        speech = False
        silence = 0
        segment_frames = 0

        def reset_segment() -> None:
            nonlocal speech, silence, segment_frames
            pre_roll.clear()
            speech, silence, segment_frames = False, 0, 0
            try:
                self._recognizer.Reset()
            except Exception:
                pass

        def check_result(result_json: str) -> bool:
            try:
                text = json.loads(result_json).get("text", "")
            except (ValueError, AttributeError):
                return False
            now = time.monotonic()
            if _is_exact_phrase(text) and now - self._last_trigger >= _COOLDOWN_SECONDS:
                self._last_trigger = now
                try:
                    self._on_detect()
                except Exception as exc:
                    self._logger(f"Wake callback failed: {exc}")
                return True
            return False

        while self._running:
            try:
                frame = self._queue.get(timeout=0.25)
                if frame is None:
                    continue
                if self._reset_requested.is_set():
                    self._reset_requested.clear()
                    reset_segment()
                    while True:
                        try:
                            self._queue.get_nowait()
                        except queue.Empty:
                            break
                    continue
                pcm = np.asarray(frame, dtype=np.int16).reshape(-1)
                raw = pcm.tobytes()
                energy = float(np.sqrt(np.mean(pcm.astype(np.float32) ** 2))) if pcm.size else 0.0
                voiced = energy >= _ENERGY_THRESHOLD
                if not speech:
                    if not voiced:
                        pre_roll.append(raw)
                        continue
                    speech = True
                    for prior in pre_roll:
                        self._recognizer.AcceptWaveform(prior)
                    pre_roll.clear()
                completed = self._recognizer.AcceptWaveform(raw)
                segment_frames += len(pcm)
                silence = silence + 1 if not voiced else 0

                if completed and check_result(self._recognizer.Result()):
                    reset_segment()
                    continue

                # Only examine finalized utterances; no continuous transcript is retained.
                finalized = silence >= _SILENCE_FRAMES or segment_frames >= _MAX_SEGMENT_SECONDS * _SAMPLE_RATE
                if finalized:
                    detected = check_result(self._recognizer.FinalResult())
                    reset_segment()
                    if detected:
                        self._last_trigger = time.monotonic()
            except queue.Empty:
                continue
            except Exception as exc:
                self._logger(f"Local phrase recognition error: {exc}")
                reset_segment()
