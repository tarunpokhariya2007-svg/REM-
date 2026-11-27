import json
import subprocess
import sys
import threading
import time
from pathlib import Path

from core import outcome
from core.lazyimport import LazyModule, installed

# Imported on first USE, not at launch — see core/lazyimport.py. PAUSE is 0.06
# here rather than computer_control's 0.05, as it always was: typing into
# somebody else's chat window wants a little more room than a hotkey does.
_PYAUTOGUI = installed("pyautogui")
_PYPERCLIP = installed("pyperclip")


def _configure_pyautogui(module) -> None:
    module.FAILSAFE = True
    module.PAUSE    = 0.06


pyautogui = LazyModule("pyautogui", _configure_pyautogui)
pyperclip = LazyModule("pyperclip")

def _base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent

def _get_os() -> str:
    try:
        cfg = json.loads(
            (_base_dir() / "config" / "api_keys.json").read_text(encoding="utf-8")
        )
        return cfg.get("os_system", "windows").lower()
    except Exception:
        return "windows"


def _require_pyautogui():
    if not _PYAUTOGUI:
        raise RuntimeError("PyAutoGUI not installed. Run: pip install pyautogui")


def _paste_text(text: str) -> None:
    _require_pyautogui()

    os_name = _get_os()
    paste_hotkey = ("command", "v") if os_name == "mac" else ("ctrl", "v")

    if _PYPERCLIP:
        pyperclip.copy(text)
        time.sleep(0.15)
        pyautogui.hotkey(*paste_hotkey)
        time.sleep(0.1)
    else:
        pyautogui.write(text, interval=0.03)


def _clear_and_paste(text: str) -> None:
    _require_pyautogui()
    os_name = _get_os()
    select_all = ("command", "a") if os_name == "mac" else ("ctrl", "a")
    pyautogui.hotkey(*select_all)
    time.sleep(0.1)
    pyautogui.press("delete")
    time.sleep(0.1)
    _paste_text(text)

def _open_app(app_name: str) -> bool:
    _require_pyautogui()
    os_name = _get_os()

    try:
        if os_name == "windows":
            pyautogui.press("win")
            time.sleep(0.5)
            _paste_text(app_name)
            time.sleep(0.6)
            pyautogui.press("enter")
            time.sleep(2.5)
            return True

        elif os_name == "mac":
            result = subprocess.run(
                ["open", "-a", app_name],
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=10,
            )
            if result.returncode != 0:
                result = subprocess.run(
                    ["open", "-a", f"{app_name}.app"],
                    capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=10,
                )
            time.sleep(2.5)
            return result.returncode == 0

        else: 
            launched = False
            for launcher in [
                ["gtk-launch", app_name.lower()],
                [app_name.lower()],
            ]:
                try:
                    subprocess.Popen(
                        launcher,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                    launched = True
                    break
                except FileNotFoundError:
                    continue
            time.sleep(2.5)
            return launched

    except Exception as e:
        print(f"[SendMessage] ⚠️ Could not open {app_name}: {e}")
        return False


def _open_browser_url(url: str) -> bool:
    import webbrowser
    try:
        webbrowser.open(url)
        time.sleep(4.0) 
        return True
    except Exception as e:
        print(f"[SendMessage] ⚠️ Could not open browser: {e}")
        return False

def _search_in_app(query: str) -> None:
    _require_pyautogui()
    os_name = _get_os()
    search_hotkey = ("command", "f") if os_name == "mac" else ("ctrl", "f")

    pyautogui.hotkey(*search_hotkey)
    time.sleep(0.5)
    _clear_and_paste(query)
    time.sleep(1.0)

def _desktop_send(app_name: str, receiver: str, message: str) -> str:
    from core import desktop_control
    if desktop_control.should_stop():
        return outcome.failed("send_message", "computer control was stopped; no message was sent",
                              platform=app_name, to=receiver)
    if not _open_app(app_name):
        return outcome.failed(
            "send_message", f"{app_name} could not be opened, so nothing was "
            f"typed and no message was sent",
            platform=app_name, to=receiver,
        )

    time.sleep(1.0)
    if desktop_control.should_stop():
        return outcome.failed("send_message", "computer control was stopped before contact search; no message was sent",
                              platform=app_name, to=receiver)
    _search_in_app(receiver)
    if desktop_control.should_stop():
        return outcome.failed("send_message", "computer control was stopped before opening the conversation; no message was sent",
                              platform=app_name, to=receiver)
    pyautogui.press("enter")
    time.sleep(0.8)

    _paste_text(message)
    time.sleep(0.2)
    if desktop_control.should_stop():
        return outcome.failed("send_message", "computer control was stopped; the draft was not sent",
                              platform=app_name, to=receiver)
    pyautogui.press("enter")
    time.sleep(0.3)

    # UNVERIFIED, AND THAT IS THE HONEST ANSWER.
    #
    # Everything above is keystrokes with sleeps between them: press Win, type
    # the app name, Ctrl+F, type the contact, Enter, paste, Enter. Nothing
    # reads back which window received any of it. On a machine one second
    # slower than these sleeps assume, the whole sequence lands somewhere else
    # — and the old code still returned "Message sent to X via Y."
    #
    # There is no read-back to add here without a driver per application, and
    # `_send_whatsapp` shows what that costs to build. Until one exists for a
    # given app, the truthful thing is to say the keystrokes went out and the
    # result is unconfirmed, which is something the user can glance at their
    # screen and check.
    return outcome.unverified(
        "send_message",
        f"the message was typed into {app_name} blind - no confirmation is "
        f"available for this app, so it is NOT known to have been sent",
        platform=app_name, to=receiver,
    )

def _send_whatsapp(receiver: str, message: str, *, task_id: str = "") -> str:
    """WhatsApp goes through the verified driver when there is one."""
    try:
        from plugins import _whatsapp_core as wa
    except Exception as e:
        print(f"[SendMessage] WhatsApp driver unavailable ({e}) — typing blind.")
        return _desktop_send("WhatsApp", receiver, message)

    try:
        transport, why = wa.get()
    except Exception as e:
        print(f"[SendMessage] WhatsApp driver failed to start ({e}) — typing blind.")
        return _desktop_send("WhatsApp", receiver, message)

    if transport is None:
        print(f"[SendMessage] {why} — typing blind.")
        return _desktop_send("WhatsApp", receiver, message)

    from core import desktop_control
    if desktop_control.should_stop():
        return outcome.failed("send_message", "computer control was stopped; no message was sent",
                              platform="WhatsApp", to=receiver)
    dhash = wa.draft_hash(message)
    ctx = desktop_control.current_bound_auth_context()
    eff_tid = str(task_id or (getattr(ctx, "task_id", "") if ctx is not None else "") or "")
    ok_send, deny_reason, _conf = desktop_control.is_send_authorized(
        receiver=receiver,
        draft_hash=dhash,
        composition_task_id=eff_tid,
        utterance=str(getattr(ctx, "instruction", "") if ctx is not None else ""),
        auth_context=ctx,
    )
    if not ok_send and ctx is not None:
        return outcome.failed("send_message", deny_reason, platform="WhatsApp", to=receiver)

    sent, failure = transport.send_message_to(receiver, message, task_id=eff_tid)
    if sent:
        return outcome.done("send_message", platform="WhatsApp",
                            to=receiver, verified=True, sending_status="sent",
                            lifecycle_state=desktop_control.MSG_SEND_VERIFIED,
                            bubble_verified=True)
    if "unverified" in str(failure or "").casefold():
        return outcome.unverified(
            "send_message",
            "I attempted to send the message, but WhatsApp has not confirmed that it was sent.",
            platform="WhatsApp", to=receiver,
            lifecycle_state=desktop_control.MSG_SEND_UNVERIFIED,
            verified=False,
        )
    return outcome.failed("send_message", failure,
                          platform="WhatsApp", to=receiver)

def _send_telegram(receiver: str, message: str) -> str:
    return _desktop_send("Telegram", receiver, message)

def _send_signal(receiver: str, message: str) -> str:
    return _desktop_send("Signal", receiver, message)


def _send_discord(receiver: str, message: str) -> str:
    return _desktop_send("Discord", receiver, message)


def _send_instagram(receiver: str, message: str) -> str:
    _require_pyautogui()

    if not _open_browser_url("https://www.instagram.com/direct/new/"):
        return outcome.failed(
            "send_message", "Instagram could not be opened in the browser, so "
            "nothing was sent", platform="Instagram", to=receiver)

    _paste_text(receiver)
    time.sleep(1.5)

    pyautogui.press("down")
    time.sleep(0.3)
    pyautogui.press("enter")   
    time.sleep(0.4)

    for _ in range(4):
        pyautogui.press("tab")
        time.sleep(0.15)
    pyautogui.press("enter")
    time.sleep(2.0)

    _paste_text(message)
    time.sleep(0.2)
    pyautogui.press("enter")
    time.sleep(0.3)

    return outcome.unverified(
        "send_message",
        "the message was typed into Instagram in the browser blind - the page "
        "was never read back, so it is NOT known to have been sent",
        platform="Instagram", to=receiver)


def _send_messenger(receiver: str, message: str) -> str:
    _require_pyautogui()

    if not _open_browser_url("https://www.messenger.com/"):
        return outcome.failed(
            "send_message", "Messenger could not be opened in the browser, so "
            "nothing was sent", platform="Messenger", to=receiver)


    _search_in_app(receiver)
    time.sleep(0.5)
    pyautogui.press("down")
    time.sleep(0.3)
    pyautogui.press("enter")
    time.sleep(1.0)

    _paste_text(message)
    time.sleep(0.2)
    pyautogui.press("enter")
    time.sleep(0.3)

    return outcome.unverified(
        "send_message",
        "the message was typed into Messenger in the browser blind - the page "
        "was never read back, so it is NOT known to have been sent",
        platform="Messenger", to=receiver)

_PLATFORM_MAP = [
    ({"whatsapp", "wp", "wapp"},              _send_whatsapp),
    ({"telegram", "tg"},                      _send_telegram),
    ({"instagram", "ig", "insta"},            _send_instagram),
    ({"signal"},                               _send_signal),
    ({"discord"},                              _send_discord),
    ({"messenger", "facebook", "fb"},         _send_messenger),
]


def _resolve_platform(platform_str: str):
    key = platform_str.lower().strip()
    for keywords, handler in _PLATFORM_MAP:
        if any(k in key for k in keywords):
            return handler
    return lambda r, m: _desktop_send(platform_str.strip().title(), r, m)


def _is_voice_authorized_send(receiver: str, platform: str, *, message_text: str = "") -> tuple[bool, str]:
    """Return True when an active, explicit voice/typed task clearly authorized sending without GUI confirm."""
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
        ok, _reason = desktop_control.authorize_user_request(
            ctx.instruction, "send_message",
            {"receiver": receiver, "platform": platform, "message_text": message_text},
            source=src, auth_context=ctx,
        )
        if not ok:
            return False, ""
        import hashlib
        dhash = hashlib.sha256(str(message_text or "").encode("utf-8", errors="ignore")).hexdigest()[:16] if message_text else ""
        comp_tid = str(getattr(ctx, "browser_task_id", "") or getattr(ctx, "task_id", "") or "")
        ok_send, _deny, _conf = desktop_control.is_send_authorized(
            receiver=receiver,
            draft_hash=dhash,
            composition_task_id=comp_tid,
            utterance=str(getattr(ctx, "instruction", "") or ""),
            auth_context=ctx,
            check_duplicate=False,
        )
        if not ok_send:
            return False, ""
        return True, str(getattr(ctx, "task_id", "") or "")
    except Exception:
        return False, ""


def send_message(
    parameters: dict,
    response=None,
    player=None,
    session_memory=None,
) -> str:
    params       = parameters or {}
    receiver     = params.get("receiver", "").strip()
    message_text = params.get("message_text", "").strip()
    platform     = params.get("platform", "whatsapp").strip()

    if not receiver:
        try:
            from core import desktop_control
            snap = desktop_control.snapshot()
            candidate = str(snap.get("active_contact") or "").strip()
            if candidate and candidate != "—":
                receiver = candidate
        except Exception:
            pass
    if not receiver:
        return outcome.failed("send_message", "no recipient was given")
    if not message_text:
        try:
            from core import desktop_control
            comp = desktop_control.get_active_composition_state() or {}
            message_text = str(comp.get("generated_content") or "").strip()
        except Exception:
            pass
    if not message_text:
        return outcome.failed("send_message", "no message text was given",
                              to=receiver)
    if not _PYAUTOGUI:
        return outcome.failed(
            "send_message",
            "PyAutoGUI is not installed, so the desktop cannot be driven - "
            "run: pip install pyautogui",
            to=receiver, platform=platform,
        )

    voice_direct, task_id = _is_voice_authorized_send(receiver, platform, message_text=message_text)
    if voice_direct:
        try:
            from core import desktop_control
            desktop_control.record_diagnostics(
                gui_confirm_required=False,
                gui_confirm_reason="explicit_voice_send",
                tool_name="send_message",
            )
        except Exception:
            pass
        if platform.casefold() in {"whatsapp", "wp", "wapp"}:
            try:
                from core import desktop_control
                with desktop_control.action(f"send_message:{platform} to {receiver}"):
                    res = _send_whatsapp(receiver, message_text, task_id=task_id)
                    desktop_control.report(f"send_message:{platform}", str(res)[:140])
                    if player:
                        player.set_desktop_control_status(desktop_control.snapshot())
                    return res
            except Exception as exc:
                return outcome.failed("send_message", f"voice-authorized send failed: {exc}", platform=platform, to=receiver)
        return _start_confirmed_send(player, platform, receiver, message_text)

    # Fallback HUD gate when invoked without an unambiguous direct voice authorization
    try:
        from core import confirm, desktop_control
        desktop_control.record_diagnostics(
            gui_confirm_required=True,
            gui_confirm_reason="no_direct_voice_send_context",
            tool_name="send_message",
        )
        if confirm.pending_title():
            return outcome.failed("send_message",
                                  "another confirmation is already waiting; no message was sent",
                                  platform=platform, to=receiver)
        preview = message_text[:180] + ("…" if len(message_text) > 180 else "")
        pending = confirm.request(
            key="send-message",
            title=f"Send message to {receiver} via {platform}?",
            detail=f"Review the recipient and message before approving:\n{preview}",
            run=lambda: _start_confirmed_send(player, platform, receiver, message_text),
        )
        return pending
    except Exception as exc:
        return outcome.failed("send_message",
                              f"could not request user confirmation ({exc}); no message was sent",
                              platform=platform, to=receiver)


def _start_confirmed_send(player, platform: str, receiver: str,
                          message_text: str) -> str:
    preview = message_text[:50] + ("…" if len(message_text) > 50 else "")
    print(f"[SendMessage] 📨 {platform} → {receiver}: {preview}")
    if player:
        player.write_log(f"[msg] {platform} → {receiver}")
    threading.Thread(
        target=_send_and_report,
        args=(player, platform, receiver, message_text),
        daemon=True, name="send-message",
    ).start()
    return outcome.started("send_message", platform=platform, to=receiver)


def _send_and_report(player, platform: str, receiver: str,
                     message_text: str) -> None:
    """Do the sending, then say what actually happened — including when it did
    not happen, and including when nobody can tell.

    Every path here reports. A tool that says `started` and then goes quiet on
    failure has only moved the false success one step later, which is the whole
    thing this rewrite exists to stop.
    """
    try:
        from core import desktop_control
        with desktop_control.action(f"send_message:{platform} to {receiver}"):
            result = _resolve_platform(platform)(receiver, message_text)
            desktop_control.report(f"send_message:{platform}", str(result)[:140])
            if player:
                player.set_desktop_control_status(desktop_control.snapshot())
    except Exception as exc:
        print(f"[SendMessage] {type(exc).__name__}: {exc}")
        result = outcome.failed("send_message", f"it failed: {exc}",
                                to=receiver, platform=platform)

    # A prefix test on a fixed vocabulary, not a search for the word "sent".
    # The old check was `"sent" in text and "not sent" not in text`, which is
    # one careless rewording away from putting a tick next to a message that
    # never went.
    verified = result.startswith(f"status={outcome.DONE} ")

    # The full status line goes to the TERMINAL, where a developer reads it.
    # The activity log beside the assistant's own sentences is for the
    # conversation and for things that went wrong — a line reading
    # `status=done action=send_message verified=true` there is machinery
    # showing through, and it was reported as exactly that.
    print(f"[SendMessage] {'✅' if verified else '⚠️'} {result[:160]}")
    if player and outcome.is_problem(result):
        player.write_log(f"[msg] could not confirm the message to {receiver} — "
                         f"see the terminal")

    if verified:
        # It went, and the user asked for it to go. Announcing a success they
        # already expect is noise — the log and the panel carry the record.
        return

    if player and hasattr(player, "request_say"):
        try:
            player.request_say(
                "Tell the user, in one or two short sentences in their own "
                "language, exactly what this says happened — and if it says "
                "the message was not sent or not confirmed, say that plainly "
                "rather than claiming it was sent: " + result
            )
        except Exception:
            pass


# ── Tool declaration (auto-discovered by core/action_loader.py) ──────────────
TOOL = {
    "name": "send_message",
    "description": (
        "Sends a text message via WhatsApp, Telegram, or another messaging "
        "platform. Write 'message_text' in the USER'S OWN LANGUAGE, or pass the "
        "verified active draft ONLY after the user clearly says 'Yes, send it' or 'Send the message'. "
        "On status=done verified=True report that the message was verified sent; on "
        "status=unverified say exactly 'I attempted to send the message, but WhatsApp has not confirmed that it was sent.'; "
        "never report success unless outgoing bubble evidence is verified."
    ),
    "tier": "core",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "receiver": {
                "type": "STRING",
                "description": "Recipient contact name"
            },
            "message_text": {
                "type": "STRING",
                "description": "The message to send"
            },
            "platform": {
                "type": "STRING",
                "description": "Platform: WhatsApp, Telegram, etc."
            }
        },
        "required": [
            "receiver",
            "message_text",
            "platform"
        ]
    },
    "handler": send_message,
}
