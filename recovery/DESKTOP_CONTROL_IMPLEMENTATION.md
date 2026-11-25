# Desktop Control Implementation Record

Date: 2026-11-24

This record covers the bounded hardening implemented in this pass. It does not assert that the entire desktop-autonomy master plan is complete.

## Implemented

- Added `REM_CHROME_CDP_URL` as an explicit opt-in attachment to an existing Chrome DevTools endpoint. It accepts only HTTP(S)/WebSocket endpoints on `localhost`, `127.0.0.1`, or `::1`, requires a port, and rejects credentials/query strings. REM never launches Chrome with remote debugging enabled.
- CDP attachment uses Playwright's supported `connect_over_cdp(..., no_defaults=True)` API, reuses an existing context and tab, and fails closed when the configured endpoint/context/tab is unavailable. Closing the REM browser session disconnects through `Browser.close()` for a connected browser rather than closing the user's attached default context.
- Added direct-request authorization for `go_to` and URL-bearing `new_tab`: the destination host labels and explicit port must be named by the latest direct request. This prevents a webpage suggestion from supplying a new navigation destination through those paths.
- Browser URL normalization now permits only HTTP(S), rejects URL credentials, malformed ports and non-web schemes, and supports explicit loopback React development URLs such as `localhost:5173` over HTTP.
- Added terminal confirmation outcome callbacks for approved, denied, expired and failed actions. The callback sends a constrained system event into the current Gemini Live-compatible session, through `send_realtime_input(text=...)` and the same realtime input lock used for other text/image events. A stale session is rejected. Logs no longer include the returned action detail or exception text.
- Changed the new-project development-agent path so it no longer executes planner-generated code automatically or auto-repairs based on its output. The HUD must approve an exact command and working directory first. The runner accepts only a project-contained Python script, uses `shell=False`, applies a maximum 300-second timeout, registers terminate for emergency cancellation, captures output without logging or sending it to Gemini, and reports the process exit code without claiming application correctness.
- Added the baseline and active-path audit, implementation/test/security/validation reports, and backups of pre-edit files in `recovery/integration_backup/desktop_hardening_baseline/`.

## Runtime screen access findings

- MSS captured the current Windows virtual desktop at 1920×1080 without saving or logging pixels.
- Windows accessibility metadata did not work in this venv. `win32ui.pyd` directly imports the absent `mfc140u.dll`; `pywinauto` imports `win32ui` as part of its UIA module initialization. PyWin32 312's `win32api` and `win32gui` work with Python 3.12.10, so the measured blocker is the missing MFC runtime dependency, not a demonstrated general ABI mismatch.
- OCR is unavailable: neither `rapidocr_onnxruntime` nor `paddleocr` is installed.
- Screen image/question delivery has mocked coverage. No authenticated Gemini Live session was used to prove remote receipt or speech.

## Still incomplete

- Real CDP attachment and Google/YouTube navigation; no debugging endpoint was active for this test.
- Real Windows Notepad, VS Code, native-control, DPI/multi-monitor interaction and emergency-stop smoke tests.
- Actual Gemini Live confirmation result delivery and spoken response.
- Full browser redirect/private-network policy, robust URL/redirect policy for every action, and robust per-request authorization IDs/expiry.
- OS-level sandboxing and full path policy for generated code/file operations; cancellation of every subprocess type.
- Generated Python remains unsandboxed after the user approves the exact command and folder; a hostile project can use the user's ordinary file/network permissions. No automatic build-run repair is performed.
- End-to-end cancellation and input arbitration under concurrent browser/native tasks.
- Telegram replay/rate/pairing security and live integration validation.
- Full browser interaction suite (locator ambiguity/iframes/shadow DOM/tab identity), dashboard status work, and exhaustive per-plugin regression.

Those items require separate implementation or runtime setup and must remain described as unverified. No dependencies or external integrations were installed or activated in this pass.
