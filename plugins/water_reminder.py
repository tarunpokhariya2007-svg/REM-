from datetime import datetime, timezone
import threading
from plugins._common import config, data_file, plugin, read_json, write_json, safe_run

PLUGIN = plugin("water_reminder", "Record hydration intake, report today's logged total toward a personal target, and enable or disable optional reminders; this makes no medical recommendations.", {
    "action": {"type": "STRING", "description": "status | record | goal | enable | disable"},
    "amount_ml": {"type": "INTEGER", "description": "Amount recorded in milliliters"},
    "goal_ml": {"type": "INTEGER", "description": "Personal daily tracking target in milliliters (250-10000); not medical advice"},
}, ["action"])
PLUGIN_SETTINGS = {"namespace": "water_reminder", "title": "Water Reminder", "fields": [{"key": "interval_minutes", "label": "Reminder interval (minutes)", "type": "text", "default": "60"}]}
_timer = None
_player = None
_timer_lock = threading.RLock()

def _schedule(minutes):
    global _timer
    with _timer_lock:
        if _timer: _timer.cancel()
        def remind():
            global _timer
            say = getattr(_player, "request_say", None) if _player is not None else None
            if callable(say):
                try: say("Hydration reminder. If you would like, record water intake with REM.")
                except Exception: pass
            _timer = threading.Timer(minutes*60, remind); _timer.daemon = True; _timer.start()
        _timer = threading.Timer(minutes*60, remind); _timer.daemon = True; _timer.start()

def _today_total(state):
    today = datetime.now(timezone.utc).date().isoformat()
    total = 0
    for entry in state.get("entries", []):
        if not isinstance(entry, dict):
            continue
        stamp = str(entry.get("at", ""))
        amount = entry.get("amount_ml")
        if stamp.startswith(today) and isinstance(amount, int) and not isinstance(amount, bool):
            total += amount
    return total

def _run(p, player, memory):
    global _player, _timer
    _player = player
    path = data_file("water_reminder.json")
    state = read_json(path, {"enabled": False, "entries": []})
    if not isinstance(state, dict): state = {"enabled": False, "entries": []}
    if not isinstance(state.get("entries"), list): state["entries"] = []
    goal = state.get("daily_goal_ml", 2000)
    if not isinstance(goal, int) or isinstance(goal, bool) or not 250 <= goal <= 10000:
        goal = 2000
    action = str(p["action"]).lower()
    if action == "status":
        if state.get("enabled") and _timer is None:
            try: interval = int(config("water_reminder").get("interval_minutes", 60))
            except (TypeError, ValueError): interval = 60
            if 5 <= interval <= 1440: _schedule(interval)
        total = _today_total(state)
        return (f"Hydration reminders are {'enabled' if state.get('enabled') else 'disabled'}; "
                f"today's logged intake is {total} ml toward your personal {goal} ml target "
                f"({len(state['entries'])} total intake entries recorded). This is tracking, not medical advice.")
    if action == "goal":
        value = p.get("goal_ml")
        if not isinstance(value, int) or isinstance(value, bool) or not 250 <= value <= 10000:
            return "Choose a personal tracking target from 250 to 10000 ml. No medical target was inferred."
        state["daily_goal_ml"] = value
        write_json(path, state)
        return f"Personal hydration tracking target set to {value} ml per day; this is not medical advice."
    if action in ("enable", "disable"):
        if action == "enable":
            try: interval = int(config("water_reminder").get("interval_minutes", 60))
            except (TypeError, ValueError): interval = 60
            if not 5 <= interval <= 1440: return "Reminder interval must be between 5 minutes and 24 hours; reminders were not enabled."
            _schedule(interval)
        else:
            with _timer_lock:
                if _timer: _timer.cancel(); _timer = None
        state["enabled"] = action == "enable"; write_json(path, state)
        return f"Hydration reminders {action}d. REM does not set a daily target or provide medical advice."
    if action == "record":
        amount = p.get("amount_ml")
        if not isinstance(amount, int) or not 1 <= amount <= 5000: return "Give an amount between 1 and 5000 ml."
        state["entries"].append({"amount_ml": amount, "at": datetime.now(timezone.utc).isoformat()})
        write_json(path, state)
        total = _today_total(state)
        return f"Recorded {amount} ml. Today's logged total is {total} ml of your personal {goal} ml tracking target."
    return "Choose status, record, enable, or disable."

run = safe_run(_run)
