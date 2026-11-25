# Voice Desktop Automation — Implementation Baseline

Date: 2026-11-24

## Workspace snapshot
- Existing Python suite: 84 tests passed before this phase (`.venv` Python 3.12.10 x64).
- Active discovery: 27 plugins and 24 actions.
- Git was already dirty before this phase: 15 tracked modified files, including `memory/routines.json`; the routines file is being preserved without edits. Numerous untracked recovery artifacts and tests were already present.
- No files were staged by this phase.
- A dated copy of the existing `main.py`, `core/desktop_control.py`, and authorization tests is in `recovery/integration_backup/voice_desktop_2026-11-24/` before implementation edits.

## Existing component evidence (from prior acceptance work)
- UI Automation initializes after installation of Microsoft VC++ v14 x64 runtime; PyWinAuto 0.6.9 enumerated a disposable Chrome window and 560 descendants.
- MSS screenshot capture worked at 1920x1080.
- RapidOCR 3.10.0 with ONNX Runtime 1.32.0 performed local OCR on a fresh disposable screen, returning 65 text elements with bounding boxes.
- Physical mouse actions passed a disposable-window acceptance script; see `recovery/MOUSE_CONTROL_REPORT.md`.
- Chrome 1.65.2/CDP was tested against a disposable profile; three YouTube result IDs and resulting watch URLs matched; see `recovery/CHROME_VALIDATION_REPORT.md`.
- Gemini Live image-only interpretation was unreliable in prior testing. A production-style image + OCR context smoke test correctly read a unique marker, but no real microphone-to-action end-to-end session was tested.

## Authorization path observed before this phase
1. Direct typed requests record a shared `(_last_user_command, source)` pair.
2. Microphone transcription clears the previous authorization context at the start of a voice turn and records the transcript at turn completion.
3. Gemini Live tool calls are authorized in `_execute_tool()` against that shared latest-request pair.
4. A tool-call batch loops over function calls, each re-reading the mutable shared context.
5. Nested plugin routing (`screen_agent` → `RemUI.run_tool` → `_run_tool_for_plugin`) also re-reads the mutable shared pair.
6. Desktop authorization is a phrase/token intent filter, with a separate confirmation mechanism for consequential operations.

This leaves a concurrency window: a newer typed/dashboard request can replace the shared context between sequential tool calls from a single Live response. It also means authorization is represented as one mutable latest-request slot rather than an expiring, identifiable task context. This is the specific consistency defect addressed first; broader natural-language scope and consequential confirmation policy remain independently guarded.

## Existing safety boundaries retained
- `core.desktop_control` emergency-stop latch and input release.
- Existing action lock and per-operation confirmation gates.
- Telegram source is not a trusted desktop authorization source.
- Chrome CDP remains explicit, loopback-only, and profile-isolated in acceptance tests.
- No existing Chrome profile, unsaved document, dependency setup, plugin loader, or `memory/routines.json` is modified by this phase.
