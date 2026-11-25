# Target Accuracy Report

Date: 2026-11-24

## Baseline

- Native targets are selected from fresh UI Automation/accessibility elements by role/name; exact single matches are preferred and ambiguous candidates are refused.
- `screenact` validates enabled state, dimensions, on-screen placement and foreground/occlusion warnings before delivering input. `actions.screen_agent` re-observes after action and reports whether an interface change or focus/value was verified.
- The native path currently has no ordinal collection targeting, semantic spatial-relation targeting, or conversational "it/that" reference cache. UIA failure on this machine prevents an actual target candidate list; OCR is unavailable.
- Browser `smart_click` checks locator uniqueness and page state but currently has no dedicated YouTube result-card classifier. A generic text match is not proof that a target is the first video rather than a logo or unrelated control.
- No actual YouTube first/second-video or wrong-target acceptance test has been performed.

## Acceptance status

All physical target accuracy checks remain **not run**. New deterministic DOM-selector tests, if added, will be labelled mocked and will not count as a real Chrome acceptance test.

## Current implementation update

- On YouTube pages only, `browser_control.smart_click` now recognizes ordinal descriptions for first, second, third, and last videos. It enumerates visible `ytd-video-renderer` / `ytd-rich-item-renderer` cards, requires a visible title/watch link and a valid video ID, excludes identified ad containers, de-duplicates video IDs, and refuses missing or non-unique candidates.
- It clicks the selected video-title anchor by the inspected video ID and reports success only when the resulting URL is `/watch` with the same `v` ID. This prevents a generic logo/text locator from being treated as the requested first video.
- Deterministic fake-DOM tests cover first and second card identity, URL-ID verification, and refusal on an empty result list. They do not execute the selector JavaScript against YouTube, verify ads in a live DOM, or count as real Chrome/wrong-target acceptance tests.
- Native spatial/ordinal target classification, visual boxes and conversational target memory remain unavailable. No unsupported model confidence is used and no vision-generated coordinate is converted to native input.

## Real acceptance update — 2026-11-24

- No real Chrome/YouTube target was selected. No intended video ID, selected ID or resulting watch URL exists for a live run; YouTube correct-target selection is **not demonstrated**.
- The disposable Windows input test proved cursor positioning to an OS coordinate only. It failed to produce the expected hover callback and safely stopped before any click. It does not establish visual target accuracy or control delivery.
- Current UI Automation is blocked because `pywinauto` cannot import `win32ui`; the loader reports missing `mfc140u.dll`. Native semantic element accuracy cannot be tested until the supported runtime issue is repaired and UIA initialization is verified.
- OCR packages are absent, so OCR-derived boxes/text and spatial target selection were not tested on a fresh screen.

## Real Windows/browser verification — 2026-11-24

- **UI Automation:** after installing the approved runtime, `win32ui` imported and `Desktop(backend="uia")` initialized. The disposable Tk test window was enumerated, but Tk did not expose its child controls through UIA. In a separate disposable Chrome run with renderer accessibility enabled, pywinauto found the Chrome window and enumerated **560 descendants**, including the YouTube search box, page title and result text. UIA is working; coverage remains application/provider-dependent.
- **OCR:** RapidOCR read a fresh MSS screen capture and returned 65 text elements. It found the known test heading plus `Left click target`, `Right-click target`, and `Hover target`, each with a non-empty bounding box. The screenshot was not saved. Coordinates are recorded in `FINAL_REM_VALIDATION.md`.
- **Physical target test:** the disposable-window mouse suite passed actual target callbacks, including all click types and drag/drop. This validates input delivery to that test surface, not every application or spoken-command interpretation.
- **YouTube first video:** intended title `Learn React With This ONE Project`, ID `G6D9cBaLViA`; selected ID `G6D9cBaLViA`; resulting URL `https://www.youtube.com/watch?v=G6D9cBaLViA`; verified.
- **YouTube second video:** intended title `React Tutorial for Beginners`, ID `SqcY0GlETPk`; selected ID `SqcY0GlETPk`; resulting URL `https://www.youtube.com/watch?v=SqcY0GlETPk`; verified.
- **YouTube third video:** intended title `Build 25 React Projects – Tutorial`, ID `5ZdHfJVAY-s`; selected ID `5ZdHfJVAY-s`; resulting URL `https://www.youtube.com/watch?v=5ZdHfJVAY-s`; verified.
- **Missing target:** on a disposable `about:blank` tab, `smart_click("click the first video")` returned “Could not find element”; URL remained `about:blank`. No click occurred. A live ambiguous-card case was not tested.
- Live page inspection found a title-reporting bug: the original DOM query returned the first watch link, often the aria-hidden thumbnail with “Now playing” overlay text. `actions/browser_control.py` now prefers the actual `a#video-title` link. The unit regression and repeated live first/second/third selections all passed after this correction.
- These browser checks exercised REM's `smart_click` implementation over opt-in loopback CDP in a new temporary Chrome profile. They did not touch the existing Chrome profile. Spoken voice recognition was not used; the natural-language descriptions were passed directly to the tool.
