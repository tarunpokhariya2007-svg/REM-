"""Data-free disposable screen used by the Gemini Live image smoke test."""
import json
import sys
import tkinter as tk
from pathlib import Path

state_path = Path(sys.argv[1])
stop_path = Path(sys.argv[2])
root = tk.Tk()
root.title("REM Gemini Live Disposable Image Test")
root.geometry("860x420+180+150")
root.resizable(False, False)
root.attributes("-topmost", True)
tk.Label(root, text="REM LIVE IMAGE ACCEPTANCE 7319",
         font=("Segoe UI", 23, "bold")).pack(pady=60)
tk.Label(root, text="Unique marker: purple teapot",
         font=("Segoe UI", 19)).pack(pady=25)
tk.Button(root, text="Disposable test control",
          font=("Segoe UI", 15)).pack(pady=20)
root.update_idletasks()
root.update()
root.attributes("-topmost", True)
root.lift()
root.focus_force()
state_path.write_text(json.dumps({"ready": True}), encoding="utf-8")

def stop_check():
    if stop_path.exists():
        root.destroy()
        return
    root.after(100, stop_check)

root.after(100, stop_check)
root.mainloop()
