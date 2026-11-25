"""core/tasks/tools.py — what a background agent may use.

An agent profile names the tools it wants; this decides what it gets. The
decision is made here, in code, rather than in the profile or a prompt,
because a background agent spends its time reading pages written by strangers
and nothing it reads may widen what it can do.

THE RULES
    Ordinary background agents (no active AutonomousSession) have strictly
    read/research/notes/workdir-write tools and cannot act on the computer.

    When a task is explicitly authorized under REM AUTONOMOUS MODE with a valid,
    non-revoked, non-expired ``AutonomousSession``, ``TaskTools`` exposes the
    session-scoped project, local-observation, browser, and desktop tools,
    evaluating every single call against the session's permission categories,
    directory/app boundaries, and sensitive-operation gates.
"""
from __future__ import annotations

import subprocess
import time
from pathlib import Path

from core import outcome

_WEB = {"web_search", "search_web", "fetch_page"}
KNOWN = ("search_web", "web_search", "fetch_page", "notes", "write_file", "file_processor",
         "recall_memory")
_AUTONOMOUS_KNOWN = (
    "task_checkpoint",
    "list_project_files",
    "read_project_file",
    "write_project_file",
    "edit_project_file",
    "run_project_command",
    "desktop_ui",
    "browser_control",
    "open_app",
    "screen_process",
)
_MAX_WRITE = 2_000_000

_DECLS = {
    "search_web": {
        "name": "search_web",
        "description": ("Search the web and get a list of results — title, address and snippet — "
                        "to choose which pages to read with fetch_page. Try several phrasings, and "
                        "other languages where the topic is better covered. news=true for recent "
                        "news articles."),
        "parameters": {"type": "OBJECT", "properties": {
            "query": {"type": "STRING"},
            "max_results": {"type": "INTEGER", "description": "3-15, default 8"},
            "news": {"type": "BOOLEAN"}},
            "required": ["query"]},
    },
    "fetch_page": {
        "name": "fetch_page",
        "description": ("Read a web page or an online PDF in full — the page's own text, not a search "
                        "snippet. Use it on the most promising results before relying on them. "
                        "Public http(s) addresses only."),
        "parameters": {"type": "OBJECT", "properties": {
            "url": {"type": "STRING", "description": "the full address, starting with http"},
            "max_chars": {"type": "INTEGER", "description": "how much text to return, 2000-40000 "
                                                            "(default 15000)"}},
            "required": ["url"]},
    },
    "notes": {
        "name": "notes",
        "description": ("Your working notes for this task, kept on disk. add = record one finding, "
                        "with the URL it came from; read = get them all back. Long tasks trim old tool "
                        "results out of your memory to save space — your notes are never trimmed, so "
                        "note everything you will need for the final result."),
        "parameters": {"type": "OBJECT", "properties": {
            "action": {"type": "STRING", "enum": ["add", "read"]},
            "text": {"type": "STRING", "description": "for add: the finding and its source"}},
            "required": ["action"]},
    },
    "write_file": {
        "name": "write_file",
        "description": ("Save a file for the user in this task's folder — a table as CSV, a draft, "
                        "collected data. Returns where it was saved."),
        "parameters": {"type": "OBJECT", "properties": {
            "path": {"type": "STRING", "description": "a file name, e.g. comparison.csv"},
            "content": {"type": "STRING"}},
            "required": ["path", "content"]},
    },
    "recall_memory": {
        "name": "recall_memory",
        "description": "Look up a stored fact about the user. Empty query lists all.",
        "parameters": {"type": "OBJECT", "properties": {"query": {"type": "STRING"}}},
    },
    "task_checkpoint": {
        "name": "task_checkpoint",
        "description": ("Manage autonomous task plan and checkpoints: plan (set ordered steps), "
                        "update (mark step done/in_progress/failed/blocked_approval with verification), "
                        "pending_approval (record a blocked sensitive action and continue independent work), "
                        "status (read current steps and progress)."),
        "parameters": {"type": "OBJECT", "properties": {
            "action": {"type": "STRING", "enum": ["plan", "update", "pending_approval", "status"]},
            "steps": {"type": "ARRAY", "items": {"type": "STRING"}, "description": "for plan: ordered list of step titles"},
            "step_id": {"type": "STRING", "description": "for update: step id or title"},
            "status": {"type": "STRING", "enum": ["pending", "in_progress", "done", "failed", "blocked_approval", "skipped"]},
            "verification": {"type": "STRING", "description": "observed verification evidence"},
            "note": {"type": "STRING"},
            "title": {"type": "STRING", "description": "for pending_approval: title of blocked sensitive action"},
            "detail": {"type": "STRING", "description": "for pending_approval: details for user review"},
            "category": {"type": "STRING", "description": "for pending_approval: sensitive category"}},
            "required": ["action"]},
    },
    "list_project_files": {
        "name": "list_project_files",
        "description": "List files inside an approved project directory.",
        "parameters": {"type": "OBJECT", "properties": {
            "path": {"type": "STRING", "description": "directory relative to project root or absolute within approved_dirs"},
            "max_entries": {"type": "INTEGER"}}},
    },
    "read_project_file": {
        "name": "read_project_file",
        "description": "Read a text file inside an approved project directory.",
        "parameters": {"type": "OBJECT", "properties": {
            "path": {"type": "STRING"},
            "start_line": {"type": "INTEGER"},
            "end_line": {"type": "INTEGER"}},
            "required": ["path"]},
    },
    "write_project_file": {
        "name": "write_project_file",
        "description": "Create or overwrite a file inside an approved project directory.",
        "parameters": {"type": "OBJECT", "properties": {
            "path": {"type": "STRING"},
            "content": {"type": "STRING"}},
            "required": ["path", "content"]},
    },
    "edit_project_file": {
        "name": "edit_project_file",
        "description": "Replace an exact substring in a file inside an approved project directory.",
        "parameters": {"type": "OBJECT", "properties": {
            "path": {"type": "STRING"},
            "target": {"type": "STRING"},
            "replacement": {"type": "STRING"}},
            "required": ["path", "target", "replacement"]},
    },
    "run_project_command": {
        "name": "run_project_command",
        "description": ("Run a non-elevated terminal command or test suite inside an approved project "
                        "directory. Software installs, admin commands, and destructive deletions are blocked."),
        "parameters": {"type": "OBJECT", "properties": {
            "command": {"type": "STRING"},
            "cwd": {"type": "STRING"},
            "is_test": {"type": "BOOLEAN"},
            "timeout_s": {"type": "INTEGER"}},
            "required": ["command"]},
    },
}


def _inside(root: Path, rel: str) -> Path:
    root = root.resolve()
    given = Path(str(rel or ""))
    target = (given if given.is_absolute() else root / given).resolve()
    if target != root and root not in target.parents:
        raise PermissionError("that path is outside this task's folder")
    return target


class TaskTools:
    def __init__(self, workdir: Path, wanted, log=print, *,
                 autonomous_session_id: str = "", player=None):
        self.workdir = Path(workdir)
        self.workdir.mkdir(parents=True, exist_ok=True)
        self._log = log
        self._autonomous_session_id = str(autonomous_session_id or "")
        self._player = player
        allowed_pool = set(KNOWN)
        extra_wanted: list[str] = []
        if self._autonomous_session_id:
            allowed_pool.update(_AUTONOMOUS_KNOWN)
            extra_wanted = list(_AUTONOMOUS_KNOWN)
        names = [n for n in dict.fromkeys(list(wanted) + extra_wanted) if n in allowed_pool]
        dropped = [n for n in wanted if n not in allowed_pool]
        if dropped:
            log(f"[Tasks] not available to background agents: {', '.join(dropped)}")
        if _WEB & set(names) and "recall_memory" in names:
            names.remove("recall_memory")
        self.names = names
        self._borrowed: dict = {}
        # Called with (requested_url, document) for every page read in full —
        # the research runner keeps the text to check quotes against.
        self.on_page = None

    @property
    def has_web(self) -> bool:
        return bool(_WEB & set(self.names))

    def _session(self):
        if not self._autonomous_session_id:
            return None
        from core import autonomous_mode
        return autonomous_mode.get_session(self._autonomous_session_id)

    def _action(self, name: str) -> dict:
        if name not in self._borrowed:
            import importlib
            tool_name = "screen_agent" if name == "desktop_ui" else name
            mod = importlib.import_module(f"actions.{tool_name}")
            tool = getattr(mod, "TOOL", None)
            if not (isinstance(tool, dict) and callable(tool.get("handler"))):
                raise RuntimeError(f"actions/{tool_name}.py has no usable TOOL")
            self._borrowed[name] = tool
        return self._borrowed[name]

    def declarations(self) -> list[dict]:
        out = []
        for name in self.names:
            if name in _DECLS:
                out.append(dict(_DECLS[name]))
                continue
            if name == "screen_process":
                out.append({
                    "name": "screen_process",
                    "description": "Local screen observation (UIA/OCR metadata) for the active autonomous task. Does not upload images to cloud models.",
                    "parameters": {"type": "OBJECT", "properties": {
                        "angle": {"type": "STRING"}, "text": {"type": "STRING"}},
                        "required": ["text"]},
                })
                continue
            try:
                tool = self._action(name)
            except Exception as e:
                self._log(f"[Tasks] {name} unavailable: {e}")
                continue
            desc = tool["description"]
            if name == "file_processor":
                desc = ("Read, summarise or analyse a file in this task's folder. Files the user "
                        "attached are in inputs/. file_path is relative to the task's folder.")
            elif name == "web_search":
                desc = ("A quick synthesised answer from a search engine, with sources. Good for "
                        "orienting; for evidence, read the pages themselves with fetch_page. "
                        "mode: search | news | research | price | compare.")
            out.append({"name": name, "description": desc, "parameters": tool["parameters"]})
        return out

    def execute(self, name: str, args: dict) -> str:
        args = dict(args or {})
        if name not in self.names:
            return outcome.failed(name, "this tool is not available to this agent")

        # Autonomous session authorization & emergency-stop guard on EVERY tool call
        sess = self._session()
        if self._autonomous_session_id:
            from core import autonomous_mode, desktop_control
            if desktop_control.should_stop():
                return outcome.failed(name, "desktop control is paused or stopped by emergency stop")
            if sess is None:
                return outcome.failed(name, "autonomous task session is missing")
            ok, reason = sess.is_valid()
            if not ok:
                return outcome.failed(name, reason)

            if name not in {"notes", "write_file", "task_checkpoint"}:
                allowed, cat, reason = autonomous_mode.evaluate_tool_access(sess, name, args)
                if not allowed:
                    if cat not in {"permission_category_denied", "network_disabled", "expired_or_revoked"}:
                        entry = autonomous_mode.record_pending_approval(
                            sess, category=cat,
                            title=f"Approve {name}: {str(args)[:80]}",
                            detail=reason, tool=name, args_summary=str(args),
                        )
                        return (f"[APPROVAL_REQUIRED_QUEUED] {reason} "
                                f"Recorded pending approval id={entry['id']}. "
                                f"Do not execute this step; continue independent permitted work.")
                    autonomous_mode.log_event(sess, "tool_denied", name, cat, "denied", reason)
                    return outcome.failed(name, reason)

        try:
            if name == "task_checkpoint":
                return self._checkpoint(args, sess)
            if name == "list_project_files":
                return self._list_project_files(args, sess)
            if name == "read_project_file":
                return self._read_project_file(args, sess)
            if name == "write_project_file":
                return self._write_project_file(args, sess)
            if name == "edit_project_file":
                return self._edit_project_file(args, sess)
            if name == "run_project_command":
                return self._run_project_command(args, sess)
            if name == "screen_process" and sess is not None:
                return self._local_screen_observe(args, sess)
            if name == "fetch_page":
                return self._fetch(args)
            if name == "search_web":
                return self._search(args)
            if name == "notes":
                return self._notes(args)
            if name == "write_file":
                return self._write(args)
            if name == "recall_memory":
                from memory.memory_manager import search_memory
                return search_memory(str(args.get("query") or ""), limit=8)

            from core.action_loader import _call_handler
            if name == "file_processor":
                if str(args.get("action") or "").lower() in ("run", "test"):
                    return outcome.failed(name, "running code is not available to background tasks")
                if not args.get("file_path"):
                    return outcome.failed(name, "file_path is required")
                args["file_path"] = str(_inside(self.workdir, args["file_path"]))

            ctx = {"player": self._player}
            if sess is not None:
                ctx["session_memory"] = {"user_request": sess.objective, "autonomous_session_id": sess.session_id}
            result = str(_call_handler(self._action(name)["handler"], args, ctx) or "Done.")
            if sess is not None:
                from core import autonomous_mode
                autonomous_mode.log_event(
                    sess, "tool_call", name,
                    autonomous_mode.required_permission_category(name, args),
                    "done", result[:200],
                )
            if "<<<UNTRUSTED" not in result and name in ("file_processor", "web_search"):
                result = outcome.untrusted("file" if name == "file_processor" else "web", result)
            return result
        except Exception as e:
            if sess is not None:
                from core import autonomous_mode
                autonomous_mode.log_event(sess, "tool_error", name, "error", "failed", f"{type(e).__name__}: {e}")
            return outcome.failed(name, f"{type(e).__name__}: {e}")

    # ── autonomous project & checkpoint tools ────────────────────────────────
    def _resolve_approved_path(self, raw: str, sess) -> Path:
        if sess is None or not sess.approved_dirs:
            raise PermissionError("no approved project directory is configured")
        p = Path(str(raw or "."))
        target = (p if p.is_absolute() else sess.approved_dirs[0] / p).resolve()
        for root in sess.approved_dirs:
            r = root.resolve()
            if target == r or r in target.parents:
                return target
        raise PermissionError(f"path '{raw}' is outside the approved directories")

    def _checkpoint(self, args: dict, sess) -> str:
        if sess is None:
            return outcome.failed("task_checkpoint", "no autonomous session is bound")
        from core import autonomous_mode
        act = str(args.get("action") or "status").strip().lower()
        if act == "plan":
            steps = autonomous_mode.set_steps(sess, list(args.get("steps") or []))
            return outcome.done("task_checkpoint", planned=len(steps), steps=[s["id"] for s in steps])
        if act == "update":
            updated = autonomous_mode.update_step(
                sess, str(args.get("step_id") or ""),
                str(args.get("status") or "done"),
                verification=str(args.get("verification") or ""),
                note=str(args.get("note") or ""),
            )
            if not updated:
                return outcome.failed("task_checkpoint", "unknown step_id")
            return outcome.done("task_checkpoint", step=updated["id"], status=updated["status"])
        if act == "pending_approval":
            entry = autonomous_mode.record_pending_approval(
                sess,
                category=str(args.get("category") or "sensitive_operation"),
                title=str(args.get("title") or "Pending user approval"),
                detail=str(args.get("detail") or ""),
                tool="task_checkpoint",
            )
            return outcome.done("task_checkpoint", pending_approval_id=entry["id"])
        return outcome.done("task_checkpoint", steps=sess.steps, pending_approvals=len(sess.pending_approvals))

    def _list_project_files(self, args: dict, sess) -> str:
        target = self._resolve_approved_path(args.get("path") or ".", sess)
        if not target.is_dir():
            return outcome.failed("list_project_files", "directory does not exist")
        cap = max(1, min(400, int(args.get("max_entries") or 120)))
        items = []
        skip = {".git", "__pycache__", ".venv", "venv", "node_modules"}
        for entry in sorted(target.iterdir()):
            if entry.name in skip:
                continue
            items.append(entry.name + ("/" if entry.is_dir() else ""))
            if len(items) >= cap:
                break
        return outcome.done("list_project_files", directory=str(target), entries=items)

    def _read_project_file(self, args: dict, sess) -> str:
        target = self._resolve_approved_path(args.get("path") or "", sess)
        if not target.is_file():
            return outcome.failed("read_project_file", "file does not exist")
        lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
        start = max(1, int(args.get("start_line") or 1))
        end = min(len(lines), int(args.get("end_line") or (start + 299)))
        snippet = "\n".join(f"{i}: {lines[i - 1]}" for i in range(start, end + 1))
        return f"File: {target} (lines {start}-{end} of {len(lines)})\n{snippet[:30_000]}"

    def _write_project_file(self, args: dict, sess) -> str:
        from core import autonomous_mode
        target = self._resolve_approved_path(args.get("path") or "", sess)
        content = str(args.get("content") or "")
        if len(content.encode("utf-8")) > _MAX_WRITE:
            return outcome.failed("write_project_file", "file exceeds 2 MB limit")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        autonomous_mode.record_file_modified(sess, target)
        autonomous_mode.log_event(sess, "file_write", "write_project_file",
                                  autonomous_mode.CAT_PROJECT_FILES, "done", str(target))
        return outcome.done("write_project_file", path=str(target), bytes=target.stat().st_size)

    def _edit_project_file(self, args: dict, sess) -> str:
        from core import autonomous_mode
        target = self._resolve_approved_path(args.get("path") or "", sess)
        if not target.is_file():
            return outcome.failed("edit_project_file", "file does not exist")
        old_text = str(args.get("target") or "")
        new_text = str(args.get("replacement") or "")
        if not old_text:
            return outcome.failed("edit_project_file", "target substring is required")
        body = target.read_text(encoding="utf-8")
        if old_text not in body:
            return outcome.failed("edit_project_file", "target substring not found")
        updated = body.replace(old_text, new_text, 1)
        target.write_text(updated, encoding="utf-8")
        autonomous_mode.record_file_modified(sess, target)
        autonomous_mode.log_event(sess, "file_edit", "edit_project_file",
                                  autonomous_mode.CAT_PROJECT_FILES, "done", str(target))
        return outcome.done("edit_project_file", path=str(target), bytes=target.stat().st_size)

    def _run_project_command(self, args: dict, sess) -> str:
        from core import autonomous_mode
        cmd = str(args.get("command") or "").strip()
        if not cmd:
            return outcome.failed("run_project_command", "command is required")
        cwd = self._resolve_approved_path(args.get("cwd") or ".", sess)
        if not cwd.is_dir():
            return outcome.failed("run_project_command", "cwd is not a directory")
        timeout = max(5, min(300, int(args.get("timeout_s") or 120)))
        proc = subprocess.run(
            cmd, cwd=str(cwd), shell=True, capture_output=True,
            text=True, encoding="utf-8", errors="replace", timeout=timeout,
        )
        out = ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip()[-12_000:]
        passed = proc.returncode == 0
        if args.get("is_test"):
            autonomous_mode.record_test_run(sess, cmd, passed, out[-400:] or f"exit={proc.returncode}")
        autonomous_mode.log_event(
            sess, "command", "run_project_command", autonomous_mode.CAT_PROJECT_FILES,
            "done" if passed else "failed", f"exit={proc.returncode} cmd={cmd[:120]}",
        )
        return outcome.done("run_project_command", returncode=proc.returncode, output=out)

    def _local_screen_observe(self, args: dict, sess) -> str:
        from core import autonomous_mode, screenview
        from core.screen_observation import describe_view
        view = screenview.look(budget_s=1.2, allow_ocr=True)
        summary = describe_view(view)
        autonomous_mode.log_event(
            sess, "local_screen_observe", "screen_process",
            autonomous_mode.CAT_LOCAL_SCREEN, "done",
            f"app={view.app or 'unknown'} source={view.source}",
        )
        return outcome.done("screen_process", observation=summary)

    # ── native tools ─────────────────────────────────────────────────────────
    def _fetch(self, args: dict) -> str:
        from core.tasks import web

        url = str(args.get("url") or "").strip()
        try:
            doc = web.fetch_document(url)
        except (web.Refused, web.FetchError) as e:
            return outcome.failed("fetch_page", str(e), url=url)
        if self.on_page is not None:
            try:
                self.on_page(url, doc)
            except Exception as e:
                self._log(f"[Tasks] page hook failed: {e}")
        return web.format_page(doc, args.get("max_chars") or 15_000)

    def _search(self, args: dict) -> str:
        from actions.web_search import _ddg_news, _ddg_search

        query = " ".join(str(args.get("query") or "").split())
        if not query:
            return outcome.failed("search_web", "search needs a query")
        try:
            n = max(3, min(15, int(args.get("max_results") or 8)))
        except (TypeError, ValueError):
            n = 8
        rows = _ddg_news(query, max_results=n) if args.get("news") else _ddg_search(query, max_results=n)
        rows = [r for r in rows or [] if r.get("url")]
        if not rows:
            return outcome.failed("search_web", "no results — try other words or another language",
                                  query=query)
        lines = [f"Results for: {query}"]
        for i, r in enumerate(rows, 1):
            lines.append(f"{i}. {r.get('title', '')}\n   {r['url']}\n   {(r.get('snippet') or '')[:300]}")
        return outcome.untrusted("web", "\n".join(lines))

    def _notes(self, args: dict) -> str:
        path = self.workdir / "notes.md"
        if str(args.get("action") or "read").lower() == "add":
            text = " ".join(str(args.get("text") or "").split())
            if not text:
                return outcome.failed("notes", "add needs the text of the note")
            with path.open("a", encoding="utf-8") as fh:
                fh.write(f"- [{time.strftime('%H:%M')}] {text}\n")
            count = sum(1 for _ in path.open(encoding="utf-8"))
            return outcome.done("notes", added=True, total_notes=count)
        if not path.exists():
            return "No notes yet."
        return path.read_text(encoding="utf-8")[-60_000:]

    def _write(self, args: dict) -> str:
        content = str(args.get("content") or "")
        if len(content.encode("utf-8")) > _MAX_WRITE:
            return outcome.failed("write_file", "that is larger than 2 MB")
        target = _inside(self.workdir / "files", str(args.get("path") or "").strip() or "output.txt")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return outcome.done("write_file", saved=str(target), bytes=target.stat().st_size)
