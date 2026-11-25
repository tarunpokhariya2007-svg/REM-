"""Trusted desktop authorization and REM AUTONOMOUS MODE (Overnight Mode).

Provides:
- Explicit two-state mode: ENABLED / DISABLED (default DISABLED).
- Task-scoped authorization sessions (``AutonomousSession``) with explicit
  working-directory, application, network, duration, and retry boundaries.
- Separate permission categories so local screen observation never grants
  cloud image sharing, and routine local/project work never bypasses
  sensitive-operation approval gates.
- Non-blocking pending-approval recording when the user is away/asleep
  (silence is never interpreted as approval).
- Checkpoints, execution logs, revocation, Emergency Stop, and verified
  completion reports.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Optional
from urllib.parse import urlsplit

DISABLED = "DISABLED"
ENABLED = "ENABLED"

TRUSTED_SOURCES = frozenset({"voice", "typed", "dashboard", "ui"})

# Separate permission categories (Requirement 5)
CAT_LOCAL_DESKTOP = "local_desktop_automation"
CAT_LOCAL_SCREEN = "local_screen_observation"
CAT_BROWSER = "browser_automation"
CAT_PROJECT_FILES = "approved_project_files"
CAT_EXTERNAL_NETWORK = "external_network_access"
CAT_CLOUD_IMAGE = "cloud_image_sharing"
CAT_SENSITIVE = "sensitive_operations"

PERMISSION_CATEGORIES = frozenset({
    CAT_LOCAL_DESKTOP,
    CAT_LOCAL_SCREEN,
    CAT_BROWSER,
    CAT_PROJECT_FILES,
    CAT_EXTERNAL_NETWORK,
    CAT_CLOUD_IMAGE,
    CAT_SENSITIVE,
})

DEFAULT_ALLOWED_OPERATIONS = frozenset({
    CAT_LOCAL_DESKTOP,
    CAT_LOCAL_SCREEN,
    CAT_BROWSER,
    CAT_PROJECT_FILES,
    CAT_EXTERNAL_NETWORK,
})

DEFAULT_PERMITTED_APPS = (
    "chrome", "google chrome", "msedge", "edge", "firefox",
    "code", "vscode", "visual studio code",
    "notepad", "windows terminal", "terminal", "powershell", "pwsh", "cmd",
    "explorer", "python", "pythonw",
)

DEFAULT_MAX_DURATION_S = 8 * 3600.0
DEFAULT_MAX_RETRIES = 3

_BASE_DIR = Path(__file__).resolve().parent.parent
_SESSIONS_DIR = _BASE_DIR / "memory" / "autonomous_sessions"
_REPORTS_DIR = _BASE_DIR / "reports"

_lock = threading.RLock()
_mode: str = DISABLED
_active_session_id: Optional[str] = None
_sessions: dict[str, "AutonomousSession"] = {}
_listeners: list[Callable[[dict], None]] = []


def _norm_path(p: str | Path) -> Path:
    return Path(p).expanduser().resolve()


def _is_inside_any(target: Path, roots: Iterable[Path]) -> bool:
    try:
        resolved = target.expanduser().resolve()
    except Exception:
        return False
    for root in roots:
        try:
            r = root.expanduser().resolve()
        except Exception:
            continue
        if resolved == r or r in resolved.parents:
            return True
    return False


def _is_loopback_url(url: str) -> bool:
    raw = str(url or "").strip()
    if not raw:
        return True
    try:
        parsed = urlsplit(raw if "://" in raw else f"https://{raw}")
        host = (parsed.hostname or "").casefold()
        return host in {"localhost", "127.0.0.1", "::1"}
    except Exception:
        return False


@dataclass
class AutonomousSession:
    session_id: str
    task_id: str
    objective: str
    source: str
    approved_dirs: tuple[Path, ...]
    permitted_apps: tuple[str, ...]
    allowed_operations: frozenset[str]
    internet_access: bool
    max_duration_s: float
    max_retries: int
    deliverables: tuple[str, ...]
    created_at: float
    expires_at: float
    created_wall: float
    revoked: bool = False
    revoke_reason: str = ""
    status: str = "active"  # active | completed | revoked | stopped | expired
    steps: list[dict] = field(default_factory=list)
    execution_log: list[dict] = field(default_factory=list)
    files_modified: list[str] = field(default_factory=list)
    tests_executed: list[dict] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)
    pending_approvals: list[dict] = field(default_factory=list)
    output_locations: list[str] = field(default_factory=list)

    def is_valid(self, *, now: float | None = None) -> tuple[bool, str]:
        at = time.monotonic() if now is None else float(now)
        if self.revoked:
            return False, f"Trusted session was revoked ({self.revoke_reason or 'revoked by user'})."
        if self.status in {"revoked", "stopped"}:
            return False, f"Trusted session is {self.status}."
        if _mode != ENABLED:
            return False, "REM Autonomous Mode is DISABLED."
        if at >= self.expires_at:
            self.status = "expired"
            return False, "Trusted session authorization has expired."
        if self.source not in TRUSTED_SOURCES:
            return False, "Trusted session source is untrusted."
        return True, ""

    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "task_id": self.task_id,
            "objective": self.objective,
            "source": self.source,
            "approved_dirs": [str(p) for p in self.approved_dirs],
            "permitted_apps": list(self.permitted_apps),
            "allowed_operations": sorted(self.allowed_operations),
            "internet_access": self.internet_access,
            "max_duration_s": self.max_duration_s,
            "max_retries": self.max_retries,
            "deliverables": list(self.deliverables),
            "created_wall": self.created_wall,
            "remaining_s": max(0.0, round(self.expires_at - time.monotonic(), 1)),
            "revoked": self.revoked,
            "revoke_reason": self.revoke_reason,
            "status": self.status,
            "steps": list(self.steps),
            "execution_log": list(self.execution_log[-300:]),
            "files_modified": list(self.files_modified),
            "tests_executed": list(self.tests_executed),
            "failures": list(self.failures),
            "pending_approvals": list(self.pending_approvals),
            "output_locations": list(self.output_locations),
        }

    def save(self, sessions_dir: Path | None = None) -> Path:
        root = Path(sessions_dir) if sessions_dir else _SESSIONS_DIR
        root.mkdir(parents=True, exist_ok=True)
        path = root / f"{self.session_id}.json"
        tmp = root / f"{self.session_id}.json.tmp"
        tmp.write_text(json.dumps(self.to_dict(), indent=2), encoding="utf-8")
        os.replace(tmp, path)
        return path


def subscribe(callback: Callable[[dict], None]) -> None:
    with _lock:
        if callback not in _listeners:
            _listeners.append(callback)
        snap = snapshot()
    try:
        callback(snap)
    except Exception:
        pass


def _emit() -> dict:
    with _lock:
        snap = snapshot()
        listeners = tuple(_listeners)
    for cb in listeners:
        try:
            cb(snap)
        except Exception:
            pass
    return snap


def get_mode() -> str:
    with _lock:
        return _mode


def is_enabled() -> bool:
    with _lock:
        return _mode == ENABLED


def snapshot() -> dict:
    with _lock:
        sess = _sessions.get(_active_session_id) if _active_session_id else None
        if sess is not None:
            valid, _ = sess.is_valid()
        else:
            valid = False
        return {
            "mode": _mode,
            "enabled": _mode == ENABLED,
            "session_active": valid,
            "session_id": sess.session_id if sess else "",
            "task_id": sess.task_id if sess else "",
            "objective": sess.objective if sess else "",
            "status": sess.status if sess else "idle",
            "pending_approvals": len(sess.pending_approvals) if sess else 0,
            "steps_done": sum(1 for s in sess.steps if s.get("status") == "done") if sess else 0,
            "steps_total": len(sess.steps) if sess else 0,
            "approved_dirs": [str(p) for p in sess.approved_dirs] if sess else [],
            "remaining_s": max(0.0, round(sess.expires_at - time.monotonic(), 1)) if sess else 0.0,
        }


def set_mode(mode: str, *, reason: str = "") -> dict:
    global _mode
    norm = str(mode or "").strip().upper()
    if norm not in {ENABLED, DISABLED}:
        raise ValueError("REM AUTONOMOUS MODE must be ENABLED or DISABLED")
    with _lock:
        _mode = norm
        if norm == DISABLED and _active_session_id and _active_session_id in _sessions:
            sess = _sessions[_active_session_id]
            if not sess.revoked and sess.status == "active":
                sess.revoked = True
                sess.revoke_reason = reason or "REM Autonomous Mode disabled"
                sess.status = "revoked"
                try:
                    sess.save()
                except Exception:
                    pass
    return _emit()


def enable() -> dict:
    return set_mode(ENABLED)


def disable(reason: str = "REM Autonomous Mode disabled") -> dict:
    return set_mode(DISABLED, reason=reason)


def start_session(
    objective: str,
    *,
    source: str,
    task_id: str = "",
    approved_dirs: Iterable[str | Path] | None = None,
    permitted_apps: Iterable[str] | None = None,
    allowed_operations: Iterable[str] | None = None,
    internet_access: bool = True,
    max_duration_s: float = DEFAULT_MAX_DURATION_S,
    max_retries: int = DEFAULT_MAX_RETRIES,
    deliverables: Iterable[str] | None = None,
    allow_cloud_image: bool = False,
    now: float | None = None,
) -> AutonomousSession:
    """Create a task-scoped trusted session after explicit user authorization."""
    global _active_session_id
    if source not in TRUSTED_SOURCES:
        raise PermissionError("Autonomous session requires a direct trusted user request.")
    obj = str(objective or "").strip()
    if not obj:
        raise ValueError("Task objective cannot be empty.")
    with _lock:
        if _mode != ENABLED:
            raise PermissionError("REM AUTONOMOUS MODE is DISABLED; enable it before starting an autonomous session.")

        dirs = tuple(_norm_path(d) for d in (approved_dirs or [_BASE_DIR]))
        apps = tuple(str(a).strip().casefold() for a in (permitted_apps or DEFAULT_PERMITTED_APPS) if str(a).strip())
        ops = set(allowed_operations or DEFAULT_ALLOWED_OPERATIONS)
        # Sensitive operations are never granted in bulk.
        ops.discard(CAT_SENSITIVE)
        # Cloud image sharing stays off unless explicitly enabled and opted in.
        if not allow_cloud_image:
            ops.discard(CAT_CLOUD_IMAGE)
        else:
            try:
                from memory.config_manager import get_cloud_screen_vision_enabled
                if not get_cloud_screen_vision_enabled():
                    ops.discard(CAT_CLOUD_IMAGE)
            except Exception:
                ops.discard(CAT_CLOUD_IMAGE)
        if not internet_access:
            ops.discard(CAT_EXTERNAL_NETWORK)
        ops = {o for o in ops if o in PERMISSION_CATEGORIES}

        dur = max(60.0, min(12 * 3600.0, float(max_duration_s or DEFAULT_MAX_DURATION_S)))
        at = time.monotonic() if now is None else float(now)
        sid = uuid.uuid4().hex
        tid = str(task_id or "").strip() or uuid.uuid4().hex[:12]

        # Revoke any superseded active session cleanly
        if _active_session_id and _active_session_id in _sessions:
            prev = _sessions[_active_session_id]
            if not prev.revoked and prev.status == "active":
                prev.revoked = True
                prev.revoke_reason = "superseded by new autonomous session"
                prev.status = "revoked"

        sess = AutonomousSession(
            session_id=sid,
            task_id=tid,
            objective=obj,
            source=source,
            approved_dirs=dirs,
            permitted_apps=apps,
            allowed_operations=frozenset(ops),
            internet_access=bool(internet_access),
            max_duration_s=dur,
            max_retries=max(1, min(10, int(max_retries or DEFAULT_MAX_RETRIES))),
            deliverables=tuple(str(d).strip() for d in (deliverables or []) if str(d).strip()),
            created_at=at,
            expires_at=at + dur,
            created_wall=time.time(),
        )
        _sessions[sid] = sess
        _active_session_id = sid
        log_event(sess, "session_started", "autonomous_mode", CAT_PROJECT_FILES, "done",
                  f"objective={obj[:120]}; dirs={[str(d) for d in dirs]}")
        try:
            sess.save()
        except Exception:
            pass
    _emit()
    return sess


def get_session(session_id: str | None = None) -> Optional[AutonomousSession]:
    with _lock:
        sid = session_id or _active_session_id
        if not sid:
            return None
        return _sessions.get(sid)


def active_session(*, now: float | None = None) -> Optional[AutonomousSession]:
    with _lock:
        if not _active_session_id:
            return None
        sess = _sessions.get(_active_session_id)
        if sess is None:
            return None
        ok, _ = sess.is_valid(now=now)
        return sess if ok else None


def revoke_session(session_id: str | None = None, *, reason: str = "revoked by user",
                   disable_mode: bool = False) -> bool:
    global _mode
    changed = False
    with _lock:
        targets = ([_sessions[session_id]] if session_id and session_id in _sessions
                   else ([_sessions[_active_session_id]] if _active_session_id and _active_session_id in _sessions else []))
        for sess in targets:
            if not sess.revoked:
                sess.revoked = True
                sess.revoke_reason = reason
                sess.status = "revoked"
                log_event(sess, "session_revoked", "autonomous_mode", CAT_SENSITIVE, "revoked", reason)
                try:
                    sess.save()
                except Exception:
                    pass
                changed = True
        if disable_mode and _mode == ENABLED:
            _mode = DISABLED
            changed = True
    if changed:
        _emit()
    return changed


def emergency_stop(reason: str = "Emergency stop triggered") -> dict:
    """Immediately revoke active autonomous session, disable mode, and stop desktop control."""
    global _mode
    with _lock:
        _mode = DISABLED
        for sess in _sessions.values():
            if not sess.revoked and sess.status == "active":
                sess.revoked = True
                sess.revoke_reason = reason
                sess.status = "stopped"
                log_event(sess, "emergency_stop", "autonomous_mode", CAT_SENSITIVE, "stopped", reason)
                try:
                    sess.save()
                except Exception:
                    pass
    try:
        from core.tasks import desktop as tasks_desktop
        eng = tasks_desktop.engine()
        if eng is not None:
            for t in eng.list(50):
                if t.get("status") in ("queued", "scheduled", "running", "waiting_quota") and t.get("autonomous_session_id"):
                    eng.cancel(t["id"], finish=False)
    except Exception:
        pass
    return _emit()


def log_event(sess: AutonomousSession, event: str, tool: str, category: str,
              status: str, detail: str = "") -> None:
    with _lock:
        sess.execution_log.append({
            "ts": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
            "event": str(event),
            "tool": str(tool),
            "category": str(category),
            "status": str(status),
            "detail": str(detail or "")[:400],
        })


def record_pending_approval(sess: AutonomousSession, *, category: str, title: str,
                            detail: str, tool: str = "", args_summary: str = "") -> dict:
    """Record a consequential action that requires explicit user approval (never auto-approved)."""
    with _lock:
        entry = {
            "id": uuid.uuid4().hex[:10],
            "ts": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
            "category": str(category),
            "title": str(title)[:180],
            "detail": str(detail)[:500],
            "tool": str(tool),
            "args_summary": str(args_summary)[:240],
            "status": "pending_approval",
        }
        # Avoid flooding duplicates with same title & tool
        for existing in sess.pending_approvals:
            if existing.get("title") == entry["title"] and existing.get("tool") == entry["tool"]:
                return existing
        sess.pending_approvals.append(entry)
        log_event(sess, "pending_approval", tool or "confirm", CAT_SENSITIVE,
                  "pending_approval", f"{title} ({category})")
        try:
            sess.save()
        except Exception:
            pass
    _emit()
    return entry


def set_steps(sess: AutonomousSession, steps: list[str | dict]) -> list[dict]:
    with _lock:
        normalized = []
        for idx, item in enumerate(steps, 1):
            if isinstance(item, dict):
                normalized.append({
                    "id": str(item.get("id") or f"step-{idx}"),
                    "title": str(item.get("title") or f"Step {idx}"),
                    "status": str(item.get("status") or "pending"),
                    "attempts": int(item.get("attempts") or 0),
                    "verification": str(item.get("verification") or ""),
                    "note": str(item.get("note") or ""),
                })
            else:
                normalized.append({
                    "id": f"step-{idx}",
                    "title": str(item),
                    "status": "pending",
                    "attempts": 0,
                    "verification": "",
                    "note": "",
                })
        sess.steps = normalized
        log_event(sess, "plan_updated", "task_checkpoint", CAT_PROJECT_FILES, "done",
                  f"{len(normalized)} step(s)")
        try:
            sess.save()
        except Exception:
            pass
    _emit()
    return list(sess.steps)


def update_step(sess: AutonomousSession, step_id: str, status: str, *,
                verification: str = "", note: str = "") -> dict | None:
    with _lock:
        for step in sess.steps:
            if step["id"] == step_id or step["title"].casefold() == str(step_id).casefold():
                if status in ("in_progress", "done", "failed", "blocked_approval", "pending", "skipped"):
                    step["status"] = status
                if status == "in_progress":
                    step["attempts"] = int(step.get("attempts") or 0) + 1
                if verification:
                    step["verification"] = str(verification)[:400]
                if note:
                    step["note"] = str(note)[:400]
                if status == "failed" and note:
                    sess.failures.append(f"{step['title']}: {note}")
                log_event(sess, "step_checkpoint", "task_checkpoint", CAT_PROJECT_FILES,
                          status, f"{step['title']} — {verification or note}")
                try:
                    sess.save()
                except Exception:
                    pass
                _emit()
                return dict(step)
    return None


def record_file_modified(sess: AutonomousSession, path: str | Path) -> None:
    with _lock:
        p = str(path)
        if p not in sess.files_modified:
            sess.files_modified.append(p)
        try:
            sess.save()
        except Exception:
            pass


def record_test_run(sess: AutonomousSession, command: str, passed: bool, summary: str) -> None:
    with _lock:
        sess.tests_executed.append({
            "ts": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
            "command": str(command)[:200],
            "passed": bool(passed),
            "summary": str(summary)[:500],
        })
        if not passed:
            sess.failures.append(f"Test failed ({command}): {summary[:200]}")
        try:
            sess.save()
        except Exception:
            pass


# ── Sensitive operation & permission evaluation ──────────────────────────────

_SENSITIVE_TOOLS = {
    "send_message": ("external_communication", "Sending messages requires explicit approval."),
    "gmail_control": ("external_communication", "Sending or mutating email requires explicit approval."),
    "upload_video": ("external_communication", "Publishing content requires explicit approval."),
    "whatsapp_call": ("external_communication", "Placing calls requires explicit approval."),
    "calendar_control": ("consequential_mutation", "Calendar mutations require explicit approval."),
    "desktop_control": ("destructive_action", "Desktop file cleanup/deletion requires explicit approval."),
}

_SENSITIVE_CLICK_WORDS = (
    "send", "submit", "delete", "remove", "erase", "purchase", "buy",
    "place order", "checkout", "pay", "transfer", "publish", "share",
    "install", "uninstall", "allow access", "disable security",
    "change password", "reset password", "security settings", "administrator",
)

_SENSITIVE_COMMAND_RE = re.compile(
    r"(?:\b(?:pip|pip3|npm|yarn|pnpm|winget|choco|scoop)\s+install\b|"
    r"\b(?:rm\s+-rf|del\s+/[sqf]|rmdir\s+/s|remove-item\b.*-recurse|format\b|diskpart\b)|"
    r"\b(?:runas|sudo|pkexec|bcdedit|reg\s+(?:add|delete)|net\s+user|netsh\b)|"
    r"start-process\b.*-verb\s+runas|\|\s*(?:sh|bash|iex|invoke-expression)\b)",
    re.IGNORECASE,
)


def classify_sensitive_operation(tool: str, args: dict | None,
                                 sess: AutonomousSession | None = None) -> tuple[bool, str, str]:
    """Identify genuinely consequential actions that silence must NEVER approve."""
    t = str(tool or "").strip().lower()
    a = dict(args or {})
    act = str(a.get("action") or a.get("mode") or "").strip().lower().replace("-", "_")

    if t in _SENSITIVE_TOOLS:
        if t == "gmail_control" and act in {"list", "read", "search", "inbox"}:
            return False, "", ""
        cat, msg = _SENSITIVE_TOOLS[t]
        return True, cat, msg

    if t == "file_controller" and act in {"delete", "delete_file", "remove", "rmdir", "trash"}:
        return True, "destructive_action", "Deleting files requires explicit user approval."

    if t == "computer_settings" and act in {"shutdown", "restart", "toggle_wifi", "lock_screen"}:
        return True, "security_or_system_action", f"System action '{act}' requires explicit approval."

    if t == "game_updater" and act in {"install", "download", "update"}:
        return True, "software_install", "Installing or downloading software requires explicit approval."

    if t in {"desktop_ui", "screen_agent", "browser_control"}:
        if act == "press" and str(a.get("key") or "").strip().lower() in {"alt+f4", "winleft+l"}:
            return True, "consequential_desktop_action", "Closing windows or locking screen requires explicit approval."
        target_text = " ".join(str(a.get(k) or "") for k in ("target", "description", "text", "selector")).casefold()
        if act in {"click", "right_click", "double_click", "smart_click", "fill_form"}:
            for word in _SENSITIVE_CLICK_WORDS:
                if word in target_text:
                    return True, "consequential_ui_action", f"UI action involving '{word}' requires explicit approval."

    if t in {"run_project_command", "project_command", "run_command", "terminal"}:
        cmd = str(a.get("command") or "")
        if _SENSITIVE_COMMAND_RE.search(cmd):
            return True, "privileged_or_install_command", "Installing software, deleting recursively, or admin commands require explicit approval."

    # Check approved directory boundaries for file/project tools
    if sess is not None:
        for path_key in ("path", "file_path", "folder", "cwd", "target_path", "destination"):
            raw = a.get(path_key)
            if raw:
                candidate = Path(str(raw))
                if not candidate.is_absolute():
                    candidate = sess.approved_dirs[0] / candidate
                if not _is_inside_any(candidate, sess.approved_dirs):
                    return True, "outside_task_scope", f"Path '{raw}' is outside the approved task directories."

    return False, "", ""


def required_permission_category(tool: str, args: dict | None) -> str:
    t = str(tool or "").strip().lower()
    a = dict(args or {})
    act = str(a.get("action") or "").strip().lower().replace("-", "_")

    if t in {"screen_process"}:
        angle = str(a.get("angle") or "screen").strip().lower()
        if angle == "camera":
            return CAT_SENSITIVE
        return CAT_LOCAL_SCREEN
    if t in {"desktop_ui", "screen_agent"} and act in {"observe", "find"}:
        return CAT_LOCAL_SCREEN
    if t in {"desktop_ui", "screen_agent", "computer_control", "computer_settings", "open_app"}:
        return CAT_LOCAL_DESKTOP
    if t == "browser_control":
        return CAT_BROWSER
    if t in {"web_search", "fetch_page"}:
        return CAT_EXTERNAL_NETWORK
    if t in {"file_controller", "file_processor", "dev_agent", "code_helper",
             "read_project_file", "write_project_file", "edit_project_file",
             "list_project_files", "run_project_command", "project_command"}:
        return CAT_PROJECT_FILES
    if t in {"notes", "task_checkpoint"}:
        return CAT_PROJECT_FILES
    return CAT_SENSITIVE


def evaluate_tool_access(
    sess: AutonomousSession,
    tool: str,
    args: dict | None,
    *,
    now: float | None = None,
) -> tuple[bool, str, str]:
    """Evaluate whether ``tool`` with ``args`` is permitted under ``sess``.

    Returns ``(allowed, category, reason)``.
    """
    ok, reason = sess.is_valid(now=now)
    if not ok:
        return False, "expired_or_revoked", reason

    t = str(tool or "").strip().lower()
    a = dict(args or {})
    act = str(a.get("action") or "").strip().lower().replace("-", "_")

    # Recursive check for desktop_ui task/tool wrappers
    if t in {"desktop_ui", "screen_agent"} and act == "task":
        steps = a.get("steps")
        if not isinstance(steps, list) or not steps or len(steps) > 8:
            return False, "invalid_task_steps", "The desktop task is not a valid bounded sequence."
        for step in steps:
            if not isinstance(step, dict):
                return False, "invalid_task_steps", "A desktop task step is invalid."
            sub_ok, sub_cat, sub_reason = evaluate_tool_access(sess, "desktop_ui", step, now=now)
            if not sub_ok:
                return False, sub_cat, sub_reason
        return True, CAT_LOCAL_DESKTOP, ""

    if t in {"desktop_ui", "screen_agent"} and act == "tool":
        sub_name = str(a.get("tool_name") or "").strip()
        if sub_name not in {"open_app", "browser_control", "web_search", "file_controller"}:
            return False, "outside_task_scope", f"Routed tool {sub_name!r} is not permitted in autonomous desktop tasks."
        return evaluate_tool_access(sess, sub_name, a.get("tool_parameters") or {}, now=now)

    # Sensitive-operation gate ALWAYS runs before category permission grants
    is_sens, sens_cat, sens_reason = classify_sensitive_operation(t, a, sess)
    if is_sens:
        return False, sens_cat, sens_reason

    cat = required_permission_category(t, a)
    if cat not in sess.allowed_operations:
        return False, "permission_category_denied", (
            f"Permission category '{cat}' is not authorized for this autonomous task."
        )

    # Browser external network check
    if t == "browser_control" and act in {"go_to", "new_tab", "search"}:
        url = str(a.get("url") or "")
        if (act == "search" or (url and not _is_loopback_url(url))) and not sess.internet_access:
            return False, "network_disabled", "Internet access is disabled for this task session."

    # App allowlist check for open_app / focus_window
    if t == "open_app" or (t in {"desktop_ui", "screen_agent"} and act == "focus_window"):
        app = str(a.get("app_name") or a.get("target") or "").strip().casefold()
        if app and sess.permitted_apps:
            if not any(allowed in app or app in allowed for allowed in sess.permitted_apps):
                return False, "outside_task_scope", f"Application '{app}' is not in the permitted applications for this task."

    # Camera requires explicit direct user request even in autonomous mode
    if t == "screen_process" and str(a.get("angle") or "screen").strip().lower() == "camera":
        return False, "camera_not_authorized", "Camera access is never auto-authorized by overnight tasks."

    return True, cat, ""


def build_completion_report(sess: AutonomousSession, *, final_summary: str = "") -> str:
    """Build a structured verification-first Markdown completion report (Requirement 9)."""
    with _lock:
        d = sess.to_dict()

    actions_lines = [
        f"- [{e['ts']}] `{e['tool']}` ({e['category']}) → **{e['status']}**"
        + (f": {e['detail']}" if e.get("detail") else "")
        for e in d["execution_log"]
    ] or ["- None recorded."]

    files_lines = [f"- `{p}`" for p in d["files_modified"]] or ["- None modified."]

    test_lines = [
        f"- `{t['command']}` → **{'PASS' if t['passed'] else 'FAIL'}**: {t['summary']}"
        for t in d["tests_executed"]
    ] or ["- No tests executed."]

    failure_lines = [f"- {f}" for f in d["failures"]] or ["- None."]

    approval_lines = [
        f"- [{a['id']}] **{a['title']}** (`{a['category']}`, status=`{a['status']}`): {a['detail']}"
        for a in d["pending_approvals"]
    ] or ["- None."]

    remaining_lines = [
        f"- [{s['id']}] {s['title']} (status=`{s['status']}`)"
        + (f" — {s['note']}" if s.get("note") else "")
        for s in d["steps"]
        if s.get("status") != "done"
    ] or ["- None."]

    output_lines = [f"- `{p}`" for p in d["output_locations"]] or [
        f"- Session checkpoint: `{_SESSIONS_DIR / (sess.session_id + '.json')}`"
    ]

    actual_results = (final_summary or "").strip()
    if not actual_results:
        done_steps = [s for s in d["steps"] if s.get("status") == "done"]
        actual_results = (
            f"Completed {len(done_steps)}/{len(d['steps'])} planned step(s). "
            f"Session status: {d['status']}."
        )

    sections = [
        f"# REM Autonomous Task Completion Report",
        "",
        f"- **Session ID:** `{d['session_id']}`",
        f"- **Task ID:** `{d['task_id']}`",
        f"- **Status:** `{d['status']}`",
        "",
        "## Original Task",
        d["objective"],
        "",
        "## Actions Performed",
        *actions_lines,
        "",
        "## Files Modified",
        *files_lines,
        "",
        "## Tests Executed",
        *test_lines,
        "",
        "## Actual Results",
        actual_results,
        "",
        "## Failures",
        *failure_lines,
        "",
        "## Pending Approvals",
        *approval_lines,
        "",
        "## Remaining Work",
        *remaining_lines,
        "",
        "## Output Locations",
        *output_lines,
        "",
    ]
    return "\n".join(sections)


def save_completion_report(sess: AutonomousSession, *, final_summary: str = "",
                           reports_dir: Path | None = None) -> Path:
    rdir = Path(reports_dir) if reports_dir else _REPORTS_DIR
    rdir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y-%m-%d-%H%M%S", time.localtime())
    path = rdir / f"{stamp}-autonomous-{sess.task_id}.md"
    with _lock:
        if str(path) not in sess.output_locations:
            sess.output_locations.append(str(path))
        text = build_completion_report(sess, final_summary=final_summary)
    path.write_text(text, encoding="utf-8")
    try:
        sess.save()
    except Exception:
        pass
    return path
