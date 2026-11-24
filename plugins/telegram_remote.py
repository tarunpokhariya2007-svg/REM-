"""Allowlisted Telegram bridge used by core.cloud.TelegramFront.

The bridge accepts text only from paired chat IDs and hands it to the host's
on_text_command callback. It never evaluates text as code or invokes a shell.
"""
from __future__ import annotations

import queue
import threading
import time
from plugins._common import config, data_file, plugin, read_json, write_json, safe_run

PLUGIN=plugin("telegram_remote","Securely pair Telegram chat IDs and relay their text to REM's restricted remote assistant. Commands are allowlisted by the cloud task engine; this plugin never executes shell or Python code.",{"action":{"type":"STRING","description":"status | start | stop"}},["action"])
PLUGIN_SETTINGS={"namespace":"telegram_remote","title":"Telegram Remote","fields":[{"key":"bot_token","label":"Bot token","type":"password"},{"key":"allowed_chat_ids","label":"Authorized chat IDs","type":"text"},{"key":"pairing_code","label":"One-time pairing code","type":"password"}]}

_state={"running":False,"stopping":False,"turn_chat":None,"turn_open":False,"turn_at":0.0,"seen":0,"skew":None}
_lock=threading.RLock(); _stop=threading.Event(); _thread=None; _player=None; _out=queue.Queue(maxsize=100); _offset=0

def _cfg(): return config("telegram_remote")
def _parse_ids(value):
    if isinstance(value,(list,tuple,set)): values=value
    else: values=str(value or "").replace(";",",").split(",")
    out=[]
    for v in values:
        try: out.append(int(str(v).strip()))
        except (TypeError,ValueError): continue
    return sorted(set(out))
def _turn_active():
    return bool(_state.get("turn_open") and time.monotonic()-float(_state.get("turn_at",0))<180)
def _keyboard(cfg): return {"remove_keyboard":True}
def _put(item):
    try: _out.put_nowait(item); return True
    except queue.Full: return False
def _enqueue(chat,text): return _put({"kind":"text","chat":chat,"text":str(text)[:3800]})

def _api(method,*,params=None,json_body=None,files=None,timeout=30):
    import requests
    token="".join(str(_cfg().get("bot_token") or "").split())
    if not token: raise RuntimeError("Telegram bot token is not configured")
    url=f"https://api.telegram.org/bot{token}/{method}"
    if files: response=requests.post(url,data=params,files=files,timeout=timeout)
    elif json_body is not None: response=requests.post(url,json=json_body,timeout=timeout)
    else: response=requests.get(url,params=params,timeout=timeout)
    response.raise_for_status(); data=response.json()
    if not data.get("ok"): raise RuntimeError("Telegram API rejected the request")
    return data.get("result")

def _send(item):
    chat=item.get("chat")
    if not chat: return
    kind=item.get("kind","text")
    if kind=="text": _api("sendMessage",json_body={"chat_id":chat,"text":str(item.get("text",""))[:4000],"reply_markup":item.get("markup")})
    elif kind in ("photo","document"):
        field="photo" if kind=="photo" else "document"; method="sendPhoto" if kind=="photo" else "sendDocument"
        import io
        params={"chat_id":chat,"caption":str(item.get("caption",""))[:1024]}
        _api(method,params=params,files={field:(item.get("name","file.bin"),io.BytesIO(item.get("data",b"")),item.get("mime","application/octet-stream"))},timeout=120)

def _message(update):
    global _offset
    _offset=max(_offset,int(update.get("update_id",0))+1)
    msg=update.get("message") or update.get("edited_message") or {}
    chat=msg.get("chat",{}); cid=chat.get("id")
    if cid is None: return
    text=str(msg.get("text") or "").strip()
    if not text: return
    cfg=_cfg(); approved=_parse_ids(cfg.get("allowed_chat_ids"))
    if int(cid) not in approved:
        parts=text.split(maxsplit=1)
        pair=str(cfg.get("pairing_code") or "").strip()
        if parts and parts[0].lower() in ("/start","/pair") and len(parts)>1 and pair and parts[1].strip()==pair:
            approved.append(int(cid)); from memory.config_manager import save_plugin_config
            save_plugin_config("telegram_remote",{"allowed_chat_ids":",".join(map(str,sorted(set(approved)))),"pairing_code":""})
            _enqueue(cid,"This chat is paired with REM. Keep the pairing code private.")
        return
    if _player is None or not callable(getattr(_player,"on_text_command",None)):
        _enqueue(cid,"REM is not accepting commands right now."); return
    with _lock:
        _state.update(turn_chat=int(cid),turn_open=True,turn_at=time.monotonic(),seen=int(_state.get("seen",0))+1)
    try: _player.on_text_command(text)
    except Exception as exc:
        print(f"[Telegram] command dispatch failed: {type(exc).__name__}")
        _enqueue(cid,"That command could not be handed to REM.")

def _poll():
    global _offset
    saved=read_json(data_file("telegram_remote_offset.json"),{"offset":0}); _offset=max(_offset,int(saved.get("offset",0)))
    failures=0
    while not _stop.is_set():
        try:
            updates=_api("getUpdates",params={"offset":_offset,"timeout":20,"allowed_updates":"[\"message\"]"},timeout=30) or []
            failures=0
            for update in updates:
                _message(update)
                write_json(data_file("telegram_remote_offset.json"),{"offset":_offset})
            try:
                item=_out.get_nowait(); _send(item)
            except queue.Empty: pass
        except Exception as exc:
            failures+=1
            # Do not log the URL: it contains the bot token.
            print(f"[Telegram] poll failed: {type(exc).__name__}")
            if "409" in str(exc): break
            _stop.wait(min(30,2**min(failures,5)))
    with _lock: _state.update(running=False,stopping=False,turn_open=False,turn_chat=None)

def run(parameters,player=None,session_memory=None):
    global _thread,_player
    try:
        action=str((parameters or {}).get("action","status")).lower()
        if action=="status": return f"Telegram bridge is {'running' if _state['running'] else 'stopped'}; {len(_parse_ids(_cfg().get('allowed_chat_ids')))} authorized chat(s)."
        if action=="start":
            if not str(_cfg().get("bot_token") or "").strip(): return "Configure the Telegram bot token in plugin settings."
            if not _parse_ids(_cfg().get("allowed_chat_ids")) and not str(_cfg().get("pairing_code") or "").strip(): return "Configure an authorized chat ID or one-time pairing code."
            with _lock:
                if _state["running"]: return "Telegram bridge is already running."
                _player=player; _stop.clear(); _state.update(running=True,stopping=False)
                _thread=threading.Thread(target=_poll,name="rem-telegram-bridge",daemon=True); _thread.start()
            return "Telegram bridge started for paired chat IDs."
        if action=="stop":
            with _lock: _state["stopping"]=True; _stop.set()
            if _thread and _thread is not threading.current_thread(): _thread.join(timeout=3)
            return "Telegram bridge stop requested."
        return "Choose status, start, or stop."
    except Exception as exc: return f"Telegram bridge failed safely: {type(exc).__name__}: {exc}"

def _set_offset(offset):
    global _offset
    _offset=int(offset)
