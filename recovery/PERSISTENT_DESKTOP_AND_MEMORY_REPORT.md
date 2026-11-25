# Persistent Desktop and Memory — Implementation Report

Date: 2026-11-24
Project: `C:\important\Mark-LVII-main\Mark-LVII-main`

## Summary

This is a partial implementation of the requested desktop-intelligence upgrade. UIA now carries richer local control identity and supported pattern metadata. Supported UIA Invoke, SelectionItem, Toggle, ExpandCollapse operations are attempted semantically before physical-input fallback (left-click only falls back when Invoke is unsupported; stale controls fail closed). Persistent local observation and cloud screenshot sharing have separate saved settings and separate consent dialogs. The UI exposes observation, UIA/OCR, browser, and local-memory status. Browser contexts are checked for stale handles before reuse, and audio queue overflow is reported without content.

The full requested milestone is **not complete**. UIA event subscriptions, richer relationship/event caches, app-exclusion real tests, persistent-observation GUI restart tests, a SQLite migration, real semantic UIA pattern interaction, full browser lifecycle acceptance, keyboard acceptance, Qt shutdown, and real voice-to-action tests remain incomplete. Do not treat mocked tests as proof of those capabilities.

## Phase 0 architecture audit

The initial audit is recorded in [PERSISTENT_DESKTOP_AND_MEMORY_AUDIT.md](PERSISTENT_DESKTOP_AND_MEMORY_AUDIT.md).

- Python: 3.12.10 x64 in the project venv.
- Baseline suite: 87 tests passed before this prompt's implementation edits.
- Baseline discovery: 27 active plugins and 24 actions.
- Existing dirty workspace included `memory/routines.json`; this file was not edited by this implementation. No staging/commit/push occurred.
- Runtime components already installed: pywinauto, RapidOCR, ONNX Runtime, MSS, and stdlib SQLite. No dependencies were installed.
- Existing memory is JSON (`memory/long_term.json`), not SQLite. No second database was introduced and no memory records were migrated.

## Implementation details

### UI Automation

`core.screenview.Element` now retains AutomationId, runtime ID, parent label, focused state, process/window identity, and the subset of UIA patterns relevant to its control role. Pattern probing is role-scoped to limit COM calls. These local runtime IDs are not passed to the model. `screen_agent` includes safe control metadata in its output.

`core.screenview.perform_uia_pattern()` rechecks emergency-stop state, foreground process/title, current UIA tree, and runtime ID immediately before pattern execution. `core.screenact.click()` uses UIA Invoke for eligible left clicks. A stale control does not fall back to its old screen coordinates. `screen_agent` exposes explicit `select`, `toggle`, `expand`, and `collapse`, using SelectionItem, Toggle, and ExpandCollapse patterns. Other action paths continue to use the existing guarded physical mouse/keyboard controller.

UIA focus/window/structure event subscriptions and a stable event-fed UIA cache were not implemented. Persistent observation still polls fresh trees at an adaptive low rate.

### Persistent local observation and cloud privacy

`memory/config_manager.py` stores these settings through the existing atomic config writer:

- `persistent_desktop_observation_enabled` (defaults off)
- `cloud_screen_vision_enabled` (defaults off)
- `desktop_observation_exclusions` (defaults empty)

The COMPUTER USE panel requires a Yes response when either observation permission is first enabled. Local persistent mode calls UIA with OCR and pixel-digest fallback disabled. It does not capture or upload screenshots. Cloud screenshot delivery is refused before capture when its separate permission is off. Emergency Stop turns the saved local observation setting off, forces ON-DEMAND mode, and retains the existing stop latch/input release behavior. The setting is not automatically re-enabled after emergency stop.

Windows sign-in, credentials, lock and consent applications are hard-excluded. User exclusion names are matched locally against foreground process/title. The screenshot path performs the exclusion/secure-input-desktop preflight before capture. Secure desktop detection is implemented but was not exercised in a real UAC/secure-desktop test.

The new consent UI, app-exclusion editing, pause/resume persistence, and restart restoration have not been exercised in a live REM GUI session. Config read/write separation and screen-send refusal are mocked/temp-file tested.

### Browser lifecycle and verification

`actions/browser_control.py` now checks whether an existing Playwright context and attached browser are still live. Stale references are discarded and the configured endpoint/owned profile is reopened through the existing security path; it does not silently convert an attached Chrome session into a different profile. A context with no open tab may create a tab in that same context. Resume failure logs expose only the origin and exception type, not the full path/query.

A mocked closed-CDP-context reconnect test was added. No fresh live Chrome/CDP run was performed in this phase. Previous disposable-profile YouTube first/second/third watch-ID tests remain documented in `recovery/CHROME_VALIDATION_REPORT.md`.

### Audio queue and local memory

Inbound microphone queue overflow is counted and reported at most once every 30 seconds; the log contains a count only, not audio bytes. The event-loop enqueue remains nonblocking. When full, the oldest queued chunk is removed and the newest chunk is accepted to limit stale-audio latency; this intentional loss policy is covered by a mocked test. It is not live audio saturation acceptance.

The current memory manager is JSON-backed and already supports bounded prompts and local search. The prompt requested a SQLite upgrade but no existing SQLite memory schema/database exists. Introducing a separate SQLite database would conflict with the explicit no-duplicate-database constraint. Therefore SQLite migration, migrations, typed records/checkpoints, and real persistence/retrieval acceptance were not implemented. Existing `long_term.json` and `memory/routines.json` were left untouched.

### Mouse, keyboard, authorization, and safety

Existing central input lock, action-specific authorization, request snapshot, confirmation gate, password-field protection and emergency stop remain in place. This phase did not rewrite mouse or keyboard input. Prior disposable physical mouse results remain in `recovery/MOUSE_CONTROL_REPORT.md`; keyboard actions were not physically tested here. A real voice-to-tool session and spoken feedback test were not run.

## Test matrix

| Feature | Implemented | Mocked test | Real test | Result |
|---|---:|---:|---:|---|
| UIA discovery metadata (AutomationId, runtime ID, patterns) | Yes | Passed | Existing PyWinAuto smoke enumerated a disposable Chrome tree; this phase’s Tk fixture exposed no child controls. A follow-up screenview smoke could not foreground the fixture and read only two elements from the then-foreground non-disposable browser window; no screenshot or action occurred. | **MOCKED TEST PASSED; this phase’s real fixture check failed** |
| UIA Invoke/Selection/Toggle/ExpandCollapse | Partial | Passed for pattern extraction, Invoke fallback, select/toggle/expand/collapse dispatch, stale-target refusal | No real semantic pattern invocation | **MOCKED TEST PASSED** |
| UIA events and event-fed control cache | No | No | No | **NOT STARTED** |
| Persistent observation consent/settings | Yes | Temp config preserves unrelated settings; emergency disable tested | GUI consent/restart/pause test not run | **MOCKED TEST PASSED** |
| Cloud screen permission separate from local UIA | Yes | Cloud-off path proves no capture; settings separation tested | Live cloud authorization workflow not run this phase | **MOCKED TEST PASSED** |
| Secure desktop and excluded apps | Partial | Hard exclusion classifier and preflight order tested | No UAC/secure-desktop or configured exclusion app test | **IMPLEMENTED — NOT TESTED (Windows secure desktop)** |
| Physical mouse | Existing implementation | Existing suite | Earlier disposable-window suite passed; not repeated | **REAL COMPONENT TEST PASSED (prior report)** |
| Physical keyboard | Existing implementation | Existing suite only | No app-level keyboard acceptance | **IMPLEMENTED — NOT TESTED** |
| Multi-monitor/DPI targeting | Existing implementation | Existing suite | Not repeated | **IMPLEMENTED — NOT TESTED in this phase** |
| Browser DOM/CDP | Existing implementation | Existing endpoint and lifecycle tests passed | Earlier disposable Chrome/YouTube ID checks passed | **REAL COMPONENT TEST PASSED (prior report)** |
| Browser recovery | Improved stale context handling | Mocked closed-context reconnect passed | No live disconnect/reconnect test | **MOCKED TEST PASSED** |
| OCR screenshot fallback | Existing implementation | Existing tests passed | This real disposable Tk attempt returned 68 OCR elements but detected none of the expected fixture labels; earlier local OCR test in prior report returned text and boxes | **REAL TEST MIXED; this fixture attempt failed expected-label detection** |
| SQLite memory/schema | No migration | None | None | **NOT STARTED; existing store is JSON** |
| Memory retrieval/persistence | Existing JSON path | Existing suite | Not tested in this phase | **IMPLEMENTED — NOT TESTED in this phase** |
| Audio queue stability | Overflow counted/rate-limited | Passed queue-full test | No live audio saturation test | **MOCKED TEST PASSED** |
| Emergency stop | Existing behavior preserved; persistent consent is cleared | Passed disable/state test | Earlier disposable mouse-drag stop passed | **REAL COMPONENT TEST PASSED (prior physical input report)** |
| Gemini Live | Existing screen image delivery gated by user config | Dispatch and refusal tests passed | Earlier image+OCR marker test passed; no new Live test | **REAL COMPONENT TEST PASSED (prior report only)** |
| Full voice-to-action | No new full path | No end-to-end mock proves it | Not run | **NOT STARTED** |
| Qt shutdown/thread-affinity | No changes | None | Not run | **NOT STARTED** |

### Fresh Windows smoke details

The disposable acceptance script launched a temporary Tk window and closed it afterward. PyWinAuto initialized and found the top-level test window, but Tk exposed no child controls. RapidOCR ran against a fresh screenshot and returned 68 elements; none matched the expected fixture labels. The screenshot was not saved or sent to a model; local OCR ran over the full desktop image while the disposable window was topmost, so background content could also have been processed in memory. OCR output was filtered to the fixture strings only. In a separate follow-up, the disposable fixture could not be brought to the foreground; `screenview.look()` therefore returned a UIA view of the then-foreground non-disposable browser window (two elements). No click, typing, screenshot, or browser navigation occurred during that follow-up. This accidental foreground mismatch is disclosed here; no further active-window UIA checks were made. That is not valid disposable-window evidence and is recorded as a failed fixture attempt. The existing earlier report has a separate successful UIA enumeration on disposable Chrome. This phase has no real semantic Invoke/Selection/Toggle action acceptance.

## Files changed in this phase

- `core/screenview.py`
- `core/screenact.py`
- `core/desktop_control.py`
- `core/screen_observation.py`
- `actions/screen_agent.py`
- `actions/browser_control.py`
- `plugins/screen_agent.py`
- `memory/config_manager.py`
- `gui/window.py`
- `main.py`
- `readme.md`
- `tests/test_visual_targeting_extensions.py`
- `tests/test_screen_observation.py`
- `tests/test_desktop_hardening_paths.py`
- `tests/test_persistent_observation.py` (new)
- `recovery/PERSISTENT_DESKTOP_AND_MEMORY_AUDIT.md`
- `recovery/PERSISTENT_DESKTOP_AND_MEMORY_REPORT.md` (this report)
- Dated pre-edit snapshots in `recovery/integration_backup/desktop_intelligence_2026-11-24/` for the implementation files and tests covered by the phase baseline.

The pre-existing `memory/routines.json` modification remains untouched. No packages were added or changed.

## Validation

- Targeted screen, UIA, browser hardening, authorization and persistent-setting tests: **54 passed**.
- Full `python -m unittest discover -s tests -q`: **100 passed**.
- `compileall` over modified modules and tests: **passed**.
- `git diff --check`: **passed**; Git emitted only its existing LF/CRLF notices.
- Active plugin/action discovery remains **27 plugins / 24 actions**.
- Real Windows UIA/OCR smoke: mixed/failed fixture result described above.
- Real mouse and Chrome tests: prior acceptance reports only; not repeated for this phase.
- Real Gemini Live and full voice-to-action: not run for this phase.

## Launch and manual setup

```powershell
cd C:\important\Mark-LVII-main\Mark-LVII-main
& "..\.venv\Scripts\python.exe" main.py
```

To enable persistent local awareness, open COMPUTER USE and turn on **PERSISTENT LOCAL UIA OBSERVATION**, then accept the consent dialog. To permit on-demand screenshots to reach the selected cloud model, independently enable **ALLOW SCREEN IMAGES TO SELECTED CLOUD MODEL** and accept its consent. Both are off by default in a new configuration. Existing Chrome CDP remains opt-in and loopback-only via `REM_CHROME_CDP_URL`.

## Remaining work

1. Implement and test UIA focus/window/structure/property event subscriptions on a dedicated worker with safe COM apartment/lifecycle handling.
2. Add a real disposable Win32/WPF/WinForms UIA fixture that exposes accessible buttons/edit controls; verify actual UIA Invoke/Value/Selection/Toggle patterns and stale references.
3. Fix or characterize why the fresh Tk OCR fixture labels were not recognized; repeat with a known test app and boxes.
4. Run persistent-observation UI consent, pause/resume, restart, exclusion, screenshot-offline and emergency-disable scenarios in a disposable REM session.
5. Test secure-desktop refusal and application exclusions on real Windows without capturing credentials.
6. Add comprehensive browser disconnected/context-closed/CDP timeout tests and real disposable Chrome reconnect acceptance.
7. Decide whether a safe one-time JSON-to-SQLite migration is desired; do not create a duplicate DB or migrate until data-preserving backup/rollback behavior is designed.
8. Run physical keyboard, multi-monitor, live audio queue saturation, Qt shutdown, Gemini Live, and end-to-end voice-to-action acceptance with disposable apps and data.

## Follow-up: native UIA fixture diagnosis (2026-11-24)

A compiled, disposable .NET WinForms fixture was launched with a unique title. The Windows computer-use accessibility tree exposed the edit field, button, list and list items, checkbox, combo box, tab items, menu item, IDs, and focused edit control. No existing Chrome, VS Code, or unsaved document was read or acted on during the fixture test.

REM's Python 3.12.10 x64 environment reports PyWinAuto 0.6.9. Calling `UIAElementInfo` on the fixture HWND initialized successfully and returned the correct window and process identity, but `children()` and `Desktop(backend="uia").descendants()` returned only one `TitleBar` child. Consequently `core.screenview._scan_windows()` emitted only the top-level window element. A normal `look()` attempt exceeded the current 1.2-second scan budget and marked that window slow for the Python process. The observed failure is therefore in REM's PyWinAuto UIA tree enumeration for this provider, plus the scan-budget fallback; it is not evidence that UIA or the MFC runtime cannot initialize. Exact cause within the mismatch between the provider tree exposed to the Windows accessibility client and PyWinAuto's `IUIAutomationElement.FindAll` control-view result still needs a focused backend-level diagnosis. No semantic action was attempted because REM did not produce a valid target.

Added `focus` as a first-class UIA element operation that re-finds the runtime ID, validates window/process/AutomationId and stop state, then calls `UIAElementInfo.set_focus()` without using a coordinate click. Its regression test is mocked; it is not counted as real acceptance because REM cannot yet discover the fixture child through its scanner.

The fixture process was closed after the test. The locally compiled `recovery/windows_acceptance/NativeUiaFixture.exe` could not be deleted by the workspace shell (Windows returned Access Denied); it is a generated, disposable test binary. Its C# source remains for diagnosis. No packages or runtimes were installed. `memory/routines.json` was not opened or written by this continuation.

Follow-up validation: `python -m unittest discover -s tests -q` — **101 passed**; `compileall` — **passed**; `git diff --check` — **passed** with the existing LF/CRLF notices. These are offline tests and do not change the real UIA result above.

## UIA enumeration follow-up (2026-11-25)

An independent comparison refined the earlier WinForms result. On the same disposable HWND/PID, PyWinAuto 0.6.9 and a direct comtypes CUIAutomation query each returned only the TitleBar child; REM visited the root and that child, retained only the root, and filtered the unnamed title bar. The Windows .NET System.Windows.Automation API returned ten client child HWNDs plus the title bar, but all client nodes were generic Pane elements with no supported Value/Invoke/Toggle/SelectionItem/ExpandCollapse/LegacyIAccessible patterns. The Computer Use accessibility output was not counted as direct UIA evidence.

The fixture reported .NET Framework release 533509 (4.8.1), and its configured legacy-accessibility switches were false. Enabling those fixture-only switches did not change the result. The failure is before REM conversion/filtering; the precise provider/UIA control-tree discrepancy remains unresolved. No semantic action was attempted. See recovery/UIA_ENUMERATION_ROOT_CAUSE_REPORT.md for the HWND/PID trace, diagnostic logs, counts, and limitations. The new REM_UIA_DIAGNOSTICS environment switch is off by default and omits UIA values/screenshots.

Current follow-up tests: 103 offline tests passed, including the diagnostic-logging tests. Real UIA semantic focus/value/invoke, stale references, and UIA events remain unvalidated.
