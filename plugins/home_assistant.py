from plugins._common import config, plugin, safe_run
PLUGIN = plugin("home_assistant", "Read Home Assistant entities and control configured lights or switches. Lock, alarm, and security entities are never controlled by this plugin.", {"action": {"type": "STRING", "description": "list | state | turn_on | turn_off"}, "entity_id": {"type": "STRING", "description": "Home Assistant entity ID"}}, ["action"])
PLUGIN_SETTINGS = {"namespace": "home_assistant", "title": "Home Assistant", "fields": [{"key": "url", "label": "Instance URL", "type": "text"}, {"key": "token", "label": "Long-lived access token", "type": "password"}]}
def _run(p, player, memory):
    cfg=config("home_assistant"); base=str(cfg.get("url","")).rstrip("/"); token=str(cfg.get("token","")); action=str(p.get("action","")).lower(); eid=str(p.get("entity_id",""))
    if not base or not token: return "Configure the Home Assistant URL and access token in plugin settings."
    if action not in ("list","state","turn_on","turn_off"): return "Choose list, state, turn_on, or turn_off."
    if action != "list" and (len(eid)>160 or not __import__("re").fullmatch(r"[a-z0-9_]+\.[a-z0-9_]+",eid)):
        return "Provide an entity ID in domain.name format."
    try:
        import requests
        headers={"Authorization":f"Bearer {token}","Content-Type":"application/json"}
        if action=="list":
            r=requests.get(base+"/api/states",headers=headers,timeout=10); r.raise_for_status(); return "Entities: "+", ".join(x.get("entity_id","") for x in r.json()[:100])
        if not eid or len(eid)>160: return "Provide a valid entity ID."
        r=requests.get(base+"/api/states/"+eid,headers=headers,timeout=10); r.raise_for_status(); state=r.json()
        if action=="state": return f"{eid}: {state.get('state')}"
        domain=eid.split(".",1)[0]
        if domain not in ("light","switch"): return "Only light and switch entities are controllable; security-sensitive and other domains are refused."
        from core import confirm
        if confirm.pending_title(): return "Another confirmation is pending; no device command was sent."
        def change():
            resp=requests.post(base+f"/api/services/{domain}/{action}",headers=headers,json={"entity_id":eid},timeout=10); resp.raise_for_status(); return f"Home Assistant accepted {action} for {eid}; read state again to verify."
        return confirm.request("ha-"+eid,f"Turn {action.removeprefix('turn_')} {eid}?","Confirm this Home Assistant device command.",change)
    except Exception as exc: return f"Home Assistant request failed safely: {type(exc).__name__}: {exc}"
run=safe_run(_run)
