# Desktop Hardening Test Results

Date: 2026-11-24

## Mocked/unit tests

- Before this pass: `python -m unittest discover -s tests -p 'test_*.py'` — 62 tests passed.
- Before the final code-execution gate: full suite — 69 tests passed.
- After the final code-execution gate: `..\\.venv\\Scripts\\python.exe -m unittest discover -s tests -p 'test_*.py'` — **73 tests passed** in 8.252 seconds.
- Added `tests/test_desktop_hardening_paths.py` for loopback-only CDP endpoint validation, attach/reuse/disconnect semantics, unsafe browser URL rejection, local React URL support, user-request destination binding, confirmation approval/denial/expiry/failure feedback, stale Live session rejection, and text result delivery to a fake session.
- Added `tests/test_dev_agent_execution_gate.py` for generated-code approval, project-contained script checks, shell metacharacter rejection, captured-output withholding, exit status and timeout termination. Its tests run only fixed harmless scripts in temporary directories.
- Existing `tests/test_screen_observation.py` covers screenshot dispatch, capture/encoding/delivery failure, lifecycle, and frame-plus-question queueing with mocked sessions. Existing screen-agent and authorization tests cover read-only observation and no input dispatch for observe paths.
- Tests are mocked/offline; they do not prove that Gemini Live accepted or spoke a result.

## Actual Windows runtime checks

- Host: Windows 11, Python 3.12.10, PyWin32 312, pywinauto 0.6.9, MSS 10.2.0, Playwright 1.65.2, google-genai 2.34.0.
- MSS captured an in-memory virtual desktop frame at 1920×1080. The frame was neither saved nor printed.
- `win32api` and `win32gui` imported. `win32ui` failed to load because `mfc140u.dll` is missing; pywinauto's UIA initialization consequently fails. Runtime UI Automation result: **not working**.
- RapidOCR and PaddleOCR were both absent. Runtime OCR result: **unavailable**.
- No real Notepad/VS Code operation, Chrome CDP session, Gemini Live call, or external service action was run.
- A Windows app inventory found the existing VS Code project window and left it untouched. Launching Notepad restored a pre-existing unsaved `*Programming is basically a way to g - Notepad` document. Per the no-overwrite/no-close rule, no input was sent and the document was left open and untouched; this is not counted as a Notepad interaction test.
- The environment's asyncio default event-loop creation blocks in `socket.accept()` while creating its Proactor self-pipe. Tests that exercise async code therefore use the repository's immediate-coroutine harness with fake sessions; no live asyncio network loop was used for the CDP or Gemini checks.

## Static checks

- `..\\.venv\\Scripts\\python.exe -m compileall -q main.py actions core plugins providers tests` — passed after final code changes.
- `git diff --check` — passed after final code changes; Git emitted only its existing LF-to-CRLF notices.
- `pytest` was unavailable in the project venv and was not installed. The repository's unittest suite ran successfully.

## Visual targeting extension validation

- `..\\.venv\\Scripts\\python.exe -m unittest discover -s tests -p 'test_*.py'` — **81 tests passed** after adding coverage for action schema exposure, right/double authorization, confirmed right-click preservation, read-only/no-input observation, semantic cursor-move dispatch, YouTube first/second target identity and empty-result refusal. This run was mocked/offline; it did not contact Gemini or YouTube.
- `..\\.venv\\Scripts\\python.exe -m compileall -q main.py actions core plugins providers tests` — passed.
- `git diff --check` — passed. Git emitted line-ending normalization notices for pre-existing modified files.
- Physical cursor, native Windows target, YouTube page, Chrome CDP, Gemini Live, and confirmation round-trip acceptance tests remain unrun.

## Approved dependency and live acceptance update — 2026-11-24

- Latest full mocked/offline unittest suite after installation and selector correction: **84 tests passed**. The new live-title regression test is included. No tests were installed; `pytest` remains absent, so unittest was used.
- Official Microsoft-signed x64 Visual C++ v14 Redistributable installation completed (exit code 0, runtime 14.52.37029.00). `win32ui` now imports; pywinauto UIA initialized and read the disposable Chrome UI tree (560 descendants). The earlier statements above describe the pre-install state.
- RapidOCR 3.10.0 + ONNX Runtime 1.32.0 were installed into the existing REM venv. A real 1920×1080 MSS screenshot was OCRed locally; known test text and bounding boxes were returned. No screenshot was saved.
- Real disposable-window physical mouse checks passed: absolute/relative movement, left/right/double-click, hover, scroll, drag/drop, release, and emergency stop during active drag.
- Real disposable Chrome/CDP checks passed for first, second and third visible YouTube results. Each URL's watch ID matched the currently inspected card. A blank-page missing target was refused without navigation. The existing Chrome profile was untouched.
- Real Gemini Live connected and accepted a cropped disposable-window image/question. An image-only probe misread the locally OCR-confirmed marker. A second probe using the production-style read-only/untrusted `vision_question()` plus actual OCR context generated audio whose Live transcript correctly reported `purple teapot`. This verifies the enriched image-plus-OCR path on a controlled screen, while raw image-only interpretation remains unreliable.
- Playwright startup's pre-driver hang was traced to sandbox-restricted loopback socketpair initialization. The same startup passed outside that sandbox in 0.47 seconds, after which CDP tests passed.

