from datetime import datetime, timezone
import threading
from plugins._common import config, data_file, plugin, read_json, write_json, safe_run

PLUGIN = plugin("water_reminder", "Record hydration intake and enable or disable optional reminders; this makes no medical recommendations.", {
    "action": {"type": "STRING", "description": "status | record | enable | disable"},
    "amount_ml": {"type": "INTEGER", "description": "Amount recorded in milliliters"},
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

def _run(p, player, memory):
    global _player, _timer
    _player = player
    path = data_file("water_reminder.json")
    state = read_json(path, {"enabled": False, "entries": []})
    action = str(p["action"]).lower()
    if action == "status":
        if state.get("enabled") and _timer is None:
            try: interval = int(config("water_reminder").get("interval_minutes", 60))
            except (TypeError, ValueError): interval = 60
            if 5 <= interval <= 1440: _schedule(interval)
        return f"Hydration reminders are {'enabled' if state.get('enabled') else 'disabled'}; {len(state.get('entries', []))} intake entries recorded."
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
        state.setdefault("entries", []).append({"amount_ml": amount, "at": datetime.now(timezone.utc).isoformat()})
        write_json(path, state); return f"Recorded {amount} ml."
    return "Choose status, record, enable, or disable."

run = safe_run(_run)
