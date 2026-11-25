import threading
from datetime import datetime, timezone
from plugins._common import data_file, plugin, read_json, write_json, safe_run

_lock = threading.RLock()
_timer = None
_player = None
_state = {"status": "stopped", "phase": "work", "seconds_left": 0,
          "task": "", "work_blocks": 0}

PLUGIN = plugin("pomodoro", "Start, pause, resume, inspect, or stop a Pomodoro timer. Notifications are spoken through REM when available.", {
    "action": {"type": "STRING", "description": "start | pause | resume | stop | status | stats"},
    "work_minutes": {"type": "INTEGER", "description": "Work duration (1-180 minutes)"},
    "break_minutes": {"type": "INTEGER", "description": "Break duration (1-60 minutes)"},
    "task": {"type": "STRING", "description": "Optional short label for this focus session"},
}, ["action"])

def _notify(text):
    say = getattr(_player, "request_say", None) if _player is not None else None
    if callable(say):
        try: say(text)
        except Exception: pass
    try:
        # The timer thread only writes to a persisted state file. The next call
        # reports it; no GUI object or thread-unsafe widget is touched.
        path = data_file("pomodoro.json"); state = read_json(path, {})
        state["notification"] = text; write_json(path, state)
    except Exception: pass

def _tick():
    global _timer
    with _lock:
        if _state["status"] != "running": return
        _state["seconds_left"] -= 1
        if _state["seconds_left"] <= 0:
            completed_phase = _state["phase"]
            if completed_phase == "work":
                _state["work_blocks"] = int(_state.get("work_blocks", 0)) + 1
                try:
                    path = data_file("pomodoro.json")
                    data = read_json(path, {"completed": []})
                    if not isinstance(data, dict): data = {"completed": []}
                    completed = data.get("completed")
                    if not isinstance(completed, list): completed = []
                    completed.append({
                        "phase": "work",
                        "minutes": max(1, int(_state.get("work_seconds", 1500)) // 60),
                        "task": str(_state.get("task", ""))[:60],
                        "at": datetime.now(timezone.utc).isoformat(),
                    })
                    data["completed"] = completed[-500:]
                    write_json(path, data)
                except Exception:
                    pass
            _state["phase"] = "break" if completed_phase == "work" else "work"
            if _state["phase"] == "break":
                short_break = _state.get("break_seconds", 300)
                long_break = max(short_break, 900)
                cycle = int(_state.get("work_blocks", 0))
                _state["seconds_left"] = long_break if cycle and cycle % 4 == 0 else short_break
            else:
                _state["seconds_left"] = _state.get("work_seconds", 1500)
            _notify(f"Pomodoro { _state['phase'] } interval started.")
        _timer = threading.Timer(1, _tick); _timer.daemon = True; _timer.start()

def _run(p, player, memory):
    global _timer
    global _player
    _player = player
    action = str(p["action"]).lower()
    with _lock:
        if action == "start":
            work, brk = p.get("work_minutes", 25), p.get("break_minutes", 5)
            if not isinstance(work, int) or not 1 <= work <= 180 or not isinstance(brk, int) or not 1 <= brk <= 60: return "Work must be 1-180 minutes and break 1-60 minutes."
            if _timer: _timer.cancel()
            task = str(p.get("task", "") or "").strip()[:60]
            _state.update(status="running", phase="work", work_seconds=work*60, break_seconds=brk*60, seconds_left=work*60, task=task, work_blocks=0)
            _tick(); return f"Pomodoro started: {work} minutes of work, then {brk} minutes break" + (f" on {task}." if task else ".")
        if action == "pause":
            if _timer: _timer.cancel(); _timer = None
            _state["status"] = "paused"; return "Pomodoro paused."
        if action == "resume":
            if _state["status"] != "paused": return "There is no paused Pomodoro timer."
            _state["status"] = "running"; _tick(); return "Pomodoro resumed."
        if action == "stop":
            if _timer: _timer.cancel(); _timer = None
            _state.update(status="stopped", seconds_left=0); return "Pomodoro stopped."
        if action == "status":
            task = f"; task {_state.get('task')}" if _state.get("task") else ""
            return f"Pomodoro is {_state['status']}; phase {_state['phase']}; {_state['seconds_left']} seconds remain{task}."
        if action == "stats":
            data = read_json(data_file("pomodoro.json"), {"completed": []})
            rows = data.get("completed", []) if isinstance(data, dict) else []
            today = datetime.now(timezone.utc).date().isoformat()
            focus = [r for r in rows if isinstance(r, dict) and r.get("phase") == "work"
                     and str(r.get("at", "")).startswith(today)]
            minutes = sum(int(r.get("minutes", 0)) for r in focus
                          if isinstance(r.get("minutes", 0), int) and r.get("minutes", 0) > 0)
            tasks = {}
            for row in focus:
                task = str(row.get("task", "")).strip()
                if task: tasks[task] = tasks.get(task, 0) + int(row.get("minutes", 0))
            detail = "; top tasks: " + ", ".join(
                f"{name} ({value} min)" for name, value in sorted(tasks.items(), key=lambda x: -x[1])[:3]
            ) if tasks else ""
            return f"Today: {len(focus)} completed focus blocks, {minutes} minutes{detail}."
    return "Choose start, pause, resume, stop, or status."

run = safe_run(_run)
