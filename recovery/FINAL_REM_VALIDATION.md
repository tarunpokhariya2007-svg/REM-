# REM Desktop Hardening Validation

Date: 2026-11-24  
Last completed phase: architecture discovery, targeted hardening implementation, bounded semantic mouse/YouTube ordinal additions, mocked tests, and local Windows dependency/capture checks.

## Completed in this pass

- Recorded pre-existing worktree changes and protected user data in `REM_HARDENING_BASELINE.md`.
- Traced the current voice → Gemini Live tool call → authorization → dispatch → image/text injection → FunctionResponse path in `DESKTOP_CONTROL_AUDIT.md`.
- Added opt-in loopback-only Chrome CDP attach, HTTP(S)-only URL validation, direct-request host/port binding, Gemini-session confirmation outcome feedback, and a user-confirmed bounded runner for generated Python projects.
- Backed up each edited pre-existing source/document file before editing under `recovery/integration_backup/desktop_hardening_baseline/`.
- Ran the test suite before edits: 62 tests passed. After the first changes, 69 tests passed; after the code-execution change, all **73 unittest tests passed**. Project-wide compileall and `git diff --check` passed after final code changes.
- Verified in a real Windows process that MSS capture works at 1920×1080. Identified the exact `win32ui` dependency blocker and confirmed OCR packages are absent.
- Added accessibility-backed semantic move/hover/right/double-click and bounded relative/named-position cursor actions. Added explicit authorization wording checks for right/double-click.
- Added YouTube ordinal card targeting for first/second/third/last with unique video identity and resulting watch-ID verification. Tests use fake DOM/page objects only.
- Latest mocked/offline suite: **81 unittest tests passed**; compileall and diff-check passed. Detailed status is in `TEST_RESULTS.md`.

## Not completed

This is not completion of the complete desktop-autonomy master plan. Real Chrome attachment/navigation, live Gemini tool/confirmation delivery, Notepad/VS Code interaction, physical mouse acceptance, UIA-backed native target accuracy, multi-monitor/DPI control, end-to-end emergency-stop tests, broad URL redirect policy, per-request authorization lifecycle redesign, OS sandboxing, complete file/path hardening, comprehensive Telegram hardening, and dashboard/status additions remain unimplemented or unverified. The current host cannot exercise UIA or OCR because of the runtime dependency conditions documented above. The optional Notepad smoke stopped when launching it restored an existing unsaved document; no text was entered and the document was left open. No dependencies were installed and no live external actions were performed.

## Protection checks

- Do not stage, commit, push, reset or clean Git state.
- Preserve `memory/routines.json`, credentials, settings, recovered originals and all earlier user changes. This pass does not edit `memory/routines.json`.
- Final test/static/diff results are recorded in `TEST_RESULTS.md`; residual risks are in `SECURITY_HARDENING_REPORT.md`.
- Existing worktree changes remain present, including the pre-existing modification to `memory/routines.json`; this pass did not edit that file. No changes were staged, committed, pushed, reset or cleaned.

## Files changed by this hardening pass

- `actions/browser_control.py`
- `core/confirm.py`
- `core/desktop_control.py`
- `main.py`
- `readme.md`
- `actions/screen_agent.py`
- `core/screenact.py`
- `plugins/screen_agent.py`
- `tests/test_visual_targeting_extensions.py`
- `recovery/MOUSE_CONTROL_REPORT.md`
- `recovery/TARGET_ACCURACY_REPORT.md`
- `recovery/CHROME_VALIDATION_REPORT.md`
- `recovery/integration_backup/visual_targeting/` (pre-edit snapshots of each modified existing source file)
- `tests/test_desktop_hardening_paths.py`
- `actions/dev_agent.py`
- `tests/test_dev_agent_execution_gate.py`
- `recovery/REM_HARDENING_BASELINE.md`
- `recovery/DESKTOP_CONTROL_AUDIT.md`
- `recovery/DESKTOP_CONTROL_IMPLEMENTATION.md`
- `recovery/SECURITY_HARDENING_REPORT.md`
- `recovery/TEST_RESULTS.md`
- `recovery/FINAL_REM_VALIDATION.md`
- Backups: `recovery/integration_backup/desktop_hardening_baseline/` (pre-edit `actions/browser_control.py`, `actions/dev_agent.py`, `core/confirm.py`, `core/desktop_control.py`, `main.py`, and `readme.md`).

## Real Windows, browser and Live acceptance update — 2026-11-24

### Results

- **Mocked/offline tests:** 83 unittest tests passed (`..\\.venv\\Scripts\\python.exe -m unittest discover -s tests -v`). This includes mocked screen observation delivery, UIA-unavailable screenshot fallback, OCR adapter mapping, semantic input guards, and YouTube fake-DOM identity/URL checks. These do not count as real UIA/OCR/browser/Live verification.
- **Windows screenshot:** MSS capture works in the real Windows process at 1920×1080 (confirmed in prior local check). The screenshot payload was not sent to an external service during this acceptance work.
- **Windows UI Automation:** unavailable. The immediate import failure is `win32ui` DLL loading because `mfc140u.dll` is missing. Python is CPython 3.12 x64 and PyWin32 312; architecture/package-tag evidence is consistent, so this is not established as a Python/PyWin32 ABI mismatch. `pywinauto` 0.6.9 cannot initialize `Desktop` until this loader dependency is resolved. The appropriate Microsoft-supported remediation identified is the x64 Visual C++ v14 Redistributable (official Microsoft latest-supported package; the MFC deployment guidance covers `mfc140u.dll`). It was not installed because system-runtime changes require approval.
- **OCR:** no supported OCR backend is installed. The code now recognizes current RapidOCR result objects and keeps OCR optional. The chosen local CPU route is current `rapidocr` with `onnxruntime`, based on upstream RapidOCR install guidance; neither package was installed, so no actual text/bounding-box OCR was performed. Dependency installation requires approval.
- **Physical mouse:** the real cursor reached the requested coordinate, and Windows confirmed it was over the disposable test window. However, the expected Tk hover event never registered. The harness stopped before clicks. Physical control acceptance therefore **failed/incomplete**, despite cursor movement itself succeeding. No action was sent to a user document or existing Chrome window. Details are in `MOUSE_CONTROL_REPORT.md`.
- **Live Chrome/YouTube:** not run. No disposable Chrome profile or CDP session was launched; existing Chrome was not interacted with. No target video ID or resulting watch URL was produced. Details are in `CHROME_VALIDATION_REPORT.md` and `TARGET_ACCURACY_REPORT.md`.
- **Gemini Live:** no real authenticated Live session was available or exercised. Unit tests verify mocked dispatch/result delivery only. No screenshot was sent to Gemini in this work.

### Completion status

Acceptance is **not complete**. User's completion criterion requires demonstrated physical mouse control and correct-target browser selection; neither has been demonstrated. UIA restoration and local OCR also require the user's approval to install the supported runtime and OCR dependencies. No system runtime or Python dependency was installed. No Git stage, commit, push, reset or cleanup was performed. `memory/routines.json` remains a pre-existing modified file and was not edited in this pass.

### Test harness changes

- `core/screenact.py` now checks emergency-stop state between bounded drag segments and releases a held button on cancellation; a mocked regression test covers release behavior.
- `core/screenview.py` recognizes the current RapidOCR API alongside existing optional OCR backends; a mocked adapter test covers text and box mapping.
- `recovery/windows_acceptance/mouse_test_window.py` and `run_mouse_acceptance.py` create and close a disposable local test window. This harness is not part of the offline test suite; its physical run did not pass the hover guard and stopped without clicking.

## Approved dependency setup and acceptance results — 2026-11-24

### Dependencies and UI Automation

- Installed the official `https://aka.ms/vc14/vc_redist.x64.exe` package after verifying a valid Microsoft Corporation Authenticode signature. Installer exit code: **0**. Runtime registry reports x64 version **14.52.37029.00**; `System32\mfc140u.dll` and `System32\vcruntime140.dll` now exist.
- The original `win32ui` import failure was caused by missing MFC runtime DLLs, not an established Python/PyWin32 ABI mismatch. Environment: CPython **3.12.10 x64**, PyWin32 **312**, pywinauto **0.6.9**.
- After install, `import win32ui` succeeded. `Desktop(backend="uia")` initialized. On the disposable Chrome window, pywinauto found the window and enumerated **560 UIA descendants**, including browser controls and YouTube page text. Tk test labels themselves were not UIA children; that provider limitation was covered by OCR fallback.

### OCR

- Installed into the existing REM virtual environment only: **rapidocr 3.10.0**, **onnxruntime 1.32.0**. Resolved runtime dependencies include `opencv-python-headless 5.1.0.95`, `Shapely 2.2.0`, `pyclipper 1.4.0`, `omegaconf 2.4.0`, `tqdm 4.70.1`, `colorlog 6.12.0`, `flatbuffers 25.12.19`, and `packaging 26.4`. No global Python packages were installed.
- First inference populated the RapidOCR model files under `.venv\Lib\site-packages\rapidocr\models` (PP-OCRv6 detection/recognition and the mobile classifier). No screenshot content was uploaded for OCR.
- Actual MSS screenshot OCR found 65 elements, including `REM MOUSE ACCEPTANCE — disposable test surface` at `[394,188,657,32]`, `Left click target` at `[373,271,112,26]`, `Right-click target` at `[567,273,122,23]`, and `Hover target` at `[972,272,92,24]`. The screenshot was not saved.

### Physical mouse

- **Passed on a disposable window:** absolute movement, relative movement, left-click, right-click, double-click, hover, scroll, drag/drop, mouse-down/release, and emergency stop during an active drag. The test checked actual cursor position plus real app callbacks/drop result. The emergency stop interrupted the drag, released the button, and prevented the drop.
- The prior hover miss came from mixing client-relative Tk coordinates with the outer Win32 window rectangle. Correct client mapping uses `ClientToScreen` and `GetClientRect`; the effective client scale measured about 1.25× while `GetDpiForWindow` reported 96. A separate fixture-executable issue also caused the self-window safety check to refuse clicks when the fixture ran as `python.exe`; testing with `pythonw.exe` left the safety guard intact and let REM target the separate disposable process.

### Chrome and target accuracy

- The initially stalled Playwright startup was an environment restriction: the Windows Proactor event loop blocked in its localhost `socket.socketpair()` fallback inside the sandbox. Outside that restriction, Playwright **1.65.2** started in **0.47 s** with bundled Node **26.13.1**.
- REM attached to a new, loopback-only CDP Chrome process with a temporary profile. Existing Chrome was not touched.
- Live YouTube checks through REM's `smart_click` passed:
  - First: `Learn React With This ONE Project`, intended/selected ID `G6D9cBaLViA`, URL `https://www.youtube.com/watch?v=G6D9cBaLViA`.
  - Second: `React Tutorial for Beginners`, intended/selected ID `SqcY0GlETPk`, URL `https://www.youtube.com/watch?v=SqcY0GlETPk`.
  - Third: `Build 25 React Projects – Tutorial`, intended/selected ID `5ZdHfJVAY-s`, URL `https://www.youtube.com/watch?v=5ZdHfJVAY-s`.
- Each resulting watch ID matched the freshly inspected ordinal card. A missing-target request on `about:blank` refused and left the URL unchanged. No live ambiguous-card case or spoken-voice command test was run.
- The live run exposed and fixed a title extraction bug: a selector chose the thumbnail's duration overlay before the actual video-title anchor. The code now prioritizes `a#video-title`; a regression test and live retest passed.

### Gemini Live

- The configured Gemini key was present and local-only mode was off. A real session connected to `models/gemini-3.1-flash-live-preview`; the frame and question sends returned, and the service generated audio.
- **Image-only interpretation was unreliable:** local OCR confirmed `Unique marker: purple teapot`, but an image-only Live prompt returned `VJ351`; an earlier call described unrelated configuration content.
- **The production-style observation prompt passed:** a second real Live session received the same kind of cropped image plus `vision_question()`'s read-only/untrusted prompt and actual OCR text. Gemini generated 161,282 bytes of audio, and Live's spoken transcript correctly said: “The exact unique marker shown is: purple teapot.” The OCR bounding polygon on the cropped screenshot was `(383,322)`, `(779,323)`, `(779,360)`, `(383,359)`. This verifies the configured image-plus-local-OCR-context Live observation path on a controlled screen; image-only recognition remains unreliable.
- The test sent only a cropped, data-free disposable window to the already configured Gemini service. No screenshot was saved.

### Tests, changed files and protection

- Latest offline suite: **84 tests passed** via `..\.venv\Scripts\python.exe -m unittest discover -s tests -v`. This suite is separate from the physical, UIA, OCR, Chrome and Gemini results above.
- No edit to `memory/routines.json`; no staging, commit or push. User's existing Chrome profile and unsaved documents were not interacted with.
- This acceptance update changed `core/screenact.py`, `core/screenview.py`, `actions/browser_control.py`, `tests/test_visual_targeting_extensions.py`, the four reports in this folder, and disposable test harnesses under `recovery/windows_acceptance/`. A pre-edit snapshot of the updated browser selector is in `recovery/integration_backup/windows_acceptance/actions/browser_control.py`.

### Completion status

Physical mouse control, UI Automation, OCR fallback, live YouTube first/second/third target selection, and the production-style Gemini Live image-plus-OCR observation all passed on the stated disposable targets. Gemini's image-only interpretation was wrong in separate probes, so REM should keep using its OCR context and treat raw vision as fallible. Spoken voice-transcription commands and a live ambiguous-video target remain untested.
