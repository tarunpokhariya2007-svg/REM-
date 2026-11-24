from plugins._common import plugin, route, safe_run

PLUGIN = plugin("chat_takeover", "Read the visible chat UI and prepare a reply; sending is delegated to REM's messaging action which requires its existing confirmation step.", {
    "action": {"type": "STRING", "description": "read | draft | send"},
    "contact": {"type": "STRING", "description": "Expected conversation name"},
    "reply": {"type": "STRING", "description": "Reply text"},
}, ["action"])

def _run(p, player, memory):
    action, contact = str(p["action"]).lower(), str(p.get("contact", "")).strip()
    if action == "read": return route(player, "screen_agent", {"action": "observe"})
    if action == "draft":
        reply = str(p.get("reply", "")).strip()
        return f"Draft for {contact or 'the visible chat'}: {reply}" if reply else "Provide the reply text to draft."
    if action == "send":
        if not contact or not str(p.get("reply", "")).strip(): return "Provide both an exact contact name and reply text."
        return route(player, "send_message", {"platform": "whatsapp", "receiver": contact, "message_text": str(p["reply"])})
    return "Choose read, draft, or send. Sending uses the existing confirmation gate."

run = safe_run(_run)
