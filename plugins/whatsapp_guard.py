from plugins._common import config, plugin, safe_run

PLUGIN = plugin("whatsapp_guard", "Check the visible WhatsApp window for an incoming call and report configured policy. This build does not run a hidden call monitor; automatic response is disabled.", {
    "action": {"type": "STRING", "description": "status | check | configure"},
    "auto_decline": {"type": "BOOLEAN", "description": "Explicitly allow automatic decline behavior (not implemented by this visible-UI build)"},
    "auto_reply": {"type": "STRING", "description": "Optional reply policy; never sent by this build"},
}, ["action"])
PLUGIN_SETTINGS = {"namespace": "whatsapp_guard", "title": "WhatsApp Guard", "fields": [{"key": "auto_decline", "label": "Allow automatic decline (future monitor required)", "type": "toggle"}]}

def _run(p, player, memory):
    action = str(p["action"]).lower()
    if action == "status":
        cfg = config("whatsapp_guard")
        return f"WhatsApp guard is not monitoring calls in the background. Auto-decline setting: {bool(cfg.get('auto_decline'))}; automatic replies are disabled."
    if action == "check":
        from core import screenview
        view=screenview.look(budget_s=1.2,allow_ocr=True)
        if view.degraded: return f"Incoming-call state is unverified: {view.reason or 'the active UI could not be read'}."
        if "whatsapp" not in (view.app+" "+view.title).casefold(): return "WhatsApp is not the active window; incoming-call state is unknown."
        controls=[e.label for e in view.elements if e.interactable]
        call_controls=[name for name in controls if any(term in name.casefold() for term in ("answer","decline","incoming call"))]
        return "Possible incoming call controls are visible: " + ", ".join(call_controls[:6]) + ". No call was answered or declined." if call_controls else "No incoming-call controls were detected in the visible WhatsApp window."
    if action == "configure":
        return "Use plugin settings to configure the explicit policy. This build does not auto-answer, decline, or reply to calls."
    return "Choose status, check, or configure."

run = safe_run(_run)
