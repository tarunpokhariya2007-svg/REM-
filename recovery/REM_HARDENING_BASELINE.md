# REM Desktop Hardening Baseline

Date: 2026-11-24  
Branch: `main`  
Project: `C:\important\Mark-LVII-main\Mark-LVII-main`

## Working tree protection snapshot

At the start of this hardening task the worktree was already dirty. These changes are pre-existing user work and are preserved:

- Modified: `.gitignore`, `actions/dev_agent.py`, `actions/screen_agent.py`, `core/desktop_control.py`, `main.py`, `memory/routines.json`, `plugins/screen_agent.py`, `plugins/shopping_list.py`, `plugins/telegram_remote.py`, `readme.md`, `tests/test_reconstructed_plugins.py`.
- Untracked: `core/screen_observation.py`, `plugins/aircraft_report.py`, `recovery/`, `tests/test_desktop_control_authorization.py`, `tests/test_recovered_plugin_adapters.py`, `tests/test_screen_observation.py`.
- No staged changes were present. `memory/routines.json` is user data and must not be edited.
- The recovery integration report states that 27 plugins were valid/enabled and that 62 focused tests passed before this task. Original recovered source is held in `recovery/original_plugins/`; do not alter it.

## Current architecture

- `main.py` owns Gemini Live session lifecycle, function declaration assembly, authorization against the latest direct user transcript, tool dispatch, and `types.FunctionResponse` replies.
- `core/action_loader.py` and the plugin registry discover existing action and plugin tools. The current integration report records 27 enabled plugins. This work does not replace either loader.
- `screen_process` in `main.py` captures a fresh screenshot, obtains best-effort local metadata from `core.screenview.look()`, sends the image by `session.send_realtime_input(video=Blob(...))`, then sends a question containing the user request. Its tool result is returned to the model through the normal Live function-response path.
- `core/screenview.py` prefers platform accessibility APIs (Windows UIA through `pywinauto`), then OCR when an OCR engine is installed. Screen capture uses `mss`; the screen observation module keeps metadata separate from pixels and does not persist screenshots.
- `actions/screen_agent.py` performs fresh observation, semantic target resolution and verified actions. `core/screenact.py` owns platform input operations. `core/desktop_control.py` provides the shared action lock, pause/stop state, cancellation callbacks and authorization checks.
- `actions/browser_control.py` uses Playwright semantic DOM locators in its automation session and routes operations through the shared desktop action lock. Its current session implementation launches persistent profiles; it has no existing Chrome CDP attachment path.
- `core/confirm.py` stores one UI-confirmed pending operation, expires it after 90 seconds, and runs an approved operation on a worker thread.
- `providers/schema.py` maps tool declarations among provider dialects. Gemini Live uses Gemini declarations assembled in `main.py` and returns `FunctionResponse`s.
- `readme.md`, `recovery/INTEGRATION_REPORT.md`, and `recovery/FEATURE_GAP_ANALYSIS.md` document user-facing capabilities and prior recovery decisions.

## Existing working features and known limits

- The latest user changes include screen observation, request-bound desktop authorization, emergency stop/pause/resume, a dedicated screen observation implementation, and screen observation tests. These are baseline state, not changes made by this task.
- The recovery report records 27 enabled plugins, aircraft and shopping-list adapters, and safer development-agent dependency handling.
- Before this pass, the development agent no longer installed model-proposed dependencies automatically, but the new-project flow still executed the planner's generated command automatically and could retry/fix based on its output.
- Runtime on this host: Windows 11, Python 3.12.10, PyWin32 312, pywinauto 0.6.9, MSS 10.2.0, Playwright 1.65.2, google-genai 2.34.0. The installed `win32ui.pyd` imports `mfc140u.dll`, which is not present in the venv or `C:\Windows\System32`; importing it fails with Windows DLL-load error 126. This breaks pywinauto's import chain even though `win32api`/`win32gui` import. MSS capture succeeds at 1920×1080. Neither RapidOCR nor PaddleOCR is installed.
- Confirmation-gated actions execute after the tool response has already returned. At baseline `core.confirm.resolve()` only logs the result; it does not send the result into the active Gemini Live session.
- Browser automation does not attach to an already-running Chrome debugging endpoint. Its profile-launch fallback may use a separate REM profile when Chrome owns the regular profile.
- Full real Windows control, Chrome navigation, Live Gemini, and external-service tests have not been established by the prior recovery report. These must be labelled separately from mocked tests.

## Guardrails for this task

- Back up each existing source file before editing it under `recovery/integration_backup/`.
- Preserve originals, configuration, credentials and current architecture. No dependency installs, Git staging/commit/push, destructive Git commands, or changes to `memory/routines.json`.
- Prefer local/offline tests. Do not send real messages, make purchases, access private account content, or invoke destructive actions.
- Report implementation and verification limits precisely; never claim a Windows/live test that did not run.
