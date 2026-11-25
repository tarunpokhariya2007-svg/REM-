import contextvars
import threading
import time
import unittest
from types import SimpleNamespace

import main
from core.request_context import RequestContext, REQUEST_CONTEXT_TTL


class _Registry:
    def __init__(self, name="browser_control"):
        self.name = name
        self.calls = []

    def has(self, name):
        return name == self.name

    def run(self, name, parameters, ctx=None, **kwargs):
        self.calls.append((name, parameters, ctx, kwargs))
        return "status=done action=test verified=true"


def _rem():
    rem = main.RemLive.__new__(main.RemLive)
    rem._request_context_lock = threading.Lock()
    rem._request_context = None
    rem._last_user_command = ""
    rem._last_user_command_source = "untrusted"
    rem.ui = SimpleNamespace()
    rem._dispatch_context_local = contextvars.ContextVar(
        f"request-context-test-{id(rem)}", default=None)
    rem._action_registry = _Registry()
    rem._plugin_registry = _Registry("test_plugin")
    return rem


class NestedRequestContextTests(unittest.TestCase):
    def test_authorized_nested_browser_action_uses_originating_context(self):
        rem = _rem()
        origin = RequestContext.create("Open https://example.com in Chrome", "voice")
        rem._record_user_request("Check the weather", "dashboard")
        result = rem._run_with_request_context(
            origin,
            lambda: rem._run_tool_for_plugin(
                "browser_control", {"action": "go_to", "url": "https://example.com"}),
        )
        self.assertIn("status=done", result)
        self.assertEqual(len(rem._action_registry.calls), 1)

    def test_authorized_nested_desktop_action_uses_originating_context(self):
        rem = _rem()
        rem._action_registry = _Registry("desktop_ui")
        origin = RequestContext.create("Click the Run test button", "voice")
        result = rem._run_with_request_context(
            origin,
            lambda: rem._run_tool_for_plugin(
                "desktop_ui", {"action": "click", "target": "Run test button"}),
        )
        self.assertIn("status=done", result)
        self.assertEqual(len(rem._action_registry.calls), 1)

    def test_unrelated_nested_action_is_denied(self):
        rem = _rem()
        origin = RequestContext.create("Check the weather", "voice")
        result = rem._run_with_request_context(
            origin,
            lambda: rem._run_tool_for_plugin(
                "browser_control", {"action": "go_to", "url": "https://example.com"}),
        )
        self.assertIn("failed", result)
        self.assertEqual(rem._action_registry.calls, [])

    def test_missing_nested_context_does_not_fall_back_to_new_global_request(self):
        rem = _rem()
        rem._record_user_request("Open https://example.com", "voice")
        result = rem._run_tool_for_plugin(
            "browser_control", {"action": "go_to", "url": "https://example.com"})
        self.assertIn("missing_or_unrelated_context", result)
        self.assertEqual(rem._action_registry.calls, [])

    def test_expired_nested_context_is_untrusted_and_denied(self):
        rem = _rem()
        expired = RequestContext.create(
            "Open https://example.com", "voice",
            now=time.monotonic() - REQUEST_CONTEXT_TTL - 1,
        )
        result = rem._run_with_request_context(
            expired,
            lambda: rem._run_tool_for_plugin(
                "browser_control", {"action": "go_to", "url": "https://example.com"}),
        )
        self.assertIn("failed", result)
        self.assertEqual(rem._action_registry.calls, [])

    def test_explicitly_missing_live_batch_context_does_not_read_latest_context(self):
        rem = _rem()
        rem._record_user_request("Open https://example.com", "voice")
        fc = SimpleNamespace(name="browser_control",
                             args={"action": "go_to", "url": "https://example.com"},
                             id="missing-context")
        coroutine = rem._execute_tool(fc, request_context=None)
        try:
            coroutine.send(None)
        except StopIteration as done:
            result = done.value
        else:
            self.fail("authorization refusal unexpectedly awaited I/O")
        self.assertIn("direct user request", result.response["result"].lower())
        self.assertEqual(rem._action_registry.calls, [])

    def test_omitted_context_does_not_borrow_latest_global_request(self):
        rem = _rem()
        rem._record_user_request("Open https://example.com", "voice")
        fc = SimpleNamespace(name="browser_control",
                             args={"action": "go_to", "url": "https://example.com"},
                             id="omitted-context")
        coroutine = rem._execute_tool(fc)
        try:
            coroutine.send(None)
        except StopIteration as done:
            result = done.value
        else:
            self.fail("authorization refusal unexpectedly awaited I/O")
        self.assertIn("direct user request", result.response["result"].lower())
        self.assertEqual(rem._action_registry.calls, [])


class LivePcmContractTests(unittest.TestCase):
    def test_queued_live_audio_declares_its_16000_hz_rate(self):
        from google.genai import types

        class Queue:
            async def get(self):
                return {"data": b"\x00\x00" * 160, "mime_type": "audio/pcm;rate=16000"}

        class Lock:
            async def __aenter__(self):
                return self
            async def __aexit__(self, *_args):
                return False

        class Session:
            def __init__(self):
                self.sent = []
            async def send_realtime_input(self, **kwargs):
                self.sent.append(kwargs["audio"])
                raise __import__("asyncio").CancelledError()

        rem = main.RemLive.__new__(main.RemLive)
        rem.out_queue = Queue()
        rem._realtime_input_lock = Lock()
        rem.session = Session()
        coroutine = rem._send_realtime()
        with self.assertRaises(__import__("asyncio").CancelledError):
            coroutine.send(None)
        self.assertEqual(len(rem.session.sent), 1)
        self.assertIsInstance(rem.session.sent[0], types.Blob)
        self.assertEqual(rem.session.sent[0].mime_type, "audio/pcm;rate=16000")


if __name__ == "__main__":
    unittest.main()
