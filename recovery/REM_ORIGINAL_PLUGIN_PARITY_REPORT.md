# REM Original Plugin Parity Report

Audit date: 2026-11-25  
Project: `C:\important\Mark-LVII-main\Mark-LVII-main`

## Scope and preservation

Compared all 27 active public plugin entries in `plugins/` with the supplied loose recovered sources, the earlier recovery manifest/comparison, and delegated actions/shared services. The public entries are loader-discovered `PLUGIN` declarations; underscore helpers and `_template.py` are not plugin entries. Loader discovery remains covered by the existing complete-suite test. Historical source was read and AST-inspected; it was not imported or executed. No original file under `recovery/original_plugins/` was changed.

The loose recovery directory contains 12 plugin sources, `_telegram_ops.py`, and `main_REM_fixed.py` (a historical entry-point snapshot). The Hostinger archive contains historical `cloud_agent.py`, `cloud.py`, `reminder.py`, `lazyimport.py`, and README material. Previously inspected project archives did not add an authoritative original plugin set.

## Inventory

### Current active public plugin entries (27)

`aircraft_report`, `calendar_control`, `calorie_counter`, `chat_takeover`, `code_helper_plugin`, `dev_agent_plugin`, `document_review`, `excel_writer`, `flight_finder_plugin`, `game_updater_plugin`, `gmail_control`, `home_assistant`, `image_generator`, `obsidian_vault`, `pomodoro`, `printer_control`, `pushup_counter`, `quiz`, `screen_agent`, `shopping_list`, `spaced_repetition`, `telegram_remote`, `upload_video`, `water_reminder`, `whatsapp_call`, `whatsapp_guard`, `youtube_video_plugin`.

Shared active plugin helpers include `plugins/_common.py`, `plugins/_google_oauth.py`, and `plugins/_whatsapp_core.py`. There is no active `_telegram_ops.py` and no machine-readable manifest separate from plugin declarations/settings.

### Supplied loose originals

`aircraft_report.py`, `calorie_counter (4).py`, `chat_takeover.py`, `document_review.py`, `excel_writer.py`, `game_updater.py`, `pomodoro.py`, `quiz.py`, `shopping_list.py`, `telegram_remote.py`, `water_reminder.py`, `whatsapp_call.py`, `_telegram_ops.py`, and `main_REM_fixed.py`.

The loose originals map to 12 functional plugin/helper sources; the historical main file is not a plugin. All are available and preserved in `recovery/original_plugins/loose/`.

## Feature parity matrix

| Plugin/source | Original feature | Current implementation and evidence | Status | Required action |
|---|---|---|---|---|
| `aircraft_report` | ADS-B nearest/overhead/list/details, location/radius, radar panel, background proximity watch | Current `plugins/aircraft_report.py` is a separate ADS-B plugin, not the passenger-flight finder. It uses current settings/UI/outcome APIs and contains request budgeting/backoff and untrusted feed handling. Earlier audit reports these integrations; current unit suite does not contact ADS-B/Nominatim. | IMPROVED; external behavior NOT TESTED | Keep separate from `flight_finder_plugin`; run a service fixture/live-feed validation only in a dedicated, approved test. |
| `calorie_counter` | Webcam scan UI, preview/HUD, model estimate and spoken result | Current plugin accepts a local image or requests a one-shot webcam capture after REM confirmation, then uses the configured vision provider; estimates are marked approximate. Live preview/scan bar is absent. | SECURITY-RESTRICTED / PARTIALLY PRESERVED | Keep current confirmation/provider route. Do not restore continuous preview until the camera-consent, preview-thread and privacy lifecycle are designed and tested. |
| `chat_takeover` | Read visible chat, generate and repeatedly send automatic replies, stop/status and time bounds | Current plugin offers read/draft/send. Sending delegates to `send_message` with recipient and confirmation controls. Autonomous reply loop is intentionally absent. | SECURITY-RESTRICTED | Do not restore automatic sending. Current exact-recipient send confirmation remains the safe route. |
| `document_review` | Accept source-grounded structured title/summary/findings/quotes/suggestions/unclear points and render severity-ranked review | Current plugin still delegates file reading/review to `actions/file_processor.py`. This turn restored the original structured finding input/display route via `ui.show_review`, with bounds, severity normalization/order, plain-text fallback and an explicit not-legal-advice response. | PARTIALLY PRESERVED; structured display RESTORED | Keep source extraction delegated to file processor. Verify the findings against the displayed source; no legal conclusions are automated here. |
| `excel_writer` | Natural-language workbook spec, data, total row and optional bar/line/pie chart saved as a new workbook | Current implementation uses explicit workbook data and retains no-overwrite/new-output semantics and source-copy editing. This turn restored optional safe `total_columns` and chart type/category/value fields; numeric conversion is limited to requested numeric/chart-value columns. | IMPROVED; parity PARTIAL | Keep exclusive new-output writes and formula restrictions. Chart and total logic has mocked/temp-file tests; no Excel GUI acceptance. |
| `game_updater` | Steam/Epic discovery, update/install, UIA progress, scheduling, optional shutdown | Active `plugins/game_updater.py` is a compatibility route into `actions/game_updater.py`; it does not copy the 115 KB original UIA driver. Current dispatch retains REM's action safety/confirmation path. The original's auto-EULA behavior is not restored. | PRESERVED IN SHARED SERVICE / SECURITY-RESTRICTED; detailed parity CANNOT VERIFY | No game installs/downloads were run. Compare concrete actions against the current action implementation before adding a specific missing operation. |
| `pomodoro` | Start/stop/status/stats, task label, focus history, repeated work/break phases, long break every fourth cycle and UI/speech notification | Current timer retains start/pause/resume/stop/status and stored phase records. This turn restored task labels, daily focus stats and per-task totals; work completion is recorded as work (rather than the next phase), and every fourth completed block uses a 15-minute minimum long break. No per-second HUD countdown is present. | IMPROVED; PARTIALLY PRESERVED | Validate timer cancellation/shutdown and persistence in the running GUI before claiming lifecycle acceptance. |
| `quiz` | User/context-supplied questions, multiple choice, true/false, fill-blank/open questions, grading and stop/clear | Current plugin generates questions through the configured REM model and uses the existing asynchronous quiz panel. This turn added optional supplied question payloads, bounded sanitation, answer-key resolution, multiple-choice/true-false marking, deferral for open/fill-blank answers, and a stop route. Maximum is intentionally capped at 10 (current schema limit) versus the original 20. | IMPROVED; PARTIALLY PRESERVED | Keep model generation for topic/difficulty mode; test panel callbacks in GUI. No independent history store was added. |
| `shopping_list` | Add multi-item/quantity, list, check bought, remove, clear all/done; local persistence | Current plugin already supported all these restored operations, category tags, and migration from the reconstructed `{item, quantity, category}` row shape without dropping values. | IMPROVED; CORE FEATURES FULLY PRESERVED | Retain the current local JSON schema and migration tests. Original long-term-memory note writes remain intentionally omitted. |
| `telegram_remote` + `_telegram_ops.py` | Pair allowlisted chats; relay tasks; menus/status/away alerts; photos/screenshots/camera/file intake; audio transcription/TTS; stop/panic/leases | Current plugin is a narrower authenticated/pair-code text relay through `on_external_text_command`; untrusted text remains on the external-command route and cannot itself authorize desktop control. This turn added constant-time pairing-code comparison and per-chat/global attempt ceilings. `_telegram_ops.py` remains recovered-only; remote media, file transfer, speech and general remote desktop operations are not active. Pairing code expiry is still not persisted/enforced. | SECURITY-RESTRICTED / PARTIALLY PRESERVED | Keep media/screenshot operations disabled. Add pairing-code expiry and lifecycle tests before advertising pairing as time-limited. No Telegram network was contacted. |
| `water_reminder` | Interval reminders, intake, daily status, personal goal and reminder start/stop | Current plugin retains reminders and persistent intake entries. This turn restored a bounded personal `goal` action, sums today's UTC-dated intake for status, and keeps historical entries/other fields. It does not make medical recommendations; it does not migrate entries to a second store. | IMPROVED; PARTIALLY PRESERVED | Keep user-selected target language. Add timezone-specific day rollover only if the user explicitly wants local-day accounting; current total uses UTC date. |
| `whatsapp_call` | Inspect contact and place voice/video call or hang up through historical `_whatsapp_core` | Original `_whatsapp_core.py` is unavailable. Current helper is reconstructed; the active plugin confirms the visible contact and requires REM confirmation for call requests. End-call click reports only interaction, not verified disconnection. | SECURITY-RESTRICTED / PARTIALLY PRESERVED | Keep the current confirmation and contact checks. Do not fabricate/restore the missing call helper; real WhatsApp calls were not tested. |
| `main_REM_fixed.py` | Historical full application entry point | Current `main.py` is the active architecture and has no direct replacement import. | INTENTIONALLY NOT RESTORED | Preserve as historical source; do not overwrite `main.py`. |

## Shared helpers, archive modules and missing sources

| Historical source | Availability | Current counterpart | Decision |
|---|---|---|---|
| `_telegram_ops.py` | Recovered loose source; preserved, not active | None. Active bridge is narrower and does not import it. | Do not copy in alone: it is coupled to the recovered remote plugin and includes screenshot/camera/file/audio paths. |
| `_whatsapp_core.py` | No recovered historical source found | Reconstructed `plugins/_whatsapp_core.py` | Keep current helper; historical `place_call`/hangup API compatibility cannot be inferred. |
| `_google_oauth.py` | No recovered historical source found | Reconstructed OAuth helper used by Gmail/Calendar | Keep current OAuth flow; no source to restore. |
| `_common.py` | No historical original found | Current plugin configuration/data/schema/dispatch/safe-run helper | Keep as active architecture helper. |
| Hostinger `cloud_agent.py` | Present inside preserved archive | No active plugin of this name; current remote task path is `core/cloud.py` + `plugins/telegram_remote.py` | No direct restoration; it requires protocol, authentication and authorization comparison. |
| Hostinger `cloud.py` | Present inside preserved archive; differs from current `core/cloud.py` | Current cloud service | Do not overwrite or merge wholesale. No service was contacted. |
| Hostinger `reminder.py` | Present in archive | `actions/reminder.py` + current routines | Keep current REM routine/cloud integration; avoid old `.jarvis` state and OS scheduler migration. |
| Hostinger `lazyimport.py` | Present and previously hash-verified identical | `core/lazyimport.py` | No work required. |
| Printer source | No original recovered | Reconstructed read-only `plugins/printer_control.py` | Keep read-only behavior; no print job control added. |

## Current-only plugins without a supplied matching original

The following are reconstructed/current-only relative to the supplied source set; this is not a claim that no other backup exists: `calendar_control`, `code_helper_plugin`, `dev_agent_plugin`, `flight_finder_plugin`, `gmail_control`, `home_assistant`, `image_generator`, `obsidian_vault`, `printer_control`, `pushup_counter`, `screen_agent`, `spaced_repetition`, `upload_video`, `whatsapp_guard`, and `youtube_video_plugin`.

## Integration plan and implementation outcome

1. Preserve current plugin loader, tool names except existing established schema additions, shared helpers, config namespaces and data paths. **Completed.**
2. Compare original function/schema behavior through delegated current actions before restoration. **Completed for the supplied sources using the prior parity audit plus current source checks; external integrations remain untested.**
3. Restore only low-risk, source-supported missing features without replacing current security or data paths. **Completed this turn:** structured review display, safe spreadsheet totals/charts, supplied-question quiz and grading, daily hydration target/status, Pomodoro task/stats/long break, Telegram pairing rate limits and constant-time code compare. Shopping-list check-off/multi-item support and aircraft report were already integrated before this turn.
4. Do not restore the old autonomous chat sender, old Steam auto-EULA path, old Telegram media/remote desktop surface, or missing WhatsApp core. **Kept restricted.**
5. Test registration, schema, isolated function behavior and persistence with disposable files/fake transports. **Plugin parity unit tests passed; no real external-service action was run.**

## Modified files this plugin-restoration turn

- `plugins/water_reminder.py` — daily target and today's logged total.
- `plugins/pomodoro.py` — task labels, work history/stats, fourth-cycle long break.
- `plugins/quiz.py` — supplied questions, safe normalization/grading, stop action.
- `plugins/document_review.py` — structured finding rendering and severity ordering.
- `plugins/excel_writer.py` — safe total rows and optional charts on new outputs.
- `plugins/telegram_remote.py` — rate-limited pairing and constant-time code compare.
- `tests/test_plugin_parity_restoration.py` — focused feature parity tests.
- `recovery/REM_ORIGINAL_PLUGIN_PARITY_REPORT.md` — this report.

## Plugin test results and remaining gaps

Focused plugin parity and reconstructed-plugin tests passed (22 tests in the focused run); the complete repository suite subsequently passed **136 tests** after the final edits. Python compilation and `git diff --check` also passed. Tests use temporary files and mocks. No original file, personal account, actual Telegram/WhatsApp endpoint, game launcher, Excel GUI, camera or remote cloud service was used.

Not restored or not proven: chat takeover auto-send, Telegram remote media/file/voice features and pairing-code expiry, calorie live preview, full historical game updater feature parity, WhatsApp call driver, original Excel natural-language specification generator, 20-question quiz capacity, per-second Pomodoro HUD, and long-term-memory side notes for water/shopping/focus. Security, session and real Windows acceptance are reported separately in `REM_FINAL_MASTER_COMPLETION_REPORT.md`.
