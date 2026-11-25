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

    @classmethod
    def create(cls, instruction: str, source: str, *, now: float | None = None,
               ttl: float | None = None, autonomous_session_id: str = "",
               task_id: str = ""):
        at = time.monotonic() if now is None else float(now)
        lifetime = REQUEST_CONTEXT_TTL if ttl is None else max(0.0, float(ttl))
        return cls(
            task_id or uuid.uuid4().hex,
            str(instruction or "").strip(),
            str(source),
            at,
            at + lifetime,
            str(autonomous_session_id or ""),
        )

    def age(self, *, now: float | None = None) -> float:
        return max(0.0, (time.monotonic() if now is None else float(now)) - self.created_at)

    def is_expired(self, *, now: float | None = None) -> bool:
        return (time.monotonic() if now is None else float(now)) >= self.expires_at
