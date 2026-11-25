"""Visible-session launcher for the provider probe and REM adapter acceptance."""
import contextlib
import os
import runpy
import sys
from pathlib import Path

base = Path(__file__).resolve().parent
os.environ["REM_UIA_ACCEPTANCE"] = "1"
log_path = base / "uia_accessibility_acceptance.jsonl"
sys.path.insert(0, str(base.parent.parent))
with log_path.open("w", encoding="utf-8") as stream:
    with contextlib.redirect_stdout(stream):
        try:
            runpy.run_path(str(base / "uia_accessibility_probe.py"),
                           run_name="__main__")
        except SystemExit:
            pass

# Keep the standalone provider probe separate from acceptance: the second
# runner calls REM's actual screenview scanner and semantic action entry point.
runpy.run_path(str(base / "run_rem_uia_adapter_acceptance.py"),
               run_name="__main__")
