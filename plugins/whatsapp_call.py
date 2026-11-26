import time

from core import outcome
from core.appidentity import fold
from plugins._common import plugin, route, safe_run
from plugins._whatsapp_core import (
    extract_whatsapp_contacts,
    locate_whatsapp_call_button,
    locate_whatsapp_search_bar,
    observe,
    set_active_chat,
    verify_call_started,
    verify_selected_chat,
)

PLUGIN = plugin(
    "whatsapp_call",
    (
        "Inspect WhatsApp, verify a contact conversation, locate the voice or video call control, "
        "and initiate or end the call. When explicitly authorized by the user's direct voice command "
        "for a verified recipient, executes directly without asking for a second HUD confirmation."
    ),
    {
        "action": {"type": "STRING", "description": "inspect | request_call | call | end_call"},
        "contact": {"type": "STRING", "description": "Contact name or active chat to call"},
        "video": {"type": "BOOLEAN", "description": "True for video call, False/omitted for voice call"},
    },
    ["action"],
)


def _is_voice_authorized_call(contact: str, video: bool) -> tuple[bool, str]:
    try:
        from core import autonomous_mode, desktop_control
        if autonomous_mode.active_session() is not None:
            return False, ""
        ctx = desktop_control.current_bound_auth_context()
        if ctx is None or ctx.is_expired() or desktop_control.should_stop():
            return False, ""
        src = str(getattr(ctx, "source", "") or "").casefold()
        if src not in {"voice", "typed"}:
            return False, ""
        ok, _ = desktop_control.authorize_user_request(
            ctx.instruction, "whatsapp_call",
            {"action": "request_call", "contact": contact, "video": bool(video)},
            source=src, auth_context=ctx,
        )
        if not ok:
            return False, ""
        combined = " ".join([
            str(getattr(ctx, "instruction", "") or ""),
            str(getattr(ctx, "parent_instruction", "") or ""),
        ]).casefold()
        if not any(w in combined for w in ("call", "ring", "dial", "voice", "phone", "video")):
            return False, ""
        return True, str(getattr(ctx, "task_id", "") or "")
    except Exception:
        return False, ""


def _resolve_contact_for_call(view, contact: str):
    """Ensure the requested contact's WhatsApp chat is open & verified before locating call controls."""
    from core import desktop_control, screenact, screenview

    hdr = verify_selected_chat(view, expected_contact=contact)
    if hdr.get("verified"):
        return view, hdr, ""

    # Try locating unique contact row in currently visible search/chat pane
    contacts = extract_whatsapp_contacts(view, query=contact)
    if not contacts:
        sb = locate_whatsapp_search_bar(view)
        if sb is not None:
            pt_sb = screenact.Point(sb["x"], sb["y"], origin="element", label=sb["label"])
            mv = screenact.move_cursor(pt_sb)
            clk = screenact.click(pt_sb, expect_process=getattr(view, "app", "") or "")
            if mv.ok and clk.ok:
                screenact.type_text(contact, clear_first=True)
                time.sleep(0.35)
                view = screenview.look(budget_s=1.5, allow_ocr=True)
                contacts = extract_whatsapp_contacts(view, query=contact)

    if not contacts:
        return view, hdr, f"Could not find WhatsApp contact {contact!r}."
    exact = [c for c in contacts if fold(c["display_name"]) == fold(contact)]
    if len(contacts) > 1 and len(exact) != 1:
        names = ", ".join(c["display_name"] for c in contacts[:4])
        return view, hdr, f"ASK:I found {len(contacts)} WhatsApp contacts matching {contact!r}: {names}. Which one should I call?"
    chosen = exact[0] if len(exact) == 1 else contacts[0]
    pt_c = screenact.Point(chosen["point"][0], chosen["point"][1], origin="element", label=chosen["display_name"])
    screenact.move_cursor(pt_c)
    clk = screenact.click(pt_c, expect_process=getattr(view, "app", "") or "")
    if not clk.ok:
        return view, hdr, f"Could not open WhatsApp chat for {chosen['display_name']!r}: {clk.error}."
    time.sleep(0.35)
    after = screenview.look(budget_s=1.5, allow_ocr=True)
    hdr_after = verify_selected_chat(after, expected_contact=chosen["display_name"])
    desktop_control.set_active_target(
        target_type="contact", exact_target_name=chosen["display_name"],
        browser=("chrome" if "chrome" in fold(getattr(after, "app", "") or "") else "whatsapp"),
        point=chosen["point"], rect=chosen.get("rect"),
        results=contacts,
    )
    if not hdr_after.get("verified"):
        return after, hdr_after, f"Clicked {chosen['display_name']!r}, but the active chat header did not verify."
    return after, hdr_after, ""


def _execute_verified_call(contact: str, video: bool, player=None) -> str:
    from core import appidentity, desktop_control, screenact, screenview

    if desktop_control.should_stop():
        return outcome.failed("whatsapp_call", "computer control is paused or stopped", contact=contact)

    fg = appidentity.focus_window_by_app("WhatsApp")
    if not fg.get("activated"):
        fg = appidentity.focus_window_by_app("chrome", title_hint="WhatsApp")
    if not fg.get("activated"):
        screenact.focus_app("WhatsApp")

    fresh = screenview.look(budget_s=1.5, allow_ocr=True)
    if getattr(fresh, "degraded", False) or "whatsapp" not in fold((getattr(fresh, "app", "") or "") + " " + (getattr(fresh, "title", "") or "")):
        desktop_control.record_diagnostics(call_status="failed:window_unavailable", error_reason="WhatsApp window not accessible")
        return outcome.failed("whatsapp_call", "WhatsApp is not visible or accessible right now; no call was started.", contact=contact)

    fresh, hdr, resolve_err = _resolve_contact_for_call(fresh, contact)
    if resolve_err.startswith("ASK:"):
        desktop_control.record_diagnostics(call_status="ambiguous_contact")
        return outcome.ask("whatsapp_call", resolve_err[4:], contact=contact, verified=False)
    if resolve_err:
        desktop_control.record_diagnostics(call_status="failed:contact_unverified", error_reason=resolve_err[:80])
        return outcome.failed("whatsapp_call", resolve_err, contact=contact)

    btn = locate_whatsapp_call_button(fresh, video=video)
    kind_label = "video call" if video else "voice call"
    if btn is None:
        desktop_control.record_diagnostics(call_status=f"failed:no_{kind_label.replace(' ', '_')}_button", error_reason="missing call control")
        return outcome.failed(
            "whatsapp_call",
            f"I opened {hdr.get('active_contact') or contact}'s chat, but could not find the {kind_label} button; no call was started.",
            contact=hdr.get("active_contact") or contact,
        )

    pt = screenact.Point(btn["x"], btn["y"], origin="element", label=btn["label"])
    mv = screenact.move_cursor(pt)
    clk = screenact.click(pt, expect_process=getattr(fresh, "app", "") or "")
    if not clk.ok:
        desktop_control.record_diagnostics(call_status="failed:click_refused", error_reason=str(clk.error)[:80])
        return outcome.failed("whatsapp_call", f"clicking the {kind_label} button failed: {clk.error}", contact=contact)

    time.sleep(0.55)
    after_call = screenview.look(budget_s=1.5, allow_ocr=True)
    c_state = verify_call_started(after_call, contact=str(hdr.get("active_contact") or contact))
    set_active_chat(contact_name=str(hdr.get("active_contact") or contact), verified=True)
    if c_state.get("started"):
        desktop_control.record_diagnostics(
            call_status=f"started:{btn['kind']}",
            active_contact=str(hdr.get("active_contact") or contact),
            selected_chat_verified=True,
            exec_status="completed",
            verify_status="verified",
        )
        return outcome.done(
            "whatsapp_call",
            action="request_call",
            contact=str(hdr.get("active_contact") or contact),
            call_type=btn["kind"],
            button_label=btn["label"],
            call_state=c_state["state"],
            cursor_moved=mv.ok,
            click_executed=clk.ok,
            verified=True,
            detail=f"{hdr.get('active_contact') or contact}'s {kind_label} has started.",
        )

    desktop_control.record_diagnostics(
        call_status="unverified:no_call_ui",
        exec_status="completed",
        verify_status="unverified",
        error_reason="WhatsApp did not show an active call screen after click",
    )
    return outcome.unverified(
        "whatsapp_call",
        f"I clicked {btn['label']!r}, but WhatsApp did not confirm that the {kind_label} interface started.",
        action="request_call",
        contact=str(hdr.get("active_contact") or contact),
        call_type=btn["kind"],
        cursor_moved=mv.ok,
        click_executed=clk.ok,
        verified=False,
    )


def _run(p, player, memory):
    from core import desktop_control

    action = str(p.get("action") or "request_call").lower().strip()
    if action == "call":
        action = "request_call"
    if action not in ("inspect", "request_call", "end_call"):
        return "Choose inspect, request_call, or end_call."
    if action == "inspect":
        return observe(player)
    if action == "end_call":
        from core import screenview
        view = screenview.look(budget_s=1.2, allow_ocr=False)
        if view.degraded or "whatsapp" not in (view.app + " " + view.title).casefold():
            return "No accessible active WhatsApp call window was found; no control was clicked."
        names = [e.label.casefold() for e in view.elements if e.interactable]
        if not any("end call" in name or "hang up" in name for name in names):
            return "No visible end-call control was identified; call state is unverified and nothing was clicked."
        result = route(player, "screen_agent", {"action": "click", "target": "end call"})
        return f"End-call control action: {result}. This confirms the control interaction only; call disconnection is not independently verified."

    contact = str(p.get("contact") or "").strip()
    if not contact or fold(contact) in {"him", "her", "them", "it"}:
        snap_c = str(desktop_control.snapshot().get("active_contact") or "").strip()
        if snap_c and snap_c != "—":
            contact = snap_c
    if not contact:
        return "Which contact should I call, Tarun-kun?"

    video = bool(p.get("video", False))
    voice_direct, _tid = _is_voice_authorized_call(contact, video)
    if voice_direct:
        desktop_control.record_diagnostics(
            gui_confirm_required=False,
            gui_confirm_reason="explicit_voice_call",
            tool_name="whatsapp_call",
        )
        return _execute_verified_call(contact, video, player=player)

    from core import confirm
    desktop_control.record_diagnostics(
        gui_confirm_required=True,
        gui_confirm_reason="no_direct_voice_call_context",
        tool_name="whatsapp_call",
    )
    if confirm.pending_title():
        return "Another confirmation is pending; no call was started."
    return confirm.request(
        "whatsapp-call",
        f"Start {'video' if video else 'voice'} call with {contact}?",
        "Review the active WhatsApp conversation and confirm the contact before the call begins.",
        lambda: _execute_verified_call(contact, video, player=player),
    )


run = safe_run(_run)
