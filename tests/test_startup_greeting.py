import unittest
from unittest.mock import patch
from tests.manual_async import run_ready


class StartupGreetingTests(unittest.TestCase):
    def test_greeting_is_sent_once_and_does_not_fetch_or_announce_news(self):
        import main

        class Session:
            def __init__(self): self.sent = []
            async def send_client_content(self, **kwargs): self.sent.append(kwargs)

        rem = main.RemLive.__new__(main.RemLive)
        rem.session = Session()
        rem._startup_greeting_sent = False
        rem._turn_done_event = None
        with patch.object(main, "load_memory", return_value={"identity": {}}), \
             patch.object(main, "_fetch_news_sync", side_effect=AssertionError("startup must not fetch news")):
            run_ready(rem._send_startup_greeting())
            run_ready(rem._send_startup_greeting())
        self.assertEqual(len(rem.session.sent), 1)
        prompt = rem.session.sent[0]["turns"]["parts"][0]["text"].casefold()
        self.assertIn("ready to help", prompt)
        self.assertIn("do not announce news, markets", prompt)

    def test_legacy_startup_briefing_routine_is_suppressed(self):
        import main

        rem = main.RemLive.__new__(main.RemLive)
        rem._loop = object()
        rem.session = object()
        with patch("asyncio.run_coroutine_threadsafe") as schedule, \
             patch.object(main.RemLive, "_send_startup_greeting", return_value=None) as greet, \
             patch.object(main.RemLive, "_send_startup_briefing", return_value=None) as briefing:
            rem._routine_builtin("briefing", {"_trigger_type": "startup", "news": True})
            schedule.assert_not_called()
        greet.assert_not_called()
        briefing.assert_not_called()

    def test_scheduled_or_explicit_briefing_keeps_existing_news_path(self):
        import main

        rem = main.RemLive.__new__(main.RemLive)
        rem._loop = object()
        rem.session = object()
        options = {"_trigger_type": "time", "news": True}
        with patch("asyncio.run_coroutine_threadsafe") as schedule, \
             patch.object(main.RemLive, "_send_startup_greeting", return_value=None) as greet, \
             patch.object(main.RemLive, "_send_startup_briefing", return_value=None) as briefing:
            rem._routine_builtin("briefing", options)
            schedule.assert_called_once()
            schedule.call_args.args[0].close()
        briefing.assert_called_once_with({"news": True})
        greet.assert_not_called()


if __name__ == "__main__":
    unittest.main()
