"""WhatsApp regression tests: identity, composer readback, send/duplicate, calls."""
import os
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import desktop_control, screenview  # noqa: E402
from plugins import _whatsapp_core as wa  # noqa: E402

E = screenview.Element
MON = {"left": 0, "top": 0, "width": 1920, "height": 1080}


def view(elements, title="WhatsApp"):
    return SimpleNamespace(app="WhatsApp.exe", title=title, monitor=MON, windows=[], degraded=False,
                           source="uia", elements=elements)


def header(name):
    return E(eid="hdr", role="text", name=name, value="", left=580, top=105, width=160, height=24)


def composer(value="", name="Type a message", **kw):
    return E(eid="comp", role="edit", name=name, value=value, left=580, top=940, width=680, height=40, **kw)


def bubble(text, top=500, eid="b"):
    return E(eid=eid, role="text", name=text, value="", left=700, top=top, width=300, height=24)


class ContactIdentity(unittest.TestCase):
    def test_whole_word_matching_only(self):
        m = wa.contact_match
        self.assertEqual(m("Rio", "Rio"), "exact")
        self.assertEqual(m("Rio", "Rio Sharma"), "partial")
        self.assertEqual(m("rio", "RÍO"), "exact")
        for wrong in ("Mario", "Riordan", "Ri", "Ariosto"):
            self.assertEqual(m("Rio", wrong), "none", wrong)
        self.assertEqual(m("Rio Sharma", "Rio"), "none")

    def test_header_for_mario_does_not_verify_rio(self):
        v = view([header("Mario"), composer()])
        res = wa.verify_whatsapp_active_chat(v, expected_contact="Rio")
        self.assertFalse(res["verified"])

    def test_correct_header_with_composer_verifies(self):
        v = view([header("Rio"), composer()])
        res = wa.verify_whatsapp_active_chat(v, expected_contact="Rio")
        self.assertTrue(res["verified"], res)

    def test_header_without_composer_is_not_verified(self):
        v = view([header("Rio")])
        self.assertFalse(wa.verify_whatsapp_active_chat(v, expected_contact="Rio")["verified"])


class ComposerReadback(unittest.TestCase):
    def test_placeholder_is_not_content(self):
        self.assertEqual(wa.normalize_composer_text("Type a message"), "")
        self.assertEqual(wa.normalize_composer_text("\u200b\ufeff "), "")
        self.assertEqual(wa.normalize_composer_text("hi\u00a0there"), "hi there")

    def test_placeholder_displayed_means_empty(self):
        self.assertEqual(wa.read_composer(view([header("Rio"), composer()]))["status"], "empty")

    def test_real_draft_is_read(self):
        r = wa.read_composer(view([header("Rio"), composer(value="hello Rio", name="Type a message")]))
        self.assertEqual((r["status"], r["text"]), ("has_text", "hello Rio"))

    def test_unreadable_composer_is_not_reported_empty(self):
        # Name is not the placeholder and no value is exposed: we simply cannot tell.
        el = composer(value="", name="Message")
        el = E(eid="comp", role="edit", name="Compose box", value="", left=580, top=940, width=680, height=40)
        r = wa.read_composer(view([header("Rio"), el]))
        self.assertNotEqual(r["status"], "empty")
        self.assertEqual(wa.composer_text_value(view([header("Rio"), el]))[0], False)

    def test_draft_drawn_as_text_in_composer_row_is_read(self):
        el = E(eid="comp", role="edit", name="Compose box", value="", left=580, top=940, width=680, height=40)
        drawn = E(eid="t", role="text", name="remember the milk", value="", left=600, top=945, width=200, height=24)
        r = wa.read_composer(view([header("Rio"), el, drawn]))
        self.assertEqual(r["status"], "has_text")
        self.assertIn("remember the milk", r["text"])


class OutgoingVerification(unittest.TestCase):
    def test_new_bubble_and_empty_composer_is_sent(self):
        before = view([header("Rio"), composer(value="see you at 6")])
        after = view([header("Rio"), bubble("see you at 6"), composer()])
        self.assertTrue(wa.verify_outgoing_message(after, "see you at 6", before_view=before))

    def test_draft_still_in_composer_is_not_sent(self):
        before = view([header("Rio"), composer(value="see you at 6")])
        after = view([header("Rio"), composer(value="see you at 6")])
        self.assertFalse(wa.verify_outgoing_message(after, "see you at 6", before_view=before))

    def test_empty_unreadable_composer_without_bubble_is_not_sent(self):
        after = view([header("Rio"), E(eid="comp", role="edit", name="Compose box", value="", left=580,
                                       top=940, width=680, height=40)])
        self.assertFalse(wa.verify_outgoing_message(after, "see you at 6"))

    def test_older_identical_message_is_not_new_evidence(self):
        before = view([header("Rio"), bubble("ok", top=400), composer(value="ok")])
        after = view([header("Rio"), bubble("ok", top=400), composer()])
        self.assertFalse(wa.verify_outgoing_message(after, "ok", before_view=before))


class CallVerification(unittest.TestCase):
    idle = None

    def setUp(self):
        self.idle = view([header("Rio"),
                          E(eid="v", role="button", name="Voice call", value="", left=1780, top=100, width=40, height=32),
                          E(eid="vd", role="button", name="Video call", value="", left=1720, top=100, width=40, height=32),
                          composer()])

    def test_idle_header_buttons_are_not_a_call(self):
        res = wa.verify_call_started(self.idle, contact="Rio", before_view=None)
        self.assertFalse(res["started"])

    def test_voice_call_screen_verified(self):
        after = view([bubble("Rio Calling...", 300), E(eid="e", role="button", name="End call", value="", left=1, top=1, width=9, height=9)])
        res = wa.verify_call_started(after, contact="Rio", video=False, before_view=self.idle)
        self.assertTrue(res["started"], res)
        self.assertEqual(res["kind"], "voice")

    def test_video_screen_is_rejected_for_voice_request(self):
        after = view([bubble("Rio Calling...", 300),
                      E(eid="c", role="button", name="Turn off camera", value="", left=1, top=1, width=9, height=9),
                      E(eid="e", role="button", name="End call", value="", left=1, top=1, width=9, height=9)])
        res = wa.verify_call_started(after, contact="Rio", video=False, before_view=self.idle)
        self.assertFalse(res["started"])
        self.assertEqual(res["state"], "wrong_kind")
        self.assertTrue(wa.verify_call_started(after, contact="Rio", video=True, before_view=self.idle)["started"])

    def test_call_with_someone_else_is_rejected(self):
        after = view([bubble("Mario Calling...", 300),
                      E(eid="e", role="button", name="End call", value="", left=1, top=1, width=9, height=9)])
        res = wa.verify_call_started(after, contact="Rio", before_view=self.idle)
        self.assertFalse(res["started"])
        self.assertEqual(res["state"], "wrong_contact")


class SendFlow(unittest.TestCase):
    """_type_and_submit with scripted desktop views (no real desktop)."""

    def setUp(self):
        self.pressed = []
        self.typed = []
        self.drv = object.__new__(wa.WhatsAppDriver)
        self.idle = view([header("Rio"), composer()])

    def _run(self, views, message="see you at 6", dhash="h-send-1", gen=None):
        from core import task_control
        it = iter(views)
        with patch.object(wa.WhatsAppDriver, "_view", side_effect=lambda *a, **k: next(it)), \
             patch("core.screenact.type_text", side_effect=lambda t, **kw: self.typed.append(t) or SimpleNamespace(ok=True, error="")), \
             patch("core.screenact.press", side_effect=lambda k, **kw: self.pressed.append(k) or SimpleNamespace(ok=True, error="")), \
             patch("time.sleep"):
            return self.drv._type_and_submit(self.idle, "Rio", message, "", task_control.cancel_generation() if gen is None else gen, dhash)

    def test_happy_path_sends_and_verifies(self):
        staged = view([header("Rio"), composer(value="see you at 6")])
        after = view([header("Rio"), bubble("see you at 6"), composer()])
        ok, why = self._run([staged, after])
        self.assertTrue(ok, why)
        self.assertEqual(self.pressed, ["enter"])

    def test_enter_not_pressed_when_draft_unreadable(self):
        opaque = E(eid="comp", role="edit", name="Compose box", value="", left=580, top=940, width=680, height=40)
        ok, why = self._run([view([header("Rio"), opaque])])
        self.assertFalse(ok)
        self.assertEqual(self.pressed, [])

    def test_enter_not_pressed_when_draft_text_differs(self):
        staged = view([header("Rio"), composer(value="see you at 7")])
        ok, _ = self._run([staged])
        self.assertFalse(ok)
        self.assertEqual(self.pressed, [])

    def test_enter_not_pressed_when_chat_changed(self):
        staged = view([header("Mario"), composer(value="see you at 6")])
        ok, _ = self._run([staged])
        self.assertFalse(ok)
        self.assertEqual(self.pressed, [])

    def test_unconfirmed_send_reports_unverified_and_blocks_retry(self):
        staged = view([header("Rio"), composer(value="see you at 6")])
        still = view([header("Rio"), composer(value="see you at 6")])
        ok, why = self._run([staged, still], dhash="h-unconfirmed")
        self.assertFalse(ok)
        self.assertIn("unverified", why)
        self.assertIn("will not send it again", why)
        # the attempt was recorded when Enter was pressed, so a retry is blocked
        self.assertTrue(desktop_control.has_sent_attempt(receiver="Rio", draft_hash="h-unconfirmed", task_id=""))

    def test_cancel_before_enter_leaves_draft_and_presses_nothing(self):
        from core import task_control
        gen = task_control.cancel_generation()
        task_control.request_cancel("test")
        ok, why = self._run([view([header("Rio"), composer(value="see you at 6")])], gen=gen)
        self.assertFalse(ok)
        self.assertIn("cancelled", why)
        self.assertEqual(self.pressed, [])
        self.assertEqual(self.typed, [])


class DuplicatePrevention(unittest.TestCase):
    def test_attempt_recording_blocks_identical_resend(self):
        key = dict(receiver="Rio", draft_hash="abc123", task_id="")
        self.assertFalse(desktop_control.has_sent_attempt(**key))
        desktop_control.record_sent_attempt(**key)
        self.assertTrue(desktop_control.has_sent_attempt(**key))


if __name__ == "__main__":
    unittest.main()
