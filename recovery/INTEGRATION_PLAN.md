# REM Plugin Recovery Integration Plan

Status: audit complete; safe additive integrations implemented and tested offline; remote-control and call integrations remain inactive.

This plan preserves the current plugin loader, Gemini Live dispatch, REM voice path, confirmation and emergency-stop systems, input locks, and user configuration. It does not authorize replacing active files or contacting a VPS/service.

## Decisions by recovery item

| Item | Decision | Integration route if later approved |
|---|---|---|
| All current reconstructed plugins | Retain as active baseline. They are loader-valid, enabled, collision-free, and architecture-aligned in the current audit. | Keep current implementations; add isolated adapters only for reviewed missing features. |
| Recovered `game_updater.py` | Do not restore directly. The active compatibility plugin deliberately routes to the current core action; the recovered name collides with a core tool and its `accept_eula` default is too permissive. | Port selected Steam/Epic feature gaps through the current action and preserve its authorization/confirmation behavior. |
| Recovered `telegram_remote.py` + `_telegram_ops.py` | Do not restore directly or copy into active `plugins/`. This is a coupled, feature-rich remote-control pair. | Build a narrow adapter around the current paired-chat/cloud task engine. Add only individually enabled features after reviewing trust-source propagation, privacy/egress, emergency stop, confirmation, duplicate delivery, and worker stop/join semantics. Keep screen/camera transfer disabled absent explicit settings. |
| Hostinger `cloud_agent.py` + `cloud.py` | Do not restore directly. Both are historical and the cloud module differs from current `core/cloud.py`. | Compare protocol and public function contracts; if required, port through a compatibility layer into current `core/cloud.py` without overwriting it. Require local tests using fake transport and a separately authorized staging service before any network smoke test. |
| Hostinger `reminder.py` | Do not restore directly. Current `actions/reminder.py` is the architecture owner and has REM routine/cloud integration. | Port only a demonstrated missing reminder capability, using current paths, routine APIs, and config. Avoid old `.jarvis` storage and shell scheduler assumptions unless specifically migrated. |
| Hostinger `lazyimport.py` | Already compatible and byte-identical to `core/lazyimport.py`. | No action. Keep the active core file. |
| Recovered `whatsapp_call.py` | Do not restore directly. Required historical `_whatsapp_core` is missing and current helper does not implement its old call API. | If call execution remains a goal, add methods only through the current helper after proving exact active contact, explicit REM confirmation, emergency-stop compliance, and post-action verification. |
| Recovered `chat_takeover.py` | Retain reconstructed version. | Do not port automatic send/reply loop without a separately designed, bounded session, exact-recipient revalidation, visible on/off state, timeout, stop path, and approval rules. Read/draft features may be compared independently. |
| Recovered `calorie_counter (4).py` | Retain reconstructed version. | Port HUD camera display only if the current camera-consent flow and provider privacy configuration are preserved. Never trigger capture from screen observation. |
| Recovered `document_review.py` | Retain current file-processor adapter. | A structured review formatter can be adapted as an output schema, with source quotes and uncertainty, after checking current document text bounds and avoiding unsupported legal conclusions. |
| Recovered `excel_writer.py` | Retain current no-overwrite implementation. | Adapt natural-language request/chart specification into the existing new-copy writer; tests must prove source workbook is unchanged and output path collision is refused. |
| Recovered `pomodoro.py` | Retain current timer. | Consider task labels/focus statistics as additive features using current persistence and player APIs. |
| Recovered `quiz.py` | Retain current model/provider integration. | Add an optional prebuilt-question input adapter only after verifying current quiz answer callbacks and asynchronous result path. |
| Recovered `shopping_list.py` | Retain current JSON-backed plugin. | Add multi-item parsing, check-off, and clear-done as additive actions with migration-safe storage tests. |
| Recovered `water_reminder.py` | Retain current plugin and settings. | Add goals and explicit interval overrides after defining rollover, timezone, persistence format, and user-data migration tests. |
| Recovered `aircraft_report.py` | Not a restoration of an existing tool. | Treat as a new plugin proposal separate from passenger `flight_finder_plugin`; review location privacy, endpoint/rate limits, background task lifecycle, and UI thread requirements before implementation. |
| Recovered `main_REM_fixed.py` | Preserve as historical application source only. | No integration into `main.py`; cherry-picking individual verified changes would require a separate scoped review. |
| Recovered `aircraft_report.py` | Adapted as new `plugins/aircraft_report.py`. | Uses current loader/settings/player/outcome interfaces; public ADS-B and optional Nominatim lookups are not invoked during tests. Feed endpoint allowlist, conservative local daily adsb.lol budget, 180-second minimum background interval, 429 backoff, local ignored cache/usage files, attribution, and untrusted result wrapping were added. |
| Recovered `shopping_list.py` | Added missing features to the current `plugins/shopping_list.py`. | Supports comma-separated adds, check/bought, clear-done, and migration from current `{item, quantity, category}` rows to `{name, qty, category, done}` while retaining existing singular `item`, add/list/remove/clear behavior. |
| Model-generated dependency installation | Hardened current `actions/dev_agent.py`; this is not recovered plugin code. | Generated dependency lists and missing-import retries no longer invoke `pip install`; the project build reports the package needed and requests explicit user approval. |
| `_google_oauth.py` and `_whatsapp_core.py` originals | Missing; current reconstructed helpers remain active. | Do not fabricate source or substitute helpers based on filenames. Extend current helpers only when a specific API need is justified and tested. |
| Printer modules | No historical source found. | Keep the current read-only `printer_control`; any write/job control is a new feature requiring its own confirmation and safety review. |

## Staged integration sequence

1. **Source validation:** Confirm provenance/version for each recovered source, inspect complete side-effect paths, compare schemas and helper contracts, record hashes. The preserved copies and comparison are the audit artifacts for this stage.
2. **API compatibility:** For one selected item at a time, map historical calls to current `RemUI`, action/plugin registry, config manager, `core.confirm`, `core.outcome`, provider, and task APIs. Do not add compatibility shims speculatively.
3. **Threat and privacy review:** Identify external data flows, screen/camera/audio capture, messaging targets, background workers, confirmation paths, secrets, file writes, and stop/emergency behavior. Treat screen/phone content as untrusted instructions.
4. **Adapter design:** Keep the active plugin name/schema stable unless a migration is intentional. Avoid tool-name collisions with inline tools/actions. Route risky actions through existing core authorization and confirmation paths.
5. **Tests before activation:** Add isolated tests for schema/loader discovery, dispatch, cancellation, confirmation, privacy gates, failure reporting, source/data preservation, and worker shutdown. Use fake transports; no real Telegram, WhatsApp, cloud, OAuth, camera, or printer operation in unit tests.
6. **Reviewed activation:** Only after tests and a user-visible diff are ready, integrate one adapter and rerun the relevant suite. A real remote-service test is a separate step and must use an explicitly selected test endpoint/account.

## Current stop point

The safe additive integrations above are complete. The active plugin loader and dispatch code are unchanged. Recovered originals remain preserved and are not discoverable by the loader. `memory/routines.json`, credentials, and existing user data were not modified. The aircraft tracker has only been tested with mocks; no external services were contacted. Telegram remote media/file features, WhatsApp calling, cloud-agent integration, autonomous chat takeover, and printer writes remain out of scope for activation pending dedicated safety work.
