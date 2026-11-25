import unittest
from types import SimpleNamespace
from unittest.mock import patch


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
