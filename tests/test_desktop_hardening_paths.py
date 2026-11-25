import asyncio
import threading
import time
import unittest
from unittest.mock import patch

from actions import browser_control
from core import confirm, desktop_control


def _run_immediate(coroutine):
    try:
        coroutine.send(None)
    except StopIteration as finished:
        return finished.value
    raise AssertionError("test coroutine unexpectedly waited for external I/O")


class ChromeCdpConfigurationTests(unittest.TestCase):
    def test_endpoint_is_opt_in_and_loopback_only(self):
        self.assertIsNone(browser_control._configured_chrome_cdp_endpoint("") )
        for endpoint in (
            "http://127.0.0.1:9222",
            "http://localhost:9222",
            "ws://[::1]:9222/devtools/browser/test",
        ):
            with self.subTest(endpoint=endpoint):
                self.assertEqual(
                    browser_control._configured_chrome_cdp_endpoint(endpoint), endpoint)
        for endpoint in (
            "http://192.168.1.4:9222",
            "http://example.com:9222",
            "http://user:pass@127.0.0.1:9222",
            "http://127.0.0.1:9222/?token=secret",
            "file:///tmp/chrome",
        ):
            with self.subTest(endpoint=endpoint), self.assertRaises(ValueError):
                browser_control._configured_chrome_cdp_endpoint(endpoint)

    def test_browser_urls_reject_unsafe_schemes_and_keep_local_dev_urls(self):
        for url in ("javascript:alert(1)", "file:///C:/secret.txt",
                    "data:text/html,hello", "https://user:pass@example.com"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                browser_control._normalize_url(url)
        self.assertEqual(browser_control._normalize_url("youtube.com"),
                         "https://youtube.com")
        self.assertEqual(browser_control._normalize_url("localhost:5173"),
                         "http://localhost:5173")

    def test_navigation_host_and_port_must_be_in_direct_request(self):
        self.assertTrue(desktop_control.authorize_user_request(
            "Open YouTube", "browser_control",
            {"action": "go_to", "url": "https://youtube.com"}, source="voice")[0])
        self.assertFalse(desktop_control.authorize_user_request(
            "Open YouTube", "browser_control",
            {"action": "go_to", "url": "https://unrelated.example"}, source="voice")[0])
        self.assertTrue(desktop_control.authorize_user_request(
            "Open my React project at localhost port 5173", "browser_control",
            {"action": "go_to", "url": "http://localhost:5173"}, source="voice")[0])
        self.assertFalse(desktop_control.authorize_user_request(
            "Open localhost port 3000", "browser_control",
            {"action": "go_to", "url": "http://localhost:5173"}, source="voice")[0])

    def test_cdp_attaches_existing_tab_and_close_only_disconnects(self):
        class Page:
            def is_closed(self): return False

        class Context:
            def __init__(self):
                self.pages = [Page(), Page()]
                self.close_calls = 0
            async def close(self): self.close_calls += 1

        class Browser:
            def __init__(self):
                self.contexts = [Context()]
                self.close_calls = 0
            async def close(self): self.close_calls += 1

        browser = Browser()

        class Chromium:
            async def connect_over_cdp(self, endpoint, **kwargs):
                self.endpoint, self.kwargs = endpoint, kwargs
                return browser

        class Playwright:
            def __init__(self): self.chromium = Chromium()

        async def run():
            session = browser_control._BrowserSession("chrome")
            session._pw = Playwright()
            with patch.object(browser_control, "_configured_chrome_cdp_endpoint",
                              return_value="http://127.0.0.1:9222"):
                await session._launch()
            self.assertIs(session._page, browser.contexts[0].pages[-1])
            self.assertEqual(session.profile_kind, "attached")
            self.assertEqual(session._pw.chromium.kwargs,
                             {"timeout": 5000, "no_defaults": True})
            await session._async_close()
            self.assertEqual(browser.close_calls, 1)
            self.assertEqual(browser.contexts[0].close_calls, 0)

        _run_immediate(run())

    def test_closed_cdp_context_is_discarded_and_reconnected_to_opt_in_endpoint(self):
        class Page:
            def __init__(self): self.url = "about:blank"
            def is_closed(self): return False

        class Context:
            def __init__(self): self.pages = [Page()]

        browser = type("Browser", (), {"contexts": [Context()]})()
        class Chromium:
            async def connect_over_cdp(self, endpoint, **kwargs):
                self.endpoint = endpoint
                return browser
        class Playwright:
            chromium = Chromium()
        class ClosedContext:
            @property
            def pages(self): raise RuntimeError("Target page, context or browser has been closed")

        async def run():
            session = browser_control._BrowserSession("chrome")
            session._pw = Playwright()
            session._context = ClosedContext()
            session._page = object()
            session._browser = None
            with patch.object(browser_control, "_configured_chrome_cdp_endpoint",
                              return_value="http://127.0.0.1:9222"):
                await session._launch()
            self.assertIs(session._context, browser.contexts[0])
            self.assertIs(session._page, browser.contexts[0].pages[0])

        _run_immediate(run())


class ConfirmationFeedbackTests(unittest.TestCase):
    def setUp(self):
        self.old_callbacks = (confirm._show_cb, confirm._hide_cb,
                              confirm._log_cb, confirm._result_cb)
        self.events = []
        confirm.bind(show=lambda *_: None, hide=lambda: None,
                     log=lambda *_: None,
                     result=lambda *args: self.events.append(args))

    def tearDown(self):
        confirm.bind(*self.old_callbacks)

    def test_denial_and_expiry_are_reported_without_running_action(self):
        calls = []
        confirm.request("test-denied", "Test deny", "", lambda: calls.append(1))
        confirm.resolve(False)
        self.assertEqual(self.events[-1][1], "denied")
        self.assertEqual(calls, [])

        confirm.request("test-expired", "Test expire", "", lambda: calls.append(2))
        with confirm._lock:
            confirm._pending.at = time.monotonic() - confirm.TIMEOUT_SECONDS - 1
        confirm.resolve(True)
        self.assertEqual(self.events[-1][1], "expired")
        self.assertEqual(calls, [])

    def test_approval_and_failure_are_reported(self):
        finished = threading.Event()
        confirm.bind(show=lambda *_: None, hide=lambda: None, log=lambda *_: None,
                     result=lambda *args: (self.events.append(args), finished.set()))
        confirm.request("test-approved", "Test approve", "", lambda: "verified result")
        confirm.resolve(True)
        self.assertTrue(finished.wait(2))
        self.assertEqual(self.events[-1][1:], ("approved", "verified result"))

        finished.clear()
        confirm.request("test-failed", "Test fail", "",
                        lambda: (_ for _ in ()).throw(RuntimeError("private detail")))
        confirm.resolve(True)
        self.assertTrue(finished.wait(2))
        self.assertEqual(self.events[-1][1], "failed")
        self.assertNotIn("private detail", self.events[-1][2])


class AudioQueueOverflowTests(unittest.TestCase):
    def test_full_live_audio_queue_replaces_oldest_nonblocking_with_safe_notice(self):
        import asyncio
        from main import RemLive
        rem = RemLive.__new__(RemLive)
        rem.out_queue = asyncio.Queue(maxsize=1)
        rem._audio_queue_overflows = 0
        rem._audio_overflow_notice_at = 0.0
        class UI:
            def __init__(self): self.logs = []
            def write_log(self, text): self.logs.append(text)
        rem.ui = UI()
        self.assertTrue(rem._queue_realtime_audio({"data": b"first"}))
        self.assertTrue(rem._queue_realtime_audio({"data": b"second"}))
        self.assertTrue(rem._queue_realtime_audio({"data": b"third"}))
        self.assertEqual(rem._audio_queue_overflows, 2)
        self.assertEqual(rem.out_queue.get_nowait(), {"data": b"third"})
        self.assertEqual(len(rem.ui.logs), 1)
        self.assertNotIn("third", rem.ui.logs[0])
        self.assertIn("replaced older audio", rem.ui.logs[0])

    def test_outcome_is_sent_only_to_the_requesting_live_session(self):
        from main import RemLive

        class Session:
            def __init__(self): self.inputs = []
            async def send_realtime_input(self, **kwargs): self.inputs.append(kwargs)

        rem = RemLive.__new__(RemLive)
        session = Session()
        rem.session = session
        rem._realtime_input_lock = asyncio.Lock()
        rem._arm_stall = lambda: None
        _run_immediate(rem._send_confirmation_result(session, "SYSTEM EVENT: approved"))
        self.assertEqual(session.inputs, [{"text": "SYSTEM EVENT: approved"}])
        with self.assertRaisesRegex(RuntimeError, "session has ended"):
            _run_immediate(rem._send_confirmation_result(Session(), "stale"))


if __name__ == "__main__":
    unittest.main()
