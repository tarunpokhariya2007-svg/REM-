from plugins._common import plugin, route, safe_run
PLUGIN = plugin("youtube_video_plugin", "Compatibility entry for REM's built-in YouTube tool for video search, playback, metadata, and transcript-backed summaries.", {"action": {"type": "STRING", "description": "play | summarize | get_info | trending"}, "query": {"type": "STRING", "description": "Search query"}, "save": {"type": "BOOLEAN", "description": "Save summary to Notepad"}, "region": {"type": "STRING", "description": "Trending country code"}, "url": {"type": "STRING", "description": "YouTube video URL"}})
def _run(p, player, memory): return route(player, "youtube_video", p)
run = safe_run(_run)
