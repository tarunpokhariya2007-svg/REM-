"""core/task_control.py - what did the person just ask for, and is it still wanted?

One small, dependency-free module shared by WhatsApp, YouTube and every other
browser/desktop tool so they follow the SAME rules:

* ``classify_utterance`` - cancel / correction / confirm / command.
* ``request_cancel`` - a lightweight, non-latching cancel. (``emergency_stop``
  latches the whole desktop layer, turns off persistent observation and writes
  that to config; that is right for a panic button and wrong for "cancel".)
* ``has_send_command`` / ``call_kind`` - intent read from the CURRENT utterance
  only, negation-aware, whole-word.
* ``deletion_scope`` - separates clearing a DRAFT from deleting a SENT message or
  a whole conversation; the latter two are never inferred.

It only reads the text it is given; it never grants permission by itself. Those
decisions stay in ``core.desktop_control``.
"""
from __future__ import annotations

import re
import threading
import time

_WORD = re.compile(r"[0-9a-z\u00c0-\u024f\u0900-\u097f']+")


def _words(text: str) -> list[str]:
    return _WORD.findall(str(text or "").casefold().replace("’", "'"))


_FILLERS = {"please", "rem", "ok", "okay", "hey", "just", "now", "kindly", "uh", "um", "hmm"}

# Whole-utterance cancel phrases (after fillers are dropped).
_CANCEL_EXACT = {
    "cancel", "cancel that", "cancel it", "cancel this", "cancel everything", "cancel all",
    "cancel the task", "cancel the action", "cancel the message", "cancel the call",
    "cancel the request", "cancel the operation", "cancel task",
    "never mind", "nevermind", "forget it", "forget that", "forget about it", "abort", "abort it",
    "abort that", "stop everything", "stop that", "stop it", "stop doing that", "stop the task",
    "dont do that", "don't do that", "do not do that", "leave it", "leave it alone",
    "ruko", "ruk jao", "rehne do", "rahne do", "mat karo", "rok do", "cancel karo",
}

_CORRECTION_FIRST = {"no", "nope", "nah", "nahi", "nahin", "actually", "wait", "sorry", "instead",
                     "galat", "correction", "rather"}
_CORRECTION_PHRASES = ("not that", "not this", "i meant", "i mean", "i said", "change it to",
                       "make it", "my mistake", "no i")
_CONFIRM_ONLY = {"yes", "yeah", "yep", "yup", "ok", "okay", "sure", "haan", "ha", "correct", "right",
                 "do it", "go ahead", "yes please", "yes do it", "yes go ahead"}


def classify_utterance(text: str) -> str:
    """'cancel' | 'correction' | 'confirm' | 'command'.

    * cancel      - drop pending work and the task chain; authorizes nothing.
    * correction  - replaces the previous intent (history must not accumulate).
    * confirm     - a bare yes; may only confirm what was JUST asked, never a new thing.
    * command     - anything else.
    """
    raw = " ".join(_words(text))
    words = [w for w in _words(text) if w not in _FILLERS]
    if not words:
        return "confirm" if raw in _CONFIRM_ONLY else "command"
    flat = " ".join(words)
    if flat in _CANCEL_EXACT:
        return "cancel"
    # "cancel ..." followed by a short object is still a cancel; a long sentence is not.
    if words[0] in {"cancel", "abort"} and len(words) <= 4:
        return "cancel"
    if flat in _CONFIRM_ONLY:
        return "confirm"
    low = " ".join(_words(text))
    if len(words) > 1 and (words[0] in _CORRECTION_FIRST or any(low.startswith(p) for p in _CORRECTION_PHRASES)):
        return "correction"
    return "command"


# --------------------------------------------------------------------------- cancellation
_lock = threading.Lock()
_generation = 0
_last_cancel_at = 0.0
_last_cancel_reason = ""


def cancel_generation() -> int:
    """Monotonic counter. A tool captures it at start and aborts if it changed."""
    with _lock:
        return _generation


def was_cancelled_since(generation: int) -> bool:
    return cancel_generation() != generation


def last_cancel() -> tuple[float, str]:
    with _lock:
        return _last_cancel_at, _last_cancel_reason


def request_cancel(reason: str = "user cancel") -> int:
    """Cancel pending/in-flight operations WITHOUT latching the desktop layer.

    Bumps the generation, clears every task-scoped target/typing payload/composition
    draft authorization, and calls registered in-flight cancellers. Control stays
    available for the next command.
    """
    global _generation, _last_cancel_at, _last_cancel_reason
    with _lock:
        _generation += 1
        _last_cancel_at = time.monotonic()
        _last_cancel_reason = str(reason or "")[:80]
        gen = _generation
    try:
        from core import desktop_control as dc
        dc.clear_all_task_state()
        dc._cancel_inflight()
        dc.record_diagnostics(last_auth_decision="CANCELLED by user",
                              sending_status="cancelled", current_action="Cancelled")
    except Exception:
        pass
    return gen


# --------------------------------------------------------------------------- intent from THIS utterance
_NEGATORS = {"dont", "don't", "not", "never", "no", "without", "nahi", "nahin", "mat", "na"}
_QUESTIONERS = {"did", "had", "have", "was", "were", "what", "when", "where", "why", "how", "who", "which"}
_SEND_WORDS = {"send", "sent", "deliver", "bhejo", "bhej", "bhejna", "post"}
_CALL_WORDS = {"call", "ring", "dial", "phone"}


def _verb_affirmed(text: str, verbs: set[str]) -> bool:
    words = _words(text)
    for i, w in enumerate(words):
        if w in verbs and w != "sent":
            window = words[max(0, i - 3):i]
            if any(n in _NEGATORS for n in window):
                continue
            if any(q in _QUESTIONERS for q in words[max(0, i - 2):i]):
                continue        # "what did I send" is a question, not an instruction
            if i + 1 < len(words) and words[i + 1] in {"mat", "nahi", "nahin"}:
                continue
            return True
    return False


def has_send_command(text: str) -> bool:
    """True when THIS utterance tells REM to send, as a whole word, not negated.

    'send it' yes; "don't send it", 'do not send', 'sender', 'resend later' no.
    The previous check searched earlier turns for the substring "send", so a
    stale or negated send could authorize a new one.
    """
    return _verb_affirmed(text, _SEND_WORDS)


def call_kind(text: str) -> str:
    """'video' | 'voice' | '' from the person's own words."""
    words = set(_words(text))
    if not _verb_affirmed(text, _CALL_WORDS):
        return ""
    if "video" in words:
        return "video"
    return "voice"


# --------------------------------------------------------------------------- deletion scope
_DELETE_WORDS = {"delete", "remove", "erase", "clear", "wipe", "unsend", "empty", "discard"}


def deletion_scope(text: str) -> str:
    """Return the *scope* of a deletion request.

    ''               not a deletion request
    'clear_draft'    the unsent text in the message box (safe, local, undoable by retyping)
    'delete_message' a message that was already SENT (destructive, needs confirmation)
    'delete_chat'    a whole conversation or its history (destructive, needs confirmation)

    Ambiguous wording ("delete the message") resolves to the MORE destructive
    reading so a confirmation is demanded; it never silently picks the safe one.
    """
    words = _words(text)
    if not any(w in _DELETE_WORDS for w in words):
        return ""
    ws = set(words)
    if ws & {"chat", "conversation", "thread", "history", "chats", "conversations"}:
        return "delete_chat"
    if ws & {"draft", "composer", "box", "field", "typed", "typing", "bar"} and not ws & {"sent"}:
        return "clear_draft"
    if ws & {"sent", "unsend", "everyone", "bhej", "bheja"}:
        return "delete_message"
    if ws & {"message", "messages", "text", "msg"}:
        # "clear the message" is how people say "empty the draft"; "delete the message"
        # is more often about a sent one. Only "clear/empty" + message => draft.
        if ws & {"clear", "empty", "erase"} and not ws & {"delete", "remove", "unsend"}:
            return "clear_draft"
        return "delete_message"
    return ""
