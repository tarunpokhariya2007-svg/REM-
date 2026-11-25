import asyncio
import json
import os
import tempfile
import threading
import time
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from actions import browser_control, screen_agent
from actions.system_monitor import SystemMonitor
from core import autonomous_mode, desktop_control, screen_observation
from core.avatar_anim import (
    ANGRY,
    CONTEXTUAL_EMOTION_DETECTION,
    DEFAULT_NEUTRAL,
    EXPLICIT_USER_COMMAND,
    EXPRESSIONS,
    HAPPY,
    LOVE,
    NEUTRAL,
    SAD,
    SHY,
    SLEEPY,
    SURPRISED,
    AvatarAnimator,
    parse_explicit_expression_command,
)
from core.conversation import detect_language
from core.request_context import RequestContext
from memory import config_manager


def _run_immediate(coro):
    try:
        coro.send(None)
    except StopIteration as done:
        return done.value
    raise AssertionError("Coroutine yielded unexpectedly")


class PersistentLocalScreenTests(unittest.TestCase):
    def test_local_screen_observation_defaults_true_and_separates_from_cloud(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "api_keys.json"
            with patch.object(config_manager, "CONFIG_FILE", cfg):
                self.assertTrue(config_manager.get_local_screen_observation_enabled())
                self.assertFalse(config_manager.get_cloud_screen_vision_enabled())

                config_manager.save_local_screen_observation_enabled(False)
                self.assertFalse(config_manager.get_local_screen_observation_enabled())
                self.assertFalse(config_manager.get_cloud_screen_vision_enabled())

                config_manager.save_local_screen_observation_enabled(True)
                self.assertTrue(config_manager.get_local_screen_observation_enabled())
                self.assertFalse(config_manager.get_cloud_screen_vision_enabled())

                config_manager.save_cloud_screen_vision_enabled(True)
                config_manager.revoke_screen_access()
                self.assertFalse(config_manager.get_local_screen_observation_enabled())
                self.assertFalse(config_manager.get_cloud_screen_vision_enabled())

    def test_revoked_local_screen_blocks_observation_and_returns_refusal(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "api_keys.json"
            with patch.object(config_manager, "CONFIG_FILE", cfg):
                config_manager.revoke_screen_access()
                ok, reason = desktop_control.authorize_user_request(
                    "Can you see whether WhatsApp Web is open?",
                    "screen_process",
                    {"angle": "screen"},
                    source="voice",
                )
                self.assertFalse(ok)
                self.assertIn("revoked", reason.lower())

                with self.assertRaises(PermissionError):
                    screen_observation.assert_local_observation_allowed()

                obs = screen_agent.screen_agent({"action": "observe"})
                self.assertIn("status=failed", obs)
                self.assertIn("revoked", obs.lower())

    def test_local_screen_inspection_checks_chrome_tabs_without_cloud(self):
        fake_view = SimpleNamespace(
            app="chrome.exe",
            title="WhatsApp - Google Chrome",
            source="uia",
            degraded=False,
            elements=[],
        )
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "api_keys.json"
            with patch.object(config_manager, "CONFIG_FILE", cfg), \
                 patch("core.screenview.look", return_value=fake_view), \
                 patch("core.screen_observation.describe_view", return_value="WhatsApp - Google Chrome\nScan QR code to log in"), \
                 patch("actions.browser_control.inspect_open_chrome_tabs_local", return_value=["WhatsApp Web (https://web.whatsapp.com/)"]):
                summary = screen_observation.inspect_local_screen("Is WhatsApp Web open?")
        self.assertIn("https://web.whatsapp.com/", summary)
        self.assertIn("Browser windows/tabs detected locally", summary)


class BrowserVerificationAndUrlsTests(unittest.TestCase):
    def test_canonicalizes_whatsapp_and_youtube_urls(self):
        self.assertEqual(browser_control._normalize_url("WhatsApp Web"), "https://web.whatsapp.com/")
        self.assertEqual(browser_control._normalize_url("https://web.whatsapp"), "https://web.whatsapp.com/")
        self.assertEqual(browser_control._normalize_url("https://www.youtube."), "https://www.youtube.com/")
        self.assertEqual(browser_control._normalize_url("youtube.com"), "https://youtube.com")

    def test_native_open_without_verification_never_claims_loaded(self):
        with patch("actions.browser_control._verify_native_browser",
                   return_value="status=unverified action=browser_control reason='window could not be confirmed'"):
            msg = browser_control._native_outcome("chrome", "https://web.whatsapp.com/")
        self.assertTrue(msg.startswith("status=unverified"))

    def test_session_inspect_page_and_switch_tab(self):
        class FakeLocator:
            def __init__(self, text):
                self._text = text

            async def inner_text(self, timeout=1500):
                return self._text

        class FakePage:
            def __init__(self, url, title, text):
                self.url = url
                self._title = title
                self._text = text

            def is_closed(self):
                return False

            async def title(self):
                return self._title

            async def evaluate(self, script):
                if "readyState" in script:
                    return "complete"
                return self._text

            def locator(self, _sel):
                return FakeLocator(self._text)

            async def bring_to_front(self):
                return None

        p1 = FakePage("https://www.youtube.com/", "YouTube", "Home Trending Subscriptions")
        p2 = FakePage("https://web.whatsapp.com/", "WhatsApp", "Scan QR code to log in with phone")
        ctx = SimpleNamespace(pages=[p1, p2])
        p1.context = ctx
        p2.context = ctx
        sess = object.__new__(browser_control._BrowserSession)
        sess.browser_name = "chrome"
        sess._browser = None
        sess._spec = None
        sess._page = p1
        sess._context = ctx

        inspected = _run_immediate(sess.inspect_page())
        self.assertIn("page_state=loaded", inspected)
        self.assertIn("https://www.youtube.com/", inspected)

        switched = _run_immediate(sess.switch_tab("WhatsApp"))
        self.assertIn("status=done", switched)
        self.assertIs(sess._page, p2)
        inspected2 = _run_immediate(sess.inspect_page())
        self.assertIn("page_state=login_required", inspected2)


class AuthorizationPropagationAndConsistencyTests(unittest.TestCase):
    def test_natural_whatsapp_visibility_queries_authorized(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "api_keys.json"
            with patch.object(config_manager, "CONFIG_FILE", cfg):
                for tool, args in (
                    ("screen_process", {"angle": "screen", "text": "Is WhatsApp Web open?"}),
                    ("desktop_ui", {"action": "observe"}),
                    ("browser_control", {"action": "inspect"}),
                    ("browser_control", {"action": "get_url"}),
                    ("browser_control", {"action": "get_text"}),
                ):
                    with self.subTest(tool=tool, args=args):
                        ok, reason = desktop_control.authorize_user_request(
                            "Can you see whether WhatsApp Web is open?",
                            tool,
                            args,
                            source="voice",
                        )
                        self.assertTrue(ok, reason)

    def test_open_whatsapp_authorizes_navigation_and_verification(self):
        ok_go, reason_go = desktop_control.authorize_user_request(
            "Open WhatsApp Web in Chrome",
            "browser_control",
            {"action": "go_to", "url": "https://web.whatsapp.com/", "browser": "chrome"},
            source="typed",
        )
        self.assertTrue(ok_go, reason_go)
        ok_url, reason_url = desktop_control.authorize_user_request(
            "Open WhatsApp Web in Chrome",
            "browser_control",
            {"action": "get_url"},
            source="typed",
        )
        self.assertTrue(ok_url, reason_url)


class ExpressionCommandsAndPriorityTests(unittest.TestCase):
    def test_all_explicit_expression_commands_parse(self):
        cases = {
            "Smile": HAPPY,
            "Show me an angry expression": ANGRY,
            "Look sleepy": SLEEPY,
            "Can you cry?": SAD,
            "Show me your surprised face": SURPRISED,
            "Act shy please": SHY,
            "Show me affection": LOVE,
            "Return to normal": NEUTRAL,
        }
        for cmd, expected in cases.items():
            with self.subTest(cmd=cmd):
                self.assertEqual(parse_explicit_expression_command(cmd), expected)

    def test_explicit_command_priority_beats_contextual_detection(self):
        anim = AvatarAnimator()
        anim.set_expression(SAD, hold=10.0, source=EXPLICIT_USER_COMMAND)
        self.assertEqual(anim.expression_state, SAD)
        self.assertEqual(anim.expression_source, EXPLICIT_USER_COMMAND)

        # Contextual emotion updates must not overwrite an active explicit user command
        anim.set_expression(HAPPY, hold=6.0, source=CONTEXTUAL_EMOTION_DETECTION)
        self.assertEqual(anim.expression_state, SAD)
        self.assertEqual(anim.expression_source, EXPLICIT_USER_COMMAND)

        # Explicit reset to NEUTRAL clears immediately
        anim.set_expression(NEUTRAL, hold=0.0, source=EXPLICIT_USER_COMMAND)
        self.assertEqual(anim.expression_state, NEUTRAL)
        self.assertEqual(anim.expression_source, DEFAULT_NEUTRAL)

    def test_prompt_documents_all_eight_expressions_and_tarun_kun(self):
        prompt = (Path(__file__).resolve().parent.parent / "core" / "prompt.txt").read_text(encoding="utf-8")
        for name in EXPRESSIONS:
            self.assertIn(name, prompt)
        self.assertIn("Tarun-kun", prompt)
        self.assertNotIn("as Master", prompt)


class LanguageMonitorGreetingAndUiTests(unittest.TestCase):
    def test_spanish_and_english_language_detection(self):
        self.assertEqual(
            detect_language("Me da miedo el silencio y la oscuridad, me puedes contar un chiste"),
            "Spanish",
        )
        self.assertEqual(detect_language("Can you see whether WhatsApp Web is open?"), "English")

    def test_ram_alert_requires_sustained_high_usage_and_higher_threshold(self):
        mon = SystemMonitor()
        self.assertGreaterEqual(mon.thresholds["ram"], 95.0)
        with patch("actions.system_monitor.psutil.cpu_percent", return_value=20.0), \
             patch("actions.system_monitor.psutil.virtual_memory", return_value=SimpleNamespace(percent=92.0)), \
             patch("actions.system_monitor._get_cpu_temp", return_value=-1.0), \
             patch("actions.system_monitor._get_gpu_usage", return_value=-1.0):
            # 92% must never fire a spurious warning
            for _ in range(4):
                self.assertIsNone(mon.check())

    def test_startup_greeting_time_of_day_tarun_kun_and_single_shot(self):
        from main import RemLive

        class FakeSession:
            def __init__(self):
                self.sent = []

            async def send_client_content(self, turns=None, turn_complete=True):
                self.sent.append(turns)

        for hr, expected in (
            (9, "Good morning"),
            (14, "Good afternoon"),
            (19, "Good evening"),
            (23, "It's quite late"),
        ):
            with self.subTest(hour=hr):
                rem = RemLive.__new__(RemLive)
                rem._startup_greeting_sent = False
                rem.session = FakeSession()
                rem._turn_done_event = None
                with patch("main.load_memory", return_value={"identity": {}}):
                    asyncio.run(rem._send_startup_greeting(now=datetime(2026, 11, 26, hr, 0)))
                self.assertEqual(len(rem.session.sent), 1)
                prompt_text = rem.session.sent[0]["parts"][0]["text"]
                self.assertTrue(prompt_text.startswith(expected))
                self.assertIn("Tarun-kun", prompt_text)
                self.assertIn("ready to help", prompt_text.lower())
                self.assertIn("do not announce news, markets", prompt_text.lower())

                # Reconnect must NOT send startup greeting a second time
                rem.session = FakeSession()
                asyncio.run(rem._send_startup_greeting(now=datetime(2026, 11, 26, hr, 5)))
                self.assertEqual(len(rem.session.sent), 0)

    def test_speak_error_uses_tarun_kun(self):
        from main import RemLive

        spoken = []
        rem = RemLive.__new__(RemLive)
        rem.ui = SimpleNamespace(write_log=lambda _msg: None)
        rem.speak = lambda txt: spoken.append(txt)
        rem.speak_error("browser_control", RuntimeError("boom"))
        self.assertIn("Tarun-kun", spoken[0])
        self.assertNotIn("Sir", spoken[0])

    def test_computer_use_panel_screen_controls_offscreen(self):
        import subprocess
        import sys

        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "api_keys.json"
            repo_root = str(Path(__file__).resolve().parent.parent)
            script = f"""
import json
from pathlib import Path
from unittest.mock import patch
from PyQt6.QtWidgets import QApplication
from core import desktop_control
from memory import config_manager
from gui.window import MainWindow

cfg = Path({str(cfg)!r})
face_path = str(Path({repo_root!r}) / "gui" / "assets" / "rem" / "base.png")
app = QApplication([])
with patch.object(config_manager, "CONFIG_FILE", cfg):
    win = MainWindow(face_path)
    assert win._local_screen_obs.isChecked() is True
    assert win._cloud_screen_vision.isChecked() is False
    assert win._revoke_screen_btn.text() == "REVOKE SCREEN ACCESS"
    win._set_desktop_control_status(desktop_control.snapshot())
    assert win._computer_rows["SCREEN"]._value == "LOCAL READY", win._computer_rows["SCREEN"]._value

    win._revoke_screen_access()
    app.processEvents()
    assert win._local_screen_obs.isChecked() is False
    assert win._cloud_screen_vision.isChecked() is False
    assert win._computer_rows["SCREEN"]._value == "REVOKED / OFF", win._computer_rows["SCREEN"]._value
    saved = json.loads(cfg.read_text(encoding="utf-8"))
    assert saved["local_screen_observation_enabled"] is False
    assert saved["cloud_screen_vision_enabled"] is False
"""
            env = dict(os.environ)
            env["QT_QPA_PLATFORM"] = "offscreen"
            proc = subprocess.run(
                [sys.executable, "-c", script],
                cwd=repo_root,
                env=env,
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(proc.returncode, 0, msg=proc.stderr or proc.stdout)


if __name__ == "__main__":
    unittest.main()
