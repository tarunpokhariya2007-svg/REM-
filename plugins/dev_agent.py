from plugins._common import plugin, route, safe_run
PLUGIN = plugin("dev_agent_plugin", "Compatibility entry for REM's built-in dev_agent. Multi-file project work is delegated to its existing checkpointed development tool.", {"description": {"type": "STRING", "description": "Project to build or requested project change"}, "mode": {"type": "STRING", "description": "improve | build | status"}, "folder": {"type": "STRING", "description": "Existing project folder"}, "language": {"type": "STRING", "description": "Programming language"}, "project_name": {"type": "STRING", "description": "New project name"}, "timeout": {"type": "INTEGER", "description": "Timeout in seconds"}}, ["description"])
def _run(p, player, memory): return route(player, "dev_agent", p)
run = safe_run(_run)
