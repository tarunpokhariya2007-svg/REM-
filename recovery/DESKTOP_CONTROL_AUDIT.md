# Desktop Control Architecture Audit

Date: 2026-11-24  
Scope: active code paths in the existing REM checkout, with emphasis on screen observation, browser/native actions, request authorization, confirmation, and tool feedback.

## Spoken-to-tool path

1. Gemini Live yields `response.tool_call` in `RemLive._receive_audio()` in `main.py`.
2. The current direct transcript is recorded with source `voice`; `_execute_tool()` gets `(user_request, source)` and checks desktop tools with `core.desktop_control.authorize_user_request()`.
3. `_execute_tool()` dispatches actions through `_action_registry`, plugins through `_plugin_registry`, or the built-in screen path. The plugin compatibility route `RemUI.run_tool` also performs authorization for registered desktop tools.
4. `screen_process` uses a fresh `_capture_screen()`, calls `screenview.look(..., allow_ocr=True)` for best-effort metadata, enters `desktop_control.action("observe")`, and stores the image/question for delivery.
5. `_flush_pending_vision()` sends an image `Blob` through `send_realtime_input(video=...)` and then sends text containing the actual request and local observation. Delivery exceptions are handled as failure and the model is told not to describe an image it did not receive.
6. `_execute_tool()` returns a Gemini `types.FunctionResponse`; `_receive_audio()` calls `session.send_tool_response(function_responses=...)` for the tool-call batch.

The screen observation tool is therefore discoverable only when the active provider exposes tools and its vision capability enables `screen_process`. The current screen tool schema uses `angle` and `text`. The tool call result itself is ordinary text metadata; the image and observation question are separately supplied to the same Live session. This sequence is the SDK's existing application path, but a real Live round-trip has not been validated in this local audit.

## Read-only observation

- `core/screen_observation.py` supplies `capture_observation`, sanitised metadata formatting, image preflight and an explicit read-only/untrusted-screen prompt.
- `core/screenview.py` declares independent `mss`, `pywinauto`, and OCR availability and captures using `mss`; UIA is attempted on Windows, with OCR only if an OCR backend is installed and requested.
- `actions/screen_agent.py` has an `observe` action that obtains local accessibility/OCR details without requesting an input event.
- `desktop_control.action("observe")` shares the central lock and stop check but sets `mouse_keyboard=False`.
- No evidence in this audit shows the screen code treating on-screen instructions as authorization. `vision_question()` explicitly marks screen content as untrusted and read-only.
- Actual Windows runtime check: `mss` captured the 1920×1080 virtual desktop. `screenview.look()` returned `source=none`, degraded, with no accessible elements. The error is `DLL load failed while importing win32ui: The specified module could not be found.` PE import inspection shows `win32ui.pyd` requires `mfc140u.dll`; that DLL is absent from the venv and `C:\Windows\System32`. Pywinauto's package import chain loads `win32ui`, so Windows UIA is **not functioning** in this runtime. `win32api` and `win32gui` import successfully.
- PyWin32 312's extensions are present in this Python 3.12 venv, and `win32api`/`win32gui` load; this does not look like a general interpreter ABI mismatch. The immediate failure is the missing MFC runtime DLL required by `win32ui`. No dependency was installed. No OCR backend is installed, so OCR is also unavailable.
- If UIA/OCR metadata fails, the explicit Live screen path still separately captures via MSS and queues a screenshot plus the actual user question to Gemini. The local mocked test verifies this routing; an actual authenticated Gemini Live transfer was not run.

## Native desktop path

- `actions/screen_agent.py` uses fresh `screenview.look()` observations, resolves named controls, refuses missing/ambiguous targets, calls `screenact`, then re-observes to verify.
- `core/desktop_control.py` has a process-wide non-blocking action lock, pause and emergency-stop events, cancellers, and `action()` guards. `pause()` and `emergency_stop()` cancel registered futures and call `screenact.release_all()`.
- `actions/computer_control.py` has lower-level PyAutoGUI operations with stop checks in input helpers. It remains exposed as a separate tool, so the central Live authorization gate and shared action lock are important boundaries.
- Cancellation is cooperative for native event helpers and depends on a registered cancellation callback for long-running operations. An OS input already delivered cannot be recalled; the code can release held buttons/keys best-effort.
- `LIVE ASSIST` local accessibility polling is separate from the explicit screenshot-to-Gemini observation.

## Browser path

- `actions/browser_control.py` supports Playwright locator-backed DOM operations, uniqueness checks, visible/enabled checks, page-state comparisons and timeout handling. Browser operations are wrapped in the shared desktop action lock.
- `_BrowserSession._launch()` calls `launch_persistent_context()` with the normal browser profile only when the browser process is not already running. If it is running, code falls back to a persistent REM profile.
- This task adds an opt-in `REM_CHROME_CDP_URL` path for explicit loopback-only Chrome endpoints. It does not enable a port or launch a remote-debug browser. CDP attachment is covered by mocked tests only; no real debug endpoint was configured or tested. When attached, the last listed live tab is selected because Playwright CDP does not provide a reliable foreground-tab identity.
- This task rejects non-HTTP(S) browser URLs, credentials in URLs, and malformed ports. For `go_to`/`new_tab`, authorization requires the destination host labels and explicit port (when provided) to be present in the direct user request. This does not yet validate every redirect destination or implement a complete DNS/private-network policy.
- No Playwright browser scenario or real Chrome CDP test was performed as part of this audit.

## Authorization and trust boundaries

- Authorization uses the latest direct transcript and source (`voice`, `typed`, `dashboard`); untrusted sources are rejected. Screen observation has a separate direct-request classifier and does not require a target or input action.
- `screen_agent` bounded task/routed-tool steps are recursively checked against the same request.
- Existing authorization is largely token/substring intent matching. It is not a formal policy decision tied to a unique request ID. A stale transcript can remain the latest context if a call arrives outside the expected user turn; this warrants a request-lifecycle audit.
- Current Telegram is described in recovery docs as a restricted external text relay. It is not treated as a trusted direct local request by the desktop authorization source allowlist.
- Web content and UI text are untrusted data in screen prompts; authorization should continue to derive only from direct input.

## Code execution and file boundaries

- `actions/dev_agent.py` writes generated project files beneath the selected `PROJECTS_DIR`; model-proposed dependencies are not installed. Before this task, its build path nevertheless ran a planner-generated command automatically and could use raw output to ask a model for repairs.
- This task moves that run behind the existing HUD confirmation, displays the exact command and working directory, limits the target to an existing `.py` file resolved under the generated project folder, disables shell interpretation, caps execution at 300 seconds, registers process termination with central desktop cancellation, captures output, and returns only exit/output-length metadata to Gemini.
- This is not an OS sandbox: after a human approves the run, generated code can still access the user's normal process permissions and network. Output is withheld from Gemini but is not analyzed for program correctness. The project remains unverified unless the user independently reviews the run status/output.
- `actions/file_controller.py` confines resolved operations to the user's home directory and protects root folders from deletion. This audit did not add per-operation request binding or eliminate filesystem race conditions.

## Confirmation and results

- `core.confirm.request()` creates a single 90-second pending operation and shows exact title/detail in the REM UI. The UI owns accept/reject; a model-supplied boolean cannot approve it.
- This task adds terminal callbacks for approval, denial, expiry and failure. `RemLive` queues a system event into the still-active session using the project's existing `send_realtime_input(text=...)` API under the realtime input lock. It rejects results from an ended/replaced session. This is unit-tested against a fake session; actual Gemini Live delivery remains unverified.
- Normal synchronous tool results are returned via `FunctionResponse` and `send_tool_response` in the same tool-call batch. The outcome structure is effectively a short result string, not a consistently typed object.

## Risks and bounded follow-up priorities

1. Add an explicitly configured, loopback-only Chrome CDP attach path and report refusal/fallback; do not silently enable a debugging port.
2. Bridge pending confirmation outcomes back into the active Live session using an SDK-supported input method and lifecycle/session identity checks.
3. Tighten browser URL policy without breaking intended navigation; test schemes and private/local destinations.
4. Convert authorization from broad word intersection to request/action-specific decisions and expire context per turn.
5. Exercise stop/cancel and lock behavior under tests; distinguish cancellation from already-delivered OS input.
6. Runtime-check `mss`, `pywinauto`, PyWin32, OCR packages and Windows API availability without installing dependencies.
7. Validate the controlled CDP path against a separately launched disposable Chrome instance and a real Live confirmation round-trip.

## Evidence limits

This is a code-path audit, not proof of actual Windows UIA, Chrome, Gemini Live, OCR, or external service behavior. Test classes and exact runtime checks belong in `recovery/TEST_RESULTS.md` as they are executed.

## Targeting extension follow-up

The screen agent now routes bounded semantic cursor move/hover/right/double-click operations through the same `desktop_control.action()` lock and `screenact` availability checks. Right/double actions have request-intent checks, and consequential labels retain confirmation. Browser smart-click has a YouTube-only ordinal video-card path that verifies the resulting watch ID. This is code and mocked-test evidence only; it does not resolve the host UIA/MFC failure or establish actual native visual accuracy, a real YouTube DOM run, real physical mouse delivery, or live Gemini delivery.
