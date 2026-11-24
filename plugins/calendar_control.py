from datetime import datetime, timedelta, timezone
from plugins._common import plugin, safe_run
from plugins._google_oauth import service

_SCOPES = ["https://www.googleapis.com/auth/calendar.events"]
PLUGIN = plugin("calendar_control", "Search, create, update, or delete Google Calendar events. Every calendar mutation requires on-screen confirmation and conflicting times are refused.", {
    "action": {"type": "STRING", "description": "search | create | update | delete"},
    "query": {"type": "STRING", "description": "Search text"},
    "event_id": {"type": "STRING", "description": "Event ID returned by search"},
    "summary": {"type": "STRING", "description": "New event title"},
    "start": {"type": "STRING", "description": "ISO-8601 start time with timezone offset"},
    "end": {"type": "STRING", "description": "ISO-8601 end time with timezone offset"},
    "timezone": {"type": "STRING", "description": "IANA timezone for event, such as Asia/Kolkata"},
}, ["action"])
PLUGIN_SETTINGS = {"namespace": "calendar_control", "title": "Google Calendar OAuth", "fields": [{"key": "client_secrets_file", "label": "OAuth client JSON path", "type": "text"}, {"key": "token_file", "label": "Token cache path", "type": "text"}, {"key": "timezone", "label": "IANA timezone", "type": "text", "placeholder": "Asia/Kolkata"}]}

def _run(p, player, memory):
    action = str(p.get("action", "")).lower()
    if action not in ("search", "create", "update", "delete"): return "Choose search, create, update, or delete."
    api = service("calendar_control", "calendar", "v3", _SCOPES).events()
    if action == "search":
        now = datetime.now(timezone.utc)
        data = api.list(calendarId="primary", timeMin=now.isoformat(), timeMax=(now+timedelta(days=60)).isoformat(), q=str(p.get("query", "")), singleEvents=True, orderBy="startTime", maxResults=20).execute()
        items = data.get("items", [])
        if not items: return "No matching upcoming events."
        from core import outcome
        return outcome.untrusted("calendar", "Events: " + "; ".join(f"{e.get('summary','(untitled)')} at {e.get('start',{}).get('dateTime',e.get('start',{}).get('date',''))}" for e in items))
    if action in ("update", "delete"):
        event_id = str(p.get("event_id", "")).strip()
        if not event_id or len(event_id) > 300: return "Provide a valid event_id from calendar search."
        try: current = api.get(calendarId="primary", eventId=event_id).execute()
        except Exception as exc: return f"Could not load the event: {type(exc).__name__}: {exc}"
        from core import confirm
        if confirm.pending_title(): return "Another confirmation is pending; no calendar change was made."
        if action == "delete":
            return confirm.request("calendar-delete", f"Delete calendar event: {str(current.get('summary','(untitled)'))[:80]}?", "Review the event before confirming deletion.", lambda: _delete_event(api,event_id))
        body = {"summary": str(p.get("summary") or current.get("summary") or "").strip()}
        try:
            if p.get("start"):
                start=datetime.fromisoformat(p["start"]); end=datetime.fromisoformat(p["end"])
                if start.tzinfo is None or end.tzinfo is None:
                    from zoneinfo import ZoneInfo
                    from plugins._common import config
                    zone=str(p.get("timezone") or config("calendar_control").get("timezone") or "").strip()
                    if not zone: return "Local updated times need an IANA timezone in the request or Calendar plugin settings."
                    tz=ZoneInfo(zone)
                    if start.tzinfo is None: start=start.replace(tzinfo=tz)
                    if end.tzinfo is None: end=end.replace(tzinfo=tz)
                if end<=start: return "End must be later than start."
                overlaps=api.list(calendarId="primary",timeMin=start.isoformat(),timeMax=end.isoformat(),singleEvents=True,maxResults=20).execute().get("items",[])
                if any(e.get("id")!=event_id for e in overlaps): return "Another calendar event overlaps the proposed time. Nothing was changed."
                body["start"]={"dateTime":start.isoformat()}; body["end"]={"dateTime":end.isoformat()}
            elif p.get("end"):
                return "Provide both start and end times when changing the event time."
        except Exception: return "Updated start/end must be valid ISO-8601 values with timezone offsets."
        return confirm.request("calendar-update", f"Update calendar event: {body['summary'][:80]}?", "Review the requested event changes before confirming.", lambda: _update_event(api,event_id,body))
    title = str(p.get("summary", "")).strip()
    try:
        start, end = datetime.fromisoformat(p["start"]), datetime.fromisoformat(p["end"])
        if start.tzinfo is None or end.tzinfo is None:
            from zoneinfo import ZoneInfo
            from plugins._common import config
            zone = str(p.get("timezone") or config("calendar_control").get("timezone") or "").strip()
            if not zone: return "Local start/end times need an IANA timezone in the request or Calendar plugin settings."
            tz = ZoneInfo(zone)
            if start.tzinfo is None: start = start.replace(tzinfo=tz)
            if end.tzinfo is None: end = end.replace(tzinfo=tz)
        if end <= start: return "End must be later than start."
    except Exception: return "Provide valid ISO-8601 start and end times with timezone offsets."
    if not title: return "Provide an event title."
    # Detect overlap before create; never modify existing events.
    overlaps = api.list(calendarId="primary", timeMin=start.isoformat(), timeMax=end.isoformat(), singleEvents=True, maxResults=20).execute().get("items", [])
    if overlaps: return "A calendar event overlaps this time: " + "; ".join(e.get("summary", "(untitled)") for e in overlaps[:5]) + ". No event was created."
    from core import confirm
    if confirm.pending_title(): return "Another confirmation is pending; no event was created."
    body = {"summary": title, "start": {"dateTime": start.isoformat()}, "end": {"dateTime": end.isoformat()}}
    return confirm.request("calendar-create", f"Create calendar event: {title[:80]}?", f"{start.isoformat()} to {end.isoformat()}. Review before confirming.", lambda: f"Created event {api.insert(calendarId='primary', body=body).execute().get('id','')}; calendar API response received.")

def _delete_event(api,event_id):
    try: api.delete(calendarId="primary",eventId=event_id).execute(); return "Calendar API accepted event deletion."
    except Exception as exc: return f"Calendar deletion failed: {type(exc).__name__}: {exc}"

def _update_event(api,event_id,body):
    try: api.patch(calendarId="primary",eventId=event_id,body=body).execute(); return "Calendar API accepted the event update."
    except Exception as exc: return f"Calendar update failed: {type(exc).__name__}: {exc}"

run = safe_run(_run)
