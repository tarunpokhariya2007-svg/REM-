# Persistent Desktop and Memory — Phase 0 Audit

Date: 2026-11-24
Project: `C:\important\Mark-LVII-main\Mark-LVII-main`

## Baseline and preservation

- Python: 3.12.10 x64 in the project virtual environment.
- Offline suite before this upgrade: **87 tests passed**.
- Discovery: **27 active plugins**, **24 active actions**.
- Existing worktree is dirty from earlier approved REM work. It includes edits in desktop control, screen observation, mouse targeting, browser control, plugins, tests, and an already modified `memory/routines.json`. Do not reset any of it. `memory/routines.json` is not part of this work and has not been opened for writing.
- `pywinauto`, RapidOCR, ONNX Runtime, MSS, and stdlib SQLite are available in the virtual environment. Earlier real Windows acceptance recorded successful UIA initialization, screenshot capture, OCR, and physical mouse interactions in the existing `recovery/*` reports.
- No packages were installed for this audit.

## Existing architecture

### Windows UI Automation and screen view

- `core/screenview.py` owns cross-platform screen inspection. `look()` identifies the foreground process/window, enumerates visible windows, and tries UIA on Windows before OCR. It bounds UIA traversal by time/depth/element count, records visible UIA names, roles, values for selected controls, enabled state, bounds, password protection, and a coarse digest. OCR is a local fallback and reports degraded semantics.
- `pywinauto.uia_element_info.UIAElementInfo` is the Windows tree provider. ValuePattern and TextPattern are read opportunistically. Password content is suppressed.
- Gaps: projected `Element`s do not carry AutomationId, runtime identity, parent/child context, focused state, or available pattern names. UIA structure/property/focus event subscriptions are absent. The view is freshly rescanned, which avoids long-lived stale wrappers but is not event-driven.

### Desktop actions

- `actions/screen_agent.py` semantically matches the latest `Element` list, refuses unresolved ties, and delegates to `core/screenact.py`.
- `core/screenact.py` validates stop state, control bounds, enabled/protected fields and input delivery. Even a UIA-discovered element is reduced to its rectangle and acted on through PyAutoGUI/physical input. There is no Invoke, SelectionItem, Toggle, ExpandCollapse, or direct ValuePattern action layer. Therefore the current implementation observes the UIA tree first, but semantic UIA patterns are not yet the primary actuation mechanism.
- The central `desktop_control` action lock and stop latch exist. Existing reports include real physical mouse acceptance on a disposable window; keyboard API delivery has not received the same real app-level acceptance in the reports read for this audit.

### Observation, consent, and privacy

- `ON-DEMAND`, `LIVE ASSIST`, and `COMPUTER CONTROL` are current modes. `LIVE ASSIST` starts a daemon that locally calls `screenview.look(allow_ocr=False)` and backs off from 2 to 8 seconds when stable; it does not upload screenshots.
- Status is shown in the existing COMPUTER USE panel. Pause and emergency stop exist for control.
- There is no persistent observation setting, first-enable consent flow, independent observation pause/resume, app exclusion setting, or separate persistent cloud-vision permission. ON-DEMAND screen tool calls send their captured image to the active Live model under the existing direct-request path; a future persistent local-observation preference must not enable this upload.
- Secure desktop content is not intentionally captured as an app target, but a clear explicit secure-desktop/excluded-app policy is not present as a settings feature.

### Browser/CDP

- `actions/browser_control.py` uses Playwright/DOM where available and has an explicit opt-in, loopback-only CDP endpoint validator. Attached profiles are not closed by REM. Disposable-profile YouTube targeting and watch-ID verification are documented as passed in `recovery/CHROME_VALIDATION_REPORT.md`.
- Lifecycle helpers select a live page or create one when none are available; `_resume_last_page` logs and continues on failure. Current tests cover endpoint restrictions and not-closed ownership, but not all closed browser/context/reconnect cases listed in the prompt.

### OCR and screenshot

- MSS captures locally. `core/screenview.py` prefers UIA; RapidOCR/ONNX Runtime, legacy RapidOCR and PaddleOCR are optional fallbacks. Previous acceptance records real OCR strings and bounding boxes from a disposable screenshot. Observation reports retain degraded/source metadata.

### Memory

- The existing persistent store is **JSON**, `memory/long_term.json`, operated by `memory/memory_manager.py`; it has identity/preferences/projects/relationships/wishes/notes categories, dates, bounded values, a local search function, and a prompt budget that avoids dumping the full store.
- There is no existing SQLite memory database/schema to migrate. `memory/routines.json` is a separate user data file and is explicitly out of scope. Creating a second database would violate the no-duplicate-store requirement; any future database design requires a separate migration decision and must preserve the JSON source.

### Audio, task planning, and authorization

- `main.py` receives Gemini Live voice transcripts and dispatches tool calls; audio output uses an asyncio queue of 200 chunks and drops a chunk when full (`QueueFull` is caught). No overflow metric/backpressure policy is exposed by the inspected path.
- `core/request_context.py` now supplies immutable expiring task snapshots; `main.py` captures one per Live tool-call batch and threads it through nested plugin routes. **87 tests passed after this change.**
- `core/desktop_control.py` retains action classification, confirmation via `core/confirm.py`, the action lock and emergency-stop release/cancel path. Screen observations and actuation are distinct tools/intents; persistent observation must remain local and must not authorize actions.

## Files to consider

1. UIA: `core/screenview.py`, `core/screenact.py`, `actions/screen_agent.py`, `tests/`.
2. Persistent local observation and visible status: `memory/config_manager.py`, `core/desktop_control.py`, `gui/window.py`, `ui.py`, tests.
3. Browser recovery: `actions/browser_control.py`, `tests/`.
4. Memory: `memory/memory_manager.py` and tests, retaining `long_term.json` and never touching `routines.json`.
5. Audio queue: `main.py`, tests.
6. Reports/docs: `recovery/`, README only if verified user-facing behavior changes.

## Prioritized implementation sequence

1. UIA-first semantic pattern operations with refreshed identity and tests; then verify with disposable controls.
2. Persistent local observation OFF by default with explicit first-enable consent, independent local/cloud permissions, visible status, pause/resume, emergency OFF, and exclusions.
3. Browser lifecycle failure coverage and recovery without claiming an attached session is owned.
4. Memory improvements only within the existing JSON store unless a safe SQLite migration is separately designed; no duplicate DB.
5. Queue overflow diagnostics/backpressure and bounded-memory regression tests.
6. Real Windows/UIA/browser tests using disposable apps/profile; full end-to-end voice acceptance only when it can be run without touching personal windows/data.

No implementation files were changed while preparing this audit.

## Phase 0 status after implementation work

The Phase 0 findings remain the baseline for the implementation report. Controlled changes began only after the audit file was written and pre-edit copies were prepared. UIA pattern-based semantic interaction is now partially implemented; it has mocked coverage but not a real semantic control fixture. Persistent local observation and separate cloud screenshot consent have been added with local config storage and GUI controls. The existing UIA observer still polls rather than subscribing to UIA events. The memory store remains JSON; no duplicate SQLite database was created. See `PERSISTENT_DESKTOP_AND_MEMORY_REPORT.md` for test categories, real Windows smoke outcomes, modified files, and remaining work.
