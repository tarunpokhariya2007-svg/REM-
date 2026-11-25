import unittest
from unittest.mock import patch
from unittest.mock import patch

from actions.browser_control import _BrowserSession


class _Immediate:
    def __init__(self, value):
        self.value = value
    def __await__(self):
        if False:
            yield None
        return self.value


def _resolve(coro):
    try:
        coro.send(None)
    except StopIteration as done:
        return done.value
    raise AssertionError("unexpected asynchronous wait")


class BrowserVerificationTests(unittest.TestCase):
    def test_edge_cdp_is_opt_in_and_loopback_only(self):
        from actions.browser_control import _configured_browser_cdp_endpoint
        with patch.dict("os.environ", {"REM_EDGE_CDP_URL": "http://localhost:9333"}):
            self.assertEqual(_configured_browser_cdp_endpoint("edge"),
                             "http://localhost:9333")
        with self.assertRaises(ValueError):
            _configured_browser_cdp_endpoint("edge", "http://192.168.1.2:9333")
        self.assertIsNone(_configured_browser_cdp_endpoint("firefox"))

    def test_enter_is_allowed_for_search_input(self):
        class Focused:
            async def count(self): return 1
            async def evaluate(self, _script):
                return {"tag": "INPUT", "type": "search", "role": "",
                        "label": "", "placeholder": "Search", "name": "q",
                        "editable": True}

        class Keyboard:
            def __init__(self): self.keys, self.page = [], None
            async def press(self, key):
                self.keys.append(key)
                self.page.url = "http://127.0.0.1/results?q=test"

        class Page:
            url = "http://127.0.0.1/"
            keyboard = Keyboard()
            async def evaluate(self, _script): return True
            def locator(self, _selector): return Focused()
            def is_closed(self): return False

        session = _BrowserSession.__new__(_BrowserSession)
        session._page = Page()
        session._page.keyboard.page = session._page
        session._launch = lambda: _Immediate(None)
        with patch("actions.browser_control.asyncio.sleep", lambda *_a: _Immediate(None)):
            result = _resolve(session.press("Enter"))
        self.assertIn("status=done", result)
        self.assertIn("search_submit", result)
        self.assertEqual(session._page.keyboard.keys, ["Enter"])

    def test_enter_remains_blocked_for_non_search_editable_form(self):
        class Focused:
            async def count(self): return 1
            async def evaluate(self, _script):
                return {"tag": "INPUT", "type": "text", "role": "",
                        "label": "Account name", "placeholder": "Account name", "name": "account",
                        "editable": True}

        class Keyboard:
            async def press(self, _key):
                raise AssertionError("generic Enter must not submit editable form")

        class Page:
            url = "http://127.0.0.1/"
            keyboard = Keyboard()
            async def evaluate(self, _script): return True
            def locator(self, _selector): return Focused()
            def is_closed(self): return False

        session = _BrowserSession.__new__(_BrowserSession)
        session._page = Page()
        session._launch = lambda: _Immediate(None)
        result = _resolve(session.press("Enter"))
        self.assertIn("could submit this form", result)

    def test_searchbox_click_verifies_focus_when_page_does_not_navigate(self):
        class Body:
            async def inner_text(self, **_kwargs): return "Search"

        class Page:
            url = "http://127.0.0.1/"
            async def title(self): return "Search page"
            def locator(self, _selector): return Body()

        class Searchbox:
            async def count(self): return 1
            async def is_visible(self): return True
            async def is_enabled(self): return True
            async def evaluate(self, script):
                if "document.activeElement" in script:
                    return True
                return {"text": "", "aria": "Search", "title": "",
                        "type": "search", "role": "searchbox", "method": "get",
                        "searchForm": True}
            async def click(self, **_kwargs): return None

        session = _BrowserSession.__new__(_BrowserSession)
        before = ("http://127.0.0.1/", "Search page", "Search")
        with patch("actions.browser_control.asyncio.sleep", lambda *_a: _Immediate(None)):
            result = _resolve(session._click_unique(Page(), Searchbox(), before,
                                                   "searchbox Search"))
        self.assertIn("focus_searchbox", result)
        self.assertIn("status=done", result)

    def test_hidden_page_receives_no_key_or_click(self):
        class Page:
            url = "http://127.0.0.1/"
            async def evaluate(self, _script): return False
            async def goto(self, *_args, **_kwargs):
                raise AssertionError("hidden page navigation should not be attempted")
            def locator(self, _selector):
                raise AssertionError("hidden page should not be queried for a target")
            def is_closed(self): return False

        session = _BrowserSession.__new__(_BrowserSession)
        session._page = Page()
        session._launch = lambda: _Immediate(None)
        self.assertIn("page is hidden", _resolve(session.press("Enter")))
        self.assertIn("page is hidden", _resolve(session.smart_click("First result")))
        self.assertIn("navigation was not attempted",
                      _resolve(session.go_to("http://127.0.0.1/results")))

    def test_first_second_and_third_results_use_identity_and_verify_navigation(self):
        rows = [
            {"label": "First result", "href": "http://127.0.0.1/result/1", "index": 0},
            {"label": "Second result", "href": "http://127.0.0.1/result/2", "index": 1},
            {"label": "Third result", "href": "http://127.0.0.1/result/3", "index": 2},
        ]

        class Link:
            def __init__(self, page, index): self.page, self.index = page, index
            async def get_attribute(self, _name): return rows[self.index]["href"]
            async def is_visible(self): return True
            async def is_enabled(self): return True
            async def count(self): return 1
            async def evaluate(self, _script):
                return {"text": rows[self.index]["label"], "aria": "", "title": "",
                        "type": "", "role": "", "method": "", "searchForm": False}
            async def click(self, **_kwargs):
                self.page.url = rows[self.index]["href"]
                self.page.title_text = rows[self.index]["label"]
                self.page.body_text = "Destination verified"

        class Links:
            def __init__(self, page): self.page = page
            def nth(self, index): return Link(self.page, index)

        class Body:
            def __init__(self, page): self.page = page
            async def inner_text(self, **_kwargs): return self.page.body_text

        class Page:
            url = "http://127.0.0.1/"
            title_text = "Results"
            body_text = "First Second Third"
            def is_closed(self): return False
            async def evaluate(self, script):
                return True if "document.visibilityState" in script else rows
            async def title(self): return self.title_text
            def locator(self, selector):
                return Links(self) if selector == "a[href]" else Body(self)

        with patch("actions.browser_control.asyncio.sleep", lambda *_a: _Immediate(None)):
            for ordinal, number in (("first", 1), ("second", 2), ("third", 3)):
                with self.subTest(ordinal=ordinal):
                    session = _BrowserSession.__new__(_BrowserSession)
                    session._page = Page()
                    session._launch = lambda: _Immediate(None)
                    result = _resolve(session.smart_click(f"Click the {ordinal} result"))
                    self.assertIn("verified page state changed", result)
                    self.assertEqual(session._page.url,
                                     f"http://127.0.0.1/result/{number}")


if __name__ == "__main__":
    unittest.main()
