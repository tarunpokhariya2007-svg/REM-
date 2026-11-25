# REM Browser Automation Acceptance

Date: 2026-11-25  
Status: **PARTIAL — CDP navigation and DOM discovery verified; visible interaction is safely blocked while the real page is hidden.**

## Setup and identity evidence

Test environment: actual Windows Chrome, disposable temporary profile, explicit loopback-only CDP port, and a local HTTP test page. The user's normal Chrome profile and external websites were not used.

On the most recent diagnostic run:

- CDP attached to a target with URL `http://127.0.0.1:<ephemeral-loopback-port>/` and a recorded nonempty target ID; the context exposed one Playwright page.
- The target returned `document.visibilityState="hidden"` after `page.bring_to_front()`.
- Win32 found the corresponding Chrome top-level window as visible and not minimized (`Chrome_WidgetWin_1`, HWND `6293552`, title `about:blank - Google Chrome`, rectangle `[10,10,1060,855]`).
- `GetForegroundWindow()` belonged to PID `19492`, not the disposable Chrome PID `27948`.
- A prior direct `SetForegroundWindow()` call returned a Win32 error; `ShowWindow(SW_RESTORE)` did not make the document visible. This run therefore establishes that the target was not headless or minimized and that the Win32 foreground window belonged to another process. The Windows foreground restriction prevented the test runner from activating Chrome. The exact foreground-lock policy state and why CDP target activation did not promote the Chrome HWND remain unresolved.

Root cause established at the application level: the harness's CDP page was a background/inactive document while a different process owned the foreground; the page URL could be read, but the document was not visible or ready for user interaction. `Page.bring_to_front` changed neither `visibilityState` nor foreground ownership. The exact Windows foreground-lock state that caused `SetForegroundWindow` rejection was not identified, so that OS-level mechanism remains an unresolved detail. REM now refuses navigation as well as input while the current page is hidden. Do not treat this as a completed browser interaction repair.

## Acceptance results

| Check | Result | Evidence |
|---|---|---|
| Disposable Chrome startup and loopback CDP attach | PASS | Fresh temp profile; explicit endpoint; no user profile |
| CDP URL/DOM access | PASS | Target URL, title, `document.readyState="complete"`, search input and result labels were readable |
| Visible navigation | PARTIAL | The earlier implementation reported URL/readiness success despite hidden visibility. Current implementation refuses navigation unless the current page is visible; mocked hidden-page test passes, but no real visible retest is available. |
| DOM/title/result text discovery | PASS | Local test title, search input and three link labels found |
| Search field identification | PARTIAL | DOM exposed one `type=search`/searchbox. Current REM correctly refused typing because the page was hidden; visible typing was not demonstrated in this run. |
| Query typing, Ctrl+A replacement, Enter submission | BLOCKED | Page hidden. Before the new guard, fill readback worked but Enter did not submit. Current guard prevents mutation/key delivery while hidden. |
| First, second, third result | BLOCKED | Page hidden. Earlier real attempt timed out waiting for links to be stable; current run refused all clicks before target interaction. No destination was opened. |
| Named/missing/ambiguous/disabled target | PARTIAL | Earlier real run refused missing, ambiguous, and disabled targets on a visible-to-DOM but hidden document; current guard refuses earlier due hidden page. Named result selection not demonstrated. |
| Closed tab/context recovery | PASS | Tab close recovery, blank-page recovery, and reconnect to a second disposable profile passed. |
| Tab switching | NOT TESTED | Not included in current real harness. |

## Implementation and tests

`actions/browser_control.py` now confirms navigation by checking URL, document readiness, and page visibility. It uses a unique visible enabled searchbox for generic search typing, reports searchbox clicks only after focus is verified, reports Enter submission only after a URL change is confirmed, supports semantic first/second/third/last results scoped to primary result links, verifies the chosen href before click, and rechecks page state after interaction. It refuses navigation, typing, keypresses, form fill, and clicks whenever the document is hidden. Consequential page actions remain subject to existing confirmation logic.

Mocked tests cover Enter safety, hidden-page refusal, and correct first/second/third destination identity and post-click state verification. These do not replace a real visible Chrome acceptance test.

## Remaining blocker

Run the same test after the disposable Chrome window is genuinely foregrounded and its document reports `visibilityState="visible"`. The current unattended runner cannot claim foreground ownership under Windows policy. Do not bypass the OS foreground restriction, force clicks, or use coordinates on an unverified target. Until this test runs visibly, the requested search/selection workflow is not accepted.

## Master mission recheck — 2026-11-25

No new visible Chrome acceptance was performed in this continuation. The missing runtime-log artifact means individual `status=unverified` events cannot be correlated to a specific CDP state. Existing evidence still shows the hidden-document/foreground-owner mismatch. The guard remains fail-closed; browser acceptance remains **PARTIAL/BLOCKED**. Request-context unit tests and hidden-page/ordinal selector tests passed, but they do not count as real visible interaction.

## Latest mission continuation — 2026-11-25

The browser module now accepts optional `REM_EDGE_CDP_URL` as well as `REM_CHROME_CDP_URL`, each restricted to explicit loopback HTTP/WebSocket endpoints. Browser tests cover the Edge configuration path. The disposable Chrome harness now captures startup diagnostics and cleans up its own process if startup fails; added Crashpad-disable flags did not resolve the environment failure.

Fresh Chrome result: **BLOCKED before CDP**. Chrome with a new temporary profile exited with Crashpad `TransactNamedPipe: The pipe has been ended` / crash-server startup failure (exit `4294930433`). No search or result selection was performed. The disposable Edge attempt returned no verified startup or CDP result and is **NOT VERIFIED**. No personal browser profile was accessed. Historical CDP attach/DOM pass and hidden-page foreground block remain as recorded above.

Full regression: 171/171 passed; compileall and diff check passed. The new model-facing conversational result guidance is unit-tested, not live-speech-tested.

## Website-name clarification continuation — 2026-11-26

The browser URL normalizer previously appended `.com` to every bare word, despite the tool contract saying not to invent addresses. It now maps a short explicit list of common sites and returns one `ask` outcome for an unknown bare name; complete domains and validated URLs continue to navigate normally. A unit test verifies ambiguous-name clarification. No fresh live Chrome or Edge interaction was attempted, so visible navigation remains at its prior acceptance status.
