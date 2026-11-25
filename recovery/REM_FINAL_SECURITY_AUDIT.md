# REM Final Security Audit

Date: 2026-11-25  
Status: **PARTIAL — active tools have explicit central risk metadata and unknown names fail closed; a full adversarial and per-parameter consent audit is incomplete.**

## Inventory and risk classifications

Current declarations: 8 inline tools, 20 actions, and 27 plugins (55 distinct names). `core/tool_security.py` is the central risk manifest. `tests/test_tool_security_manifest.py` parses the declarations without importing plugins and verifies that all 55 names have policies.

| Tool | Risk category | Permission / confirmation owner |
|---|---|---|
| `system_status` | Read-only | No confirmation |
| `screen_process` | Security-sensitive | Cloud image permission; camera consent |
| `close_camera` | Reversible local action | No confirmation |
| `manage_monitor` | Reversible local action | Add/remove/list topic validation |
| `shutdown_rem` | Security-sensitive | Explicit shutdown intent |
| `save_memory` | Security-sensitive | User-directed memory write |
| `recall_memory` | Security-sensitive | Private memory read |
| `undo` | Reversible local action | Undo target validation |
| `background_tasks` | Security-sensitive | Task-engine action policy |
| `browser_control` | Security-sensitive | Consequential page-action confirmation; visible-page guard |
| `code_helper` | Security-sensitive | Code write/run authorization and guards |
| `computer_control` | Security-sensitive | Desktop authorization; action-specific confirmation |
| `computer_settings` | Security-sensitive | Restart/shutdown/network confirmation |
| `desktop_control` | Destructive action | Exact target review for organize/clean |
| `desktop_ui` | Security-sensitive | Desktop authorization, emergency stop, action confirmation |
| `dev_agent` | Security-sensitive | Checkpointed development authorization |
| `file_controller` | Destructive action | Operation-specific target checks/confirmation |
| `file_processor` | Security-sensitive | Uploaded-file policy; code execution requires review |
| `flight_finder` | Read-only | External search; no booking/payment |
| `game_updater` | Security-sensitive | Download/install/schedule policy |
| `open_app` | Reversible local action | Launch verification |
| `reminder` | Reversible local action | Task Scheduler operation validation |
| `routines` | Security-sensitive | Routine action/schedule validation |
| `send_message` | External communication | On-screen confirmation and delivery verification |
| `video_player` | Reversible local action | Local playback; external URLs are navigation |
| `weather_report` | Read-only | External query |
| `web_search` | Read-only | External query |
| `youtube_video` | Read-only | External read/playback; no account mutation |
| `aircraft_report` | Security-sensitive | External location query and opt-in watch lifecycle |
| `calendar_control` | Financial or consequential | On-screen confirmation for mutations |
| `calorie_counter` | Security-sensitive | Camera permission/consent |
| `chat_takeover` | Security-sensitive | Private conversation read; sending delegated to confirmed action |
| `code_helper_plugin` | Security-sensitive | Delegated code write/run authorization |
| `dev_agent_plugin` | Security-sensitive | Delegated checkpointed development authorization |
| `document_review` | Read-only | Local document read |
| `excel_writer` | Reversible local action | Writes a separate output copy |
| `flight_finder_plugin` | Read-only | External search; no booking/payment |
| `game_updater_plugin` | Security-sensitive | Delegated update/install policy |
| `gmail_control` | External communication | On-screen send confirmation; mailbox is private data |
| `home_assistant` | Security-sensitive | Entity allowlist; locks/alarms excluded |
| `image_generator` | Financial or consequential | External service and new output path validation |
| `obsidian_vault` | Reversible local action | Existing-note changes require confirmation |
| `pomodoro` | Reversible local action | Local timer state |
| `printer_control` | Read-only | MQTT telemetry only |
| `pushup_counter` | Security-sensitive | One-shot camera consent |
| `quiz` | Read-only | Configured model request and local display |
| `screen_agent` | Security-sensitive | Desktop authorization, emergency stop, action confirmation |
| `shopping_list` | Reversible local action | Explicit item semantics for clear/remove |
| `spaced_repetition` | Reversible local action | Local card/review state |
| `telegram_remote` | External communication | Pairing, allowlist, and cloud task restrictions |
| `upload_video` | External communication | On-screen confirmation before publication |
| `water_reminder` | Reversible local action | Local hydration/reminder state |
| `whatsapp_call` | External communication | On-screen confirmation before call |
| `whatsapp_guard` | Security-sensitive | Private visible call-state inspection |
| `youtube_video_plugin` | Read-only | External read/playback; no account mutation |

## Enforcement and evidence

`_execute_tool` and nested plugin routing now consult `tool_policy()` before dispatch. Unknown names have no policy and are denied before an action or plugin registry is invoked. The manifest is classification metadata; it does not create a generic confirmation dialog. Each listed confirmation remains implemented at its tool-specific call site.

Mocked/unit evidence: all 55 names map to a risk category; unknown policy lookup returns no policy; unknown direct Live and nested dispatch are rejected before a registry is invoked. Request-context tests cover bound authorization, unrelated/missing context, expiry, and explicit missing Live batch context. No live external communication, account mutation, or financial action was performed.

## Remaining security limitations

Status is PARTIAL. This was not an exhaustive adversarial review of every argument, prompt-injection path, plugin sub-action, or external service. Existing per-action confirmation was not re-proven for every tool in this pass. The manifest fails closed for new names, but known tools still depend on their implementation-specific checks. Review those checks whenever a capability changes.

## Runtime error-hardening continuation — 2026-11-25

- `_execute_tool()` no longer falls back to the mutable latest request when a caller omits `request_context`; missing provenance is denied. This narrows authority and preserves the existing manifest.
- `core/error_reporting.py` emits structured, sanitized diagnostics. Exception strings, URLs, text entered into memory tools, and returned tool text are excluded from routine diagnostic output. `main.py` now reports tool result length only; `save_memory` no longer prints the stored value.
- `local_screen_observation_enabled` is the canonical persisted preference. It is not cloud-egress authorization. The existing cloud-image setting and preflight remain separate and default-off.
- New unit tests verify omitted-context denial, error-record redaction and preference precedence. They do not establish a real Gemini authorization/tool call.

Status remains **PARTIAL**: the missing runtime-log artifact prevents root attribution of each Live refusal; real Live voice-to-tool execution and visible browser acceptance are still unverified. No external communication or consequential action was performed during this continuation.

## Latest mission continuation — 2026-11-25

Conversation response handling now applies status-specific, concise model guidance to final Live tool results and removes recognized low-level browser/UIA/Playwright/coordinate diagnostics before they are sent back as tool text. The new tests validate policy text and a browser timeout sanitation case. This does not establish that Gemini Live followed the guidance in audible speech; the real voice-to-tool attempt remains NOT VERIFIED.

Current automated result: **171/171** unittest tests passed; `compileall -q .` and `git diff --check` passed. Audit coverage remains representative rather than exhaustive across every per-tool argument and all 20 adversarial scenarios. Do not interpret the full suite as a complete security audit.
