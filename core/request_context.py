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

    @classmethod
    def create(cls, instruction: str, source: str, *, now: float | None = None):
        at = time.monotonic() if now is None else float(now)
        return cls(uuid.uuid4().hex, str(instruction or "").strip(), str(source),
                   at, at + REQUEST_CONTEXT_TTL)

    def age(self, *, now: float | None = None) -> float:
        return max(0.0, (time.monotonic() if now is None else float(now)) - self.created_at)

    def is_expired(self, *, now: float | None = None) -> bool:
        return (time.monotonic() if now is None else float(now)) >= self.expires_at
