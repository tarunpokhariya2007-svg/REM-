import unittest
from unittest.mock import patch
from types import SimpleNamespace


class VisualTargetingExtensionTests(unittest.TestCase):
    def test_semantic_mouse_actions_are_exposed_by_plugin_schema(self):
        from actions.screen_agent import TOOL
        from plugins import screen_agent
        action_text = TOOL["parameters"]["properties"]["action"]["description"]
        plugin_text = screen_agent.PLUGIN["parameters"]["properties"]["action"]["description"]
        for name in ("hover", "move", "move_relative", "right_click", "double_click"):
            self.assertIn(name, action_text)
            self.assertIn(name, plugin_text)
            self.assertIn(name, screen_agent._DESKTOP_ACTIONS)
        for name in ("select", "toggle", "expand", "collapse"):
            self.assertIn(name, action_text)
            self.assertIn(name, plugin_text)
            self.assertIn(name, screen_agent._DESKTOP_ACTIONS)

    def test_observe_is_read_only(self):
        from actions import screen_agent
        from core.screenview import View
        with patch.object(screen_agent, "_view", return_value=View(
                source="uia", degraded=False, app="notepad", title="Untitled")), \
             patch("core.screenact.click") as click, \
             patch("core.screenact.type_text") as type_text, \
             patch("core.screenact.press") as press:
            result = screen_agent.screen_agent({"action": "observe"})
        self.assertIn("notepad", result)
        click.assert_not_called()
        type_text.assert_not_called()
        press.assert_not_called()

    def test_named_cursor_move_resolves_fresh_semantic_element(self):
        from actions import screen_agent
        from core.screenview import Element, View, ROLE_BUTTON
        view = View(source="uia", degraded=False, app="notepad", title="Test",
                    elements=[Element(1, ROLE_BUTTON, "Save", "", 10, 20, 80, 30)])
        act = SimpleNamespace(ok=True, action="move", target="Save", error="")
        with patch.object(screen_agent, "_view", return_value=view), \
             patch("core.screenact.move_cursor", return_value=act) as move, \
             patch("core.screenact.click") as click:
            result = screen_agent.screen_agent({"action": "move", "target": "Save"})
        self.assertIn("move", result)
        move.assert_called_once_with(view.elements[0])
        click.assert_not_called()

    def test_mouse_intent_authorization_distinguishes_right_and_double_click(self):
        from core.desktop_control import authorize_user_request
        self.assertFalse(authorize_user_request(
            "Click the folder", "desktop_ui",
            {"action": "right_click", "target": "folder"}, source="voice")[0])
        self.assertTrue(authorize_user_request(
            "Right-click the folder", "desktop_ui",
            {"action": "right_click", "target": "folder"}, source="voice")[0])
        self.assertFalse(authorize_user_request(
            "Click the icon", "desktop_ui",
            {"action": "double_click", "target": "icon"}, source="voice")[0])

    def test_confirmed_right_click_keeps_the_requested_action(self):
        from actions import screen_agent
        from core.screenview import Element, View, ROLE_BUTTON
        view = View(source="uia", degraded=False, app="explorer", title="Files",
                    elements=[Element(1, ROLE_BUTTON, "Delete", "", 10, 10, 40, 20)])
        act = SimpleNamespace(ok=True, action="right_click", error="")
        with patch.object(screen_agent, "_view", return_value=view), \
             patch.object(screen_agent.time, "sleep"), \
             patch("core.screenact.right_click", return_value=act) as right_click, \
             patch("core.screenact.click") as left_click:
            screen_agent._confirmed_click("Delete", "explorer", "Files",
                                          click_action="right_click")
        right_click.assert_called_once()
        left_click.assert_not_called()

    def test_current_rapidocr_result_maps_text_and_box_to_screenview_element(self):
        import numpy as np
        from core import screenview
        output = SimpleNamespace(
            boxes=np.array([[[10, 12], [90, 12], [90, 32], [10, 32]]]),
            txts=("Module not found",), scores=(0.98,),
        )
        image = np.zeros((100, 120, 3), dtype=np.uint8)
        with patch.object(screenview, "_HAS_MSS", True), \
             patch.object(screenview, "_HAS_PIL", True), \
             patch.object(screenview, "capture", return_value=(image, {"left": 50, "top": 70}, 1.0)), \
             patch.object(screenview, "_ocr_reader", return_value=lambda _image: output):
            elements = screenview._scan_ocr({"left": 50, "top": 70}, float("inf"))
        self.assertEqual(len(elements), 1)
        self.assertEqual(elements[0].name, "Module not found")
        self.assertEqual(elements[0].rect, (60, 82, 80, 20))

    def test_drag_releases_button_when_emergency_stop_arrives_mid_path(self):
        from core import screenact
        with patch.object(screenact, "available", side_effect=[
                (True, ""), (True, ""), (True, ""), (False, "computer control is paused or emergency-stopped")]), \
             patch.object(screenact, "_on_screen", return_value=True), \
             patch.object(screenact, "_lands_on_self", return_value=False), \
             patch.object(screenact.pyautogui, "moveTo"), \
             patch.object(screenact.pyautogui, "mouseDown") as mouse_down, \
             patch.object(screenact.pyautogui, "mouseUp") as mouse_up, \
             patch.object(screenact.time, "sleep"):
            result = screenact.drag(screenact.Point(100, 100),
                                    screenact.Point(200, 200), duration=0.4)
        self.assertFalse(result.ok)
        self.assertIn("cancelled safely", result.error)
        mouse_down.assert_called_once_with(button="left")
        mouse_up.assert_called_once_with(button="left")


class UiaSemanticActionTests(unittest.TestCase):
    def test_sensitive_windows_are_always_excluded(self):
        from core.screenview import observation_exclusion
        self.assertIn("excluded", observation_exclusion("LogonUI.exe", "Sign in").lower())

    def test_uia_element_exports_identity_and_supported_patterns(self):
        from core.screenview import _element_from_uia

        class Rect:
            left, top, right, bottom = 1, 2, 101, 42
        class Raw:
            CurrentIsPassword = False
            def GetRuntimeId(self): return [4, 5, 6]
            def GetCurrentPattern(self, pattern_id):
                return object() if pattern_id in (10000, 10015) else None
        info = SimpleNamespace(rectangle=Rect(), visible=True, name="Save",
                               control_type="Button", enabled=True,
                               automation_id="save", has_keyboard_focus=True,
                               element=Raw(), process_id=99,
                               parent=SimpleNamespace(name="Toolbar"))
        element = _element_from_uia(info, 1, 2, 1.0)
        self.assertEqual(element.automation_id, "save")
        self.assertEqual(element.runtime_id, (4, 5, 6))
        self.assertEqual(element.parent_name, "Toolbar")
        self.assertIn("invoke", element.patterns)
        self.assertNotIn("toggle", element.patterns)  # not queried for Button controls
        self.assertTrue(element.focused)
        self.assertEqual(element.window_pid, 99)

    def test_left_click_uses_uia_invoke_without_physical_click(self):
        from core.screenact import click
        from core.screenview import Element
        target = Element(1, "button", "Save", "", 10, 10, 80, 30,
                        runtime_id=(3, 4), patterns=("invoke",), window_pid=123,
                        window_title="Editor")
        with patch("core.desktop_control.should_stop", return_value=False), \
             patch("core.screenview.perform_uia_pattern",
                   return_value=(True, "UI Automation pattern completed")) as invoke, \
             patch("core.screenact.pyautogui.click") as physical_click:
            result = click(target)
        self.assertTrue(result.ok)
        self.assertIn("UI Automation", result.detail)
        invoke.assert_called_once_with(target, "invoke")
        physical_click.assert_not_called()

    def test_uia_stale_control_fails_closed_before_coordinate_fallback(self):
        from core.screenact import click
        from core.screenview import Element
        target = Element(1, "button", "Save", "", 10, 10, 80, 30,
                        runtime_id=(3, 4), patterns=("invoke",), window_pid=123)
        with patch("core.desktop_control.should_stop", return_value=False), \
             patch("core.screenview.perform_uia_pattern",
                   return_value=(False, "the UI Automation control disappeared; observe again")), \
             patch("core.screenact.pyautogui.click") as physical_click:
            result = click(target)
        self.assertFalse(result.ok)
        physical_click.assert_not_called()

    def test_uia_invoke_timeout_does_not_retry_as_physical_click(self):
        from core.screenact import click
        from core.screenview import Element
        target = Element(1, "button", "Run test search", "", 10, 10, 80, 30,
                        runtime_id=(3, 4), patterns=("invoke",), window_pid=123)
        with patch("core.desktop_control.should_stop", return_value=False), \
             patch("core.screenview.perform_uia_pattern",
                   return_value=(False, "CUIAutomation8 failed: TimeoutError")), \
             patch("core.screenact.pyautogui.click") as physical_click:
            result = click(target)
        self.assertFalse(result.ok)
        self.assertIn("TimeoutError", result.error)
        physical_click.assert_not_called()

    def test_uia_focus_uses_fresh_element_set_focus_without_click(self):
        from core.screenview import Element, perform_uia_pattern

        target = Element(1, "edit", "Search", "", 1, 1, 100, 25,
                         runtime_id=(3, 4), automation_id="search",
                         window_pid=123, window_title="Fixture")
        with patch("core.screenview._OS", "windows"), \
             patch("core.screenview._foreground",
                   return_value=("fixture.exe", "Fixture", 123, 99)), \
             patch("core.desktop_control.should_stop", return_value=False), \
             patch("core.windows_uia.perform", return_value=(True, "focused")) as adapter:
            ok, detail = perform_uia_pattern(target, "focus")

        self.assertTrue(ok, detail)
        adapter.assert_called_once()
        self.assertEqual(adapter.call_args.args[:2], (99, 123))
        self.assertEqual(adapter.call_args.args[8], "focus")

    def test_select_toggle_and_expand_use_semantic_patterns_only(self):
        from contextlib import nullcontext
        from actions import screen_agent
        from core.screenview import Element, View
        cases = (("select", "Open file", "selection_item", None),
                 ("toggle", "Wi-Fi", "toggle", None),
                 ("expand", "Projects", "expand_collapse", "expand"),
                 ("collapse", "Projects", "expand_collapse", "collapse"))
        for action, label, pattern, value in cases:
            with self.subTest(action=action):
                element = Element(1, "checkbox" if action == "toggle" else "button",
                                  label, "", 10, 10, 80, 30,
                                  patterns=(pattern,), runtime_id=(8, 9), window_pid=10,
                                  window_title="Test window")
                view = View(source="uia", degraded=False, app="test.exe",
                            title="Test window", pid=10, elements=[element])
                with patch.object(screen_agent, "_view", return_value=view), \
                     patch.object(screen_agent.desktop_control, "action",
                                  return_value=nullcontext()), \
                     patch.object(screen_agent, "_verified_after",
                                  return_value=(view, "verified by fresh UIA view")), \
                     patch.object(screen_agent.screenview, "perform_uia_pattern",
                                  return_value=(True, "UIA complete")) as perform, \
                     patch.object(screen_agent.screenact, "click",
                                  side_effect=AssertionError("must not click")):
                    result = screen_agent.screen_agent({"action": action, "target": label})
                self.assertIn("verified", result)
                perform.assert_called_once_with(element, pattern, value)

    def test_named_type_uses_valuepattern_without_keyboard_or_screenshot(self):
        from contextlib import nullcontext
        from actions import screen_agent
        from core.screenview import Element, View
        element = Element(1, "edit", "Search field", "", 10, 10, 200, 24,
                          patterns=("value",), runtime_id=(8, 9),
                          window_pid=10, window_title="Test window")
        view = View(source="uia", degraded=False, app="fixture.exe",
                    title="Test window", pid=10, elements=[element])
        with patch.object(screen_agent, "_view", return_value=view), \
             patch.object(screen_agent.desktop_control, "action", return_value=nullcontext()), \
             patch.object(screen_agent, "_verified_after",
                          return_value=(view, "verified: exact UIA value")), \
             patch.object(screen_agent.screenview, "perform_uia_pattern",
                          return_value=(True, "ValuePattern readback matched")) as perform, \
             patch.object(screen_agent.screenact, "type_text",
                          side_effect=AssertionError("physical typing should not run")), \
             patch.object(screen_agent.screenview, "capture",
                          side_effect=AssertionError("screenshot should not run")):
            result = screen_agent.screen_agent({
                "action": "type", "target": "Search field", "text": "safe query"})
        self.assertIn("verified", result)
        perform.assert_called_once_with(element, "value", "safe query")

    def test_named_button_click_uses_invoke_without_screen_capture(self):
        from contextlib import nullcontext
        from actions import screen_agent
        from core.screenview import Element, View
        element = Element(1, "button", "Run", "", 10, 10, 80, 24,
                          patterns=("invoke",), runtime_id=(8, 9),
                          window_pid=10, window_title="Test window")
        view = View(source="uia", degraded=False, app="fixture.exe",
                    title="Test window", pid=10, elements=[element])
        with patch.object(screen_agent, "_view", return_value=view), \
             patch.object(screen_agent.desktop_control, "action", return_value=nullcontext()), \
             patch.object(screen_agent, "_verified_after",
                          return_value=(view, "verified: status changed")), \
             patch.object(screen_agent.screenview, "perform_uia_pattern",
                          return_value=(True, "InvokePattern completed")) as perform, \
             patch.object(screen_agent.screenact, "click",
                          side_effect=AssertionError("physical click should not run")), \
             patch.object(screen_agent.screenview, "capture",
                          side_effect=AssertionError("screenshot should not run")):
            result = screen_agent.screen_agent({"action": "click", "target": "Run"})
        self.assertIn("verified", result)
        perform.assert_called_once_with(element, "invoke", None)


class UIADiagnosticLoggingTests(unittest.TestCase):
    def test_diagnostic_logging_is_off_by_default(self):
        import os
        from unittest.mock import patch
        from core.screenview import _uia_diagnostic
        with patch.dict(os.environ, {"REM_UIA_DIAGNOSTICS": "0"}), \
             patch("core.screenview.logging.getLogger") as get_logger:
            _uia_diagnostic("probe", window_title="fixture")
        get_logger.assert_not_called()

    def test_opt_in_diagnostics_drop_value_and_password_fields(self):
        import json, os
        from unittest.mock import patch
        from core.screenview import _uia_diagnostic
        with patch.dict(os.environ, {"REM_UIA_DIAGNOSTICS": "1"}), \
             patch("core.screenview.logging.getLogger") as get_logger:
            _uia_diagnostic("probe", window_title="fixture", control_type="Edit",
                            name="Search field", value="never log this",
                            password_value="never log this either")
        payload = json.loads(get_logger.return_value.warning.call_args.args[1])
        self.assertEqual(payload["event"], "probe")
        self.assertEqual(payload["name"], "Search field")
        self.assertNotIn("value", payload)
        self.assertNotIn("password_value", payload)
        self.assertNotIn("never log this", json.dumps(payload))


class YouTubeOrdinalTargetTests(unittest.TestCase):
    def test_live_card_script_prefers_title_link_over_thumbnail_overlay(self):
        from actions.browser_control import _BrowserSession

        class Locator:
            script = ""
            async def evaluate_all(self, script):
                self.script = script
                return [{"id": "abcdefghijk", "title": "Actual video title"}]
            async def count(self): return 1
            async def is_visible(self): return True
            async def is_enabled(self): return True
            async def click(self, **kwargs): return None

        class Page:
            url = "https://www.youtube.com/results?search_query=demo"
            locator_instances = None
            def __init__(self): self.locator_instances = []
            def locator(self, selector):
                locator = Locator()
                self.locator_instances.append(locator)
                return locator
            async def wait_for_url(self, *args, **kwargs):
                self.url = "https://www.youtube.com/watch?v=abcdefghijk"

        page = Page()
        coro = object.__new__(_BrowserSession)._click_youtube_video_ordinal(page, "first", ())
        with self.assertRaises(StopIteration) as completed:
            coro.send(None)
        script = page.locator_instances[0].script
        self.assertLess(script.index("a#video-title"),
                        script.index("a[href*=\"/watch?v=\"]"))
        self.assertIn("getAttribute('title')", script)
        self.assertIn("aria-hidden=\"true\"", script)
        self.assertIn("Opened and verified", completed.exception.value)

    def test_first_video_uses_card_identity_and_verifies_opened_id(self):
        from actions.browser_control import _BrowserSession

        class Locator:
            def __init__(self, selector):
                self.selector = selector
            async def evaluate_all(self, script):
                self.assert_selector = "ytd-video-renderer" in self.selector
                return [{"id": "abcdefghijk", "title": "First actual video"},
                        {"id": "lmnopqrstuv", "title": "Second actual video"}]
            async def count(self): return 1
            async def is_visible(self): return True
            async def is_enabled(self): return True
            async def click(self, **kwargs): return None

        class Page:
            url = "https://www.youtube.com/results?search_query=demo"
            def locator(self, selector):
                self.selector = selector
                return Locator(selector)
            async def wait_for_url(self, *args, **kwargs):
                self.url = "https://www.youtube.com/watch?v=abcdefghijk"

        session = object.__new__(_BrowserSession)
        page = Page()
        coro = session._click_youtube_video_ordinal(page, "first", ())
        try:
            coro.send(None)
        except StopIteration as completed:
            result = completed.value
        else:
            self.fail("mock browser coroutine unexpectedly required an event loop")
        self.assertIn("Opened and verified", result)
        self.assertIn("First actual video", result)
        self.assertIn("abcdefghijk", page.selector)

    def test_second_video_uses_second_card_identity(self):
        from actions.browser_control import _BrowserSession
        class Locator:
            def __init__(self, selector): self.selector = selector
            async def evaluate_all(self, script):
                return [{"id": "abcdefghijk", "title": "First"},
                        {"id": "lmnopqrstuv", "title": "Second"}]
            async def count(self): return 1
            async def is_visible(self): return True
            async def is_enabled(self): return True
            async def click(self, **kwargs): return None
        class Page:
            url = "https://www.youtube.com/results?search_query=demo"
            selector = ""
            def locator(self, selector):
                self.selector = selector
                return Locator(selector)
            async def wait_for_url(self, *args, **kwargs):
                self.url = "https://www.youtube.com/watch?v=lmnopqrstuv"
        page = Page()
        coro = object.__new__(_BrowserSession)._click_youtube_video_ordinal(page, "second", ())
        with self.assertRaises(StopIteration) as completed:
            coro.send(None)
        self.assertIn("Opened and verified", completed.exception.value)
        self.assertIn("lmnopqrstuv", page.selector)

    def test_missing_video_refuses_without_click(self):
        from actions.browser_control import _BrowserSession
        clicked = []
        class Locator:
            async def evaluate_all(self, script): return []
            async def click(self, **kwargs): clicked.append(True)
        class Page:
            url = "https://youtube.com/results"
            def locator(self, selector): return Locator()
        coro = object.__new__(_BrowserSession)._click_youtube_video_ordinal(Page(), "first", ())
        try:
            coro.send(None)
        except StopIteration as completed:
            result = completed.value
        else:
            self.fail("mock browser coroutine unexpectedly required an event loop")
        self.assertIn("nothing was clicked", result)
        self.assertEqual([], clicked)


if __name__ == "__main__":
    unittest.main()
