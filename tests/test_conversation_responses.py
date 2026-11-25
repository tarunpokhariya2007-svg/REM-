import unittest

from core.conversation import format_tool_result, tool_result_guidance


class ToolConversationGuidanceTests(unittest.TestCase):
    def test_verified_result_requests_brief_natural_confirmation(self):
        guidance = tool_result_guidance("status=done action=go_to verified=true")
        self.assertIn("short, warm confirmation", guidance)
        self.assertIn("verified result", guidance)

    def test_unverified_result_forbids_success_claim_and_user_workaround(self):
        guidance = tool_result_guidance("status=unverified action=click")
        self.assertIn("could not confirm", guidance)
        self.assertIn("Do not imply success", guidance)
        self.assertIn("do not ask the user to perform", guidance)

    def test_failed_result_avoids_internal_terms_and_repeated_apology(self):
        guidance = tool_result_guidance("status=failed action=browser_control")
        self.assertIn("at most once", guidance)
        self.assertIn("implementation details", guidance)
        for term in ("coordinates", "OCR", "UI Automation", "CDP"):
            self.assertNotIn(term, guidance)

    def test_started_result_does_not_claim_completion_or_poll(self):
        guidance = tool_result_guidance("status=started action=open_app")
        self.assertIn("without claiming completion", guidance)
        self.assertIn("do not call the tool again", guidance)

    def test_unknown_legacy_text_still_requires_truthful_plain_speech(self):
        guidance = tool_result_guidance("Browser error (TimeoutError)")
        self.assertIn("Never claim the action succeeded", guidance)
        self.assertIn("internal automation details", guidance)

    def test_low_level_browser_failure_is_sanitized_before_model_receives_it(self):
        safe = format_tool_result(
            "browser_control",
            "Browser error (go_to): Playwright TimeoutError at selector #password",
        )
        self.assertEqual(
            safe,
            "status=failed action=browser_control "
            "reason=the requested action could not be completed",
        )
        self.assertNotIn("Playwright", safe)
        self.assertNotIn("password", safe)


if __name__ == "__main__":
    unittest.main()
