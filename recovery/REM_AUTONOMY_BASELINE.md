# REM Autonomy Baseline

Date: 2026-11-24  
Branch: `main`  
Project: `C:\important\Mark-LVII-main\Mark-LVII-main`

## Protected working-tree state

At discovery, the tree already contained the earlier user work listed in `REM_HARDENING_BASELINE.md` and the prior recovery reports. In particular, `memory/routines.json` was already modified. It is preserved and was not read or edited for this task. Recovered originals under `recovery/original_plugins/` remain authoritative archival copies and are not modified. Nothing is staged, committed, pushed, reset or cleaned.

## Verified baseline

- Active flow: `main.py` builds Gemini Live declarations from the action and plugin registries; `_execute_tool()` authorizes desktop tools against the current direct transcript, dispatches actions/plugins, and returns `types.FunctionResponse`. `screen_process` sends a fresh screenshot as Live video input and then sends the user question as text. Confirmation outcomes use the existing realtime input interface when the originating session is still active.
- Native semantics: `actions/screen_agent.py` reads a fresh `core.screenview.View`, matches accessible elements by their labels/roles, refuses ambiguous matches, and calls `core.screenact`. It currently supports observe/find/click/type/press/scroll/focus-window and lifecycle actions. It has no semantic relative/ordinal target engine or short-lived conversational element reference.
- Physical input: `core/screenact.py` drives PyAutoGUI and supports element/Point click, double/right click, hover, drag, scroll, key input, typing and release-all. A target `Point` is checked for on-screen placement, but the layer does not establish that a vision-generated coordinate belongs to the intended semantic object. No public cursor-move-to-element or relative cursor action is exposed through `screen_agent`.
- Browser semantics: `actions/browser_control.py` supports Playwright DOM/role/text locators and uniqueness checks. The opt-in loopback-only Chrome CDP attach implementation exists. `smart_click()` uses general locator matching and does not specially identify/number YouTube video cards; there is no real Chrome acceptance result.
- Windows vision dependencies: actual runtime checks on Windows 11 / Python 3.12.10 show MSS captures 1920×1080. UIA is unavailable because pywinauto imports `win32ui`, whose `win32ui.pyd` depends on absent `mfc140u.dll`. OCR backends (`rapidocr_onnxruntime`, `paddleocr`) are absent. No dependencies or Windows runtime were installed.
- Gemini SDK: installed `google-genai` is 2.34.0. The current code uses `send_realtime_input(video=Blob(...))` and text input. Tests validate fake-session routing only; no live Gemini session was exercised.
- Current test baseline: 73 offline/unittest cases passed before this task; project compilation passed. CDP attach and Gemini confirmation delivery have mock coverage only.
- No physical cursor or application-control acceptance tests were run for this task. The previous optional Notepad launch restored an unsaved document; it was left open and untouched. Existing VS Code and Chrome windows are user-owned and are not targets for destructive testing.

## Objective boundaries

This pass will improve deterministic browser ordinal video targeting and expose semantic native cursor/right/double-click actions when accessible elements exist. Without working UIA or OCR, REM cannot truthfully promise native visual target accuracy on this host. Visual screenshots can still be delivered to Gemini for description, but the current system does not convert a free-form vision answer into an independently validated native target.

## Implementation controls

- Back up every existing source file under `recovery/integration_backup/visual_targeting/` before editing.
- Do not install packages, MFC runtimes, OCR engines or browser software.
- Do not use the existing unsaved Notepad document, current VS Code project window, or current Chrome session for input tests.
- Keep all source changes behind current action locking, authorization, pause/stop and confirmation behavior.
- Verify ordinal browser targets from DOM identity and resulting video URL, not just from an input event.

## Extension delivered after baseline

- Added semantic native cursor movement, hover, right-click and double-click to the existing screen agent; added measured named positions and capped relative movement.
- Added explicit authorization checks for right/double-click intent and retained the existing desktop lock, stop checks and confirmation callback semantics.
- Added YouTube ordinal DOM targeting for first/second/third/last results with video ID verification. These selectors have mocked tests only.
- Latest offline suite: 81 unittest tests pass. UIA/OCR are unavailable on this host; no physical cursor, live Chrome, YouTube, or Gemini acceptance test was performed. See `MOUSE_CONTROL_REPORT.md`, `TARGET_ACCURACY_REPORT.md`, `CHROME_VALIDATION_REPORT.md`, and `TEST_RESULTS.md`.
