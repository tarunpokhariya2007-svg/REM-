"""core/media_state.py - what a YouTube player is ACTUALLY doing.

WHY THIS EXISTS
    The old playback code read ``document.querySelector('video')`` - the first
    <video> on the page, which on YouTube can be a hover-preview, an advert or
    the real player - and then returned ``status=done`` whenever the keystroke
    or ``v.pause()`` call had been issued, even when its own readback said the
    video was still playing. This module replaces that with:

    * ONE scoped state read (``STATE_JS``) of the real player (``#movie_player``),
      including advert, loading, unavailable and consent-wall detection;
    * pure judgement functions (``judge_*``) that decide success from a state
      snapshot only - so they are unit-testable without any browser;
    * small async helpers that act, wait a BOUNDED time, re-read and judge.

    A caller must never report success unless a ``judge_*`` function said so.
"""
from __future__ import annotations

import asyncio
import re
import time
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

# --------------------------------------------------------------------------- JS
# Read-only. Scoped to the main player so hover previews are never mistaken for it.
STATE_JS = r"""() => {
  const host = location.hostname || '';
  const out = {url: location.href, host, path: location.pathname};
  out.consent = host.startsWith('consent.') ||
    !!document.querySelector('ytd-consent-bump-v2-lightbox, tp-yt-paper-dialog form[action*="consent"], form[action*="consent.youtube.com"]');
  const p = document.querySelector('#movie_player');
  if (!p) { out.player = false; return out; }
  out.player = true;
  const v = p.querySelector('video.html5-main-video') || p.querySelector('video');
  out.video = !!v;
  out.ad = p.classList.contains('ad-showing') || p.classList.contains('ad-interrupting') ||
           !!p.querySelector('.ytp-ad-player-overlay, .ytp-ad-text, .ytp-ad-preview-container');
  out.adSkippable = !!p.querySelector('.ytp-ad-skip-button, .ytp-ad-skip-button-modern, .ytp-skip-ad-button');
  const err = p.querySelector('.ytp-error');
  const errVisible = !!(err && err.getBoundingClientRect().width > 0 && getComputedStyle(err).display !== 'none');
  out.error = errVisible ? ((err.innerText || '').trim().slice(0, 160)) : '';
  if (v) {
    out.paused = !!v.paused; out.ended = !!v.ended;
    out.currentTime = Number(v.currentTime || 0);
    out.duration = Number(isFinite(v.duration) ? v.duration : 0);
    out.readyState = Number(v.readyState || 0);
    out.seeking = !!v.seeking;
    out.mediaVolume = Number(v.volume);
  }
  try { out.volume = typeof p.getVolume === 'function' ? Number(p.getVolume()) : null; } catch (e) { out.volume = null; }
  try { out.muted = typeof p.isMuted === 'function' ? !!p.isMuted() : (v ? !!v.muted : null); } catch (e) { out.muted = null; }
  let vid = '';
  try { vid = new URL(location.href).searchParams.get('v') || ''; } catch (e) {}
  try { const d = p.getVideoData && p.getVideoData(); if (d) { out.playerVideoId = d.video_id || ''; out.playerTitle = d.title || ''; } } catch (e) {}
  out.videoId = vid;
  const h1 = document.querySelector('ytd-watch-metadata h1, h1.ytd-watch-metadata, #title h1');
  out.title = ((h1 && (h1.innerText || h1.textContent)) || out.playerTitle || document.title.replace(/ - YouTube$/, '') || '').trim();
  out.fullscreen = !!document.fullscreenElement;
  return out;
}"""

# Acts on the real player only. Refuses to touch an advert. Returns {ok, blocked?, ...}.
ACTION_JS = r"""(a) => {
  const p = document.querySelector('#movie_player');
  if (!p) return {ok: false, blocked: 'no_player'};
  const v = p.querySelector('video.html5-main-video') || p.querySelector('video');
  if (!v) return {ok: false, blocked: 'no_video'};
  const ad = p.classList.contains('ad-showing') || p.classList.contains('ad-interrupting');
  if (ad && a.op !== 'focus') return {ok: false, blocked: 'ad'};
  switch (a.op) {
    case 'pause': if (p.pauseVideo) p.pauseVideo(); else v.pause(); return {ok: true};
    case 'play':  if (p.playVideo) p.playVideo(); else v.play(); return {ok: true};
    case 'seek_by': {
      const dur = Number(isFinite(v.duration) ? v.duration : 0);
      let t = Number(v.currentTime || 0) + Number(a.value || 0);
      t = Math.max(0, dur > 0 ? Math.min(dur - 0.5, t) : t);
      if (p.seekTo) p.seekTo(t, true); else v.currentTime = t;
      return {ok: true, target: t};
    }
    case 'seek_to': {
      const dur = Number(isFinite(v.duration) ? v.duration : 0);
      let t = Math.max(0, dur > 0 ? Math.min(dur - 0.5, Number(a.value || 0)) : Number(a.value || 0));
      if (p.seekTo) p.seekTo(t, true); else v.currentTime = t;
      return {ok: true, target: t};
    }
    case 'set_volume': {
      const n = Math.max(0, Math.min(100, Math.round(Number(a.value))));
      if (p.setVolume) { p.setVolume(n); if (n > 0 && p.isMuted && p.isMuted()) p.unMute(); }
      else { v.volume = n / 100; v.muted = (n === 0); }
      return {ok: true, target: n};
    }
    case 'mute':   if (p.mute) p.mute(); else v.muted = true; return {ok: true};
    case 'unmute': if (p.unMute) p.unMute(); else v.muted = false; return {ok: true};
    case 'focus':  p.focus(); return {ok: true, focused: document.activeElement === p};
  }
  return {ok: false, blocked: 'unknown_op'};
}"""

# --------------------------------------------------------------------------- model
@dataclass
class PlayerState:
    player: bool = False
    video: bool = False
    url: str = ""
    video_id: str = ""
    title: str = ""
    paused: bool = True
    ended: bool = False
    current_time: float = 0.0
    duration: float = 0.0
    ready_state: int = 0
    seeking: bool = False
    ad: bool = False
    ad_skippable: bool = False
    error: str = ""
    consent: bool = False
    volume: float | None = None
    muted: bool | None = None
    fullscreen: bool = False
    raw: dict = field(default_factory=dict)


def parse_state(raw: Any) -> PlayerState:
    d = raw if isinstance(raw, dict) else {}
    def _f(k, default=0.0):
        try:
            return float(d.get(k, default) or 0.0)
        except (TypeError, ValueError):
            return float(default)
    vol = d.get("volume")
    return PlayerState(
        player=bool(d.get("player")), video=bool(d.get("video")),
        url=str(d.get("url") or ""),
        video_id=str(d.get("playerVideoId") or d.get("videoId") or ""),
        title=str(d.get("title") or ""),
        paused=bool(d.get("paused", True)), ended=bool(d.get("ended")),
        current_time=_f("currentTime"), duration=_f("duration"),
        ready_state=int(_f("readyState")), seeking=bool(d.get("seeking")),
        ad=bool(d.get("ad")), ad_skippable=bool(d.get("adSkippable")),
        error=str(d.get("error") or ""), consent=bool(d.get("consent")),
        volume=(None if vol is None else float(vol)),
        muted=(None if d.get("muted") is None else bool(d.get("muted"))),
        fullscreen=bool(d.get("fullscreen")), raw=d,
    )


# --------------------------------------------------------------------------- judges
def _norm(text: str) -> str:
    t = unicodedata.normalize("NFKD", str(text or "")).casefold()
    t = "".join(c for c in t if not unicodedata.combining(c))
    return re.sub(r"[^\w]+", " ", t).strip()


def title_matches(wanted: str, actual: str) -> bool:
    """True when ``actual`` is plausibly the video the person asked for.

    Requires every word of the shorter title to appear in the longer one, so a
    spoken fragment ("lofi hip hop") matches "lofi hip hop radio - beats to
    relax/study to" but "lofi" alone cannot match an unrelated "Lofi Girl".
    Empty ``wanted`` never matches (nothing was asked for => nothing to verify).
    """
    w, a = set(_norm(wanted).split()), set(_norm(actual).split())
    if not w or not a:
        return False
    short, long_ = (w, a) if len(w) <= len(a) else (a, w)
    return short <= long_


@dataclass
class Verdict:
    ok: bool
    status: str        # playing|paused|loading|ad|unavailable|consent|no_player|wrong_video|ended|changed|unchanged
    reason: str = ""


def judge_page(s: PlayerState) -> Verdict | None:
    """Blocking conditions common to every action. None => nothing blocks."""
    if s.consent:
        return Verdict(False, "consent", "a YouTube consent screen is blocking the page")
    if not s.player or not s.video:
        return Verdict(False, "no_player", "no YouTube player was found on this page")
    if s.error:
        return Verdict(False, "unavailable", f"YouTube reports: {s.error}")
    if s.ad:
        return Verdict(False, "ad", "an advertisement is playing, not the requested video")
    return None


def judge_playing(s: PlayerState, *, expected_id: str = "", expected_title: str = "") -> Verdict:
    blocked = judge_page(s)
    if blocked:
        return blocked
    if expected_id and s.video_id:
        # The video id is authoritative; titles can be truncated or re-encoded.
        if s.video_id != expected_id:
            return Verdict(False, "wrong_video", f"player is on video {s.video_id}, not {expected_id}")
    elif expected_title and s.title and not title_matches(expected_title, s.title):
        return Verdict(False, "wrong_video", f"page title {s.title!r} does not match {expected_title!r}")
    if s.ended:
        return Verdict(False, "ended", "the video has ended")
    if s.paused:
        return Verdict(False, "paused", "the video is paused")
    if s.ready_state < 3 or s.seeking:
        return Verdict(False, "loading", "the video is still loading")
    return Verdict(True, "playing")


def judge_paused(s: PlayerState) -> Verdict:
    blocked = judge_page(s)
    if blocked:
        return blocked
    return Verdict(True, "paused") if s.paused else Verdict(False, "playing", "the video is still playing")


def judge_seek(before: PlayerState, after: PlayerState, delta: float, *,
               elapsed: float = 0.0) -> Verdict:
    """Forward: position must advance by ~delta (never less). Backward: must land
    near before-delta, allowing for playback progress during ``elapsed`` seconds."""
    blocked = judge_page(after)
    if blocked:
        return blocked
    if before.video_id and after.video_id and before.video_id != after.video_id:
        return Verdict(False, "changed", "a different video loaded during the seek")
    slack = 1.5 + (0.0 if after.paused else max(0.0, elapsed) + 1.0)
    dur = after.duration
    if delta >= 0:
        want = before.current_time + delta
        if dur > 0:
            want = min(want, dur - 0.5)
        ok = after.current_time >= want - 1.5 or (dur > 0 and after.current_time >= dur - 1.5)
    else:
        want = max(0.0, before.current_time + delta)
        ok = abs(after.current_time - want) <= slack or (want == 0.0 and after.current_time <= slack)
    return Verdict(ok, "changed" if ok else "unchanged",
                   "" if ok else f"position {after.current_time:.1f}s is not near the requested {want:.1f}s")


def judge_volume(s: PlayerState, target: float) -> Verdict:
    blocked = judge_page(s)
    if blocked:
        return blocked
    if s.volume is None:
        return Verdict(False, "unchanged", "the player did not report its volume")
    return (Verdict(True, "changed") if abs(s.volume - float(target)) <= 1.0
            else Verdict(False, "unchanged", f"player volume is {s.volume:.0f}, not {target:.0f}"))


def judge_muted(s: PlayerState, want_muted: bool) -> Verdict:
    blocked = judge_page(s)
    if blocked:
        return blocked
    if s.muted is None:
        return Verdict(False, "unchanged", "the player did not report its mute state")
    return Verdict(True, "changed") if s.muted == want_muted else Verdict(False, "unchanged", "mute state did not change")


def judge_fullscreen(s: PlayerState, want: bool) -> Verdict:
    return Verdict(True, "changed") if s.fullscreen == want else Verdict(
        False, "unchanged", f"fullscreen is {'on' if s.fullscreen else 'off'}, expected {'on' if want else 'off'}")


# --------------------------------------------------------------------------- async I/O
async def read_state(page) -> PlayerState:
    try:
        return parse_state(await page.evaluate(STATE_JS))
    except Exception:
        return PlayerState()


async def act(page, op: str, value: float | None = None) -> dict:
    try:
        res = await page.evaluate(ACTION_JS, {"op": op, "value": value})
        return res if isinstance(res, dict) else {"ok": False, "blocked": "bad_result"}
    except Exception as exc:
        return {"ok": False, "blocked": f"error:{type(exc).__name__}"}


async def wait_until(page, judge: Callable[[PlayerState], Verdict], *, timeout: float = 4.0,
                     interval: float = 0.25) -> tuple[Verdict, PlayerState]:
    """Poll for a bounded time; return the last verdict. Never loops forever."""
    deadline = time.monotonic() + max(0.1, timeout)
    state = await read_state(page)
    verdict = judge(state)
    while not verdict.ok and time.monotonic() < deadline:
        if verdict.status in {"consent", "unavailable", "ad", "no_player", "wrong_video"}:
            break      # waiting will not fix these
        await asyncio.sleep(interval)
        state = await read_state(page)
        verdict = judge(state)
    return verdict, state


def facts(s: PlayerState) -> dict:
    """Facts safe to hand to outcome.* (no raw dict)."""
    return {
        "title": s.title[:120], "video_id": s.video_id,
        "player_state": ("ad" if s.ad else "paused" if s.paused else "playing"),
        "current_time": round(s.current_time, 1), "duration": round(s.duration, 1),
        "volume": (None if s.volume is None else round(s.volume)),
        "muted": s.muted, "fullscreen": s.fullscreen,
    }
