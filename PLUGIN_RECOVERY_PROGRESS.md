# Plugin reconstruction progress

This file tracks the requested reconstruction while preserving the pre-existing worktree edits.

## Existing implementation reused

- `screen_agent.py`: plugin wrapper over `actions.screen_agent` with bounded task steps and existing tool routing.
- The project already implements core equivalents for code helper, development agent, YouTube, flight search, game updates, browser control, file processing, email messaging, reminders, and desktop actions. Name collisions prevent duplicate plugin registrations for core tool names.

## Requested plugin files

- [x] `screen_agent.py` (supports bounded steps and safe delegation to existing tools)
- [x] `_whatsapp_core.py` (UIA-first transport; refuses ambiguous contacts)
- [x] `whatsapp_call.py` (confirmation before call; end-call state remains unverified)
- [x] `whatsapp_guard.py` (visible-window check; no background monitor)
- [x] `chat_takeover.py` (delegates message send through existing confirmation gate)
- [x] `telegram_remote.py` (cloud caller-compatible poller and paired chat allowlist)
- [x] `gmail_control.py` (OAuth; send confirmation)
- [x] `calendar_control.py` (OAuth; confirmed create/update/delete and overlap check)
- [x] `obsidian_vault.py` (configured vault; overwrite requires confirmation)
- [x] `document_review.py` (read-only file processor adapter)
- [x] `excel_writer.py` (create or edit to a new copy; formula restrictions)
- [x] `code_helper.py` (routes to existing core action as `code_helper_plugin`)
- [x] `dev_agent.py` (routes to existing core action as `dev_agent_plugin`)
- [x] `image_generator.py` (configured OpenAI-compatible image endpoint)
- [x] `upload_video.py` (OAuth; confirmation before upload)
- [x] `youtube_video.py` (routes to existing core action as `youtube_video_plugin`)
- [x] `printer_control.py` (MQTT telemetry only; no job commands)
- [x] `home_assistant.py` (read plus confirmed light/switch control)
- [x] `game_updater.py` (routes to existing core action as `game_updater_plugin`)
- [x] `flight_finder.py` (routes to existing core action as `flight_finder_plugin`)
- [x] `shopping_list.py` (local JSON store)
- [x] `pomodoro.py` (in-process timer)
- [x] `water_reminder.py` (in-process optional reminder timer and intake log)
- [x] `quiz.py` (model-generated quiz, displayed in UI)
- [x] `spaced_repetition.py` (local SM-2 schedule)
- [x] `calorie_counter.py` (vision estimate; camera capture confirmed)
- [x] `pushup_counter.py` (bounded confirmed camera session)

## Validation

- [x] Existing screen-agent compilation, discovery, and offline tests.
- [x] Full plugin declaration/discovery suite.
- [x] Offline plugin tests: 21 passed, including schema checks, discovery/collision checks, and safety-path cases.
- [x] `python -m compileall -q plugins tests` and `git diff --check` (exit 0; Git emitted a line-ending notice for the already-modified README).
- [x] `python -m core.screenview` and `python -m core.screenact` self-tests (both exit 0; live UIA read degraded because `win32ui` could not load).
- [ ] `providers._tests`: attempted twice but stalled in its first `test_real_caller_shapes` test; terminated after bounded waits. No passing result claimed.
- [x] README plugin documentation.
