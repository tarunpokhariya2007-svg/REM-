import unittest
import threading
import time

from core.desktop_control import authorize_user_request
from core.request_context import RequestContext, REQUEST_CONTEXT_TTL


class DesktopControlAuthorizationTests(unittest.TestCase):
    def allow_observation(self, request, tool="screen_process", args=None, source="voice"):
        allowed, reason = authorize_user_request(
            request, tool, args if args is not None else {"text": "What is visible?"},
            source=source,
        )
        self.assertTrue(allowed, f"{request!r} was denied: {reason}")

    def test_spoken_observation_phrases_and_question_paraphrase(self):
        for request in (
            "Check my screen.", "What is visible?", "Describe my display.",
            "Which application is open?", "Read the error in my React terminal.",
            "Look at my VS Code window.", "What is written in this window?",
            "REM, read the current test window.",
        ):
            with self.subTest(request=request):
                # The tool's generated query intentionally need not match the utterance.
                self.allow_observation(request, args={"text": "What application is open?"})

    def test_hindi_and_transliterated_hindi_observation(self):
        for request in (
            "मेरी स्क्रीन देखो", "स्क्रीन पर क्या दिख रहा है", "विंडो में क्या लिखा है",
            "screen par kya dikh raha hai", "meri screen dekho", "kaun sa app khula hai",
        ):
            with self.subTest(request=request):
                self.allow_observation(request)

    def test_screen_agent_observe_is_read_only_and_needs_direct_intent(self):
        self.allow_observation(
            "Read the error visible in my terminal", "desktop_ui",
            {"action": "observe", "text": "What does the error say?"},
        )
        denied = authorize_user_request(
            "Send this file to Alex", "desktop_ui", {"action": "observe"},
            source="voice",
        )
        self.assertFalse(denied[0])

    def test_untrusted_or_stale_request_cannot_authorize_observation(self):
        for source in ("untrusted", "tool", "screen", "email", "telegram"):
            with self.subTest(source=source):
                allowed, _ = authorize_user_request(
                    "Check my screen", "screen_process", {"text": "Describe this"},
                    source=source,
                )
                self.assertFalse(allowed)
        allowed, _ = authorize_user_request(
            "Turn the volume down", "screen_process", {"text": "Check screen"},
            source="typed",
        )
        self.assertFalse(allowed)

    def test_unknown_tool_and_invalid_camera_request_are_denied(self):
        self.assertFalse(authorize_user_request(
            "Check my screen", "invented_tool", {}, source="voice")[0])
        self.assertFalse(authorize_user_request(
            "Check my screen", "screen_process", {"angle": "camera"},
            source="voice")[0])
        self.assertTrue(authorize_user_request(
            "Look at the webcam", "screen_process", {"angle": "camera"},
            source="voice")[0])

    def test_click_and_type_keep_target_and_payload_authorization(self):
        self.assertTrue(authorize_user_request(
            "Click the Save button", "desktop_ui",
            {"action": "click", "target": "Save"}, source="typed")[0])
        self.assertFalse(authorize_user_request(
            "Check my screen", "desktop_ui",
            {"action": "click", "target": "Delete"}, source="voice")[0])
        self.assertTrue(authorize_user_request(
            "Type hello in the search box", "desktop_ui",
            {"action": "type", "target": "search box", "text": "hello"},
            source="dashboard")[0])
        self.assertFalse(authorize_user_request(
            "Type hello in the search box", "desktop_ui",
            {"action": "type", "target": "search box", "text": "delete all"},
            source="dashboard")[0])

    def test_request_context_has_task_identity_and_expires(self):
        context = RequestContext.create("Check my screen", "voice", now=10)
        self.assertTrue(context.task_id)
        self.assertEqual(context.age(now=12), 2)
        self.assertFalse(context.is_expired(now=12))
        self.assertTrue(context.is_expired(now=10 + REQUEST_CONTEXT_TTL))

    def test_live_tool_batch_keeps_its_context_when_new_request_arrives(self):
        from main import RemLive

        rem = RemLive.__new__(RemLive)
        rem._request_context_lock = threading.Lock()
        rem._dispatch_context_local = threading.local()
        rem._record_user_request("Open YouTube", "voice")
        batch_context = rem._current_request_context()

        # A different direct request may arrive while the first tool worker is
        # running. Nested plugin routes must continue to use the batch snapshot.
        rem._record_user_request("Turn the volume down", "dashboard")
        seen = rem._run_with_request_context(
            batch_context,
            lambda: rem._usable_request_context(rem._bound_request_context()))
        self.assertEqual(seen, ("Open YouTube", "voice"))
        self.assertNotEqual(rem._current_request_context().task_id,
                            batch_context.task_id)

    def test_expired_live_context_is_untrusted(self):
        from main import RemLive

        rem = RemLive.__new__(RemLive)
        expired = RequestContext.create("Check my screen", "voice",
                                        now=time.monotonic() - REQUEST_CONTEXT_TTL - 1)
        self.assertEqual(rem._usable_request_context(expired), ("", "untrusted"))

    def test_contextvar_dispatch_isolated_between_concurrent_tasks(self):
        from concurrent.futures import ThreadPoolExecutor
        from main import RemLive

        rem = RemLive.__new__(RemLive)
        import contextvars
        rem._dispatch_context_local = contextvars.ContextVar("parallel_task_ctx", default=None)
        a = RequestContext.create("Open YouTube", "voice")
        b = RequestContext.create("Open Notepad", "dashboard")
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(rem._run_with_request_context, ctx,
                                   lambda: rem._bound_request_context().task_id)
                       for ctx in (a, b)]
            a_id, b_id = (future.result(timeout=5) for future in futures)
        self.assertNotEqual(a_id, b_id)

    def test_nested_routes_keep_expired_context_expired(self):
        from main import RemLive

        rem = RemLive.__new__(RemLive)
        rem._dispatch_context_local = threading.local()
        expired = RequestContext.create(
            "Open YouTube", "voice", now=time.monotonic() - REQUEST_CONTEXT_TTL - 5)
        seen = rem._run_with_request_context(
            expired, lambda: rem._usable_request_context(rem._bound_request_context()))
        self.assertEqual(seen, ("", "untrusted"))


if __name__ == "__main__":
    unittest.main()
