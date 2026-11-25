# REM Windows UIA Enumeration — Root-Cause Report

Date: 2026-11-25  
Project: C:\important\Mark-LVII-main\Mark-LVII-main

## Finding

The confirmed REM failure occurs in UIA child enumeration, before REM's element conversion and filtering:

1. Windows foreground discovery returns the disposable fixture HWND, PID, and title.
2. REM passes that HWND to PyWinAuto 0.6.9 as UIAElementInfo(hwnd).
3. UIAElementInfo.children() returns one child: TitleBar.
4. A separate direct COM call using comtypes CUIAutomation and FindAll(TreeScope.Descendants) returns the same one TitleBar descendant.
5. REM visits the Window and TitleBar nodes. The Window is retained; the unnamed TitleBar is discarded by the existing role/name filter. No edit, button, list, checkbox, combo, tab, or menu node reaches _element_from_uia().

This rules out a wrong foreground HWND/PID, a conversion bug for those controls, and the existing unnamed-element filter as the cause. The filter explains why the final REM view has only the top-level Window; it does not explain why the client controls are absent from PyWinAuto's returned child tree.

The exact Windows/provider-level reason for the difference between UIA clients remains unconfirmed. The evidence points to a mismatch between PyWinAuto/comtypes' CUIAutomation control-tree result and the managed .NET UI Automation view for this WinForms fixture. The direct .NET client sees the child HWNDs, but reports them only as generic Pane elements without usable semantic patterns. The Computer Use tree showed richer semantic roles, but its output does not establish what the direct Windows UIA COM client exposes.

No MFC or PyWin32 installation issue was found in this path: win32ui imports in the REM venv, and PyWinAuto initializes and identifies the top-level Window. No dependency/runtime changes were made.

## Enumeration trace and instrumentation

The complete REM path is:

look() → _foreground() → _foreground_windows() → Windows GetForegroundWindow / GetWindowTextW / GetWindowThreadProcessId → _scan_windows(hwnd, scale, deadline) → UIAElementInfo(hwnd) → children() breadth-first traversal → _element_from_uia() → role/name/value filter → final View.elements.

There is no separate Application.connect() call or backend-selection registry in this path. REM explicitly chooses the Windows UIA branch and passes the foreground HWND to PyWinAuto's UIA element wrapper.

Opt-in structured logging was added to core/screenview.py. Set REM_UIA_DIAGNOSTICS=1 only for a controlled local test. The emitted events record:

- foreground HWND, process name/PID, window title, and selected backend;
- root name, control type, AutomationId, PID, and runtime ID;
- raw nodes visited, discovered types/names/IDs/runtime IDs, returned count, filtered count, child enumeration error classes, and timeouts.

The logger does not include UIA values, password values, or screenshots. It is silent by default. Window titles and accessible names can contain screen context, so the opt-in setting should be limited to disposable/local diagnostics.

Observed diagnostic for the fixture: root_connected reported HWND 0x180d16, PID 16768, type Window, the exact fixture title, and runtime ID [42,1576214]. enumeration_complete reported two nodes visited, one returned, one filtered, and types TitleBar and Window.

## Independent Windows UI Automation comparison

The probe used the .NET System.Windows.Automation API on the same unique fixture title and HWND. It did not inspect or act on other windows and never read control values.

| Path | Root identity | Returned children | Semantic patterns |
|---|---|---|---|
| PyWinAuto UIAElementInfo.children() | Correct Window/title/runtime ID | One TitleBar | None |
| comtypes CUIAutomation FindAll(Descendants) | Same HWND/PID/title/runtime ID | One TitleBar | None |
| .NET AutomationElement.FromHandle().FindAll(Descendants, TrueCondition) | Same HWND/PID/title/runtime ID | Ten client nodes plus TitleBar | Client nodes are ControlType.Pane; Value, Invoke, Toggle, SelectionItem, ExpandCollapse, and LegacyIAccessible queries returned unsupported |
| Computer Use accessibility tree | Same disposable title | Shows edit, button, list, checkbox, combo, tabs, menu | Not treated as direct UIA COM proof |

The .NET nodes' class names identified fixture WinForms child HWNDs: WindowsForms10.EDIT, .BUTTON, .LISTBOX, .COMBOBOX, .SysTabControl32, and related classes. Their reported AutomationIds were HWND-like numeric values, not stable control names.

The fixture sidecar config set Switch.UseLegacyAccessibilityFeatures, .2, and .3 to false. A fixture-local runtime probe confirmed those values; the installed .NET Framework release key was 533509 (4.8.1). This did not change the observed UIA types/patterns. Microsoft documents the WinForms accessibility switches and client-side provider model in [WinForms accessibility guidance](https://learn.microsoft.com/en-us/dotnet/framework/migration-guide/retargeting/4.7.x#accessibility-improvements-in-windows-forms-controls) and [UI Automation client-side providers](https://learn.microsoft.com/en-us/dotnet/framework/ui-automation/client-side-ui-automation-provider-implementation).

## Semantic action and event results

No semantic action was issued. REM could not resolve a real child element with the correct semantic identity; the direct .NET UIA result did not support the required patterns. Therefore:

- Real UIA search-field discovery through REM: failed.
- Real UIA button/list/checkbox/combo/tab/menu discovery through REM: failed.
- Real UIA SetFocus, Value.SetValue/read-back, Invoke/postcondition: not attempted.
- Stale-element re-resolution and window-close behavior: not tested.
- UIA focus, structure, property, open/close event subscriptions: not implemented/tested.
- Mouse or coordinate fallback: not used.

Mocked SetFocus coverage remains a unit test and is not real acceptance.

## Files changed for this investigation

- core/screenview.py — opt-in structured UIA diagnostics.
- tests/test_visual_targeting_extensions.py — tests for diagnostic opt-in/privacy filtering.
- recovery/windows_acceptance/NativeUiaFixture.cs — disposable WinForms fixture source and local runtime/switch report.
- recovery/windows_acceptance/NativeUiaFixture.exe.config — fixture-only accessibility switches.
- recovery/windows_acceptance/NativeUiaFixture.runtime.txt — fixture runtime/switch values only.
- recovery/windows_acceptance/NativeUiaFixture.exe — locally compiled disposable fixture.
- recovery/windows_acceptance/UIADirectProbe.cs and .exe — direct .NET UIA probe and compiled binary.
- recovery/REMAINING_WORK_COMPLETION_REPORT.md.
- recovery/PERSISTENT_DESKTOP_AND_MEMORY_REPORT.md.

The fixture was left minimized after a Windows UI input signal; it was not force-focused or closed after that signal. It contains synthetic controls and no user document. No dependencies or system runtimes were installed. memory/routines.json was not touched. No changes were staged, committed, or pushed.

## Validation

- python -m unittest tests.test_visual_targeting_extensions -q: 19 passed.
- python -m unittest discover -s tests -q: 103 passed.
- compileall and py_compile of touched Python modules: passed.
- git diff --check: passed, with existing LF/CRLF notices.
- Actual Windows UIA enumeration: failed for REM's semantic child tree, measured above.
- No Gemini Live session, browser action, screenshot, OCR, click, or text input was performed.

The direct UIA result did not satisfy the acceptance criteria. No fallback action was represented as UIA success; the backend/provider mismatch is documented rather than claimed fixed.

## Follow-up: Accessibility Insights challenge and query-method comparison (2026-11-25)

The user supplied independent Accessibility Insights findings for the disposable fixture: `edit 'Search field'` with ValuePattern available and writable; `button 'Run test search'` with InvokePattern available; and semantic list, checkbox, combo box, tab, and menu entries. This is accepted as evidence that a client can expose the fixture semantically. It supersedes any interpretation that the controls themselves are inaccessible. It does not by itself identify which UIA client route or fixture state produced the difference.

A fresh run was scoped to the uniquely titled `NativeUiaFixture.exe` window only (HWND `0x2805f6`, PID `26936`; title `REM UIA Disposable Acceptance 2026-11-24`). The standalone .NET probe was expanded to compare:

- `AutomationElement.FromHandle(hwnd)`.
- `AutomationElement.RootElement.FindFirst(TreeScope.Children, Name == exact fixture title)`.
- `TreeScope.Children` and `TreeScope.Descendants` under the handle root.
- `TreeWalker.ControlViewWalker`, `RawViewWalker`, and `ContentViewWalker`.
- `AutomationElement.FromHandle(child.NativeWindowHandle)` for the discovered native EDIT, BUTTON, and LISTBOX HWNDs.
- `AutomationElement.FromPoint` at each such child’s reported bounding-box center.

The handle and desktop-root routes returned the same window identity (same HWND/PID/runtime ID) and each reported 9 direct children. Each tree view exposed the same 9 child nodes. The EDIT, BUTTON, LISTBOX, checkbox-button, and other client HWNDs were all `ControlType.Pane`; `TryGetCurrentPattern(ValuePattern.Pattern)` and `TryGetCurrentPattern(InvokePattern.Pattern)` were false on the EDIT/BUTTON, including when reopened directly by child HWND. Thus a wrong root, a Children-versus-Descendants scope choice, a Control-versus-Raw-versus-Content view, or REM’s conversion/filtering is not where these direct .NET observations lose the semantic roles. The `FromHandle(child HWND)` route agrees with traversal and is also generic Pane.

`FromPoint` could not provide a meaningful comparison in this run: the window was minimized/off-screen, its native rectangle was approximately `(-40000,-40000,-39751,-39957)`, and child rectangles were also negative. `FromPoint` consequently resolved to a desktop `#32769` element owned by another process, not the fixture. The probe did not treat that as evidence about the visible-window provider response. A WindowPattern restore attempt did not bring this window into the desktop work area. The live UIA call ran from a standalone .NET Framework console program marked `[STAThread]`; a separate COM-thread/apartment experiment was not performed in this follow-up.

No semantic action was attempted: there is no currently discovered writable ValuePattern or InvokePattern in the direct API output. No focus, value setting/read-back, invoke, mouse action, or keyboard action occurred. Therefore the required 12-step real acceptance sequence remains **not passed**. Specifically, we cannot honestly claim that REM discovered an Edit/Button or verified the status/list update. The supplied Accessibility Insights output and this direct .NET probe conflict at the client/provider observation boundary. The exact reason for that conflict remains unconfirmed; candidates requiring a visible-window capture include differences in provider route/client behavior or fixture state. No dependency changes or random backend replacement were made.

The isolated probe records only element metadata and boolean pattern availability; it does not read values, log credentials, or modify user documents. The only source changes for this follow-up are this probe and the two requested recovery reports. No changes were made to `core/screenview.py`, plugins, memory, configuration, or safety controls. `memory/routines.json` was not touched.

### Acceptance status

- Exact named disposable window and PID matched: **yes**.
- Desktop root and FromHandle root identity match: **yes**.
- Child scope and three tree views compared: **yes; all generic Pane children**.
- Child HWND direct `FromHandle` compared: **yes; same Pane semantics**.
- FromPoint at bounds center: **attempted; inconclusive because the target was minimized/off-screen**.
- Edit ControlType.Edit / writable ValuePattern: **not exposed by this .NET client run**.
- Button ControlType.Button / InvokePattern: **not exposed by this .NET client run**.
- SetValue/read-back, Invoke, status change, results-list update: **not run**.
- Real acceptance: **failed/blocked; not claimed**.

### Additional provider evidence from the desktop accessibility adapter

The desktop app inventory showed two windows with the same fixture title: the original minimized instance (HWND 0x2805f6/PID 26936) and a fresh instance (HWND 0x6f06f2/PID 26584). The probe was updated to select the second by exact HWND and the desktop-root query now matches both exact title and PID, avoiding ambiguity from duplicate titles. Both root routes returned the fresh root identity and nine direct children. Its direct UIA observations remain generic Pane elements with no Value/Invoke support.

The desktop Computer Use accessibility adapter, when asked to bind the fresh fixture, independently displayed the semantic tree (SearchBox as edit, SearchButton as button, ResultList and its items, checkbox, combo box, tabs, menu bar). This agrees with the user-provided Accessibility Insights results that some inspection client sees the intended controls. However, the adapter then reported the window minimized when queried for current state or an action, and the native window rectangle remained at the Windows minimized sentinel (`-32000,-32000`); descendants' reported bounds were around `-40000`. No input action was made. This further isolates the unresolved issue to differing client/provider state or route, but still does not establish the exact root cause or make a semantic action safe through the direct .NET API.

The probe calls were `[STAThread]` `AutomationElement.FromHandle`, desktop `AutomationElement.RootElement.FindFirst(TreeScope.Children, AndCondition(NameProperty, ProcessIdProperty))`, `FindAll(TreeScope.Children/Descendants, Condition.TrueCondition)`, the Control/Raw/Content `TreeWalker`s, `AutomationElement.FromHandle(childHwnd)`, and `AutomationElement.FromPoint(centerOfBoundingRectangle)`. The tree queries are all on the same STA thread. FromPoint returned desktop `#32769`, so it is explicitly inconclusive. No alternate apartment comparison was performed. The probe recompiled and ran successfully against the selected fresh fixture; real SetValue/Invoke acceptance is still blocked and unclaimed.


## Final differential pass (2026-11-25): live fixture, official client source, and remaining gap

### Verified live test state

The previously minimized fixture was restored through the documented Computer Use `get_window` + `activate_window` flow. A fresh screenshot showed the fixture and all controls on screen; the search field had focus. The selected fixture was HWND `0x6f06f2`, PID `26584`, title `REM UIA Disposable Acceptance 2026-11-24`. `GetWindowRect` returned `(388,122)-(1148,742)`. This is a different process instance from the previously minimized fixture (HWND `0x2805f6`, PID `26936`); the probe now selects the visible instance by exact HWND and matches the desktop root by exact title plus PID.

### Verified direct API results on the visible fixture

The .NET Framework `System.Windows.Automation` probe (`[STAThread]`) used `AutomationElement.FromHandle(hwnd)`, `AutomationElement.RootElement.FindFirst(TreeScope.Children, AndCondition(NameProperty, ProcessIdProperty))`, `FindAll` with Children/Descendants scopes, Control/Raw/Content TreeWalkers, and child-HWND `AutomationElement.FromHandle`. Both root routes returned HWND `0x6f06f2`, PID `26584`, RuntimeId `[42,7276274]`, FrameworkId `WinForm`; root bounds were `(485,153,950,775)`, `IsOffscreen=false`, and each root had 10 direct children.

The Edit-like child was ControlType.Pane, empty Name, AutomationId `2886022`, class `WindowsForms10.EDIT.app.0.34f5582_r6_ad1`, HWND `0x2c0986`, PID `26584`, RuntimeId `[42,2886022]`, FrameworkId `WinForm`, bounds `(525,256,450,25)`, `IsOffscreen=false`. Value, Invoke, and LegacyIAccessible pattern availability all reported false.

The search button child was ControlType.Pane, Name `Run test search`, AutomationId `1313254`, class `WindowsForms10.BUTTON.app.0.34f5582_r6_ad1`, HWND `0x1409e6`, PID `26584`, RuntimeId `[42,1313254]`, FrameworkId `WinForm`, bounds `(995,254,188,40)`, `IsOffscreen=false`. Value, Invoke, and LegacyIAccessible pattern availability all reported false.

ProviderDescription was not returned by the .NET Framework property table (`AutomationProperty.LookupById(30107)` did not resolve it), so it is recorded as unavailable in this client. No control value was read. These are measurements from the same visible fixture process, not mocked data.

For the visible pass, `AutomationElement.FromPoint` was called at the centers computed from the UIA bounding rectangles: Edit `(750,268.5)` and button `(1089,274)`. Both calls failed with `System.ComponentModel.Win32Exception: Access is denied`; no element was returned. This replaces the earlier minimized-window FromPoint attempt, which was inconclusive. The coordinates used here came from the visible controls' UIA bounding rectangles.

A separate, read-only MSAA probe called `AccessibleObjectFromWindow(childHwnd, OBJID_CLIENT, IID_IAccessible)`. It returned an IAccessible object for both child HWNDs; the button's `accName(0)` was `Run test search`, while the edit's was null in this call. `accRole(0)` returned integer `10` for each (the raw result was not a semantic role label). Attempts to bridge those objects with `IUIAutomation.ElementFromIAccessible` returned `E_INVALIDARG`. This did not reproduce Accessibility Insights' reported UIA `LegacyIAccessiblePattern`, and does not establish MSAA as its route.

### Verified Accessibility Insights implementation route from official source

The public Microsoft `accessibility-insights-windows` source imports `Axe.Windows.Desktop.UIAutomation` in its hierarchy implementation ([HierarchyControl.xaml.cs](https://github.com/microsoft/accessibility-insights-windows/blob/main/src/AccessibilityInsights.SharedUx/Controls/HierarchyControl.xaml.cs)). Its `Axe.Windows` `A11yAutomation` source explicitly wraps `CUIAutomation`, selects `CUIAutomation8` on Windows versions after Windows 7, and discovers process elements by starting at `GetRootElement()` (the desktop), using a ControlView tree walker, and selecting child/grandchild elements whose ProcessId matches ([A11yAutomation.cs](https://github.com/microsoft/axe-windows/blob/main/src/Desktop/UIAutomation/A11yAutomation.cs)). That source establishes a standard UI Automation COM client implemented through the Axe.Windows wrapper. It does not show a separate MSAA-only or custom desktop adapter as the main element discovery route. It also establishes a material difference from REM's PyWinAuto/previous probe route: client object version (`CUIAutomation8` vs legacy `CUIAutomation`/managed UIA2) and traversal root/algorithm (desktop process-ID ControlView walk vs direct fixture-HWND child/descendant query).

A controlled Python COM attempt instantiated CUIAutomation8 and queried `ElementFromHandle(hwnd).FindAll(Children/Descendants, TrueCondition)`; it returned 0 children on this fixture. The legacy CUIAutomation COM probe returned only the title bar. My attempt to reproduce Axe.Windows' exact desktop-root ControlView traversal from Python failed at `TreeWalker.GetFirstChildElement(desktopRoot)` with `COMError: E_POINTER` before enumerating children. This is an instrumentation failure, not evidence that the Axe.Windows route fails. Accessibility Insights itself could not be launched during this turn: the desktop launch request stalled and its computer-use continuation expired before the application returned a window. No same-run Accessibility Insights RuntimeId/HWND/FrameworkId/provider description was therefore captured. The user-supplied prior semantic roles/pattern findings remain independent evidence, not a fresh identity comparison.

### Facts, hypotheses, and unresolved questions

**Verified facts:** the live fixture was restored and visibly on-screen; managed .NET UIA roots matched by HWND and by desktop title+PID; the managed .NET UIA child nodes remained generic Pane elements without the requested patterns; visible FromPoint returned Access Denied for both measured centers; official Accessibility Insights/Axe.Windows source uses CUIAutomation8 and desktop-root ControlView process matching; a separate Computer Use adapter displayed semantic roles, but its backend identity is not inferred from that display alone.

**Strong source-based explanation, not yet a proven runtime root cause:** Accessibility Insights' reported semantic controls may be exposed by its CUIAutomation8 desktop-root/process-ID ControlView traversal while REM's PyWinAuto route and this direct .NET client ask a different UIA client/element path. The same-run UIA8 walker comparison and live Accessibility Insights element identities are still needed to determine whether the client version, root traversal, a WinForms client-side provider, or another provider-state difference is decisive.

**Unresolved:** why CUIAutomation8 desktop ControlView walker returns semantic Edit/Button through Accessibility Insights but the independent managed .NET UIA path returns Pane; why direct UIA FromPoint fails Access Denied while the same root and bounding properties are readable; whether Accessibility Insights' selected element is the identical runtime element seen by the direct probe; and whether MSAA/IAccessibleEx is involved beneath the UIA wrapper.

### Controlled acceptance status

No ValuePattern was retrieved; no `SetValue`, read-back, focus, InvokePattern, button invocation, status check, or results-list verification was performed. The 8-step semantic acceptance remains **failed/not passed**. No UIA action was substituted with coordinates, clicks, or typing. No production REM/UIA integration change is justified until the exact same-run provider route is demonstrated. The only workspace source touched for this pass is the isolated diagnostic probe; the two reports are documentation updates. No dependency/runtime changes were made; no active REM code, plugins, safety controls, or `memory/routines.json` were touched. No tests were run; the probe compiled, then completed the visible UIA2 comparison and the separately reported UIA8/MSAA diagnostics.


### CUIAutomation8 desktop-root reproduction result

On the same visible fixture process (PID 26584), a fresh COM client created with the documented CUIAutomation8 CLSID called `GetRootElement()`, then queried `FindAll(TreeScope.Children/Descendants, CreateTrueCondition())` and `ControlViewWalker`/`RawViewWalker.GetFirstChildElement(desktopRoot)`. `GetRootElement()` returned a Desktop element (ControlType ID 50033; reported PID 3984), but both FindAll scopes returned zero and both walkers returned null for the first child. Repeating the direct CUIAutomation8 `ElementFromHandle(fixtureHwnd)` query also found zero children/descendants. In contrast, the managed .NET client returned ten direct children and legacy CUIAutomation returned a title bar. This means the official Axe.Windows source route (CUIAutomation8 + desktop ControlView) is now also reproduced as empty in this session; the source route alone does not explain the user-supplied Accessibility Insights result. The Python COM call did not throw on these empty results, unlike the earlier attempted walker invocation that failed with E_POINTER, so that earlier failure was an instrumentation issue in that particular code path.

The current evidence narrows the unresolved discrepancy to the *actual Accessibility Insights runtime context/client state versus the probe clients*, including possible UIAccess/process/desktop context or a difference in which app window/fixture instance was observed. Those are hypotheses only. Accessibility Insights could not be launched to inspect its current process manifest, token, selected element identity, or provider details. No claim is made that UIAccess is enabled or that it is the cause.

## Same-session context and Axe.Windows differential update (2026-11-25)

### Confirmed facts

- A fresh instance of the named disposable fixture was opened in the desktop session. Its accessibility tree reports semantic Edit `Search field` (AutomationId `SearchBox`), Button `Run test search` (`SearchButton`), List `Test results`, checkbox, combo box, tabs, and menu bar. This is a live desktop accessibility observation, not a UIA semantic action.
- Accessibility Insights was launched successfully into the same desktop session while the fixture was present. Its Inspect page is open. The page currently has no selected target (its Patterns pane says “No available pattern...” and its instructions ask for a target to be hovered/focused). This run did **not** capture Accessibility Insights properties for the fixture field or button or matching RuntimeIds/HWNDs/patterns.
- Four fixture windows with the same title are in the desktop inventory. The newest window ID is `4851910` (`0x4a00e6`); earlier IDs are `2622966`, `7276274`, and `1051076`. The desktop accessibility adapter can bind the newest instance and returns the semantic tree. This must not be conflated with prior direct probe results for HWND `0x6f06f2` / PID `26584`.
- The command-shell process environment cannot enumerate the desktop fixture or Accessibility Insights processes: `Get-Process` listed only isolated shell host processes, and `Get-CimInstance Win32_Process` reported CIM access unavailable. Therefore this run could not safely collect process token/integrity/UIAccess/architecture/session details or run a low-level probe from the shell. No process injection, elevation, or security-setting change was attempted.
- Published Axe.Windows source uses `CUIAutomation8` except on Windows 7, obtains `GetRootElement()`, gets the Control tree walker, and matches children/grandchildren by process ID. Its HWND-root overload calls `ElementFromHandle(rootWindowHandle)`, includes the root, then performs the same process matching. See [Axe.Windows A11yAutomation.cs](https://github.com/microsoft/axe-windows/blob/main/src/Desktop/UIAutomation/A11yAutomation.cs), particularly `GetUIAutomationInterface`, `FindProcessMatchingChildrenOrGrandchildren`, and `ElementsFromProcessId`.
- The source also protects `GetRootElement()` with a process-local lock due to a previously observed UI Automation issue. This is a known workaround in Axe.Windows, not evidence that lock behavior caused the discrepancy here. [Accessibility Insights hierarchy source](https://github.com/microsoft/accessibility-insights-windows/blob/main/src/AccessibilityInsights.SharedUx/Controls/HierarchyControl.xaml.cs) imports the Axe.Windows UIAutomation layer.

### Failed and unavailable diagnostics

- The previous visible-fixture run recorded `AutomationElement.FromPoint` failures at the UIA-reported Edit center `(750,268.5)` and button center `(1089,274)` as `System.ComponentModel.Win32Exception: Access is denied`. The old probe did not record numeric HRESULT, timestamp, calling thread ID, or apartment state for each call. These fields cannot be reconstructed after the fact and remain a diagnostic gap.
- The previous CUIAutomation8 desktop-root query returned zero children/descendants and null first children for Control/Raw walkers; its direct fixture-handle query returned zero children/descendants. Those calls did not emit per-call HRESULT or thread apartment data. “Empty” was returned rather than an exception.
- Although Accessibility Insights is running in the current same desktop session, its selected target is not the fixture. Same-run RuntimeId/HWND/PID/FrameworkId/provider description/bounds/pattern comparison remains outstanding. No coordinate click, UIA value setting, or typing was used to select or activate fixture controls.
- Current per-process user, integrity level, elevation, UIAccess, process architecture, Windows session, COM initialization, and UIA initialization are **not measured** for REM, Accessibility Insights, or the fixture. No elevation or security change was made.

### Fact / hypothesis / blocker distinction

**Facts:** Accessibility Insights uses the published Axe.Windows UI Automation layer; source shows CUIAutomation8 plus desktop-root ControlView process matching. In this session the fixture adapter exposes semantic controls, and Accessibility Insights is open. The historical direct .NET and CUIAutomation8 probes disagree with the adapter's semantic tree; historical visible FromPoint calls failed Access Denied.

**Hypotheses only:** UIA client version, provider/client-side providers, COM apartment, process security context, selected fixture instance, desktop/session state, or Axe.Windows' traversal/locking sequence could explain the mismatch. No cause has been isolated. The source-level lock is a candidate to test, not a diagnosis.

**Blockers:** same-target Accessibility Insights selection and identity capture; process/security/apartment metadata; fresh instrumented CUIAutomation8 per-call HRESULT logs against HWND `0x4a00e6`; and requested pattern retrieval. No semantic action was performed. Exact runtime root cause remains unresolved and semantic acceptance has not passed.

## UIA adapter outcome (2026-11-25)

### Confirmed root cause for the observed discrepancy

The prior diagnostic process was running in a Low-integrity AppContainer (`FIRENEXUS\tarun`, integrity SID `S-1-16-4096`, AppContainer true, UIAccess false). Its attempts to open normal desktop processes were denied (`OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION)` returned WinError 5); its CUIAutomation8 desktop traversal returned no children and `FromPoint` returned `0x80070005` (`E_ACCESSDENIED`). A diagnostic launched in the interactive user's ordinary Medium-integrity, non-AppContainer context used CUIAutomation8 from an MTA and successfully enumerated the same disposable WinForms fixture. This supports the security-context differential as the exact cause of the *probe discrepancy*. It does not measure the token of a separately running REM process, which was not active for this test, and does not establish that every REM deployment runs in the low sandbox.

The successful route followed the Accessibility Insights/Axe.Windows sequence: create CUIAutomation8; initialize COM MTA on the worker thread; obtain the desktop root under a lock; traverse ControlView and match PID/HWND; cross-check the result against `ElementFromHandle`; inspect actual patterns. The visible fixture exposed Search field as Edit (50004), AutomationId `SearchBox`, writable ValuePattern, and Run test search as Button (50000), AutomationId `SearchButton`, with InvokePattern. This contradicts the earlier CUIAutomation8-empty result from the low AppContainer process. MFC was not the cause of this enumeration discrepancy.

### Implementation and validation

Added `core/windows_uia.py` as a bounded CUIAutomation8 adapter and routed the existing `screenview` Windows scan and semantic action path through it. It performs COM work on a dedicated MTA worker, checks window PID/HWND and re-resolves runtime identity before action, reads supported pattern capabilities, honors the existing emergency-stop check, refuses protected-value writes, and returns HRESULT details on API failures. PyWinAuto remains the compatibility fallback if the COM client cannot be imported. No coordinate input is performed by this adapter. Updated the existing focus-path unit test for the new adapter boundary and added `tests/test_windows_uia_adapter.py`.

The direct real CUIAutomation8 acceptance probe on the disposable fixture succeeded for field discovery, ValuePattern retrieval, `IsReadOnly=False`, `SetValue("REM_UIA_TEST_123")`, exact readback, button discovery, InvokePattern retrieval, and `Invoke()`. A fresh visual inspection showed the fixture status changed to `Search completed: REM_UIA_TEST_123`. The fixture's own WinForms handler does not modify the results list; the observed list remained Alpha/Bravo/Charlie. Its accessible status element keeps the fixed Name `Search result status`, so comparing that UIA Name before/after falsely reports no change. Therefore the button action worked, but the explicit list-update postcondition did not pass and full requested acceptance is **not passed**. The field was cleaned back to empty using ValuePattern. No mouse click or coordinate typing was used.

The same exact adapter module integrated into active REM was not separately exercised from a normal Medium-integrity REM runtime in this pass. The live probe confirms the CUIAutomation8 API sequence and provider behavior; the integrated adapter has mocked coverage only. This is a remaining validation item, not a claim of live REM integration success.

### Tests and limitations

- Mocked: `tests.test_windows_uia_adapter` (3 tests) passed; full `unittest discover -s tests` passed (106 tests); `compileall -q core tests` passed; `git diff --check` passed. One first full-suite run exposed the old focus test still mocking PyWinAuto; that test was updated to mock the adapter seam, then the full suite passed.
- Real Windows: CUIAutomation8 discovery, pattern retrieval, ValuePattern write/readback, InvokePattern invocation, and visible status change passed on the disposable fixture. The list postcondition failed because the fixture handler does not update that list. No real checkbox/combo/list/tab pattern actions were performed.
- Not established: live REM process token/context; live REM-through-adapter action; accessibility property support for status text beyond the fixed Name; successful results-list update; other-control semantic actions.

Changed for this pass: `core/windows_uia.py`, `core/screenview.py`, `tests/test_windows_uia_adapter.py`, `tests/test_visual_targeting_extensions.py`, and this report. Existing dirty files and `memory/routines.json` were preserved; no dependency installation, staging, commit, or push occurred.

## Integrated adapter acceptance — 2026-11-25

The previously missing integrated-path test was completed on a visible disposable WinForms fixture. The acceptance runner ran in the ordinary desktop session as Python 3.12.10 x64 (`pythonw.exe`), user `FIRENEXUS\tarun`, Medium integrity (`S-1-16-8192`), not elevated, UIAccess false, AppContainer false, session 2. The fixture was PID 9884, HWND `0xD1089C`, title `REM UIA Disposable Acceptance 2026-11-24`. The caller initialized COM STA; the REM adapter created CUIAutomation8 on its dedicated MTA worker. The adapter discovered 25 elements.

Crucially, this pass invoked REM's actual `core.screenview._scan_windows()` and `core.screenview.perform_uia_pattern()` entry points, which call the integrated `core.windows_uia` adapter; this was not only the standalone probe. It found exactly one Edit named Search field (ControlType 50004, AutomationId `SearchBox`), exposed ValuePattern and `value_read_only=false`, set `REM_INTEGRATED_UIA_TEST`, and the adapter's immediate readback plus a fresh REM scan matched exactly. It found exactly one Button named Run test search (ControlType 50000, AutomationId `SearchButton`), exposed InvokePattern, and invoked it. The fixture visibly displayed `Search completed: REM_INTEGRATED_UIA_TEST`. No mouse coordinates or keyboard input were used.

Other genuinely exposed patterns were exercised through the same integrated action entry point on the disposable fixture: checkbox TogglePattern was toggled and restored; combo ExpandCollapsePattern was expanded and collapsed; the Alpha result ListItem SelectionItemPattern was selected. The visible fixture afterward showed the checkbox restored off, combo collapsed, and Alpha selected. Tabs exposed SelectionItemPattern on tab children, but those children had empty RuntimeIds; REM's stale/identity checks intentionally prevent acting on them. No WindowPattern was reported for the window root. Combo SelectionPattern was not exposed (the combo exposed Value and ExpandCollapse); the list container exposed SelectionPattern, but this is a read/query pattern rather than a selection action and was not misrepresented as an action.

This is a live integrated-adapter acceptance in the same normal desktop security context REM is expected to use, but it was launched as an acceptance Python process importing REM's actual adapter; the full `main.py` Gemini Live application was not running, so its own process context and voice-to-UIA path remain unmeasured. The runner and JSONL evidence are retained in `recovery/windows_acceptance/run_rem_uia_adapter_acceptance.py` and `rem_uia_adapter_acceptance.jsonl`.

A failed semantic Invoke now fails closed. The action layer no longer retries a UIA Invoke failure (including timeout/unknown completion) as a physical click, preventing a possible duplicate action. ValuePattern writes now verify exact CurrentValue readback before reporting completion.

Final validation after these changes: 107 automated tests passed; Python compilation passed for core/actions/plugins/memory/tests/Windows acceptance harnesses; `git diff --check` passed (only existing LF/CRLF conversion notices). No unrelated modified files were reverted. `memory/routines.json` remained as found in the initially dirty worktree; no staging, commit, or push occurred.

Additional adapter coverage: UIA TextPattern reads bounded visible text where exposed; SelectionPattern reports selected-item count, and WindowPattern routes normal/restore/maximize/minimize requests through `SetWindowVisualState`. These latest pattern routes are compilation/mock-reviewed only; no real window-management or WindowPattern acceptance has been performed. The current overall suite is 117 passed.
