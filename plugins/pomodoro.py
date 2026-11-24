import threading
from datetime import datetime, timezone
from plugins._common import data_file, plugin, read_json, write_json, safe_run

_lock = threading.RLock()
_timer = None
_player = None
_state = {"status": "stopped", "phase": "work", "seconds_left": 0}

PLUGIN = plugin("pomodoro", "Start, pause, resume, inspect, or stop a Pomodoro timer. Notifications are spoken through REM when available.", {
    "action": {"type": "STRING", "description": "start | pause | resume | stop | status"},
    "work_minutes": {"type": "INTEGER", "description": "Work duration (1-180 minutes)"},
    "break_minutes": {"type": "INTEGER", "description": "Break duration (1-60 minutes)"},
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
            _state["phase"] = "break" if _state["phase"] == "work" else "work"
            _state["seconds_left"] = (_state.get("break_seconds", 300) if _state["phase"] == "break" else _state.get("work_seconds", 1500))
            _notify(f"Pomodoro { _state['phase'] } interval started.")
            try:
                path = data_file("pomodoro.json"); data = read_json(path, {"completed": []})
                data.setdefault("completed", []).append({"phase": _state["phase"], "at": datetime.now(timezone.utc).isoformat()})
                write_json(path, data)
            except Exception: pass
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
            _state.update(status="running", phase="work", work_seconds=work*60, break_seconds=brk*60, seconds_left=work*60)
            _tick(); return f"Pomodoro started: {work} minutes of work, then {brk} minutes break."
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
            return f"Pomodoro is {_state['status']}; phase {_state['phase']}; {_state['seconds_left']} seconds remain."
    return "Choose start, pause, resume, stop, or status."

run = safe_run(_run)
