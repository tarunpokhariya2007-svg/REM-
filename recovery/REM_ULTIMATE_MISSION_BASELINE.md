# REM Ultimate Mission Baseline

Date: 2026-11-25  
Scope: evidence recorded before the latest implementation changes in this mission.

## Repository and environment

- Branch: `main`, HEAD `f68d061`, tracking `origin/main`.
- Worktree was already dirty at entry. It includes prior desktop/browser/security/observation work and a pre-existing edit to `memory/routines.json`; no reset, cleanup, stage, commit, or push was performed.
- `LICENSE` is present as an untracked restoration and matches the parent commit's 417 lines. `config/api_keys.json` is not listed as changed.
- Python: 3.12.10 x64; `google-genai 2.34.0`, Playwright 1.65.2, PyWinAuto 0.6.9, RapidOCR 3.10.0, ONNX Runtime 1.32.0, `nvidia-ml-py 13.615.71`, deprecated `pynvml 13.0.1` are installed. Pytest is absent; the project uses `unittest` discovery.
- Registries currently discover 8 inline tools, 20 actions, and 27 plugins (55 total); plugin loader reports 27 active, 0 rejected.
- Initial regression baseline: **164 tests passed** in 7.135 seconds. The run emits the `pynvml` deprecation warning and an expected malformed-temporary-memory-fixture diagnostic.

## Capability baseline

| Workstream | Current evidence and status |
|---|---|
| Authorization | **PARTIAL** — unit tests cover task-bound nested browser/desktop authorization, expiry, missing/unrelated contexts and interleaving. `_execute_tool()` has already been changed to deny omitted context. No fresh real Live voice tool call is available. |
| Natural conversation | **PARTIAL** — `core/prompt.txt` describes REM's personality and outcome rules exist, but there is no central sanitation/guidance layer for all legacy tool errors. Model speech cannot be inferred from prompt tests alone. |
| Browser | **PARTIAL/BLOCKED** — browser code supports Chrome and Edge Playwright channels, profile-lock fallback, DOM targets, post-navigation checks and hidden-page refusal. Prior real Chrome CDP evidence found a hidden document and different foreground PID. No real Edge search/result acceptance is established. |
| Gemini Live | **NOT VERIFIED for voice-to-tool** — historical real mic attempts streamed frames but did not confirm transcript, tool call, authorized harmless action, tool result, or response audio. SDK is currently installed. |
| Audio/TTS | **PARTIAL** — prior real digital Stereo Mix tone measurement passed; physical audibility, real Gemini playback and local TTS synthesis remain unverified/unavailable. |
| Keyboard | **PARTIAL** — historical disposable fixture passed Unicode entry/readback and a subset of navigation/focus operations. Full keyboard matrix is incomplete. |
| Mouse | **PARTIAL** — prior evidence exists, but the fresh Windows mouse runner failed absolute cursor verification: requested `(1018,472)`, actual `(865,609)`. Direct Win32 `SetCursorPos` from this process returned Access Denied. The runner's token reported AppContainer=true. No clicks were attempted after the coordinate failure. |
| UI Automation | **PARTIAL** — earlier real acceptance record passed Edit/ValuePattern, Button/InvokePattern, text readback, status and list update, and checkbox Toggle. A fresh run launched the disposable fixture but Windows kept Chrome foreground, so the runner refused to act. List-item identity and ComboBox expand/selection patterns remain incomplete. |
| Local observation | **PARTIAL** — persistent setting and cloud separation have unit coverage; earlier real MSS/OCR/UIA tests are documented. Spoken end-to-end observation remains unverified. |
| Security | **PARTIAL** — risk manifest covers the current 55 tools and unknown tools fail closed. Tests are representative; exhaustive 20-scenario audit is not established. |
| Qt lifecycle/shutdown | **PARTIAL** — a queued-signal repair exists and cancellation is classified, but repeated real startup/shutdown and shutdown-during-active-work are untested. |
| Memory/routines | **PARTIAL** — persistence/corruption recovery tests exist; existing routine changes were present at task start and preserved. No data migration was attempted. |
| Plugins/actions | **PASS discovery / PARTIAL behavior** — 20 actions and 27 plugins load in this environment. Offline regression passes; real external integrations and cancellation coverage remain incomplete. |
| Dependencies | **PARTIAL** — `requirements.txt` now uses `nvidia-ml-py`, but both it and deprecated `pynvml` are installed, so the warning persists. No environment package was added or removed. |

## Fresh real-Windows probes at mission start

- Disposable mouse runner: **FAIL** at absolute coordinate verification. The actual cursor did not reach the requested point. Win32 `SetCursorPos` reported access denied; the observed Python token was AppContainer. This is a specific environment/security boundary; no bypass was attempted.
- Disposable UIA runner: **BLOCKED** because the test fixture did not become the foreground window; the test refused semantic actions. Earlier same-fixture acceptance remains recorded in `rem_uia_adapter_acceptance.jsonl` and is not erased by the blocked rerun.
- Disposable Chrome/CDP runner: **BLOCKED** at startup. Chrome exited with a Crashpad named-pipe error (`TransactNamedPipe: The pipe has been ended`; crash server failed to launch). The harness was changed in this mission to capture the reason and terminate only its own temporary-profile process after startup failure.
- No personal browser profile, unsaved document, external account, or user's memory data was intentionally modified.

## Regressions versus previous baseline

The initial automated suite remains at its reported 164-test pass. The fresh real mouse test reveals that prior mouse acceptance is not reproducible in the current AppContainer/desktop session; this is a real acceptance regression/blocker, not a unit-test regression. The fresh UIA test is blocked by foreground ownership, and the fresh Chrome test is blocked by Crashpad startup. No evidence establishes whether those limitations are specific to the execution container or the user's normal unsandboxed Windows session.
