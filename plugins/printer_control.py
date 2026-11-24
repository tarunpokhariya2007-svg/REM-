from plugins._common import config, plugin, safe_run
PLUGIN=plugin("printer_control","Read printer telemetry over MQTT. This recovery build is read-only and never starts, stops, or alters a print job.",{"action":{"type":"STRING","description":"status"}},["action"])
PLUGIN_SETTINGS={"namespace":"printer_control","title":"MQTT Printer","fields":[{"key":"host","label":"MQTT broker","type":"text"},{"key":"port","label":"Port","type":"text"},{"key":"username","label":"Username","type":"text"},{"key":"password","label":"Password","type":"password"},{"key":"topic","label":"Status topic","type":"text"}]}
def _run(p,player,memory):
    cfg=config("printer_control")
    if str(p.get("action","status")).lower()!="status": return "Only read-only status is supported."
    if not cfg.get("host") or not cfg.get("topic"): return "Configure the MQTT broker and printer status topic in plugin settings."
    try: import paho.mqtt.client as mqtt
    except ImportError: return "Printer telemetry needs the optional paho-mqtt package."
    result={"payload":None,"error":None}
    def on_connect(client,userdata,flags,rc,properties=None):
        if rc!=0: result["error"]=f"MQTT connection refused ({rc})"; client.disconnect(); return
        client.subscribe(str(cfg["topic"]),qos=0)
    def on_message(client,userdata,msg): result["payload"]=msg.payload[:8192].decode("utf-8",errors="replace"); client.disconnect()
    try:
        client=mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        if cfg.get("username"): client.username_pw_set(str(cfg["username"]),str(cfg.get("password","")))
        client.on_connect,client.on_message=on_connect,on_message; client.connect(str(cfg["host"]),int(cfg.get("port",1883)),10); client.loop_start()
        import time; deadline=time.monotonic()+5
        while time.monotonic()<deadline and result["payload"] is None and result["error"] is None: time.sleep(.05)
        client.loop_stop(); client.disconnect()
        return f"Printer status payload: {result['payload']}" if result["payload"] is not None else (result["error"] or "No telemetry arrived before timeout.")
    except Exception as exc: return f"MQTT status read failed: {type(exc).__name__}: {exc}"
run=safe_run(_run)
