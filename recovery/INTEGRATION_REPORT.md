# REM Plugin Recovery Integration Report

Date: 2026-11-24  
Status: safe local integrations completed; no external integrations activated.

## Recovered and preserved

- Preserved 14 loose Python files under `recovery/original_plugins/loose/` and the complete `Hostinger VPS.zip` under `recovery/original_plugins/source_archives/`. The manifest records SHA-256 values; copies were verified against their source files.
- Original `_whatsapp_core.py`, `_google_oauth.py`, and printer source were not found in the inspected inputs. Their current files are reconstructed and were not represented as originals.
- The Hostinger archive contains `cloud_agent.py`, `cloud.py`, `reminder.py`, `lazyimport.py`, and historical README. Its `lazyimport.py` matches current core source byte-for-byte; `cloud.py` differs and was not restored.
- The recovered Telegram plugin and `_telegram_ops.py` remain inactive as a coupled pair. Historical cloud/VPS instructions were not followed and no server was contacted.

## Integrated functionality

1. **Aircraft tracking:** Added recovered aircraft reporting as the independent `aircraft_report` plugin. It supports nearest/overhead/list/details and a user-started, stoppable proximity watch. It uses current REM plugin settings, tool schema, player UI, outcome format, and untrusted-content boundary. The configured location is disclosed as leaving the machine to public lookup/ADS-B services. The implementation uses the current adsb.fi v3 endpoint, an allowlist of HTTPS hosts, request timeouts, per-host pacing, 429 backoff, a conservative local adsb.lol daily budget, 180-second minimum watch cadence, local cache/usage paths, attribution, and generic log messages. No external request was made in development or tests. Data sources and current limits are documented in README; adsb.fi terms limit use to personal/non-commercial, adsb.lol documents a 500/day anonymous quota, and public Nominatim requires a descriptive User-Agent, caching, and at most one request/second. See [adsb.lol API docs](https://www.adsb.lol/docs/open-data/api), [adsb.fi API terms and limits](https://github.com/adsbfi/opendata), and [Nominatim usage policy](https://operations.osmfoundation.org/policies/nominatim/).
2. **Shopping list:** Added multi-item comma-separated adds, bought/check state, clear-bought behavior, and compatibility migration from the reconstructed row format. Existing add/list/remove/clear actions and plugin storage location remain.
3. **Dependency-install safety:** Removed unattended `pip install` calls from the core development agent. Model-proposed dependency lists now produce a clear “not installed; explicit approval required” result, and missing-import errors stop for user approval rather than attempting installation. No dependency was installed.

Other recovered plugin copies were retained for comparison. The existing current plugin or core implementation remains active for chat takeover, WhatsApp, Telegram, OAuth, Google Calendar/Gmail, document review, Excel, games, camera analysis, Pomodoro, quiz, hydration, and printer telemetry. No original code was copied over those active features.

## Security and compatibility decisions

- The plugin loader, action/plugin dispatch, Gemini Live voice pipeline, screen observation, desktop authorization, emergency-stop system, current configuration, and original helper files were not replaced.
- The aircraft tool was validated with the real loader and is a distinct name with no action/tool collision. It makes no network call unless the user configures a location and requests a lookup or starts the settings test/watch.
- Telegram remote media/file transfer, WhatsApp call execution, cloud-agent integration, automatic chat takeover, printer job control, and legacy OS reminder scheduling were not activated because they would widen authority or need unavailable helpers/protocol review.
- Static review found additional existing follow-up risks that this scoped recovery pass did not rewrite: confirmation completion is logged but not returned to Gemini; some configured service URLs and file paths rely on the user’s settings being trustworthy; and model-generated code execution remains within the development tool’s requested project scope but is not an OS sandbox. These are documented in `FEATURE_GAP_ANALYSIS.md` and require focused follow-up work.
- The pre-existing `memory/routines.json` worktree change was preserved and not edited. No credential/configuration file was read for secrets or modified.

## Files changed in this recovery pass

- `plugins/aircraft_report.py` (new)
- `plugins/shopping_list.py`
- `actions/dev_agent.py`
- `.gitignore`
- `readme.md`
- `tests/test_reconstructed_plugins.py`
- `tests/test_recovered_plugin_adapters.py` (new)
- `recovery/RECOVERY_MANIFEST.md`
- `recovery/PLUGIN_COMPARISON.md`
- `recovery/INTEGRATION_PLAN.md`
- `recovery/FEATURE_GAP_ANALYSIS.md` (new)
- `recovery/INTEGRATION_REPORT.md` (new)
- `recovery/integration_backup/before_aircraft_and_utility_adapters/` (backups of pre-integration tracked files)

Screen-observation changes and the existing modified `memory/routines.json` were present before this recovery integration and were not modified by it.

## Validation

- Current loader discovery through `main.py` tool/action reservations: **27 plugins valid, 27 enabled, 0 rejected**.
- Focused regression suite: **62 tests passed** across reconstructed plugins, screen observation, screen-agent behavior, desktop authorization, and recovered adapters.
- Python compilation: passed for modified Python files.
- `git diff --check`: passed (Git emitted line-ending conversion notices only).
- Recovered-copy and archive hashes: match recorded source hashes.
- External-service, Telegram, WhatsApp, OAuth, camera, printer, real Gemini Live, and real aircraft-feed tests: **not performed**. All integration tests are offline/mocked. No real-world side effect was performed.

## Remaining blockers and next steps

- The aircraft feature needs a user-configured location and live service availability; regional receiver coverage is not guaranteed. Public API limits and terms can change.
- Recovered WhatsApp calling cannot be restored until the original driver is found or an explicitly reviewed current driver is implemented and verified against the exact active contact.
- Recovered cloud code needs a public-contract/protocol diff and fake-transport tests before any adapter. No Hostinger access is implied or authorized by the recovered README.
- Recovered Telegram capabilities need a separate threat model and regression suite for sender identity, pairing, replay/rate limits, central authorization, confirmation, emergency stop, interruption, worker shutdown, and screenshot/camera/file privacy.
- Confirmation-result delivery back into the active assistant turn, trusted-service URL validation, arbitrary file-path policy, subprocess cancellation, and OS-level isolation for generated code remain follow-up items.
- A real aircraft smoke test requires explicit permission to send the configured location to public endpoints. A real messaging/call/service test requires separately approved test accounts and targets.
