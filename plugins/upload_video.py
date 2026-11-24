from pathlib import Path
from plugins._common import plugin, safe_run
from plugins._google_oauth import service

PLUGIN=plugin("upload_video","Upload a local video to YouTube using configured OAuth. The upload is never started until the on-screen confirmation is approved.",{"file_path":{"type":"STRING","description":"Local video path"},"title":{"type":"STRING","description":"Video title"},"description":{"type":"STRING","description":"Video description"},"privacy":{"type":"STRING","description":"private | unlisted | public"},"tags":{"type":"ARRAY","description":"Video tags","items":{"type":"STRING"}}},["file_path","title"])
PLUGIN_SETTINGS={"namespace":"upload_video","title":"YouTube Upload OAuth","fields":[{"key":"client_secrets_file","label":"OAuth client JSON path","type":"text"},{"key":"token_file","label":"Token cache path","type":"text"}]}
def _run(p,player,memory):
    path=Path(str(p.get("file_path","")).strip()).expanduser(); title=str(p.get("title","")).strip(); privacy=str(p.get("privacy","private")).lower()
    if not path.is_file(): return "The selected video file does not exist."
    if not title or privacy not in ("private","unlisted","public"): return "Provide a title and privacy value private, unlisted, or public."
    try:
        from plugins._google_oauth import service
        api=service("upload_video","youtube","v3",["https://www.googleapis.com/auth/youtube.upload"])
        from googleapiclient.http import MediaFileUpload
    except Exception as exc: return str(exc)
    from core import confirm
    if confirm.pending_title(): return "Another confirmation is pending; no upload was started."
    body={"snippet":{"title":title,"description":str(p.get("description","")),"tags":[str(x) for x in p.get("tags",[])[:50]]},"status":{"privacyStatus":privacy}}
    def upload():
        try:
            result=api.videos().insert(part="snippet,status",body=body,media_body=MediaFileUpload(str(path),chunksize=8*1024*1024,resumable=True)).execute()
            return f"YouTube upload API completed for video ID {result.get('id','')}; privacy is {privacy}."
        except Exception as exc: return f"YouTube upload failed: {type(exc).__name__}: {exc}"
    return confirm.request("youtube-upload",f"Upload '{title[:80]}' to YouTube as {privacy}?",f"File: {path.name}. Review title, description, tags, and visibility before confirming.",upload)
run=safe_run(_run)
