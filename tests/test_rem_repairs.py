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
from unittest.mock import AsyncMock, MagicMock, patch

from actions import browser_control, screen_agent
from actions.system_monitor import SystemMonitor
from core import autonomous_mode, conversation, desktop_control, screen_observation
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
from plugins import _whatsapp_core


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
        sess.profile_kind = "real"
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
        sess.profile_kind = "real"
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
        sess.profile_kind = "real"
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


class PersistentBrowserChromeYouTubeTests(unittest.TestCase):
    def test_centralized_browser_selection_priority_and_visible_window_preference(self):
        old_active, old_task = browser_control._registry._active_browser, browser_control._registry._task_browser
        try:
            browser_control._registry._active_browser = ""
            browser_control._registry._task_browser = ""
            with patch("core.screenview._foreground", return_value=("explorer.exe", "Desktop", 1, 1)), \
                 patch("core.screenview.all_windows", return_value=[("chrome.exe", "YouTube - Google Chrome")]), \
                 patch("actions.browser_control.appidentity.running", side_effect=lambda b: [f"{b}.exe"] if b in ("chrome", "edge") else []), \
                 patch("actions.browser_control._detect_default_browser", return_value="edge"):
                # Explicit wins over everything; visible Chrome window beats Windows default Edge
                self.assertEqual(browser_control.select_browser("edge", task_browser="chrome"), "edge")
                self.assertEqual(browser_control.select_browser("chrome"), "chrome")
                self.assertEqual(browser_control.select_browser(None, task_browser="chrome"), "chrome")
                self.assertEqual(browser_control.select_browser(None), "chrome")
        finally:
            browser_control._registry._active_browser = old_active
            browser_control._registry._task_browser = old_task

    def test_multi_turn_chrome_task_retains_browser_and_blocks_edge_switch(self):
        from main import RemLive

        rem = RemLive.__new__(RemLive)
        rem._request_context_lock = threading.Lock()
        rem._request_context = None
        rem._last_user_command = ""
        rem._last_user_command_source = ""
        rem._active_browser_task = None

        rem._record_user_request("Open YouTube in Chrome.", "voice")
        c1 = rem._current_request_context()
        self.assertEqual(c1.browser, "chrome")
        self.assertTrue(c1.browser_task_id)

        rem._record_user_request("Play the third video.", "voice")
        c2 = rem._current_request_context()
        self.assertEqual(c2.browser, "chrome")
        self.assertEqual(c2.browser_task_id, c1.browser_task_id)

        # Follow-up ordinal, pause, resume, close_tab all stay authorized on Chrome
        self.assertTrue(desktop_control.authorize_user_request(
            c2.instruction, "browser_control", {"action": "smart_click", "description": "third video", "browser": "chrome"},
            source=c2.source, auth_context=c2)[0])
        self.assertTrue(desktop_control.authorize_user_request(
            "Pause the video.", "browser_control", {"action": "pause_video", "browser": "chrome"},
            source="voice", auth_context=c2)[0])
        # Refuses unexpected switch to Edge inside a Chrome task
        ok_edge, _ = desktop_control.authorize_user_request(
            c2.instruction, "browser_control", {"action": "smart_click", "description": "third video", "browser": "edge"},
            source=c2.source, auth_context=c2)
        self.assertFalse(ok_edge)

        # Unrelated non-browser command drops browser task continuity
        rem._record_user_request("Turn the volume down", "voice")
        c3 = rem._current_request_context()
        self.assertEqual(c3.browser_task_id, "")
        self.assertEqual(c3.browser, "")

    def test_youtube_ordinal_title_and_playback_verification_excludes_ads(self):
        videos = [
            {"position": 1, "id": "vid11111111", "url": "https://www.youtube.com/watch?v=vid11111111", "title": "First Mix", "channel": "Ch1", "is_ad": False, "is_playable": True},
            {"position": 2, "id": "vid22222222", "url": "https://www.youtube.com/watch?v=vid22222222", "title": "Lo-Fi Beats Second", "channel": "Ch2", "is_ad": False, "is_playable": True},
            {"position": 3, "id": "vid33333333", "url": "https://www.youtube.com/watch?v=vid33333333", "title": "Minecraft Episode 3", "channel": "Ch3", "is_ad": False, "is_playable": True},
        ]

        class ClickTarget:
            def __init__(self, vid, page):
                self._vid = vid
                self._page = page
            async def count(self): return 1
            async def is_visible(self): return True
            async def is_enabled(self): return True
            async def click(self, timeout=8000):
                self._page.url = f"https://www.youtube.com/watch?v={self._vid}"
                self._page.video_paused = False

        class ResultsLocator:
            def __init__(self, vids):
                self._vids = vids
                self.script = ""
            async def evaluate_all(self, script):
                self.script = script
                return self._vids

        class MockYTPage:
            def __init__(self):
                self.url = "https://www.youtube.com/results?search_query=lofi"
                self.video_paused = True
                self.results_loc = ResultsLocator(videos)
            def is_closed(self): return False
            def locator(self, sel):
                if "ytd-video-renderer" in sel:
                    return self.results_loc
                m = __import__("re").search(r"watch\?v=([A-Za-z0-9_-]+)", sel)
                return ClickTarget(m.group(1), self) if m else SimpleNamespace(count=self._zero)
            async def _zero(self): return 0
            async def wait_for_url(self, *args, **kwargs): return None
            async def evaluate(self, js, *args):
                from tests import yt_fakes as _yf
                from core import media_state as _ms
                if js in (_ms.STATE_JS, _ms.ACTION_JS):
                    return await _yf.playing_evaluate(self, js, *args)
                if "visibilityState" in js:
                    return True
                if "v.pause()" in js:
                    self.video_paused = True
                    return {"exists": True, "paused": True, "currentTime": 12.5}
                if "v.play()" in js:
                    self.video_paused = False
                    return {"exists": True, "paused": False, "currentTime": 12.5}
                return {"exists": True, "paused": self.video_paused, "currentTime": 1.2, "readyState": 4, "duration": 180.0}

        page = MockYTPage()
        sess = object.__new__(browser_control._BrowserSession)
        sess.browser_name = "chrome"
        sess.profile_kind = "real"
        sess._browser = None
        sess._spec = None
        sess._page = page
        sess._context = SimpleNamespace(pages=[page])

        # Verify card extractor script excludes ads, playlists, channels, and shorts
        cards = asyncio.run(sess._extract_youtube_videos(page))
        self.assertEqual(len(cards), 3)
        for token in ("ytd-ad-slot-renderer", "ytd-playlist-renderer", "ytd-channel-renderer", "ytd-reel-shelf-renderer", "/shorts/"):
            self.assertIn(token, page.results_loc.script)

        # Play the 3rd video -> verifies URL + playing <video> state
        res3 = asyncio.run(sess.smart_click("Play the third video from this search"))
        self.assertIn("status=done", res3)
        self.assertIn("video_id=vid33333333", res3)
        self.assertIn("position=3", res3)
        self.assertIn("playback_verified=true", res3)

        # Pause and resume verification
        paused = asyncio.run(sess.pause_video())
        self.assertIn("playback_state=paused", paused)
        resumed = asyncio.run(sess.resume_video())
        self.assertIn("playback_state=playing", resumed)

        # Title-based selection on results page
        page.url = "https://www.youtube.com/results?search_query=lofi"
        titled = asyncio.run(sess.smart_click("Open the video titled Lo-Fi Beats Second"))
        self.assertIn("video_id=vid22222222", titled)
        self.assertIn("playback_verified=true", titled)

    def test_integration_full_chrome_lofi_sequence_without_edge(self):
        from main import RemLive

        class FakeTab:
            def __init__(self, url):
                self.url = url
                self._closed = False
                self._paused = False
            def is_closed(self): return self._closed
            async def evaluate(self, js, *a):
                from tests import yt_fakes as _yf
                from core import media_state as _ms
                if js in (_ms.STATE_JS, _ms.ACTION_JS):
                    fake = _yf.FakeYouTubePage(url=self.url, paused=self._paused)
                    res = await fake.evaluate(js, *a)
                    self._paused = fake.paused
                    return res
                if "visibilityState" in js: return True
                if "v.pause()" in js: self._paused = True; return {"exists": True, "paused": True, "currentTime": 5.0}
                if "v.play()" in js: self._paused = False; return {"exists": True, "paused": False, "currentTime": 5.0}
                if "document.querySelector('video')" in js:
                    return {"exists": True, "paused": self._paused, "currentTime": 1.0, "readyState": 4, "duration": 120.0}
                if "textarea" in js: return False
                return True
            async def title(self): return "Lo-Fi Mix - YouTube"
            def locator(self, sel):
                outer = self
                if "ytd-video-renderer" in sel:
                    async def _ev(_s):
                        return [
                            {"position": 1, "id": "aaa11111111", "title": "First", "channel": "C1"},
                            {"position": 2, "id": "bbb22222222", "title": "Lo-Fi Second", "channel": "C2"},
                        ]
                    return SimpleNamespace(evaluate_all=_ev)
                if "bbb22222222" in sel:
                    async def _c(): return 1
                    async def _t(): return True
                    async def _clk(**_kw):
                        outer.url = "https://www.youtube.com/watch?v=bbb22222222"
                        outer._paused = False
                    return SimpleNamespace(count=_c, is_visible=_t, is_enabled=_t, click=_clk)
                async def _z(): return 0
                return SimpleNamespace(count=_z, inner_text=lambda timeout=1500: asyncio.sleep(0, result="Lo-Fi"))
            async def goto(self, url, **_kw): self.url = url
            async def wait_for_url(self, *a, **kw): return None
            async def close(self): self._closed = True

        rem = RemLive.__new__(RemLive)
        rem._request_context_lock = threading.Lock()
        rem._request_context = None
        rem._last_user_command = ""
        rem._last_user_command_source = ""
        rem._active_browser_task = None

        tab = FakeTab("about:blank")
        ctx = SimpleNamespace(pages=[tab])
        tab.context = ctx
        sess = object.__new__(browser_control._BrowserSession)
        sess.browser_name = "chrome"
        sess.profile_kind = "real"
        sess._browser = None
        sess._spec = None
        sess._page = tab
        sess._context = ctx
        sess.run = lambda coro, timeout=60: asyncio.run(coro)

        with patch.object(browser_control._registry, "has", return_value=True), \
             patch.object(browser_control._registry, "get", return_value=sess):
            steps = [
                ("Open YouTube in Chrome.", {"action": "go_to", "url": "https://www.youtube.com/", "browser": "chrome"}),
                ("Search for lo-fi music.", {"action": "search", "query": "lo-fi music"}),
                ("Play the second video.", {"action": "smart_click", "description": "Play the second video"}),
                ("Pause the video.", {"action": "pause_video"}),
                ("Resume the video.", {"action": "resume_video"}),
                ("Close this tab.", {"action": "close_tab"}),
            ]
            results = []
            for text, tool_args in steps:
                rem._record_user_request(text, "voice")
                req_ctx = rem._current_request_context()
                self.assertEqual(req_ctx.browser, "chrome")
                call_args = dict(tool_args)
                if not call_args.get("browser"):
                    call_args["browser"] = req_ctx.browser
                ok, reason = desktop_control.authorize_user_request(
                    req_ctx.instruction, "browser_control", call_args, source="voice", auth_context=req_ctx
                )
                self.assertTrue(ok, f"{text}: {reason}")
                out = browser_control._browser_control_impl(call_args)
                self.assertIn("status=done", out, f"{text}: {out}")
                self.assertNotIn("edge", out.casefold())
                results.append(out)
        self.assertIn("bbb22222222", results[2])
        self.assertIn("playback_state=paused", results[3])
        self.assertIn("playback_state=playing", results[4])
        self.assertIn("action=close_tab", results[5])


class PlaywrightLifecycleRecoveryTests(unittest.TestCase):
    def test_closed_context_relaunches_in_get_page_without_leaking_error(self):
        from actions import browser_control

        sess = browser_control._BrowserSession("chrome")
        calls = {"launch": 0}

        class _DeadContext:
            def is_closed(self):
                return False

            @property
            def pages(self):
                return []

            async def new_page(self):
                raise RuntimeError("BrowserContext.new_page: Target page, context or browser has been closed")

        class _GoodPage:
            url = "https://www.youtube.com/"

            def is_closed(self):
                return False

            def on(self, *_args, **_kwargs):
                return None

            def set_default_timeout(self, *_args, **_kwargs):
                return None

        class _GoodContext:
            pages = []

            def is_closed(self):
                return False

            async def new_page(self):
                return _GoodPage()

        async def fake_launch():
            calls["launch"] += 1
            if sess._context is None:
                sess._context = _GoodContext()
                sess._page = None
                sess._browser = None

        sess._context = _DeadContext()
        sess._page = None
        sess.ensure_browser = lambda: None
        sess._launch = fake_launch
        page = asyncio.run(sess._get_page())
        self.assertIsInstance(page, _GoodPage)
        self.assertEqual(calls["launch"], 2)

    def test_disconnected_cdp_browser_and_closed_context_checked_in_launch(self):
        from actions import browser_control

        class _DisconnectedBrowser:
            def is_connected(self):
                return False

        class _ClosedCtx:
            def is_closed(self):
                return True

        sess = browser_control._BrowserSession("chrome")
        sess._browser = _DisconnectedBrowser()
        sess._context = _ClosedCtx()
        sess._pw = MagicMock()
        sess._pw.chromium = MagicMock()
        sess._pw.chromium.connect_over_cdp = AsyncMock(side_effect=RuntimeError("cdp probe stop"))

        async def probe():
            with patch.object(browser_control, "_configured_browser_cdp_endpoint", return_value="http://127.0.0.1:9222"):
                with self.assertRaises(RuntimeError):
                    await sess._launch()

        asyncio.run(probe())
        self.assertIsNone(sess._browser)
        self.assertIsNone(sess._context)

    def test_session_run_serializes_concurrent_operations(self):
        from actions import browser_control

        sess = browser_control._BrowserSession("chrome")
        loop = asyncio.new_event_loop()
        ready = threading.Event()

        def _bg():
            asyncio.set_event_loop(loop)
            ready.set()
            loop.run_forever()

        bg_thread = threading.Thread(target=_bg, daemon=True)
        bg_thread.start()
        ready.wait(2.0)
        sess._loop = loop
        order = []

        async def slow(tag, delay):
            order.append(f"start:{tag}")
            await asyncio.sleep(delay)
            order.append(f"end:{tag}")
            return tag

        try:
            t1 = threading.Thread(target=lambda: sess.run(slow("a", 0.05)))
            t2 = threading.Thread(target=lambda: sess.run(slow("b", 0.01)))
            t1.start()
            time.sleep(0.01)
            t2.start()
            t1.join(timeout=5)
            t2.join(timeout=5)
        finally:
            loop.call_soon_threadsafe(loop.stop)
            bg_thread.join(timeout=2)
            loop.close()

        self.assertEqual(len(order), 4)
        self.assertEqual(order[0].split(":")[1], order[1].split(":")[1])


def _make_fake_youtube_page():
    class _MockPage:
        def __init__(self):
            self.url = "https://www.youtube.com/results?search_query=lo-fi"
            self._closed = False
            self._paused = False
            self._video_current_time = 42.0

        def is_closed(self):
            return self._closed

        async def evaluate(self, js, *a):
            from tests import yt_fakes as _yf
            from core import media_state as _ms
            if js in (_ms.STATE_JS, _ms.ACTION_JS):
                fake = _yf.FakeYouTubePage(url=self.url, paused=self._paused,
                                           current=self._video_current_time, duration=120.0)
                res = await fake.evaluate(js, *a)
                self._paused, self._video_current_time = fake.paused, fake.current
                return res
            if "visibilityState" in js:
                return True
            if "v.currentTime = 0" in js:
                self._video_current_time = 0.0
                self._paused = False
                return {"exists": True, "paused": False, "currentTime": 0.0}
            if "v.pause()" in js:
                self._paused = True
                return {"exists": True, "paused": True, "currentTime": 5.0}
            if "v.play()" in js:
                self._paused = False
                return {"exists": True, "paused": False, "currentTime": 5.0}
            if "document.querySelector('video')" in js:
                return {"exists": True, "paused": self._paused, "currentTime": self._video_current_time, "readyState": 4, "duration": 120.0}
            if "textarea" in js:
                return False
            return True

        async def title(self):
            return "Lo-Fi Mix - YouTube"

        def locator(self, sel):
            outer = self
            if "ytd-video-renderer" in sel:
                async def _ev(_s):
                    return [
                        {"position": 1, "id": "aaa11111111", "title": "First Mix", "channel": "C1"},
                        {"position": 2, "id": "bbb22222222", "title": "Second Mix", "channel": "C2"},
                        {"position": 3, "id": "ccc33333333", "title": "Third Mix", "channel": "C3"},
                    ]
                return SimpleNamespace(evaluate_all=_ev)
            for vid in ("aaa11111111", "bbb22222222", "ccc33333333"):
                if vid in sel:
                    async def _c(): return 1
                    async def _t(): return True
                    async def _clk(**_kw):
                        outer.url = f"https://www.youtube.com/watch?v={vid}"
                        outer._paused = False
                    return SimpleNamespace(count=_c, is_visible=_t, is_enabled=_t, click=_clk)
            async def _z(): return 0
            return SimpleNamespace(count=_z, inner_text=lambda timeout=1500: asyncio.sleep(0, result="Lo-Fi"))

        async def goto(self, url, **_kw):
            self.url = url

        async def go_back(self, **_kw):
            self.url = "https://www.youtube.com/results?search_query=lo-fi"

        async def wait_for_url(self, *a, **kw):
            return None

        async def close(self):
            self._closed = True

    return _MockPage()


class ReplayAndOcrFallbackTests(unittest.TestCase):
    def test_replay_video_and_next_video_stay_in_chrome(self):
        from actions import browser_control

        page = _make_fake_youtube_page()
        page.url = "https://www.youtube.com/watch?v=aaa11111111"
        page._paused = True

        sess = object.__new__(browser_control._BrowserSession)
        sess.browser_name = "chrome"
        sess._get_page = AsyncMock(return_value=page)

        replay_res = asyncio.run(sess.replay_video())
        self.assertIn("status=done", replay_res)
        self.assertIn("replayed=true", replay_res)
        self.assertEqual(page._video_current_time, 0.0)
        self.assertFalse(page._paused)

        replay_second = asyncio.run(sess.replay_video("replay the second video"))
        self.assertIn("status=done", replay_second)
        self.assertIn("bbb22222222", replay_second)
        self.assertIn("replayed=true", replay_second)

    def test_browser_control_ocr_fallback_when_dom_misses(self):
        from actions import browser_control
        from core import screenact, screenview

        fake_sess = MagicMock()
        fake_sess.browser_name = "chrome"
        fake_sess.run.side_effect = lambda coro: coro if not hasattr(coro, "close") else "No visible element matching 'Subscribe'."
        fake_sess.smart_click = lambda desc: "No visible element matching 'Subscribe'."

        fake_el = screenview.Element(
            eid="e1",
            role="text",
            name="Subscribe",
            value="",
            left=500,
            top=400,
            width=80,
            height=24,
        )
        fake_view = SimpleNamespace(elements=[fake_el])

        fake_reg = MagicMock()
        fake_reg.get.return_value = fake_sess
        fake_reg.pop_native_url.return_value = None

        with patch.object(browser_control, "_registry", fake_reg), \
             patch.object(browser_control, "select_browser", return_value="chrome"), \
             patch.object(config_manager, "get_local_screen_observation_enabled", return_value=True), \
             patch.object(screenact, "available", return_value=(True, "")), \
             patch.object(screenview, "_foreground", return_value=("chrome.exe", "YouTube - Google Chrome", 100, 1)), \
             patch.object(screenview, "look", return_value=fake_view), \
             patch.object(screenact, "click", return_value=screenact.Act(True, "click", point=(540, 412), detail="clicked")):
            out = browser_control._browser_control_impl(
                {"action": "smart_click", "description": "Subscribe", "browser": "chrome"}
            )
        self.assertIn("status=done", out)
        self.assertIn("method=ocr_fallback", out)
        self.assertIn("browser=chrome", out)


class VoiceCursorAndMouseTests(unittest.TestCase):
    def test_screenact_recovers_from_top_left_corner_and_supports_hold_release_multi_monitor(self):
        from core import screenact, screenview

        moved = []
        win32_calls = []
        cur = [0, 0]

        def _fake_move_to(x, y, duration=0):
            cur[0], cur[1] = int(x), int(y)
            moved.append((x, y))

        def _fake_win32(x, y):
            cur[0], cur[1] = int(x), int(y)
            win32_calls.append((x, y))
            return True

        class _FakeGui:
            FAILSAFE = True
            PAUSE = 0.0
            size = staticmethod(lambda: (1920, 1080))
            moveTo = staticmethod(_fake_move_to)
            click = staticmethod(lambda *a, **k: moved.append(("click", k)))
            mouseDown = staticmethod(lambda *a, **k: moved.append(("down", k)))
            mouseUp = staticmethod(lambda *a, **k: moved.append(("up", k)))

        with patch.object(screenact, "pyautogui", _FakeGui), \
             patch.object(screenact, "get_cursor_pos", side_effect=lambda: tuple(cur)), \
             patch.object(screenact, "_win32_set_cursor_pos", side_effect=_fake_win32), \
             patch.object(screenview, "virtual_rect", return_value={"left": -1920, "top": 0, "width": 3840, "height": 1080}), \
             patch.object(screenact, "_lands_on_self", return_value=False):
            mv = screenact.move_cursor(screenact.Point(-400, 300, origin="user", label="second_monitor"))
            self.assertTrue(mv.ok, mv.error)
            self.assertTrue(win32_calls, "Expected Win32 nudge off (0,0) before pyautogui.moveTo")
            self.assertIn((-400, 300), moved)

            cc = screenact.click_current(button="left", clicks=1)
            self.assertTrue(cc.ok, cc.error)
            md = screenact.mouse_down(button="left")
            self.assertTrue(md.ok, md.error)
            mu = screenact.mouse_up(button="left")
            self.assertTrue(mu.ok, mu.error)

    def test_voice_left_click_here_and_hold_release_authorized_and_executed(self):
        from actions import computer_control, screen_agent
        from core import desktop_control, screenact
        from core.request_context import RequestContext

        prev_mode = desktop_control.snapshot().get("mode") or "ON-DEMAND"
        desktop_control.set_mode("COMPUTER CONTROL")
        try:
            ctx = RequestContext(
                task_id="req_cursor_1",
                instruction="Left-click here.",
                source="voice",
                created_at=time.time(),
                expires_at=time.time() + 120.0,
                browser="chrome",
                browser_task_id="browser_task_1",
            )
            ok, reason = desktop_control.authorize_user_request(
                "Left-click here.", "desktop_ui", {"action": "left_click", "target": "here"},
                source="voice", auth_context=ctx,
            )
            self.assertTrue(ok, reason)

            ok_hold, reason_hold = desktop_control.authorize_user_request(
                "Hold the left mouse button.", "computer_control", {"action": "mouse_down", "button": "left"},
                source="voice", auth_context=ctx,
            )
            self.assertTrue(ok_hold, reason_hold)

            with patch.object(config_manager, "get_local_screen_observation_enabled", return_value=True), \
                 patch.object(screenact, "available", return_value=(True, "")), \
                 patch.object(screenact, "get_cursor_pos", return_value=(640, 360)), \
                 patch.object(screenact, "click_current", return_value=screenact.Act(True, "click", point=(640, 360), detail="clicked")), \
                 patch.object(screenact, "mouse_down", return_value=screenact.Act(True, "mouse_down", point=(640, 360), detail="held")), \
                 patch.object(screenact, "mouse_up", return_value=screenact.Act(True, "mouse_up", point=(640, 360), detail="released")):
                out_click = screen_agent.screen_agent({"action": "left_click", "target": "here"})
                self.assertIn("click", out_click)
                out_down = computer_control.computer_control({"action": "mouse_down", "button": "left"})
                self.assertIn("mouse_down", out_down)
                out_up = computer_control.computer_control({"action": "mouse_up", "button": "left"})
                self.assertIn("mouse_up", out_up)
        finally:
            desktop_control.set_mode(prev_mode)


class Section13GFullVoiceWorkflowTests(unittest.TestCase):
    def test_ten_step_voice_chrome_cursor_youtube_replay_workflow(self):
        from actions import browser_control, screen_agent
        from core import desktop_control, screenact
        from main import RemLive

        rem = RemLive.__new__(RemLive)
        rem._request_context_lock = threading.Lock()
        rem._request_context = None
        rem._last_user_command = ""
        rem._last_user_command_source = ""
        rem._active_browser_task = None

        page = _make_fake_youtube_page()
        ctx = SimpleNamespace(pages=[page])
        page.context = ctx
        sess = object.__new__(browser_control._BrowserSession)
        sess.browser_name = "chrome"
        sess.profile_kind = "real"
        sess._browser = None
        sess._spec = None
        sess._page = page
        sess._context = ctx
        sess.run = lambda coro, timeout=60: asyncio.run(coro)

        fake_reg = MagicMock()
        fake_reg.get.return_value = sess
        fake_reg.has.return_value = True
        fake_reg.pop_native_url.return_value = None
        fake_reg.active_browser.return_value = "chrome"

        prev_mode = desktop_control.snapshot().get("mode") or "ON-DEMAND"
        desktop_control.set_mode("COMPUTER CONTROL")
        try:
            with patch.object(config_manager, "get_local_screen_observation_enabled", return_value=True), \
                 patch.object(browser_control, "_registry", fake_reg), \
                 patch.object(browser_control, "_open_native", return_value="status=done action=browser_control opened=https://www.youtube.com/ browser=chrome"), \
                 patch.object(browser_control, "_resolve_browser", return_value=("chrome", {"executable_path": "chrome.exe"})), \
                 patch.object(browser_control, "_detect_running_browser", return_value="chrome"), \
                 patch.object(browser_control, "_detect_default_browser", return_value="edge"), \
                 patch.object(screenact, "available", return_value=(True, "")), \
                 patch.object(screenact, "move_to_screen_position", return_value=screenact.Act(True, "move", point=(1600, 120), detail="moved")), \
                 patch.object(screenact, "get_cursor_pos", return_value=(1600, 120)), \
                 patch.object(screenact, "click_current", return_value=screenact.Act(True, "click", point=(1600, 120), detail="clicked")):
                plan = [
                    ("Open YouTube in Chrome.", "browser_control", {"action": "go_to", "url": "https://www.youtube.com"}),
                    ("Search for lo-fi beats.", "browser_control", {"action": "search", "query": "lo-fi beats"}),
                    ("Move cursor to top_right.", "desktop_ui", {"action": "move", "position": "top_right"}),
                    ("Left-click here.", "desktop_ui", {"action": "left_click", "target": "here"}),
                    ("Play the second video.", "browser_control", {"action": "play_video", "description": "second video"}),
                    ("Pause the video.", "browser_control", {"action": "pause_video"}),
                    ("Resume the video.", "browser_control", {"action": "resume_video"}),
                    ("Replay this video.", "browser_control", {"action": "replay_video"}),
                    ("Play the third video.", "browser_control", {"action": "play_video", "description": "third video"}),
                    ("Close this tab.", "browser_control", {"action": "close_tab"}),
                ]
                for utterance, tool_name, tool_args in plan:
                    rem._record_user_request(utterance, "voice")
                    req_ctx = rem._current_request_context()
                    self.assertEqual(req_ctx.browser, "chrome", f"Lost Chrome binding at: {utterance}")
                    args = dict(tool_args)
                    if tool_name == "browser_control" and not args.get("browser"):
                        args["browser"] = req_ctx.browser
                    ok, reason = desktop_control.authorize_user_request(
                        req_ctx.instruction, tool_name, args, source="voice", auth_context=req_ctx
                    )
                    self.assertTrue(ok, f"{utterance} blocked: {reason}")
                    if tool_name == "desktop_ui":
                        out = screen_agent.screen_agent(args)
                        self.assertFalse(out.startswith("status=failed") or "FAILED" in out, f"{utterance}: {out}")
                    else:
                        out = browser_control._browser_control_impl(args)
                        self.assertIn("status=done", out, f"{utterance}: {out}")
                        self.assertNotIn("edge", out.casefold())
        finally:
            desktop_control.set_mode(prev_mode)


class FinalLiveInteractionRepairTests(unittest.TestCase):
    def test_running_chrome_without_cdp_never_spawns_separate_rem_profile_and_uses_visible_window(self):
        from core import screenact, screenview

        sess = browser_control._BrowserSession("chrome")
        sess._pw = MagicMock()
        with patch.object(browser_control, "_configured_browser_cdp_endpoint", return_value=""), \
             patch.object(browser_control, "_discover_running_cdp_endpoint", return_value=None), \
             patch.object(browser_control, "_resolve_browser", return_value={"engine": "chromium", "exe": "chrome.exe", "channel": "chrome"}), \
             patch.object(browser_control.appidentity, "running", return_value=["chrome.exe"] * 6), \
             patch.dict(os.environ, {}, clear=False):
            os.environ.pop("REM_ALLOW_SEPARATE_BROWSER_PROFILE", None)
            with self.assertRaises(browser_control._SeparateProfileBlockedError):
                asyncio.run(sess._launch())

        # Empty YouTube home in real visible Chrome window without CDP reports honestly
        el_home = screenview.Element(
            eid="e1", role="text", name="Try searching to get started",
            value="", left=350, top=300, width=400, height=40,
        )
        fake_view = SimpleNamespace(elements=[el_home], source="uia", title="YouTube - Google Chrome",
                                    monitor={"left": 0, "top": 0, "width": 1920, "height": 1080})
        fake_reg = MagicMock()
        fake_reg.has.return_value = False
        fake_reg.get_sync.return_value = {}
        with patch.object(browser_control, "_registry", fake_reg), \
             patch.object(browser_control, "select_browser", return_value="chrome"), \
             patch.object(browser_control, "_discover_running_cdp_endpoint", return_value=None), \
             patch.object(browser_control, "appidentity") as mock_id, \
             patch.object(screenview, "_foreground", return_value=("chrome.exe", "YouTube - Google Chrome", 4321, 9876)), \
             patch.object(screenview, "all_windows", return_value=[("chrome.exe", "YouTube - Google Chrome")]), \
             patch.object(screenview, "look", return_value=fake_view):
            mock_id.running.return_value = ["chrome.exe"]
            res = browser_control._browser_control_impl(
                {"action": "play_video", "description": "second video", "browser": "chrome"}
            )
        self.assertIn("Try searching to get started", res)
        self.assertIn("0 visible video results", res)
        fake_reg.get.assert_not_called()

    def test_native_visible_chrome_selects_second_result_with_real_cursor_and_sync_invalidation(self):
        from core import screenact, screenview

        addr = screenview.Element(
            eid="a1", role="edit", name="Address and search bar",
            value="https://www.youtube.com/results?search_query=Savarana",
            left=240, top=48, width=700, height=32,
        )
        v1 = screenview.Element(eid="v1", role="link", name="Savarana Live Mix 1", value="", left=260, top=220, width=420, height=36)
        v2 = screenview.Element(eid="v2", role="link", name="Savarana Mix Rage Official", value="", left=260, top=410, width=420, height=36)
        ad = screenview.Element(eid="ad1", role="link", name="Sponsored: Shop headphones", value="", left=260, top=160, width=420, height=36)
        v_nav = screenview.Element(eid="nav1", role="link", name="Home", value="", left=20, top=160, width=100, height=36)
        fake_view = SimpleNamespace(
            elements=[addr, v_nav, ad, v1, v2],
            source="uia",
            title="Savarana - YouTube - Google Chrome",
            monitor={"left": 0, "top": 0, "width": 1920, "height": 1080},
        )
        after_view = SimpleNamespace(
            elements=[addr],
            source="uia",
            title="Savarana Mix Rage Official - YouTube - Google Chrome",
            monitor={"left": 0, "top": 0, "width": 1920, "height": 1080},
        )
        moves, clicks = [], []
        fake_reg = MagicMock()
        fake_reg.has.return_value = False
        with patch.object(browser_control, "_registry", fake_reg), \
             patch.object(browser_control, "select_browser", return_value="chrome"), \
             patch.object(browser_control, "_discover_running_cdp_endpoint", return_value=None), \
             patch.object(browser_control.appidentity, "running", return_value=["chrome.exe"]), \
             patch.object(screenview, "_foreground", side_effect=[
                 ("chrome.exe", "Savarana - YouTube - Google Chrome", 4321, 9876),
                 ("chrome.exe", "Savarana - YouTube - Google Chrome", 4321, 9876),
                 ("chrome.exe", "Savarana Mix Rage Official - YouTube - Google Chrome", 4321, 9876),
             ]), \
             patch.object(screenview, "all_windows", return_value=[("chrome.exe", "Savarana - YouTube - Google Chrome")]), \
             patch.object(screenview, "look", side_effect=[fake_view, after_view]), \
             patch.object(screenact, "move_cursor", side_effect=lambda pt: moves.append((pt.x, pt.y)) or screenact.Act(True, "move", point=(pt.x, pt.y))), \
             patch.object(screenact, "click", side_effect=lambda pt: clicks.append((pt.x, pt.y)) or screenact.Act(True, "click", point=(pt.x, pt.y))):
            out = browser_control._browser_control_impl(
                {"action": "smart_click", "description": "second thumbnail", "browser": "chrome"}
            )
        self.assertIn("status=done", out)
        self.assertIn("control_mode=native_windows", out)
        self.assertIn("cdp_available=false", out)
        self.assertIn("Savarana Mix Rage Official", out)
        self.assertIn("playback_verified=false", out)
        self.assertTrue(moves and clicks)
        # Cursor landed on v2 (260 + 210, 410 + 18) = (470, 428), skipping ad & Home nav
        self.assertEqual(clicks[0], (470, 428))
        self.assertGreaterEqual(fake_reg.invalidate_sync.call_count, 2)

    def test_ordinal_thumbnail_parsing_and_coordinate_cursor_move_and_voice_race_auth(self):
        from actions import computer_control
        from core import screenact
        from main import RemLive

        self.assertEqual(browser_control._parse_ordinal_index("second thumbnail"), 1)
        self.assertEqual(browser_control._parse_ordinal_index("third card"), 2)

        prev_mode = desktop_control.snapshot().get("mode") or "ON-DEMAND"
        desktop_control.set_mode("COMPUTER CONTROL")
        try:
            # Voice commands with coordinates and thumbnail descriptions pass authorization cleanly
            ok_move, r_move = desktop_control.authorize_user_request(
                "Move the cursor to 500, 300.", "desktop_ui", {"action": "move", "target": "500, 300"}, source="voice"
            )
            self.assertTrue(ok_move, r_move)
            ok_thumb, r_thumb = desktop_control.authorize_user_request(
                "Play the second video.", "browser_control", {"action": "smart_click", "description": "second thumbnail", "browser": "chrome"}, source="voice"
            )
            self.assertTrue(ok_thumb, r_thumb)
            # Sensitive message remains blocked without explicit recipient/send ask
            ok_bad, _ = desktop_control.authorize_user_request(
                "Open YouTube in Chrome.", "send_message", {"receiver": "Alex", "message_text": "hi"}, source="voice"
            )
            self.assertFalse(ok_bad)

            moved_pts = []
            with patch.object(config_manager, "get_local_screen_observation_enabled", return_value=True), \
                 patch.object(screenact, "available", return_value=(True, "")), \
                 patch.object(screenact, "move_cursor", side_effect=lambda pt: moved_pts.append((pt.x, pt.y)) or screenact.Act(True, "move", point=(pt.x, pt.y))):
                res = screen_agent.screen_agent({"action": "move", "target": "500, 300"})
                self.assertIn("move", res)
                self.assertEqual(moved_pts[-1], (500, 300))

            with patch.object(screenact, "_move_to", return_value=((500, 300), True)), \
                 patch.object(computer_control, "_ensure_control_active"), \
                 patch.object(computer_control, "_require_pyautogui"):
                cc_res = computer_control.computer_control({"action": "move", "target": "500, 300"})
                self.assertIn("(500, 300)", cc_res)

            # Simulate live transcription streaming race: tool_call arrives before trailing transcript lands
            rem = RemLive.__new__(RemLive)
            rem._request_context_lock = threading.Lock()
            rem._request_context = None
            rem._last_user_command = ""
            rem._last_user_command_source = ""
            rem._active_browser_task = None
            rem._active_tool_calls = 0
            rem._last_user_speech = time.monotonic()
            rem._record_user_request("Move the", "voice")
            partial_ctx = rem._current_request_context()
            rem._action_registry = MagicMock()
            rem._action_registry.has.return_value = True
            rem._action_registry.scheduling.return_value = None
            rem._action_registry.run.return_value = "move moved"
            rem._plugin_registry = MagicMock()
            rem._plugin_registry.scheduling.return_value = None
            rem.ui = MagicMock()
            rem.ui.muted = False
            rem.ui.current_file = ""

            fc = SimpleNamespace(id="fc_1", name="desktop_ui", args={"action": "move", "target": "500, 300"})

            async def _race_run():
                async def _finish_stt():
                    await asyncio.sleep(0.05)
                    rem._record_user_request("Move the cursor to 500, 300.", "voice")

                t = asyncio.create_task(_finish_stt())
                resp = await rem._execute_tool_inner(fc, request_context=partial_ctx)
                await t
                return resp

            resp = asyncio.run(_race_run())
            self.assertIn("move moved", resp.response["result"])
        finally:
            desktop_control.set_mode(prev_mode)

    def test_twelve_step_full_voice_savarana_chrome_workflow(self):
        from core import screenact
        from main import RemLive

        rem = RemLive.__new__(RemLive)
        rem._request_context_lock = threading.Lock()
        rem._request_context = None
        rem._last_user_command = ""
        rem._last_user_command_source = ""
        rem._active_browser_task = None

        page = _make_fake_youtube_page()
        page.url = "https://www.youtube.com/results?search_query=Savarana"
        ctx = SimpleNamespace(pages=[page])
        page.context = ctx
        sess = object.__new__(browser_control._BrowserSession)
        sess.browser_name = "chrome"
        sess.profile_kind = "attached"
        sess._browser = None
        sess._spec = None
        sess._page = page
        sess._context = ctx
        sess.run = lambda coro, timeout=60: asyncio.run(coro)
        sess.locate_element_screen_rect = AsyncMock(return_value={"x": 450, "y": 95, "label": "Search"})

        fake_reg = MagicMock()
        fake_reg.get.return_value = sess
        fake_reg.has.return_value = True
        fake_reg.pop_native_url.return_value = None
        fake_reg.active_browser.return_value = "chrome"

        prev_mode = desktop_control.snapshot().get("mode") or "ON-DEMAND"
        desktop_control.set_mode("COMPUTER CONTROL")
        try:
            with patch.object(config_manager, "get_local_screen_observation_enabled", return_value=True), \
                 patch.object(browser_control, "_registry", fake_reg), \
                 patch.object(browser_control, "_open_native", return_value="status=done action=browser_control opened=https://www.youtube.com/ browser=chrome"), \
                 patch.object(browser_control, "_resolve_browser", return_value={"engine": "chromium", "exe": "chrome.exe", "channel": "chrome"}), \
                 patch.object(browser_control, "_detect_running_browser", return_value="chrome"), \
                 patch.object(browser_control, "_detect_default_browser", return_value="edge"), \
                 patch.object(screenact, "available", return_value=(True, "")), \
                 patch.object(screenact, "move_cursor", return_value=screenact.Act(True, "move", point=(450, 95), detail="moved")), \
                 patch.object(screenact, "get_cursor_pos", return_value=(450, 95)), \
                 patch.object(screenact, "click_current", return_value=screenact.Act(True, "click", point=(450, 95), detail="clicked")):
                steps = [
                    ("Open YouTube in Chrome.", "browser_control", {"action": "go_to", "url": "https://www.youtube.com"}),
                    ("Search for Savarana.", "browser_control", {"action": "search", "query": "Savarana", "engine": "youtube"}),
                    ("Inspect the visible results.", "browser_control", {"action": "inspect"}),
                    ("Play the second actual video.", "browser_control", {"action": "play_video", "description": "second video"}),
                    ("Pause.", "browser_control", {"action": "pause_video"}),
                    ("Resume.", "browser_control", {"action": "resume_video"}),
                    ("Replay.", "browser_control", {"action": "replay_video"}),
                    ("Move cursor to the search box.", "desktop_ui", {"action": "move", "target": "search box"}),
                    ("Left-click.", "desktop_ui", {"action": "left_click", "target": "here"}),
                    ("Type Mix Rage.", "browser_control", {"action": "smart_type", "description": "search box", "text": "Mix Rage"}),
                    ("Search again for Mix Rage.", "browser_control", {"action": "search", "query": "Mix Rage", "engine": "youtube"}),
                    ("Play the third actual result.", "browser_control", {"action": "play_video", "description": "third video"}),
                ]
                for utterance, tool_name, tool_args in steps:
                    rem._record_user_request(utterance, "voice")
                    req_ctx = rem._current_request_context()
                    self.assertEqual(req_ctx.browser, "chrome", f"Lost Chrome binding at: {utterance}")
                    args = dict(tool_args)
                    if tool_name == "browser_control" and not args.get("browser"):
                        args["browser"] = req_ctx.browser
                    ok, reason = desktop_control.authorize_user_request(
                        req_ctx.instruction, tool_name, args, source="voice", auth_context=req_ctx
                    )
                    self.assertTrue(ok, f"{utterance} blocked: {reason}")
                    if tool_name == "desktop_ui":
                        out = screen_agent.screen_agent(args)
                        self.assertFalse(out.startswith("status=failed") or "FAILED" in out, f"{utterance}: {out}")
                    else:
                        if args.get("action") == "smart_type":
                            sess.smart_type = AsyncMock(return_value="status=done text_entered=true entered_text='Mix Rage'")
                        out = browser_control._browser_control_impl(args)
                        self.assertIn("status=done", out, f"{utterance}: {out}")
                        self.assertNotIn("edge", out.casefold())
        finally:
            desktop_control.set_mode(prev_mode)


class ScreenPermissionRestorationAndStatusTests(unittest.TestCase):
    def test_persistent_uia_toggle_off_and_emergency_stop_never_revoke_local_observation(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "api_keys.json"
            cfg.write_text("{}", encoding="utf-8")
            with patch.object(config_manager, "CONFIG_FILE", cfg):
                self.assertTrue(config_manager.get_local_screen_observation_enabled())
                self.assertFalse(config_manager.get_cloud_screen_vision_enabled())
                self.assertFalse(config_manager.is_screen_access_revoked())

                # Turning background persistent UIA polling OFF must NOT disable on-demand local observation!
                config_manager.save_persistent_desktop_observation_enabled(False)
                self.assertTrue(config_manager.get_local_screen_observation_enabled())
                self.assertFalse(config_manager.is_screen_access_revoked())

                st = desktop_control.get_screen_permission_status()
                self.assertEqual(st["runtime_status"], "GRANTED")
                self.assertTrue(st["local_observation_enabled"])
                self.assertFalse(st["cloud_image_sharing_enabled"])

                # Voice observation works cleanly when local observation is enabled
                ok, reason = desktop_control.authorize_user_request(
                    "Can you see my screen?", "desktop_ui", {"action": "observe"}, source="voice"
                )
                self.assertTrue(ok, reason)

                # Explicit revocation IS respected until user explicitly re-enables
                config_manager.revoke_screen_access()
                self.assertFalse(config_manager.get_local_screen_observation_enabled())
                self.assertTrue(config_manager.is_screen_access_revoked())
                self.assertEqual(desktop_control.get_screen_permission_status()["runtime_status"], "REVOKED")
                ok_rev, reason_rev = desktop_control.authorize_user_request(
                    "Can you see my screen?", "desktop_ui", {"action": "observe"}, source="voice"
                )
                self.assertFalse(ok_rev)
                self.assertIn("revoked", reason_rev.lower())

                # Re-enabling local screen observation clears revocation while keeping cloud disabled
                config_manager.save_local_screen_observation_enabled(True)
                self.assertTrue(config_manager.get_local_screen_observation_enabled())
                self.assertFalse(config_manager.is_screen_access_revoked())
                self.assertFalse(config_manager.get_cloud_screen_vision_enabled())
                ok_restored, reason_restored = desktop_control.authorize_user_request(
                    "Can you see my screen?", "desktop_ui", {"action": "observe"}, source="voice"
                )
                self.assertTrue(ok_restored, reason_restored)

    def test_expired_authorization_is_not_classified_as_revoked_and_unrelated_tasks_isolated(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "api_keys.json"
            cfg.write_text(json.dumps({"local_screen_observation_enabled": True, "cloud_screen_vision_enabled": False}), encoding="utf-8")
            with patch.object(config_manager, "CONFIG_FILE", cfg):
                expired = RequestContext(
                    task_id="exp_1", instruction="Can you see my screen?", source="voice",
                    created_at=time.monotonic() - 600.0, expires_at=time.monotonic() - 10.0,
                )
                self.assertTrue(expired.is_expired())
                self.assertEqual(desktop_control.get_screen_permission_status()["runtime_status"], "GRANTED")

                # Disabled without explicit revoke reports DISABLED (not REVOKED)
                config_manager.save_local_screen_observation_enabled(False)
                st_dis = desktop_control.get_screen_permission_status()
                self.assertEqual(st_dis["runtime_status"], "DISABLED")
                self.assertFalse(st_dis["explicitly_revoked"])

                # Unrelated destructive/external requests cannot hijack observation turn
                config_manager.save_local_screen_observation_enabled(True)
                ok_send, _ = desktop_control.authorize_user_request(
                    "Can you see my screen?", "send_message",
                    {"receiver": "Alex", "message_text": "hi"}, source="voice",
                )
                self.assertFalse(ok_send)


class YouTubeSearchBarDetectionAndClickTests(unittest.TestCase):
    def test_youtube_search_bar_uia_and_ocr_exclude_chrome_omnibox_and_right_click(self):
        from core import screenact, screenview

        addr = screenview.Element(
            eid="omnibox", role="edit", name="Address and search bar",
            value="https://www.youtube.com/", left=220, top=42, width=820, height=34,
        )
        yt_search = screenview.Element(
            eid="yt_sb", role="combobox", name="Search",
            value="", left=560, top=118, width=500, height=36,
        )
        fake_view = SimpleNamespace(
            app="chrome.exe", title="YouTube - Google Chrome",
            source="uia", elements=[addr, yt_search],
            monitor={"left": 0, "top": 0, "width": 1920, "height": 1080},
        )
        moved, rclicked = [], []
        with patch.object(config_manager, "get_local_screen_observation_enabled", return_value=True), \
             patch.object(browser_control, "_registry") as fake_reg, \
             patch.object(browser_control, "select_browser", return_value="chrome"), \
             patch.object(browser_control, "_discover_running_cdp_endpoint", return_value=None), \
             patch.object(browser_control.appidentity, "running", return_value=["chrome.exe"]), \
             patch.object(screenview, "_foreground", return_value=("chrome.exe", "YouTube - Google Chrome", 101, 202)), \
             patch.object(screenview, "all_windows", return_value=[("chrome.exe", "YouTube - Google Chrome")]), \
             patch.object(screenview, "look", return_value=fake_view), \
             patch.object(screenact, "available", return_value=(True, "")), \
             patch.object(screenact, "move_cursor", side_effect=lambda pt: moved.append((pt.x, pt.y)) or screenact.Act(True, "move", point=(pt.x, pt.y))), \
             patch.object(screenact, "right_click", side_effect=lambda pt, **_k: rclicked.append((pt.x, pt.y)) or screenact.Act(True, "right_click", point=(pt.x, pt.y))):
            fake_reg.has.return_value = False
            ok, reason = desktop_control.authorize_user_request(
                "Right-click the YouTube search bar.", "desktop_ui",
                {"action": "right_click", "target": "YouTube search bar"}, source="voice",
            )
            self.assertTrue(ok, reason)
            out = screen_agent.screen_agent({"action": "right_click", "target": "YouTube search bar"})
            self.assertIn("right_click", out)
            # Must hit yt_search (560+250=810, 118+18=136), NEVER Chrome's address bar at top=42
            self.assertEqual(rclicked[-1], (810, 136))

            # Verify browser_control right_click in native non-CDP Chrome uses the same search bar
            b_out = browser_control._browser_control_impl(
                {"action": "right_click", "description": "YouTube search bar", "browser": "chrome"}
            )
            self.assertIn("status=done", b_out)
            self.assertIn("cdp_available=false", b_out)
            self.assertEqual(rclicked[-1], (810, 136))

        # OCR fallback when UIA combobox is missing
        yt_ocr = screenview.Element(
            eid="ocr_sb", role="ocr_text", name="Search",
            value="", left=600, top=122, width=140, height=26,
        )
        ocr_view = SimpleNamespace(
            app="chrome.exe", title="YouTube - Google Chrome",
            source="ocr", elements=[addr, yt_ocr],
            monitor={"left": 0, "top": 0, "width": 1920, "height": 1080},
        )
        ocr_clicks = []
        with patch.object(browser_control, "_registry") as fake_reg, \
             patch.object(browser_control, "select_browser", return_value="chrome"), \
             patch.object(browser_control, "_discover_running_cdp_endpoint", return_value=None), \
             patch.object(browser_control.appidentity, "running", return_value=["chrome.exe"]), \
             patch.object(screenview, "_foreground", return_value=("chrome.exe", "YouTube - Google Chrome", 101, 202)), \
             patch.object(screenview, "all_windows", return_value=[("chrome.exe", "YouTube - Google Chrome")]), \
             patch.object(screenview, "look", return_value=ocr_view), \
             patch.object(screenact, "move_cursor", side_effect=lambda pt: screenact.Act(True, "move", point=(pt.x, pt.y))), \
             patch.object(screenact, "click", side_effect=lambda pt: ocr_clicks.append((pt.x, pt.y)) or screenact.Act(True, "click", point=(pt.x, pt.y))):
            fake_reg.has.return_value = False
            c_out = browser_control._browser_control_impl(
                {"action": "left_click", "description": "YouTube search bar", "browser": "chrome"}
            )
        self.assertIn("status=done", c_out)
        self.assertEqual(ocr_clicks[-1], (670, 135))

        # Non-YouTube page must never confuse Chrome address bar with YouTube search bar
        wiki_view = SimpleNamespace(
            app="chrome.exe", title="Wikipedia - Google Chrome",
            source="uia", elements=[addr],
            monitor={"left": 0, "top": 0, "width": 1920, "height": 1080},
        )
        with patch.object(browser_control, "_registry") as fake_reg, \
             patch.object(browser_control, "select_browser", return_value="chrome"), \
             patch.object(browser_control, "_discover_running_cdp_endpoint", return_value=None), \
             patch.object(browser_control.appidentity, "running", return_value=["chrome.exe"]), \
             patch.object(screenview, "_foreground", return_value=("chrome.exe", "Wikipedia - Google Chrome", 101, 202)), \
             patch.object(screenview, "all_windows", return_value=[("chrome.exe", "Wikipedia - Google Chrome")]), \
             patch.object(screenview, "look", return_value=wiki_view):
            fake_reg.has.return_value = False
            fail_out = browser_control._browser_control_impl(
                {"action": "right_click", "description": "YouTube search bar", "browser": "chrome"}
            )
        self.assertIn("status=failed", fail_out)


class VoiceDesktopAutonomyLanguageAndMergeSortTests(unittest.TestCase):
    def test_contextual_merge_sort_search_keyboard_and_second_video_workflow(self):
        from actions import computer_control
        from core import confirm, screenact, screenview
        from main import RemLive

        rem = RemLive.__new__(RemLive)
        rem._request_context_lock = threading.Lock()
        rem._request_context = None
        rem._last_user_command = ""
        rem._last_user_command_source = ""
        rem._active_browser_task = None

        addr = screenview.Element(
            eid="addr1", role="edit", name="Address and search bar",
            value="https://www.youtube.com/", left=220, top=42, width=820, height=34,
        )
        yt_sb = screenview.Element(
            eid="yt1", role="combobox", name="Search", value="",
            left=560, top=118, width=500, height=36, focused=True,
        )
        v1 = screenview.Element(
            eid="vid1", role="link", name="Merge Sort Algorithm in 5 Minutes",
            value="", left=260, top=220, width=420, height=36,
        )
        v2 = screenview.Element(
            eid="vid2", role="link", name="Merge Sort Visualized Step by Step",
            value="", left=260, top=410, width=420, height=36,
        )
        view_home = SimpleNamespace(
            app="chrome.exe", title="YouTube - Google Chrome", source="uia",
            elements=[addr, yt_sb], monitor={"left": 0, "top": 0, "width": 1920, "height": 1080},
            digest="d1", stable_digest="d1", degraded=False, reason="", scan_ms=10.0, windows=[],
        )
        view_results = SimpleNamespace(
            app="chrome.exe", title="merge sort - YouTube - Google Chrome", source="uia",
            elements=[addr, yt_sb, v1, v2], monitor={"left": 0, "top": 0, "width": 1920, "height": 1080},
            digest="d2", stable_digest="d2", degraded=False, reason="", scan_ms=10.0, windows=[],
        )
        view_watch = SimpleNamespace(
            app="chrome.exe", title="Merge Sort Visualized Step by Step - YouTube - Google Chrome", source="uia",
            elements=[addr], monitor={"left": 0, "top": 0, "width": 1920, "height": 1080},
            digest="d3", stable_digest="d3", degraded=False, reason="", scan_ms=10.0, windows=[],
        )

        typed_once, pressed_keys, clicked_pts = [], [], []
        prev_mode = desktop_control.snapshot().get("mode") or "ON-DEMAND"
        desktop_control.set_mode("COMPUTER CONTROL")
        try:
            with patch.object(config_manager, "get_local_screen_observation_enabled", return_value=True), \
                 patch.object(browser_control, "_registry") as fake_reg, \
                 patch.object(browser_control, "select_browser", return_value="chrome"), \
                 patch.object(browser_control, "_discover_running_cdp_endpoint", return_value=None), \
                 patch.object(browser_control.appidentity, "running", return_value=["chrome.exe"]), \
                 patch.object(screenact, "available", return_value=(True, "")), \
                 patch.object(screenact, "move_cursor", side_effect=lambda pt: screenact.Act(True, "move", point=(pt.x, pt.y))), \
                 patch.object(screenact, "click", side_effect=lambda pt, **_k: clicked_pts.append((pt.x, pt.y)) or screenact.Act(True, "click", point=(pt.x, pt.y))), \
                 patch.object(screenact, "type_text", side_effect=lambda txt, **_k: typed_once.append(txt) or screenact.Act(True, "type", detail=f"typed {len(txt)}")), \
                 patch.object(screenact, "press", side_effect=lambda key, **_k: pressed_keys.append(key) or screenact.Act(True, "press", detail=key)):
                fake_reg.has.return_value = False

                # Step 0: "Open YouTube in Chrome." binds active Chrome browser task
                rem._record_user_request("Open YouTube in Chrome.", "voice")
                ctx0 = rem._current_request_context()
                self.assertEqual(ctx0.browser, "chrome")

                # Step 1: Click the YouTube search bar
                rem._record_user_request("Click the YouTube search bar.", "voice")
                ctx1 = rem._current_request_context()
                self.assertEqual(ctx1.browser, "chrome")
                self.assertTrue(desktop_control.authorize_user_request(
                    ctx1.instruction, "browser_control",
                    {"action": "left_click", "description": "YouTube search bar", "browser": "chrome"},
                    source="voice", auth_context=ctx1)[0])
                with patch.object(screenview, "_foreground", return_value=("chrome.exe", "YouTube - Google Chrome", 101, 202)), \
                     patch.object(screenview, "all_windows", return_value=[("chrome.exe", "YouTube - Google Chrome")]), \
                     patch.object(screenview, "look", return_value=view_home):
                    r1 = browser_control._browser_control_impl(
                        {"action": "left_click", "description": "YouTube search bar", "browser": "chrome"}
                    )
                self.assertIn("status=done", r1)

                # Step 2: Contextual follow-up "Type merge sort." (empty target -> focused field, typed ONCE)
                rem._record_user_request("Type merge sort.", "voice")
                ctx2 = rem._current_request_context()
                self.assertEqual(ctx2.browser, "chrome")
                self.assertTrue(desktop_control.authorize_user_request(
                    ctx2.instruction, "desktop_ui", {"action": "type", "text": "merge sort"},
                    source="voice", auth_context=ctx2)[0])
                self.assertTrue(desktop_control.authorize_user_request(
                    ctx2.instruction, "browser_control",
                    {"action": "smart_type", "description": "YouTube search bar", "text": "merge sort", "browser": "chrome"},
                    source="voice", auth_context=ctx2)[0])
                with patch.object(screenview, "look", return_value=view_home):
                    r2 = screen_agent.screen_agent({"action": "type", "text": "merge sort"})
                self.assertIn("type", r2)
                self.assertEqual(typed_once, ["merge sort"])

                # Step 3: Contextual follow-up keyboard commands ("Press Enter", "Backspace", "Select all")
                for cmd, key_arg, expected_norm in [
                    ("Press Enter.", "Enter", "enter"),
                    ("Press Backspace.", "Backspace", "backspace"),
                    ("Select all.", "Select all", "ctrl+a"),
                ]:
                    rem._record_user_request(cmd, "voice")
                    c_k = rem._current_request_context()
                    self.assertEqual(c_k.browser, "chrome")
                    ok_k, why_k = desktop_control.authorize_user_request(
                        c_k.instruction, "desktop_ui", {"action": "press", "key": key_arg},
                        source="voice", auth_context=c_k,
                    )
                    self.assertTrue(ok_k, f"{cmd}: {why_k}")
                    with patch.object(screenview, "look", return_value=view_home), \
                         patch.object(confirm, "request", side_effect=AssertionError("Enter in searchbox should not prompt form confirm")):
                        r_k = screen_agent.screen_agent({"action": "press", "key": key_arg})
                    self.assertNotIn("Confirm", r_k)
                    self.assertEqual(pressed_keys[-1], expected_norm)

                # Step 4: "Play the second video." -> identifies real title & clicks with Windows mouse
                rem._record_user_request("Play the second video.", "voice")
                ctx4 = rem._current_request_context()
                self.assertTrue(desktop_control.authorize_user_request(
                    ctx4.instruction, "browser_control",
                    {"action": "play_video", "description": "second video", "browser": "chrome"},
                    source="voice", auth_context=ctx4)[0])
                with patch.object(screenview, "_foreground", side_effect=[
                         ("chrome.exe", "merge sort - YouTube - Google Chrome", 101, 202),
                         ("chrome.exe", "merge sort - YouTube - Google Chrome", 101, 202),
                         ("chrome.exe", "Merge Sort Visualized Step by Step - YouTube - Google Chrome", 101, 202),
                     ]), \
                     patch.object(screenview, "all_windows", return_value=[("chrome.exe", "merge sort - YouTube - Google Chrome")]), \
                     patch.object(screenview, "look", side_effect=[view_results, view_watch]):
                    r4 = browser_control._browser_control_impl(
                        {"action": "play_video", "description": "second video", "browser": "chrome"}
                    )
                self.assertIn("status=done", r4)
                self.assertIn("Merge Sort Visualized Step by Step", r4)
                self.assertEqual(clicked_pts[-1], (470, 428))

                # Sensitive command stays blocked during the same session
                ok_pay, _ = desktop_control.authorize_user_request(
                    "Play the second video.", "send_message",
                    {"receiver": "Alex", "message_text": "hello"}, source="voice", auth_context=ctx4,
                )
                self.assertFalse(ok_pay)
        finally:
            desktop_control.set_mode(prev_mode)

    def test_language_detection_prompt_personality_and_tool_result_integrity(self):
        from core.conversation import detect_language, format_tool_result, tool_result_guidance
        from main import PROMPT_PATH

        self.assertEqual(detect_language("Play the second video in Chrome."), "English")
        self.assertEqual(detect_language("Click the YouTube search bar and type merge sort."), "English")
        self.assertEqual(detect_language("मेरी स्क्रीन देखो"), "Hindi")
        self.assertEqual(detect_language("meri screen par kya hai batao"), "Hindi")

        # Verified tool results containing cdp_available=false / UIA / coordinates must NEVER flip to failed
        done_msg = (
            "status=done action=browser_control control_mode=native_windows "
            "cdp_available=false title='Merge Sort Visualized Step by Step' verified=true"
        )
        self.assertEqual(format_tool_result("browser_control", done_msg), done_msg)
        self.assertIn("actual video title", tool_result_guidance(done_msg))

        prompt_txt = PROMPT_PATH.read_text(encoding="utf-8")
        self.assertIn("Tarun-kun", prompt_txt)
        self.assertIn("My protocols prevent me from", prompt_txt)
        self.assertIn("fluent, natural conversational English", prompt_txt)

    def test_visual_grounding_never_hallucinates_when_uia_and_ocr_are_empty(self):
        from core import screen_observation, screenview
        from actions import browser_control

        empty_ocr_view = SimpleNamespace(
            app="chrome.exe", title="YouTube - Google Chrome", source="ocr",
            degraded=True, reason="OCR returned no text",
            monitor={"left": 0, "top": 0, "width": 1920, "height": 1080},
            elements=[], windows=[],
        )
        with patch.object(screenview, "look", return_value=empty_ocr_view), \
             patch.object(browser_control, "inspect_open_chrome_tabs_local", return_value=["chrome.exe: YouTube - Google Chrome"]):
            info = screen_observation.inspect_local_screen("What videos are on YouTube?")
        self.assertIn("uia_items=0", info)
        self.assertIn("ocr_items=0", info)
        self.assertIn("extracted_text_count=0", info)
        self.assertIn("confidence=uncertain", info)
        self.assertIn("page_state=unverified", info)
        self.assertIn("target_identified=False", info)
        self.assertIn("Observation uncertainty:", info)

        # When view.source == "ocr" actually extracted OCR items, count them accurately
        ocr_el = screenview.Element(
            eid=1, role=screenview.ROLE_TEXT, name="Merge Sort in 3 Minutes",
            value="", left=300, top=400, width=260, height=30,
            enabled=True, focused=False, scrollable=False, depth=0,
        )
        pop_ocr_view = SimpleNamespace(
            app="chrome.exe", title="short - YouTube - Google Chrome", source="ocr",
            degraded=True, reason="Using local OCR",
            monitor={"left": 0, "top": 0, "width": 1920, "height": 1080},
            elements=[ocr_el], windows=[],
        )
        with patch.object(screenview, "look", return_value=pop_ocr_view), \
             patch.object(browser_control, "inspect_open_chrome_tabs_local", return_value=[]):
            info_ocr = screen_observation.inspect_local_screen("What videos are on YouTube?")
        self.assertIn("uia_items=0", info_ocr)
        self.assertIn("ocr_items=1", info_ocr)
        self.assertIn("extracted_text_count=1", info_ocr)
        self.assertIn("Merge Sort in 3 Minutes", info_ocr)

    def test_scan_ocr_grants_post_load_window_after_cold_reader_init(self):
        from core import screenview
        from PIL import Image

        class FakeResult:
            boxes = [[[10, 10], [110, 10], [110, 40], [10, 40]]]
            txts = ["Search"]
            scores = [0.99]

        def slow_reader():
            # Simulate cold load finishing slightly after initial tight deadline
            time.sleep(0.05)
            screenview._ocr_engine = lambda _img: FakeResult()
            return screenview._ocr_engine

        prev_engine = screenview._ocr_engine
        screenview._ocr_engine = None
        try:
            img = Image.new("RGB", (300, 100), (20, 20, 20))
            mon = {"left": 0, "top": 0, "width": 300, "height": 100}
            with patch.object(screenview, "_ocr_reader", side_effect=slow_reader), \
                 patch.object(screenview, "capture", return_value=(img, mon, 1.0)):
                els = screenview._scan_ocr(mon, time.monotonic() + 0.02)
            self.assertEqual(len(els), 1)
            self.assertEqual(els[0].name, "Search")
        finally:
            screenview._ocr_engine = prev_engine

    def test_short_followup_reply_and_task_id_stability_plus_deny_diagnostics(self):
        from main import RemLive
        from core import desktop_control
        import io
        from contextlib import redirect_stdout

        rem = RemLive.__new__(RemLive)
        rem._request_context_lock = threading.Lock()
        rem._dispatch_context_local = threading.local()
        rem._request_context = None
        rem._last_user_command = ""
        rem._last_user_command_source = ""
        rem._active_browser_task = None
        rem._last_assistant_question = ""
        rem._current_voice_turn_task_id = ""

        # Step 1: User asks to type in the YouTube search bar
        rem._record_user_request("Just type in the search bar of the YouTube.", "voice")
        c1 = rem._current_request_context()
        self.assertTrue(c1.task_id)
        self.assertTrue(c1.browser_task_id)

        # Assistant asks clarification question -> short single-word reply "short"
        rem._last_assistant_question = "What would you like me to type?"
        rem._record_user_request("short", "voice")
        c2 = rem._current_request_context()
        self.assertEqual(c2.task_id, c1.task_id)
        self.assertEqual(c2.browser_task_id, c1.browser_task_id)
        self.assertIn("search bar", c2.parent_instruction.casefold())

        # Both browser_control smart_type and screen_agent type must be authorized for "short"
        ok_b, why_b = desktop_control.authorize_user_request(
            c2.instruction, "browser_control",
            {"action": "smart_type", "description": "YouTube search bar", "text": "short"},
            source="voice", auth_context=c2,
        )
        self.assertTrue(ok_b, why_b)
        ok_s, why_s = desktop_control.authorize_user_request(
            c2.instruction, "screen_agent",
            {"action": "type", "target": "YouTube search bar", "text": "short"},
            source="voice", auth_context=c2,
        )
        self.assertTrue(ok_s, why_s)

        # Follow-up Enter preserves task_id
        rem._record_user_request("Press Enter.", "voice")
        c3 = rem._current_request_context()
        self.assertEqual(c3.task_id, c1.task_id)
        self.assertTrue(desktop_control.authorize_user_request(
            c3.instruction, "screen_agent", {"action": "press", "key": "enter"},
            source="voice", auth_context=c3,
        )[0])

        # Structured [Auth] DENY diagnostics on page-injected payload
        buf = io.StringIO()
        with redirect_stdout(buf):
            ok_inj, _ = desktop_control.authorize_user_request(
                c2.instruction, "browser_control",
                {"action": "smart_type", "description": "search bar", "text": "wire transfer"},
                source="voice", auth_context=c2,
            )
        self.assertFalse(ok_inj)
        log_out = buf.getvalue()
        self.assertIn("[Auth] DENY", log_out)
        self.assertIn("expected_task=", log_out)
        self.assertIn("received_task=", log_out)
        self.assertIn("source=voice", log_out)
        self.assertIn("reason=", log_out)

        # Unrelated command drops browser task continuity and creates a fresh task_id
        rem._record_user_request("Turn the volume down", "voice")
        c4 = rem._current_request_context()
        self.assertEqual(c4.browser_task_id, "")
        self.assertNotEqual(c4.task_id, c1.task_id)

    def test_youtube_cv2_masthead_search_fallback_and_unreadable_honesty(self):
        import numpy as np
        from PIL import Image
        from actions import browser_control
        from core import screenact, screenview

        # Construct a synthetic 1920x1080 screenshot at 125% DPI (2400x1350 physical px)
        # containing a centered rounded-rectangle search pill at logical y=105..145, x=560..1160.
        px = 1.25
        arr = np.full((1350, 2400, 3), 18, dtype=np.uint8)
        y0, y1 = int(105 * px), int(145 * px)
        x0, x1 = int(560 * px), int(1160 * px)
        arr[y0:y1, x0:x1] = 42
        arr[y0:y0 + 2, x0:x1] = 160
        arr[y1 - 2:y1, x0:x1] = 160
        arr[y0:y1, x0:x0 + 2] = 160
        arr[y0:y1, x1 - 2:x1] = 160
        img = Image.fromarray(arr)
        mon = {"left": 0, "top": 0, "width": 1920, "height": 1080}

        with patch.object(screenview, "capture", return_value=(img, mon, px)):
            box = browser_control._detect_youtube_masthead_search_box(monitor=mon)
        self.assertIsNotNone(box)
        self.assertEqual(box["source"], "visual_masthead_cv2")
        self.assertTrue(560 <= box["x"] <= 1160)
        self.assertTrue(105 <= box["y"] <= 145)

        # Verify smart_type clicks visually located bar and types once cleanly,
        # while unreadable YouTube results never fabricate video titles.
        empty_view = SimpleNamespace(
            app="chrome.exe", title="YouTube - Google Chrome", source="ocr",
            degraded=True, reason="no text", monitor=mon, elements=[], windows=[],
        )
        moved, clicked, typed = [], [], []
        with patch.object(screenview, "_foreground", return_value=("chrome.exe", "YouTube - Google Chrome", 101, 202)), \
             patch.object(screenview, "all_windows", return_value=[("chrome.exe", "YouTube - Google Chrome")]), \
             patch.object(screenview, "look", return_value=empty_view), \
             patch.object(screenview, "capture", return_value=(img, mon, px)), \
             patch.object(screenact, "move_cursor", side_effect=lambda pt, **kw: (moved.append((pt.x, pt.y)), screenact.Act(True, "move", point=(pt.x, pt.y)))[1]), \
             patch.object(screenact, "click", side_effect=lambda pt, **kw: (clicked.append((pt.x, pt.y)), screenact.Act(True, "click", point=(pt.x, pt.y)))[1]), \
             patch.object(screenact, "press", return_value=screenact.Act(True, "press")), \
             patch.object(screenact, "type_text", side_effect=lambda text, **kw: (typed.append(text), screenact.Act(True, "type"))[1]):
            res_type = browser_control._native_browser_action(
                "smart_type", {"description": "YouTube search bar", "text": "short"}, "chrome",
            )
            self.assertIn("status=done", res_type)
            self.assertEqual(typed, ["short"])
            self.assertEqual(len(clicked), 1)
            self.assertEqual(len(moved), 1)

            res_inspect = browser_control._native_browser_action("inspect", {}, "chrome")
            self.assertIn("status=unverified", res_inspect)
            self.assertIn("confidence=uncertain", res_inspect)
            self.assertIn("Tarun-kun, I can see YouTube, but I can't reliably read the video titles right now.", res_inspect)

            res_play = browser_control._native_browser_action("play_video", {"description": "second video"}, "chrome")
            self.assertIn("status=unverified", res_play)
            self.assertIn("Tarun-kun, I can see YouTube, but I can't reliably read the video titles right now.", res_play)


class FinalTargetResolutionAndAuthTests(unittest.TestCase):
    def setUp(self):
        from core import desktop_control
        desktop_control.clear_task_targets()

    def tearDown(self):
        from core import desktop_control
        desktop_control.clear_task_targets()

    def _make_rem(self):
        from main import RemLive
        rem = RemLive.__new__(RemLive)
        rem._request_context_lock = threading.Lock()
        rem._dispatch_context_local = None
        rem._request_context = None
        rem._last_user_command = ""
        rem._last_user_command_source = ""
        rem._active_browser_task = None
        rem._last_assistant_question = ""
        rem._current_voice_turn_task_id = ""
        return rem

    def test_video_identified_then_yes_click_it_and_unrelated_target_rejected(self):
        from actions import browser_control
        from core import desktop_control, screenact, screenview

        rem = self._make_rem()
        mon = {"left": 0, "top": 0, "width": 1920, "height": 1080}
        v1 = screenview.Element(eid="v1", role="text", name="First Coding Video Title", value="", left=260, top=240, width=420, height=36)
        v2 = screenview.Element(eid="v2", role="text", name="Python Async Deep Dive", value="", left=260, top=420, width=420, height=36)
        view_ocr = SimpleNamespace(
            app="chrome.exe", title="python - YouTube - Google Chrome",
            source="ocr", elements=[v1, v2], monitor=mon, degraded=True, reason="OCR", windows=[],
        )
        view_watch = SimpleNamespace(
            app="chrome.exe", title="Python Async Deep Dive - YouTube - Google Chrome",
            source="uia", elements=[], monitor=mon, degraded=False, reason="", windows=[],
        )

        # Turn 1: User asks for title of the second video; inspect grounds and registers v2 as active target
        rem._record_user_request("Tell me the title of the second video in Chrome.", "voice")
        c1 = rem._current_request_context()
        moves, clicks = [], []
        with patch.object(screenview, "_foreground", side_effect=[
                 ("chrome.exe", "python - YouTube - Google Chrome", 101, 202),
                 ("chrome.exe", "python - YouTube - Google Chrome", 101, 202),
                 ("chrome.exe", "python - YouTube - Google Chrome", 101, 202),
                 ("chrome.exe", "python - YouTube - Google Chrome", 101, 202),
                 ("chrome.exe", "Python Async Deep Dive - YouTube - Google Chrome", 101, 202),
             ]), \
             patch.object(screenview, "all_windows", return_value=[("chrome.exe", "python - YouTube - Google Chrome")]), \
             patch.object(screenview, "look", side_effect=[view_ocr, view_ocr, view_watch]), \
             patch.object(screenact, "move_cursor", side_effect=lambda pt: (moves.append((pt.x, pt.y)), screenact.Act(True, "move", point=(pt.x, pt.y)))[1]), \
             patch.object(screenact, "click", side_effect=lambda pt: (clicks.append((pt.x, pt.y)), screenact.Act(True, "click", point=(pt.x, pt.y)))[1]):
            res_inspect = rem._run_with_request_context(
                c1, lambda: browser_control._native_browser_action("inspect", {}, "chrome")
            )
            self.assertIn("status=done", res_inspect)
            self.assertIn("Python Async Deep Dive", res_inspect)

            # Turn 2: "Yes, click it." resolves "it" AND exact identified title under same task
            rem._record_user_request("Yes, click it.", "voice")
            c2 = rem._current_request_context()
            self.assertEqual(c2.task_id, c1.task_id)
            self.assertEqual(c2.browser_task_id, c1.browser_task_id)

            ok_it, why_it = desktop_control.authorize_user_request(
                c2.instruction, "browser_control",
                {"action": "smart_click", "description": "Python Async Deep Dive", "browser": "chrome"},
                source="voice", auth_context=c2,
            )
            self.assertTrue(ok_it, why_it)

            # Reject unrelated target through pronoun turn
            ok_bad, _ = desktop_control.authorize_user_request(
                c2.instruction, "browser_control",
                {"action": "smart_click", "description": "Delete Account", "browser": "chrome"},
                source="voice", auth_context=c2,
            )
            self.assertFalse(ok_bad)

            res_click = rem._run_with_request_context(
                c2, lambda: browser_control._native_browser_action("smart_click", {"description": "it"}, "chrome")
            )
            self.assertIn("status=done", res_click)
            self.assertIn("verification_status=verified", res_click)
            self.assertIn("Python Async Deep Dive", res_click)
            self.assertEqual(clicks, [(470, 438)])

    def test_search_field_identified_then_click_that_and_short_payload_enter(self):
        from actions import browser_control
        from core import desktop_control, screenact, screenview

        rem = self._make_rem()
        mon = {"left": 0, "top": 0, "width": 1920, "height": 1080}
        sb_el = screenview.Element(eid="sb", role="text", name="Search", value="", left=600, top=112, width=220, height=34)
        view_yt = SimpleNamespace(
            app="chrome.exe", title="YouTube - Google Chrome",
            source="ocr", elements=[sb_el], monitor=mon, degraded=True, reason="OCR", windows=[],
        )
        rem._record_user_request("Find the YouTube search bar in Chrome.", "voice")
        c1 = rem._current_request_context()
        clicks, typed, pressed = [], [], []

        with patch.object(screenview, "_foreground", return_value=("chrome.exe", "YouTube - Google Chrome", 101, 202)), \
             patch.object(screenview, "all_windows", return_value=[("chrome.exe", "YouTube - Google Chrome")]), \
             patch.object(screenview, "look", return_value=view_yt), \
             patch.object(screenact, "move_cursor", side_effect=lambda pt: screenact.Act(True, "move", point=(pt.x, pt.y))), \
             patch.object(screenact, "click", side_effect=lambda pt: (clicks.append((pt.x, pt.y)), screenact.Act(True, "click", point=(pt.x, pt.y)))[1]), \
             patch.object(screenact, "type_text", side_effect=lambda txt, **kw: (typed.append(txt), screenact.Act(True, "type"))[1]), \
             patch.object(screenact, "press", side_effect=lambda k, **kw: (pressed.append(k), screenact.Act(True, "press"))[1]):
            pt = rem._run_with_request_context(
                c1, lambda: browser_control.locate_control_screen_point("YouTube search bar", "chrome.exe")
            )
            self.assertIsNotNone(pt)

            # Follow-up "click that."
            rem._record_user_request("Click that.", "voice")
            c2 = rem._current_request_context()
            self.assertEqual(c2.task_id, c1.task_id)
            ok_clk, why_clk = desktop_control.authorize_user_request(
                c2.instruction, "browser_control",
                {"action": "smart_click", "description": "that", "browser": "chrome"},
                source="voice", auth_context=c2,
            )
            self.assertTrue(ok_clk, why_clk)
            res_clk = rem._run_with_request_context(
                c2, lambda: browser_control._native_browser_action("smart_click", {"description": "that"}, "chrome")
            )
            self.assertIn("status=done", res_clk)
            self.assertTrue(clicks)

            # Clarification -> one-word typing payload "short" -> "Type that" -> "Press Enter."
            rem._last_assistant_question = "What would you like me to type?"
            rem._record_user_request("short", "voice")
            c3 = rem._current_request_context()
            self.assertEqual(c3.task_id, c1.task_id)
            ok_type, why_type = desktop_control.authorize_user_request(
                c3.instruction, "browser_control",
                {"action": "smart_type", "description": "YouTube search bar", "text": "short", "browser": "chrome"},
                source="voice", auth_context=c3,
            )
            self.assertTrue(ok_type, why_type)
            res_type = rem._run_with_request_context(
                c3, lambda: browser_control._native_browser_action("smart_type", {"description": "YouTube search bar", "text": "short"}, "chrome")
            )
            self.assertIn("status=done", res_type)
            self.assertEqual(typed, ["short"])

            # Inherited typing payload via "Type that" and subsequent "Press Enter."
            rem._record_user_request("Type that.", "voice")
            c3b = rem._current_request_context()
            self.assertTrue(desktop_control.authorize_user_request(
                c3b.instruction, "browser_control",
                {"action": "smart_type", "description": "YouTube search bar", "text": "short", "browser": "chrome"},
                source="voice", auth_context=c3b,
            )[0])

            rem._record_user_request("Press Enter.", "voice")
            c4 = rem._current_request_context()
            self.assertEqual(c4.task_id, c1.task_id)
            self.assertTrue(desktop_control.authorize_user_request(
                c4.instruction, "screen_agent", {"action": "press", "key": "enter"},
                source="voice", auth_context=c4,
            )[0])
            res_enter = rem._run_with_request_context(
                c4, lambda: browser_control._native_browser_action("press", {"key": "enter"}, "chrome")
            )
            self.assertIn("status=done", res_enter)
            self.assertEqual(pressed[-1], "enter")

    def test_ocr_youtube_filtering_nested_tools_hindi_chatter_unverified_click_and_revocation(self):
        from actions import browser_control
        from core import conversation, desktop_control, screenact, screenview
        from memory import config_manager

        mon = {"left": 0, "top": 0, "width": 1920, "height": 1080}
        noise_els = [
            screenview.Element(eid="n1", role="text", name="All", value="", left=260, top=145, width=100, height=28),
            screenview.Element(eid="n2", role="text", name="1.2M views · 3 days ago", value="", left=260, top=290, width=220, height=22),
            screenview.Element(eid="n3", role="text", name="@CodeChannel", value="", left=260, top=320, width=140, height=22),
            screenview.Element(eid="n4", role="text", name="12:34", value="", left=260, top=350, width=60, height=20),
            screenview.Element(eid="v1", role="text", name="Real First Result Video Title", value="", left=260, top=220, width=420, height=34),
            screenview.Element(eid="v2", role="text", name="Real Second Result Video Title", value="", left=260, top=440, width=420, height=34),
        ]
        ocr_view = SimpleNamespace(
            app="chrome.exe", title="short - YouTube - Google Chrome",
            source="ocr", elements=noise_els, monitor=mon, degraded=True, reason="OCR", windows=[],
        )
        videos, _ = browser_control._extract_native_youtube_candidates(ocr_view)
        self.assertEqual([v["title"] for v in videos], [
            "Real First Result Video Title",
            "Real Second Result Video Title",
        ])

        rem = self._make_rem()
        rem._record_user_request("Click the second video in Chrome.", "voice")
        c_en = rem._current_request_context()
        # Unrelated Hindi remark must neither run browser actions nor wipe active browser task
        rem._record_user_request("हाँ ठीक है अच्छा लग रहा है", "voice")
        c_hi = rem._current_request_context()
        self.assertEqual(c_hi.browser_task_id, "")
        self.assertIsNotNone(rem._active_browser_task)

        # Nested screen_agent -> browser_control continuity
        self.assertTrue(desktop_control.authorize_user_request(
            c_en.instruction, "screen_agent",
            {"action": "tool", "tool_name": "browser_control", "tool_parameters": {"action": "smart_click", "description": "second video", "browser": "chrome"}},
            source="voice", auth_context=c_en,
        )[0])

        # Unverified click when title does NOT change after physical click
        with patch.object(screenview, "_foreground", return_value=("chrome.exe", "short - YouTube - Google Chrome", 101, 202)), \
             patch.object(screenview, "all_windows", return_value=[("chrome.exe", "short - YouTube - Google Chrome")]), \
             patch.object(screenview, "look", return_value=ocr_view), \
             patch.object(screenact, "move_cursor", side_effect=lambda pt: screenact.Act(True, "move", point=(pt.x, pt.y))), \
             patch.object(screenact, "click", side_effect=lambda pt: screenact.Act(True, "click", point=(pt.x, pt.y))):
            unv = rem._run_with_request_context(
                c_en, lambda: browser_control._native_browser_action("smart_click", {"description": "second video"}, "chrome")
            )
        self.assertIn("status=unverified", unv)
        self.assertIn("execution_status=completed", unv)
        self.assertIn("verification_status=unverified", unv)
        self.assertIn("I couldn't confirm", unv)
        self.assertIn("could not confirm", conversation.tool_result_guidance(unv).casefold())
        self.assertIn("I clicked the video", conversation.tool_result_guidance("status=done action=smart_click verified=true"))

        # Explicit screen revocation still blocks observation
        with patch.object(config_manager, "get_local_screen_observation_enabled", return_value=False), \
             patch.object(config_manager, "is_screen_access_revoked", return_value=True):
            ok_rev, rev_msg = desktop_control.authorize_user_request(
                "Check my screen", "screen_process", {"angle": "screen"}, source="voice",
            )
        self.assertFalse(ok_rev)
        self.assertIn("revoked", rev_msg.casefold())

    def test_trusted_local_autonomy_persistence_and_unified_action_classification(self):
        import tempfile
        from core import desktop_control
        from core.request_context import RequestContext
        from memory import config_manager

        # Persistence across reload using isolated temp config file
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_dir = Path(tmpdir)
            tmp_cfg = tmp_dir / "api_keys.json"
            tmp_cfg.write_text("{}", encoding="utf-8")
            with patch.object(config_manager, "CONFIG_DIR", tmp_dir), \
                 patch.object(config_manager, "CONFIG_FILE", tmp_cfg):
                config_manager.save_local_autonomy_mode("standard")
                self.assertEqual(config_manager.get_local_autonomy_mode(), "standard")
                config_manager.save_local_autonomy_mode("trusted")
                self.assertEqual(config_manager.get_local_autonomy_mode(), "trusted")

        # Unified semantic decisions across tools
        with patch.object(config_manager, "get_local_screen_observation_enabled", return_value=True), \
             patch.object(config_manager, "is_screen_access_revoked", return_value=False), \
             patch.object(config_manager, "get_local_autonomy_mode", return_value="trusted"):
            ctx = RequestContext.create(
                "Tell me the second video's title.", "voice", browser="chrome", browser_task_id="bt_uni"
            )
            # screen_process(screen) and desktop_ui(observe) agree 100%
            d1 = desktop_control.evaluate_authorization(ctx.instruction, "screen_process", {"angle": "screen"}, source="voice", auth_context=ctx)
            d2 = desktop_control.evaluate_authorization(ctx.instruction, "desktop_ui", {"action": "observe"}, source="voice", auth_context=ctx)
            self.assertTrue(d1["allowed"])
            self.assertEqual(d1["allowed"], d2["allowed"])
            self.assertEqual(d1["action_category"], "screen.observe")
            self.assertEqual(d2["action_category"], "screen.observe")
            self.assertEqual(d1["authorization_profile"], "trusted")
            self.assertEqual(d1["browser_task_id"], "bt_uni")
            self.assertEqual(d1["revocation_status"], "GRANTED")

            # Chit-chat still denied by BOTH
            self.assertFalse(desktop_control.authorize_user_request("Hello", "screen_process", {"angle": "screen"}, source="voice")[0])
            self.assertFalse(desktop_control.authorize_user_request("Hello", "desktop_ui", {"action": "observe"}, source="voice")[0])

            # Seek forward / focus search / Hinglish all unified cleanly across browser_control, computer_settings, video_player
            self.assertTrue(desktop_control.authorize_user_request("Forward 10 seconds.", "computer_settings", {"action": "seek_forward", "value": "10"}, source="voice", auth_context=ctx)[0])
            self.assertTrue(desktop_control.authorize_user_request("Ten seconds forward.", "browser_control", {"action": "seek_forward", "seconds": 10}, source="voice", auth_context=ctx)[0])
            self.assertTrue(desktop_control.authorize_user_request("Ab search karo.", "computer_settings", {"action": "focus_search"}, source="voice", auth_context=ctx)[0])
            self.assertTrue(desktop_control.authorize_user_request("Ab search karo.", "browser_control", {"action": "focus_search"}, source="voice", auth_context=ctx)[0])
            self.assertTrue(desktop_control.authorize_user_request("Video resume karo.", "browser_control", {"action": "resume_video"}, source="voice", auth_context=ctx)[0])

            # Sensitive/message safeguard stays denied
            self.assertFalse(desktop_control.authorize_user_request("Forward 10 seconds.", "send_message", {"receiver": "Alex", "message_text": "hi"}, source="voice", auth_context=ctx)[0])

    def test_foreground_window_resolution_self_guard_and_youtube_seek_execution(self):
        from actions import browser_control
        from core import appidentity, desktop_control, screenact, screenview

        # Verify _ensure_browser_foreground refuses input cleanly when Windows refuses foreground
        with patch.object(screenact, "focus_app", return_value=screenact.Act(False, "focus_app", error="refused")), \
             patch.object(browser_control, "_find_browser_window", return_value={"found": True, "foreground": False, "browser": "chrome", "app": "chrome.exe", "title": "YouTube - Google Chrome", "pid": 444}), \
             patch.object(appidentity, "focus_window_by_app", return_value={"activated": False, "reason": "foreground_lock"}):
            res_fg_fail = browser_control._native_browser_action("focus_search", {}, "chrome")
        self.assertIn("status=failed", res_fg_fail)
        self.assertIn("did not become the foreground window", res_fg_fail)

        # Verify CDP seek_video changes currentTime and returns verified before/after seconds
        sess = browser_control._BrowserSession("chrome")
        from tests import yt_fakes as _yf
        fake_page = _yf.FakeYouTubePage(url="https://www.youtube.com/watch?v=abc", current=12.0)
        sess._get_page = AsyncMock(return_value=fake_page)
        res_cdp_seek = asyncio.run(sess.seek_video(10.0))
        self.assertIn("status=done", res_cdp_seek)
        self.assertIn("playback_before=12.0", res_cdp_seek)
        self.assertIn("playback_after=22.0", res_cdp_seek)

        # Verify native YouTube seek sends 'l' and records before/after timestamp when UIA/OCR exposes clock
        before_view = SimpleNamespace(
            app="chrome.exe", title="Watch - YouTube - Google Chrome", source="uia",
            elements=[screenview.Element(eid="t1", role="text", name="0:15 / 5:00", value="", left=100, top=600, width=80, height=20)],
            monitor={"left": 0, "top": 0, "width": 1920, "height": 1080}, windows=[],
        )
        after_view = SimpleNamespace(
            app="chrome.exe", title="Watch - YouTube - Google Chrome", source="uia",
            elements=[screenview.Element(eid="t1", role="text", name="0:25 / 5:00", value="", left=100, top=600, width=80, height=20)],
            monitor={"left": 0, "top": 0, "width": 1920, "height": 1080}, windows=[],
        )
        pressed = []
        with patch.object(screenview, "_foreground", return_value=("chrome.exe", "Watch - YouTube - Google Chrome", 444, 999)), \
             patch.object(screenview, "all_windows", return_value=[("chrome.exe", "Watch - YouTube - Google Chrome")]), \
             patch.object(screenview, "is_own_pid", return_value=False), \
             patch.object(screenview, "look", side_effect=[before_view, after_view]), \
             patch.object(screenact, "press", side_effect=lambda k, **kw: (pressed.append(k), screenact.Act(True, "press"))[1]):
            res_native_seek = browser_control._native_browser_action("seek_forward", {"seconds": 10}, "chrome")
        self.assertEqual(pressed, ["l"])
        self.assertIn("status=done", res_native_seek)
        self.assertIn("playback_before=15.0", res_native_seek)
        self.assertIn("playback_after=25.0", res_native_seek)
        self.assertIn("Skipped forward 10 seconds", res_native_seek)
        self.assertIn("seek_forward +10s", desktop_control.snapshot().get("last_media_status", ""))


class WhatsAppCompositionAndLiveStabilityTests(unittest.TestCase):
    def test_whatsapp_contact_deduplication_and_chat_selection_verification(self):
        from actions import browser_control
        from core import desktop_control, screenact, screenview
        from plugins import _whatsapp_core

        # Sub-elements belonging to the same visual contact card for "Rio" must collapse into 1 unique contact
        view_single = SimpleNamespace(
            app="chrome.exe",
            title="WhatsApp - Google Chrome",
            source="uia+ocr",
            monitor={"left": 0, "top": 0, "width": 1920, "height": 1080},
            windows=[],
            elements=[
                screenview.Element(eid="sb", role="edit", name="Search or start new chat", value="Rio", left=120, top=145, width=280, height=34),
                screenview.Element(eid="c1", role="listitem", name="Rio 5:10 PM Hey, how was your day?", value="", left=60, top=225, width=320, height=58),
                screenview.Element(eid="c2", role="text", name="Rio", value="", left=110, top=232, width=90, height=22),
                screenview.Element(eid="c3", role="ocr_text", name="Rio", value="", left=112, top=234, width=88, height=20),
                screenview.Element(eid="hdr", role="text", name="Rio", value="", left=560, top=105, width=100, height=24),
                screenview.Element(eid="comp", role="edit", name="Type a message", value="", left=560, top=940, width=700, height=42),
            ],
        )
        single_contacts = _whatsapp_core.extract_whatsapp_contacts(view_single, query="Rio")
        self.assertEqual(len(single_contacts), 1, single_contacts)
        self.assertEqual(single_contacts[0]["display_name"], "Rio")

        # Two visually distinct contacts >40px apart must remain 2 distinct contacts
        view_two = SimpleNamespace(
            app="chrome.exe",
            title="WhatsApp - Google Chrome",
            source="uia",
            monitor={"left": 0, "top": 0, "width": 1920, "height": 1080},
            windows=[],
            elements=[
                screenview.Element(eid="c1", role="text", name="Rio", value="", left=110, top=230, width=90, height=22),
                screenview.Element(eid="c2", role="text", name="Rio Work", value="", left=110, top=320, width=130, height=22),
            ],
        )
        two_contacts = _whatsapp_core.extract_whatsapp_contacts(view_two, query="Rio")
        self.assertEqual(len(two_contacts), 2, two_contacts)

        # Selecting unique Rio updates active_contact, unique_contact_count, selected_chat_verified & click diagnostics
        with patch.object(screenview, "_foreground", return_value=("chrome.exe", "WhatsApp - Google Chrome", 512, 8888)), \
             patch.object(screenview, "all_windows", return_value=[("chrome.exe", "WhatsApp - Google Chrome")]), \
             patch.object(screenview, "is_own_pid", return_value=False), \
             patch.object(screenview, "look", return_value=view_single), \
             patch.object(screenact, "get_cursor_pos", side_effect=[(20, 20), (155, 243)]), \
             patch.object(screenact, "move_cursor", side_effect=lambda pt: screenact.Act(True, "move", point=(pt.x, pt.y))), \
             patch.object(screenact, "click", side_effect=lambda pt: screenact.Act(True, "click", point=(pt.x, pt.y))):
            res = browser_control._native_browser_action("select_contact", {"description": "Rio"}, "chrome")
        self.assertIn("status=done", res)
        self.assertIn("active_contact=Rio", res)
        self.assertIn("unique_contact_count=1", res)
        self.assertIn("selected_chat_verified=true", res)
        self.assertIn("target_hwnd=8888", res)
        self.assertIn("target_pid=512", res)
        self.assertIn("cursor_before=20,20", res)
        self.assertIn("cursor_after=155,243", res)
        self.assertIn("dispatch_status=completed", res)
        self.assertIn("input_status=completed", res)
        snap = desktop_control.snapshot()
        self.assertEqual(snap["active_contact"], "Rio")
        self.assertEqual(snap["unique_contact_count"], 1)
        self.assertTrue(snap["selected_chat_verified"])

    def test_whatsapp_autonomous_composition_iterative_edit_and_send_gate(self):
        from actions import browser_control
        from core import desktop_control, screenact, screenview
        from core.request_context import RequestContext
        from memory import config_manager

        desktop_control.set_active_target(
            target_type="contact", exact_target_name="Rio", browser="chrome", tab_title="WhatsApp",
        )
        desktop_control.record_diagnostics(active_contact="Rio", selected_chat_verified=True)

        draft_long = (
            "Hey Rio!\nHope your day is going well.\nI wrapped up some coding work today.\n"
            "Taking a quick breather now.\nLet's catch up soon!"
        )
        draft_short = "Hey Rio! Had a busy coding day—hope yours is going great. Catch up soon!"

        ctx = RequestContext.create(
            "Write a friendly message about how your day was, 5 to 10 lines",
            "voice", browser="chrome", browser_task_id="bt_wa_comp",
        )
        with patch.object(config_manager, "get_local_autonomy_mode", return_value="trusted"), \
             patch.object(config_manager, "get_local_screen_observation_enabled", return_value=True), \
             patch.object(config_manager, "is_screen_access_revoked", return_value=False):
            ev = desktop_control.evaluate_authorization(
                ctx.instruction, "browser_control",
                {"action": "smart_type", "description": "message box", "text": draft_long},
                source="voice", auth_context=ctx,
            )
            self.assertTrue(ev["allowed"], ev["reason"])
            comp = desktop_control.get_active_composition_state(ctx)
            self.assertTrue(comp["composition_requested"])
            self.assertTrue(comp["typing_authorized"])
            self.assertFalse(comp["message_sent"])

            # Iterative follow-up edit: "Make it shorter" uses clear_first=True and stays unsent
            ctx_short = RequestContext.create(
                "Make it shorter", "voice",
                parent_instruction=ctx.instruction,
                root_task_id=ctx.root_task_id,
                spoken_inputs=(ctx.instruction, "Make it shorter"),
                browser=ctx.browser,
                browser_task_id=ctx.browser_task_id,
            )
            ev_short = desktop_control.evaluate_authorization(
                ctx_short.instruction, "browser_control",
                {"action": "smart_type", "description": "message box", "text": draft_short},
                source="voice", auth_context=ctx_short,
            )
            self.assertTrue(ev_short["allowed"], ev_short["reason"])

            wa_view = SimpleNamespace(
                app="chrome.exe", title="WhatsApp - Google Chrome", source="uia",
                monitor={"left": 0, "top": 0, "width": 1920, "height": 1080}, windows=[],
                elements=[
                    screenview.Element(eid="hdr", role="text", name="Rio", value="", left=560, top=105, width=100, height=24),
                    screenview.Element(eid="comp", role="edit", name="Type a message", value="", left=560, top=940, width=700, height=42),
                ],
            )
            wa_after = SimpleNamespace(
                app="chrome.exe", title="WhatsApp - Google Chrome", source="uia",
                monitor={"left": 0, "top": 0, "width": 1920, "height": 1080}, windows=[],
                elements=[
                    screenview.Element(eid="hdr", role="text", name="Rio", value="", left=560, top=105, width=100, height=24),
                    screenview.Element(eid="comp", role="edit", name="Type a message", value=draft_short, left=560, top=940, width=700, height=42),
                ],
            )
            clear_flags = []
            with patch.object(screenview, "_foreground", return_value=("chrome.exe", "WhatsApp - Google Chrome", 512, 8888)), \
                 patch.object(screenview, "all_windows", return_value=[("chrome.exe", "WhatsApp - Google Chrome")]), \
                 patch.object(screenview, "is_own_pid", return_value=False), \
                 patch.object(screenview, "look", side_effect=[wa_view, wa_after]), \
                 patch.object(screenact, "get_cursor_pos", side_effect=[(50, 50), (910, 961)]), \
                 patch.object(screenact, "move_cursor", side_effect=lambda pt: screenact.Act(True, "move", point=(pt.x, pt.y))), \
                 patch.object(screenact, "click", side_effect=lambda pt: screenact.Act(True, "click", point=(pt.x, pt.y))), \
                 patch.object(screenact, "type_text", side_effect=lambda txt, clear_first=False: (clear_flags.append(clear_first), screenact.Act(True, "type"))[1]):
                typed_res = browser_control._native_browser_action(
                    "smart_type", {"description": "message box", "text": draft_short}, "chrome"
                )
            self.assertEqual(clear_flags, [True])
            self.assertIn("status=done", typed_res)
            self.assertIn("message_sent=false", typed_res)
            self.assertEqual(desktop_control.snapshot()["sending_status"], "not_sent")

            # Unrequested AI payload outside composition flow MUST stay denied (payload_mismatch)
            unreq_ctx = RequestContext.create("Type hello into the message box", "voice", browser="chrome")
            ok_unreq, msg_unreq = desktop_control.authorize_user_request(
                unreq_ctx.instruction, "browser_control",
                {"action": "smart_type", "description": "message box", "text": "Transfer funds now"},
                source="voice", auth_context=unreq_ctx,
            )
            self.assertFalse(ok_unreq)
            self.assertIn("not provided in your request", msg_unreq)

            # Sensitive financial terms injected during composition stay denied
            ok_fin, _ = desktop_control.authorize_user_request(
                ctx.instruction, "browser_control",
                {"action": "smart_type", "description": "message box", "text": "Please wire bitcoin password"},
                source="voice", auth_context=ctx,
            )
            self.assertFalse(ok_fin)

            # send_message denies implicit look requests, allows explicit "Send it" for verified contact
            ok_no_send, _ = desktop_control.authorize_user_request(
                "Look at Rio", "send_message", {"receiver": "Rio", "message_text": draft_short},
                source="voice", auth_context=ctx,
            )
            self.assertFalse(ok_no_send)
            ctx_send = RequestContext.create(
                "Send it", "voice",
                parent_instruction=ctx.instruction,
                root_task_id=ctx.root_task_id,
                spoken_inputs=(ctx.instruction, "Make it shorter", "Send it"),
                browser=ctx.browser,
                browser_task_id=ctx.browser_task_id,
            )
            ok_explicit_send, send_reason = desktop_control.authorize_user_request(
                ctx_send.instruction, "send_message", {"receiver": "Rio", "message_text": draft_short},
                source="voice", auth_context=ctx_send,
            )
            self.assertTrue(ok_explicit_send, send_reason)

    def test_screenshot_scroll_authorization_and_physical_click_diagnostics(self):
        from actions import browser_control
        from core import desktop_control, screenact, screenview
        from core.request_context import RequestContext
        from memory import config_manager

        ctx = RequestContext.create("Capture screen and scroll down", "voice", browser="chrome", browser_task_id="bt_scr")
        with patch.object(config_manager, "get_local_autonomy_mode", return_value="trusted"), \
             patch.object(config_manager, "get_local_screen_observation_enabled", return_value=True), \
             patch.object(config_manager, "is_screen_access_revoked", return_value=False):
            self.assertTrue(desktop_control.authorize_user_request("Take a screenshot", "browser_control", {"action": "screenshot"}, source="voice", auth_context=ctx)[0])
            self.assertTrue(desktop_control.authorize_user_request("Scroll down", "browser_control", {"action": "scroll", "direction": "down"}, source="voice", auth_context=ctx)[0])

        search_view = SimpleNamespace(
            app="chrome.exe", title="lofi - YouTube - Google Chrome", source="uia",
            monitor={"left": 0, "top": 0, "width": 1920, "height": 1080}, windows=[],
            elements=[
                screenview.Element(eid="v1", role="hyperlink", name="Lofi Chill Mix One", value="", left=240, top=220, width=420, height=40),
                screenview.Element(eid="v2", role="hyperlink", name="Second Lofi Video", value="", left=240, top=420, width=420, height=40),
            ],
        )
        watch_view = SimpleNamespace(
            app="chrome.exe", title="Second Lofi Video - YouTube - Google Chrome", source="uia",
            monitor={"left": 0, "top": 0, "width": 1920, "height": 1080}, windows=[], elements=[],
        )
        with patch.object(
            screenview, "_foreground",
            side_effect=[
                ("chrome.exe", "lofi - YouTube - Google Chrome", 777, 12345),
                ("chrome.exe", "lofi - YouTube - Google Chrome", 777, 12345),
                ("chrome.exe", "Second Lofi Video - YouTube - Google Chrome", 777, 12345),
            ],
        ), \
             patch.object(screenview, "all_windows", return_value=[("chrome.exe", "lofi - YouTube - Google Chrome")]), \
             patch.object(screenview, "is_own_pid", return_value=False), \
             patch.object(screenview, "look", side_effect=[search_view, watch_view]), \
             patch.object(screenact, "get_cursor_pos", side_effect=[(10, 10), (450, 440)]), \
             patch.object(screenact, "move_cursor", side_effect=lambda pt: screenact.Act(True, "move", point=(pt.x, pt.y))), \
             patch.object(screenact, "click", side_effect=lambda pt: screenact.Act(True, "click", point=(pt.x, pt.y))):
            out = browser_control._native_browser_action("smart_click", {"description": "second video"}, "chrome")
        self.assertIn("status=done", out)
        self.assertIn("target_hwnd=12345", out)
        self.assertIn("target_pid=777", out)
        self.assertIn("foreground_hwnd=12345", out)
        self.assertIn("cursor_before=10,10", out)
        self.assertIn("cursor_after=450,440", out)
        self.assertIn("dispatch_status=completed", out)
        self.assertIn("input_status=completed", out)
        self.assertIn("verification_status=verified", out)

    def test_gemini_live_error_classification_and_bounded_backoff(self):
        from main import _classify_live_error, _next_live_backoff

        c_1008 = _classify_live_error(RuntimeError("1008 None. Operation is not implemented key=AIzaSySecret123"))
        self.assertEqual(c_1008["category"], "inactivity_close_1008")
        self.assertNotIn("AIzaSySecret123", c_1008["reason"])
        self.assertTrue(c_1008["drop_resume"])

        c_res = _classify_live_error(RuntimeError("1007 session resumption handle expired"))
        self.assertEqual(c_res["category"], "resumption_rejected")
        self.assertTrue(c_res["drop_resume"])

        c_cancel = _classify_live_error(asyncio.CancelledError(), closing=True)
        self.assertEqual(c_cancel["category"], "shutdown_cancel")

        c_quota = _classify_live_error(RuntimeError("429 ResourceExhausted quota exceeded"))
        self.assertEqual(c_quota["category"], "quota_or_rate_limit")

        b1 = _next_live_backoff(1.5, attempt=1)
        b2 = _next_live_backoff(b1, attempt=2)
        b_cap = _next_live_backoff(40.0, attempt=6)
        self.assertGreaterEqual(b1, 1.5)
        self.assertGreater(b2, b1)
        self.assertLessEqual(b_cap, 46.0)


class VoiceFirstWhatsAppMessagingAndCallTests(unittest.TestCase):
    def test_placeholder_ids_rejected_and_ordinal_contact_selected(self):
        from actions import browser_control
        from core import desktop_control, screenact, screenview
        from core.request_context import RequestContext
        from plugins import _whatsapp_core

        noisy_view = SimpleNamespace(
            app="chrome.exe",
            title="WhatsApp - Google Chrome",
            source="uia+ocr",
            monitor={"left": 0, "top": 0, "width": 1920, "height": 1080},
            windows=[],
            elements=[
                screenview.Element(eid="p1", role="text", name="-0-", value="", left=100, top=180, width=50, height=20),
                screenview.Element(eid="p2", role="text", name="-0+", value="", left=100, top=205, width=50, height=20),
                screenview.Element(eid="p3", role="text", name="12:45 PM", value="", left=300, top=225, width=60, height=18),
                screenview.Element(eid="p4", role="text", name="3", value="", left=330, top=245, width=30, height=18),
                screenview.Element(eid="c1", role="listitem", name="Rio Alpha", value="", left=80, top=230, width=260, height=40),
                screenview.Element(eid="c2", role="listitem", name="Rio Beta", value="", left=80, top=320, width=260, height=40),
            ],
        )
        contacts = _whatsapp_core.extract_whatsapp_contacts(noisy_view, query="Rio")
        self.assertEqual([c["display_name"] for c in contacts], ["Rio Alpha", "Rio Beta"])
        for c in contacts:
            self.assertNotIn("-0-", str(c["id"]))
            self.assertNotIn("-0+", str(c["id"]))
            self.assertIsNone(c["contact_id"])

        ctx = RequestContext.create("Search for Rio", "voice", browser="chrome", browser_task_id="bt_rio_ord")
        tok = desktop_control.bind_auth_context(ctx)
        try:
            after_open = SimpleNamespace(
                app="chrome.exe", title="WhatsApp - Google Chrome", source="uia",
                monitor={"left": 0, "top": 0, "width": 1920, "height": 1080}, windows=[],
                elements=[
                    screenview.Element(eid="hdr", role="text", name="Rio Alpha", value="", left=580, top=105, width=150, height=24),
                    screenview.Element(eid="comp", role="edit", name="Type a message", value="", left=580, top=940, width=680, height=40),
                ],
            )
            with patch.object(screenview, "_foreground", return_value=("chrome.exe", "WhatsApp - Google Chrome", 512, 8888)), \
                 patch.object(screenview, "all_windows", return_value=[("chrome.exe", "WhatsApp - Google Chrome")]), \
                 patch.object(screenview, "is_own_pid", return_value=False), \
                 patch.object(screenview, "look", side_effect=[noisy_view, after_open]), \
                 patch.object(screenact, "get_cursor_pos", side_effect=[(10, 10), (210, 250)]), \
                 patch.object(screenact, "move_cursor", side_effect=lambda pt: screenact.Act(True, "move", point=(pt.x, pt.y))), \
                 patch.object(screenact, "click", side_effect=lambda pt: screenact.Act(True, "click", point=(pt.x, pt.y))):
                res = browser_control._native_browser_action("select_contact", {"description": "the first one"}, "chrome")
        finally:
            desktop_control.reset_auth_context(tok)
        self.assertIn("status=done", res)
        self.assertIn('active_contact="Rio Alpha"', res)
        self.assertIn("selected_chat_verified=true", res)

    def test_hindi_draft_translation_and_replacement_without_sending(self):
        from actions import browser_control
        from core import desktop_control, gemini, screenact, screenview
        from core.request_context import RequestContext

        ctx = RequestContext.create("Write a Hindi message to Rio asking how his day went", "voice", browser="chrome", browser_task_id="bt_hi_en")
        desktop_control.set_active_target(target_type="contact", exact_target_name="Rio", browser="chrome", tab_title="WhatsApp - Google Chrome", auth_context=ctx)
        desktop_control.record_diagnostics(active_contact="Rio", selected_chat_verified=True)

        hi_draft = "नमस्ते Rio! आज आपका दिन कैसा रहा?"
        en_draft = "Hey Rio! Hope your day went really well today!"
        wa_base = SimpleNamespace(
            app="chrome.exe", title="WhatsApp - Google Chrome", source="uia",
            monitor={"left": 0, "top": 0, "width": 1920, "height": 1080}, windows=[],
            elements=[
                screenview.Element(eid="hdr", role="text", name="Rio", value="", left=580, top=105, width=120, height=24),
                screenview.Element(eid="comp", role="edit", name="Type a message", value="", left=580, top=940, width=680, height=40),
            ],
        )
        wa_after_hi = SimpleNamespace(
            app="chrome.exe", title="WhatsApp - Google Chrome", source="uia",
            monitor={"left": 0, "top": 0, "width": 1920, "height": 1080}, windows=[],
            elements=[
                screenview.Element(eid="hdr", role="text", name="Rio", value="", left=580, top=105, width=120, height=24),
                screenview.Element(eid="comp", role="edit", name="Type a message", value=hi_draft, left=580, top=940, width=680, height=40),
            ],
        )
        wa_after_en = SimpleNamespace(
            app="chrome.exe", title="WhatsApp - Google Chrome", source="uia",
            monitor={"left": 0, "top": 0, "width": 1920, "height": 1080}, windows=[],
            elements=[
                screenview.Element(eid="hdr", role="text", name="Rio", value="", left=580, top=105, width=120, height=24),
                screenview.Element(eid="comp", role="edit", name="Type a message", value=en_draft, left=580, top=940, width=680, height=40),
            ],
        )
        typed_calls = []
        tok = desktop_control.bind_auth_context(ctx)
        try:
            with patch.object(gemini, "text", side_effect=[hi_draft, en_draft]), \
                 patch.object(screenview, "_foreground", return_value=("chrome.exe", "WhatsApp - Google Chrome", 512, 8888)), \
                 patch.object(screenview, "all_windows", return_value=[("chrome.exe", "WhatsApp - Google Chrome")]), \
                 patch.object(screenview, "is_own_pid", return_value=False), \
                 patch.object(screenview, "look", side_effect=[wa_base, wa_after_hi, wa_after_hi, wa_after_en]), \
                 patch.object(screenact, "move_cursor", side_effect=lambda pt: screenact.Act(True, "move", point=(pt.x, pt.y))), \
                 patch.object(screenact, "click", side_effect=lambda pt: screenact.Act(True, "click", point=(pt.x, pt.y))), \
                 patch.object(screenact, "type_text", side_effect=lambda txt, clear_first=False: (typed_calls.append((txt, clear_first)), screenact.Act(True, "type"))[1]):
                r1 = browser_control._native_browser_action("compose_message", {"receiver": "Rio"}, "chrome")
                ctx2 = RequestContext.create(
                    "Translate it into English and remove the Hindi message", "voice",
                    parent_instruction=ctx.instruction, root_task_id=ctx.root_task_id,
                    spoken_inputs=(ctx.instruction, "Translate it into English"),
                    browser="chrome", browser_task_id=ctx.browser_task_id,
                )
                tok2 = desktop_control.bind_auth_context(ctx2)
                try:
                    r2 = browser_control._native_browser_action("update_draft", {"receiver": "Rio"}, "chrome")
                finally:
                    desktop_control.reset_auth_context(tok2)
        finally:
            desktop_control.reset_auth_context(tok)
        self.assertEqual(typed_calls, [(hi_draft, True), (en_draft, True)])
        self.assertIn("message_sent=false", r1)
        self.assertIn("message_sent=false", r2)
        comp_state = desktop_control.get_active_composition_state(ctx)
        self.assertEqual(comp_state["generated_content"], en_draft)
        self.assertFalse(comp_state["message_sent"])

    def test_voice_only_send_message_skips_redundant_confirm_and_blocks_duplicate(self):
        from actions import send_message
        from core import confirm, desktop_control, screenact, screenview
        from core.request_context import RequestContext
        from memory import config_manager
        from plugins import _whatsapp_core

        with desktop_control._sent_attempts_lock:
            desktop_control._sent_attempt_records.clear()

        msg = "Hey Rio, unique idempotent test message!"
        ctx_send = RequestContext.create("Send it", "voice", browser="chrome", browser_task_id="bt_voice_send", parent_instruction="Write a message to Rio")
        desktop_control.record_diagnostics(active_contact="Rio", selected_chat_verified=True)
        desktop_control.set_active_composition_state(
            composition_requested=True, composition_task_id=ctx_send.task_id,
            selected_contact="Rio", generated_content=msg,
            typing_authorized=True, message_sent=False, auth_context=ctx_send,
        )
        open_view = SimpleNamespace(
            degraded=False, app="WhatsApp.exe", title="WhatsApp",
            monitor={"left": 0, "top": 0, "width": 1920, "height": 1080},
            elements=[
                screenview.Element(eid="hdr", role="text", name="Rio", value="", left=580, top=105, width=120, height=24),
                screenview.Element(eid="comp", role="edit", name="Type a message", value="", left=580, top=940, width=680, height=40),
            ],
        )
        staged_view = SimpleNamespace(
            degraded=False, app="WhatsApp.exe", title="WhatsApp",
            monitor={"left": 0, "top": 0, "width": 1920, "height": 1080},
            elements=[
                screenview.Element(eid="hdr", role="text", name="Rio", value="", left=580, top=105, width=120, height=24),
                screenview.Element(eid="comp", role="edit", name="Type a message", value=msg, left=580, top=940, width=680, height=40),
            ],
        )
        sent_view = SimpleNamespace(
            degraded=False, app="WhatsApp.exe", title="WhatsApp",
            monitor={"left": 0, "top": 0, "width": 1920, "height": 1080},
            elements=[
                screenview.Element(eid="hdr", role="text", name="Rio", value="", left=580, top=105, width=120, height=24),
                screenview.Element(eid="comp", role="edit", name="Type a message", value="", left=580, top=940, width=680, height=40),
                screenview.Element(eid="out", role="text", name=msg, value="", left=900, top=780, width=320, height=40),
            ],
        )
        tok = desktop_control.bind_auth_context(ctx_send)
        try:
            with patch.object(config_manager, "get_local_autonomy_mode", return_value="trusted"), \
                 patch.object(confirm, "request", side_effect=AssertionError("must not show redundant GUI confirmation")) as c_req, \
                 patch("core.appidentity.focus_window_by_app", return_value={"activated": True}), \
                 patch.object(_whatsapp_core.WhatsAppDriver, "_view", side_effect=[open_view, staged_view, sent_view, open_view]), \
                 patch.object(screenact, "type_text", return_value=screenact.Act(True, "type")), \
                 patch.object(screenact, "press", return_value=screenact.Act(True, "press")):
                first_res = send_message.send_message({"receiver": "Rio", "message_text": msg, "platform": "whatsapp"})
                dup_res = send_message.send_message({"receiver": "Rio", "message_text": msg, "platform": "whatsapp"})
        finally:
            desktop_control.reset_auth_context(tok)
        self.assertIn("status=done", first_res)
        self.assertIn("verified=true", first_res)
        self.assertIn("duplicate send blocked", dup_res)
        self.assertEqual(desktop_control.snapshot()["gui_confirm_required"], False)

        # Uncontextualized send_message call (no bound voice context) MUST still request GUI confirm fail-closed
        with patch.object(confirm, "pending_title", return_value=""), \
             patch.object(confirm, "request", return_value="confirmation pending") as c_req2:
            fallback_res = send_message.send_message({"receiver": "Rio", "message_text": msg, "platform": "whatsapp"})
        self.assertEqual(fallback_res, "confirmation pending")
        c_req2.assert_called_once()

    def test_whatsapp_voice_call_direct_voice_execution_and_button_safety(self):
        from core import confirm, desktop_control, screenact, screenview
        from core.request_context import RequestContext
        from memory import config_manager
        from plugins import whatsapp_call

        # Ensure video call button is NEVER clicked for a voice call request
        video_only_view = SimpleNamespace(
            degraded=False, app="WhatsApp.exe", title="WhatsApp",
            monitor={"left": 0, "top": 0, "width": 1920, "height": 1080},
            elements=[
                screenview.Element(eid="hdr", role="text", name="Rio", value="", left=580, top=105, width=120, height=24),
                screenview.Element(eid="vbtn", role="button", name="Video call", value="", left=1720, top=100, width=40, height=32),
                screenview.Element(eid="comp", role="edit", name="Type a message", value="", left=580, top=940, width=680, height=40),
            ],
        )
        both_btns_view = SimpleNamespace(
            degraded=False, app="WhatsApp.exe", title="WhatsApp",
            monitor={"left": 0, "top": 0, "width": 1920, "height": 1080},
            elements=[
                screenview.Element(eid="hdr", role="text", name="Rio", value="", left=580, top=105, width=120, height=24),
                screenview.Element(eid="vbtn", role="button", name="Video call", value="", left=1720, top=100, width=40, height=32),
                screenview.Element(eid="abtn", role="button", name="Voice call", value="", left=1780, top=100, width=40, height=32),
                screenview.Element(eid="comp", role="edit", name="Type a message", value="", left=580, top=940, width=680, height=40),
            ],
        )
        calling_active_view = SimpleNamespace(
            degraded=False, app="WhatsApp.exe", title="WhatsApp",
            monitor={"left": 0, "top": 0, "width": 1920, "height": 1080},
            elements=[
                screenview.Element(eid="st", role="text", name="Rio Calling... Voice call", value="", left=880, top=320, width=220, height=36),
                screenview.Element(eid="end", role="button", name="End call", value="", left=920, top=600, width=60, height=60),
            ],
        )

        ctx_call = RequestContext.create("REM, call Rio.", "voice", browser="chrome", browser_task_id="bt_call_rio")
        tok = desktop_control.bind_auth_context(ctx_call)
        try:
            with patch.object(config_manager, "get_local_autonomy_mode", return_value="trusted"), \
                 patch.object(confirm, "request", side_effect=AssertionError("voice call must not ask redundant confirm")), \
                 patch("core.appidentity.focus_window_by_app", return_value={"activated": True}), \
                 patch.object(screenview, "look", return_value=video_only_view):
                no_voice_btn_res = whatsapp_call.run({"action": "request_call", "contact": "Rio", "video": False})
            self.assertIn("could not find the voice call button", no_voice_btn_res)

            clicked_pts = []
            with patch.object(config_manager, "get_local_autonomy_mode", return_value="trusted"), \
                 patch.object(confirm, "request", side_effect=AssertionError("voice call must not ask redundant confirm")), \
                 patch("core.appidentity.focus_window_by_app", return_value={"activated": True}), \
                 patch.object(screenview, "look", side_effect=[both_btns_view, calling_active_view]), \
                 patch.object(screenact, "move_cursor", side_effect=lambda pt: screenact.Act(True, "move", point=(pt.x, pt.y))), \
                 patch.object(screenact, "click", side_effect=lambda pt, **kw: (clicked_pts.append((pt.x, pt.y, pt.label)), screenact.Act(True, "click"))[1]):
                call_res = whatsapp_call.run({"action": "request_call", "contact": "Rio", "video": False})
        finally:
            desktop_control.reset_auth_context(tok)
        self.assertIn("status=done", call_res)
        self.assertIn("call_type=voice_call", call_res)
        self.assertIn("verified=true", call_res)
        self.assertEqual(clicked_pts, [(1800, 116, "Voice call")])
        self.assertEqual(desktop_control.snapshot()["gui_confirm_required"], False)

        # Without active voice context, confirm.request remains fail-closed
        with patch.object(confirm, "pending_title", return_value=""), \
             patch.object(confirm, "request", return_value="call confirmation pending") as c_req:
            guarded = whatsapp_call.run({"action": "request_call", "contact": "Rio", "video": False})
        self.assertEqual(guarded, "call confirmation pending")
        c_req.assert_called_once()

    def test_asr_ram_normalization_does_not_mangle_hardware_phrases(self):
        from main import _normalize_rem_address

        self.assertEqual(_normalize_rem_address("Ram, open WhatsApp"), "REM open WhatsApp")
        self.assertEqual(_normalize_rem_address("Ram call Rio"), "REM call Rio")
        self.assertEqual(_normalize_rem_address("My laptop has 16 GB RAM"), "My laptop has 16 GB RAM")


class LiveWhatsAppRegressionSuiteTests(unittest.TestCase):
    def test_stable_contact_extraction_across_repeated_observations_with_cards_and_previews(self):
        from core import screenview
        from plugins import _whatsapp_core

        def _make_noisy_frame(preview_txt):
            return SimpleNamespace(
                app="chrome.exe",
                title="WhatsApp - Google Chrome",
                source="uia+ocr",
                monitor={"left": 0, "top": 0, "width": 1920, "height": 1080},
                windows=[],
                elements=[
                    screenview.Element(eid="sb", role="edit", name="Search or start new chat", value="", left=120, top=145, width=280, height=34),
                    screenview.Element(eid="card1", role="listitem", name=f"Rio 12:15 PM {preview_txt}", value="", left=60, top=220, width=320, height=64),
                    screenview.Element(eid="c1_t", role="text", name="Rio", value="", left=110, top=226, width=90, height=22),
                    screenview.Element(eid="c1_p", role="ocr_text", name=preview_txt, value="", left=110, top=254, width=190, height=18),
                    screenview.Element(eid="c1_tm", role="ocr_text", name="12:15 PM", value="", left=330, top=226, width=50, height=18),
                    screenview.Element(eid="st1", role="ocr_text", name="Photo", value="", left=110, top=272, width=50, height=16),
                    screenview.Element(eid="ocr_n", role="ocr_text", name="Rio 2", value="", left=110, top=310, width=90, height=22),
                    screenview.Element(eid="ocr_p", role="ocr_text", name="Hey how was your day today", value="", left=110, top=336, width=210, height=18),
                    screenview.Element(eid="bad1", role="ocr_text", name="-0-", value="", left=90, top=390, width=40, height=18),
                    screenview.Element(eid="bad2", role="ocr_text", name="-0+", value="", left=90, top=420, width=40, height=18),
                ],
            )

        r1 = _whatsapp_core.extract_whatsapp_contacts(_make_noisy_frame("Hey see you at 5"), query="")
        r2 = _whatsapp_core.extract_whatsapp_contacts(_make_noisy_frame("Okay sounds good!"), query="")
        r3 = _whatsapp_core.extract_whatsapp_contacts(_make_noisy_frame("Voice message"), query="")
        self.assertEqual([c["display_name"] for c in r1], ["Rio", "Rio 2"])
        self.assertEqual([c["display_name"] for c in r2], ["Rio", "Rio 2"])
        self.assertEqual([c["display_name"] for c in r3], ["Rio", "Rio 2"])

    def test_failed_chat_open_and_wrong_active_chat_never_report_done(self):
        from actions import browser_control
        from core import desktop_control, screenact, screenview
        from core.conversation import tool_result_guidance
        from plugins import _whatsapp_core

        list_view = SimpleNamespace(
            app="chrome.exe", title="WhatsApp - Google Chrome", source="uia",
            monitor={"left": 0, "top": 0, "width": 1920, "height": 1080}, windows=[],
            elements=[
                screenview.Element(eid="c1", role="listitem", name="Rio", value="", left=60, top=220, width=300, height=56),
            ],
        )
        wrong_open = SimpleNamespace(
            app="chrome.exe", title="WhatsApp - Google Chrome", source="uia",
            monitor={"left": 0, "top": 0, "width": 1920, "height": 1080}, windows=[],
            elements=[
                screenview.Element(eid="hdr", role="text", name="Bob", value="", left=580, top=105, width=100, height=24),
                screenview.Element(eid="comp", role="edit", name="Type a message", value="", left=580, top=940, width=680, height=40),
            ],
        )
        with patch.object(screenview, "_foreground", return_value=("chrome.exe", "WhatsApp - Google Chrome", 512, 8888)), \
             patch.object(screenview, "all_windows", return_value=[("chrome.exe", "WhatsApp - Google Chrome")]), \
             patch.object(screenview, "is_own_pid", return_value=False), \
             patch.object(screenview, "look", side_effect=[list_view, wrong_open, wrong_open]), \
             patch.object(screenact, "get_cursor_pos", return_value=(210, 248)), \
             patch.object(screenact, "move_cursor", side_effect=lambda pt: screenact.Act(True, "move", point=(pt.x, pt.y))), \
             patch.object(screenact, "click", side_effect=lambda pt: screenact.Act(True, "click", point=(pt.x, pt.y))):
            res = browser_control._native_browser_action("select_contact", {"description": "Rio"}, "chrome")
        self.assertIn("status=unverified", res)
        self.assertIn("couldn't confirm that the chat opened", res)
        self.assertIn("selected_chat_verified=false", res)
        self.assertNotIn("status=done", res)
        self.assertIn("could not confirm", tool_result_guidance(res).casefold())
        # Wrong chat header verification also explicitly rejects expected contact
        chk = _whatsapp_core.verify_whatsapp_active_chat(wrong_open, expected_contact="Rio")
        self.assertFalse(chk["verified"])
        self.assertIn("Bob", chk["reason"])

    def test_shared_composer_dom_uia_and_ocr_fallback_plus_clear_field(self):
        from actions import browser_control
        from core import desktop_control, screenact, screenview
        from core.request_context import RequestContext
        from memory import config_manager
        from plugins import _whatsapp_core

        # 1. DOM composer helper
        fake_page = SimpleNamespace(
            evaluate=AsyncMock(return_value={
                "found": True, "header": "Rio", "text": "old hindi", "focused": True, "rect": [580, 940, 680, 42],
            })
        )
        dom_comp = asyncio.run(_whatsapp_core.dom_locate_whatsapp_composer(fake_page))
        self.assertTrue(dom_comp["composer_found"])
        self.assertEqual(dom_comp["composer_type"], "dom_contenteditable")
        self.assertEqual(dom_comp["active_chat_id"], "chat:rio")
        self.assertTrue(dom_comp["focus_verified"])

        # 2. UIA composer helper
        uia_view = SimpleNamespace(
            app="WhatsApp.exe", title="WhatsApp", source="uia",
            monitor={"left": 0, "top": 0, "width": 1920, "height": 1080},
            elements=[screenview.Element(eid="c", role="edit", name="Type a message", value="some draft", left=580, top=940, width=680, height=40, focused=True)],
        )
        uia_comp = _whatsapp_core.locate_whatsapp_composer(uia_view, active_chat={"chat_id": "chat:rio"})
        self.assertTrue(uia_comp["composer_found"])
        self.assertEqual(uia_comp["source"], "uia")
        self.assertTrue(uia_comp["focus_verified"])

        # 3. OCR fallback composer inside bottom-right band (never omnibox/search bar)
        ocr_view = SimpleNamespace(
            app="chrome.exe", title="WhatsApp - Google Chrome", source="ocr",
            monitor={"left": 0, "top": 0, "width": 1920, "height": 1080},
            elements=[
                screenview.Element(eid="sb", role="ocr_text", name="Search or start new chat", value="", left=120, top=140, width=220, height=24),
                screenview.Element(eid="msg", role="ocr_text", name="Type a message", value="", left=600, top=950, width=200, height=28),
            ],
        )
        ocr_comp = _whatsapp_core.locate_whatsapp_composer(ocr_view, active_chat={"chat_id": "chat:rio"})
        self.assertTrue(ocr_comp["composer_found"])
        self.assertEqual(ocr_comp["composer_type"], "ocr_composer_band")
        self.assertEqual(ocr_comp["y"], 964)

        # 4. clear_field authorization & verified empty readback
        ctx_open = RequestContext.create("Search for Rio", "voice", browser="chrome", browser_task_id="bt_clr")
        ctx_clear = RequestContext.create(
            "Remove all the text from the message bar", "voice",
            parent_instruction=ctx_open.instruction, root_task_id=ctx_open.root_task_id,
            spoken_inputs=(ctx_open.instruction, "Remove all the text from the message bar"),
            browser="chrome", browser_task_id=ctx_open.browser_task_id,
        )
        with patch.object(config_manager, "get_local_autonomy_mode", return_value="trusted"):
            ok_clr, reason_clr = desktop_control.authorize_user_request(
                ctx_clear.instruction, "browser_control",
                {"action": "clear_field", "description": "message bar", "browser": "chrome"},
                source="voice", auth_context=ctx_clear,
            )
            self.assertTrue(ok_clr, reason_clr)

            # Follow-up "Type it again" also allowed with existing draft
            desktop_control.set_active_composition_state(
                composition_requested=True, selected_contact="Rio",
                generated_content="Hey Rio!", typing_authorized=True, auth_context=ctx_clear,
            )
            ctx_again = RequestContext.create(
                "Type it again", "voice",
                parent_instruction=ctx_clear.instruction, root_task_id=ctx_clear.root_task_id,
                spoken_inputs=(ctx_open.instruction, "Remove all the text from the message bar", "Type it again"),
                browser="chrome", browser_task_id=ctx_clear.browser_task_id,
            )
            ok_type, reason_type = desktop_control.authorize_user_request(
                ctx_again.instruction, "browser_control",
                {"action": "smart_type", "description": "message box", "text": "Hey Rio!", "browser": "chrome"},
                source="voice", auth_context=ctx_again,
            )
            self.assertTrue(ok_type, reason_type)

        empty_view = SimpleNamespace(
            app="chrome.exe", title="WhatsApp - Google Chrome", source="uia",
            monitor={"left": 0, "top": 0, "width": 1920, "height": 1080}, windows=[],
            elements=[
                screenview.Element(eid="hdr", role="text", name="Rio", value="", left=580, top=105, width=100, height=24),
                screenview.Element(eid="c", role="edit", name="Type a message", value="", left=580, top=940, width=680, height=40, focused=True),
            ],
        )
        pressed_clr = []
        with patch.object(screenview, "_foreground", return_value=("chrome.exe", "WhatsApp - Google Chrome", 512, 8888)), \
             patch.object(screenview, "all_windows", return_value=[("chrome.exe", "WhatsApp - Google Chrome")]), \
             patch.object(screenview, "is_own_pid", return_value=False), \
             patch.object(screenview, "look", side_effect=[uia_view, empty_view]), \
             patch.object(screenact, "get_cursor_pos", return_value=(700, 958)), \
             patch.object(screenact, "move_cursor", side_effect=lambda pt: screenact.Act(True, "move", point=(pt.x, pt.y))), \
             patch.object(screenact, "click", side_effect=lambda pt: screenact.Act(True, "click", point=(pt.x, pt.y))), \
             patch.object(screenact, "press", side_effect=lambda k, **kw: (pressed_clr.append(k), screenact.Act(True, "press"))[1]):
            clr_out = browser_control._native_browser_action("clear_field", {"description": "message bar"}, "chrome")
        self.assertEqual(pressed_clr, ["ctrl+a", "backspace", "delete"])
        self.assertIn("status=done", clr_out)
        self.assertIn("composer_empty=true", clr_out)
        self.assertIn("The message box is empty now", clr_out)

    def test_uncertain_asr_and_background_speech_preserve_task_without_authorizing_noise(self):
        from core import desktop_control
        from main import RemLive, _is_uncertain_asr

        self.assertTrue(_is_uncertain_asr(""))
        self.assertTrue(_is_uncertain_asr("hmm..."))
        self.assertTrue(_is_uncertain_asr("uh"))
        self.assertFalse(_is_uncertain_asr("Clear it"))
        self.assertFalse(_is_uncertain_asr("Send it"))

        rem = RemLive.__new__(RemLive)
        rem._request_context_lock = threading.Lock()
        rem._request_context = None
        rem._last_user_command = ""
        rem._last_user_command_source = "untrusted"
        rem._active_browser_task = None
        rem._last_assistant_question = ""
        rem._current_voice_turn_task_id = ""
        rem._record_user_request("Open WhatsApp in Chrome and search for Rio", "voice")
        root_tid = rem._current_request_context().browser_task_id
        self.assertTrue(root_tid)

        # Uncertain fragment does NOT authorize click AND does NOT wipe active browser_task_id
        rem._record_user_request("um", "voice")
        self.assertIsNotNone(rem._active_browser_task)
        self.assertEqual(rem._active_browser_task["task_id"], root_tid)
        ok_noise, _ = desktop_control.authorize_user_request(
            rem._current_request_context().instruction, "browser_control",
            {"action": "smart_click", "description": "Rio"}, source="voice", auth_context=rem._current_request_context(),
        )
        self.assertFalse(ok_noise)

        # Real follow-up immediately inherits the same browser_task_id
        rem._record_user_request("Open it again", "voice")
        self.assertEqual(rem._current_request_context().browser_task_id, root_tid)
        rem._record_user_request("Remove all the text from the message bar", "voice")
        self.assertEqual(rem._current_request_context().browser_task_id, root_tid)


class LiveRoutingObservationAndCorrectionTests(unittest.TestCase):
    def setUp(self):
        browser_control._registry.clear_site_targets()
        browser_control._registry.invalidate_sync()
        desktop_control.clear_all_task_state()

    def test_chrome_target_registry_separates_whatsapp_and_youtube(self):
        from core import appidentity, screenact, screenview

        windows = [
            ("chrome.exe", "Lo-fi Beats - YouTube - Google Chrome"),
            ("chrome.exe", "WhatsApp - Google Chrome"),
        ]
        with patch.object(screenview, "_foreground", return_value=("chrome.exe", "Lo-fi Beats - YouTube - Google Chrome", 2100, 9101)), \
             patch.object(screenview, "all_windows", return_value=windows), \
             patch.object(screenview, "is_own_pid", return_value=False), \
             patch.object(browser_control, "_win32_window_identity_for_title", return_value=(9202, 2200, "chrome.exe", "WhatsApp - Google Chrome")):
            wa_win = browser_control._find_browser_window("chrome", site_hint="whatsapp")
            yt_win = browser_control._find_browser_window("chrome", site_hint="youtube")

        self.assertTrue(wa_win["found"])
        self.assertFalse(wa_win["site_mismatch"])
        self.assertEqual(wa_win["hwnd"], 9202)
        self.assertIn("WhatsApp", wa_win["title"])
        self.assertEqual(yt_win["hwnd"], 9101)
        self.assertIn("YouTube", yt_win["title"])
        reg = browser_control.get_browser_target_registry("chrome")
        self.assertEqual(reg["whatsapp"]["hwnd"], 9202)
        self.assertEqual(reg["youtube"]["hwnd"], 9101)

        # Open web.whatsapp.com when WhatsApp Chrome window already exists -> focus instead of launching duplicate
        with patch.object(appidentity, "running", return_value=True), \
             patch.object(browser_control, "_find_browser_window", return_value=wa_win), \
             patch.object(browser_control, "_ensure_browser_foreground", return_value=(True, {**wa_win, "foreground": True}, "")), \
             patch.object(browser_control.subprocess, "Popen") as mock_popen:
            res = browser_control._open_native("https://web.whatsapp.com/", "chrome")
        self.assertFalse(mock_popen.called)
        self.assertIn("status=done", res)
        self.assertIn("navigation_completed=true", res)

    def test_get_url_fast_path_skips_ocr_and_reports_timings(self):
        from core import screenview

        t0 = time.monotonic()
        fake_view = SimpleNamespace(
            app="chrome.exe",
            title="WhatsApp - Google Chrome",
            source="uia",
            degraded=False,
            elements=[SimpleNamespace(role="edit", name="Address and search bar", label="Address bar", value="https://web.whatsapp.com/")],
            windows=[("chrome.exe", "WhatsApp - Google Chrome")],
            pid=2200,
        )
        look_calls = []

        def _fake_look(**kw):
            look_calls.append(kw)
            return fake_view

        with patch.object(screenview, "_foreground", return_value=("chrome.exe", "WhatsApp - Google Chrome", 2200, 9202)), \
             patch.object(screenview, "all_windows", return_value=[("chrome.exe", "WhatsApp - Google Chrome")]), \
             patch.object(screenview, "is_own_pid", return_value=False), \
             patch.object(screenview, "look", side_effect=_fake_look):
            out = browser_control._native_browser_action("get_url", {"url": "https://web.whatsapp.com/"}, "chrome")
        elapsed_ms = (time.monotonic() - t0) * 1000.0
        self.assertLess(elapsed_ms, 500.0)
        self.assertEqual(len(look_calls), 1)
        self.assertFalse(look_calls[0]["allow_ocr"])
        self.assertIn("status=done", out)
        self.assertIn("timing_total_ms=", out)
        self.assertIn("https://web.whatsapp.com/", out)

    def test_inspect_local_screen_never_attributes_background_chrome_ocr_to_python_self(self):
        from core import appidentity, screenview

        self_view = SimpleNamespace(
            app="python.exe", title="REM - Mark LVII", source="self", degraded=True,
            reason="the assistant's own window is in front", elements=[],
            windows=[("python.exe", "REM - Mark LVII"), ("chrome.exe", "WhatsApp - Google Chrome")],
            pid=1111, monitor={"left": 0, "top": 0, "width": 1920, "height": 1080},
        )
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "api_keys.json"
            with patch.object(config_manager, "CONFIG_FILE", cfg), \
                 patch.object(screenview, "_foreground", return_value=("python.exe", "REM - Mark LVII", 1111, 1001)), \
                 patch.object(screenview, "is_own_pid", side_effect=lambda p: p == 1111), \
                 patch.object(screenview, "look", return_value=self_view), \
                 patch.object(screenview, "_scan_ocr", side_effect=AssertionError("must not scrape background OCR under app=self")), \
                 patch.object(appidentity, "focus_window_by_app", return_value={"activated": False, "reason": "foreground_lock"}):
                summary = screen_observation.inspect_local_screen("Check whether WhatsApp is open in Chrome")
        self.assertIn("Foreground rejection: REM's own window is in front", summary)
        self.assertIn("target_matched=False", summary)
        self.assertIn("confidence=uncertain", summary)
        self.assertIn("ocr_items=0", summary)

    def test_correction_and_and_check_continuity_without_system_status_confusion(self):
        from main import RemLive, TOOL_DECLARATIONS

        sys_decl = next(d for d in TOOL_DECLARATIONS if d["name"] == "system_status")
        self.assertIn("NEVER call this tool to check open applications", sys_decl["description"])

        rem = RemLive.__new__(RemLive)
        rem._request_context_lock = threading.Lock()
        rem._request_context = None
        rem._last_user_command = ""
        rem._last_user_command_source = "untrusted"
        rem._active_browser_task = None
        rem._last_assistant_question = ""
        rem._current_voice_turn_task_id = ""

        # Step 1: User starts with YouTube, then corrects to WhatsApp
        rem._record_user_request("Open YouTube in Chrome", "voice")
        btid = rem._current_request_context().browser_task_id
        self.assertTrue(btid)

        rem._record_user_request("No, WhatsApp", "voice")
        ctx_corr = rem._current_request_context()
        self.assertEqual(ctx_corr.browser_task_id, btid)
        ok_go, reason_go = desktop_control.authorize_user_request(
            ctx_corr.instruction, "browser_control",
            {"action": "go_to", "url": "https://web.whatsapp.com/"},
            source="voice", auth_context=ctx_corr,
        )
        self.assertTrue(ok_go, reason_go)

        # Step 2: Short follow-up "And check" authorizes observation without browser_task=none
        rem._record_user_request("And check", "voice")
        ctx_chk = rem._current_request_context()
        self.assertEqual(ctx_chk.browser_task_id, btid)
        for tool, args in (
            ("desktop_ui", {"action": "observe"}),
            ("screen_process", {"angle": "screen", "text": "And check"}),
            ("browser_control", {"action": "inspect"}),
        ):
            ok, reason = desktop_control.authorize_user_request(
                ctx_chk.instruction, tool, args, source="voice", auth_context=ctx_chk,
            )
            self.assertTrue(ok, f"{tool} failed: {reason}")

        # Step 3: "Check my window directly" authorizes window observation
        rem._record_user_request("Check my window directly", "voice")
        ctx_dir = rem._current_request_context()
        ok_dir, reason_dir = desktop_control.authorize_user_request(
            ctx_dir.instruction, "screen_process", {"angle": "screen", "text": "Check my window directly"},
            source="voice", auth_context=ctx_dir,
        )
        self.assertTrue(ok_dir, reason_dir)

        # Step 4: "Cancel" invalidates active task & stale contexts
        rem._record_user_request("Cancel", "voice")
        self.assertIsNone(rem._active_browser_task)
        ok_after_cancel, _ = desktop_control.authorize_user_request(
            ctx_corr.instruction, "browser_control", {"action": "smart_type", "text": "hello"},
            source="voice", auth_context=ctx_corr,
        )
        self.assertFalse(ok_after_cancel)

    def test_whatsapp_search_bar_recovery_and_strict_rio_matching(self):
        from core import screenact, screenview

        miss_view = SimpleNamespace(
            app="chrome.exe", title="WhatsApp - Google Chrome", source="uia", degraded=False, elements=[],
            monitor={"left": 0, "top": 0, "width": 1920, "height": 1080},
        )
        sb_view = SimpleNamespace(
            app="chrome.exe", title="WhatsApp - Google Chrome", source="uia", degraded=False,
            elements=[
                screenview.Element(eid="sb", role="edit", name="Search input textbox", value="", left=90, top=140, width=260, height=34),
            ],
            monitor={"left": 0, "top": 0, "width": 1920, "height": 1080},
        )
        results_view = SimpleNamespace(
            app="chrome.exe", title="WhatsApp - Google Chrome", source="uia", degraded=False,
            elements=[
                screenview.Element(eid="c1", role="listitem", name="Mario\nYesterday\nHey", value="", left=80, top=220, width=280, height=64),
                screenview.Element(eid="c2", role="listitem", name="Rio\nToday\nReady", value="", left=80, top=290, width=280, height=64),
            ],
            monitor={"left": 0, "top": 0, "width": 1920, "height": 1080},
        )
        typed = []
        with patch.object(screenview, "_foreground", return_value=("chrome.exe", "WhatsApp - Google Chrome", 2200, 9202)), \
             patch.object(screenview, "all_windows", return_value=[("chrome.exe", "WhatsApp - Google Chrome")]), \
             patch.object(screenview, "is_own_pid", return_value=False), \
             patch.object(screenview, "look", side_effect=[miss_view, sb_view, results_view]), \
             patch.object(screenact, "move_cursor", side_effect=lambda pt: screenact.Act(True, "move", point=(pt.x, pt.y))), \
             patch.object(screenact, "click", side_effect=lambda pt: screenact.Act(True, "click", point=(pt.x, pt.y))), \
             patch.object(screenact, "type_text", side_effect=lambda txt, **kw: (typed.append(txt), screenact.Act(True, "type"))[1]):
            out = browser_control._native_browser_action("search_contact", {"query": "Rio", "description": "WhatsApp search bar"}, "chrome")
        self.assertEqual(typed, ["Rio"])
        self.assertIn("status=done", out)
        self.assertIn("recovery_attempts=2", out)
        self.assertIn("unique_contact_count=1", out)
        self.assertIn("matched_contacts=Rio", out)
        self.assertNotIn("Mario", out)


class WhatsAppLifecycleAndVerificationTests(unittest.TestCase):
    def setUp(self):
        desktop_control.clear_all_task_state()
        desktop_control._sent_attempt_records.clear()
        browser_control._registry.clear_site_targets()
        browser_control._registry.invalidate_sync()
        browser_control._mark_opening_inflight("chrome", "whatsapp", False)

    def _ctx(self, instruction: str, *, task_id: str = "t-wa", btid: str = "bt-wa", age_s: float = 0.0):
        now = time.monotonic() - age_s
        return RequestContext.create(
            instruction, "voice", now=now, task_id=task_id,
            browser="chrome", browser_task_id=btid, spoken_inputs=(instruction,),
        )

    def test_1_to_5_sandy_silence_yes_draft_and_recipient_change_confirmation_rules(self):
        ctx = self._ctx("Compose a friendly message for Rio")
        comp = desktop_control.set_active_composition_state(
            composition_requested=True,
            composition_task_id="t-wa",
            selected_contact="Rio",
            generated_content="Hey Rio, hope your day is going well!",
            draft_verified=True,
            auth_context=ctx,
        )
        self.assertEqual(desktop_control.get_message_lifecycle(ctx)["state"], desktop_control.MSG_DRAFT_VERIFIED)

        conf = desktop_control.arm_send_confirmation(
            recipient="Rio", draft="Hey Rio, hope your day is going well!",
            composition_task_id="t-wa", auth_context=ctx,
        )
        self.assertEqual(desktop_control.get_message_lifecycle(ctx)["state"], desktop_control.MSG_AWAITING_CONFIRMATION)

        # 1. "Sandy" after send confirmation MUST NOT send
        eval_sandy = desktop_control.evaluate_send_confirmation_utterance("Sandy", auth_context=ctx)
        self.assertFalse(eval_sandy["authorized"])
        ok_sandy_ctx = self._ctx("Sandy")
        ok_send_sandy, deny_sandy = desktop_control.authorize_user_request(
            "Sandy", "send_message",
            {"receiver": "Rio", "message_text": "Hey Rio, hope your day is going well!", "platform": "WhatsApp"},
            source="voice", auth_context=ok_sandy_ctx,
        )
        self.assertFalse(ok_send_sandy)

        # 2. Silence MUST NOT send
        eval_silence = desktop_control.evaluate_send_confirmation_utterance("", auth_context=ctx)
        self.assertFalse(eval_silence["authorized"])
        ok_sil, _, _ = desktop_control.is_send_authorized(
            receiver="Rio", draft_hash=comp["generated_content_hash"],
            composition_task_id="t-wa", utterance="", auth_context=ctx,
        )
        self.assertFalse(ok_sil)

        # 3. "Yes, send it" authorizes ONLY the exact pending draft and recipient
        eval_yes = desktop_control.evaluate_send_confirmation_utterance("Yes, send it", auth_context=ctx)
        self.assertTrue(eval_yes["authorized"])
        self.assertEqual(desktop_control.get_message_lifecycle(ctx)["state"], desktop_control.MSG_SEND_AUTHORIZED)
        yes_ctx = self._ctx("Yes, send it")
        ok_yes, deny_yes, _ = desktop_control.is_send_authorized(
            receiver="Rio", draft_hash=comp["generated_content_hash"],
            composition_task_id="t-wa", utterance="Yes, send it", auth_context=yes_ctx,
        )
        self.assertTrue(ok_yes, deny_yes)

        # 4. Changed draft invalidates confirmation
        ok_changed_draft, _ = desktop_control.is_send_authorized(
            receiver="Rio", draft_hash="deadbeef12345678",
            composition_task_id="t-wa", utterance="Yes, send it", auth_context=yes_ctx,
        )[:2]
        self.assertFalse(ok_changed_draft)

        # Re-arm and confirm, then test 5. Changed recipient invalidates confirmation
        desktop_control.arm_send_confirmation(
            recipient="Rio", draft="Hey Rio, hope your day is going well!",
            composition_task_id="t-wa", auth_context=ctx,
        )
        desktop_control.evaluate_send_confirmation_utterance("Send the message", auth_context=ctx)
        ok_changed_rec, _, _ = desktop_control.is_send_authorized(
            receiver="Mario", draft_hash=comp["generated_content_hash"],
            composition_task_id="t-wa", utterance="Send the message", auth_context=yes_ctx,
        )
        self.assertFalse(ok_changed_rec)

    def test_6_to_8_enter_unverified_without_bubble_verified_with_bubble_and_duplicate_blocked(self):
        from core import screenact, screenview
        from plugins import _whatsapp_core as _wa

        ctx = self._ctx("Yes, send it")
        draft = "Hey Rio, checking in!"
        dhash = _wa.draft_hash(draft)
        desktop_control.set_active_composition_state(
            composition_requested=True, composition_task_id="t-wa",
            selected_contact="Rio", generated_content=draft, draft_verified=True, auth_context=ctx,
        )
        desktop_control.arm_send_confirmation(recipient="Rio", draft=draft, composition_task_id="t-wa", auth_context=ctx)
        desktop_control.evaluate_send_confirmation_utterance("Yes, send it", auth_context=ctx)

        before_view = SimpleNamespace(
            app="chrome.exe", title="WhatsApp - Google Chrome", source="uia", degraded=False,
            elements=[
                screenview.Element(eid="hdr", role="text", name="Rio", value="", left=580, top=105, width=100, height=24),
                screenview.Element(eid="c", role="edit", name="Type a message", value=draft, left=580, top=940, width=680, height=40, focused=True),
            ],
            monitor={"left": 0, "top": 0, "width": 1920, "height": 1080},
        )
        # Composer cleared, BUT NO outgoing bubble appeared!
        empty_no_bubble = SimpleNamespace(
            app="chrome.exe", title="WhatsApp - Google Chrome", source="uia", degraded=False,
            elements=[
                screenview.Element(eid="hdr", role="text", name="Rio", value="", left=580, top=105, width=100, height=24),
                screenview.Element(eid="c", role="edit", name="Type a message", value="", left=580, top=940, width=680, height=40, focused=True),
            ],
            monitor={"left": 0, "top": 0, "width": 1920, "height": 1080},
        )

        # 6. Enter pressed without new bubble => SEND_UNVERIFIED (never "Message sent")
        tok = desktop_control.bind_auth_context(ctx)
        try:
            with patch.object(screenview, "_foreground", return_value=("chrome.exe", "WhatsApp - Google Chrome", 2200, 9202)), \
                 patch.object(screenview, "all_windows", return_value=[("chrome.exe", "WhatsApp - Google Chrome")]), \
                 patch.object(screenview, "is_own_pid", return_value=False), \
                 patch.object(screenview, "look", side_effect=[before_view, empty_no_bubble]), \
                 patch.object(screenact, "press", return_value=screenact.Act(True, "press")):
                out_unver = browser_control._native_browser_action("press", {"key": "enter"}, "chrome")
        finally:
            desktop_control.reset_auth_context(tok)
        self.assertIn("status=unverified", out_unver)
        self.assertIn("WhatsApp has not confirmed that it was sent", out_unver)
        self.assertEqual(desktop_control.get_message_lifecycle(ctx)["state"], desktop_control.MSG_SEND_UNVERIFIED)

        # 8. Duplicate send is blocked automatically after unverified send attempt
        ok_dup, dup_reason, _ = desktop_control.is_send_authorized(
            receiver="Rio", draft_hash=dhash, composition_task_id="t-wa",
            utterance="Yes, send it", auth_context=ctx,
        )
        self.assertFalse(ok_dup)
        self.assertIn("duplicate", dup_reason.lower())

        # 7. Positive outgoing bubble + verified conversation header establishes SEND_VERIFIED
        with_bubble = SimpleNamespace(
            app="chrome.exe", title="WhatsApp - Google Chrome", source="uia", degraded=False,
            elements=[
                screenview.Element(eid="hdr", role="text", name="Rio", value="", left=580, top=105, width=100, height=24),
                screenview.Element(eid="b1", role="text", name=draft, value="", left=900, top=820, width=320, height=34),
                screenview.Element(eid="c", role="edit", name="Type a message", value="", left=580, top=940, width=680, height=40, focused=True),
            ],
            monitor={"left": 0, "top": 0, "width": 1920, "height": 1080},
        )
        det = _wa.verify_outgoing_message_detailed(with_bubble, draft, before_view=before_view, expected_contact="Rio")
        self.assertTrue(det["verified"])
        self.assertTrue(det["bubble_found"])
        self.assertTrue(det["composer_cleared"])

    def test_9_to_11_rio_not_mario_no_arbitrary_search_field_and_first_chat_verified(self):
        from core import screenact, screenview
        from plugins import _whatsapp_core as _wa

        # 9 & 10. Search must NOT use omnibox/composer if no WhatsApp search field exists
        omnibox_only = SimpleNamespace(
            app="chrome.exe", title="WhatsApp - Google Chrome", source="uia", degraded=False,
            elements=[
                screenview.Element(eid="addr", role="edit", name="Address and search bar", value="web.whatsapp.com", left=120, top=42, width=800, height=30),
                screenview.Element(eid="comp", role="edit", name="Type a message", value="", left=580, top=940, width=680, height=40),
            ],
            monitor={"left": 0, "top": 0, "width": 1920, "height": 1080},
        )
        with patch.object(screenview, "_foreground", return_value=("chrome.exe", "WhatsApp - Google Chrome", 2200, 9202)), \
             patch.object(screenview, "all_windows", return_value=[("chrome.exe", "WhatsApp - Google Chrome")]), \
             patch.object(screenview, "is_own_pid", return_value=False), \
             patch.object(screenview, "look", return_value=omnibox_only), \
             patch.object(screenact, "press", side_effect=AssertionError("must never press Enter blindly")), \
             patch.object(screenact, "click", side_effect=AssertionError("must never click unverified field")):
            fail_search = browser_control._native_browser_action("search_contact", {"query": "Rio"}, "chrome")
        self.assertIn("status=failed", fail_search)
        self.assertIn("search_bar_found=false", fail_search)

        # 9. Verify Rio whole-word contact matching rejects Mario
        self.assertEqual(_wa.contact_match("Rio", "Mario"), "none")
        self.assertEqual(_wa.contact_match("Rio", "Rio"), "exact")

        # 11. "Open the first chat" verifies header before returning done
        list_view = SimpleNamespace(
            app="chrome.exe", title="WhatsApp - Google Chrome", source="uia", degraded=False,
            elements=[
                screenview.Element(eid="c1", role="listitem", name="Rio\nToday\nReady", value="", left=80, top=220, width=280, height=64),
                screenview.Element(eid="c2", role="listitem", name="Mario\nYesterday\nHi", value="", left=80, top=290, width=280, height=64),
            ],
            monitor={"left": 0, "top": 0, "width": 1920, "height": 1080},
        )
        opened_view = SimpleNamespace(
            app="chrome.exe", title="WhatsApp - Google Chrome", source="uia", degraded=False,
            elements=[
                screenview.Element(eid="hdr", role="text", name="Rio", value="", left=580, top=105, width=100, height=24),
                screenview.Element(eid="comp", role="edit", name="Type a message", value="", left=580, top=940, width=680, height=40),
            ],
            monitor={"left": 0, "top": 0, "width": 1920, "height": 1080},
        )
        with patch.object(screenview, "_foreground", return_value=("chrome.exe", "WhatsApp - Google Chrome", 2200, 9202)), \
             patch.object(screenview, "all_windows", return_value=[("chrome.exe", "WhatsApp - Google Chrome")]), \
             patch.object(screenview, "is_own_pid", return_value=False), \
             patch.object(screenview, "look", side_effect=[list_view, opened_view]), \
             patch.object(screenact, "get_cursor_pos", return_value=(100, 100)), \
             patch.object(screenact, "move_cursor", side_effect=lambda pt: screenact.Act(True, "move", point=(pt.x, pt.y))), \
             patch.object(screenact, "click", side_effect=lambda pt: screenact.Act(True, "click", point=(pt.x, pt.y))):
            out_first = browser_control._native_browser_action("smart_click", {"description": "open the first chat"}, "chrome")
        self.assertIn("status=done", out_first)
        self.assertIn("active_contact=Rio", out_first)
        self.assertIn("selected_chat_verified=true", out_first)

    def test_12_to_14_reconnect_expiry_inflight_open_continuity_and_ocr_stats(self):
        from core import screenview
        from main import RemLive

        # 12. Reconnect preserves draft/recipient & invalidates pre-reconnect send confirmation
        ctx = self._ctx("Compose message to Rio")
        draft = "Hey Rio, see you at 5"
        comp = desktop_control.set_active_composition_state(
            composition_requested=True, composition_task_id="t-wa",
            selected_contact="Rio", generated_content=draft, draft_verified=True, auth_context=ctx,
        )
        desktop_control.arm_send_confirmation(recipient="Rio", draft=draft, composition_task_id="t-wa", auth_context=ctx)
        desktop_control.evaluate_send_confirmation_utterance("Yes, send it", auth_context=ctx)
        desktop_control.on_live_reconnect()
        preserved = desktop_control.get_active_composition_state(ctx)
        self.assertIsNotNone(preserved)
        self.assertEqual(preserved["selected_contact"], "Rio")
        self.assertTrue(preserved["needs_reverify"])
        self.assertIsNone(desktop_control.get_pending_send_confirmation(ctx))
        ok_post_rc, _, _ = desktop_control.is_send_authorized(
            receiver="Rio", draft_hash=comp["generated_content_hash"],
            composition_task_id="t-wa", utterance="Yes", auth_context=ctx,
        )
        self.assertFalse(ok_post_rc)

        # 13. In-flight WhatsApp opening joins existing open window instead of returning busy failure
        browser_control._mark_opening_inflight("chrome", "whatsapp", True)
        try:
            with patch.object(browser_control, "_find_browser_window", return_value={
                "found": True, "foreground": True, "browser": "chrome",
                "app": "chrome.exe", "title": "WhatsApp - Google Chrome", "hwnd": 9202, "pid": 2200, "site_mismatch": False,
            }), \
                 patch.object(browser_control, "_ensure_browser_foreground", return_value=(True, {"found": True, "foreground": True, "hwnd": 9202, "pid": 2200, "title": "WhatsApp - Google Chrome"}, "")):
                joined = browser_control._wait_or_inspect_inflight_open("chrome", "whatsapp", url="https://web.whatsapp.com/")
        finally:
            browser_control._mark_opening_inflight("chrome", "whatsapp", False)
        self.assertIn("joined_inflight=true", joined)
        self.assertIn("status=done", joined)

        # 14. Browser task continuity survives legitimate WhatsApp workflow + switch action
        rem = RemLive.__new__(RemLive)
        rem._request_context_lock = threading.Lock()
        rem._request_context = None
        rem._last_user_command = ""
        rem._last_user_command_source = "untrusted"
        rem._active_browser_task = None
        rem._last_assistant_question = ""
        rem._current_voice_turn_task_id = ""

        rem._record_user_request("Open WhatsApp in Chrome", "voice")
        btid = rem._current_request_context().browser_task_id
        for utt, tool, args in (
            ("Search for Rio in WhatsApp", "browser_control", {"action": "search_contact", "query": "Rio"}),
            ("Open the first chat", "browser_control", {"action": "smart_click", "description": "first chat"}),
            ("Switch tab to WhatsApp", "browser_control", {"action": "switch", "browser": "chrome"}),
        ):
            rem._record_user_request(utt, "voice")
            c = rem._current_request_context()
            self.assertEqual(c.browser_task_id, btid)
            ok, reason = desktop_control.authorize_user_request(
                c.instruction, tool, args, source="voice", auth_context=c,
            )
            self.assertTrue(ok, f"{utt}: {reason}")

        # OCR profiler telemetry available
        stats = screenview.ocr_stats()
        self.assertIn("initialized", stats)
        self.assertIn("init_count", stats)
        self.assertIn("exec_count", stats)


class ChromeNavigationTabReuseAndQtShutdownTests(unittest.TestCase):
    def setUp(self):
        desktop_control.clear_all_task_state()
        desktop_control._sent_attempt_records.clear()
        browser_control._registry.clear_site_targets()
        browser_control._registry.invalidate_sync("chrome")

    def tearDown(self):
        desktop_control.clear_all_task_state()
        desktop_control._sent_attempt_records.clear()
        browser_control._registry.clear_site_targets()
        browser_control._registry.invalidate_sync("chrome")

    def test_j01_open_whatsapp_reuses_existing_chrome_window_no_duplicate(self):
        with patch.object(browser_control.appidentity, "running", return_value=True), \
             patch.object(browser_control, "_find_browser_window", return_value={
                 "found": True, "foreground": False, "browser": "chrome",
                 "app": "chrome.exe", "title": "WhatsApp - Google Chrome",
                 "hwnd": 4101, "pid": 8201, "matched_site": "whatsapp", "site_mismatch": False,
             }), \
             patch.object(browser_control, "_ensure_browser_foreground", return_value=(
                 True,
                 {"found": True, "foreground": True, "browser": "chrome",
                  "app": "chrome.exe", "title": "WhatsApp - Google Chrome", "hwnd": 4101, "pid": 8201},
                 "",
             )), \
             patch.object(browser_control.subprocess, "Popen") as mock_popen:
            res = browser_control._open_native("https://web.whatsapp.com/", "chrome")

        mock_popen.assert_not_called()
        self.assertIn("status=done", res)
        self.assertIn("reused_existing=true", res)
        self.assertIn("foreground_verified=true", res)
        self.assertIn("target_matched=true", res)
        self.assertIn("navigation_lifecycle=SUCCESS", res)

    def test_j02_open_youtube_reuses_existing_chrome_window_no_duplicate(self):
        with patch.object(browser_control.appidentity, "running", return_value=True), \
             patch.object(browser_control, "_find_browser_window", return_value={
                 "found": True, "foreground": True, "browser": "chrome",
                 "app": "chrome.exe", "title": "YouTube - Google Chrome",
                 "hwnd": 4102, "pid": 8201, "matched_site": "youtube", "site_mismatch": False,
             }), \
             patch.object(browser_control, "_ensure_browser_foreground", return_value=(
                 True,
                 {"found": True, "foreground": True, "browser": "chrome",
                  "app": "chrome.exe", "title": "YouTube - Google Chrome", "hwnd": 4102, "pid": 8201},
                 "",
             )), \
             patch.object(browser_control.subprocess, "Popen") as mock_popen:
            res = browser_control._open_native("https://www.youtube.com/", "chrome")

        mock_popen.assert_not_called()
        self.assertIn("status=done", res)
        self.assertIn("reused_existing=true", res)
        self.assertIn("foreground_verified=true", res)
        self.assertIn("target_matched=true", res)

    def test_j03_project_overview_and_new_tab_never_verify_whatsapp_or_youtube(self):
        for title, url in (
            ("Project Overview - Google Chrome", ""),
            ("New Tab - Google Chrome", "chrome://newtab/"),
            ("", "about:blank"),
        ):
            for site in ("whatsapp", "youtube"):
                ident = browser_control.verify_site_page_identity(site, url=url, title=title)
                self.assertFalse(ident["verified"], f"{site} falsely verified for {title!r}/{url!r}")
                self.assertFalse(ident["target_matched"])

        with patch.object(browser_control.appidentity, "running", return_value=True), \
             patch("core.screenview.all_windows", return_value=[("chrome.exe", "Project Overview - Google Chrome")]), \
             patch("core.screenview._foreground", return_value=("chrome.exe", "Project Overview - Google Chrome", 991, 9910)), \
             patch("core.screenview.look", return_value=SimpleNamespace(title="Project Overview - Google Chrome", elements=[])):
            res = browser_control._verify_native_browser("chrome", "https://web.whatsapp.com/", timeout_s=0.05)

        self.assertTrue(res.startswith("status=unverified "))
        self.assertIn("target_matched=false", res)
        self.assertIn("page_state=wrong_tab", res)

    def test_j04_google_search_results_rejected_for_whatsapp_and_youtube(self):
        ident_wa = browser_control.verify_site_page_identity(
            "whatsapp",
            url="https://www.google.com/search?q=whatsapp+web",
            title="whatsapp web - Google Search - Google Chrome",
        )
        self.assertFalse(ident_wa["verified"])
        self.assertFalse(ident_wa["target_matched"])
        self.assertEqual(ident_wa["page_state"], "wrong_tab")

        ident_yt = browser_control.verify_site_page_identity(
            "youtube",
            url="https://www.google.com/search?q=youtube",
            title="youtube - Google Search - Google Chrome",
        )
        self.assertFalse(ident_yt["verified"])
        self.assertEqual(ident_yt["page_state"], "wrong_tab")

        # Also ensure _site_key_from_url_or_title does not tag Google Search SERP as whatsapp/youtube
        self.assertEqual(
            browser_control._site_key_from_url_or_title("", "whatsapp web - Google Search - Google Chrome"),
            "google",
        )

    def test_j05_loading_past_deadline_returns_timeout_not_done(self):
        with patch.object(browser_control.appidentity, "running", return_value=True), \
             patch("core.screenview.all_windows", return_value=[("chrome.exe", "Loading... - Google Chrome")]), \
             patch("core.screenview._foreground", return_value=("chrome.exe", "Loading... - Google Chrome", 991, 9910)), \
             patch("core.screenview.look", return_value=SimpleNamespace(title="Loading... - Google Chrome", elements=[])):
            res = browser_control._verify_native_browser("chrome", "https://www.youtube.com/", timeout_s=0.05)

        self.assertTrue(res.startswith("status=timeout "))
        self.assertIn("page_state=loading", res)
        self.assertIn("foreground_verified=false", res)

    def test_j06_loaded_page_without_foreground_returns_unverified(self):
        with patch.object(browser_control.appidentity, "running", return_value=True), \
             patch("core.screenview.all_windows", return_value=[("chrome.exe", "WhatsApp - Google Chrome")]), \
             patch("core.screenview.is_own_pid", return_value=False), \
             patch.object(browser_control, "_ensure_browser_foreground", return_value=(False, {"found": True, "foreground": False}, "foreground lock")):
            res = browser_control._verify_native_browser("chrome", "https://web.whatsapp.com/", timeout_s=0.05)

        self.assertTrue(res.startswith("status=unverified "))
        self.assertIn("target_matched=true", res)
        self.assertIn("foreground_verified=false", res)

    def test_j07_screen_observation_target_mismatch_reports_uncertain_wrong_tab(self):
        fake_view = SimpleNamespace(
            app="chrome.exe",
            title="Project Overview - Google Chrome",
            hwnd=5511,
            pid=7711,
            source="uia",
            degraded=False,
            reason="",
            windows=[("chrome.exe", "Project Overview - Google Chrome")],
            elements=[SimpleNamespace(name="Overview", role="document", text="", value="")],
        )
        with patch("core.screenview.look", return_value=fake_view), \
             patch("core.screenview._foreground", return_value=("chrome.exe", "Project Overview - Google Chrome", 7711, 5511)), \
             patch("core.appidentity.focus_window_by_app", return_value={"activated": False, "reason": "wrong_tab"}), \
             patch("core.screenview.is_own_pid", return_value=False):
            obs = screen_observation.inspect_local_screen(
                question="Check YouTube in Chrome", target_site="youtube",
            )

        self.assertIn("target_matched=False", obs)
        self.assertIn("page_state=wrong_tab", obs)
        self.assertIn("confidence=uncertain", obs)
        self.assertIn("target_identified=False", obs)

    def test_j08_repeat_open_and_it_isnt_opening_preserve_browser_continuity(self):
        from main import RemLive

        rem = RemLive.__new__(RemLive)
        rem._request_context_lock = threading.Lock()
        rem._request_context = None
        rem._last_user_command = ""
        rem._last_user_command_source = "untrusted"
        rem._active_browser_task = None
        rem._last_assistant_question = ""
        rem._current_voice_turn_task_id = ""

        rem._record_user_request("Open YouTube in Chrome", "voice")
        btid = rem._current_request_context().browser_task_id
        for phrase, act in (
            ("Open YouTube again", "go_to"),
            ("It isn't opening", "reload"),
        ):
            rem._record_user_request(phrase, "voice")
            ctx = rem._current_request_context()
            self.assertEqual(ctx.browser_task_id, btid)
            ok, reason = desktop_control.authorize_user_request(
                ctx.instruction, "browser_control", {"action": act, "url": "https://www.youtube.com/"},
                source="voice", auth_context=ctx,
            )
            self.assertTrue(ok, f"{phrase} / {act} denied: {reason}")

    def test_j09_explicit_reload_refresh_retry_authorize_reload_action(self):
        for phrase in ("Refresh the page", "Reload YouTube", "Retry loading", "Reopen it"):
            ctx = RequestContext.create(
                phrase, "voice", now=time.monotonic(),
                browser="chrome", browser_task_id="bt-reload-1", spoken_inputs=(phrase,),
            )
            ok, reason = desktop_control.authorize_user_request(
                phrase, "browser_control", {"action": "reload"}, source="voice", auth_context=ctx,
            )
            self.assertTrue(ok, f"Expected reload authorized for {phrase!r}, got {reason}")

    def test_j10_no_to_suggested_refresh_never_authorizes_reload(self):
        from main import RemLive

        rem = RemLive.__new__(RemLive)
        rem._request_context_lock = threading.Lock()
        rem._request_context = None
        rem._last_user_command = ""
        rem._last_user_command_source = "untrusted"
        rem._active_browser_task = None
        rem._last_assistant_question = "Would you like me to refresh the page?"
        rem._current_voice_turn_task_id = ""

        rem._record_user_request("Refresh the page", "voice")
        rem._last_assistant_question = "YouTube didn't update yet. Should I refresh the page?"
        rem._record_user_request("No", "voice")
        ctx = rem._current_request_context()
        ok, reason = desktop_control.authorize_user_request(
            ctx.instruction, "browser_control", {"action": "reload"}, source="voice", auth_context=ctx,
        )
        self.assertFalse(ok)
        self.assertIn("declined", reason.lower())

        ok_direct, _ = desktop_control.authorize_user_request(
            "Do not reload", "browser_control", {"action": "reload"}, source="voice",
        )
        self.assertFalse(ok_direct)

    def test_j11_youtube_hindi_open_and_honest_language_change_verification(self):
        self.assertEqual(
            browser_control.youtube_hindi_intent("Open YouTube in Hindi")["mode"],
            "open_in_hindi",
        )
        self.assertEqual(
            browser_control.youtube_hindi_intent("Change YouTube's language to Hindi")["mode"],
            "change_language",
        )

        # Mock Playwright page staying in English when "Change YouTube's language to Hindi" is asked -> unverified
        fake_page = MagicMock()
        fake_page.url = "https://www.youtube.com/"
        fake_page.is_closed.return_value = False
        fake_page.goto = AsyncMock(return_value=None)
        fake_page.title = AsyncMock(return_value="YouTube")
        fake_page.bring_to_front = AsyncMock(return_value=None)
        fake_loc = MagicMock()
        fake_loc.inner_text = AsyncMock(return_value="Home Shorts Subscriptions")
        fake_page.locator = MagicMock(return_value=fake_loc)

        async def _eval_en(expr):
            if "visibilityState" in expr:
                return True
            if "documentElement.lang" in expr:
                return "en"
            return True

        fake_page.evaluate = AsyncMock(side_effect=_eval_en)
        sess = browser_control._BrowserSession("chrome")
        sess._context = SimpleNamespace(pages=[fake_page])
        sess._page = fake_page

        ctx = RequestContext.create(
            "Change YouTube's language to Hindi", "voice",
            now=time.monotonic(), browser="chrome", browser_task_id="bt-hi",
            spoken_inputs=("Change YouTube's language to Hindi",),
        )
        tok = desktop_control.bind_auth_context(ctx)
        try:
            res = asyncio.run(sess.go_to("https://www.youtube.com/"))
        finally:
            desktop_control.reset_auth_context(tok)
        self.assertTrue(res.startswith("status=unverified "))
        self.assertIn("language_verified=false", res)

        # Whereas "Open YouTube in Hindi" navigates with ?hl=hi&gl=IN and verifies cleanly
        fake_page.goto = AsyncMock(side_effect=lambda u, **kw: setattr(fake_page, "url", u))
        ctx_open = RequestContext.create(
            "Open YouTube in Hindi", "voice",
            now=time.monotonic(), browser="chrome", browser_task_id="bt-hi2",
            spoken_inputs=("Open YouTube in Hindi",),
        )
        tok2 = desktop_control.bind_auth_context(ctx_open)
        try:
            res_open = asyncio.run(sess.go_to("https://www.youtube.com/"))
        finally:
            desktop_control.reset_auth_context(tok2)
        self.assertIn("status=done", res_open)
        self.assertIn("hl=hi", res_open)

    def test_j12_playwright_go_to_reuses_matching_site_tab_without_new_page(self):
        yt_page = MagicMock()
        yt_page.url = "https://www.youtube.com/"
        yt_page.is_closed.return_value = False
        yt_page.title = AsyncMock(return_value="YouTube")
        yt_page.bring_to_front = AsyncMock(return_value=None)
        yt_loc = MagicMock()
        yt_loc.inner_text = AsyncMock(return_value="Home Shorts Subscriptions")
        yt_page.locator = MagicMock(return_value=yt_loc)
        yt_page.evaluate = AsyncMock(return_value=True)

        wa_page = MagicMock()
        wa_page.url = "https://web.whatsapp.com/"
        wa_page.is_closed.return_value = False
        wa_page.title = AsyncMock(return_value="WhatsApp")
        wa_page.bring_to_front = AsyncMock(return_value=None)
        wa_page.evaluate = AsyncMock(return_value=True)

        new_page_mock = AsyncMock()
        sess = browser_control._BrowserSession("chrome")
        sess._context = SimpleNamespace(pages=[yt_page, wa_page], new_page=new_page_mock)
        sess._page = wa_page

        res = asyncio.run(sess.go_to("https://www.youtube.com/"))
        new_page_mock.assert_not_called()
        yt_page.goto.assert_not_called()
        self.assertIn("status=done", res)
        self.assertIn("tab_reused=true", res)
        self.assertIn("navigation_lifecycle=SUCCESS", res)

    def test_j13_error_page_fails_and_stage_timings_recorded(self):
        fake_page = MagicMock()
        fake_page.url = "about:blank"
        fake_page.is_closed.return_value = False
        fake_page.goto = AsyncMock(side_effect=lambda u, **kw: setattr(fake_page, "url", u))
        fake_page.title = AsyncMock(return_value="This site can't be reached")
        fake_page.bring_to_front = AsyncMock(return_value=None)
        err_loc = MagicMock()
        err_loc.inner_text = AsyncMock(return_value="ERR_NAME_NOT_RESOLVED")
        fake_page.locator = MagicMock(return_value=err_loc)
        fake_page.evaluate = AsyncMock(return_value=True)

        sess = browser_control._BrowserSession("chrome")
        sess._context = SimpleNamespace(pages=[fake_page])
        sess._page = fake_page

        res = asyncio.run(sess.go_to("https://www.youtube.com/"))
        self.assertTrue(res.startswith("status=failed "))
        self.assertIn("page_state=error", res)
        self.assertEqual(browser_control.NAV_REQUESTED, "REQUESTED")
        self.assertEqual(browser_control.NAV_SUCCESS, "SUCCESS")

    def test_j14_reload_verifies_dom_and_native_window_state(self):
        fake_page = MagicMock()
        fake_page.url = "https://www.youtube.com/"
        fake_page.is_closed.return_value = False
        fake_page.reload = AsyncMock(return_value=None)
        fake_page.title = AsyncMock(return_value="YouTube")
        fake_page.evaluate = AsyncMock(return_value=True)
        loc = MagicMock()
        loc.inner_text = AsyncMock(return_value="Home Subscriptions")
        fake_page.locator = MagicMock(return_value=loc)

        sess = browser_control._BrowserSession("chrome")
        sess._context = SimpleNamespace(pages=[fake_page])
        sess._page = fake_page
        res = asyncio.run(sess.reload())
        self.assertIn("status=done", res)
        self.assertIn("foreground_verified=true", res)
        self.assertIn("target_matched=true", res)

    def test_qt_mainwindow_clean_shutdown_no_timer_or_notifier_warnings(self):
        import subprocess, sys
        repo_root = str(Path(__file__).resolve().parent.parent)
        script = (
            "import sys\n"
            "from PyQt6.QtWidgets import QApplication\n"
            "from PyQt6.QtCore import QTimer\n"
            "from gui.window import MainWindow\n"
            "app = QApplication(sys.argv)\n"
            "win = MainWindow('face.png')\n"
            "win.show()\n"
            "QTimer.singleShot(40, win.close)\n"
            "QTimer.singleShot(80, app.quit)\n"
            "app.exec()\n"
            "win.shutdown_qt_resources()\n"
            "app.processEvents()\n"
        )
        env = dict(os.environ)
        env["QT_QPA_PLATFORM"] = "offscreen"
        proc = subprocess.run(
            [sys.executable, "-c", script],
            cwd=repo_root, env=env, capture_output=True, text=True, timeout=25,
        )
        self.assertEqual(proc.returncode, 0, f"stderr={proc.stderr!r}\nstdout={proc.stdout!r}")
        for bad in (
            "QObject::killTimer",
            "Timers cannot be stopped from another thread",
            "QObject::~QObject",
            "QWinEventNotifier",
        ):
            self.assertNotIn(bad, proc.stderr, f"Unexpected Qt shutdown warning {bad!r} in stderr: {proc.stderr}")


class LiveBrowserAutomationRepairTests(unittest.TestCase):
    def setUp(self):
        desktop_control.clear_all_task_state()
        desktop_control._sent_attempt_records.clear()
        browser_control._registry.clear_site_targets()
        browser_control._registry.invalidate_sync("chrome")

    def tearDown(self):
        desktop_control.clear_all_task_state()
        desktop_control._sent_attempt_records.clear()
        browser_control._registry.clear_site_targets()
        browser_control._registry.invalidate_sync("chrome")

    def test_issue1_strip_composite_and_extract_whatsapp_contacts_handles_empty_malformed(self):
        from plugins import _whatsapp_core as _wa

        for bad in (None, "", "   \t\n  ", "\n\n", 42):
            out = _wa._strip_composite_contact_name(bad)
            self.assertIsInstance(out, str)
            if bad in (None, "", "   \t\n  ", "\n\n"):
                self.assertEqual(out, "")

        self.assertEqual(_wa._strip_composite_contact_name("Rio\n10:30 AM\nHey there"), "Rio")
        self.assertEqual(_wa._strip_composite_contact_name("Rio yesterday"), "Rio")

        # UIA + OCR fallback paths with malformed/None/empty entries alongside "Rio"
        for src in ("uia", "ocr"):
            fake_view = SimpleNamespace(
                app="chrome.exe",
                title="WhatsApp - Google Chrome",
                source=src,
                monitor={"left": 0, "top": 0, "width": 1920, "height": 1080},
                window_rect=(0, 0, 1920, 1080),
                elements=[
                    SimpleNamespace(name="", role="text", left=120, top=180, width=220, height=30),
                    SimpleNamespace(name=None, role="listitem", left=120, top=220, width=240, height=50),
                    SimpleNamespace(name="   \n  ", role="ocr_text", left=120, top=280, width=220, height=28),
                    SimpleNamespace(name="Rio\n10:30 AM", role=("ocr_text" if src == "ocr" else "listitem"),
                                    left=120, top=320, width=260, height=48, center=(240, 344)),
                ],
            )
            contacts = _wa.extract_whatsapp_contacts(fake_view, query="Rio")
            self.assertEqual(len(contacts), 1, f"source={src}: {contacts}")
            self.assertEqual(contacts[0]["display_name"], "Rio")

    def test_issue2_real_signature_scroll_integration_down_up_focus_and_clears_stale_coords(self):
        from core import screenact

        wheel_calls: list[tuple[str, int]] = []
        desktop_control.set_active_target(
            target_type="video", exact_target_name="Old Video",
            browser="chrome", tab_title="YouTube - Google Chrome",
            point=(400, 420), rect=(300, 380, 200, 80),
        )
        self.assertIsNotNone(desktop_control.get_active_target(None))

        orig_scroll = screenact.scroll

        def _real_sig_scroll(direction="down", amount=3, *, target=None):
            # Enforce exact real parameter signature and body of core.screenact.scroll
            return orig_scroll(direction, amount, target=target)

        with patch.object(browser_control, "_find_browser_window", return_value={
            "found": True, "foreground": False, "browser": "chrome",
            "app": "chrome.exe", "title": "YouTube - Google Chrome", "hwnd": 6101, "pid": 3101, "matched_site": "youtube",
        }), \
             patch.object(browser_control, "_ensure_browser_foreground", return_value=(
                 True,
                 {"found": True, "foreground": True, "browser": "chrome",
                  "app": "chrome.exe", "title": "YouTube - Google Chrome", "hwnd": 6101, "pid": 3101, "matched_site": "youtube"},
                 "",
             )) as mock_fg, \
             patch.object(screenact, "available", return_value=(True, "")), \
             patch.object(screenact, "_wheel", side_effect=lambda d, n: wheel_calls.append((d, n))), \
             patch.object(screenact, "scroll", wraps=_real_sig_scroll):
            res_down = browser_control._native_browser_action("scroll", {"direction": "down", "amount": 500}, "chrome")
            res_up = browser_control._native_browser_action("scroll", {"direction": "up", "amount": 4}, "chrome")

        mock_fg.assert_called()
        self.assertEqual(wheel_calls, [("down", 5), ("up", 4)])
        self.assertIn("status=done", res_down)
        self.assertIn("direction=down", res_down)
        self.assertIn("amount=5", res_down)
        self.assertIn("status=done", res_up)
        self.assertIn("direction=up", res_up)
        self.assertIn("amount=4", res_up)
        self.assertIsNone(desktop_control.get_active_target(None))

    def test_issue3_youtube_grid_ordering_horizontal_row_ambiguity_and_playback_check(self):
        from core import screenact, screenview

        # Boundary test: 298 vs 320 sit in Row 1 (must sort left-to-right before Row 2 at top=560)
        raw = [
            {"title": "Row2 Card1", "left": 240, "top": 560, "width": 300, "height": 40, "point": (390, 580), "rect": (240, 560, 300, 40)},
            {"title": "Row1 Card3", "left": 920, "top": 315, "width": 300, "height": 40, "point": (1070, 335), "rect": (920, 315, 300, 40)},
            {"title": "Row1 Card1", "left": 240, "top": 298, "width": 300, "height": 40, "point": (390, 318), "rect": (240, 298, 300, 40)},
            {"title": "Row1 Card2", "left": 580, "top": 320, "width": 300, "height": 40, "point": (730, 340), "rect": (580, 320, 300, 40)},
        ]
        ordered, rows, ambig, _secs = browser_control._cluster_youtube_video_rows(raw)
        self.assertFalse(ambig)
        self.assertEqual([v["title"] for v in ordered], ["Row1 Card1", "Row1 Card2", "Row1 Card3", "Row2 Card1"])
        self.assertEqual(len(rows), 2)
        self.assertEqual(ordered[2]["col_index"], 3)

        # Native "Third video horizontally" clicks Row1 Card3 and verifies Pause (k) -> playback_verified=True
        home_view = SimpleNamespace(
            app="chrome.exe", title="YouTube - Google Chrome", hwnd=7101, pid=4101, source="uia",
            monitor={"left": 0, "top": 0, "width": 1920, "height": 1080}, window_rect=(0, 0, 1920, 1080),
            elements=[
                SimpleNamespace(name="Row1 Card1 - Official Video", role="link", left=240, top=298, width=280, height=36, center=(380, 316)),
                SimpleNamespace(name="Row1 Card2 - Live Session", role="link", left=580, top=318, width=280, height=36, center=(720, 336)),
                SimpleNamespace(name="Row1 Card3 - Third Song", role="link", left=920, top=308, width=280, height=36, center=(1060, 326)),
                SimpleNamespace(name="Row2 Card1 - Next Row", role="link", left=240, top=580, width=280, height=36, center=(380, 598)),
            ],
        )
        watch_view = SimpleNamespace(
            app="chrome.exe", title="Row1 Card3 - Third Song - YouTube - Google Chrome", hwnd=7101, pid=4101, source="uia",
            monitor={"left": 0, "top": 0, "width": 1920, "height": 1080}, window_rect=(0, 0, 1920, 1080),
            elements=[
                SimpleNamespace(name="Pause (k)", role="button", left=50, top=680, width=40, height=30, center=(70, 695)),
            ],
        )
        clicked_pts: list[tuple[int, int]] = []
        with patch.object(browser_control, "_find_browser_window", return_value={
            "found": True, "foreground": True, "browser": "chrome",
            "app": "chrome.exe", "title": "YouTube - Google Chrome", "hwnd": 7101, "pid": 4101, "matched_site": "youtube",
        }), \
             patch.object(screenview, "is_own_pid", return_value=False), \
             patch.object(screenview, "look", side_effect=[home_view, watch_view]), \
             patch.object(screenact, "move_cursor", side_effect=lambda pt: screenact.Act(True, "move", point=(pt.x, pt.y))), \
             patch.object(screenact, "click", side_effect=lambda pt: (clicked_pts.append((pt.x, pt.y)), screenact.Act(True, "click", point=(pt.x, pt.y)))[1]):
            with patch.object(browser_control, "_find_browser_window", side_effect=[
                {"found": True, "foreground": True, "browser": "chrome", "app": "chrome.exe", "title": "YouTube - Google Chrome", "hwnd": 7101, "pid": 4101, "matched_site": "youtube"},
                {"found": True, "foreground": True, "browser": "chrome", "app": "chrome.exe", "title": "YouTube - Google Chrome", "hwnd": 7101, "pid": 4101, "matched_site": "youtube"},
                {"found": True, "foreground": True, "browser": "chrome", "app": "chrome.exe", "title": "Row1 Card3 - Third Song - YouTube - Google Chrome", "hwnd": 7101, "pid": 4101, "matched_site": "youtube"},
            ]):
                res = browser_control._native_browser_action("smart_click", {"description": "third video horizontally"}, "chrome")

        self.assertEqual(clicked_pts, [(1060, 326)])
        self.assertIn("status=done", res)
        self.assertIn("playback_verified=true", res)
        self.assertIn("action_state=ACTION_VERIFIED", res)

        # Genuine multi-section shelf ambiguity in Playwright DOM asks instead of guessing
        fake_page = MagicMock()
        fake_page.url = "https://www.youtube.com/"
        sess = browser_control._BrowserSession("chrome")
        sess._page = fake_page
        with patch.object(sess, "_extract_youtube_videos", AsyncMock(return_value=[
            {"id": "vid111111", "title": "A1", "channel": "C1", "rect": [100, 200, 200, 40], "section": "Breaking News"},
            {"id": "vid222222", "title": "A2", "channel": "C2", "rect": [400, 200, 200, 40], "section": "Breaking News"},
            {"id": "vid333333", "title": "B1", "channel": "C3", "rect": [100, 500, 200, 40], "section": "Trending Now"},
            {"id": "vid444444", "title": "B2", "channel": "C4", "rect": [400, 500, 200, 40], "section": "Trending Now"},
        ])):
            amb_res = asyncio.run(sess._click_youtube_video_ordinal(fake_page, 1, "second video"))
        self.assertIn("status=ask", amb_res)

    def test_issue3_smart_click_ordinal_authorization_fixes_target_not_named(self):
        for voice_req, target_desc in (
            ("Play the second video.", "second video"),
            ("Open the second video.", "2nd video"),
            ("Click on the second video.", "second thumbnail in top row"),
            ("Third video horizontally.", "third video horizontally"),
            ("Third video horizontally.", "3rd horizontal video"),
        ):
            ctx = RequestContext.create(voice_req, "voice", browser="chrome", browser_task_id="bt-ord")
            ok, reason = desktop_control.authorize_user_request(
                voice_req, "browser_control",
                {"action": "smart_click", "description": target_desc},
                source="voice", auth_context=ctx,
            )
            self.assertTrue(ok, f"Expected authorized for {voice_req!r} -> {target_desc!r}, got {reason}")

        # Wrong ordinal or unrequested target remains strictly denied
        ctx_sec = RequestContext.create("Click on the second video.", "voice", browser="chrome", browser_task_id="bt-ord2")
        ok_bad, _ = desktop_control.authorize_user_request(
            "Click on the second video.", "browser_control",
            {"action": "smart_click", "description": "Buy Now button"},
            source="voice", auth_context=ctx_sec,
        )
        self.assertFalse(ok_bad)

    def test_issue4_wrong_window_explorer_reacquisition_and_refusal(self):
        from core import appidentity, screenact, screenview

        # Record previous Chrome HWND/site target, then Explorer steals foreground
        browser_control._registry.note_site_target("chrome", "youtube", {
            "hwnd": 8844, "pid": 4422, "title": "YouTube - Google Chrome",
            "url": "https://www.youtube.com/", "foreground": True,
        })
        explorer_view = SimpleNamespace(
            app="explorer.exe", title="Program Manager", hwnd=1111, pid=2222,
            source="uia", degraded=False, reason="",
            windows=[("explorer.exe", "Program Manager"), ("chrome.exe", "YouTube - Google Chrome")],
            elements=[],
        )
        chrome_view = SimpleNamespace(
            app="chrome.exe", title="YouTube - Google Chrome", hwnd=8844, pid=4422,
            source="uia", degraded=False, reason="",
            windows=[("chrome.exe", "YouTube - Google Chrome")],
            elements=[SimpleNamespace(name="Search", role="combobox", left=400, top=120, width=400, height=34)],
        )

        # Case A: Explorer foreground -> focus_hwnd reacquires Chrome 8844 cleanly
        with patch.object(screenview, "_foreground", side_effect=[
            ("explorer.exe", "Program Manager", 2222, 1111),
            ("chrome.exe", "YouTube - Google Chrome", 4422, 8844),
        ]), \
             patch.object(screenview, "look", side_effect=[explorer_view, chrome_view]), \
             patch.object(screenview, "is_own_pid", return_value=False), \
             patch.object(appidentity, "focus_hwnd", return_value={"activated": True, "target_hwnd": 8844, "target_pid": 4422}) as mock_fh:
            obs = screen_observation.inspect_local_screen(question="Check screen", target_site="youtube")
        mock_fh.assert_called_with(8844)
        self.assertIn("target_matched=True", obs)

        # Case B: Reacquisition fails and Explorer stays foreground -> refuse scroll/click & return target_matched=False
        with patch.object(screenview, "_foreground", return_value=("explorer.exe", "Program Manager", 2222, 1111)), \
             patch.object(screenview, "look", return_value=explorer_view), \
             patch.object(screenview, "is_own_pid", return_value=False), \
             patch.object(appidentity, "focus_hwnd", return_value={"activated": False, "reason": "foreground_lock"}), \
             patch.object(appidentity, "focus_window_by_app", return_value={"activated": False, "reason": "foreground_lock"}):
            obs_fail = screen_observation.inspect_local_screen(question="Check YouTube", target_site="youtube")
            nav_fail = browser_control._native_browser_action("scroll", {"direction": "down"}, "chrome")
        self.assertIn("target_matched=False", obs_fail)
        self.assertIn("explorer.exe", obs_fail)
        self.assertTrue(nav_fail.startswith("status=failed "))
        self.assertIn("foreground_activation_failed", nav_fail)

    def test_issue5_dom_first_whatsapp_search_and_select_without_sending(self):
        fake_page = MagicMock()
        fake_page.url = "https://web.whatsapp.com/"
        fake_page.is_closed.return_value = False
        sb_loc = MagicMock()
        sb_loc.click = AsyncMock(return_value=None)
        sb_loc.fill = AsyncMock(return_value=None)
        fake_page.locator = MagicMock(return_value=SimpleNamespace(first=sb_loc))
        fake_page.mouse = MagicMock(click=AsyncMock(return_value=None))

        async def _eval_wa(script, *a):
            if "chat-list-search" in script:
                return {"found": True, "visible": True, "enabled": True, "focused": True, "value": "", "label": "Search", "rect": [50, 120, 260, 32], "x": 180, "y": 136}
            if "#pane-side" in script:
                return [{"title": "Rio\n10:30 AM", "chatId": "rio@c.us", "rect": [40, 200, 300, 60], "point": [180, 230]}]
            if "document.querySelector('#main')" in script:
                return {"found": True, "header": "Rio", "composer": True, "focused": False, "text": "", "url": "https://web.whatsapp.com/"}
            return {}

        fake_page.evaluate = AsyncMock(side_effect=_eval_wa)
        sess = browser_control._BrowserSession("chrome")
        sess._context = SimpleNamespace(pages=[fake_page])
        sess._page = fake_page

        res_search = asyncio.run(sess.search_whatsapp_contact("Rio"))
        self.assertIn("status=done", res_search)
        self.assertIn("matched_contacts=Rio", res_search)
        self.assertIn("search_source=dom", res_search)

        res_select = asyncio.run(sess.select_whatsapp_contact("Rio"))
        self.assertIn("status=done", res_select)
        self.assertIn("active_contact=Rio", res_select)
        self.assertIn("selected_chat_verified=true", res_select)
        self.assertIn("message_sent=false", res_select)


class CategoryChipsAndLiveAcceptanceRepairTests(unittest.TestCase):
    def setUp(self):
        desktop_control.clear_all_task_state()
        desktop_control._sent_attempt_records.clear()
        browser_control._registry.clear_site_targets()
        browser_control._registry.invalidate_sync("chrome")

    def tearDown(self):
        desktop_control.clear_all_task_state()
        desktop_control._sent_attempt_records.clear()
        browser_control._registry.clear_site_targets()
        browser_control._registry.invalidate_sync("chrome")

    def _yt_chip_and_grid_view(self):
        # Exact reproduction of YouTube Home screenshot layout:
        # Row 0 (y=165): All, Music, Mixes, Gaming, Grandmaster chess title, The Avengers, Science fiction
        # Row 1 (y=340): First Video Alpha, Second Video Beta, Third Video Gamma
        return SimpleNamespace(
            app="chrome.exe",
            title="YouTube - Google Chrome",
            hwnd=9101,
            pid=5101,
            source="ocr",
            monitor={"left": 0, "top": 0, "width": 1920, "height": 1080},
            window_rect=(0, 0, 1920, 1080),
            elements=[
                SimpleNamespace(name="All", role="ocr_text", left=240, top=165, width=48, height=32, center=(264, 181)),
                SimpleNamespace(name="Music", role="ocr_text", left=305, top=166, width=72, height=32, center=(341, 182)),
                SimpleNamespace(name="Mixes", role="ocr_text", left=390, top=165, width=74, height=32, center=(427, 181)),
                SimpleNamespace(name="Gaming", role="ocr_text", left=480, top=166, width=88, height=32, center=(524, 182)),
                SimpleNamespace(name="Grandmaster chess title", role="ocr_text", left=585, top=165, width=190, height=32, center=(680, 181)),
                SimpleNamespace(name="The Avengers", role="ocr_text", left=790, top=166, width=130, height=32, center=(855, 182)),
                SimpleNamespace(name="Science fiction", role="ocr_text", left=935, top=165, width=145, height=32, center=(1007, 181)),
                SimpleNamespace(name="First Video Alpha", role="ocr_text", left=240, top=340, width=320, height=36, center=(400, 358)),
                SimpleNamespace(name="Second Video Beta", role="ocr_text", left=620, top=345, width=320, height=36, center=(780, 363)),
                SimpleNamespace(name="Third Video Gamma", role="ocr_text", left=1000, top=342, width=320, height=36, center=(1160, 360)),
            ],
        )

    def test_g1_g2_g3_g5_category_chips_never_enter_video_ordinals(self):
        from core import screenact, screenview

        view = self._yt_chip_and_grid_view()
        cls = browser_control.classify_youtube_elements(view)
        chip_labels = [c["label"] for c in cls[browser_control.ELEM_CATEGORY_CHIP]]
        video_titles = [v["title"] for v in cls[browser_control.ELEM_VIDEO_CARD]]

        self.assertIn("All", chip_labels)
        self.assertIn("Science fiction", chip_labels)
        self.assertIn("The Avengers", chip_labels)
        self.assertEqual(video_titles, ["First Video Alpha", "Second Video Beta", "Third Video Gamma"])
        self.assertNotIn("Science fiction", video_titles)

        # Clicking "second video horizontally" and "third video" picks VIDEO_CARD #2 and #3, never "Science fiction"
        for desc, expected_pt, expected_title, watch_title in (
            ("second video horizontally", (780, 363), "Second Video Beta", "Second Video Beta - YouTube - Google Chrome"),
            ("third video", (1160, 360), "Third Video Gamma", "Third Video Gamma - YouTube - Google Chrome"),
        ):
            clicks = []
            watch_view = SimpleNamespace(
                app="chrome.exe", title=watch_title, hwnd=9101, pid=5101, source="uia",
                monitor={"left": 0, "top": 0, "width": 1920, "height": 1080}, window_rect=(0, 0, 1920, 1080),
                elements=[SimpleNamespace(name="Pause (k)", role="button", left=50, top=680, width=40, height=30, center=(70, 695))],
            )
            with patch.object(browser_control, "_find_browser_window", side_effect=[
                {"found": True, "foreground": True, "browser": "chrome", "app": "chrome.exe", "title": "YouTube - Google Chrome", "hwnd": 9101, "pid": 5101, "matched_site": "youtube"},
                {"found": True, "foreground": True, "browser": "chrome", "app": "chrome.exe", "title": "YouTube - Google Chrome", "hwnd": 9101, "pid": 5101, "matched_site": "youtube"},
                {"found": True, "foreground": True, "browser": "chrome", "app": "chrome.exe", "title": watch_title, "hwnd": 9101, "pid": 5101, "matched_site": "youtube"},
            ]), \
                 patch.object(screenview, "is_own_pid", return_value=False), \
                 patch.object(screenview, "look", side_effect=[view, watch_view]), \
                 patch.object(screenact, "move_cursor", side_effect=lambda pt: screenact.Act(True, "move", point=(pt.x, pt.y))), \
                 patch.object(screenact, "click", side_effect=lambda pt: (clicks.append((pt.x, pt.y)), screenact.Act(True, "click", point=(pt.x, pt.y)))[1]):
                res = browser_control._native_browser_action("smart_click", {"description": desc}, "chrome")
            self.assertEqual(clicks, [expected_pt])
            self.assertIn(expected_title, res)
            self.assertNotIn("Science fiction", res)

    def test_g4_click_all_resolves_to_category_chip_and_verifies(self):
        from core import screenact, screenview

        ctx = RequestContext.create("Click on the All.", "voice", browser="chrome", browser_task_id="bt-all")
        self.assertTrue(desktop_control.authorize_user_request(
            "Click on the All.", "browser_control", {"action": "smart_click", "description": "All category chip"},
            source="voice", auth_context=ctx,
        )[0])

        view_before = self._yt_chip_and_grid_view()
        view_after = SimpleNamespace(
            app="chrome.exe", title="YouTube - Google Chrome", hwnd=9101, pid=5101, source="ocr",
            monitor={"left": 0, "top": 0, "width": 1920, "height": 1080}, window_rect=(0, 0, 1920, 1080),
            elements=[
                SimpleNamespace(name="All", role="tab", left=240, top=165, width=48, height=32, center=(264, 181), selected=True),
                SimpleNamespace(name="Fresh All Video", role="ocr_text", left=240, top=340, width=320, height=36, center=(400, 358)),
            ],
        )
        clicks = []
        with patch.object(browser_control, "_find_browser_window", return_value={
            "found": True, "foreground": True, "browser": "chrome", "app": "chrome.exe",
            "title": "YouTube - Google Chrome", "hwnd": 9101, "pid": 5101, "matched_site": "youtube",
        }), \
             patch.object(screenview, "is_own_pid", return_value=False), \
             patch.object(screenview, "look", side_effect=[view_before, view_after]), \
             patch.object(screenact, "move_cursor", side_effect=lambda pt: screenact.Act(True, "move", point=(pt.x, pt.y))), \
             patch.object(screenact, "click", side_effect=lambda pt: (clicks.append((pt.x, pt.y)), screenact.Act(True, "click", point=(pt.x, pt.y)))[1]):
            res = browser_control._native_browser_action("smart_click", {"description": "All"}, "chrome")
        self.assertEqual(clicks, [(264, 181)])
        self.assertIn("status=done", res)
        self.assertIn("target_class=CATEGORY_CHIP", res)
        self.assertIn("chip_selected=true", res)

    def test_g6_g7_whatsapp_search_authorizes_workflow_but_blocks_send(self):
        from main import RemLive

        rem = RemLive.__new__(RemLive)
        rem._request_context_lock = threading.Lock()
        rem._request_context = None
        rem._last_user_command = ""
        rem._last_user_command_source = "untrusted"
        rem._active_browser_task = None
        rem._last_assistant_question = ""
        rem._current_voice_turn_task_id = ""

        rem._record_user_request("Open WhatsApp in Chrome browser.", "voice")
        rem._record_user_request("Now open the chat with Rio.", "voice")
        ctx = rem._current_request_context()

        for tool, args in (
            ("browser_control", {"action": "smart_click", "description": "Search or start new chat"}),
            ("browser_control", {"action": "smart_type", "description": "WhatsApp search field", "text": "Rio"}),
            ("browser_control", {"action": "press", "key": "Enter"}),
            ("browser_control", {"action": "search_contact", "query": "Rio"}),
            ("browser_control", {"action": "select_contact", "description": "Rio"}),
        ):
            ok, reason = desktop_control.authorize_user_request(
                ctx.instruction, tool, args, source="voice", auth_context=ctx,
            )
            self.assertTrue(ok, f"{args} should authorize during WhatsApp search, got: {reason}")

        # Must NEVER authorize sending a message during contact search
        ok_send, _ = desktop_control.authorize_user_request(
            ctx.instruction, "send_message",
            {"platform": "whatsapp", "receiver": "Rio", "message_text": "hello"},
            source="voice", auth_context=ctx,
        )
        self.assertFalse(ok_send)

    def test_g8_unclear_destination_never_inherits_previous_youtube_tab(self):
        from main import RemLive

        rem = RemLive.__new__(RemLive)
        rem._request_context_lock = threading.Lock()
        rem._request_context = None
        rem._last_user_command = ""
        rem._last_user_command_source = "untrusted"
        rem._active_browser_task = None
        rem._last_assistant_question = ""
        rem._current_voice_turn_task_id = ""

        rem._record_user_request("Open YouTube in Chrome.", "voice")
        rem._record_user_request("Open the Euro Win Chrome browser now.", "voice")
        ctx = rem._current_request_context()

        ok_yt, _ = desktop_control.authorize_user_request(
            ctx.instruction, "browser_control",
            {"action": "go_to", "url": "https://www.youtube.com/"},
            source="voice", auth_context=ctx,
        )
        self.assertFalse(ok_yt)

        tok = desktop_control.bind_auth_context(ctx)
        try:
            with patch.object(browser_control._registry, "has", return_value=False):
                out = browser_control._browser_control_impl({"action": "go_to", "url": "", "browser": "chrome"})
        finally:
            desktop_control.reset_auth_context(tok)
        self.assertIn("status=ask", out)

    def test_g9_g10_failed_or_unverified_click_flushes_stale_coords_and_requires_evidence(self):
        from core import screenact, screenview

        desktop_control.set_active_target(
            target_type="video", exact_target_name="Stale Button",
            browser="chrome", tab_title="YouTube - Google Chrome",
            point=(500, 400), rect=(450, 380, 100, 40),
        )
        static_view = SimpleNamespace(
            app="chrome.exe", title="YouTube - Google Chrome", hwnd=9101, pid=5101, source="uia",
            monitor={"left": 0, "top": 0, "width": 1920, "height": 1080}, window_rect=(0, 0, 1920, 1080),
            elements=[SimpleNamespace(name="Settings Menu", role="button", left=500, top=400, width=100, height=40, center=(550, 420))],
        )
        # Click succeeds at OS level, but post-click screen state is completely unchanged -> must be unverified + flush target
        with patch.object(browser_control, "_find_browser_window", return_value={
            "found": True, "foreground": True, "browser": "chrome", "app": "chrome.exe",
            "title": "YouTube - Google Chrome", "hwnd": 9101, "pid": 5101,
        }), \
             patch.object(screenview, "is_own_pid", return_value=False), \
             patch.object(screenview, "look", return_value=static_view), \
             patch.object(screenact, "move_cursor", return_value=screenact.Act(True, "move", point=(550, 420))), \
             patch.object(screenact, "click", return_value=screenact.Act(True, "click", point=(550, 420))):
            res = browser_control._native_browser_action("smart_click", {"description": "Settings Menu"}, "chrome")

        self.assertTrue(res.startswith("status=unverified "))
        self.assertIn("action_state=ACTION_UNVERIFIED", res)
        self.assertIsNone(desktop_control.get_active_target(None))


class EvidenceProvenanceAndTitleIntegrityTests(unittest.TestCase):
    """Section 7 regressions for video-card provenance, OCR conflict safety, and WhatsApp intent search."""

    def setUp(self):
        desktop_control.clear_all_task_state()
        desktop_control._sent_attempt_records.clear()
        browser_control._registry.clear_site_targets()
        browser_control._registry.invalidate_sync("chrome")
        screen_observation._PROVENANCE_HISTORY.clear()

    def tearDown(self):
        desktop_control.clear_all_task_state()
        desktop_control._sent_attempt_records.clear()
        browser_control._registry.clear_site_targets()
        browser_control._registry.invalidate_sync("chrome")
        screen_observation._PROVENANCE_HISTORY.clear()

    def test_01_youtube_dom_card_extraction_preserves_structured_provenance(self):
        class FakeLocator:
            def __init__(self, rows):
                self._rows = rows

            async def evaluate_all(self, _js):
                return [dict(r) for r in self._rows]

        dom_rows = [
            {
                "position": 1, "id": "vid000001", "url": "https://www.youtube.com/watch?v=vid000001",
                "title": "Video One", "exact_title": "Video One", "channel": "Ch1",
                "rect": [40, 220, 300, 40], "card_bbox": [30, 120, 320, 240],
                "title_bbox": [40, 220, 300, 40], "thumbnail_bbox": [30, 120, 320, 180],
                "section": "", "is_ad": False, "is_playable": True,
                "extraction_source": "DOM", "association_confidence": 0.98,
                "card_ordering": "row_major", "evidence_status": "OBSERVED",
            },
            {
                "position": 2, "id": "vid000002", "url": "https://www.youtube.com/watch?v=vid000002",
                "title": "Video Two", "exact_title": "Video Two", "channel": "Ch2",
                "rect": [400, 220, 300, 40], "card_bbox": [390, 120, 320, 240],
                "title_bbox": [400, 220, 300, 40], "thumbnail_bbox": [390, 120, 320, 180],
                "section": "", "is_ad": False, "is_playable": True,
                "extraction_source": "DOM", "association_confidence": 0.98,
                "card_ordering": "row_major", "evidence_status": "OBSERVED",
            },
        ]

        class FakePage:
            url = "https://www.youtube.com/results?search_query=test"

            async def title(self):
                return "test - YouTube"

            def locator(self, _sel):
                return FakeLocator(dom_rows)

        sess = browser_control._BrowserSession("chrome")
        import asyncio
        cards = asyncio.run(sess._extract_youtube_videos(FakePage()))
        self.assertEqual(len(cards), 2)
        for c in cards:
            self.assertEqual(c["extraction_source"], "DOM")
            self.assertEqual(c["evidence_status"], "OBSERVED")
            self.assertTrue(c.get("observation_id", "").startswith("obs_"))
            self.assertIsNotNone(c["thumbnail_bbox"])
            self.assertIsNotNone(c["card_bbox"])
            self.assertIsNotNone(c["title_bbox"])
        prov = screen_observation.get_latest_observation_provenance(url_contains="youtube.com")
        self.assertIsNotNone(prov)
        self.assertEqual(len(prov["targets"]), 2)

    def test_02_youtube_ocr_bounding_box_card_association(self):
        view = SimpleNamespace(
            app="chrome.exe", title="YouTube - Google Chrome", hwnd=9301, pid=4301, source="ocr",
            monitor={"left": 0, "top": 0, "width": 1920, "height": 1080}, window_rect=(0, 0, 1920, 1080),
            elements=[
                SimpleNamespace(name="Clean Robotics Walkthrough", role="ocr_text", left=260, top=340, width=300, height=28, center=(410, 354)),
                SimpleNamespace(name="Python Asyncio Deep Dive", role="ocr_text", left=640, top=340, width=300, height=28, center=(790, 354)),
            ],
        )
        cls = browser_control.classify_youtube_elements(view)
        cards = cls[browser_control.ELEM_VIDEO_CARD]
        self.assertEqual(len(cards), 2)
        self.assertEqual(cards[0]["exact_title"], "Clean Robotics Walkthrough")
        self.assertEqual(cards[0]["extraction_source"], "OCR")
        self.assertEqual(cards[0]["evidence_status"], "ASSOCIATED")
        self.assertEqual(cards[0]["title_bbox"], [260, 340, 300, 28])

    def test_03_conflicting_ocr_fragments_never_merged_and_block_fabrication(self):
        from core import screenview
        # Card 1 & Card 2 clean; Card 3 has conflicting Gojo / Myra fragments in the same card slot
        view = SimpleNamespace(
            app="chrome.exe", title="YouTube - Google Chrome", hwnd=9302, pid=4302, source="ocr",
            monitor={"left": 0, "top": 0, "width": 1920, "height": 1080}, window_rect=(0, 0, 1920, 1080),
            elements=[
                SimpleNamespace(name="First Verified Tutorial", role="ocr_text", left=260, top=340, width=280, height=28, center=(400, 354)),
                SimpleNamespace(name="Second Verified Tutorial", role="ocr_text", left=620, top=340, width=280, height=28, center=(760, 354)),
                SimpleNamespace(name="Satoru Gojo vs Toji Fushiguro", role="ocr_text", left=980, top=330, width=280, height=28, center=(1120, 344)),
                SimpleNamespace(name="MYRA AI Got Angry and Started Crying!", role="ocr_text", left=985, top=390, width=290, height=28, center=(1130, 404)),
            ],
        )
        cls = browser_control.classify_youtube_elements(view)
        cards = cls[browser_control.ELEM_VIDEO_CARD]
        self.assertEqual(len(cards), 3)
        third = cards[2]
        self.assertEqual(third["evidence_status"], "UNKNOWN")
        self.assertEqual(third["exact_title"], "")
        self.assertGreaterEqual(len(third["conflicting_titles"]), 2)

        with patch.object(browser_control, "_find_browser_window", return_value={
            "found": True, "foreground": True, "browser": "chrome", "app": "chrome.exe",
            "title": "YouTube - Google Chrome", "hwnd": 9302, "pid": 4302,
        }), \
             patch.object(screenview, "is_own_pid", return_value=False), \
             patch.object(screenview, "look", return_value=view):
            out = browser_control._native_browser_action("play_video", {"description": "third video"}, "chrome")
        self.assertTrue(out.startswith("status=unverified "))
        self.assertIn("evidence_status=UNKNOWN", out)
        self.assertIn("cannot reliably read the third video's title", out)

    def test_04_ordinal_selection_uses_verified_third_card_and_reports_exact_title(self):
        from core import screenact, screenview
        pre_view = SimpleNamespace(
            app="chrome.exe", title="YouTube - Google Chrome", hwnd=9303, pid=4303, source="uia",
            monitor={"left": 0, "top": 0, "width": 1920, "height": 1080}, window_rect=(0, 0, 1920, 1080),
            elements=[
                SimpleNamespace(name="Video Alpha One", role="link", left=260, top=340, width=260, height=30, center=(390, 355)),
                SimpleNamespace(name="Video Beta Two", role="link", left=620, top=340, width=260, height=30, center=(750, 355)),
                SimpleNamespace(name="Video Gamma Three", role="link", left=980, top=340, width=260, height=30, center=(1110, 355)),
            ],
        )
        post_view = SimpleNamespace(
            app="chrome.exe", title="Video Gamma Three - YouTube - Google Chrome", hwnd=9303, pid=4303, source="uia",
            monitor={"left": 0, "top": 0, "width": 1920, "height": 1080}, window_rect=(0, 0, 1920, 1080),
            elements=[SimpleNamespace(name="Pause (k)", role="button", left=100, top=700, width=40, height=40, center=(120, 720))],
        )
        seq_wins = [
            {"found": True, "foreground": True, "browser": "chrome", "app": "chrome.exe", "title": "YouTube - Google Chrome", "hwnd": 9303, "pid": 4303},
            {"found": True, "foreground": True, "browser": "chrome", "app": "chrome.exe", "title": "YouTube - Google Chrome", "hwnd": 9303, "pid": 4303},
            {"found": True, "foreground": True, "browser": "chrome", "app": "chrome.exe", "title": "Video Gamma Three - YouTube - Google Chrome", "hwnd": 9303, "pid": 4303},
            {"found": True, "foreground": True, "browser": "chrome", "app": "chrome.exe", "title": "Video Gamma Three - YouTube - Google Chrome", "hwnd": 9303, "pid": 4303},
        ]
        with patch.object(browser_control, "_find_browser_window", side_effect=seq_wins), \
             patch.object(screenview, "is_own_pid", return_value=False), \
             patch.object(screenview, "look", side_effect=[pre_view, post_view]), \
             patch.object(screenact, "move_cursor", return_value=screenact.Act(True, "move", point=(1110, 355))), \
             patch.object(screenact, "click", return_value=screenact.Act(True, "click", point=(1110, 355))):
            out = browser_control._native_browser_action("play_video", {"description": "third video"}, "chrome")
        self.assertTrue(out.startswith("status=done "))
        self.assertIn('title="Video Gamma Three"', out)
        self.assertIn("playback_verified=true", out)

    def test_05_stale_observations_invalidated_after_scroll_and_failed_click(self):
        from core import screenact
        rec = screen_observation.record_observation_provenance(
            hwnd=9304, pid=4304, active_app="chrome.exe", active_url="https://www.youtube.com/",
            tab_identity="YouTube - Google Chrome", source="UIA", confidence="high", stale=False,
        )
        self.assertIsNotNone(screen_observation.get_latest_observation_provenance(hwnd=9304))
        with patch.object(browser_control, "_find_browser_window", return_value={
            "found": True, "foreground": True, "browser": "chrome", "app": "chrome.exe",
            "title": "YouTube - Google Chrome", "hwnd": 9304, "pid": 4304,
        }), \
             patch.object(screenact, "scroll", return_value=screenact.Act(True, "scroll")):
            browser_control._native_browser_action("scroll", {"direction": "down", "amount": 4}, "chrome")
        self.assertIsNone(screen_observation.get_latest_observation_provenance(hwnd=9304))
        marked = screen_observation.get_observation_provenance(rec["observation_id"])
        self.assertTrue(marked["stale"])
        self.assertEqual(marked["stale_reason"], "scroll")

    def test_06_whatsapp_intent_phrase_parsed_and_opens_contact_via_sidebar_search(self):
        from core import screenact, screenview
        parsed = _whatsapp_core.parse_whatsapp_intent_target("Chat named Rio")
        self.assertTrue(parsed["is_intent"])
        self.assertEqual(parsed["contact"], "Rio")
        parsed_asr = _whatsapp_core.parse_whatsapp_intent_target("Now open Shed with the Rio.")
        self.assertTrue(parsed_asr["is_intent"])
        self.assertEqual(parsed_asr["contact"], "Rio")

        # Initial WhatsApp screen has sidebar search box, NO Rio visible yet
        initial_view = SimpleNamespace(
            app="chrome.exe", title="WhatsApp - Google Chrome", hwnd=9401, pid=4401, source="uia",
            monitor={"left": 0, "top": 0, "width": 1920, "height": 1080}, window_rect=(0, 0, 1920, 1080),
            elements=[
                SimpleNamespace(name="Search input textbox", role="textbox", left=90, top=140, width=260, height=34, center=(220, 157), enabled=True, automation_id="chat-list-search"),
                SimpleNamespace(name="Alice", role="listitem", left=90, top=240, width=260, height=60, center=(220, 270)),
            ],
        )
        # After sidebar search types "Rio", Rio appears in sidebar results
        searched_view = SimpleNamespace(
            app="chrome.exe", title="WhatsApp - Google Chrome", hwnd=9401, pid=4401, source="uia",
            monitor={"left": 0, "top": 0, "width": 1920, "height": 1080}, window_rect=(0, 0, 1920, 1080),
            elements=[
                SimpleNamespace(name="Search input textbox", role="textbox", value="Rio", left=90, top=140, width=260, height=34, center=(220, 157), enabled=True),
                SimpleNamespace(name="Rio", role="listitem", left=90, top=240, width=260, height=60, center=(220, 270)),
            ],
        )
        # After clicking Rio, chat header confirms Rio is open
        opened_view = SimpleNamespace(
            app="chrome.exe", title="WhatsApp - Google Chrome", hwnd=9401, pid=4401, source="uia",
            monitor={"left": 0, "top": 0, "width": 1920, "height": 1080}, window_rect=(0, 0, 1920, 1080),
            elements=[
                SimpleNamespace(name="Rio", role="heading", left=560, top=92, width=160, height=28, center=(640, 106)),
                SimpleNamespace(name="Type a message", role="textbox", left=620, top=990, width=700, height=40, center=(970, 1010)),
            ],
        )
        wa_win = {"found": True, "foreground": True, "browser": "chrome", "app": "chrome.exe", "title": "WhatsApp - Google Chrome", "hwnd": 9401, "pid": 4401}
        typed_texts: list[str] = []
        clicked_labels: list[str] = []

        def fake_type(txt, clear_first=False):
            typed_texts.append(txt)
            return screenact.Act(True, "type")

        def fake_click(pt):
            clicked_labels.append(getattr(pt, "label", ""))
            return screenact.Act(True, "click", point=(pt.x, pt.y))

        with patch.object(browser_control, "_find_browser_window", return_value=wa_win), \
             patch.object(screenview, "is_own_pid", return_value=False), \
             patch.object(screenview, "look", side_effect=[initial_view, searched_view, opened_view]), \
             patch.object(screenact, "move_cursor", return_value=screenact.Act(True, "move", point=(220, 270))), \
             patch.object(screenact, "click", side_effect=fake_click), \
             patch.object(screenact, "type_text", side_effect=fake_type):
            out = browser_control._native_browser_action("smart_click", {"description": "Chat named Rio"}, "chrome")

        self.assertTrue(out.startswith("status=done "), out)
        self.assertIn("selected_chat_verified=true", out)
        self.assertEqual(typed_texts, ["Rio"])
        self.assertIn("Rio", clicked_labels)

    def test_07_whatsapp_sidebar_search_authorized_while_composer_and_send_remain_protected(self):
        ctx = RequestContext.create(
            "Open the chat with Rio", "voice", browser_task_id="wa-auth-1"
        )
        for tool, args in (
            ("browser_control", {"action": "smart_click", "description": "WhatsApp sidebar search box"}),
            ("browser_control", {"action": "smart_type", "description": "WhatsApp sidebar search box", "text": "Rio"}),
            ("browser_control", {"action": "smart_click", "description": "Chat named Rio"}),
        ):
            ok, reason = desktop_control.authorize_user_request(
                ctx.instruction, tool, args, source="voice", auth_context=ctx,
            )
            self.assertTrue(ok, f"Expected allow for {args}, got deny={reason}")

        # Composer typing and Send/Call MUST remain strictly denied during chat-open request!
        for tool, args in (
            ("browser_control", {"action": "smart_type", "description": "Type a message", "text": "Rio"}),
            ("whatsapp", {"action": "send_message", "contact": "Rio", "message": "Hi"}),
            ("whatsapp", {"action": "call", "contact": "Rio"}),
        ):
            ok, reason = desktop_control.authorize_user_request(
                ctx.instruction, tool, args, source="voice", auth_context=ctx,
            )
            self.assertFalse(ok, f"Expected deny for protected {tool}:{args}, got allow ({reason})")

    def test_08_whatsapp_contact_workflow_never_touches_composer_or_presses_enter(self):
        from core import screenact, screenview
        initial_view = SimpleNamespace(
            app="chrome.exe", title="WhatsApp - Google Chrome", hwnd=9402, pid=4402, source="uia",
            monitor={"left": 0, "top": 0, "width": 1920, "height": 1080}, window_rect=(0, 0, 1920, 1080),
            elements=[
                SimpleNamespace(name="Search input textbox", role="textbox", left=90, top=140, width=260, height=34, center=(220, 157), enabled=True),
                SimpleNamespace(name="Type a message", role="textbox", left=620, top=990, width=700, height=40, center=(970, 1010)),
            ],
        )
        empty_search_view = SimpleNamespace(
            app="chrome.exe", title="WhatsApp - Google Chrome", hwnd=9402, pid=4402, source="uia",
            monitor={"left": 0, "top": 0, "width": 1920, "height": 1080}, window_rect=(0, 0, 1920, 1080),
            elements=[
                SimpleNamespace(name="Search input textbox", role="textbox", value="GhostUser", left=90, top=140, width=260, height=34, center=(220, 157), enabled=True),
            ],
        )
        wa_win = {"found": True, "foreground": True, "browser": "chrome", "app": "chrome.exe", "title": "WhatsApp - Google Chrome", "hwnd": 9402, "pid": 4402}
        with patch.object(browser_control, "_find_browser_window", return_value=wa_win), \
             patch.object(screenview, "is_own_pid", return_value=False), \
             patch.object(screenview, "look", side_effect=[initial_view, empty_search_view]), \
             patch.object(screenact, "move_cursor", return_value=screenact.Act(True, "move", point=(220, 157))), \
             patch.object(screenact, "click", return_value=screenact.Act(True, "click", point=(220, 157))), \
             patch.object(screenact, "type_text", return_value=screenact.Act(True, "type")), \
             patch.object(screenact, "press") as mock_press:
            out = browser_control._native_browser_action("smart_click", {"description": "Chat named GhostUser"}, "chrome")
        self.assertTrue(out.startswith("status=unverified "), out)
        self.assertIn("selected_chat_verified=false", out)
        mock_press.assert_not_called()

    def test_09_provenance_question_reports_recorded_facts_without_fabrication(self):
        rec = screen_observation.record_observation_provenance(
            hwnd=9501, pid=4501, active_app="chrome.exe",
            active_url="https://www.youtube.com/results?search_query=gojo",
            tab_identity="gojo - YouTube - Google Chrome", source="DOM", confidence="high",
            targets=[{
                "exact_title": "Satoru Gojo vs Toji Fushiguro",
                "extraction_source": "DOM",
                "evidence_status": "OBSERVED",
                "card_bbox": [900, 200, 320, 240],
                "title_bbox": [900, 390, 320, 36],
                "thumbnail_bbox": [900, 200, 320, 180],
                "association_confidence": 0.98,
            }],
        )
        ans = screen_observation.explain_title_provenance("Satoru Gojo vs Toji Fushiguro")
        self.assertIn(rec["observation_id"], ans)
        self.assertIn("extraction_source=DOM", ans)
        self.assertIn("evidence_status=OBSERVED", ans)
        missing = screen_observation.explain_title_provenance("Nonexistent Fabricated Video")
        self.assertIn("not verified in recorded provenance", missing)

    def test_10_spoken_response_guidance_forbids_title_invention_and_unverified_playback_claims(self):
        g_unknown = conversation.tool_result_guidance(
            "status=unverified source=browser_control evidence_status=UNKNOWN "
            "I can see the video cards, but I cannot reliably read the third video's title."
        )
        self.assertIn("never guess or invent a title", g_unknown)
        self.assertIn("cannot reliably read the requested video's title", g_unknown)

        g_no_play = conversation.tool_result_guidance(
            "status=done source=browser_control title='Exact Video' playback_verified=false"
        )
        self.assertIn("do NOT say 'I'm playing it'", g_no_play)


if __name__ == "__main__":
    unittest.main()
