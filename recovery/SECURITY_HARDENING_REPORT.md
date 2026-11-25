# Security Hardening Report

Date: 2026-11-24

## Changes

- Chrome CDP is opt-in through `REM_CHROME_CDP_URL`; endpoint validation limits it to an explicitly supplied loopback host and port. REM does not open a debugging listener. CDP attach failure is reported as failure without an automatic target switch.
- A connected Chrome session's default context is not closed when REM disconnects.
- Navigation through `go_to` and URL-bearing `new_tab` must name the destination host and non-default port in the direct user request. Browser content remains outside the direct request context.
- Only credential-free HTTP(S) page URLs are accepted by browser normalization. Local development sites on loopback remain available when named in the request.
- Confirmation outcomes are returned to the still-active assistant session without putting action result text or exception details in the local log. The injected message explicitly says it is not a new request or authorization and directs the assistant not to call tools.
- The development agent no longer runs the planner's Python command as a side effect of creating a project. It shows the exact command/folder in the HUD confirmation, validates a project-contained `.py` script, uses no shell, enforces a bounded timeout and central cancellation callback, and withholds captured process output from Gemini.

## Preserved controls

- The existing plugin/action loaders and Gemini Live function response path remain in place.
- Browser and native operations still use `core.desktop_control.action()` and its shared action lock/pause/stop checks.
- The existing emergency-stop mechanism was not bypassed or reset.
- Screen content remains untrusted; observation prompts explicitly disallow clicking, typing, commands or file changes.
- Existing confirmation remains user-interface controlled; Gemini cannot approve by writing a tool argument.
- No screenshot was persisted or sent to an external service during local runtime checks. No dependency was installed.

## Remaining security limits

- This does not yet implement a comprehensive redirect/DNS rebinding/private-network policy across every browser operation.
- Confirmation outcome injection and CDP behavior have only mocked tests, not a live session.
- Existing word-based request authorization has not been replaced with unique request IDs and a formal policy engine.
- Generated code is not OS sandboxed; file path policy, subprocess cancellation, and Telegram replay/rate-limit proofs remain incomplete as recorded in `FEATURE_GAP_ANALYSIS.md`.
- The new-project run gate is an informed command/folder approval, not code review or sandboxing. Approved code retains the process user's OS permissions and may access files/network. Output is not evaluated by Gemini, so exit code zero is not a proof of correct behavior.
- Real Chrome attachment is a high-authority local capability. A configured debugging endpoint must remain loopback-only and under the user's control.
