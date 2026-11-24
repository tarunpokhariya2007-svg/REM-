from plugins._common import plugin, route, safe_run
from plugins._whatsapp_core import observe

PLUGIN = plugin("whatsapp_call", "Inspect the visible WhatsApp conversation and locate call controls. A call is never placed automatically; ask the user to confirm before any call action.", {
    "action": {"type": "STRING", "description": "inspect | request_call | end_call"},
    "contact": {"type": "STRING", "description": "Expected visible contact name"},
    "video": {"type": "BOOLEAN", "description": "Request video rather than voice call"},
}, ["action"])

def _run(p, player, memory):
    action = str(p["action"]).lower()
    if action not in ("inspect", "request_call", "end_call"): return "Choose inspect, request_call, or end_call."
    observed = observe(player)
    if action == "inspect": return observed
    if action == "end_call":
        from core import screenview
        view=screenview.look(budget_s=1.2,allow_ocr=False)
        if view.degraded or "whatsapp" not in (view.app+" "+view.title).casefold(): return "No accessible active WhatsApp call window was found; no control was clicked."
        names=[e.label.casefold() for e in view.elements if e.interactable]
        if not any("end call" in name or "hang up" in name for name in names): return "No visible end-call control was identified; call state is unverified and nothing was clicked."
        result=route(player,"screen_agent",{"action":"click","target":"end call"})
        return f"End-call control action: {result}. This confirms the control interaction only; call disconnection is not independently verified."
    contact = str(p.get("contact", "")).strip()
    if not contact: return "Name the contact before requesting a call."
    from core import confirm
    if confirm.pending_title(): return "Another confirmation is pending; no call was started."
    def start_call():
        from core import screenview
        fresh = screenview.look(budget_s=1.2, allow_ocr=False)
        if fresh.degraded or "whatsapp" not in (fresh.app + " " + fresh.title).casefold() or contact.casefold() not in fresh.title.casefold(): return "The requested contact is not confirmed as the active WhatsApp conversation; no call was started."
        target = "video call" if p.get("video") else "voice call"
        found = route(player, "screen_agent", {"action": "find", "target": target})
        if "match': None" in found or '"match": None' in found: return f"Could not confirm a unique {target} control; no call was started. {found}"
        return route(player, "screen_agent", {"action": "click", "target": target})
    return confirm.request("whatsapp-call", f"Start {'video' if p.get('video') else 'voice'} call with {contact}?", "Review the active WhatsApp conversation and confirm the contact before the call begins.", start_call)

run = safe_run(_run)
