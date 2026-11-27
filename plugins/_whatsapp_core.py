"""WhatsApp intelligence & UI/DOM Automation driver for Desktop and Chrome WhatsApp Web.

Unified single source of truth for:
1. Deterministic contact extraction (DOM -> UIA chat-card containers -> OCR vertical card bands)
   with placeholder/status/counter rejection so unchanged search results never drift (e.g. 21/19/16).
2. Shared `verify_whatsapp_active_chat()` verifying app, tab, right conversation pane,
   header text, active_chat_id, composer association, and observation timestamp/source.
3. Shared `locate_whatsapp_composer()` returning structured metadata (`composer_found`,
   `composer_type`, `application`, `active_chat_id`, `focus_verified`, `observation_id`,
   `confidence`) and unified clear/type/replace/readback helpers.
4. Verified single-execution sending with idempotency protection and voice-call verification.
"""
from __future__ import annotations

import hashlib
import re
import unicodedata
import threading
import time
from typing import Any

from core.appidentity import fold

_WA_CHROME_NOISE = frozenset({
    "whatsapp", "chats", "status", "channels", "communities", "archive", "archived",
    "all", "unread", "favorites", "groups", "search", "search or start new chat",
    "search unread chats", "ask meta ai or search", "type a message", "new chat",
    "menu", "settings", "profile", "end to end encrypted", "use whatsapp on your computer",
    "use whatsapp on desktop", "download", "log out", "mute notifications",
    "clear messages", "delete chat", "voice call", "video call", "search...",
    "contacts", "messages", "no chats, contacts or messages found", "chats, contacts and messages",
    "lock whatsapp", "starred messages", "select chats",
})

_WA_STATUS_PREVIEW_NOISE = frozenset({
    "photo", "video", "audio", "voice message", "gif", "sticker", "document",
    "contact card", "location", "live location", "poll", "event",
    "missed voice call", "missed video call", "voice call", "video call",
    "incoming voice call", "incoming video call", "typing...", "recording audio...",
    "this message was deleted", "you deleted this message",
    "waiting for this message", "click here for contact info", "online",
    "pinned", "muted", "draft", "draft:", "you", "you:", "read", "delivered",
})

_TIME_RE = re.compile(
    r"^(?:\d{1,2}[:;.]\d{2}(?:\s*[ap]m)?|yesterday|today|just now|"
    r"monday|tuesday|wednesday|thursday|friday|saturday|sunday|"
    r"\d{1,2}/\d{1,2}/\d{2,4})$",
    re.I,
)
_PHONE_RE = re.compile(r"\+?\d[\d\s\-()]{6,}\d")
_UNREAD_RE = re.compile(r"^\d{1,4}$")
# Reject OCR/UIA glyph artifacts & empty/placeholder IDs like "-0-", "-0+", "---", "+0"
_PLACEHOLDER_RE = re.compile(r"^[\W_0+\-|=~.*•·]+$")

_CHAT_LOCK = threading.RLock()
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


_INVISIBLE_RE = re.compile(r"[\u200b-\u200f\u2060\ufeff]")
_CONTROL_NAME_WORDS = frozenset({"message", "compose", "composer", "box", "field", "input", "type", "write",
                                 "text", "edit", "textbox", "bar", "here"})
_PLACEHOLDERS = frozenset({"type a message", "type a message (ocr)", "message composer",
                           "write a message", "type here", "message"})


def normalize_composer_text(text: Any) -> str:
    """Draft text as a person would see it: no zero-width/NBSP, no stray blank
    lines, and a placeholder ("Type a message") is NOT content."""
    t = _INVISIBLE_RE.sub("", str(text or "").replace("\u00a0", " "))
    t = "\n".join(line.rstrip() for line in t.replace("\r", "").split("\n")).strip()
    return "" if t.casefold() in _PLACEHOLDERS else t


def _name_words(text: str) -> list[str]:
    """Accent-insensitive, case-insensitive WORDS (``fold`` joins them, which is
    right for equality but useless for whole-word comparison)."""
    t = unicodedata.normalize("NFKD", str(text or "")).casefold()
    t = "".join(ch for ch in t if not unicodedata.combining(ch))
    return re.findall(r"[^\W_]+", t)


def contact_match(expected: str, candidate: str) -> str:
    """'exact' | 'partial' | 'none' for a requested name against a visible name.

    Whole-word comparison only: "Rio" matches "Rio" (exact) and "Rio Sharma"
    (partial, every requested word is present) but NEVER "Mario", "Riordan" or
    the shorter header "Ri". The old bidirectional substring test accepted all
    of those, so a message for Rio could be typed into Mario's chat.
    """
    e, c = _name_words(expected), _name_words(candidate)
    if not e or not c:
        return "none"
    if e == c:
        return "exact"
    return "partial" if set(e) <= set(c) else "none"


def is_valid_contact_label(name: str) -> bool:
    cleaned = " ".join(str(name or "").split()).strip()
    if not cleaned or len(cleaned) < 2:
        return False
    low = cleaned.casefold().rstrip(":")
    if (
        low in _WA_CHROME_NOISE
        or low in _WA_STATUS_PREVIEW_NOISE
        or _TIME_RE.match(cleaned)
        or _UNREAD_RE.match(cleaned)
        or _PLACEHOLDER_RE.match(cleaned)
    ):
        return False
    if low.startswith(("draft:", "you:", "missed voice call", "missed video call", "click here for")):
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
        if snap.get("observation_id"):
            updates["obs_id"] = str(snap["observation_id"])[:32]
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
    ts_cut = re.split(r"\s+\d{1,2}[:;.]\d{2}(?:\s*[AP]M)?\b", display_name, maxsplit=1, flags=re.IGNORECASE)
    if ts_cut and ts_cut[0].strip():
        display_name = ts_cut[0].strip()
    for day_word in ("yesterday", "today"):
        m = re.split(rf"\s+\b{day_word}\b", display_name, maxsplit=1, flags=re.IGNORECASE)
        if m and m[0].strip():
            display_name = m[0].strip()
    return display_name.strip(" -|•·")


def _window_geometry(view) -> tuple[int, int, int, int]:
    mon = getattr(view, "monitor", None) or {}
    win_rect = getattr(view, "window_rect", None)
    if win_rect and len(win_rect) == 4 and win_rect[2] > 200:
        return tuple(int(v) for v in win_rect)
    return (
        int(mon.get("left", 0) or 0),
        int(mon.get("top", 0) or 0),
        int(mon.get("width", 1920) or 1920),
        int(mon.get("height", 1080) or 1080),
    )


def _inside_rect(el, card_rect: tuple[int, int, int, int]) -> bool:
    cl, ct, cw, ch = card_rect
    et = int(getattr(el, "top", 0) or 0)
    el_left = int(getattr(el, "left", 0) or 0)
    ew = int(getattr(el, "width", 0) or 0)
    eh = int(getattr(el, "height", 0) or 0)
    cx = el_left + max(1, ew // 2)
    cy = et + max(1, eh // 2)
    return (cl - 6) <= cx <= (cl + cw + 6) and (ct - 4) <= cy <= (ct + ch + 4)


def extract_whatsapp_contacts(view, query: str = "", *, source_override: str = "") -> list[dict]:
    """Deterministically extract UNIQUE visible WhatsApp contact/chat cards.

    Strategy (in priority order):
    1. UIA chat-card container rows (`listitem`/`treeitem`/`row`/`cell` >= 34px tall)
       absorb all child text/OCR elements inside their bounds so title + preview +
       timestamp + badge remain ONE card.
    2. Remaining unconsumed text/OCR nodes are grouped by vertical chat-card bands
       (~64px height) in the left pane so the lower preview line (`+20..56px`) attaches
       to the contact title above it instead of generating fluctuating extra contacts.
    3. Rejects placeholders (`-0-`, `-0+`), counters, timestamps, and status words.
    """
    if view is None:
        return []
    elements = list(getattr(view, "elements", ()) or ())
    w_left, w_top, w_w, _w_h = _window_geometry(view)
    pane_top_floor = w_top + 115
    pane_right_ceil = w_left + int(w_w * 0.46)
    view_src = (source_override or str(getattr(view, "source", "") or "uia")).casefold()
    now_m = time.monotonic()
    obs_id = str(getattr(view, "stable_digest", "") or getattr(view, "digest", "") or f"obs_{int(now_m * 1000)}")

    # Pre-filter candidate left-pane elements
    left_els: list[tuple[int, Any]] = []
    for idx, el in enumerate(elements):
        role = str(getattr(el, "role", "") or "").casefold()
        if role in {"edit", "combobox", "searchbox", "scrollbar", "separator", "image"}:
            continue
        top = int(getattr(el, "top", 0) or 0)
        left = int(getattr(el, "left", 0) or 0)
        width = int(getattr(el, "width", 0) or 0)
        height = int(getattr(el, "height", 0) or 0)
        if top < pane_top_floor or left > pane_right_ceil or width < 20 or height < 10:
            continue
        left_els.append((idx, el))

    consumed_indices: set[int] = set()
    raw_cards: list[dict] = []

    # Pass 1: UIA container rows (`listitem`, `treeitem`, `row`, `cell`, `button` with card height >= 36)
    containers = [
        (idx, el) for idx, el in left_els
        if str(getattr(el, "role", "") or "").casefold() in {"listitem", "treeitem", "row", "cell", "button", "group"}
        and int(getattr(el, "height", 0) or 0) >= 34
        and int(getattr(el, "width", 0) or 0) >= 120
    ]
    containers.sort(key=lambda pair: (int(getattr(pair[1], "top", 0) or 0), int(getattr(pair[1], "left", 0) or 0)))

    for c_idx, card_el in containers:
        if c_idx in consumed_indices:
            continue
        c_rect = (
            int(getattr(card_el, "left", 0) or 0),
            int(getattr(card_el, "top", 0) or 0),
            int(getattr(card_el, "width", 0) or 0),
            int(getattr(card_el, "height", 0) or 0),
        )
        children = [
            (j, child) for j, child in left_els
            if j != c_idx and j not in consumed_indices and _inside_rect(child, c_rect)
        ]
        children.sort(key=lambda pair: (int(getattr(pair[1], "top", 0) or 0), int(getattr(pair[1], "left", 0) or 0)))
        card_texts: list[str] = []
        raw_card_name = " ".join(str(getattr(card_el, "name", "") or getattr(card_el, "label", "") or "").split())
        if raw_card_name:
            card_texts.append(raw_card_name)
        for _j, child in children:
            t = " ".join(str(getattr(child, "name", "") or getattr(child, "label", "") or "").split())
            if t:
                card_texts.append(t)

        # Prefer clean topmost child title when child exists (otherwise strip composite card text)
        best_title = ""
        preview = ""
        phone = None
        for txt in ([ " ".join(str(getattr(ch, "name", "") or "").split()) for _j, ch in children ] or card_texts):
            m_ph = _PHONE_RE.search(txt)
            if m_ph and not phone:
                phone = m_ph.group(0).strip()
            cand = _strip_composite_contact_name(txt)
            if not best_title and is_valid_contact_label(cand):
                best_title = cand
            elif best_title and is_valid_contact_label(cand) and fold(cand) != fold(best_title) and not preview:
                preview = cand[:140]
        if not best_title and raw_card_name:
            cand = _strip_composite_contact_name(raw_card_name)
            if is_valid_contact_label(cand):
                best_title = cand
        if not best_title:
            continue

        consumed_indices.add(c_idx)
        for j, _ch in children:
            consumed_indices.add(j)

        raw_auto_id = _clean_identifier(getattr(card_el, "automation_id", None) or getattr(card_el, "contact_id", None))
        raw_chat_id = _clean_identifier(getattr(card_el, "chat_id", None))
        dn_fold = fold(best_title)
        cx, cy = (getattr(card_el, "center", None) or (c_rect[0] + c_rect[2] // 2, c_rect[1] + c_rect[3] // 2))
        chat_id = raw_chat_id or raw_auto_id or (f"phone:{re.sub(r'[^0-9+]', '', phone)}" if phone else f"chat:{dn_fold}")
        raw_cards.append({
            "display_name": best_title[:120],
            "contact_id": raw_auto_id,
            "phone": phone or "",
            "id": chat_id,
            "dedupe_key": raw_chat_id or raw_auto_id or phone or dn_fold,
            "last_message_preview": preview,
            "rect": c_rect,
            "point": (int(cx), int(cy)),
            "top": c_rect[1],
            "left": c_rect[0],
            "source": ("dom" if "dom" in view_src else "uia"),
            "confidence": ("high" if (raw_auto_id or raw_chat_id or children) else "medium"),
            "observed_at": now_m,
            "observation_id": obs_id,
            "element": card_el,
        })

    # Pass 2: Unconsumed UIA/OCR labels grouped into vertical card bands
    remaining_rows: list[dict] = []
    for idx, el in left_els:
        if idx in consumed_indices:
            continue
        role = str(getattr(el, "role", "") or "").casefold()
        if role == "button":
            continue
        name = " ".join(str(getattr(el, "name", "") or getattr(el, "label", "") or "").split())
        if not name or len(name) < 2:
            continue
        top = int(getattr(el, "top", 0) or 0)
        left = int(getattr(el, "left", 0) or 0)
        width = int(getattr(el, "width", 0) or 0)
        height = int(getattr(el, "height", 0) or 0)
        # Skip far-right small timestamp column unless paired inside card container
        if left > w_left + int(w_w * 0.36) and width < 80 and _TIME_RE.match(name):
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
        is_ocr = ("ocr" in role) or (view_src == "ocr" and role in {"text", "ocr_text", ""}) or role == "text"
        cx, cy = (getattr(el, "center", None) or (left + max(1, width // 2), top + max(1, height // 2)))
        chat_id = raw_chat_id or raw_auto_id or (f"phone:{re.sub(r'[^0-9+]', '', phone)}" if phone else f"chat:{dn_fold}")
        remaining_rows.append({
            "display_name": display_name[:120],
            "contact_id": raw_auto_id,
            "phone": phone or "",
            "id": chat_id,
            "dedupe_key": raw_chat_id or raw_auto_id or phone or dn_fold,
            "last_message_preview": (str(getattr(el, "value", "") or "").strip()[:140]),
            "rect": (left, top, width, height),
            "point": (int(cx), int(cy)),
            "top": top,
            "left": left,
            "source": ("dom" if "dom" in view_src else ("ocr" if is_ocr else "uia")),
            "confidence": ("high" if (raw_auto_id or raw_chat_id) else "medium"),
            "observed_at": now_m,
            "observation_id": obs_id,
            "element": el,
        })

    # Attach remaining rows to existing card containers or group top-to-bottom within 56px vertical card bands
    remaining_rows.sort(key=lambda r: (r["top"], r["left"]))
    deduped = list(raw_cards)
    q_fold = fold(query) if query else ""
    for row in remaining_rows:
        r_fold = fold(row["display_name"])
        merged = False
        for existing in deduped:
            e_fold = fold(existing["display_name"])
            same_id = bool(row["contact_id"] and existing["contact_id"] and row["contact_id"] == existing["contact_id"])
            same_phone = bool(row["phone"] and existing["phone"] and row["phone"] == existing["phone"])
            same_name = (r_fold == e_fold)
            same_name_overlap = abs(row["top"] - existing["top"]) <= 44 and (r_fold in e_fold or e_fold in r_fold)
            # In WhatsApp's single left chat column, a lower line within 12..54px below a card title
            # (with no card separator/query match) is usually the message preview line for THAT card.
            dy = row["top"] - existing["top"]
            is_lower_preview_line = (
                12 <= dy <= 52
                and abs(row["left"] - existing["left"]) <= 48
                and (not q_fold or q_fold not in r_fold)
                and (not row["contact_id"] and not row["phone"])
            )
            if same_id or same_phone or same_name or same_name_overlap or is_lower_preview_line:
                if not existing["phone"] and row["phone"]:
                    existing["phone"] = row["phone"]
                if not existing["contact_id"] and row["contact_id"]:
                    existing["contact_id"] = row["contact_id"]
                    existing["id"] = row["id"]
                if not existing["last_message_preview"] and fold(row["display_name"]) != e_fold:
                    existing["last_message_preview"] = row["display_name"][:140]
                if same_name_overlap and len(row["display_name"]) < len(existing["display_name"]) and row["display_name"] in existing["display_name"]:
                    existing["display_name"] = row["display_name"]
                merged = True
                break
        if not merged:
            deduped.append(row)

    deduped.sort(key=lambda r: (r["top"], r["left"]))
    if q_fold:
        exact_m = [c for c in deduped if fold(c["display_name"]) == q_fold]
        prefix_m = [c for c in deduped if fold(c["display_name"]).startswith(q_fold) and c not in exact_m]
        sub_m = [
            c for c in deduped
            if (q_fold in fold(c["display_name"]) or q_fold in fold(c.get("phone", "")))
            and c not in exact_m and c not in prefix_m
        ]
        ranked = exact_m + prefix_m + sub_m
        deduped = ranked  # When a specific search query is supplied, never return unrelated non-matching items

    for pos, item in enumerate(deduped, 1):
        item["position"] = pos
    return deduped


def locate_whatsapp_search_bar(view) -> dict | None:
    """Identify WhatsApp's chat search input while excluding Chrome's address bar and composer."""
    if view is None:
        return None
    elements = list(getattr(view, "elements", ()) or ())
    w_left, w_top, w_w, w_h = _window_geometry(view)
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
        is_ocr = ("ocr" in role) or (view_src == "ocr" and role in {"text", "ocr_text", ""}) or role == "text"
        label_cf = str(getattr(el, "name", "") or getattr(el, "label", "") or "").strip().casefold()
        top = int(getattr(el, "top", 0) or 0)
        left = int(getattr(el, "left", 0) or 0)
        if is_ocr and toolbar_floor <= top <= w_top + 230 and left <= left_pane_ceil:
            if "search" in label_cf or "new chat" in label_cf:
                cx, cy = getattr(el, "center", (left + 100, top + 14))
                return {"x": int(cx), "y": int(cy), "label": "WhatsApp search field (OCR)", "source": "ocr", "element": el}
    return None


def locate_whatsapp_composer(view, *, active_chat: dict | None = None) -> dict:
    """Shared WhatsApp composer locator for Web (UIA/OCR/DOM) & Desktop.

    Always returns structured metadata including:
    `composer_found`, `composer_type`, `application`, `active_chat_id`,
    `focus_verified`, `observation_id`, `confidence`, plus `x`, `y`, `label`,
    `source`, `element` when found. Never confuses Chrome's omnibox or search bar.
    """
    chat_info = active_chat or get_active_chat()
    app_name = str(getattr(view, "app", "") or ("chrome.exe" if chat_info.get("platform") == "whatsapp_web" else "WhatsApp") or "")
    obs_id = str(getattr(view, "stable_digest", "") or getattr(view, "digest", "") or f"obs_{int(time.monotonic() * 1000)}")
    active_cid = str(chat_info.get("chat_id") or "")
    base_not_found = {
        "composer_found": False,
        "composer_type": "none",
        "application": app_name,
        "active_chat_id": active_cid,
        "focus_verified": False,
        "observation_id": obs_id,
        "confidence": "uncertain",
        "x": 0,
        "y": 0,
        "label": "",
        "source": "none",
        "element": None,
    }
    if view is None:
        return base_not_found

    elements = list(getattr(view, "elements", ()) or ())
    w_left, w_top, w_w, w_h = _window_geometry(view)
    composer_floor = w_top + int(w_h * 0.55)
    bottom_band = w_top + int(w_h * 0.68)
    chat_left_floor = w_left + int(w_w * 0.27)
    view_src = str(getattr(view, "source", "") or "").casefold()

    # Pass 1: Explicit semantic UIA editable controls in conversation pane
    for el in elements:
        role = str(getattr(el, "role", "") or "").casefold()
        label_cf = str(getattr(el, "name", "") or getattr(el, "label", "") or "").casefold()
        if any(x in label_cf for x in ("search", "filter", "address", "omnibox", "url")):
            continue
        top = int(getattr(el, "top", 0) or 0)
        left = int(getattr(el, "left", 0) or 0)
        width = int(getattr(el, "width", 0) or 0)
        no_coords = (top == 0 and left == 0 and width == 0)
        is_lexical_node = role in {"group", "custom"} and ("type a message" in label_cf or ("message" in label_cf and top >= bottom_band))
        if (role in {"edit", "document", "textbox", "combobox"} or is_lexical_node) and not getattr(el, "protected", False):
            if top >= composer_floor or ("message" in label_cf and (left >= chat_left_floor or no_coords)):
                cx, cy = getattr(el, "center", (left + max(60, width // 2), top + 18))
                return {
                    "composer_found": True,
                    "composer_type": ("dom_editable" if "dom" in view_src else f"uia_{role or 'edit'}"),
                    "application": app_name,
                    "active_chat_id": active_cid,
                    "focus_verified": bool(getattr(el, "focused", False)),
                    "observation_id": obs_id,
                    "confidence": "high",
                    "x": int(cx),
                    "y": int(cy),
                    "label": getattr(el, "name", "") or getattr(el, "label", "") or "Type a message",
                    "source": ("dom" if "dom" in view_src else "uia"),
                    "element": el,
                }

    # Pass 2: OCR / static-text fallback strictly inside bottom conversation composer bar
    for el in elements:
        role = str(getattr(el, "role", "") or "").casefold()
        is_ocr = ("ocr" in role) or (view_src == "ocr" and role in {"text", "ocr_text", ""}) or role == "text"
        label_cf = str(getattr(el, "name", "") or getattr(el, "label", "") or "").strip().casefold()
        top = int(getattr(el, "top", 0) or 0)
        left = int(getattr(el, "left", 0) or 0)
        if any(x in label_cf for x in ("search", "filter", "address", "omnibox", "url")):
            continue
        if is_ocr and top >= bottom_band and left >= chat_left_floor and (
            "type a message" in label_cf
            or "write a message" in label_cf
            or "type here" in label_cf
            or ("message" in label_cf and "encrypted" not in label_cf)
        ):
            cx, cy = getattr(el, "center", (left + 160, top + 16))
            return {
                "composer_found": True,
                "composer_type": "ocr_composer_band",
                "application": app_name,
                "active_chat_id": active_cid,
                "focus_verified": bool(getattr(el, "focused", False)),
                "observation_id": obs_id,
                "confidence": "medium",
                "x": int(cx),
                "y": int(cy),
                "label": getattr(el, "name", "") or "Type a message (OCR)",
                "source": "ocr",
                "element": el,
            }
    return base_not_found


def read_composer(view) -> dict:
    """What is in the composer? status: empty | has_text | unreadable | not_found.

    ``unreadable`` is a first-class answer. A UIA/OCR composer that does not
    expose its value used to be reported as EMPTY, which made "clear the draft"
    and "message sent" succeed without evidence. Emptiness now needs positive
    evidence: a readable value of "", or the placeholder being displayed.
    """
    comp = locate_whatsapp_composer(view)
    if not comp.get("composer_found") or comp.get("element") is None:
        return {"status": "not_found", "text": "", "composer": comp}
    el = comp["element"]
    raw_val = str(getattr(el, "value", "") or "")
    label = " ".join(str(getattr(el, "name", "") or getattr(el, "label", "") or "").split())
    patterns = {str(p).casefold() for p in (getattr(el, "patterns", ()) or ())}
    value_exposed = (getattr(el, "value_read_only", None) is not None) or ("value" in patterns) \
        or (comp.get("source") == "dom")
    val = normalize_composer_text(raw_val)
    if val:
        return {"status": "has_text", "text": val, "composer": comp}
    if label.casefold() in _PLACEHOLDERS:
        # Placeholder on display => nothing typed (WhatsApp hides it once text exists).
        return {"status": "empty", "text": "", "composer": comp}
    if value_exposed and not raw_val.strip():
        return {"status": "empty", "text": "", "composer": comp}
    if label and comp.get("source") != "dom" and not (set(_name_words(label)) & _CONTROL_NAME_WORDS):
        # Some builds expose the draft AS the accessible name. A generic control name
        # ("Compose box", "Message field") is not a draft, so it never counts.
        return {"status": "has_text", "text": normalize_composer_text(label), "composer": comp}
    band = _composer_band_text(view, comp)
    if band:
        return {"status": "has_text", "text": band, "composer": comp}
    return {"status": "unreadable", "text": "", "composer": comp}


def _composer_band_text(view, comp: dict) -> str:
    """Text drawn INSIDE the composer's own row (UIA text children / OCR lines).

    Only elements vertically within the composer row and right of the chat-list
    divider count, so message bubbles above it and the search box beside it do not.
    """
    cy, cx = int(comp.get("y", 0) or 0), int(comp.get("x", 0) or 0)
    if not cy:
        return ""
    w_left, _t, w_w, _h = _window_geometry(view)
    left_floor = w_left + int(w_w * 0.27)
    parts = []
    for el in getattr(view, "elements", ()) or ():
        if el is comp.get("element"):
            continue
        top = int(getattr(el, "top", 0) or 0)
        height = int(getattr(el, "height", 0) or 0)
        left = int(getattr(el, "left", 0) or 0)
        ecy = top + height // 2
        if abs(ecy - cy) > 28 or left < left_floor:
            continue
        txt = normalize_composer_text(getattr(el, "value", "") or getattr(el, "name", "") or "")
        low = txt.casefold()
        if not txt or low in _WA_CHROME_NOISE or low in _PLACEHOLDERS or len(txt) < 1:
            continue
        parts.append((left, txt))
    return normalize_composer_text(" ".join(t for _l, t in sorted(parts)))


def composer_text_value(view) -> tuple[bool, str]:
    """Backward-compatible `(readback_supported, value)`; unreadable => (False, "")."""
    r = read_composer(view)
    if r["status"] in {"empty", "has_text"}:
        return True, r["text"]
    return False, ""


def verify_whatsapp_active_chat(
    view=None,
    expected_contact: str = "",
    *,
    tab_title: str = "",
    tab_url: str = "",
) -> dict:
    """Shared verifier for an actually open WhatsApp conversation.

    Verifies WhatsApp app/tab, right-hand conversation pane, conversation header text,
    resolved contact identity (NEVER treating Chrome's tab title 'WhatsApp - Google Chrome'
    as a contact name), chat identifier, composer association, and observation timestamp.
    """
    now_m = time.monotonic()
    base = {
        "verified": False,
        "active_contact": "",
        "chat_id": "",
        "chat_header": "",
        "chat_url": str(tab_url or ""),
        "platform": "",
        "application": "",
        "has_composer": False,
        "source": "none",
        "confidence": 0.0,
        "observation_id": f"obs_{int(now_m * 1000)}",
        "observed_at": now_m,
        "reason": "",
    }
    if view is None:
        base["reason"] = "no screen observation was available"
        return base

    app_s = str(getattr(view, "app", "") or "")
    title_s = str(tab_title or getattr(view, "title", "") or "")
    if "whatsapp" not in fold(f"{app_s} {title_s} {tab_url}"):
        base["reason"] = "WhatsApp is not the active application or browser tab"
        return base

    is_web = any(b in fold(app_s) for b in ("chrome", "msedge", "edge", "firefox", "brave")) or ("web.whatsapp.com" in str(tab_url or "").casefold())
    platform_name = "whatsapp_web" if is_web else "whatsapp_desktop"
    obs_id = str(getattr(view, "stable_digest", "") or getattr(view, "digest", "") or base["observation_id"])

    elements = list(getattr(view, "elements", ()) or ())
    w_left, w_top, w_w, _w_h = _window_geometry(view)
    chat_left = w_left + int(w_w * 0.27)
    hdr_top = w_top + 40
    hdr_bottom = w_top + 205
    exp_fold = fold(expected_contact) if expected_contact else ""

    header_candidates: list[str] = []
    for el in elements:
        top = int(getattr(el, "top", 0) or 0)
        left = int(getattr(el, "left", 0) or 0)
        if not (hdr_top <= top <= hdr_bottom and left >= chat_left):
            continue
        role = str(getattr(el, "role", "") or "").casefold()
        if role in {"edit", "combobox", "searchbox"}:
            continue
        name = " ".join(str(getattr(el, "name", "") or getattr(el, "label", "") or "").split())
        if not is_valid_contact_label(name):
            continue
        header_candidates.append(name)

    matched = ""
    if exp_fold:
        for cand in header_candidates:
            if contact_match(expected_contact, cand) != "none":
                matched = cand
                break
        if not matched and not is_web and contact_match(expected_contact, title_s) != "none" and fold(title_s) != "whatsapp":
            matched = expected_contact
    elif header_candidates:
        matched = header_candidates[0]

    comp = locate_whatsapp_composer(view, active_chat={"platform": platform_name, "chat_id": (f"chat:{fold(matched)}" if matched else "")})
    has_composer = bool(comp.get("composer_found"))
    # A conversation is only "verified" with its composer in view: a header
    # alone can belong to a stale pane or a profile/info drawer.
    verified = bool(matched and has_composer)
    chat_id = ("chat:" + fold(matched)) if matched else ""
    src = str(getattr(view, "source", "uia") or "uia")
    conf = 0.92 if (verified and has_composer) else (0.72 if verified else 0.2)

    reason = ""
    if not verified:
        if matched and not has_composer:
            reason = f"the {matched!r} header is visible but the message box is not"
        elif expected_contact and header_candidates:
            reason = f"active chat header is {header_candidates[0]!r}, not {expected_contact!r}"
        elif expected_contact:
            reason = f"conversation header for {expected_contact!r} was not visible in the right pane"
        else:
            reason = "no active conversation header was found in the right pane"

    res = {
        "verified": verified,
        "active_contact": matched,
        "chat_id": chat_id,
        "chat_header": matched,
        "chat_url": str(tab_url or ""),
        "platform": platform_name,
        "application": app_s or ("WhatsApp (Chrome)" if is_web else "WhatsApp Desktop"),
        "has_composer": has_composer,
        "source": src,
        "confidence": conf,
        "observation_id": obs_id,
        "observed_at": now_m,
        "reason": reason,
    }
    if verified:
        set_active_chat(
            platform=platform_name,
            contact_name=matched,
            chat_id=chat_id,
            chat_url=str(tab_url or ""),
            chat_header=matched,
            source=src,
            verified=True,
            confidence=conf,
            observation_id=obs_id,
        )
    elif expected_contact and header_candidates and contact_match(expected_contact, header_candidates[0]) == "none":
        # User switched to a different open conversation — do not keep stale contact identity!
        set_active_chat(
            platform=platform_name,
            contact_name=header_candidates[0],
            chat_id=f"chat:{fold(header_candidates[0])}",
            chat_header=header_candidates[0],
            source=src,
            verified=bool(has_composer),
            confidence=0.6,
            observation_id=obs_id,
        )
    return res


def verify_selected_chat(view, expected_contact: str = "") -> dict:
    """Backward-compatible wrapper around `verify_whatsapp_active_chat()`."""
    return verify_whatsapp_active_chat(view, expected_contact=expected_contact)


def locate_whatsapp_call_button(view, *, video: bool = False) -> dict | None:
    """Locate WhatsApp's voice-call (or video-call) header control with strict separation."""
    if view is None:
        return None
    elements = list(getattr(view, "elements", ()) or ())
    w_left, w_top, w_w, _w_h = _window_geometry(view)
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


# Markers that exist ONLY once a call session is on screen. The idle chat header
# also contains controls called "Voice call", "Video call" and menu items such as
# "Mute notifications"; matching those (as the old check did) reported a call as
# started while nothing had been dialled.
_CALL_SESSION_MARKERS = ("end call", "hang up", "calling", "ringing", "connecting", "reconnecting",
                         "end-to-end encrypted call", "leave call")
_VIDEO_CALL_MARKERS = ("turn off camera", "turn on camera", "stop video", "start video", "camera off", "camera on")


def _view_texts(view) -> list[str]:
    out = []
    for el in getattr(view, "elements", ()) or ():
        t = " ".join(str(getattr(el, "name", "") or getattr(el, "label", "") or getattr(el, "value", "") or "").split())
        if t:
            out.append(t.casefold())
    return out


def verify_call_started(view, *, contact: str = "", video: bool = False, before_view=None) -> dict:
    """Verify an actual call session, of the right KIND, with the right PERSON.

    Evidence must be NEW relative to ``before_view`` (the idle chat) so controls
    that were already there cannot count. Voice and video are told apart by the
    in-call camera controls, and a call of the wrong kind is reported as such.
    """
    if view is None:
        return {"started": False, "state": "no_view", "contact_matched": False, "kind": "unknown"}
    texts = _view_texts(view)
    before = set(_view_texts(before_view)) if before_view is not None else set()
    title = str(getattr(view, "title", "") or "").casefold()
    fresh = [t for t in texts if t not in before]
    session_hits = [t for t in fresh if any(m in t for m in _CALL_SESSION_MARKERS)]
    kind = "video" if any(m in t for t in fresh for m in _VIDEO_CALL_MARKERS) else "voice"
    if not session_hits:
        return {"started": False, "state": "not_detected", "contact_matched": False, "kind": "unknown"}
    joined = " | ".join(texts) + " | " + title
    contact_matched = (not contact) or any(contact_match(contact, t) != "none" for t in texts + [title])
    kind_ok = (kind == ("video" if video else "voice"))
    state = "active_or_ringing" if (contact_matched and kind_ok) else ("wrong_kind" if contact_matched else "wrong_contact")
    return {"started": bool(contact_matched and kind_ok), "state": state,
            "contact_matched": bool(contact_matched), "kind": kind}


def _bubble_count(view, snippet: str) -> int:
    n = 0
    comp = locate_whatsapp_composer(view)
    comp_y = int(comp.get("y", 0) or 0) if comp.get("composer_found") else 0
    for el in getattr(view, "elements", ()) or ():
        role = str(getattr(el, "role", "") or "").casefold()
        if role in {"edit", "combobox", "searchbox", "textbox"}:
            continue
        if comp_y and (int(getattr(el, "top", 0) or 0) + int(getattr(el, "height", 0) or 0) // 2) >= comp_y - 20:
            continue    # the composer row is not a sent bubble
        txt = fold(str(getattr(el, "value", "") or getattr(el, "name", "") or ""))
        if snippet and snippet in txt:
            n += 1
    return n


def verify_outgoing_message(after_view, draft: str, before_view=None) -> bool:
    """True only with POSITIVE evidence the message left the composer.

    Required: a message bubble containing the draft that was not there before
    (count increased), AND the composer is not still holding that draft. An
    unreadable composer is never evidence of "sent".
    """
    if after_view is None:
        return False
    snippet = fold(normalize_composer_text(draft))[:40]
    if not snippet:
        return False
    after_n = _bubble_count(after_view, snippet)
    before_n = _bubble_count(before_view, snippet) if before_view is not None else 0
    state = read_composer(after_view)
    still_in_composer = state["status"] == "has_text" and snippet in fold(state["text"])
    # The draft sitting in the composer is not a bubble; read_composer's own
    # element is excluded by role, but OCR may surface it as plain text.
    if still_in_composer:
        return False
    return after_n > before_n


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

    is_translate = bool(previous_draft and any(w in inst_cf for w in ("translate", "into english", "in english", "into hindi", "in hindi", "replace it with english", "with english")))
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


async def dom_verify_active_chat(page, expected_contact: str = "") -> dict:
    """Verify active WhatsApp Web conversation header & composer directly from live DOM."""
    try:
        info = await page.evaluate(
            """() => {
                const main = document.querySelector('#main');
                if (!main) return {found: false, header: '', composer: false, url: location.href};
                const hdr = main.querySelector('header span[title], header [data-testid="conversation-info-header-chat-title"], header span[dir="auto"]');
                const htxt = hdr ? (hdr.getAttribute('title') || hdr.innerText || '').trim() : '';
                const comp = main.querySelector('footer div[contenteditable="true"], div[contenteditable="true"][data-tab="10"], [role="textbox"][data-tab="10"]');
                return {
                    found: true,
                    header: htxt,
                    composer: !!comp,
                    focused: !!(comp && (document.activeElement === comp || comp.contains(document.activeElement))),
                    text: comp ? (comp.innerText || comp.textContent || '') : '',
                    url: location.href,
                };
            }"""
        )
    except Exception:
        info = {}
    header = str((info or {}).get("header") or "").strip()
    has_comp = bool((info or {}).get("composer"))
    exp_fold = fold(expected_contact) if expected_contact else ""
    matched = header if (header and is_valid_contact_label(header) and (not exp_fold or contact_match(expected_contact, header) != "none")) else ""
    verified = bool(matched and has_comp)
    if not verified:
        if not (info or {}).get("found"):
            reason = "WhatsApp Web has no open conversation pane"
        elif header and exp_fold and not matched:
            reason = f"active chat header is {header!r}, not {expected_contact!r}"
        elif matched and not has_comp:
            reason = "the message box is not visible"
        else:
            reason = "no active conversation header was found"
    else:
        reason = ""
    chat_id = f"chat:{fold(matched)}" if matched else ""
    if verified:
        set_active_chat(
            platform="whatsapp_web", contact_name=matched, chat_id=chat_id,
            chat_url=str((info or {}).get("url") or ""), chat_header=matched,
            source="dom", verified=True, confidence=0.98,
        )
    return {
        "verified": verified,
        "active_contact": matched,
        "chat_id": chat_id,
        "chat_header": matched,
        "chat_url": str((info or {}).get("url") or ""),
        "platform": "whatsapp_web",
        "application": "chrome",
        "has_composer": has_comp,
        "composer_text": normalize_composer_text((info or {}).get("text") or ""),
        "reason": reason,
        "source": "dom",
        "confidence": (0.98 if verified else 0.15),
    }


async def dom_locate_whatsapp_composer(page) -> dict:
    """Locate & verify WhatsApp Web composer element strictly inside `#main footer` via Playwright DOM."""
    try:
        meta = await page.evaluate(
            """() => {
                const main = document.querySelector('#main');
                if (!main) return {found: false};
                const comp = main.querySelector('footer div[contenteditable="true"], div[contenteditable="true"][data-tab="10"], footer [role="textbox"]');
                if (!comp) return {found: false};
                const hdr = main.querySelector('header span[title], header span[dir="auto"]');
                const htxt = hdr ? (hdr.getAttribute('title') || hdr.innerText || '').trim() : '';
                const r = comp.getBoundingClientRect();
                return {
                    found: true,
                    header: htxt,
                    text: (comp.innerText || comp.textContent || ''),
                    focused: document.activeElement === comp || comp.contains(document.activeElement),
                    rect: [Math.round(r.left), Math.round(r.top), Math.round(r.width), Math.round(r.height)],
                };
            }"""
        )
    except Exception:
        meta = {}
    if not (meta or {}).get("found"):
        return {
            "composer_found": False, "composer_type": "none", "application": "chrome",
            "active_chat_id": get_active_chat().get("chat_id") or "",
            "focus_verified": False, "observation_id": "dom", "confidence": "uncertain",
        }
    hdr = str(meta.get("header") or "").strip()
    cid = f"chat:{fold(hdr)}" if hdr else str(get_active_chat().get("chat_id") or "")
    return {
        "composer_found": True,
        "composer_type": "dom_contenteditable",
        "application": "chrome",
        "active_chat_id": cid,
        "focus_verified": bool(meta.get("focused")),
        "observation_id": "dom",
        "confidence": "high",
        "composer_text": normalize_composer_text(meta.get("text") or ""),
        "rect": tuple(meta.get("rect") or (0, 0, 0, 0)),
        "source": "dom",
    }


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
        from core import appidentity, desktop_control, screenact, screenview, task_control

        gen = task_control.cancel_generation()
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

        hdr = verify_whatsapp_active_chat(view, expected_contact=receiver)
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
            hdr = verify_whatsapp_active_chat(view, expected_contact=receiver)
            if not hdr.get("verified"):
                return False, "active chat title does not match the requested contact; nothing was typed"

        return self._type_and_submit(view, receiver, message, task_id, gen, dhash)

    def _type_and_submit(self, view, receiver: str, message: str, task_id: str, gen: int, dhash: str) -> tuple[bool, str]:
        """Type into the verified chat, read the draft back, submit, verify the bubble."""
        from core import desktop_control, screenact, task_control
        if task_control.was_cancelled_since(gen) or desktop_control.should_stop():
            return False, "cancelled before anything was typed"
        comp = locate_whatsapp_composer(view)
        if not comp.get("composer_found"):
            return False, "No unique message field was found."
        pt_comp = screenact.Point(comp["x"], comp["y"], origin="element", label=comp["label"])
        entered = screenact.type_text(message, target=pt_comp, clear_first=True)
        if not entered.ok:
            return False, f"could not enter the message: {entered.error}"

        # ---- the draft must be READ BACK as the intended text before Enter is allowed
        staged = self._view()
        rb = read_composer(staged)
        want = fold(normalize_composer_text(message))
        got = fold(rb["text"])
        if rb["status"] != "has_text":
            return False, ("the message box could not be read back (" + rb["status"] + "), so Enter was not pressed "
                           "and nothing was sent")
        if want[:60] != got[:60] or (len(want) <= 60 and want != got):
            return False, "the message box does not contain exactly the intended text; it was not submitted"
        hdr2 = verify_whatsapp_active_chat(staged, expected_contact=receiver)
        if not hdr2.get("verified"):
            return False, f"the open conversation is no longer {receiver!r}; it was not submitted"
        if task_control.was_cancelled_since(gen) or desktop_control.should_stop():
            return False, "cancelled before sending; the draft was left in the message box"

        # Enter may succeed even if we cannot SEE it succeed, so the attempt is recorded
        # first: an unconfirmed send must never be retried into a duplicate.
        desktop_control.record_sent_attempt(receiver=receiver, draft_hash=dhash, task_id=task_id)
        sent = screenact.press("enter")
        if not sent.ok:
            return False, f"message submission failed: {sent.error}"
        time.sleep(0.30)
        after = self._view()
        if not verify_outgoing_message(after, message, before_view=staged):
            return False, ("unverified: Enter was pressed but the message was not confirmed in the chat; it may "
                           "have been sent, so I will not send it again")
        desktop_control.mark_composition_sent(receiver=receiver, draft_hash=dhash)
        return True, "exact chat matched, draft read back, message submitted, and outgoing bubble verified"


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
