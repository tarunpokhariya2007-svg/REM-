from pathlib import Path
from plugins._common import config, plugin, safe_run

PLUGIN=plugin("image_generator","Generate an image through a configured OpenAI-compatible image-generation endpoint. Credentials are read from plugin settings; output paths are never overwritten.",{"prompt":{"type":"STRING","description":"Image prompt"},"output_path":{"type":"STRING","description":"New PNG output path"}},["prompt","output_path"])
PLUGIN_SETTINGS={"namespace":"image_generator","title":"Image Provider","fields":[{"key":"endpoint","label":"Images API endpoint","type":"text"},{"key":"api_key","label":"Provider API key","type":"password"},{"key":"model","label":"Image model","type":"text"}]}
def _run(p,player,memory):
    cfg=config("image_generator"); endpoint=str(cfg.get("endpoint","")).strip(); key=str(cfg.get("api_key","")).strip(); model=str(cfg.get("model","")).strip()
    if not endpoint or not key or not model: return "No image provider is configured. Add an OpenAI-compatible images endpoint, model, and credential in plugin settings."
    prompt=str(p.get("prompt","")).strip(); path=Path(str(p.get("output_path","")).strip()).expanduser()
    if not prompt or len(prompt)>4000: return "Provide an image prompt up to 4000 characters."
    if path.suffix.lower() not in (".png",".jpg",".jpeg",".webp"): return "Output path must end in .png, .jpg, .jpeg, or .webp."
    if path.exists(): return "Output already exists; it was not overwritten. Choose a new path."
    try:
        import requests, base64
        from PIL import Image
        response=requests.post(endpoint,headers={"Authorization":"Bearer "+key},json={"model":model,"prompt":prompt,"n":1,"size":"1024x1024","response_format":"b64_json"},timeout=180); response.raise_for_status()
        payload=response.json()["data"][0]["b64_json"]; raw=base64.b64decode(payload,validate=True)
        path.parent.mkdir(parents=True,exist_ok=True)
        with path.open("xb") as stream: stream.write(raw)
        with Image.open(path) as img: img.verify()
        return f"Generated and verified image at {path}."
    except Exception as exc: return f"Image generation failed safely: {type(exc).__name__}: {exc}"
run=safe_run(_run)
