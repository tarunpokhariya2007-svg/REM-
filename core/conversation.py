"""Central, conservative guidance for translating tool outcomes into speech."""
from __future__ import annotations

import re


_STATUS = re.compile(r"^\s*status=(done|unverified|failed|ask|started)\b", re.I)
_INTERNAL_ERROR = re.compile(
    r"(?:\btraceback\b|\bHRESULT\b|\bWinError\b|\bPlaywright\b|"
    r"\bCDP\b|\bUI Automation\b|\bUIA\b|\bselector\b|\bcoordinates?\b|"
    r"^(?:Browser|Click|Type|Key|Scroll) error\b|^Could not start browser session\b)",
    re.I | re.M,
)


def format_tool_result(tool_name: str, result: object) -> str:
    """Prevent known low-level failures from being repeated as spoken prose."""
    text = str(result or "")
    if _INTERNAL_ERROR.search(text):
        return (f"status=failed action={tool_name} "
                "reason=the requested action could not be completed")
    return text


def tool_result_guidance(result: object) -> str:
    """Return concise response rules based on a tool's declared outcome."""
    match = _STATUS.match(str(result or ""))
    status = match.group(1).casefold() if match else "unknown"
    rules = {
        "done": (
            "The tool says the result was verified. Give one short, warm confirmation "
            "of only that verified result. Do not mention internal mechanisms."),
        "unverified": (
            "The result is not confirmed. Say briefly what was attempted and that you "
            "could not confirm it. Do not imply success or retry repeatedly; do not ask "
            "the user to perform or verify the action."),
        "failed": (
            "The action failed. Apologize at most once, give one brief user-facing "
            "reason, and do not claim success. Do not mention implementation details "
            "or ask the user to perform the action."),
        "ask": (
            "The tool needs one essential clarification or confirmation. Ask only that "
            "concise question; do not ask whether the user still wants the requested action."),
        "started": (
            "The action is only underway. Say so briefly without claiming completion; "
            "wait for its completion result and do not call the tool again to poll."),
        "unknown": (
            "Use only facts in the result. Never claim the action succeeded without "
            "verification. Speak naturally and briefly; omit internal automation details, "
            "and do not ask the user to perform or verify an action REM can check."),
    }
    return "[REM response guidance] " + rules[status]
