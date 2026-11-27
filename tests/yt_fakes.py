"""A small stateful fake of a YouTube watch page for regression tests.

It answers the two scripts in ``core.media_state`` (``STATE_JS`` and
``ACTION_JS``) from an internal player model, so tests exercise the real
verification logic rather than canned strings. Everything about the model is
deliberately overridable so a test can make the player misbehave (ignore pause,
start with an ad, refuse seeks, show a consent wall, ...).
"""
from __future__ import annotations

from core import media_state as ms


class FakeKeyboard:
    def __init__(self, page):
        self.page = page
        self.pressed: list[str] = []

    async def press(self, key):
        self.pressed.append(key)
        if key == "f" and self.page.focused and self.page.keys_work:
            self.page.fullscreen = True
        elif key == "Escape":
            self.page.fullscreen = False


class FakeYouTubePage:
    def __init__(self, *, url="https://www.youtube.com/watch?v=abcdefghijk",
                 title="Actual video title", paused=False, ad=False, consent=False,
                 error="", duration=300.0, current=10.0, volume=80.0, muted=False,
                 ready_state=4, has_player=True):
        self.url = url
        self.title = title
        self.paused = paused
        self.ad = ad
        self.consent = consent
        self.error = error
        self.duration = duration
        self.current = current
        self.volume = volume
        self.muted = muted
        self.ready_state = ready_state
        self.has_player = has_player
        self.fullscreen = False
        self.focused = False
        # misbehaviour switches
        self.ignore_pause = False
        self.ignore_play = False
        self.ignore_seek = False
        self.ignore_volume = False
        self.keys_work = True
        self.calls: list[tuple] = []
        self.keyboard = FakeKeyboard(self)

    # --- helpers used by production code
    def is_closed(self):
        return False

    async def wait_for_url(self, *a, **k):
        return None

    async def evaluate(self, script, arg=None):
        if script == ms.STATE_JS:
            return self._state()
        if script == ms.ACTION_JS:
            return self._action(arg or {})
        raise AssertionError("unexpected script: " + str(script)[:60])

    # --- model
    def _video_id(self):
        from urllib.parse import parse_qsl, urlsplit
        return next((v for k, v in parse_qsl(urlsplit(self.url).query) if k == "v"), "")

    def _state(self):
        d = {"url": self.url, "host": "www.youtube.com", "consent": self.consent}
        if not self.has_player:
            d["player"] = False
            return d
        d.update(player=True, video=True, ad=self.ad, adSkippable=False, error=self.error,
                 paused=self.paused, ended=False, currentTime=self.current, duration=self.duration,
                 readyState=self.ready_state, seeking=False, volume=self.volume, muted=self.muted,
                 videoId=self._video_id(), playerVideoId=self._video_id(), title=self.title,
                 fullscreen=self.fullscreen)
        return d

    def _action(self, a):
        op = a.get("op")
        self.calls.append((op, a.get("value")))
        if not self.has_player:
            return {"ok": False, "blocked": "no_player"}
        if self.ad and op != "focus":
            return {"ok": False, "blocked": "ad"}
        if op == "pause":
            if not self.ignore_pause:
                self.paused = True
        elif op == "play":
            if not self.ignore_play:
                self.paused = False
        elif op in ("seek_by", "seek_to"):
            if not self.ignore_seek:
                t = (self.current + float(a["value"])) if op == "seek_by" else float(a["value"])
                self.current = max(0.0, min(self.duration - 0.5, t))
        elif op == "set_volume":
            if not self.ignore_volume:
                self.volume = max(0, min(100, round(float(a["value"]))))
                if self.volume > 0:
                    self.muted = False
        elif op == "mute":
            self.muted = True
        elif op == "unmute":
            self.muted = False
        elif op == "focus":
            self.focused = True
            return {"ok": True, "focused": True}
        return {"ok": True}


async def playing_evaluate(page, script, arg=None):
    """Adapter so legacy ad-hoc fake pages can answer the player protocol.

    The page reports a healthy, playing video for whatever /watch URL it is on.
    ``page.video_paused`` (if present) is honoured and kept in sync.
    """
    fake = FakeYouTubePage(url=page.url, paused=bool(getattr(page, "video_paused", False)))
    result = await fake.evaluate(script, arg)
    if hasattr(page, "video_paused"):
        page.video_paused = fake.paused
    return result
