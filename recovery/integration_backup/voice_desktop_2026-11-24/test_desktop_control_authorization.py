import unittest

from core.desktop_control import authorize_user_request


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


if __name__ == "__main__":
    unittest.main()
