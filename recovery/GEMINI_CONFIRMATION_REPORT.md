# Gemini Confirmation Report

Date: 2026-11-24

- Normal Live tool calls return `types.FunctionResponse` through `send_tool_response()`; screen image/question input uses the same active session through `send_realtime_input`.
- A UI confirmation terminal result is forwarded as a text system event via `send_realtime_input(text=...)` while the originating session is active, serialized by the realtime-input lock. Stale/replaced sessions are rejected. The event says it is not new user intent and authorizes no additional action.
- Unit tests cover fake-session result delivery and stale-session refusal. No live Gemini confirmation approval/denial/expiry was performed, so actual receipt and speech remain unverified.
- The function response returned before a deferred confirmation is resolved. The later outcome is a realtime event, not a retroactive function response; the model is instructed not to call tools in response to that event.
