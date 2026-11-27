"""Cancellation, correction, send/call intent and authorization continuity."""
import os
import sys
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import desktop_control, task_control  # noqa: E402
from core.request_context import RequestContext  # noqa: E402


def auth(text, tool, args, ctx=None, source="voice"):
    ctx = ctx or RequestContext.create(text, source)
    return desktop_control.authorize_user_request(text, tool, args, source=source, auth_context=ctx)


class UtteranceClassification(unittest.TestCase):
    def test_cancel_phrases(self):
        for u in ("Cancel", "cancel that", "Never mind", "forget it", "stop everything", "cancel the message",
                  "REM, cancel", "abort", "rehne do", "ruko"):
            self.assertEqual(task_control.classify_utterance(u), "cancel", u)

    def test_stop_the_video_is_a_command_not_a_cancel(self):
        for u in ("stop the video", "pause the video", "cancel culture documentary on youtube please now and then",
                  "search for lofi"):
            self.assertNotEqual(task_control.classify_utterance(u), "cancel", u)

    def test_corrections(self):
        for u in ("No, I meant Rio", "actually search for jazz", "not that one the second one", "I said Rio"):
            self.assertEqual(task_control.classify_utterance(u), "correction", u)

    def test_bare_yes_is_a_confirmation_not_a_command(self):
        self.assertEqual(task_control.classify_utterance("yes"), "confirm")
        self.assertEqual(task_control.classify_utterance("okay"), "confirm")


class IntentFromCurrentUtterance(unittest.TestCase):
    def test_send_requires_a_whole_unnegated_word(self):
        yes = ("send it", "REM, send the message to Rio", "bhejo")
        no = ("don't send it", "do not send", "never send that", "resend later", "sender is Rio", "what did I send")
        for u in yes:
            self.assertTrue(task_control.has_send_command(u), u)
        for u in no:
            self.assertFalse(task_control.has_send_command(u), u)

    def test_call_kind_comes_from_the_persons_words(self):
        self.assertEqual(task_control.call_kind("call Rio"), "voice")
        self.assertEqual(task_control.call_kind("video call Rio"), "video")
        self.assertEqual(task_control.call_kind("call Rio on video"), "video")
        self.assertEqual(task_control.call_kind("stop the video"), "")
        self.assertEqual(task_control.call_kind("don't call Rio"), "")

    def test_deletion_scopes_are_distinct(self):
        s = task_control.deletion_scope
        self.assertEqual(s("clear my draft"), "clear_draft")
        self.assertEqual(s("clear the message box"), "clear_draft")
        self.assertEqual(s("delete the message"), "delete_message")
        self.assertEqual(s("unsend that"), "delete_message")
        self.assertEqual(s("delete this chat"), "delete_chat")
        self.assertEqual(s("delete the whole conversation"), "delete_chat")
        self.assertEqual(s("search for lofi"), "")


class VoiceSendAuthorization(unittest.TestCase):
    def _ctx(self, instruction, parent=""):
        return RequestContext.create(instruction, "voice", parent_instruction=parent, browser_task_id="bt1")

    def _decide(self, ctx):
        from actions import send_message
        tok = desktop_control.bind_auth_context(ctx)
        try:
            with patch("core.autonomous_mode.active_session", return_value=None):
                return send_message._is_voice_authorized_send("Rio", "WhatsApp")[0]
        finally:
            desktop_control.reset_auth_context(tok)

    def test_explicit_send_in_current_utterance_is_honoured(self):
        self.assertTrue(self._decide(self._ctx("send hello to Rio on WhatsApp")))

    def test_earlier_send_does_not_authorize_a_later_utterance(self):
        # the old code searched parent_instruction, so this authorized a send
        self.assertFalse(self._decide(self._ctx("write hello to Rio", parent="send hello to Rio")))

    def test_negated_send_never_authorizes(self):
        self.assertFalse(self._decide(self._ctx("write hello to Rio but don't send it")))


class CancellationStopsEverything(unittest.TestCase):
    def test_context_created_before_a_cancel_authorizes_nothing(self):
        ctx = RequestContext.create("pause the video", "voice", browser="chrome", browser_task_id="bt-cancel")
        ok, _ = auth("pause the video", "browser_control", {"action": "pause_video"}, ctx)
        self.assertTrue(ok)
        time.sleep(0.01)
        task_control.request_cancel("test")
        ok, reason = auth("pause the video", "browser_control", {"action": "pause_video"}, ctx)
        self.assertFalse(ok)
        self.assertIn("cancel", reason.lower())

    def test_new_context_after_cancel_works_and_cancel_is_not_a_latch(self):
        task_control.request_cancel("test")
        self.assertFalse(desktop_control.should_stop())
        time.sleep(0.01)
        ctx = RequestContext.create("pause the video", "voice", browser="chrome", browser_task_id="bt-after")
        ok, _ = auth("pause the video", "browser_control", {"action": "pause_video"}, ctx)
        self.assertTrue(ok)

    def test_generation_changes_on_cancel(self):
        g = task_control.cancel_generation()
        self.assertFalse(task_control.was_cancelled_since(g))
        task_control.request_cancel("x")
        self.assertTrue(task_control.was_cancelled_since(g))

    def test_cancel_keeps_sent_attempt_records(self):
        desktop_control.record_sent_attempt(receiver="Rio", draft_hash="keep-me", task_id="")
        task_control.request_cancel("x")
        self.assertTrue(desktop_control.has_sent_attempt(receiver="Rio", draft_hash="keep-me", task_id=""))


class AuthorizationIsNotReusedAcrossTargets(unittest.TestCase):
    def test_youtube_request_cannot_authorize_a_whatsapp_send(self):
        ctx = RequestContext.create("play lofi on YouTube", "voice", browser="chrome", browser_task_id="bt-yt")
        ok, _ = auth("play lofi on YouTube", "send_message",
                     {"receiver": "Rio", "message_text": "hi", "platform": "WhatsApp"}, ctx)
        self.assertFalse(ok)

    def test_fragment_does_not_authorize_typing(self):
        ctx = RequestContext.create("uh", "voice", browser="chrome", browser_task_id="bt-frag")
        ok, _ = auth("uh", "browser_control", {"action": "type", "text": "hello"}, ctx)
        self.assertFalse(ok)

    def test_deleting_a_chat_is_not_satisfied_by_clearing_the_composer(self):
        ctx = RequestContext.create("delete this chat with Rio", "voice", browser="chrome", browser_task_id="bt-del")
        ok, reason = auth("delete this chat with Rio", "browser_control", {"action": "clear_field"}, ctx)
        self.assertFalse(ok)
        self.assertIn("confirmation", reason)

    def test_deleting_a_sent_message_is_not_satisfied_by_clearing_the_composer(self):
        ok, _ = auth("delete the message I sent", "browser_control", {"action": "clear_field"})
        self.assertFalse(ok)

    def test_clearing_a_draft_is_allowed_by_an_explicit_request(self):
        ctx = RequestContext.create("clear the draft in the message box", "voice", browser="chrome", browser_task_id="bt-clr")
        ok, reason = auth("clear the draft in the message box", "browser_control", {"action": "clear_field"}, ctx)
        self.assertTrue(ok, reason)

    def test_clear_field_without_any_clear_request_is_not_blanket_allowed(self):
        # previously every action in the task chain's "routine" list skipped intent checks
        ctx = RequestContext.create("open whatsapp", "voice", browser="chrome", browser_task_id="bt-blanket")
        ok, _ = auth("open whatsapp", "browser_control", {"action": "close_tab"}, ctx)
        self.assertFalse(ok)


class NewMediaActionsNeedMatchingIntent(unittest.TestCase):
    def test_set_volume_and_fullscreen_authorized_by_explicit_request(self):
        c1 = RequestContext.create("set the volume to 50 percent", "voice", browser="chrome", browser_task_id="bt-v")
        self.assertTrue(auth("set the volume to 50 percent", "browser_control",
                             {"action": "set_volume", "value": "50"}, c1)[0])
        c2 = RequestContext.create("turn on fullscreen", "voice", browser="chrome", browser_task_id="bt-f")
        self.assertTrue(auth("turn on fullscreen", "browser_control", {"action": "fullscreen"}, c2)[0])

    def test_fullscreen_not_authorized_by_unrelated_request(self):
        c = RequestContext.create("search for lofi", "voice", browser="chrome", browser_task_id="bt-x")
        self.assertFalse(auth("search for lofi", "browser_control", {"action": "fullscreen"}, c)[0])


if __name__ == "__main__":
    unittest.main()
