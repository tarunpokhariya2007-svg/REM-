# Mouse Control Report

Date: 2026-11-24

`core.screenact` uses the existing PyAutoGUI input backend with fail-safe enabled, checks central stop state before input, validates element bounds, and supports clicks (including right/double), hover, drag, scroll and best-effort input release. Desktop operations are wrapped by the shared non-blocking action lock in current high-level paths.

The high-level `desktop_ui` tool does not currently expose semantic `hover`, `right_click`, `double_click`, cursor movement to a named element, relative movement or named screen-position movement. Arbitrary `Point` objects are accepted by `screenact` and only checked for screen bounds; the public high-level semantic tool does not currently call this API from vision-generated coordinates.

No physical cursor action, click, drag, held-button cancellation or multi-monitor cursor test was run in this pass. No claim of tested physical control is made. Existing Chrome, VS Code and restored unsaved Notepad windows were not used for input.

## Current implementation update

- `desktop_ui` and the `screen_agent` plugin now expose `hover`, semantic `move` to a named accessibility element, named desktop positions (center/corners), bounded relative cursor movement (maximum 200 logical pixels per axis), `right_click`, and `double_click`.
- Named actions resolve a fresh element from the current accessibility view before actuation. The move path checks availability/emergency-stop before and after cursor motion; the wrapper remains inside `desktop_control.action()` and its shared lock. A move/hover request never invokes click or typing.
- Right- and double-click authorization now requires the corresponding explicit wording in the direct user request. Existing confirmation behavior remains for consequential element labels.
- Mock tests verify schema exposure, semantic resolution, no click on move, and read-only observation. These do not verify PyAutoGUI physical output.
- Relative/named screen-position movement is explicit positional behavior, not semantic object targeting. UIA failure and OCR absence still prevent native name-based targeting on this Windows host. No physical cursor acceptance test was performed.

## Real Windows acceptance attempt — 2026-11-24

- **Physical absolute movement:** PyAutoGUI moved the Windows cursor to the requested physical coordinate and `GetCursorPos` (via PyAutoGUI) matched it. The pointer was confirmed over the disposable test window by `WindowFromPoint`.
- **Physical hover/action acceptance:** **failed / not accepted.** The disposable Tk window did not record the expected hover event. The test stopped before any click or other action; therefore left/right/double-click, relative motion, scroll, drag/drop, held-button release, emergency-stop interruption and cleanup remain unverified on real Windows.
- The DPI diagnostic found `GetDpiForWindow=96` while the test window's physical Win32 rectangle measured 1145×911 for Tk's 900×690 geometry. The harness calibrated using the physical window rectangle, but hover still did not reach the Tk callback. This is evidence that physical cursor placement alone does not yet prove delivery to the intended control.
- The mocked emergency-stop drag test passes; it is not a physical emergency-stop acceptance.
- No existing Chrome, VS Code or unsaved Notepad document received input. The harness used only its disposable Tk surface and closed it after the failed check.

## Re-test after dependency approval — 2026-11-24

**Real Windows results: all requested physical mouse checks passed on the disposable window.** The harness verified OS cursor coordinates and observed the corresponding test-app callback/result for each operation:

- Absolute movement: passed; requested and actual position `1018,283`.
- Relative movement: passed; `+13,+9` reached the expected OS position.
- Left-click, right-click and double-click: passed; each target callback fired.
- Hover and scroll: passed; callbacks fired.
- Drag/drop: passed; the test app confirmed the pointer released inside the drop target.
- Mouse down/release: passed; `GetAsyncKeyState` changed from down to up and the test app observed release.
- Emergency stop during drag: passed. The stop was synchronized to the test app's actual mouse-down event; REM returned `cancelled safely`, released the button, latched stop, and did not complete the drop. The test process resumed desktop control after the assertion.

### Diagnosis of the previous hover failure

Two test-only issues caused the earlier apparent failures; the production safety code was preserved:

1. The harness mapped Tk widget coordinates using the outer Win32 window rectangle. Tk widget coordinates are client-area coordinates. At the observed 125% effective scale, the outer-frame transform aimed at `1023,250`; the correct client-area point was `1018,283`. Mapping from `ClientToScreen(hwnd, (0,0))` and `GetClientRect(hwnd)` made the hover callback fire.
2. The original disposable fixture ran as `python.exe`, the same executable REM identifies as its own. The existing `_lands_on_self` guard correctly refused its clicks. Running the fixture as the distinct `pythonw.exe` executable kept the production self-window guard active and allowed safe tests against a separate process.

An early emergency-stop attempt used a fixed timer that could fire before drag mouse-down; that attempt did not count. The final test waits for the fixture's mouse-down callback before latching stop, and passed.
