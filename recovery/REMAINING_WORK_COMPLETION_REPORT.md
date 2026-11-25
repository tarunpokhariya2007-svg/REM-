# REM Remaining Work — Validation and Completion Report

Date: 2026-11-24  
Project: `C:\important\Mark-LVII-main\Mark-LVII-main`  
Virtual environment: `C:\important\Mark-LVII-main\.venv`

## Executive result

This continuation did **not** complete end-to-end desktop acceptance. The central unresolved issue is now narrowed: Windows' accessibility client exposed all controls in a disposable native WinForms window, while REM's PyWinAuto 0.6.9 UIA enumeration returned only the window and its title bar. REM therefore had no semantically identified controls on which to safely test UIA actions. The real semantic action test was stopped before any action. This is a real Windows failure, separate from passing mocked tests.

The worktree was already modified before this continuation. No pre-existing changes were reverted. `memory/routines.json` remains as it was at turn start; this file was not opened or written. No package or runtime was installed, and no stage, commit, or push was performed.

## Real Windows UIA diagnosis

The test app was compiled from `recovery/windows_acceptance/NativeUiaFixture.cs` with the machine's .NET Framework C# compiler and launched as a disposable, uniquely titled window. The Windows accessibility tree exposed:

- Search edit (`SearchBox`), button (`SearchButton`), result label.
- List and three list items, checkbox, combo box, two tab items, menu item.
- Focused-element state and accessible IDs.

In the same fixture, PyWinAuto 0.6.9 `UIAElementInfo(hwnd).children()` returned only a `TitleBar`; `Desktop(backend="uia").descendants()` returned that same single child. REM's `_scan_windows()` consequently returned one visible element (the window). A subsequent `look()` took the degraded/slow-window path under its 1.2-second scan budget. This gives the exact observed failure point: **REM's PyWinAuto UIA enumeration does not see this provider's client controls, despite the Windows accessibility tree seeing them.** The deeper cause of that tree-view mismatch still needs investigation; the fixture does not prove a general PyWinAuto failure for other apps.

`pywinauto` and `win32ui` import in the REM venv. Python is 3.12.10 x64. No MFC/runtime installation was attempted during this continuation, and no claim is made that installing one will resolve the control-tree mismatch. UIA initialization and top-level window identification succeeded; child-control enumeration through REM failed. No semantic action was attempted after the mismatch. The fixture was closed. The generated executable remains in the workspace because Windows denied deletion; the fixture source is retained for reproducibility.

Installed versions observed in the existing REM venv: PyWinAuto 0.6.9, PyWin32 312, comtypes 1.5.0, MSS 10.2.0, RapidOCR 3.10.0 (`rapidocr` package), ONNX Runtime 1.32.0, and Pillow 12.3.0. The older `rapidocr_onnxruntime` package name is absent. These were inspected only; nothing was installed. The full test run emitted PyWinAuto's `Revert to STA COM threading mode` warning, indicating COM was already initialized in a different apartment mode before PyWinAuto selected STA. This is a diagnostic lead for the tree mismatch, not proof that it caused the missing children.

## Code and tests changed in this continuation

- `core/screenview.py`: added a UIA `focus` operation using the fresh runtime-ID match and `UIAElementInfo.set_focus()`. It checks the current foreground PID/title, AutomationId, and emergency-stop state, and does not synthesize a click.
- `tests/test_visual_targeting_extensions.py`: added a mocked test for the fresh-identity UIA focus path.
- `main.py`: changed live-mic queue overflow to evict the oldest queued chunk and accept the latest one without blocking the callback; overflow counts remain rate-limited in UI logs.
- `tests/test_desktop_hardening_paths.py`: updated the bounded-queue test to verify replacement order, rate-limited logging, and that audio bytes are not logged.
- `recovery/windows_acceptance/NativeUiaFixture.cs`: added the disposable native test-window source.
- `recovery/PERSISTENT_DESKTOP_AND_MEMORY_REPORT.md`: appended the real Windows finding and follow-up validation.
- `recovery/REMAINING_WORK_COMPLETION_REPORT.md`: this report.

The generated `recovery/windows_acceptance/NativeUiaFixture.exe` is a locally compiled disposable test artifact. Attempts to delete it returned Windows `Access Denied`; it was not force-cleaned by another method. Its process was stopped.

## Validation results

- `& ..\.venv\Scripts\python.exe -m unittest tests.test_visual_targeting_extensions -q`: **17 passed**.
- `& ..\.venv\Scripts\python.exe -m unittest discover -s tests -q`: **101 passed**.
- `compileall` over `core`, `actions`, `plugins`, `main.py`, `gui`, `memory`, and `tests`: **passed**.
- `git diff --check`: **passed**; Git printed existing LF/CRLF notices.
- No external services, credentials, cloud screenshot transmission, or Gemini Live session were used in this continuation.

## Other real-component results (this continuation)

- **Screenshot capture:** not run on the disposable fixture in this continuation. Earlier acceptance reports document working MSS capture; no new desktop screenshot was taken here.
- **OCR:** not run on the fixture in this continuation. RapidOCR and ONNX Runtime are installed, but availability does not prove fixture text/box accuracy. The earlier fresh fixture run missed the expected labels; see the prior report.
- **Mouse and keyboard:** no physical input was exercised in this continuation. Prior mouse-component results are referenced in the existing mouse report; keyboard acceptance remains outstanding.
- **Chrome:** no browser was attached or controlled in this continuation. Prior YouTube selection evidence remains in `CHROME_VALIDATION_REPORT.md`; live recovery was not repeated.
- **Gemini Live:** no live credentialed session ran. The current suite verifies mocked dispatch/result paths only.

## Requirement matrix

| Requirement | Implemented | Unit tests | Mocked tests | Real Windows test | Final status |
|---|---|---|---|---|---|
| UIA discovery | Existing observer plus identity metadata | Existing suite | Yes | Top-level window found; REM scan did not enumerate fixture controls | **Blocked: backend/tree mismatch** |
| UIA child controls | Existing bounded traversal | Existing suite | Yes | UIA COM/PyWinAuto saw title bar only; .NET System.Windows.Automation saw 10 generic Pane children | **Failed for semantic UIA** |
| UIA semantic patterns | Invoke/select/toggle/expand paths; added UIA focus | Existing suite | Yes, including new focus test | No real pattern action, because REM could not resolve a child | **Partial; not accepted** |
| UIA events | No event subscription/cache lifecycle implementation | No | No | No | **Not implemented** |
| Persistent observation | Consent/settings/pause/resume/emergency disable from earlier phase | Existing suite | Yes | No disposable REM restart/lifecycle run | **Mocked only** |
| Consent persistence | Local saved setting from earlier phase | Existing suite | Yes | No real restart | **Mocked only** |
| Application exclusions | Existing classifier/settings | Existing suite | Yes | No excluded-app observation run | **Mocked only** |
| Mouse input | Existing implementation | Existing suite | Existing prior acceptance | Earlier disposable-window results in `MOUSE_CONTROL_REPORT.md`; not repeated here | **Prior component test only** |
| Keyboard input | Existing implementation | Existing suite | Yes | No app-level keyboard acceptance in this continuation | **Not validated here** |
| DPI calibration | Existing measured scaling path | Existing suite | Yes | No multi-monitor/DPI test | **Not validated here** |
| Multi-monitor support | Existing coordinate code | Existing suite | Yes | No multi-monitor test | **Not validated here** |
| Browser DOM/CDP | Existing opt-in loopback architecture | Existing suite | Yes | No fresh live browser run | **Prior disposable YouTube tests only** |
| Browser recovery | Closed-context reconnect path | Existing suite | Yes | No live disconnect/reconnect | **Mocked only this phase** |
| OCR | Optional RapidOCR path | Existing suite | Yes | Earlier OCR acceptance is documented in prior reports; latest fixture OCR missed labels | **Prior mixed result; not revalidated** |
| SQLite/memory decision | Existing memory remains JSON; no second DB created | Existing suite | Existing memory tests | No migration | **SQLite not justified without a migration request/design** |
| Memory persistence | Existing JSON memory manager | Existing suite | Yes | No restart acceptance this phase | **Mocked/current-path only** |
| Memory retrieval | Existing bounded local retrieval | Existing suite | Yes | No real retrieval session | **Not validated here** |
| Audio queue stability | Nonblocking drop-oldest policy, queue bounded, overflow counter and rate-limited diagnostic | Existing suite | Yes | No live audio saturation | **Mocked only** |
| RAM optimization | No new measurement or optimization | No | No | No process measurements | **Not implemented/validated** |
| Emergency stop | Existing authorization/stop code preserved | Existing suite | Yes | Earlier disposable mouse-stop test is in prior report; no new UIA test | **Regression suite passed; no new real test** |
| Gemini Live integration | Existing screen tool/image return path | Existing suite | Yes | No credentialed Live session here | **Mocked dispatch only this phase** |
| Voice-to-action workflow | Existing architecture retained | Existing suite | Partial | No live microphone-to-verified-action workflow | **Not completed** |

## Memory and dependency decision

The current persistent memory implementation is JSON-backed (`memory/long_term.json` and separate user routines data), not an existing SQLite store. This continuation did not create a parallel database or migrate any data. A migration would need its own data inventory, backup/rollback design, and user-approved acceptance before source JSON could be retired. No runtime or Python dependency was installed or changed.

## Remaining work

1. Diagnose why PyWinAuto 0.6.9's UIA control-view enumeration omits fixture controls visible in the Windows accessibility client. Compare UIA tree views, COM apartment/thread initialization, framework IDs, `FindAll` scopes, and a second native fixture/provider. Do not substitute OCR or coordinates when UIA supports the target.
2. After discovery works, validate real `SetFocus`, Value, Invoke, SelectionItem, Toggle, ExpandCollapse and stale-reference behavior in the fixture, verifying every postcondition.
3. Implement bounded UIA event subscriptions and teardown on a dedicated COM-initialized worker, then test focus, structure, property, open/close, and foreground events.
4. Exercise persistent observation consent, restart, pause/resume, exclusions, cloud-off and emergency-disable using a disposable REM configuration/session.
5. Repeat physical mouse and keyboard acceptance; test DPI/multiple monitors, held-input cleanup and emergency-stop behavior.
6. Revalidate disposable Chrome/CDP lifecycle, OCR labels/boxes, and local/cloud privacy behavior.
7. Audit existing memory and keep JSON unless a safe, non-duplicating migration is justified. Measure memory and audio queue behavior before claiming optimization.
8. Run credentialed Gemini Live and full voice-to-action only with an authorized session and disposable targets.

No completion claim is made for real UIA child discovery, semantic interaction, persistent restart lifecycle, UIA events, live browser recovery, full voice-to-action, or RAM optimization.

## Follow-up: instrumented UIA comparison (2026-11-25)

This section refines the 2026-11-24 note above. The Computer Use accessibility tree displayed semantic labels and roles, but that output alone is not proof that the Windows UIA COM provider exposes those same semantics.

### Exact path traced

1. core.screenview.look() calls _foreground(), which delegates on Windows to _foreground_windows().
2. _foreground_windows() calls GetForegroundWindow, gets the title with GetWindowTextW, and gets the owning PID with GetWindowThreadProcessId. The disposable fixture HWND/PID/title matched the independent window list.
3. look() selects the Windows UIA branch and calls _scan_windows(hwnd, scale, deadline). There is no separate Application.connect() call; the native HWND is passed directly to pywinauto.uia_element_info.UIAElementInfo(hwnd).
4. In PyWinAuto 0.6.9, UIAElementInfo.children() calls its underlying IUIAutomationElement.FindAll(TreeScope.Children, true_condition) path.
5. _scan_windows() breadth-first walks those children, then calls _element_from_uia() to convert visible nodes into Element.
6. The existing conversion filter discards unnamed/unvalued ROLE_OTHER or ROLE_GROUP descendants. It did not discard the missing edit/button/list/checkbox/combo/tab/menu controls: they were absent from PyWinAuto's child list before conversion.
7. look() therefore receives only the top-level window Element; the title bar is the one raw child seen by PyWinAuto and is discarded by the conversion filter.

### Same-window measurements

Fixture: NativeUiaFixture.exe, unique title REM UIA Disposable Acceptance 2026-11-24.

| Inspector/path | HWND / PID | Root | Raw children/descendants | Types/patterns |
|---|---|---|---:|---|
| PyWinAuto 0.6.9 UIAElementInfo(hwnd).children() | 0x180d16 / 16768 on the captured run | Window; correct title and runtime ID | 1 | TitleBar only |
| Direct comtypes CUIAutomation + FindAll(TreeScope.Descendants) | Same HWND/PID | Same title/runtime ID | 1 | TitleBar only |
| REM _scan_windows() with diagnostic logging | Same HWND/PID | Same root | 2 nodes visited; 1 returned; 1 filtered | Window + TitleBar; no client controls |
| .NET System.Windows.Automation FromHandle().FindAll(Descendants, TrueCondition) | Same HWND/PID | Window; same root runtime ID | 11 total, including titlebar | Ten client HWNDs reported as generic ControlType.Pane; no Value, Invoke, Toggle, SelectionItem, ExpandCollapse, or LegacyIAccessible pattern was supported |
| Computer Use accessibility tree | Same named disposable window | Window | Exposed all fixture widgets | Shows semantic roles, but is not accepted as proof of the direct UIA COM surface |

The .NET nodes' class names corresponded to the fixture's WinForms HWND classes: WindowsForms10.STATIC, EDIT, BUTTON, LISTBOX, COMBOBOX, SysTabControl32, and Window.8. Their reported AutomationIds were numeric HWND-like values, not the control names. The diagnostic probe emitted no control values.

### Root-cause conclusion and limits

The failure is before REM's element conversion/filtering. PyWinAuto's selected CUIAutomation control-tree path and a separate comtypes call both return only the title bar for this HWND. Direct .NET UI Automation finds the client HWNDs, but only as generic panes without the required semantic patterns. That is the precise failure observed; it is not a win32ui import or missing-MFC-runtime error, and it is not caused by REM's no-name filter.

The underlying Windows/provider reason for the API discrepancy remains unconfirmed. The fixture sidecar config enabled Switch.UseLegacyAccessibilityFeatures, .2, and .3 as false; a fixture-local runtime probe confirmed those values, and the installed .NET Framework release was 533509 (4.8.1). This did not change the observed UIA types/patterns. Microsoft documents the WinForms accessibility switches and the 4.7.1+ accessibility changes, but the measured behavior persisted with the switches enabled. No dependency change is justified by this evidence. See [Microsoft's .NET Framework WinForms accessibility guidance](https://learn.microsoft.com/en-us/dotnet/framework/migration-guide/retargeting/4.7.x#accessibility-improvements-in-windows-forms-controls) and [UI Automation's client-side provider model](https://learn.microsoft.com/en-us/dotnet/framework/ui-automation/client-side-ui-automation-provider-implementation).

Because the direct UIA route also failed the semantic-role/pattern check, testing stopped here as requested. No SetFocus, Value.SetValue, Invoke, click, or typing was issued. No alternate Win32 action was represented as UIA success.

### Diagnostic logging and changed files

core/screenview.py now supports opt-in structured diagnostics with REM_UIA_DIAGNOSTICS=1. It records the HWND, process/window metadata, selected backend, root metadata, visited raw node count, discovered control types/names/AutomationIds/runtime IDs, returned and filtered counts, and exception classes. It never includes UIA values or screenshots. Diagnostics are silent by default; titles and names may still reveal screen context, so the switch must only be enabled for a controlled local test.

Files changed for this follow-up:

- core/screenview.py
- tests/test_visual_targeting_extensions.py
- recovery/windows_acceptance/NativeUiaFixture.cs
- recovery/windows_acceptance/NativeUiaFixture.exe.config
- recovery/windows_acceptance/NativeUiaFixture.runtime.txt
- recovery/windows_acceptance/UIADirectProbe.cs
- recovery/windows_acceptance/UIADirectProbe.exe
- recovery/UIA_ENUMERATION_ROOT_CAUSE_REPORT.md
- recovery/REMAINING_WORK_COMPLETION_REPORT.md
- recovery/PERSISTENT_DESKTOP_AND_MEMORY_REPORT.md

The fixture was reopened with the config and the same test was repeated. A later UI-input signal left the fixture minimized; it was not force-focused or closed. This is the disposable test app, not a user document. No packages or runtimes were installed and memory/routines.json was not touched.

### Current validation

- Targeted tests.test_visual_targeting_extensions: 19 passed.
- Full python -m unittest discover -s tests -q: 103 passed.
- compileall, py_compile of the touched Python modules, and git diff --check: passed (Git reports the existing LF/CRLF notices).
- Real UIA semantic acceptance: failed/not attempted, because client controls/patterns were not available through REM's UIA path.
- Real stale-element, window-close, UIA event, semantic focus/value/invoke, and full acceptance sequence: not run.

## Accessibility Insights follow-up (2026-11-25)

The user supplied Accessibility Insights evidence for the exact fixture: semantic Search field Edit with writable ValuePattern, Run test search Button with InvokePattern, and semantic list, checkbox, combo box, tab, and menu. This confirms that another accessibility inspection route can expose the controls; the prior wording should not be read as “the controls are inaccessible.”

A direct .NET `System.Windows.Automation` comparison was run against the exact uniquely titled disposable process (HWND `0x2805f6`, PID `26936`). `FromHandle(hwnd)` and desktop `RootElement.FindFirst(TreeScope.Children, exact title)` returned the same root. Children scope, Descendants scope, ControlView, RawView, and ContentView each exposed the client HWNDs as generic Pane elements. Calling `FromHandle` on the EDIT and BUTTON child HWNDs returned those same Pane identities, with ValuePattern and InvokePattern unavailable. The source remains narrowed to a difference in UIA client/provider behavior, not REM control filtering. The exact reason for the difference from Accessibility Insights remains unconfirmed.

FromPoint was attempted on the reported child rectangles but was inconclusive because this fixture instance was minimized/off-screen at roughly (-40000,-40000); FromPoint returned the desktop root. A restore attempt did not return it to the work area. No Value.SetValue, focus, Invoke, mouse, or keyboard action was performed. The required semantic acceptance steps (including status and list postconditions) remain **not passed**. This is actual Windows UIA probing, not mocked testing; the user-provided Accessibility Insights observations are separately recorded as independent evidence.

No runtime or dependency was changed, and no semantic backend was blindly replaced. `memory/routines.json`, authorization, emergency stop, observation settings, plugins, and safety controls were left untouched. The updated direct probe is `recovery/windows_acceptance/UIADirectProbe.cs` plus its compiled executable. No tests were run because this follow-up changed only a disposable diagnostic probe and reports; earlier test counts in this report belong to the earlier run and are not new validation of this probe.

The Computer Use accessibility adapter also exposed the fresh fixture as semantic edit/button/list/checkbox/combo/tab/menu controls, consistent with the user-provided Accessibility Insights evidence. The .NET UIA probe, scoped to the fresh instance by its exact HWND and then matching desktop-root results by title plus PID, still exposed generic Pane controls without Value or Invoke patterns. The fixture window remained minimized at the OS level; its rectangles were at the minimized sentinel and FromPoint returned the desktop root. This is an additional real observation, but it does not isolate the exact provider/client difference. No action was issued.

## Final provider differential pass (2026-11-25)

### Verified facts

- The named disposable WinForms fixture was restored through Computer Use and was visibly on-screen. The inspected instance was HWND `0x6f06f2`, PID `26584`; `GetWindowRect` returned `(388,122)-(1148,742)`.
- The direct .NET `System.Windows.Automation` probe used an STA thread. FromHandle and a desktop root query matched by exact title+PID returned the same root (RuntimeId `[42,7276274]`, FrameworkId `WinForm`, `IsOffscreen=false`); each had 10 direct children.
- Direct UIA2 returned the SearchBox native child HWND `0x2c0986` as ControlType.Pane with empty name, RuntimeId `[42,2886022]`, bounds `(525,256,450,25)`, and no Value/Invoke/LegacyIAccessible patterns. It returned the Run test search HWND `0x1409e6` as ControlType.Pane, RuntimeId `[42,1313254]`, bounds `(995,254,188,40)`, and no Value/Invoke/LegacyIAccessible patterns.
- Visible-window FromPoint calls at the measured Edit center `(750,268.5)` and button center `(1089,274)` both failed with `Win32Exception: Access is denied`.
- Official Microsoft source shows Accessibility Insights imports `Axe.Windows.Desktop.UIAutomation`; Axe.Windows creates CUIAutomation8 on post-Windows-7 systems and enumerates by desktop-root ControlViewWalker, matching child/grandchild ProcessId. See [Accessibility Insights hierarchy source](https://github.com/microsoft/accessibility-insights-windows/blob/main/src/AccessibilityInsights.SharedUx/Controls/HierarchyControl.xaml.cs) and [Axe.Windows A11yAutomation source](https://github.com/microsoft/axe-windows/blob/main/src/Desktop/UIAutomation/A11yAutomation.cs).
- Direct CUIAutomation8 `ElementFromHandle().FindAll(Children/Descendants)` returned zero; legacy CUIAutomation returned only titlebar. My Python attempt to reproduce Axe.Windows' exact desktop-root walker route failed on `GetFirstChildElement(desktopRoot)` with E_POINTER before walking children. Accessibility Insights itself failed to launch via Computer Use in this run; no live identity comparison was available.
- A separate IAccessible call returned an object for both child HWNDs and the button name, but the attempted IUIAutomation `ElementFromIAccessible` bridge failed with E_INVALIDARG. This does not prove that Accessibility Insights uses MSAA.

### Hypothesis

The strongest source-based explanation is that Accessibility Insights uses a different UIA client object version and traversal path (CUIAutomation8 + desktop-root ControlView process scan) than REM's PyWinAuto route or the managed .NET direct-HWND probe. The evidence establishes this implementation difference, but has not yet shown that it alone explains the semantic-role discrepancy on this same live process.

### Unresolved

The exact runtime reason the CUIAutomation8 desktop walker or Accessibility Insights produces semantic Edit/Button roles while managed .NET UIA returns Pane; why visible FromPoint returns Access Denied; whether the accessibility adapter's semantic tree corresponds to the same provider element identities; and whether MSAA/IAccessibleEx participates below Axe.Windows' UIA wrapper.

### Acceptance and changes

The fixture state was visible and valid during the UIA2 FromPoint repeat, but the element patterns remained unavailable. No Value.SetValue, read-back, focus, Invoke, status/list postcondition, click, or typing was attempted. **Semantic acceptance did not pass and is not claimed.** No active REM implementation, plugins, authorization, emergency stop, observation settings, or memory was modified; `memory/routines.json` was not touched. The diagnostic probe compiled and ran. No project test suite was run because changes were limited to the disposable diagnostic probe and reports. No dependencies were installed; no staging, commit, or push occurred.


### Additional CUIAutomation8 live result

On the same visible PID 26584 fixture, CUIAutomation8 `GetRootElement()` returned a Desktop root, but Children and Descendants `FindAll(TrueCondition)` returned zero; ControlView and RawView walkers returned no first child. Direct CUIAutomation8 `ElementFromHandle(fixtureHwnd)` likewise returned no children/descendants. The managed .NET route still found ten direct children. Thus the official Axe.Windows source path is established, but that path also did not reproduce semantic controls from this probe environment. Remaining possibilities such as Accessibility Insights' actual UIAccess/token/desktop context are unverified hypotheses; its process could not be launched to inspect those properties. Do not treat UIAccess as the confirmed cause.

## UIA context / Axe.Windows investigation update (2026-11-25)

### Current same-session observations

- Launched a fresh disposable `NativeUiaFixture` window (desktop window ID `4851910`, hex `0x4a00e6`). Its live Computer Use accessibility tree exposed the semantic Search field, Run test search button, results list, checkbox, combo, tabs, and menu.
- Launched Accessibility Insights in the same desktop session. It is open in Live Inspect, but no fixture element is selected; the property/pattern pane is empty. Thus a same-run element identity and pattern comparison was **not captured**.
- The desktop session has four same-title fixture windows; earlier probe identity `0x6f06f2` is not the newly launched `0x4a00e6` instance.
- Current shell execution is isolated from the interactive desktop: process enumeration saw only shell host processes, and CIM process querying was denied. User/token integrity/elevation/UIAccess/architecture/session and per-process COM/UIA initialization were not measurable for REM, Accessibility Insights, or the fixture. No elevation/security setting was changed.

### Source and diagnostic status

- Official Axe.Windows source confirms the implementation sequence: instantiate CUIAutomation8 on supported Windows, call `GetRootElement()`, obtain the ControlView walker, inspect children and then grandchildren for matching PID; its HWND overload first calls `ElementFromHandle`, includes that root, and applies the same process matching. The source's `GetRootElement()` lock is a workaround for a previously observed UIA issue, not proof of this failure's cause.
- Earlier direct CUIAutomation8 probes returned zero desktop children and zero fixture-handle descendants; earlier .NET FromPoint calls failed with `Access is denied`. The old diagnostics did not preserve numeric HRESULT, timestamps, thread IDs, and apartment state per call. These omissions cannot be backfilled. Fresh exact per-call logs against the new fixture are still outstanding.

### Outcome

The source-level route difference is confirmed, but the root cause is not. Process security context, COM/UIA initialization differences, provider behavior, and actual Axe.Windows runtime behavior remain hypotheses because the three processes' contexts and same-target identities were not captured. No semantic patterns were obtained through the direct probe in this continuation, so there was no SetValue, read-back, Invoke, status/list check, click, or typing. **Real semantic acceptance remains blocked/not passed.** No active REM architecture or memory data was changed; no tests were run in this documentation-only continuation. No dependency was installed and no Git staging, commit, or push occurred.

## UIA adapter integration update (2026-11-25)

The observed discrepancy is explained for the diagnostic runs: the earlier failing probe was a Low-integrity AppContainer, while the successful CUIAutomation8 probe ran as the interactive user at Medium integrity outside an AppContainer. The low process received access denied when opening normal desktop processes, returned no desktop descendants, and got `0x80070005` from FromPoint. In the medium process, the Axe.Windows-style CUIAutomation8 desktop ControlView/PID path found the same fixture's semantic Edit and Button and their real ValuePattern/InvokePattern. This does not measure a separate REM process token and must not be generalized to every launch configuration. MFC is not supported as the cause of this discrepancy.

Implemented the separate `core/windows_uia.py` adapter and connected the existing Windows `screenview` enumeration and `perform_uia_pattern` paths to it, retaining the PyWinAuto import fallback and existing desktop stop checks. The adapter verifies process/window identity, traverses ControlView, re-resolves stale runtime IDs before semantic operations, supports available patterns, and reports HRESULTs rather than converting failures into successful empty results.

Real Windows test on the disposable WinForms fixture: discovered Search field as Edit (50004), retrieved writable ValuePattern, set and read back `REM_UIA_TEST_123`, found Run test search as Button (50000), retrieved InvokePattern and invoked it. Visual inspection confirmed the status changed to `Search completed: REM_UIA_TEST_123`. The fixture's button handler does not update its results list, so Alpha/Bravo/Charlie remained unchanged. The required list postcondition failed; overall semantic acceptance is therefore **partial, not passed**. No coordinate clicks or keyboard typing were used. The fixture input was reset through ValuePattern.

Mocked validation: 106 project tests passed, including 3 new adapter tests; Python compilation passed; `git diff --check` passed. An initial run found one existing test still mocking the replaced PyWinAuto focus path; it was updated to mock the adapter seam before the passing run. The actual live CUIAutomation8 semantic probe succeeded, but the exact newly integrated REM adapter was not independently driven in a normal Medium-integrity REM process. Checkbox/combo/list/tab semantic actions also remain untested. Do not treat these as completed real acceptance.

Changed this pass: `core/windows_uia.py`, `core/screenview.py`, `tests/test_windows_uia_adapter.py`, and `tests/test_visual_targeting_extensions.py`, plus this report. No installation, staging, commit, or push. `memory/routines.json` was not intentionally modified in this pass; it was already dirty in the shared worktree and remains preserved.

## Integrated UIA milestone and broader completion status (2026-11-25)

**Integrated real Windows UIA acceptance passed on the exact disposable WinForms fixture**, using REM's `screenview._scan_windows()` and `screenview.perform_uia_pattern()` entry points and the integrated CUIAutomation8 adapter. The acceptance process context was Python 3.12.10 x64 `pythonw.exe`, `FIRENEXUS\tarun`, Medium integrity, not elevated, UIAccess false, AppContainer false, session 2. The fixture was PID 9884 / HWND `0xD1089C`. COM was initialized STA by the runner; REM's adapter initialized a dedicated MTA worker and created CUIAutomation8.

It discovered 25 elements and uniquely matched Search field (Edit/50004, `SearchBox`) and Run test search (Button/50000, `SearchButton`). ValuePattern was available and writable; `REM_INTEGRATED_UIA_TEST` was set and read back exactly both inside the adapter and on a fresh scan. InvokePattern was available and the button was invoked. A live screenshot of the fixture showed the exact status `Search completed: REM_INTEGRATED_UIA_TEST`.

The same integrated action path also toggled and restored the checkbox, expanded/collapsed the combo box, and selected the Alpha list item. The UI afterward verified those fixture states. Tab children exposed SelectionItemPattern but had no RuntimeId, so identity-safe REM actions refused to target them. No WindowPattern was exposed by the scanned root. Combo SelectionPattern was not exposed. The list container's SelectionPattern is query-only for this API and was not treated as a selection action.

This verifies the integrated adapter in a normal desktop security context, but the test was a `pythonw` acceptance process importing REM's real modules. The normal `main.py` app was not launched, so its own token/context, Gemini Live tool dispatch, actual speech, and voice-to-UIA path are still not tested. The previous low-integrity AppContainer cause applies to the diagnostic discrepancy; don't use it to infer the running app's context.

A semantic Invoke error now stops without a physical-click retry, and ValuePattern SetValue checks exact readback. Full offline suite: **107 passed**. Compilation passed; `git diff --check` passed (with repository line-ending notices only). Existing unrelated modified files and `memory/routines.json` were preserved. No dependency install, staging, commit, or push was performed.
