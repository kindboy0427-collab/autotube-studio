# -*- coding: utf-8 -*-
"""AutoTube Studio.

Windows: every child process the app starts (ffmpeg, Claude Code / Codex CLIs, pip, yt-dlp...)
runs without a console window and at below-normal CPU priority, so no cmd windows pop up and
steal the keyboard focus, and the PC stays responsive while videos render in the background.
"""
import os
import subprocess
import tempfile
from pathlib import Path

# temp files next to the app (F:), never on a nearly full C: drive
_TMP = Path(__file__).resolve().parents[1] / "data" / "tmp"
try:
    _TMP.mkdir(parents=True, exist_ok=True)
    tempfile.tempdir = str(_TMP)
    os.environ["TEMP"] = os.environ["TMP"] = str(_TMP)
except OSError:
    pass

if os.name == "nt" and not getattr(subprocess, "_autotube_quiet", False):
    _NO_WINDOW = 0x08000000      # CREATE_NO_WINDOW
    _BELOW_NORMAL = 0x00004000   # BELOW_NORMAL_PRIORITY_CLASS
    _orig_init = subprocess.Popen.__init__

    def _quiet_init(self, *args, **kwargs):
        flags = kwargs.get("creationflags") or 0
        if not flags & 0x00000008:   # leave DETACHED_PROCESS launches (the app's own restarts) as they are
            flags |= _NO_WINDOW
        if not flags & 0x00000080 and not flags & 0x00008000:  # unless HIGH/ABOVE_NORMAL was asked for
            flags |= _BELOW_NORMAL
        kwargs["creationflags"] = flags
        _orig_init(self, *args, **kwargs)

    subprocess.Popen.__init__ = _quiet_init
    subprocess._autotube_quiet = True
