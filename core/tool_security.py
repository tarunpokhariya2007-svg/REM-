"""Fail-closed risk metadata for every tool exposed to Gemini or nested plugins.

The category is policy metadata, not a substitute for each tool's existing
authorization, confirmation, emergency-stop, or target-validation controls.
Newly discovered tools must be reviewed and added here before dispatch.
"""
from __future__ import annotations

from types import MappingProxyType


_CATEGORIES = {
    "read_only",
    "reversible_local_action",
    "external_communication",
    "destructive_action",
    "financial_or_consequential_action",
    "security_sensitive_action",
}

# name: (risk category, confirmation owner/rule). Keep this list synchronized
# with the inline, action, and plugin declarations; missing entries are denied.
_POLICY = {
    "system_status": ("read_only", "none"),
    "screen_process": ("security_sensitive_action", "cloud-image permission and camera consent gates"),
    "close_camera": ("reversible_local_action", "none"),
    "manage_monitor": ("reversible_local_action", "none"),
    "shutdown_rem": ("security_sensitive_action", "explicit shutdown intent"),
    "save_memory": ("security_sensitive_action", "user-directed memory write"),
    "recall_memory": ("security_sensitive_action", "read-only private memory access"),
    "undo": ("reversible_local_action", "undo target validation"),

    "background_tasks": ("security_sensitive_action", "task-engine action policy"),
    "browser_control": ("security_sensitive_action", "consequential page actions use confirmation"),
    "code_helper": ("security_sensitive_action", "implementation-specific authorization and write guards"),
    "computer_control": ("security_sensitive_action", "desktop authorization and action-specific confirmation"),
    "computer_settings": ("security_sensitive_action", "shutdown, restart, and network changes require confirmation"),
    "desktop_control": ("destructive_action", "desktop-organize/clean operations need exact-target review"),
    "desktop_ui": ("security_sensitive_action", "desktop authorization, emergency stop, and action-specific confirmation"),
    "dev_agent": ("security_sensitive_action", "checkpointed development authorization"),
    "file_controller": ("destructive_action", "file operation-specific confirmation and target checks"),
    "file_processor": ("security_sensitive_action", "uploaded-file action policy; code execution requires review"),
    "flight_finder": ("read_only", "external search; no booking or payment"),
    "game_updater": ("security_sensitive_action", "download/install/schedule operation-specific gates"),
    "open_app": ("reversible_local_action", "launch verification"),
    "reminder": ("reversible_local_action", "Task Scheduler operation validation"),
    "routines": ("security_sensitive_action", "routine action and schedule validation"),
    "send_message": ("external_communication", "on-screen confirmation and delivery verification"),
    "video_player": ("reversible_local_action", "local playback control; external URLs are read-only navigation"),
    "weather_report": ("read_only", "external query"),
    "web_search": ("read_only", "external query"),
    "youtube_video": ("read_only", "external read/playback; no account mutation"),

    "aircraft_report": ("security_sensitive_action", "external location query and opt-in watch lifecycle"),
    "calendar_control": ("financial_or_consequential_action", "explicit on-screen confirmation for mutations"),
    "calorie_counter": ("security_sensitive_action", "camera permission/consent gate"),
    "chat_takeover": ("security_sensitive_action", "private conversation read; send delegated to confirmed action"),
    "code_helper_plugin": ("security_sensitive_action", "delegated code write/run authorization"),
    "dev_agent_plugin": ("security_sensitive_action", "delegated checkpointed development authorization"),
    "document_review": ("read_only", "local document read"),
    "excel_writer": ("reversible_local_action", "writes a separate output copy"),
    "flight_finder_plugin": ("read_only", "external search; no booking or payment"),
    "game_updater_plugin": ("security_sensitive_action", "delegated update/install policy"),
    "gmail_control": ("external_communication", "on-screen confirmation for send; private mailbox read"),
    "home_assistant": ("security_sensitive_action", "entity allowlist; locks/alarms excluded"),
    "image_generator": ("financial_or_consequential_action", "external service and new output path validation"),
    "obsidian_vault": ("reversible_local_action", "existing-note changes require confirmation"),
    "pomodoro": ("reversible_local_action", "local timer state"),
    "printer_control": ("read_only", "MQTT telemetry only"),
    "pushup_counter": ("security_sensitive_action", "one-shot camera consent"),
    "quiz": ("read_only", "configured model request and local display"),
    "screen_agent": ("security_sensitive_action", "desktop authorization, emergency stop, and action-specific confirmation"),
    "shopping_list": ("reversible_local_action", "local list changes; clear/remove semantics need explicit target"),
    "spaced_repetition": ("reversible_local_action", "local card/review state"),
    "telegram_remote": ("external_communication", "pairing, allowlist, and cloud task restrictions"),
    "upload_video": ("external_communication", "explicit on-screen confirmation before publication"),
    "water_reminder": ("reversible_local_action", "local hydration/reminder state"),
    "whatsapp_call": ("external_communication", "explicit on-screen confirmation before call"),
    "whatsapp_guard": ("security_sensitive_action", "private visible call-state inspection"),
    "youtube_video_plugin": ("read_only", "external read/playback; no account mutation"),
}

TOOL_SECURITY = MappingProxyType(_POLICY)


def tool_policy(name: str) -> dict | None:
    """Return a copy of reviewed risk metadata, or None for fail-closed denial."""
    entry = _POLICY.get(str(name or ""))
    if entry is None:
        return None
    category, confirmation = entry
    if category not in _CATEGORIES:
        return None
    return {"category": category, "confirmation": confirmation}


def unreviewed_tool_names(names) -> list[str]:
    """List active registry names which have not been explicitly reviewed."""
    return sorted(str(name) for name in names if tool_policy(str(name)) is None)
