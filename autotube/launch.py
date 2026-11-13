# -*- coding: utf-8 -*-
"""Desktop launcher: double-click 'AutoTube Studio' -> its own app window (no browser tabs, no black console).

  pythonw -m autotube.launch            open the app window (starts the engine if needed, updates first)
  pythonw -m autotube.launch --background   start only the engine (used at Windows login)
  pythonw -m autotube.launch --restart      stop the running engine and start it again (after an update)

The engine keeps learning while the window is closed. Use [프로그램 종료] in the app to stop it.
"""
from __future__ import annotations
import json
import os
import subprocess
import sys
import time
import urllib.request
import webbrowser
from pathlib import Path

from . import config

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
DETACHED = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)


def _port():
    return config.load_settings().get("web_port", 8770)


def url():
    return f"http://127.0.0.1:{_port()}/"


def log(msg):
    p = config.DATA / "logs"
    p.mkdir(parents=True, exist_ok=True)
    with open(p / "launcher.log", "a", encoding="utf-8") as f:
        f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + str(msg) + "\n")


def running():
    try:
        with urllib.request.urlopen(url() + "api/ping", timeout=2) as r:
            return json.loads(r.read().decode()).get("app") == "autotube"
    except Exception:
        return False


def _pythonw():
    for name in ("pythonw.exe", "python.exe"):
        p = config.ROOT / ".venv" / "Scripts" / name
        if p.exists():
            return str(p)
    exe = Path(sys.executable)
    w = exe.with_name("pythonw.exe")
    return str(w if w.exists() else exe)


def start_engine():
    logs = config.DATA / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    out = open(logs / "engine.log", "a", encoding="utf-8")
    subprocess.Popen([_pythonw(), "-m", "autotube.web", "--no-browser"], cwd=str(config.ROOT),
                     stdout=out, stderr=out, stdin=subprocess.DEVNULL, creationflags=DETACHED | NO_WINDOW)
    for _ in range(120):
        if running():
            return True
        time.sleep(0.5)
    return False


def stop_engine():
    try:
        req = urllib.request.Request(url() + "api/shutdown?local=1", data=b"{}", method="POST",
                                     headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=3)
    except Exception:
        pass
    for _ in range(40):
        if not running():
            return True
        time.sleep(0.25)
    return False


def _browser():
    env = os.environ
    cands = [
        Path(env.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Microsoft/Edge/Application/msedge.exe",
        Path(env.get("ProgramFiles", r"C:\Program Files")) / "Microsoft/Edge/Application/msedge.exe",
        Path(env.get("ProgramFiles", r"C:\Program Files")) / "Google/Chrome/Application/chrome.exe",
        Path(env.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Google/Chrome/Application/chrome.exe",
        Path(env.get("LOCALAPPDATA", "")) / "Google/Chrome/Application/chrome.exe",
    ]
    return next((str(c) for c in cands if c.exists()), None)


def open_window():
    b = _browser()
    if b:  # app mode = a standalone window with its own taskbar icon, no tabs or address bar
        profile = config.DATA / "window_profile"
        subprocess.Popen([b, f"--app={url()}", f"--user-data-dir={profile}", "--window-size=1440,920",
                          "--no-first-run", "--no-default-browser-check"], creationflags=NO_WINDOW)
    else:
        webbrowser.open(url())


def try_update():
    if not config.load_settings().get("update", {}).get("auto", True):
        return
    try:
        from . import updater
        res = updater.apply(log)
        if res.get("updated"):
            log(f"자동 업데이트 완료 {res['current']} → {res['latest']}")
    except Exception as ex:
        log(f"업데이트 건너뜀: {ex}")


def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    config.ensure_dirs()
    if "--restart" in argv:
        stop_engine()
        time.sleep(1)
    if not running():
        try_update()
        if not start_engine():
            log("엔진 시작 실패 — data/logs/engine.log 확인")
            webbrowser.open(url())
            return 1
    if "--background" not in argv:
        open_window()
    return 0


if __name__ == "__main__":
    sys.exit(main())
