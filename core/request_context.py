"""Immutable, expiring context for a direct user request."""
from __future__ import annotations

from dataclasses import dataclass
import time
import uuid


REQUEST_CONTEXT_TTL = 10 * 60.0


@dataclass(frozen=True)
class RequestContext:
    task_id: str
    instruction: str
    source: str
    created_at: float
    expires_at: float
    autonomous_session_id: str = ""
    browser: str = ""
    browser_task_id: str = ""
    parent_instruction: str = ""
    root_task_id: str = ""
    spoken_inputs: tuple[str, ...] = ()

    @classmethod
    def create(cls, instruction: str, source: str, *, now: float | None = None,
               ttl: float | None = None, autonomous_session_id: str = "",
               task_id: str = "", browser: str = "", browser_task_id: str = "",
               parent_instruction: str = "", root_task_id: str = "",
               spoken_inputs: tuple[str, ...] = ()):
        at = time.monotonic() if now is None else float(now)
        lifetime = REQUEST_CONTEXT_TTL if ttl is None else max(0.0, float(ttl))
        tid = str(task_id or "").strip() or uuid.uuid4().hex
        cleaned_hist = tuple(str(s).strip() for s in (spoken_inputs or ()) if str(s or "").strip())
        return cls(
            tid,
            str(instruction or "").strip(),
            str(source),
            at,
            at + lifetime,
            str(autonomous_session_id or ""),
            str(browser or "").strip().lower(),
            str(browser_task_id or "").strip(),
            str(parent_instruction or "").strip(),
            str(root_task_id or tid).strip(),
            cleaned_hist,
        )

    def age(self, *, now: float | None = None) -> float:
        return max(0.0, (time.monotonic() if now is None else float(now)) - self.created_at)

    def is_expired(self, *, now: float | None = None) -> bool:
        return (time.monotonic() if now is None else float(now)) >= self.expires_at
