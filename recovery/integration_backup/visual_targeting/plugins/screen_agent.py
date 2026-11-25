"""Closed-loop desktop agent backed by REM's accessibility and control APIs.

The desktop implementation lives in actions.screen_agent; this plugin adds
bounded task sequencing and routes existing application/browser tools through
the interfaces exposed by RemUI.
"""
from __future__ import annotations


_DESKTOP_ACTIONS = {
    "observe", "find", "click", "type", "press", "scroll", "focus_window",
    "pause", "resume", "stop", "emergency_stop", "set_mode",
}

PLUGIN = {
    "name": "screen_agent",
    "description": (
        "Use screen_process for read-only visual questions about visible content, "
        "errors, or project files; it sends a screenshot to the vision model. "
        "Operate the active desktop through fresh UI Automation observations, "
        "semantic control matching, safe actions, and follow-up verification. "
        "Use bounded steps for multi-part native desktop tasks; route application "
        "launches to open_app and web page work to browser_control. Ambiguous "
        "targets are refused, protected fields are never typed into, and "
        "consequential actions use the existing on-screen confirmation gate."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING", "description": "observe | find | click | type | press | scroll | focus_window | pause | resume | stop | set_mode | task | tool"},
            "target": {"type": "STRING", "description": "Visible control or application window name."},
            "text": {"type": "STRING", "description": "Text for a named, non-protected field."},
            "key": {"type": "STRING", "description": "Safe allowlisted key or shortcut."},
            "direction": {"type": "STRING", "description": "Scroll direction: up, down, left, or right."},
            "amount": {"type": "INTEGER", "description": "Scroll amount."},
            "clear_first": {"type": "BOOLEAN", "description": "Clear a target field before typing when requested."},
            "mode": {"type": "STRING", "description": "Desktop control mode."},
            "steps": {"type": "ARRAY", "description": "For task: up to eight sequential desktop or existing-tool steps. Tool steps also specify tool_name and tool_parameters.", "items": {"type": "OBJECT", "properties": {"action": {"type": "STRING"}, "target": {"type": "STRING"}, "text": {"type": "STRING"}, "key": {"type": "STRING"}, "direction": {"type": "STRING"}, "amount": {"type": "INTEGER"}, "clear_first": {"type": "BOOLEAN"}, "mode": {"type": "STRING"}, "tool_name": {"type": "STRING"}, "tool_parameters": {"type": "OBJECT"}}}},
            "tool_name": {"type": "STRING", "description": "For action=tool, an existing tool name from tool_catalog."},
            "tool_parameters": {"type": "OBJECT", "description": "Arguments for the selected existing tool."},
        },
        "required": ["action"],
    },
    "tier": "core",
    "brief": "Observe and operate desktop controls, or run a short verified desktop task.",
}


def _log(player, message: str) -> None:
    if player is not None and callable(getattr(player, "write_log", None)):
        try:
            player.write_log("REM: " + message[:240])
        except Exception:
            pass


def _catalog(player):
    fn = getattr(player, "tool_catalog", None)
    return fn() if callable(fn) else []


def _run_existing(player, name: str, parameters: dict) -> str:
    catalog = _catalog(player)
    available = {item.get("name") for item in catalog if isinstance(item, dict)}
    if name not in available:
        return f"Tool {name!r} is not available in this session; no action was taken."
    fn = getattr(player, "run_tool", None)
    if not callable(fn):
        return "Existing tool execution is unavailable in this session; no action was taken."
    # Verified in main.py: RemUI.run_tool(name, parameters) -> str.
    return str(fn(name, parameters or {}))


def _one(parameters: dict, player=None, session_memory=None) -> str:
    from actions.screen_agent import screen_agent
    return screen_agent(parameters, player=player, session_memory=session_memory)


def run(parameters, player=None, session_memory=None) -> str:
    """Run one verified desktop action, a bounded sequence, or an existing tool."""
    try:
        params = parameters if isinstance(parameters, dict) else {}
        action = str(params.get("action", "observe")).strip().lower().replace("-", "_")
        if action == "tool":
            name = str(params.get("tool_name", "")).strip()
            if name not in {"open_app", "browser_control", "web_search", "file_controller"}:
                return "That existing tool is not permitted for this route."
            result = _run_existing(player, name, params.get("tool_parameters", {}))
            _log(player, f"screen_agent routed to {name}: {result}")
            return result
        if action == "task":
            steps = params.get("steps")
            if not isinstance(steps, list) or not steps or len(steps) > 8:
                return "A task needs between one and eight explicit steps."
            say = getattr(player, "request_say", None) if player is not None else None
            if callable(say):
                try: say(f"Working through {len(steps)} desktop steps.")
                except Exception: pass
            results = []
            for index, step in enumerate(steps, 1):
                if not isinstance(step, dict):
                    return f"Step {index} is invalid; stopped without running it."
                step_action = str(step.get("action", "")).strip().lower().replace("-", "_")
                if step_action == "tool":
                    name = str(step.get("tool_name", "")).strip()
                    if name not in {"open_app", "browser_control", "web_search", "file_controller"}:
                        return f"Step {index} requested a tool that is not allowed; stopped safely."
                    result = _run_existing(player, name, step.get("tool_parameters", {}))
                elif step_action in _DESKTOP_ACTIONS:
                    result = _one({**step, "action": step_action}, player, session_memory)
                else:
                    return f"Step {index} must be a supported desktop action; stopped safely."
                # Desktop actions make a fresh observation internally; routed
                # tools own their own current-state checks. Stop at pending
                # confirmation, async work, failures, or inconclusive results.
                results.append(f"Step {index}: {result}")
                if ("[CONFIRMATION_PENDING]" in result or "unverified" in result.lower()
                        or "failed" in result.lower() or "could not" in result.lower()
                        or "multiple possible" in result.lower() or "status=started" in result
                        or "status=ask" in result or "status=failed" in result
                        or "status=unverified" in result):
                    break
            summary = " ".join(results)
            _log(player, f"screen_agent task finished {len(results)} step(s)")
            return summary
        if action not in _DESKTOP_ACTIONS:
            return "Unsupported action. Use a desktop action, task, or tool."
        result = _one({**params, "action": action}, player, session_memory)
        if action in {"observe", "find"}:
            _log(player, f"screen_agent {action}: observation returned")
        else:
            _log(player, f"screen_agent {action}: completed")
        return str(result)
    except Exception as exc:
        message = f"Screen agent failed safely: {type(exc).__name__}: {exc}"
        _log(player, message)
        return message
