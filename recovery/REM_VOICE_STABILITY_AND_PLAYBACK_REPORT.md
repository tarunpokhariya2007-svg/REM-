# REM Voice Stability and Playback Report

Date: 2026-11-26  
Status: **PARTIAL — queue loss and startup announcement defects were fixed and automated checks pass; voice barge-in is implemented but not acoustically/live-session verified.**

## Findings and root causes

### Speech cutoffs and queue overflow

The Gemini Live playback queue was `asyncio.Queue(maxsize=80)`. PCM is split into 2,400-byte chunks at 24 kHz mono int16, or 50 ms/chunk, so the queue held about **4 seconds**. On overflow, `_queue_playback_audio` removed the oldest queued chunk and inserted the newest one. That policy is suitable for freshness-oriented microphone input, but it causes gaps in assistant speech when output bursts exceed playback rate. This is the confirmed code-level source of missing words under queue pressure. No per-response production/consumption trace was present to prove how often a real session reached it.

The selected Windows output stream also has a pre-existing transport check that rejects sinks consuming a 600 ms write in 0 ms. A fresh local loopback run now consumed a 0.7 second tone in 0.75 seconds. This verifies the device path's digital timing, not physical audibility or Gemini speech.

### Interruptions and echo

Voice-based barge-in was explicitly disabled in the microphone callback. Therefore the current code could not randomly stop playback due to its voice detector; UI/Emergency Stop interruption and queue loss remained possible. The existing EchoGuard already compared mic content to recently played PCM for the post-playback echo tail. The callback now applies that classifier during speech and requires a sustained positive run: normally 5 microphone blocks (~320 ms), or 12 blocks (~768 ms) when room classification is less reliable. Confirmed barge-in sends the installed SDK's `ActivityStart` event, locally advances the playback generation, drains queued old audio, then resumes sending mic PCM. Echo history reads/writes are synchronized.

Synthetic tests reject a matching playback echo and low-level noise, detect distinct speech, and verify that a single positive frame cannot trip the debounce. The callback's FFT/echo performance, false-interrupt rate in this room, genuine speech detection, and acoustic-cancellation latency have **not** been measured on live hardware. A currently in-flight device write is bounded to about 100 ms of PCM, plus the output device's own latency.

### Startup briefing

At first usable connection, the built-in startup briefing sent a greeting and concurrently fetched headlines, then sent a second news response. The no-routines fallback used the same two-phase method. Startup now schedules one greeting independently of the morning-briefing preference. A startup-triggered legacy briefing routine is ignored; scheduled or manually run briefings still use the existing news implementation. Wake-word sleep still suppresses speech while REM is asleep. Unit tests verify this routing and that the greeting code does not fetch news. A real app startup/speech test was not performed.

### Authorization refusals

The voice transcript path cleared the prior context at the start of a new voice turn and only recorded the complete text at `turn_complete`. A Gemini tool-call event may precede that marker, so its batch could capture the cleared/empty context and be correctly denied. The receiver now refreshes the immutable direct-request snapshot as each input-transcription chunk arrives. Context remains source-checked, task-bound, expiring, and fail-closed; this change does not create a session-wide authorization grant. A unit regression validates the completed transcript's navigation authorization. A real Live voice tool call was not run.

### Browser name ambiguity

Bare unknown words were silently converted to `.com`, which could send a request to a guessed unrelated site. Known site names have explicit homepage mappings; ambiguous bare names now return one clarification request. Full domains and valid URLs continue to work. This is unit-tested only; current-session browser interaction was not revalidated.

## Implemented behavior

- Playback capacity is configurable using the existing config file key `playback_buffer_seconds`. Default: **30 seconds / 600 chunks**, versus **4 seconds / 80 chunks** before. Values are clamped to 5–120 seconds. Existing unrelated settings are preserved.
- The playback buffer remains bounded. Producers await queue capacity, preserving FIFO order and applying backpressure rather than discarding the oldest active speech. The Live receive path can pause while playback catches up.
- Chunks carry a playback generation. Interruption, session replacement, and shutdown invalidate queued chunks; stale generations are discarded with a reason. Shutdown drains and closes the queue.
- Diagnostics record queue size/capacity, high-water mark, generated/enqueued/played chunks, bounded discards by reason, aggregate producer wait, mean enqueue-to-write latency, and average chunk rates. No audio contents are logged.
- A synchronized voice phase tracker covers IDLE, LISTENING, THINKING, GENERATING_AUDIO, SPEAKING, USER_INTERRUPTION, CANCELLING, and STOPPED.
- PortAudio output writes run in a worker task protected from outer task cancellation; orderly shutdown waits briefly for a blocking write and attempts stream abort if it exceeds the bounded wait.
- If a selected output device fails during a write, the uncertain in-flight batch is counted as a `device_failure` discard (it is not retried, avoiding duplicate speech), the selected stream is closed, and playback attempts the system default once for remaining chunks. A failure on the system default is surfaced as an error. This fallback is **implemented but mock fault-injection and physical device-failure recovery were not tested**.
- Device selection and its no-silent-sink transport probe were not weakened. No preferred audio device was changed.

## Tests and actual results

- Focused changed-path tests: **69 passed**.
- Full project suite: **210 passed, 0 failed** via `..\\.venv\\Scripts\\python.exe -m unittest discover -s tests`.
- Compilation: `compileall -q main.py core actions memory tests` passed.
- `git diff --check` passed with line-ending notices only.
- Stress/mock tests exercise 500 ordered chunks against a bounded eight-chunk queue, backpressure, queue close, shutdown discards, generation invalidation, echo classification, and debounce.
- Real local device loopback: **PASS for digital loopback only**. Output: Speaker / Headphone (Realtek); input: Microphone Array (Realtek); 16,800 samples over 0.7 s; write took 0.75 s; 14,400 loopback samples; 660 Hz amplitude 195.0; raw audio not saved.
- Physical speaker/headphone audibility, REM Live audio response playback, Live voice recognition/barge-in, actual TTS synthesis/playback, and long-duration real response continuity: **NOT VERIFIED**.

## Changed files

Implementation: `main.py`, `core/audio_playback.py`, `core/voice_state.py`, `core/echo.py`, `core/routines/engine.py`, `memory/config_manager.py`, `actions/browser_control.py`.

Tests: `tests/manual_async.py`, `tests/test_audio_playback.py`, `tests/test_startup_greeting.py`, `tests/test_desktop_control_authorization.py`, `tests/test_desktop_hardening_paths.py`, `tests/test_screen_observation.py`.

No dependency was installed. `memory/routines.json` and credentials/config secrets were not modified. Nothing was staged, committed, or pushed.
