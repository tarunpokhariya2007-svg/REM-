# REM Plugin Recovery Manifest

Audit date: 2026-11-24

Project: `C:\important\Mark-LVII-main\Mark-LVII-main`

## Scope and handling

During the initial audit, the active `plugins/` tree was not edited. Recovered loose Python sources were copied byte-for-byte from `C:\Users\tarun\Downloads` into `recovery/original_plugins/loose/`. The Hostinger archive was copied byte-for-byte into `recovery/original_plugins/source_archives/Hostinger VPS.zip`. SHA-256 hashes below identify the preserved copies. No recovered source was executed; comparisons used text and AST parsing. No packages were installed and no credentials were changed.

After the audit documents were complete, a separate controlled integration added `plugins/aircraft_report.py`, restored safe shopping-list features in the current plugin, and disabled unattended dependency installation in `actions/dev_agent.py`. Pre-integration versions were backed up under `recovery/integration_backup/before_aircraft_and_utility_adapters/`. The recovered originals in `recovery/original_plugins/` remain byte-for-byte unchanged.

`plugins/` remains the comparison baseline for the reconstructed implementation. No second copy of it was made, so future comparison uses the live reconstructed files in that directory. Existing unrelated worktree edits, including the prior `memory/routines.json` change, were left untouched.

## Active plugin loader inventory

`main.py` discovers actions first, then passes inline-tool and action names as reserved names to `core.plugin_loader.discover_plugins`. The loader validates `PLUGIN`, rejects name collisions, and re-reads each plugin's enabled setting at dispatch time. The initial audit found 26 plugins. After adding the independent aircraft tool, a fresh discovery using the same reserved-name scheme found 27 valid plugins, all enabled, and zero rejected:

`aircraft_report`, `calendar_control`, `calorie_counter`, `chat_takeover`, `code_helper_plugin`, `dev_agent_plugin`, `document_review`, `excel_writer`, `flight_finder_plugin`, `game_updater_plugin`, `gmail_control`, `home_assistant`, `image_generator`, `obsidian_vault`, `pomodoro`, `printer_control`, `pushup_counter`, `quiz`, `screen_agent`, `shopping_list`, `spaced_repetition`, `telegram_remote`, `upload_video`, `water_reminder`, `whatsapp_call`, `whatsapp_guard`, `youtube_video_plugin`.

The three reconstructed shared helpers are `plugins/_common.py`, `plugins/_google_oauth.py`, and `plugins/_whatsapp_core.py`; `plugins/_template.py` is the loader example, not a helper used by active plugins. Leading-underscore files are skipped as plugin entries. There is no separate machine-readable plugin manifest: each public plugin declares `PLUGIN` in its source, and README provides a capability table.

## Preserved recovered material

### Loose Python files

The following files are preserved under `recovery/original_plugins/loose/`:

| Preserved file | Classification | Current counterpart |
|---|---|---|
| `_telegram_ops.py` | Shared helper for the recovered Telegram bridge | None in active tree; active `telegram_remote.py` is a narrower implementation |
| `aircraft_report.py` | Recovered plugin, live ADS-B aircraft reporting/watch | Adapted as a separate `plugins/aircraft_report.py`; `flight_finder_plugin` remains passenger-flight search |
| `calorie_counter (4).py` | Recovered webcam/nutrition plugin | `plugins/calorie_counter.py` |
| `chat_takeover.py` | Recovered autonomous visible-chat monitor/replier | `plugins/chat_takeover.py` |
| `document_review.py` | Recovered structured document-obligation review | `plugins/document_review.py` |
| `excel_writer.py` | Recovered natural-language workbook generation | `plugins/excel_writer.py` |
| `game_updater.py` | Recovered Steam/Epic installer/updater | `plugins/game_updater.py` plus the existing core action |
| `pomodoro.py` | Recovered focus/break timer and focus tracking | `plugins/pomodoro.py` |
| `quiz.py` | Recovered user-authored question set and answer grading | `plugins/quiz.py` |
| `shopping_list.py` | Recovered persistent list with check-off/quantity behavior | `plugins/shopping_list.py` |
| `telegram_remote.py` | Recovered Telegram control/relay bridge | `plugins/telegram_remote.py` |
| `water_reminder.py` | Recovered hydration goals, intake, and periodic reminders | `plugins/water_reminder.py` |
| `whatsapp_call.py` | Recovered WhatsApp voice/video call tool | `plugins/whatsapp_call.py` |
| `main_REM_fixed.py` | Recovered application entry-point snapshot, not a plugin | `main.py` is the current entry point; not compared as a plugin |

### Archive contents

`Hostinger VPS.zip` is preserved intact. Its README maps the archived files to historical project locations and describes a VPS deployment flow. The ZIP contains:

| Archive member | Finding |
|---|---|
| `Hostinger VPS/cloud_agent.py` | Historical cloud-agent plugin; no active `plugins/cloud_agent.py` |
| `Hostinger VPS/cloud.py` | Historical cloud server/client module; differs from current `core/cloud.py` |
| `Hostinger VPS/reminder.py` | Historical reminder implementation; overlaps `actions/reminder.py` |
| `Hostinger VPS/lazyimport.py` | Byte-identical to current `core/lazyimport.py` |
| `Hostinger VPS/README.txt` | Historical deployment notes; refers to JARVIS naming and old settings paths |

The archived `cloud.py` is 145,488 bytes versus 152,138 bytes for current `core/cloud.py`; they are not byte-identical. The archived `lazyimport.py` is byte-identical to current `core/lazyimport.py`. The old reminder code writes under a `.jarvis` user-data path and uses OS scheduler scripts; current `actions/reminder.py` also integrates REM routines and optional cloud mirroring. The historical archive has not been extracted into active code.

### Other archives inspected

`C:\important\Mark-LVII-main.zip`, `C:\Users\tarun\Downloads\REM--main.zip`, `REM--main (1).zip`, and `Rem.zip` were inspected by member listing. They did not provide an additional authoritative historical plugin set. `REM--main (1).zip` contains the reconstructed plugin suite; it is a comparison snapshot, not proof of original provenance. No Hostinger modules were found in those project archives.

## Recovered-copy hashes

SHA-256 of each preserved loose file:

```text
8B55603A910BADEEE564AFE1EEDF11D04864DBD7026E74A858310020A155532B  _telegram_ops.py
E100E490D91CE596B481DAAA2389E3DCCB2036DB3E037CACB2B2159811E40B1B  aircraft_report.py
18B9C67512E8B9387448BCD15FEEA4C5B7E1B61C73A05FEA898E6CF8D96B543D  calorie_counter (4).py
5103AE56F02C804701C99DB483489DB774E32AF5288E3AD018F25103F4F64203  chat_takeover.py
90499857713091961A47530B0D7D407E3C8CA6E1B11F258A9EA15E49B833A6B2  document_review.py
7A41599C1D2B067682C52379E2B2920FF6B77A5B2F6E133D5520BEE2BFC4A375  excel_writer.py
FC978027183F400683E059243AC50EB04EC9C9F7306F39809049935448A0A47E  game_updater.py
34FC55262BE0E34F741B64B69B11223AE3670C92A2E1471DEEEB5225B369147C  main_REM_fixed.py
343E6CC324E1BB0BF28191FB09C8909A7B62589201A5FCD6FEACEAAAF355F61B  pomodoro.py
4A506E2E385CD95580F33494C373877BF3B6272D8D02B8075E0B8E9BF1260EB9  quiz.py
C6592359F2325E4D3D107A530706219B7915AF78E2B7D502ED1E35B2B4CD138E  shopping_list.py
A3D3029F3082FCCFE89D601624011160F778EBF0DDA230E9579D8C62130B4B25  telegram_remote.py
06C9540F74A0F064A16E98FC3CC69B1C1129859AAEA88B9CB7BA74BDD0391400  water_reminder.py
62E5F90CDB4786DDE174D66BFA4BDA87AD4BF58CFC2B87C1692959998324C3EE  whatsapp_call.py
```

SHA-256 of preserved `Hostinger VPS.zip`: `BB0D2A300FBFBBA0EB3769117C1017CF406DF4F6FFD28F1CA4445B4DC9B975B5`.

## Missing recovered source

- No historical `_whatsapp_core.py` or `_google_oauth.py` was present in the inspected Downloads directory or project archives. The similarly named active helpers are reconstructed code, not recovered originals.
- `_telegram_ops.py` was recovered, but it is not beside the active Telegram plugin and was not installed there.
- No original printer plugin or printer helper was found. The active `printer_control.py` is reconstructed and read-only.
- No other originals for the 26 active plugins were found in the inspected sources. Absence here means absent from the supplied Downloads files, inspected project ZIPs, and Hostinger archive; it does not claim absence from every backup or VPS filesystem.

## Dependency and configuration notes

Current optional dependencies are documented in `requirements.txt` and `readme.md`; current Gmail/Calendar settings are provided by reconstructed plugin settings schemas and the OAuth helper. The current suite uses existing `memory.config_manager` storage rather than direct credential-file edits. Relevant declared packages include Google API/OAuth libraries, OpenCV, NumPy, `pywinauto`, PyWin32, MQTT, `openpyxl`, and YouTube transcript support. The recovered sources were not dependency-installed or runtime-executed during this audit. Specific differences and risks are in `PLUGIN_COMPARISON.md`.
