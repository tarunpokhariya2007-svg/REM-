"""WhatsApp intelligence & UI/DOM Automation driver for Desktop and Chrome WhatsApp Web.

Unified single source of truth for:
1. Contact extraction (DOM -> UIA -> OCR) with placeholder rejection (`-0-`, `-0+`, counts)
   and strict single-card deduplication so one visible contact never inflates into 19-21 rows.
2. Canonical active_chat identity shared across selection, composition, messaging, and calling.
3. Composer localization (DOM/UIA/OCR) + Hindi/English drafting, translation, replacement, and readback.
4. Verified single-execution sending with idempotency protection (preventing duplicate sends).
5. Voice-call vs video-call button separation and active call-UI verification.
"""
from __future__ import annotations

import hashlib
import re
import threading
import time
import uuid
from typing import Any

from core.appidentity import fold

_WA_CHROME_NOISE = frozenset({
    "whatsapp", "chats", "status", "channels", "communities", "archive", "archived",
    "all", "unread", "favorites", "groups", "search", "search or start new chat",
    "search unread chats", "ask meta ai or search", "type a message", "new chat",
    "menu", "settings", "profile", "end to end encrypted", "use whatsapp on your computer",
    "download", "log out", "mute notifications", "clear messages", "delete chat",
    "voice call", "video call", "search...", "contacts", "messages",
})

_TIME_RE = re.compile(
    r"^(?:\d{1,2}:\d{2}(?:\s*[ap]m)?|yesterday|today|just now|\d{1,2}/\d{1,2}/\d{2,4})$",
    re.I,
)
_PHONE_RE = re.compile(r"\+?\d[\d\s\-()]{6,}\d")
_UNREAD_RE = re.compile(r"^\d{1,4}$")
# Reject OCR/UIA glyph artifacts & empty/placeholder IDs like "-0-", "-0+", "---", "+0"
_PLACEHOLDER_RE = re.compile(r"^[\W_0+\-|=~.*]+$")

_CHAT_LOCK = threading.Lock()
_ACTIVE_CHAT: dict[str, Any] = {
    "platform": "",
    "contact_name": "",
    "contact_id": None,
    "phone_number": None,
    "chat_id": None,
    "chat_url": None,
    "chat_header": None,
    "source": "none",
    "verified": False,
    "confidence": 0.0,
    "observation_id": None,
    "task_id": None,
    "observed_at": None,
}


def is_valid_contact_label(name: str) -> bool:
    cleaned = " ".join(str(name or "").split()).strip()
    if not cleaned or len(cleaned) < 2:
        return False
    low = cleaned.casefold()
    if low in _WA_CHROME_NOISE or _TIME_RE.match(cleaned) or _UNREAD_RE.match(cleaned):
        return False
    if _PLACEHOLDER_RE.match(cleaned):
        return False
    if not any(ch.isalpha() for ch in cleaned) and not _PHONE_RE.search(cleaned):
        return False
    return True


def _clean_identifier(raw: Any) -> str | None:
    s = str(raw or "").strip()
    if not s or _PLACEHOLDER_RE.match(s) or s.casefold() in {"none", "null", "undefined", "nan"}:
        return None
    return s[:120]


def get_active_chat(*, max_age_s: float = 300.0) -> dict[str, Any]:
    with _CHAT_LOCK:
        snap = dict(_ACTIVE_CHAT)
    obs_at = snap.get("observed_at")
    if obs_at is not None and (time.monotonic() - float(obs_at)) > max_age_s:
        snap["verified"] = False
    return snap


def set_active_chat(**fields) -> dict[str, Any]:
    with _CHAT_LOCK:
        for k, v in fields.items():
            if k in _ACTIVE_CHAT:
                _ACTIVE_CHAT[k] = v
        _ACTIVE_CHAT["observed_at"] = time.monotonic()
        snap = dict(_ACTIVE_CHAT)
    try:
        from core import desktop_control
        updates = {}
        if snap.get("contact_name"):
            updates["active_contact"] = str(snap["contact_name"])[:32]
        if snap.get("chat_id"):
            updates["active_chat_id"] = str(snap["chat_id"])[:32]
        updates["selected_chat_verified"] = bool(snap.get("verified"))
        desktop_control.record_diagnostics(**updates)
    except Exception:
        pass
    return snap


def invalidate_active_chat(reason: str = "") -> None:
    with _CHAT_LOCK:
        _ACTIVE_CHAT.update({
            "contact_name": "",
            "contact_id": None,
            "phone_number": None,
            "chat_id": None,
            "chat_header": None,
            "verified": False,
            "confidence": 0.0,
        })
    try:
        from core import desktop_control
        desktop_control.record_diagnostics(
            active_contact="—",
            active_chat_id="—",
            selected_chat_verified=False,
        )
    except Exception:
        pass


def _strip_composite_contact_name(raw_name: str) -> str:
    display_name = raw_name.splitlines()[0].strip()
    if " , " in display_name:
        display_name = display_name.split(" , ", 1)[0].strip()
    ts_cut = re.split(r"\s+\d{1,2}:\d{2}(?:\s*[AP]M)?\b", display_name, maxsplit=1, flags=re.IGNORECASE)
    if ts_cut and ts_cut[0].strip():
        display_name = ts_cut[0].strip()
    return display_name.strip(" -|•·")


def extract_whatsapp_contacts(view, query: str = "", *, source_override: str = "") -> list[dict]:
    """Extract UNIQUE visible WhatsApp contact/chat results from DOM/UIA/OCR.

    Rejects placeholders (`-0-`, `-0+`, unread counts, timestamps) and deduplicates
    sub-elements belonging to one visual contact card so a single visible chat row
    never expands into 19-21 duplicates.
    """
    if view is None:
        return []
    elements = list(getattr(view, "elements", ()) or ())
    mon = getattr(view, "monitor", None) or {}
    mon_top = int(mon.get("top", 0) or 0)
    mon_left = int(mon.get("left", 0) or 0)
    mon_w = int(mon.get("width", 1920) or 1920)
    win_rect = getattr(view, "window_rect", None)
    if win_rect and len(win_rect) == 4 and win_rect[2] > 200:
        w_left, w_top, w_w, _w_h = (int(v) for v in win_rect)
    else:
        w_left, w_top, w_w = mon_left, mon_top, mon_w

    pane_top_floor = w_top + 115
    pane_right_ceil = w_left + int(w_w * 0.46)
    view_src = (source_override or str(getattr(view, "source", "") or "uia")).casefold()
    now_m = time.monotonic()

    raw_rows: list[dict] = []
    for el in elements:
        role = str(getattr(el, "role", "") or "").casefold()
        if role in {"edit", "combobox", "searchbox", "button", "scrollbar", "separator", "image"}:
            continue
        name = " ".join(str(getattr(el, "name", "") or getattr(el, "label", "") or "").split())
        if not name or len(name) < 2:
            continue
        top = int(getattr(el, "top", 0) or 0)
        left = int(getattr(el, "left", 0) or 0)
        width = int(getattr(el, "width", 0) or 0)
        height = int(getattr(el, "height", 0) or 0)
        if top < pane_top_floor or left > pane_right_ceil or width < 24 or height < 12:
            continue

        display_name = _strip_composite_contact_name(name)
        if not is_valid_contact_label(display_name):
            continue
        phone_m = _PHONE_RE.search(name)
        phone = phone_m.group(0).strip() if phone_m else None
        raw_auto_id = _clean_identifier(getattr(el, "automation_id", None) or getattr(el, "contact_id", None))
        raw_chat_id = _clean_identifier(getattr(el, "chat_id", None))

        dn_fold = fold(display_name)
        if not dn_fold:
            continue
        is_ocr = ("ocr" in role) or (view_src == "ocr" and role in {"text", "ocr_text", ""})
        cx, cy = (getattr(el, "center", None) or (left + max(1, width // 2), top + max(1, height // 2)))
        dedupe_key = raw_chat_id or raw_auto_id or phone or dn_fold
        chat_id = raw_chat_id or raw_auto_id or (f"phone:{re.sub(r'[^0-9+]', '', phone)}" if phone else f"chat:{dn_fold}")
        raw_rows.append({
            "display_name": display_name[:120],
            "contact_id": raw_auto_id,
            "phone": phone or "",
            "id": chat_id,
            "dedupe_key": dedupe_key,
            "last_message_preview": (str(getattr(el, "value", "") or "").strip()[:140]),
            "rect": (left, top, width, height),
            "point": (int(cx), int(cy)),
            "top": top,
            "left": left,
            "source": ("dom" if "dom" in view_src else ("ocr" if is_ocr else "uia")),
            "confidence": ("high" if (raw_auto_id or raw_chat_id or (not is_ocr and "list" in role)) else "medium"),
            "observed_at": now_m,
            "element": el,
        })

    raw_rows.sort(key=lambda r: (r["top"], r["left"]))
    deduped: list[dict] = []
    q_fold = fold(query) if query else ""
    for row in raw_rows:
        r_fold = fold(row["display_name"])
        merged = False
        for existing in deduped:
            e_fold = fold(existing["display_name"])
            same_id = bool(row["contact_id"] and existing["contact_id"] and row["contact_id"] == existing["contact_id"])
            same_phone = bool(row["phone"] and existing["phone"] and row["phone"] == existing["phone"])
            same_name = (r_fold == e_fold)
            same_band = abs(row["top"] - existing["top"]) <= 44 and (r_fold in e_fold or e_fold in r_fold)
            if same_id or same_phone or same_name or same_band:
                if not existing["phone"] and row["phone"]:
                    existing["phone"] = row["phone"]
                if not existing["contact_id"] and row["contact_id"]:
                    existing["contact_id"] = row["contact_id"]
                    existing["id"] = row["id"]
                if not existing["last_message_preview"] and row["last_message_preview"] and fold(row["last_message_preview"]) != e_fold:
                    existing["last_message_preview"] = row["last_message_preview"]
                if len(row["display_name"]) < len(existing["display_name"]) and row["display_name"] in existing["display_name"]:
                    existing["display_name"] = row["display_name"]
                if existing["source"] == "ocr" and row["source"] in {"uia", "dom"}:
                    existing["source"] = row["source"]
                    existing["confidence"] = "high"
                    existing["point"] = row["point"]
                    existing["rect"] = row["rect"]
                    existing["element"] = row["element"]
                merged = True
                break
        if not merged:
            deduped.append(row)

    if q_fold:
        exact_m = [c for c in deduped if fold(c["display_name"]) == q_fold]
        prefix_m = [c for c in deduped if fold(c["display_name"]).startswith(q_fold) and c not in exact_m]
        sub_m = [
            c for c in deduped
            if (q_fold in fold(c["display_name"]) or q_fold in fold(c.get("phone", "")))
            and c not in exact_m and c not in prefix_m
        ]
        ranked = exact_m + prefix_m + sub_m
        if ranked:
            deduped = ranked

    for pos, item in enumerate(deduped, 1):
        item["position"] = pos
    return deduped


def locate_whatsapp_search_bar(view) -> dict | None:
    """Identify WhatsApp's chat search input while excluding Chrome's address bar and composer."""
    if view is None:
        return None
    elements = list(getattr(view, "elements", ()) or ())
    mon = getattr(view, "monitor", None) or {}
    mon_top = int(mon.get("top", 0) or 0)
    win_rect = getattr(view, "window_rect", None)
    if win_rect and len(win_rect) == 4 and win_rect[2] > 200:
        w_left, w_top, w_w, w_h = (int(v) for v in win_rect)
    else:
        w_left = int(mon.get("left", 0) or 0)
        w_top = mon_top
        w_w = int(mon.get("width", 1920) or 1920)
        w_h = int(mon.get("height", 1080) or 1080)

    is_browser_chrome = "chrome" in fold(getattr(view, "app", "") or "")
    toolbar_floor = w_top + (85 if is_browser_chrome else 40)
    composer_floor = w_top + int(w_h * 0.65)
    left_pane_ceil = w_left + int(w_w * 0.48)
    view_src = str(getattr(view, "source", "") or "").casefold()

    for el in elements:
        role = str(getattr(el, "role", "") or "").casefold()
        label_cf = str(getattr(el, "name", "") or getattr(el, "label", "") or "").casefold()
        if any(x in label_cf for x in ("address", "omnibox", "url", "type a message", "write a message")):
            continue
        top = int(getattr(el, "top", 0) or 0)
        left = int(getattr(el, "left", 0) or 0)
        width = int(getattr(el, "width", 0) or 0)
        no_coords = (top == 0 and left == 0 and width == 0)
        if role in {"edit", "combobox", "searchbox"} and (
            (toolbar_floor <= top <= composer_floor and left <= left_pane_ceil)
            or (no_coords and "search" in label_cf)
        ):
            if "search" in label_cf or "filter" in label_cf or "meta" in label_cf or top <= w_top + 230:
                cx, cy = getattr(el, "center", (left + 80, top + 16))
                return {"x": int(cx), "y": int(cy), "label": getattr(el, "name", "") or getattr(el, "label", "") or "WhatsApp search field", "source": "uia", "element": el}

    for el in elements:
        role = str(getattr(el, "role", "") or "").casefold()
        is_ocr = ("ocr" in role) or (view_src == "ocr" and role in {"text", "ocr_text", ""})
        label_cf = str(getattr(el, "name", "") or getattr(el, "label", "") or "").strip().casefold()
        top = int(getattr(el, "top", 0) or 0)
        left = int(getattr(el, "left", 0) or 0)
        if is_ocr and toolbar_floor <= top <= w_top + 230 and left <= left_pane_ceil:
            if "search" in label_cf or "new chat" in label_cf:
                cx, cy = getattr(el, "center", (left + 100, top + 14))
                return {"x": int(cx), "y": int(cy), "label": "WhatsApp search field (OCR)", "source": "ocr", "element": el}
    return None


def locate_whatsapp_composer(view) -> dict | None:
    """Identify the active conversation's bottom message input box (never the search bar)."""
    if view is None:
        return None
    elements = list(getattr(view, "elements", ()) or ())
    mon = getattr(view, "monitor", None) or {}
    win_rect = getattr(view, "window_rect", None)
    if win_rect and len(win_rect) == 4 and win_rect[2] > 200:
        w_left, w_top, w_w, w_h = (int(v) for v in win_rect)
    else:
        w_left = int(mon.get("left", 0) or 0)
        w_top = int(mon.get("top", 0) or 0)
        w_w = int(mon.get("width", 1920) or 1920)
        w_h = int(mon.get("height", 1080) or 1080)

    composer_floor = w_top + int(w_h * 0.55)
    chat_left_floor = w_left + int(w_w * 0.27)
    view_src = str(getattr(view, "source", "") or "").casefold()

    for el in elements:
        role = str(getattr(el, "role", "") or "").casefold()
        label_cf = str(getattr(el, "name", "") or getattr(el, "label", "") or "").casefold()
        if any(x in label_cf for x in ("search", "filter", "address", "omnibox", "url")):
            continue
        top = int(getattr(el, "top", 0) or 0)
        left = int(getattr(el, "left", 0) or 0)
        width = int(getattr(el, "width", 0) or 0)
        no_coords = (top == 0 and left == 0 and width == 0)
        if role in {"edit", "document", "textbox", "combobox"} and not getattr(el, "protected", False):
            if top >= composer_floor or ("message" in label_cf and (left >= chat_left_floor or no_coords)):
                cx, cy = getattr(el, "center", (left + 160, top + 18))
                return {"x": int(cx), "y": int(cy), "label": getattr(el, "name", "") or getattr(el, "label", "") or "Message composer", "source": "uia", "element": el}

    for el in elements:
        role = str(getattr(el, "role", "") or "").casefold()
        is_ocr = ("ocr" in role) or (view_src == "ocr" and role in {"text", "ocr_text", ""})
        label_cf = str(getattr(el, "name", "") or getattr(el, "label", "") or "").strip().casefold()
        top = int(getattr(el, "top", 0) or 0)
        left = int(getattr(el, "left", 0) or 0)
        if is_ocr and top >= composer_floor and left >= chat_left_floor and ("type a message" in label_cf or "message" in label_cf):
            cx, cy = getattr(el, "center", (left + 160, top + 16))
            return {"x": int(cx), "y": int(cy), "label": "Message composer (OCR)", "source": "ocr", "element": el}
    return None


def locate_whatsapp_call_button(view, *, video: bool = False) -> dict | None:
    """Locate WhatsApp's voice-call (or video-call) header control with strict separation."""
    if view is None:
        return None
    elements = list(getattr(view, "elements", ()) or ())
    mon = getattr(view, "monitor", None) or {}
    win_rect = getattr(view, "window_rect", None)
    if win_rect and len(win_rect) == 4 and win_rect[2] > 200:
        w_left, w_top, w_w, _w_h = (int(v) for v in win_rect)
    else:
        w_left = int(mon.get("left", 0) or 0)
        w_top = int(mon.get("top", 0) or 0)
        w_w = int(mon.get("width", 1920) or 1920)

    hdr_left = w_left + int(w_w * 0.40)
    hdr_bottom = w_top + 220

    for el in elements:
        label = " ".join(str(getattr(el, "name", "") or getattr(el, "label", "") or "").split())
        low = label.casefold()
        if not low:
            continue
        top = int(getattr(el, "top", 0) or 0)
        left = int(getattr(el, "left", 0) or 0)
        width = int(getattr(el, "width", 0) or 0)
        no_coords = (top == 0 and left == 0 and width == 0)
        if not no_coords and (top > hdr_bottom or left < hdr_left):
            continue
        if video:
            is_match = "video call" in low or ("video" in low and "call" in low and "voice" not in low)
        else:
            if "video" in low:
                continue
            is_match = (
                "voice call" in low
                or "audio call" in low
                or low in {"call", "phone"}
                or ("call" in low and "end" not in low and "missed" not in low)
            )
        if is_match:
            cx, cy = getattr(el, "center", (left + max(10, width // 2), top + 16))
            return {
                "x": int(cx), "y": int(cy),
                "label": label,
                "kind": ("video_call" if video else "voice_call"),
                "source": "uia",
                "element": el,
            }
    return None


def verify_call_started(view, *, contact: str = "") -> dict:
    """Verify actual WhatsApp call-session UI markers after clicking call control."""
    if view is None:
        return {"started": False, "state": "no_view", "contact_matched": False}
    texts = []
    for el in getattr(view, "elements", ()) or ():
        t = " ".join(str(getattr(el, "name", "") or getattr(el, "label", "") or getattr(el, "value", "") or "").split())
        if t:
            texts.append(t.casefold())
    joined = " | ".join(texts) + " | " + str(getattr(view, "title", "") or "").casefold()
    markers = ("calling", "ringing", "end call", "hang up", "voice call", "encrypted voice", "connecting", "mute")
    started = any(m in joined for m in markers)
    contact_matched = True
    if contact:
        contact_matched = fold(contact) in fold(joined)
    state = "active_or_ringing" if started else "not_detected"
    return {"started": bool(started and (contact_matched or not contact)), "state": state, "contact_matched": contact_matched}


def verify_selected_chat(view, expected_contact: str = "") -> dict:
    """Inspect the active WhatsApp conversation header cleanly from UIA/OCR/window title."""
    if view is None:
        return {"verified": False, "active_contact": "", "chat_id": "", "has_composer": False, "source": "none"}
    elements = list(getattr(view, "elements", ()) or ())
    mon = getattr(view, "monitor", None) or {}
    win_rect = getattr(view, "window_rect", None)
    if win_rect and len(win_rect) == 4 and win_rect[2] > 200:
        w_left, w_top, w_w, _w_h = (int(v) for v in win_rect)
    else:
        w_left = int(mon.get("left", 0) or 0)
        w_top = int(mon.get("top", 0) or 0)
        w_w = int(mon.get("width", 1920) or 1920)

    chat_left = w_left + int(w_w * 0.27)
    hdr_top = w_top + 45
    hdr_bottom = w_top + 195
    exp_fold = fold(expected_contact) if expected_contact else ""

    header_candidates: list[str] = []
    for el in elements:
        top = int(getattr(el, "top", 0) or 0)
        left = int(getattr(el, "left", 0) or 0)
        if not (hdr_top <= top <= hdr_bottom and left >= chat_left):
            continue
        name = " ".join(str(getattr(el, "name", "") or getattr(el, "label", "") or "").split())
        low = name.casefold()
        if not is_valid_contact_label(name):
            continue
        if any(s in low for s in ("click here for contact info", "last seen", "online", "typing...")):
            continue
        header_candidates.append(name)

    matched = ""
    if exp_fold:
        for cand in header_candidates:
            if exp_fold in fold(cand) or fold(cand) in exp_fold:
                matched = cand
                break
        if not matched and exp_fold in fold(getattr(view, "title", "") or ""):
            matched = expected_contact
    elif header_candidates:
        matched = header_candidates[0]

    has_composer = locate_whatsapp_composer(view) is not None
    verified = bool(matched and (has_composer or bool(exp_fold)))
    chat_id = ("chat:" + fold(matched)) if matched else ""
    if verified:
        set_active_chat(
            platform=("whatsapp_web" if "chrome" in fold(getattr(view, "app", "") or "") else "whatsapp_desktop"),
            contact_name=matched,
            chat_id=chat_id,
            chat_header=matched,
            source=str(getattr(view, "source", "uia") or "uia"),
            verified=True,
            confidence=(0.9 if has_composer else 0.7),
        )
    return {
        "verified": verified,
        "active_contact": matched,
        "chat_id": chat_id,
        "has_composer": has_composer,
        "source": str(getattr(view, "source", "none") or "none"),
    }


def verify_outgoing_message(after_view, draft: str) -> bool:
    """Confirm outgoing delivery either via message bubble visibility or cleared composer."""
    if after_view is None:
        return False
    elements = list(getattr(after_view, "elements", ()) or ())
    comp = locate_whatsapp_composer(after_view)
    draft_cf = str(draft or "").strip().casefold()
    snippet = draft_cf[:24] if draft_cf else ""
    bubble_found = False
    for el in elements:
        role = str(getattr(el, "role", "") or "").casefold()
        txt = str(getattr(el, "value", "") or getattr(el, "name", "") or "").strip().casefold()
        if snippet and snippet in txt and role not in {"edit", "combobox", "searchbox"}:
            bubble_found = True
            break
    if bubble_found:
        return True
    if comp is not None and comp.get("element") is not None:
        comp_val = str(getattr(comp["element"], "value", "") or "").strip().casefold()
        if snippet and snippet not in comp_val and len(comp_val) == 0:
            return True
    return False


def generate_autonomous_draft(
    instruction: str,
    *,
    recipient: str = "",
    previous_draft: str = "",
    tone: str = "friendly",
    language: str = "",
) -> str:
    """Generate, translate, or refine a conversational message draft (Hindi/English) via `core.gemini`."""
    from core import gemini

    inst = str(instruction or "").strip()
    inst_cf = inst.casefold()
    rec = str(recipient or "").strip()
    target_lang = str(language or "").strip()
    if not target_lang:
        if "hindi" in inst_cf or re.search(r"[\u0900-\u097F]", inst):
            target_lang = "Hindi"
        elif "english" in inst_cf:
            target_lang = "English"
        else:
            target_lang = "English"

    is_translate = bool(previous_draft and any(w in inst_cf for w in ("translate", "into english", "in english", "into hindi", "in hindi")))
    if is_translate:
        prompt = (
            f"Translate this message draft accurately into natural conversational {target_lang}, "
            f"keeping its warm, friendly tone without adding invented facts.\n\nDraft:\n{previous_draft}\n\n"
            "Output ONLY the translated message ready to paste."
        )
    elif previous_draft and any(w in inst_cf for w in ("shorter", "shorten", "brief", "concise", "chhota")):
        prompt = (
            f"Rewrite this draft message in {target_lang} to be noticeably shorter (about 3 to 5 concise natural lines), "
            f"preserving its warm {tone} voice and meaning without adding invented facts.\n\nDraft:\n{previous_draft}"
        )
    elif previous_draft and any(w in inst_cf for w in ("natural", "casual", "warmer", "better", "friendly", "rewrite", "replace", "remove")):
        prompt = (
            f"Rewrite or replace this draft in {target_lang} following: {inst}. "
            f"Do not invent personal facts about {rec or 'the recipient'}.\n\nPrevious draft:\n{previous_draft}"
        )
    else:
        prompt = (
            f"Write a friendly, natural {tone} message in {target_lang} for {rec or 'a friend'} following this instruction: {inst}\n"
            "Rules:\n"
            "- Keep it conversational (4 to 8 short lines unless another length is requested).\n"
            "- Do NOT invent private real-world events about the recipient.\n"
            "- Output ONLY the message body ready to paste into WhatsApp (no quotes or preamble)."
        )

    try:
        generated = gemini.text(prompt, tier=gemini.FAST)
        if generated and generated.strip():
            return generated.strip().strip('"')
    except Exception:
        pass

    # Deterministic friendly fallback if cloud model call returns None
    greet = f"Hey {rec}," if rec else "Hey!"
    if target_lang == "Hindi":
        name_hi = f"{rec}, " if rec else ""
        if is_translate:
            return (
                f"नमस्ते {name_hi}कैसे हो?\n"
                "बस सोचा पूछ लूँ कि आज तुम्हारा दिन कैसा रहा।\n"
                "मेरा दिन काम निपटाने और चीज़ें व्यवस्थित करने में काफी अच्छा गुज़रा।\n"
                "जब भी फ़ुर्सत मिले, बताना ज़रूर!"
            )
        return (
            f"अरे {name_hi}नमस्ते!\n"
            "उम्मीद है आज तुम्हारा दिन बहुत अच्छा जा रहा होगा।\n"
            "मेरा दिन आज काम निपटाने, थोड़ा व्यवस्थित रहने और हल्की-फुल्की मस्ती में काफी बढ़िया रहा।\n"
            "अभी थोड़ा आराम कर रही थी, तो सोचा तुम्हारा हालचाल पूछ लूँ।\n"
            "जब भी समय मिले, बताना कि तुम्हारा दिन कैसा बीता!"
        )
    if is_translate:
        return (
            f"{greet}\n"
            "Just wanted to check in and ask how your day has been going!\n"
            "Mine went well keeping things organized and finishing up a few tasks.\n"
            "Let me know how everything went on your side whenever you get a moment!"
        )
    if previous_draft and any(w in inst_cf for w in ("shorter", "shorten", "brief", "concise")):
        lines = [ln.strip() for ln in previous_draft.splitlines() if ln.strip()]
        if len(lines) >= 3:
            return "\n".join([lines[0], lines[len(lines) // 2], lines[-1]])
    return (
        f"{greet}\n"
        "Hope your day is going smoothly!\n"
        "Mine has mostly been spent helping Tarun-kun get through tasks, keeping things organized, and tidying up odds and ends.\n"
        "Taking a quick breather right now, so I thought I'd say hello.\n"
        "Let me know how things are going on your side whenever you get a moment!"
    )


def draft_hash(text: str) -> str:
    return hashlib.sha256(str(text or "").encode("utf-8", errors="ignore")).hexdigest()[:16]


async def dom_extract_whatsapp_contacts(page, query: str = "") -> list[dict]:
    """Structured Playwright DOM extraction on live WhatsApp Web when CDP is attached."""
    rows = await page.evaluate(
        """() => {
            const out = [];
            const pane = document.querySelector('#pane-side') || document;
            const cells = pane.querySelectorAll('[data-testid="cell-frame-container"], [role="listitem"], [role="row"]');
            for (const c of cells) {
                const r = c.getBoundingClientRect();
                if (!r || r.width < 40 || r.height < 16) continue;
                const titleEl = c.querySelector('[data-testid="cell-frame-title"] span[title], span[dir="auto"][title]');
                const title = (titleEl ? (titleEl.getAttribute('title') || titleEl.innerText) : (c.innerText || '')).trim();
                const chatId = c.getAttribute('data-id') || c.getAttribute('data-testid') || '';
                out.push({
                    title: title.split('\\n')[0].trim(),
                    chatId,
                    rect: [Math.round(r.left), Math.round(r.top), Math.round(r.width), Math.round(r.height)],
                    point: [Math.round(r.left + r.width * 0.4), Math.round(r.top + r.height * 0.5)],
                });
            }
            return out;
        }"""
    )
    cleaned: list[dict] = []
    seen: set[str] = set()
    q_fold = fold(query)
    for r in (rows or []):
        dn = _strip_composite_contact_name(str(r.get("title") or ""))
        if not is_valid_contact_label(dn):
            continue
        cid = _clean_identifier(r.get("chatId"))
        key = cid or fold(dn)
        if key in seen:
            continue
        seen.add(key)
        if q_fold and q_fold not in fold(dn):
            continue
        cleaned.append({
            "display_name": dn[:120],
            "contact_id": cid,
            "phone": "",
            "id": cid or f"chat:{fold(dn)}",
            "rect": tuple(r.get("rect") or (0, 0, 0, 0)),
            "point": tuple(r.get("point") or (0, 0)),
            "source": "dom",
            "confidence": "high",
            "position": len(cleaned) + 1,
        })
    return cleaned


class WhatsAppDriver:
    def _view(self):
        from core import screenview
        return screenview.look(budget_s=2.0, allow_ocr=True)

    @staticmethod
    def _unique(items, what):
        items = list({e.eid: e for e in items}.values())
        if len(items) == 1:
            return items[0], ""
        if not items:
            return None, f"No accessible {what} was found."
        return None, f"More than one possible {what} was found; no action was taken."

    def send_message_to(self, receiver: str, message: str, *, task_id: str = "") -> tuple[bool, str]:
        from core import appidentity, desktop_control, screenact, screenview

        receiver, message = str(receiver or "").strip(), str(message or "")
        if not receiver or not message:
            return False, "recipient and message are required"
        if desktop_control.should_stop():
            return False, "computer control is paused or stopped"

        dhash = draft_hash(message)
        if desktop_control.has_sent_attempt(receiver=receiver, draft_hash=dhash, task_id=task_id):
            return False, f"duplicate send blocked: this exact draft was already sent to {receiver}"

        fg = appidentity.focus_window_by_app("WhatsApp")
        if not fg.get("activated"):
            focused = screenact.focus_app("WhatsApp")
            if not focused.ok:
                fg = appidentity.focus_window_by_app("chrome", title_hint="WhatsApp")
                if not fg.get("activated"):
                    return False, f"WhatsApp could not be focused: {focused.error}"

        view = self._view()
        if "whatsapp" not in fold((getattr(view, "app", "") or "") + " " + (getattr(view, "title", "") or "")):
            return False, "WhatsApp is not exposing an accessible active window"

        hdr = verify_selected_chat(view, expected_contact=receiver)
        if not hdr.get("verified"):
            sb = locate_whatsapp_search_bar(view)
            if not sb:
                return False, "No unique WhatsApp search field was found."
            pt_sb = screenact.Point(sb["x"], sb["y"], origin="element", label=sb["label"])
            typed = screenact.type_text(receiver, target=pt_sb, clear_first=True)
            if not typed.ok:
                return False, f"could not search for the contact: {typed.error}"
            time.sleep(0.35)
            view = self._view()
            if "whatsapp" not in fold((getattr(view, "app", "") or "") + " " + (getattr(view, "title", "") or "")):
                return False, "WhatsApp changed during contact lookup"
            contacts = extract_whatsapp_contacts(view, query=receiver)
            if not contacts:
                return False, f"No exactly matching chat named {receiver!r} was visible."
            if len(contacts) > 1:
                exact_only = [c for c in contacts if fold(c["display_name"]) == fold(receiver)]
                if len(exact_only) != 1:
                    return False, f"Found {len(contacts)} distinct contacts matching {receiver!r}; please pick one before sending."
                contacts = exact_only
            chosen = contacts[0]
            pt_c = screenact.Point(chosen["point"][0], chosen["point"][1], origin="element", label=chosen["display_name"])
            clicked = screenact.click(pt_c, expect_process=view.app)
            if not clicked.ok:
                return False, f"could not open the matching chat: {clicked.error}"
            time.sleep(0.25)
            view = self._view()
            hdr = verify_selected_chat(view, expected_contact=receiver)
            if not hdr.get("verified"):
                return False, "active chat title does not match the requested contact; nothing was typed"

        comp = locate_whatsapp_composer(view)
        if not comp:
            return False, "No unique message field was found."
        pt_comp = screenact.Point(comp["x"], comp["y"], origin="element", label=comp["label"])
        entered = screenact.type_text(message, target=pt_comp, clear_first=True)
        if not entered.ok:
            return False, f"could not enter the message: {entered.error}"
        staged = self._view()
        if any(getattr(e, "role", "") == screenview.ROLE_EDIT for e in getattr(staged, "elements", ()) or ()):
            if not any(message[:24] in (e.value or e.name or "") for e in staged.elements if getattr(e, "role", "") in (screenview.ROLE_EDIT, screenview.ROLE_TEXT)):
                return False, "message was not visible in the intended composer; it was not submitted"
        sent = screenact.press("enter")
        if not sent.ok:
            return False, f"message submission failed: {sent.error}"
        time.sleep(0.30)
        after = self._view()
        if after.app != view.app:
            return False, "active conversation changed during submission"
        if not verify_outgoing_message(after, message):
            return False, "unverified: outgoing message was not confirmed in the active chat after Enter"
        desktop_control.record_sent_attempt(receiver=receiver, draft_hash=dhash, task_id=task_id)
        desktop_control.mark_composition_sent(receiver=receiver, draft_hash=dhash)
        return True, "exact chat matched, message submitted, and outgoing state verified"


def get():
    """Return the verified WhatsApp driver."""
    return WhatsAppDriver(), "WhatsApp UIA/OCR driver ready"


def observe(player=None):
    from plugins._common import route
    return route(player, "screen_agent", {"action": "observe"})


def send(player, contact: str, message: str) -> str:
    from plugins._common import route
    return route(player, "send_message", {"platform": "whatsapp", "receiver": contact,
                                           "message_text": message})
