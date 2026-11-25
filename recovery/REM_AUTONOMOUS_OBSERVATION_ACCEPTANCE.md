# REM Autonomous Observation Acceptance

Date: 2026-11-25  
Overall: **PARTIAL**

## Permission behavior

- The settings UI provides a clear **PERSISTENT LOCAL UIA OBSERVATION** checkbox. It is off by default, asks once when turned on, persists across restarts, can be switched off, and is also cleared by Emergency Stop.
- The canonical preference key is now `local_screen_observation_enabled`. The old `persistent_local_observation_enabled` and `persistent_desktop_observation_enabled` keys remain readable/writable for compatibility. If the canonical key exists, its value takes precedence, including explicit `false`.
- The existing preference controls periodic low-rate local accessibility observation. It does not grant mouse/keyboard action authority and never grants cloud screenshot sharing.
- An explicit direct screen-inspection request can use the local `desktop_ui` observation route when cloud screen vision is disabled. This route does not ask a second conversational permission; it remains constrained by the request-context authorization and desktop safety controls.
- Cloud image sharing remains a separate, default-off `cloud_screen_vision_enabled` permission. A local preference cannot pass the cloud-egress preflight.

## Evidence

| Capability | Result | Evidence type |
|---|---|---|
| Persistent preference default, compatibility and restart persistence | **PASS** | Unit tests with temporary config files; no user's config was edited |
| Explicit false overrides stale legacy true | **PASS** | New unit test |
| Local and cloud permission separation | **PASS** | Unit tests; cloud image preflight test |
| Emergency Stop clears persisted observation and latches control stop | **PASS** | Unit test |
| Window identity, UIA metadata and local OCR | **PASS (prior real Windows evidence)** | Earlier disposable-window tests; real RapidOCR text and bounding box, real MSS capture, and UIA fixture evidence are documented in the final acceptance matrix |
| Direct local screen request with cloud permission off | **PASS (mocked dispatch)** | Unit test verifies local semantic route, image not sent |
| Real spoken request → local observation → spoken response | **NOT VERIFIED** | Prior Live attempts produced no confirmed speech transcript/tool call/result/output audio in the latest run |
| Passive observation while a user is working | **NOT TESTED this continuation** | No real long-duration observation run |

## Changes in this continuation

- `memory/config_manager.py`: canonical local observation preference key and fail-safe precedence, while preserving legacy settings.
- `tests/test_persistent_observation.py`: canonical setting persistence/precedence coverage.
- `main.py`: omitted Live tool context now fails closed, and screen observations continue to respect the separate cloud-egress permission.

## Limits

This does not make REM continuously capture screenshots. Persistent mode remains low-rate local UIA observation as described in the UI. Local OCR/screenshot work is on-demand and bounded by the existing observation route. This report does not claim the full voice workflow, persistent passive observation, or visible browser task has passed in a live session.

## Latest mission continuation — 2026-11-25

Automated regression finished at **171/171** tests; compilation and diff hygiene passed. Central result sanitation/guidance has unit coverage, but no real Live observation request was recognized or returned in this continuation. Local observation evidence remains the earlier real MSS/UIA/OCR work plus mocked permission/routing tests. The persistent setting was not changed; cloud screenshot permission remains separate. Overall status remains **PARTIAL**.
