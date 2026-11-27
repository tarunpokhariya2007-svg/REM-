"""Central, conservative guidance for translating tool outcomes into speech."""
from __future__ import annotations

import re


_STATUS = re.compile(r"^\s*status=(done|verified|unverified|failed|ask|started)\b", re.I)

_SPANISH_MARKERS = (
    "hola", "gracias", "por favor", "qué", "como", "cómo", "estás", "estoy",
    "tengo", "miedo", "puedes", "dime", "háblame", "por qué", "porque", "pero",
    "buenos días", "buenas tardes", "buenas noches", "quiero", "necesito",
    "ayuda", "ayúdame", "español", "espanol", "me da", "no sé", "claro",
)


_HINDI_ROMAN_MARKERS = (
    "kya", "hai", "kaise", "dekho", "karo", "kholo", "batao", "mera", "meri",
    "aaj", "tum", "aap", "namaste", "dhanyavaad", "haan", "nahi", "samjhao",
)


def detect_language(text: str) -> str:
    """Lightweight deterministic language detector for routing & logging."""
    raw = str(text or "").strip()
    if not raw:
        return "English"
    if re.search(r"[\u0900-\u097F]", raw):
        return "Hindi"
    if re.search(r"[\u3040-\u30FF]", raw):
        return "Japanese"
    low = f" {raw.casefold()} "
    if any(ch in raw for ch in "¿¡ñÑáéíóúÁÉÍÓÚüÜ"):
        return "Spanish"
    hi_hits = sum(
        1 for m in _HINDI_ROMAN_MARKERS
        if re.search(rf"(?<!\w){re.escape(m)}(?!\w)", low)
    )
    if hi_hits >= 2:
        return "Hindi"
    es_hits = sum(
        1 for m in _SPANISH_MARKERS
        if re.search(rf"(?<!\w){re.escape(m)}(?!\w)", low)
    )
    if es_hits >= 1 and any(w in low for w in (" me ", " te ", " se ", " que ", " qué ", " de ", " la ", " el ", " es ", " no ", " un ", " una ", "hola", "gracias", "por favor")):
        return "Spanish"
    if es_hits >= 2:
        return "Spanish"
    return "English"
_INTERNAL_ERROR = re.compile(
    r"(?:\btraceback\b|\bHRESULT\b|\bWinError\b|"
    r"^(?:Browser|Click|Type|Key|Scroll) error\b|^Could not start browser session\b|"
    r"(?:Playwright|TimeoutError).*\bselector\b)",
    re.I | re.M,
)


def format_tool_result(tool_name: str, result: object) -> str:
    """Prevent raw low-level tracebacks from being spoken while preserving verified tool outcomes."""
    text = str(result or "")
    status_match = _STATUS.match(text)
    if status_match:
        st = status_match.group(1).casefold()
        if st in {"done", "verified", "unverified", "ask", "started"}:
            # Strip raw traceback/WinError noise only if somehow embedded, never invert status=done!
            if "traceback" in text.casefold():
                return text.splitlines()[0][:240]
            return text
        if not re.search(r"\b(traceback|HRESULT|WinError|Playwright)\b", text, re.I):
            return text
    if _INTERNAL_ERROR.search(text):
        return (f"status=failed action={tool_name} "
                "reason=the requested action could not be completed")
    return text


def tool_result_guidance(result: object) -> str:
    """Return concise response rules based on a tool's declared outcome."""
    text = str(result or "")
    match = _STATUS.match(text)
    status = match.group(1).casefold() if match else "unknown"
    text_cf = text.casefold()
    if status == "done" and ("verified=false" in text_cf or "selected_chat_verified=false" in text_cf):
        status = "unverified"
    if "evidence_status=unknown" in text_cf or "evidence_status=inferred" in text_cf:
        status = "unverified"
    playback_unverified = "playback_verified=false" in text_cf
    unknown_title = "evidence_status=unknown" in text_cf or "cannot reliably read" in text_cf
    rules = {
        "done": (
            "The tool says the result was verified. Give one short, warm confirmation "
            "of only that verified result in fluent natural English (or Hindi if the user spoke Hindi) "
            "— for example, 'I clicked the video, Tarun-kun.' "
            "Include the actual video title when opening a video (never guess or alter it). Never claim failure, never say protocols prohibit routine clicks, and never mention internal mechanisms."),
        "unverified": (
            "The physical action or result is not confirmed. Say honestly what happened and that you "
            "could not confirm the final state — for example, 'I sent the click, Tarun-kun, but I couldn't confirm that the video opened.' "
            "Do not imply success or retry repeatedly; do not ask the user to perform or verify the action, and never say protocols blocked clicking."),
        "failed": (
            "The action failed. Apologize at most once, give one brief user-facing reason from the tool result "
            "(if task/authorization did not match, say the active task authorization or target did not match), "
            "and do not claim success. Do not mention implementation details, "
            "never say your protocols prevent routine authorized clicks/typing, and do not ask the user to perform the action."),
        "ask": (
            "The tool needs one essential clarification or confirmation. Ask only that "
            "concise question; do not ask whether the user still wants the requested action."),
        "started": (
            "The action is only underway. Say so briefly without claiming completion; "
            "wait for its completion result and do not call the tool again to poll."),
        "unknown": (
            "Use only facts in the result. Never claim the action succeeded without "
            "verification. Speak naturally and briefly in fluent English (or Hindi if the user spoke Hindi); "
            "omit internal automation details, and do not ask the user to perform or verify an action REM can check."),
    }
    extra = ""
    if unknown_title:
        extra = (
            " The video title was NOT reliably verified: say you can see the video cards "
            "but cannot reliably read the requested video's title, and never guess or invent a title."
        )
    elif playback_unverified and status in ("done", "unverified"):
        extra = (
            " Video playback is not confirmed (`playback_verified=false`): do NOT say 'I'm playing it' "
            "or claim playback started; state honestly whether the card opened."
        )
    return "[REM response guidance] " + rules[status] + extra
