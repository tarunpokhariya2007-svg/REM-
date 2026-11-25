"""background_tasks — hand long work to an agent while the conversation goes on.

The tool is thin on purpose: everything that makes a task survive a restart,
respect a budget, wait out a quota or stay away from the user's computer lives
in core/tasks. This file turns what the model asked for into a call on the
engine and turns the answer back into facts (core/outcome.py) for the model to
say in the user's language.
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta
from pathlib import Path


def _when(at: str) -> float:
    """'YYYY-MM-DD HH:MM', or 'HH:MM' meaning the next time the clock reads it."""
    text = str(at or "").strip()
    now = datetime.now()
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M"):
        try:
            return datetime.strptime(text, fmt).timestamp()
        except ValueError:
            pass
    try:
        hm = datetime.strptime(text, "%H:%M")
    except ValueError:
        raise ValueError("'at' must be 'YYYY-MM-DD HH:MM' or 'HH:MM'") from None
    when = now.replace(hour=hm.hour, minute=hm.minute, second=0, microsecond=0)
    if when <= now:
        when += timedelta(days=1)
    return when.timestamp()


def _minutes(task: dict) -> str:
    return f"{task['used']['seconds'] / 60:.1f}/{task['budget']['minutes']} min"


def _row(t: dict) -> str:
    return f"{t['id']} [{t['status']}] {t['agent']} · {t['title']} · {_minutes(t)}"


def background_tasks(parameters: dict, player=None, session_memory=None) -> str:
    from core import outcome
    from core.tasks import ACTIVE
    from core.tasks import desktop

    eng = desktop.ensure(player)
    a = dict(parameters or {})
    action = str(a.get("action") or "list").strip().lower()

    if action == "autonomous_status":
        from core import autonomous_mode
        return outcome.done("background_tasks", **autonomous_mode.snapshot())

    if action == "revoke":
        from core import autonomous_mode
        ok = autonomous_mode.revoke_session(reason="revoked via background_tasks", disable_mode=True)
        return outcome.done("background_tasks", revoked=ok, mode=autonomous_mode.get_mode())

    if action == "start":
        task = str(a.get("task") or "").strip()
        if not task:
            return outcome.failed("background_tasks", "start needs the whole task, in the user's words")
        not_before = 0.0
        if a.get("at"):
            try:
                not_before = _when(a["at"])
            except ValueError as e:
                return outcome.failed("background_tasks", str(e))
        files = [a["file_path"]] if a.get("file_path") else None

        want_autonomous = bool(a.get("autonomous")) or str(a.get("agent") or "").lower() == "autonomous"
        extra: dict = {}
        from core import autonomous_mode
        source = str((session_memory or {}).get("source") or "typed").strip().lower()
        if want_autonomous or autonomous_mode.is_enabled():
            if source not in autonomous_mode.TRUSTED_SOURCES:
                return outcome.failed(
                    "background_tasks",
                    "Overnight Autonomous Mode tasks require a direct trusted user channel.",
                )
            if want_autonomous and not autonomous_mode.is_enabled():
                return outcome.failed(
                    "background_tasks",
                    "REM AUTONOMOUS MODE is DISABLED. Enable Overnight Autonomous Mode in the HUD first.",
                )
            if autonomous_mode.is_enabled():
                approved_dirs = a.get("approved_dirs")
                if not approved_dirs:
                    approved_dirs = [str(Path(__file__).resolve().parent.parent)]
                minutes = int(a.get("minutes") or 240)
                sess = autonomous_mode.start_session(
                    task,
                    source=source,
                    approved_dirs=approved_dirs,
                    permitted_apps=a.get("permitted_apps"),
                    internet_access=bool(a.get("internet_access", True)),
                    max_duration_s=max(60.0, minutes * 60.0),
                )
                extra["autonomous_session_id"] = sess.session_id

        try:
            agent_name = "general" if str(a.get("agent") or "").lower() == "autonomous" else (a.get("agent") or "general")
            t = eng.start(task, agent=agent_name, minutes=a.get("minutes"),
                          deliver=a.get("deliver") or "voice", not_before=not_before,
                          files=files, extra=extra)
        except ValueError as e:
            return outcome.failed("background_tasks", str(e))
        starts = time.strftime("%Y-%m-%d %H:%M", time.localtime(not_before)) if not_before else "now"
        return outcome.started("background_tasks", task_id=t["id"], agent=t["agent"],
                               minutes=t["budget"]["minutes"], starts=starts,
                               autonomous=bool(extra.get("autonomous_session_id")),
                               note="the result is announced when it is done; the conversation "
                                    "carries on meanwhile")

    if action == "agents":
        return "\n".join(f"{p.name}: {p.description} (default {p.minutes} min)"
                         for p in eng.agents(reload=True).values())

    if action == "list":
        rows = [_row(t) for t in eng.list(10)]
        return "\n".join(rows) if rows else "No background tasks yet."

    tid = str(a.get("task_id") or "").strip()
    tasks = eng.list(50)
    if tid:
        task = eng.get(tid)
    else:
        pool = tasks if action == "result" else [t for t in tasks if t["status"] in ACTIVE]
        task = pool[0] if pool else None
    if task is None:
        return outcome.failed("background_tasks", "no such task" if tid else "there is no task to act on")

    if action == "status":
        recent = [e["text"] for e in task.get("events") or [] if e["kind"] in ("progress", "tool")][-4:]
        return outcome.done("background_tasks", task_id=task["id"], status=task["status"],
                            agent=task["agent"], title=task["title"], time=_minutes(task),
                            rounds=f"{task['used']['rounds']}/{task['budget']['rounds']}",
                            recent=" | ".join(recent) or None, error=task.get("error") or None)
    if action == "result":
        if task["status"] != "done":
            return outcome.done("background_tasks", task_id=task["id"], status=task["status"],
                                note="not finished yet")
        return (outcome.done("background_tasks", task_id=task["id"], report=task.get("report") or None)
                + "\n" + outcome.untrusted("task-result", (task.get("result") or "")[:12_000]))
    if action == "finish":
        ok = eng.cancel(task["id"], finish=True)
        return (outcome.started("background_tasks", task_id=task["id"],
                                note="stopping and writing up what was found")
                if ok else outcome.failed("background_tasks", "that task is not running"))
    if action == "cancel":
        if task.get("autonomous_session_id"):
            from core import autonomous_mode
            autonomous_mode.revoke_session(task["autonomous_session_id"], reason="task cancelled")
        ok = eng.cancel(task["id"])
        return (outcome.done("background_tasks", task_id=task["id"], cancelled=True)
                if ok else outcome.failed("background_tasks", "that task is not running"))
    if action == "steer":
        ok = eng.steer(task["id"], a.get("note") or "")
        return (outcome.done("background_tasks", task_id=task["id"], note_added=True)
                if ok else outcome.failed("background_tasks", "needs a running task and a note"))
    return outcome.failed("background_tasks", f"unknown action '{action}'")


TOOL = {
    "name": "background_tasks",
    "description": (
        "Long work that runs in the background on this computer while the conversation carries on: "
        "real research, reading and comparing many sources, writing a report, working through a "
        "list, or Overnight Autonomous Mode tasks when REM AUTONOMOUS MODE is ENABLED. "
        "start = hand the task to an agent — agent 'researcher' for research, "
        "'general' for other work, or 'autonomous' / autonomous=true when REM AUTONOMOUS MODE is ENABLED; "
        "task = the whole request, self-contained, in the user's words; "
        "minutes = time budget; at = start later ('YYYY-MM-DD HH:MM' or 'HH:MM'); "
        "file_path = a file to work from. "
        "list = tasks; status = progress; result = finished result; finish = stop and write report; "
        "cancel = stop; steer = add instruction; agents = list agents; "
        "autonomous_status = inspect REM AUTONOMOUS MODE state; revoke = revoke trusted autonomous session."),
    "brief": "Hands long or overnight autonomous work to a background agent: start, list, status, result, finish, cancel, steer, autonomous_status, revoke.",
    "tier": "core",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING",
                       "enum": ["start", "list", "status", "result", "finish", "cancel", "steer", "agents", "autonomous_status", "revoke"]},
            "task": {"type": "STRING", "description": "for start: the whole task, self-contained"},
            "agent": {"type": "STRING", "description": "for start: researcher | general | autonomous"},
            "autonomous": {"type": "BOOLEAN", "description": "for start: require/use active REM AUTONOMOUS MODE session"},
            "approved_dirs": {"type": "ARRAY", "items": {"type": "STRING"}, "description": "approved project directories"},
            "minutes": {"type": "INTEGER", "description": "for start: time budget in minutes"},
            "at": {"type": "STRING", "description": "for start: when to begin, local time"},
            "deliver": {"type": "STRING", "enum": ["voice", "panel", "silent"],
                        "description": "for start: how to announce the result (default voice)"},
            "file_path": {"type": "STRING", "description": "for start: a file to work from"},
            "task_id": {"type": "STRING"},
            "note": {"type": "STRING", "description": "for steer: the new instruction"},
        },
        "required": ["action"],
    },
    "handler": background_tasks,
}
