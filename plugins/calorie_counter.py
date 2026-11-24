from pathlib import Path
from plugins._common import config, plugin, safe_run

PLUGIN=plugin("calorie_counter","Estimate food and portion from a supplied image using REM's configured vision model. Estimates are approximate and not nutritional measurements; webcam access requires confirmation.",{"image_path":{"type":"STRING","description":"Optional food image path"},"use_camera":{"type":"BOOLEAN","description":"Request a one-shot webcam capture"}},[])
PLUGIN_SETTINGS={"namespace":"calorie_counter","title":"Food Image Estimate","fields":[{"key":"camera_index","label":"Camera index","type":"text","default":"0"}]}
def _analyze(raw):
    from google.genai import types
    from core import gemini
    response=gemini.call([types.Part.from_bytes(data=raw,mime_type="image/jpeg"),"Describe the visible food and estimate a broad calorie range for the apparent portion. State uncertainty clearly; do not claim precise measurement."],tier=gemini.FAST,timeout_ms=30000)
    return (response.text or "No estimate returned.") if response else "The configured vision model did not return an estimate."
def _run(p,player,memory):
    image_path=str(p.get("image_path","")).strip()
    if image_path:
        try: return _analyze(Path(image_path).expanduser().read_bytes())
        except Exception as exc: return f"Could not analyze image: {type(exc).__name__}: {exc}"
    if not p.get("use_camera"): return "Provide an image path, or explicitly request webcam capture."
    from core import confirm
    if confirm.pending_title(): return "Another confirmation is pending; camera was not opened."
    def capture():
        try:
            import cv2
            try: index=int(config("calorie_counter").get("camera_index",0))
            except (TypeError,ValueError): index=0
            if not 0<=index<=8: return "Configured camera index must be between 0 and 8."
            camera=cv2.VideoCapture(index)
            try:
                if not camera.isOpened(): return "Camera unavailable; no image was captured."
                ok,frame=camera.read()
                if not ok: return "Camera capture failed."
                ok,encoded=cv2.imencode(".jpg",frame)
                return _analyze(encoded.tobytes()) if ok else "Could not encode the camera frame."
            finally: camera.release()
        except Exception as exc: return f"Camera analysis unavailable: {type(exc).__name__}: {exc}"
    return confirm.request("calorie-camera","Allow one-time camera capture for food estimate?","REM will capture one still frame and send it to the configured model.",capture)
run=safe_run(_run)
