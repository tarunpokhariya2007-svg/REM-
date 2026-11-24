from pathlib import Path
import hashlib
import os
import tempfile
from plugins._common import config, plugin, safe_run

PLUGIN = plugin("obsidian_vault", "Search, read, create, or update Markdown notes in the configured Obsidian vault. An existing note is only changed after confirmation.", {
    "action": {"type": "STRING", "description": "search | read | create | update"},
    "query": {"type": "STRING", "description": "Search text or note path"},
    "content": {"type": "STRING", "description": "Markdown content for a new note"},
}, ["action"])
PLUGIN_SETTINGS = {"namespace": "obsidian_vault", "title": "Obsidian Vault", "fields": [{"key": "vault_path", "label": "Vault path", "type": "text"}]}

def _run(p, player, memory):
    settings = config("obsidian_vault"); root = Path(str(settings.get("vault_path", ""))).expanduser()
    if not settings.get("vault_path"): return "Configure the Obsidian vault path in plugin settings first."
    if not root.is_dir(): return "The configured Obsidian vault path does not exist or is not a directory."
    action, query = str(p["action"]).lower(), str(p.get("query", "")).strip()
    if not query: return "Provide a note name or search query."
    if action == "search":
        hits = []
        for path in root.rglob("*.md"):
            if any(part.startswith(".") for part in path.relative_to(root).parts): continue
            try:
                if query.casefold() in path.read_text(encoding="utf-8", errors="replace").casefold(): hits.append(path.relative_to(root).as_posix())
            except OSError: continue
            if len(hits) >= 20: break
        return "No matching notes." if not hits else "Matching notes: " + ", ".join(hits)
    path = root / query
    if action in ("read", "create", "update") and path.suffix.lower() != ".md": path = path.with_suffix(".md")
    root_resolved, path = root.resolve(), path.resolve()
    if path == root_resolved or root_resolved not in path.parents: return "That note path is outside the configured vault or names the vault itself."
    if action == "read":
        if not path.is_file(): return "Note not found."
        from core import outcome
        return outcome.untrusted("obsidian", path.read_text(encoding="utf-8")[:12000])
    if action == "create":
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with path.open("x", encoding="utf-8") as stream: stream.write(str(p.get("content", "")))
        except FileExistsError: return "That note already exists; it was not overwritten."
        return f"Created {path.relative_to(root_resolved).as_posix()}."
    if action == "update":
        if not path.is_file(): return "Existing note not found; use create to make a new note."
        old = path.read_bytes(); digest = hashlib.sha256(old).hexdigest()
        from core import confirm
        if confirm.pending_title(): return "Another confirmation is pending; the note was not changed."
        def replace_note():
            try:
                current = path.resolve()
                if root_resolved not in current.parents or current != path or hashlib.sha256(current.read_bytes()).hexdigest() != digest:
                    return "The note changed or moved while confirmation was pending; nothing was overwritten."
                fd, tmp_name = tempfile.mkstemp(prefix=path.name+".",suffix=".tmp",dir=path.parent)
                try:
                    with os.fdopen(fd,"w",encoding="utf-8") as stream:
                        stream.write(str(p.get("content","")))
                        stream.flush()
                        os.fsync(stream.fileno())
                    os.replace(tmp_name,path)
                finally:
                    try: os.unlink(tmp_name)
                    except FileNotFoundError: pass
                return f"Updated {path.relative_to(root_resolved).as_posix()}."
            except Exception as exc: return f"Could not update the note: {type(exc).__name__}: {exc}"
        return confirm.request("obsidian-update",f"Replace Obsidian note {path.relative_to(root_resolved).as_posix()}?",f"The existing note has {len(old)} bytes. Review before confirming.",replace_note)
    return "Choose search, read, or create."

run = safe_run(_run)
