"""UI Automation driver used by actions.send_message for WhatsApp.

Every step starts from a fresh accessibility tree. Ambiguous contacts and
unavailable automation are refused; there is no coordinate or blind-typing
fallback in this driver.
"""
from __future__ import annotations

import time
from core.appidentity import fold


class WhatsAppDriver:
    def _view(self):
        from core import screenview
        return screenview.look(budget_s=1.5, allow_ocr=False)

    @staticmethod
    def _unique(items, what):
        items = list({e.eid: e for e in items}.values())
        if len(items) == 1: return items[0], ""
        if not items: return None, f"No accessible {what} was found."
        return None, f"More than one possible {what} was found; no action was taken."

    def send_message_to(self, receiver: str, message: str) -> tuple[bool, str]:
        from core import screenact, screenview, desktop_control
        receiver, message = str(receiver or "").strip(), str(message or "")
        if not receiver or not message: return False, "recipient and message are required"
        if desktop_control.should_stop(): return False, "computer control is paused or stopped"
        focused = screenact.focus_app("WhatsApp")
        if not focused.ok: return False, f"WhatsApp could not be focused: {focused.error}"
        view = self._view()
        if view.degraded or "whatsapp" not in fold(view.app + " " + view.title):
            return False, "WhatsApp is not exposing an accessible active window"
        edits = [e for e in view.elements if e.interactable and e.enabled and e.role in (screenview.ROLE_EDIT, screenview.ROLE_COMBOBOX) and not e.protected]
        searches = [e for e in edits if "search" in fold(e.label)]
        search, why = self._unique(searches or (edits if len(edits)==1 else []), "WhatsApp search field")
        if why: return False, why
        typed = screenact.type_text(receiver, target=search, clear_first=True)
        if not typed.ok: return False, f"could not search for the contact: {typed.error}"
        time.sleep(.25)
        view = self._view()
        if view.degraded or "whatsapp" not in fold(view.app + " " + view.title): return False, "WhatsApp changed during contact lookup"
        exact = [e for e in view.elements if e.interactable and e.enabled and fold(e.label) == fold(receiver)]
        contact, why = self._unique(exact, "exactly matching chat")
        if why: return False, why
        clicked = screenact.click(contact, expect_process=view.app)
        if not clicked.ok: return False, f"could not open the matching chat: {clicked.error}"
        time.sleep(.2)
        view = self._view()
        if view.degraded or "whatsapp" not in fold(view.app + " " + view.title): return False, "WhatsApp chat could not be verified after opening it"
        title_matches = fold(receiver) in fold(view.title)
        if not title_matches: return False, "active chat title does not match the requested contact; nothing was typed"
        fields = [e for e in view.elements if e.interactable and e.enabled and e.role == screenview.ROLE_EDIT and not e.protected and not any(x in fold(e.label) for x in ("search", "filter"))]
        editor, why = self._unique(fields, "message field")
        if why: return False, why
        entered = screenact.type_text(message, target=editor)
        if not entered.ok: return False, f"could not enter the message: {entered.error}"
        staged = self._view()
        if not any(message in (e.value or "") for e in staged.elements if e.role == screenview.ROLE_EDIT):
            return False, "message was not visible in the intended composer; it was not submitted"
        sent = screenact.press("enter")
        if not sent.ok: return False, f"message submission failed: {sent.error}"
        time.sleep(.25)
        after = self._view()
        empty = not any((e.value or "").strip() == message for e in after.elements if e.role == screenview.ROLE_EDIT)
        visible = any(message in (e.value or e.name or "") for e in after.elements)
        if after.app != view.app or not title_matches: return False, "active conversation changed during submission"
        if empty and visible: return True, "exact chat matched, message composer cleared, and sent text appeared in the conversation"
        return False, "message submission could not be verified from the updated accessibility tree"


def get():
    """Return the verified driver, even when optional UIA support is absent.

    Returning an unavailable driver (rather than None) prevents the caller from
    falling back to the legacy blind keyboard sequence.
    """
    try:
        from core import screenview
        caps = screenview.capabilities()
        if caps.get("os") == "windows" and not caps.get("accessibility_available"):
            return WhatsAppDriver(), "Windows UI Automation dependencies are unavailable; sending will be refused safely"
    except Exception:
        pass
    return WhatsAppDriver(), "UI Automation driver ready"


def observe(player=None):
    from plugins._common import route
    return route(player, "screen_agent", {"action": "observe"})


def send(player, contact: str, message: str) -> str:
    from plugins._common import route
    return route(player, "send_message", {"platform": "whatsapp", "receiver": contact,
                                           "message_text": message})
