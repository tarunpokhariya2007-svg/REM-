# REM Runtime Error Audit

Date: 2026-11-25  
Overall: **PARTIAL** — the attached prompt did not include a separate runtime-log artifact. Findings below distinguish code/test evidence from historical Windows evidence; no Live failure is attributed to an unobserved event.

## Findings and status

| Issue | Root cause / evidence | Fix and verification | Status |
|---|---|---|---|
| `missing_or_unrelated_context` | The request context is normally snapshotted at Live tool-call receipt and carried into the batch. Nested plugin dispatch reads that bound snapshot. However, `_execute_tool()` still treated an omitted context as permission to use the latest mutable global request. That was a real isolation defect, although it does not by itself prove the cause of each historical refusal. | Omitted and explicit-missing contexts now fail closed; the latest request cannot authorize the old call. Added a regression test. Existing tests cover authorized nested browser/desktop actions, unrelated/missing/expired contexts and interleaved batches. | **PASS (unit)**; real Live reproduction **NOT VERIFIED** |
| `request_not_authorized` | `authorize_user_request` compares the originating user text with the dispatched action and target. A genuine mismatch, absent/late transcript, or action not named by the user is intentionally refused. Without the missing log artifact and a Live tool-call trace, the reported instance cannot be distinguished among these cases. | Fail-closed authorization retained. Safe task/category logging does not print the request text. | **UNRESOLVED** |
| `browser_control status=unverified` | A prior real disposable-profile run found the CDP target and URL, but its document reported `visibilityState=hidden`; another PID owned the foreground, and Windows refused foreground activation. URL/DOM inspection did not establish visible interaction. | Existing code checks URL, readiness and visibility; hidden pages are refused. Unit tests cover this guard and semantic result identity. | **PARTIAL**; visible-page real search/selection **BLOCKED** |
| `search bar` not identified | The existing pipeline prioritizes DOM/UIA and then local accessibility/OCR, but actual browser input is correctly blocked while the CDP document is hidden. No fresh visible-page runtime trace was supplied. | Semantic generic-search resolution and ambiguity/disabled guards have unit coverage. | **PARTIAL**; real visible-page result **NOT VERIFIED** |
| Shutdown `CancelledError` / “wrap-up cut short” | `request_shutdown()` cancels the Live run task. `finish()` concurrently submits a cleanup coroutine, which cancels other loop tasks and saves the session summary. If the loop/future is cancelled before this finishes, a `CancelledError` can surface as a generic wrap-up error. Historical trace was not attached, so the exact race was not reproduced. | `CancelledError` is now classified separately as expected cancellation in sanitized structured diagnostics; summary cancellation is reported and propagated instead of swallowed. Cleanup completion still needs a repeated real Qt shutdown test. | **PARTIAL** |
| `host API reports success but moves no audio (0 ms for 600 ms)` | Windows DirectSound accepted the write but consumed 600 ms of PCM in ~0 ms, so it was a silent/non-consuming sink. This failure is directly tested by the transport-time probe. | Device enumeration rejects the sink and selects an endpoint/API that consumes audio. A separate real tone passed Stereo Mix loopback (14,400 samples; 660 Hz component above threshold). Physical acoustic output remains unverified. | **PASS (sink detection/digital loopback)**; physical playback **PARTIAL** |
| `FutureWarning` from `pynvml` | The project imported the deprecated distribution. The environment has both `pynvml 13.0.1` and `nvidia-ml-py 13.615.71`; both expose the `pynvml` import name, and the deprecated package currently triggers the warning. | `requirements.txt` now names NVIDIA's maintained `nvidia-ml-py` distribution; Python import names remain `pynvml`. No package was installed or removed. The warning still occurs in this already-populated environment until the conflicting deprecated distribution is removed in an approved environment-maintenance step. | **PARTIAL** |
| `Routine skipped — missed by N min` | The scheduler compares a time trigger with the routine's configured grace window and advances/skips when it is late. This message represents an intended expired occurrence, not a thrown exception. | No routine data or scheduler policy changed. | **INFO / EXPECTED** |

## Diagnostics and privacy changes

- Added `core/error_reporting.py`: structured event ID, component, operation, task ID, category, exception type, recovery and final status. Exception messages and tracebacks are omitted because they may contain credentials, transcript text or URLs.
- Tool dispatch, Live receive/batch and shutdown failure paths now use the structured record. Expected cancellation is informational; timeout/connection errors are recoverable; `MemoryError` is critical; other exceptions are functional failures.
- Console tool-result logging now records only the result length, not the result text. `save_memory` no longer prints the stored value. Generic failure responses and screen-capture failure reports omit raw exception messages.
- User-facing conversation history remains a product feature; this change does not save raw microphone audio. Live transcript/tool success was not established in this pass.

## LICENSE and repository state

Commit `f68d061` contains an explicit 417-line deletion of `LICENSE`, with no commit message explanation. The current untracked `LICENSE` matches all 417 parent-commit lines, preserving the historical contents. Its line endings differ from the parent blob representation, but line content matches. The deletion's intent cannot be proven from Git metadata; prior project documentation and the preserved exact copy support keeping the restoration. No memory, credential or routine data was edited during this continuation.

## Checks

- Prior baseline/full suite before this continuation: 159 tests passed.
- Focused suite after changes: 33 tests passed.
- New tests cover safe error records, cancellation classification, omitted-context denial and canonical observation-setting precedence.
- The post-change full suite passed **164 tests**; `python -m compileall -q .` and `git diff --check` passed. The suite emitted the known `pynvml` deprecation warning from the pre-existing venv.
- No dependencies were installed/removed. No stage, commit or push was performed.

## Latest mission continuation — 2026-11-25

Current suite: **171 passed, 0 failed**. `compileall -q .` and `git diff --check` passed. Added a central, tested response-result formatter/guidance layer. Added explicit loopback-only Edge CDP configuration alongside Chrome and hardened the disposable Chrome harness cleanup/diagnostics.

Fresh acceptance deltas: mouse absolute movement failed in this AppContainer execution context (coordinate mismatch plus Win32 Access Denied); the UIA runner safely refused because the test window was not foreground; Chrome failed at temporary-profile Crashpad startup before CDP; Edge is not verified. Earlier real UIA, screen/OCR, CDP DOM, and digital audio loopback results remain historical evidence but are not upgrades to these fresh blocked attempts. Live voice-to-tool and audible playback remain unverified.

The `pynvml` warning persists because the existing environment has both deprecated `pynvml` and maintained `nvidia-ml-py` distributions. No dependency was installed or removed. The pre-existing routine file modification remains untouched by this continuation.
