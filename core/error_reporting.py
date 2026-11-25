"""Safe, machine-readable runtime failure records for REM.

Exception messages and tracebacks are intentionally omitted: browser URLs,
provider responses, user text, and credentials can be embedded in them.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import json
import logging
import uuid


def classify_error(error: BaseException) -> str:
    if isinstance(error, (asyncio.CancelledError,
                          concurrent.futures.CancelledError)):
        return "expected_cancellation"
    if isinstance(error, (TimeoutError, ConnectionError)):
        return "recoverable"
    if isinstance(error, MemoryError):
        return "critical"
    if isinstance(error, PermissionError):
        return "functional_failure"
    return "functional_failure"


def report_error(component: str, operation: str, error: BaseException, *,
                 task_id: str | None = None, recovery: str = "none",
                 final_status: str = "failed", logger=None) -> dict:
    """Emit and return a sanitized structured diagnostic record."""
    record = {
        "event_id": uuid.uuid4().hex,
        "component": str(component or "unknown")[:80],
        "operation": str(operation or "unknown")[:100],
        "task_id": str(task_id or "missing")[:80],
        "category": classify_error(error),
        "error_type": type(error).__name__[:100],
        "recovery": str(recovery or "none")[:120],
        "final_status": str(final_status or "unknown")[:40],
    }
    target = logger or logging.getLogger("REM")
    target.log(logging.INFO if record["category"] == "expected_cancellation"
               else logging.ERROR,
               "REM_RUNTIME_ERROR %s", json.dumps(record, sort_keys=True))
    return record
