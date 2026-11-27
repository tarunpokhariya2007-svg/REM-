"""YouTube playback regression tests.

They drive the real BrowserSession methods against ``tests.yt_fakes.FakeYouTubePage``
(a stateful player model), so what is verified is the verification logic itself:
success only from player state, never from "a key was sent".
"""
import asyncio
import os
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from actions import browser_control  # noqa: E402
from core import media_state as ms  # noqa: E402
from tests.yt_fakes import FakeYouTubePage  # noqa: E402


def make_session(page, pages=None):
    sess = object.__new__(browser_control._BrowserSession)
    sess.browser_name = "chrome"
    sess._page = page
    if pages is None:
        sess._context = None
        sess._get_page = AsyncMock(return_value=page)
    else:
        sess._context = SimpleNamespace(pages=pages)
        sess._launch = AsyncMock()
        sess._get_page = AsyncMock(return_value=page)
    return sess


def run(coro):
    return asyncio.run(coro)


class PlaybackVerification(unittest.TestCase):
    def test_title_matching_is_strict_about_words(self):
        self.assertTrue(ms.title_matches("lofi hip hop", "Lofi Hip Hop Radio - beats to relax"))
        self.assertFalse(ms.title_matches("lofi", "Jazz Piano Night"))
        self.assertFalse(ms.title_matches("", "anything"))

    def test_playing_is_done_and_verified(self):
        page = FakeYouTubePage()
        sess = make_session(page)
        res = run(sess._verify_youtube_playback(page, "abcdefghijk", "Actual video title", position=1))
        self.assertIn("status=done", res)
        self.assertIn("playback_verified=true", res)

    def test_paused_video_is_never_reported_done(self):
        page = FakeYouTubePage(paused=True)
        page.ignore_play = True          # autoplay blocked and play() refused
        sess = make_session(page)
        res = run(sess._verify_youtube_playback(page, "abcdefghijk", "Actual video title", position=1))
        self.assertNotIn("status=done", res)
        self.assertIn("status=unverified", res)
        self.assertIn("playback_verified=false", res)

    def test_loading_video_is_not_playing(self):
        page = FakeYouTubePage(ready_state=1)
        sess = make_session(page)
        # bounded wait: patch to keep the test fast
        v, st = run(ms.wait_until(page, ms.judge_playing, timeout=0.3, interval=0.05))
        self.assertFalse(v.ok)
        self.assertEqual(v.status, "loading")

    def test_advert_is_not_the_requested_video(self):
        page = FakeYouTubePage(ad=True)
        sess = make_session(page)
        res = run(sess._verify_youtube_playback(page, "abcdefghijk", "Actual video title", position=1))
        self.assertNotIn("status=done", res)
        self.assertIn("advertisement", res)

    def test_wrong_video_id_fails(self):
        page = FakeYouTubePage(url="https://www.youtube.com/watch?v=zzzzzzzzzzz")
        sess = make_session(page)
        res = run(sess._verify_youtube_playback(page, "zzzzzzzzzzz", "x", position=1))
        self.assertIn("status=done", res)         # same id: fine
        page.url = "https://www.youtube.com/watch?v=abcdefghijk"
        res = run(sess._verify_youtube_playback(page, "zzzzzzzzzzz", "x", position=1))
        self.assertIn("status=failed", res)

    def test_consent_screen_is_reported_not_played(self):
        page = FakeYouTubePage(consent=True)
        res = run(make_session(page)._verify_youtube_playback(page, "abcdefghijk", "t", position=1))
        self.assertIn("status=failed", res)
        self.assertIn("consent", res)

    def test_unavailable_video_fails(self):
        page = FakeYouTubePage(error="Video unavailable")
        res = run(make_session(page)._verify_youtube_playback(page, "abcdefghijk", "t", position=1))
        self.assertIn("status=failed", res)
        self.assertIn("unavailable", res.lower())


class PlayerControls(unittest.TestCase):
    def test_pause_verified_and_idempotent(self):
        page = FakeYouTubePage(paused=False)
        sess = make_session(page)
        self.assertIn("status=done", run(sess.pause_video()))
        self.assertTrue(page.paused)
        # pausing again must NOT toggle back to playing
        self.assertIn("status=done", run(sess.pause_video()))
        self.assertTrue(page.paused)
        self.assertEqual([c[0] for c in page.calls].count("pause"), 1)

    def test_pause_that_does_not_take_is_not_done(self):
        page = FakeYouTubePage(paused=False)
        page.ignore_pause = True
        res = run(make_session(page).pause_video())
        self.assertNotIn("status=done", res)
        self.assertIn("status=unverified", res)

    def test_resume(self):
        page = FakeYouTubePage(paused=True)
        sess = make_session(page)
        self.assertIn("status=done", run(sess.resume_video()))
        self.assertFalse(page.paused)

    def test_resume_refused_is_unverified(self):
        page = FakeYouTubePage(paused=True)
        page.ignore_play = True
        self.assertIn("status=unverified", run(make_session(page).resume_video()))

    def test_seek_forward_and_backward(self):
        page = FakeYouTubePage(current=50.0, paused=True)
        sess = make_session(page)
        res = run(sess.seek_video(30))
        self.assertIn("status=done", res)
        self.assertAlmostEqual(page.current, 80.0, places=1)
        res = run(sess.seek_video(-10))
        self.assertIn("status=done", res)
        self.assertAlmostEqual(page.current, 70.0, places=1)

    def test_seek_that_does_not_move_is_not_done(self):
        page = FakeYouTubePage(current=50.0, paused=True)
        page.ignore_seek = True
        res = run(make_session(page).seek_video(30))
        self.assertNotIn("status=done", res)

    def test_seek_never_touches_an_advert(self):
        page = FakeYouTubePage(current=5.0, ad=True)
        res = run(make_session(page).seek_video(30))
        self.assertNotIn("status=done", res)
        self.assertEqual(page.calls, [])      # not even attempted

    def test_set_volume_to_50_verified_on_player_not_system(self):
        page = FakeYouTubePage(volume=80)
        res = run(make_session(page).set_volume(50))
        self.assertIn("status=done", res)
        self.assertEqual(page.volume, 50)

    def test_set_volume_ignored_is_unverified(self):
        page = FakeYouTubePage(volume=80)
        page.ignore_volume = True
        self.assertIn("status=unverified", run(make_session(page).set_volume(50)))

    def test_volume_step_up_and_clamp(self):
        page = FakeYouTubePage(volume=95)
        sess = make_session(page)
        self.assertIn("status=done", run(sess.volume_step(10)))
        self.assertEqual(page.volume, 100)

    def test_mute_is_idempotent(self):
        page = FakeYouTubePage(muted=False)
        sess = make_session(page)
        self.assertIn("status=done", run(sess.set_muted(True)))
        self.assertIn("status=done", run(sess.set_muted(True)))     # must stay muted
        self.assertTrue(page.muted)
        self.assertEqual([c[0] for c in page.calls].count("mute"), 1)
        self.assertIn("status=done", run(sess.set_muted(False)))
        self.assertFalse(page.muted)

    def test_fullscreen_enter_and_exit(self):
        page = FakeYouTubePage()
        sess = make_session(page)
        self.assertIn("status=done", run(sess.set_fullscreen(True)))
        self.assertTrue(page.fullscreen)
        self.assertEqual(page.keyboard.pressed, ["f"])
        self.assertIn("status=done", run(sess.set_fullscreen(False)))
        self.assertFalse(page.fullscreen)

    def test_fullscreen_key_ignored_is_not_done(self):
        page = FakeYouTubePage()
        page.keys_work = False
        self.assertNotIn("status=done", run(make_session(page).set_fullscreen(True)))

    def test_controls_refuse_when_no_player(self):
        page = FakeYouTubePage(has_player=False, url="https://www.youtube.com/results?search_query=x")
        sess = make_session(page)
        for coro in (sess.pause_video(), sess.resume_video(), sess.seek_video(10), sess.set_volume(40),
                     sess.set_fullscreen(True)):
            res = run(coro)
            self.assertNotIn("status=done", res)
        self.assertEqual(page.keyboard.pressed, [])        # no blind keys

    def test_consent_blocks_controls(self):
        page = FakeYouTubePage(consent=True)
        res = run(make_session(page).pause_video())
        self.assertIn("status=failed", res)


class WrongTabPrevention(unittest.TestCase):
    class _Other:
        url = "https://web.whatsapp.com/"
        keyboard = SimpleNamespace(press=AsyncMock())

        def is_closed(self):
            return False

        async def evaluate(self, script, arg=None):
            if script == ms.STATE_JS:
                return {"url": self.url, "host": "web.whatsapp.com", "player": False}
            return True

        async def bring_to_front(self):
            return None

    def test_controls_go_to_the_youtube_tab_not_the_last_tab(self):
        yt = FakeYouTubePage(paused=False)
        yt.bring_to_front = AsyncMock()

        async def _vis(script, arg=None, _orig=yt.evaluate):
            if script == "document.visibilityState === 'visible'":
                return True
            return await _orig(script, arg)
        yt.evaluate = _vis
        other = self._Other()
        sess = make_session(other, pages=[yt, other])       # WhatsApp is the LAST tab
        res = run(sess.pause_video())
        self.assertIn("status=done", res)
        self.assertTrue(yt.paused)
        other.keyboard.press.assert_not_called()

    def test_no_youtube_tab_means_no_keys(self):
        other = self._Other()
        sess = make_session(other, pages=[other])
        res = run(sess.set_fullscreen(True))
        self.assertIn("status=failed", res)
        self.assertIn("no YouTube tab", res)
        other.keyboard.press.assert_not_called()


class NativePathDoesNotToggleBlindly(unittest.TestCase):
    def _view(self, label):
        from core import screenview
        return SimpleNamespace(elements=[screenview.Element(eid=1, role="button", name=label, value="",
                                                            left=1, top=1, width=10, height=10)])

    def test_state_is_read_from_button_labels(self):
        f = browser_control._yt_native_state
        self.assertEqual(f(self._view("Pause (k)"), "playback"), "playing")
        self.assertEqual(f(self._view("Play (k)"), "playback"), "paused")
        self.assertEqual(f(self._view("Unmute (m)"), "mute"), "muted")
        self.assertEqual(f(self._view("Exit full screen (f)"), "fullscreen"), "fullscreen")
        self.assertIsNone(f(self._view("Subscribe"), "playback"))


if __name__ == "__main__":
    unittest.main()


class DispatchReachesVerifiedMethods(unittest.TestCase):
    """browser_control(action=...) must route the new actions to the verified session methods."""

    def _dispatch(self, params):
        from unittest.mock import MagicMock, patch
        page = FakeYouTubePage(volume=80)
        sess = make_session(page)
        sess.run = lambda coro, timeout=60: run(coro)
        reg = MagicMock()
        reg.get.return_value = sess
        reg.pop_native_url.return_value = None
        with patch.object(browser_control, "_registry", reg), \
             patch.object(browser_control, "select_browser", return_value="chrome"), \
             patch.object(browser_control, "_should_use_native_window", return_value=False):
            out = browser_control._browser_control_impl(dict(params, browser="chrome"))
        return out, page

    def test_set_volume_dispatch(self):
        out, page = self._dispatch({"action": "set_volume", "amount": "35"})
        self.assertIn("status=done", out)
        self.assertEqual(page.volume, 35)

    def test_set_volume_without_a_number_fails_loudly(self):
        out, page = self._dispatch({"action": "set_volume"})
        self.assertIn("status=failed", out)

    def test_fullscreen_dispatch(self):
        out, page = self._dispatch({"action": "fullscreen"})
        self.assertIn("status=done", out)
        self.assertTrue(page.fullscreen)
