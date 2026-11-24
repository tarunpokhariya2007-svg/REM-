from plugins._common import plugin, route, safe_run

PLUGIN = plugin("document_review", "Review an existing supported document for grammar, structure, or content using REM's file processor. Returns suggestions only; it never edits the source.", {
    "file_path": {"type": "STRING", "description": "Path to the document"},
    "focus": {"type": "STRING", "description": "Grammar, structure, clarity, or a review question"},
}, ["file_path"])

def _run(p, player, memory):
    path = str(p.get("file_path", "")).strip()
    if not path: return "Provide the document path."
    focus = str(p.get("focus", "grammar, structure, clarity, and content")).strip()
    return route(player, "file_processor", {"file_path": path, "action": "summarize", "instruction": f"Review the document for {focus}. Provide findings and suggested edits only. Do not modify the source file."})

run = safe_run(_run)
