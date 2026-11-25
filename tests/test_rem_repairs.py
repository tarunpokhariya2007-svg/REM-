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


class CursorChromeMonitorAndSequenceTests(unittest.TestCase):
    def test_close_tab_executes_without_prompt_when_clean_and_confirms_if_dirty(self):
        class CleanPage:
            url = "https://www.youtube.com/watch?v=123"
            def __init__(self, dirty=False):
                self._dirty = dirty
                self.closed = False
            def is_closed(self): return self.closed
            async def evaluate(self, _js): return self._dirty
            async def close(self): self.closed = True

        clean = CleanPage(dirty=False)
        fallback = CleanPage(dirty=False)
        ctx = SimpleNamespace(pages=[fallback, clean])
        clean.context = ctx
        fallback.context = ctx
        sess = object.__new__(browser_control._BrowserSession)
        sess.browser_name = "chrome"
        sess._browser = None
        sess._spec = None
        sess._page = clean
        sess._context = ctx

        sess.run = lambda c: _run_immediate(c)
        with patch.object(browser_control._registry, "has", return_value=True), \
             patch.object(browser_control._registry, "get", return_value=sess), \
             patch("core.confirm.request", side_effect=AssertionError("close_tab must not prompt when requested")):
            res = browser_control._browser_control_impl({"action": "close_tab", "browser": "chrome"})
        self.assertIn("status=done", res)
        self.assertTrue(clean.closed)

        dirty_page = CleanPage(dirty=True)
        dirty_page.context = ctx
        sess2 = object.__new__(browser_control._BrowserSession)
        sess2.browser_name = "chrome"
        sess2._browser = None
        sess2._spec = None
        sess2._page = dirty_page
        sess2._context = ctx
        confirm_res = _run_immediate(sess2.close_tab())
        self.assertTrue(str(confirm_res).startswith("CONFIRM_REQUIRED:"))

    def test_chrome_never_silently_substitutes_edge(self):
        old_active = browser_control._registry._active_browser
        try:
            with patch("actions.browser_control._resolve_browser", return_value={"engine": "chromium", "exe": None, "channel": None}), \
                 patch("actions.browser_control._find_exe_windows", return_value=None), \
                 patch("actions.browser_control.appidentity.running", return_value=[]):
                res = browser_control._open_native("https://www.youtube.com/", "chrome")
            self.assertIn("status=failed", res)
            self.assertIn("did not substitute another browser", res)
            self.assertIn("browser=chrome", res)
        finally:
            browser_control._registry._active_browser = old_active

    def test_explicit_chrome_sets_active_browser_and_reuses_running(self):
        old_active = browser_control._registry._active_browser
        try:
            with patch("actions.browser_control._resolve_browser", return_value={"engine": "chromium", "exe": "chrome.exe", "channel": None}), \
                 patch("actions.browser_control.appidentity.running", return_value=["chrome.exe"]), \
                 patch("actions.browser_control.subprocess.Popen") as popen, \
                 patch("actions.browser_control._verify_native_browser",
                       return_value="status=done action=browser_control browser=chrome reused_existing=True verified=True"):
                res = browser_control._open_native("https://www.youtube.com/", "chrome")
            popen.assert_called_once()
            self.assertEqual(browser_control._registry._active_browser, "chrome")
            self.assertIn("browser=chrome", res)
            self.assertIn("reused_existing=True", res)
        finally:
            browser_control._registry._active_browser = old_active

    def test_youtube_search_typing_verifies_real_value_and_submission(self):
        class FakeInput:
            def __init__(self, visible=True, editable=True, ph="Search"):
                self._visible = visible
                self._editable = editable
                self._ph = ph
                self._val = ""
                self._focused = False
            async def count(self): return 1
            async def is_visible(self): return self._visible
            async def is_editable(self): return self._editable
            async def get_attribute(self, name):
                return self._ph if name == "placeholder" else "search"
            async def click(self, timeout=5000): self._focused = True
            async def evaluate(self, js):
                if "activeElement" in js:
                    return self._focused
                return {"tag": "INPUT", "type": "search", "role": "combobox", "label": "Search", "placeholder": self._ph, "name": "search_query", "editable": True}
            async def fill(self, text): self._val = text
            async def input_value(self): return self._val

        yt_input = FakeInput()

        class FakeKeyboard:
            def __init__(self, page): self._p = page
            async def press(self, key):
                if key.lower() == "enter":
                    self._p.url = "https://www.youtube.com/results?search_query=Savarana"
                    self._p._title = "Savarana - YouTube"

        async def _z(): return 0

        class FakeYTPage:
            def __init__(self):
                self.url = "https://www.youtube.com/"
                self._title = "YouTube"
                self.keyboard = FakeKeyboard(self)
            def is_closed(self): return False
            async def evaluate(self, js):
                if "visibilityState" in js: return True
                return {"sx": 0, "sy": 0, "ow": 1280, "oh": 800, "iw": 1280, "ih": 720}
            async def title(self): return self._title
            def locator(self, sel):
                if "search" in sel or sel == ":focus":
                    return yt_input
                return SimpleNamespace(count=_z)
            def get_by_role(self, *_a, **_k):
                return SimpleNamespace(count=_z)

        page = FakeYTPage()
        sess = object.__new__(browser_control._BrowserSession)
        sess.browser_name = "chrome"
        sess._browser = None
        sess._spec = None
        sess._page = page
        sess._context = SimpleNamespace(pages=[page])

        typed = _run_immediate(sess.smart_type("search bar", "Savarana"))
        self.assertIn("status=done", typed)
        self.assertIn("field_detected=true", typed)
        self.assertIn("field_focused=true", typed)
        self.assertIn("text_entered=true", typed)
        self.assertIn("entered_text=Savarana", typed)
        self.assertIn("submission_completed=false", typed)
        self.assertNotIn("method=placeholder", typed)

        submitted = asyncio.run(sess.press("Enter"))
        self.assertIn("status=done", submitted)
        self.assertIn("submission_completed=true", submitted)
        self.assertIn("page_state=search_results_loaded", submitted)

    def test_real_cursor_movement_and_uncertainty_when_zero_ocr(self):
        from core import screenact

        with patch("core.screenact.available", return_value=(True, "")), \
             patch("core.screenact._on_screen", return_value=True), \
             patch("core.screenact.pyautogui.moveTo") as mto, \
             patch("core.screenact.get_cursor_pos", return_value=(420, 180)), \
             patch("core.screenact.time.sleep"):
            act = screenact.move_cursor(screenact.Point(420, 180, origin="element", label="search bar"))
        self.assertTrue(act.ok)
        self.assertEqual(act.point, (420, 180))
        self.assertIn("cursor_verified", act.detail)
        mto.assert_called_once()

        empty_view = SimpleNamespace(app="chrome.exe", title="YouTube - Chrome", source="none", degraded=True, elements=[])
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "api_keys.json"
            with patch.object(config_manager, "CONFIG_FILE", cfg), \
                 patch("core.screenview.look", return_value=empty_view), \
                 patch("core.screen_observation.describe_view", return_value="chrome.exe"), \
                 patch("actions.browser_control.inspect_open_chrome_tabs_local", return_value=[]):
                obs = screen_observation.inspect_local_screen("what is visible?")
        self.assertIn("confidence=uncertain", obs)

    def test_authorization_multistep_youtube_chrome_and_cursor_commands(self):
        full_cmd = "Open YouTube in Chrome, click the search bar, type Savarana, and search."
        for tool, args in (
            ("browser_control", {"action": "go_to", "url": "https://www.youtube.com/", "browser": "chrome"}),
            ("browser_control", {"action": "smart_click", "description": "search bar", "browser": "chrome"}),
            ("browser_control", {"action": "smart_type", "description": "search bar", "text": "Savarana", "browser": "chrome"}),
            ("browser_control", {"action": "press", "key": "Enter", "browser": "chrome"}),
            ("desktop_ui", {"action": "move", "target": "search bar"}),
        ):
            with self.subTest(tool=tool, action=args["action"]):
                ok, reason = desktop_control.authorize_user_request(full_cmd, tool, args, source="voice")
                self.assertTrue(ok, reason)

        self.assertTrue(desktop_control.authorize_user_request(
            "Move the cursor to the center",
            "desktop_ui", {"action": "move", "position": "center"}, source="voice")[0])

        self.assertTrue(desktop_control.authorize_user_request(
            "Put the cursor on the YouTube search bar",
            "desktop_ui", {"action": "move", "target": "search bar"}, source="voice")[0])
        self.assertTrue(desktop_control.authorize_user_request(
            "Close this tab.",
            "browser_control", {"action": "close_tab"}, source="voice")[0])
        self.assertFalse(desktop_control.authorize_user_request(
            "Open YouTube in Chrome",
            "send_message", {"receiver": "Alex", "message_text": "hello"}, source="voice")[0])

    def test_ram_monitor_suppresses_intrusions_and_preserves_diagnostic(self):
        mon = SystemMonitor()
        with patch("actions.system_monitor.psutil.cpu_percent", return_value=15.0), \
             patch("actions.system_monitor.psutil.virtual_memory", return_value=SimpleNamespace(percent=99.0)), \
             patch("actions.system_monitor._get_cpu_temp", return_value=-1.0), \
             patch("actions.system_monitor._get_gpu_usage", return_value=-1.0):
            for _ in range(7):
                self.assertIsNone(mon.check())
        self.assertEqual(mon.status_summary().get("ram_percent"), 99.0)


if __name__ == "__main__":
    unittest.main()
