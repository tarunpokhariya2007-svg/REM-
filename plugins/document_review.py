from plugins._common import plugin, route, safe_run

PLUGIN = plugin("document_review", "Review a supported document through REM's file processor, or display structured document findings already grounded in user-provided text. It never edits the source and does not provide legal advice.", {
    "file_path": {"type": "STRING", "description": "Optional path to a supported document when using file review"},
    "focus": {"type": "STRING", "description": "Grammar, structure, clarity, or a review question"},
    "title": {"type": "STRING", "description": "Document title for rendering findings already reviewed from source text"},
    "summary": {"type": "STRING", "description": "Short source-grounded summary in the user's language"},
    "findings": {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
        "heading": {"type": "STRING"}, "detail": {"type": "STRING"},
        "severity": {"type": "STRING", "description": "serious | caution | note"},
        "quote": {"type": "STRING"}, "suggestion": {"type": "STRING"},
    }, "required": ["heading", "detail"]}},
    "unclear": {"type": "ARRAY", "items": {"type": "STRING"},
                "description": "Points the source text does not settle"},
})

_LEVELS = ("serious", "caution", "note")
_RANK = {value: index for index, value in enumerate(_LEVELS)}

def _clip(value, limit):
    text = str(value or "").strip()
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"

def _clean_findings(raw):
    rows = []
    if not isinstance(raw, list): return rows
    for value in raw[:30]:
        if not isinstance(value, dict): continue
        heading = _clip(value.get("heading"), 120)
        detail = _clip(value.get("detail"), 1200)
        if not heading and not detail: continue
        severity = str(value.get("severity", "note")).strip().lower()
        if severity not in _RANK: severity = "note"
        rows.append({"heading": heading or detail[:60], "detail": detail,
                     "severity": severity, "quote": _clip(value.get("quote"), 400),
                     "suggestion": _clip(value.get("suggestion"), 300)})
    return sorted(rows, key=lambda row: _RANK[row["severity"]])

def _run(p, player, memory):
    path = str(p.get("file_path", "")).strip()
    if p.get("findings") is not None or p.get("summary"):
        findings = _clean_findings(p.get("findings"))
        summary = _clip(p.get("summary"), 1200)
        unclear = [_clip(value, 300) for value in (p.get("unclear") or [])
                   if str(value or "").strip()][:10]
        title = _clip(p.get("title"), 120) or "Document review"
        if not summary and not findings:
            return "No usable document findings were supplied."
        shown = False
        if player:
            show_review = getattr(player, "show_review", None)
            if callable(show_review):
                try:
                    show_review(title, summary, findings, unclear)
                    shown = True
                except Exception:
                    pass
        if not shown:
            lines = [summary] if summary else []
            for row in findings:
                lines.append(f"[{row['severity'].upper()}] {row['heading']}: {row['detail']}")
                if row["quote"]: lines.append(f"Source wording: {row['quote']}")
                if row["suggestion"]: lines.append(f"Possible next step: {row['suggestion']}")
            if unclear: lines.extend(["Unclear from the provided text:", *unclear])
            if player and callable(getattr(player, "show_content", None)):
                player.show_content(title, "\n".join(lines))
        count = len(findings)
        return (f"Reviewed {title}: {count} source-grounded finding(s) are shown. "
                "This is an explanation of the supplied text, not legal advice.")
    if not path: return "Provide a document path or source-grounded findings."
    focus = str(p.get("focus", "grammar, structure, clarity, and content")).strip()
    return route(player, "file_processor", {"file_path": path, "action": "summarize", "instruction": f"Review the document for {focus}. Provide findings and suggested edits only. Do not modify the source file."})

run = safe_run(_run)
