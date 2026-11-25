"""Disposable Tk window used only by run_mouse_acceptance.py."""
from __future__ import annotations

import json
import sys
import tkinter as tk
from pathlib import Path


state_path = Path(sys.argv[1])
stop_path = Path(sys.argv[2])
state = {"ready": False, "events": {}, "mouse_down": False, "drop_success": False,
         "keyboard_text": "", "keyboard_secondary": "",
         "keyboard_focus": "", "key_events": {}}


def save_state():
    if not root.winfo_exists():
        return
    root.update_idletasks()
    controls = {}
    for name, widget in widgets.items():
        controls[name] = {
            "left": widget.winfo_rootx(), "top": widget.winfo_rooty(),
            "width": widget.winfo_width(), "height": widget.winfo_height(),
            "hwnd": int(widget.winfo_id()),
        }
    state["controls"] = controls
    state["window"] = {"title": root.title(), "left": root.winfo_rootx(),
                       "top": root.winfo_rooty(), "width": root.winfo_width(),
                       "height": root.winfo_height()}
    temp = state_path.with_suffix(".tmp")
    temp.write_text(json.dumps(state), encoding="utf-8")
    temp.replace(state_path)


def event(name):
    state["events"][name] = state["events"].get(name, 0) + 1
    save_state()


root = tk.Tk()
root.title("REM Disposable Mouse Acceptance")
root.geometry("900x760+120+80")
root.resizable(False, False)
root.attributes("-topmost", True)
tk.Label(root, text="REM MOUSE ACCEPTANCE — disposable test surface",
         font=("Segoe UI", 16, "bold")).pack(pady=12)
marker = tk.Canvas(root, width=168, height=44, bg="#f4f4f4", highlightthickness=0)
marker.create_rectangle(8, 6, 46, 38, fill="#ff00ff", outline="#ff00ff")
marker.create_text(108, 22, text="REM_CAPTURE_MARKER", fill="#111111")
marker.pack(pady=2)

tk.Label(root, text="Disposable keyboard target — test text only").pack()
keyboard = tk.Text(root, width=66, height=3, undo=True)
keyboard.pack(pady=3)
widgets = {"keyboard": keyboard}

def keyboard_update(_event=None):
    state["keyboard_text"] = keyboard.get("1.0", "end-1c")
    save_state()

def keyboard_focus_in(_event=None):
    state["keyboard_focus"] = "keyboard"
    save_state()

def keyboard_focus_out(_event=None):
    root.after_idle(refresh_keyboard_focus)

def keyboard_keypress(event):
    key = str(event.keysym)
    state["key_events"][key] = state["key_events"].get(key, 0) + 1
    if event.state & 0x4:
        chord = "Ctrl+" + key.casefold()
        state["key_events"][chord] = state["key_events"].get(chord, 0) + 1
    save_state()

keyboard.bind("<KeyRelease>", keyboard_update, add="+")
keyboard.bind("<FocusIn>", keyboard_focus_in, add="+")
keyboard.bind("<FocusOut>", keyboard_focus_out, add="+")
keyboard.bind("<KeyPress>", keyboard_keypress, add="+")

tk.Label(root, text="Second keyboard focus target").pack()
keyboard_secondary = tk.Entry(root, width=56)
keyboard_secondary.pack(pady=2)
widgets["keyboard_secondary"] = keyboard_secondary

def secondary_update(_event=None):
    state["keyboard_secondary"] = keyboard_secondary.get()
    save_state()

def secondary_focus_in(_event=None):
    state["keyboard_focus"] = "secondary"
    save_state()

def secondary_focus_out(_event=None):
    root.after_idle(refresh_keyboard_focus)

def refresh_keyboard_focus():
    if not root.winfo_exists():
        return
    focused = root.focus_get()
    state["keyboard_focus"] = ("keyboard" if focused is keyboard else
                               "secondary" if focused is keyboard_secondary else "")
    save_state()

keyboard_secondary.bind("<KeyRelease>", secondary_update, add="+")
keyboard_secondary.bind("<FocusIn>", secondary_focus_in, add="+")
keyboard_secondary.bind("<FocusOut>", secondary_focus_out, add="+")

def tab_to_secondary(_event=None):
    keyboard_secondary.focus_set()
    return "break"

keyboard.bind("<Tab>", tab_to_secondary, add="+")

row = tk.Frame(root)
row.pack(pady=8)
widgets["capture_marker"] = marker

left = tk.Button(row, text="Left click target", width=18, height=3,
                 command=lambda: event("left_click"))
left.grid(row=0, column=0, padx=12)
widgets["left"] = left

right = tk.Label(row, text="Right-click target", relief="ridge", width=18, height=3)
right.grid(row=0, column=1, padx=12)
right.bind("<Button-3>", lambda _e: event("right_click"))
widgets["right"] = right

double = tk.Label(row, text="Double-click target", relief="ridge", width=18, height=3)
double.grid(row=0, column=2, padx=12)
double.bind("<Double-Button-1>", lambda _e: event("double_click"))
widgets["double"] = double

hover = tk.Label(row, text="Hover target", relief="ridge", width=18, height=3)
hover.grid(row=0, column=3, padx=12)
hover.bind("<Enter>", lambda _e: event("hover"))
widgets["hover"] = hover

middle = tk.Label(root, text="Middle-click target", relief="ridge", width=22, height=2)
middle.pack(pady=4)
middle.bind("<Button-2>", lambda _e: event("middle_click"))
widgets["middle"] = middle

hold = tk.Label(root, text="Hold/release target", relief="groove", width=24, height=2)
hold.pack(pady=10)
hold.bind("<ButtonPress-1>", lambda _e: (state.update(mouse_down=True), save_state()))
hold.bind("<ButtonRelease-1>", lambda _e: (state.update(mouse_down=False), event("released")))
widgets["hold"] = hold

drag_frame = tk.Frame(root)
drag_frame.pack(pady=8)
source = tk.Label(drag_frame, text="DRAG ME", relief="raised", width=18, height=3,
                  bg="#cde8ff")
source.grid(row=0, column=0, padx=30)
target = tk.Label(drag_frame, text="DROP HERE", relief="ridge", width=24, height=5,
                  bg="#ddf5df")
target.grid(row=0, column=1, padx=30)
widgets["drag_source"] = source
widgets["drop_target"] = target
drag = {"active": False, "start": (0, 0)}

def drag_start(e):
    drag["active"] = True
    drag["start"] = (e.x_root, e.y_root)
    state["mouse_down"] = True
    state["drop_success"] = False
    save_state()

def drag_end(e):
    if not drag["active"]:
        return
    drag["active"] = False
    state["mouse_down"] = False
    x, y = e.x_root, e.y_root
    bounds = (target.winfo_rootx(), target.winfo_rooty(),
              target.winfo_rootx() + target.winfo_width(),
              target.winfo_rooty() + target.winfo_height())
    state["drop_success"] = bounds[0] <= x <= bounds[2] and bounds[1] <= y <= bounds[3]
    event("drop_success" if state["drop_success"] else "drop_missed")

source.bind("<ButtonPress-1>", drag_start)
root.bind_all("<ButtonRelease-1>", drag_end, add="+")

scroll_frame = tk.Frame(root, relief="sunken", borderwidth=2)
scroll_frame.pack(pady=10)
scroll = tk.Text(scroll_frame, width=78, height=8, wrap="none")
scroll.pack(side="left")
bar = tk.Scrollbar(scroll_frame, command=scroll.yview)
bar.pack(side="right", fill="y")
scroll.configure(yscrollcommand=bar.set)
scroll.insert("1.0", "\n".join(f"scroll content row {i:02d}" for i in range(50)))
scroll.bind("<MouseWheel>", lambda _e: event("scroll"))
widgets["scroll"] = scroll

tk.Label(root, text="This window contains no user data. Close it after the test.",
         fg="#555").pack(pady=4)

def poll_stop():
    if stop_path.exists():
        try:
            stop_path.unlink()
        except OSError:
            pass
        root.destroy()
        return
    root.after(100, poll_stop)

root.update_idletasks()
root.update()  # map the window before publishing screen coordinates
root.attributes("-topmost", True)
root.lift()
root.focus_force()
keyboard.focus_force()
state["ready"] = True
save_state()
root.after(100, poll_stop)
root.mainloop()
