# -*- coding: utf-8 -*-
"""Self-update from GitHub. No manual download/unzip/overwrite.

Source: settings.update.repo (owner/name) @ branch. A public repo needs nothing;
a private repo needs GITHUB_TOKEN (read-only fine-grained token) in 설정 → API 키.
Your data/, output/ and .venv/ are never touched. The previous program files are
kept in data/backup_prev and restored automatically if the new version fails to load.
"""
from __future__ import annotations
import hashlib
import io
import json
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path

from . import config

KEEP = {"data", "output", ".venv", "install_log.txt", ".git"}


def local_version() -> str:
    p = config.ROOT / "VERSION"
    return p.read_text(encoding="utf-8").strip() if p.exists() else "0"


def _vt(v):
    out = []
    for x in str(v).strip().lstrip("v").split("."):
        try:
            out.append(int(x))
        except ValueError:
            out.append(0)
    return tuple(out + [0] * (4 - len(out)))


def _settings():
    return config.load_settings().get("update", {})


def _get(url, timeout=30, accept=None):
    hdr = {"User-Agent": "AutoTubeStudio-updater"}
    tok = config.get_secret("GITHUB_TOKEN")
    if tok and "github" in url:
        hdr["Authorization"] = "Bearer " + tok
    if accept:
        hdr["Accept"] = accept
    req = urllib.request.Request(url, headers=hdr)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def remote_version() -> str:
    s = _settings()
    repo, branch = s.get("repo", ""), s.get("branch", "main")
    if not repo:
        raise RuntimeError("업데이트 저장소가 설정되지 않았습니다")
    if config.get_secret("GITHUB_TOKEN"):
        raw = _get(f"https://api.github.com/repos/{repo}/contents/VERSION?ref={branch}",
                   accept="application/vnd.github.raw")
    else:
        raw = _get(f"https://raw.githubusercontent.com/{repo}/{branch}/VERSION")
    return raw.decode("utf-8").strip()


def check() -> dict:
    cur = local_version()
    try:
        rem = remote_version()
    except Exception as ex:
        return {"current": cur, "latest": None, "available": False, "error": str(ex)[:200]}
    return {"current": cur, "latest": rem, "available": _vt(rem) > _vt(cur), "error": None}


def _download_zip() -> bytes:
    s = _settings()
    repo, branch = s["repo"], s.get("branch", "main")
    if config.get_secret("GITHUB_TOKEN"):
        return _get(f"https://api.github.com/repos/{repo}/zipball/{branch}", timeout=180)
    return _get(f"https://codeload.github.com/{repo}/zip/refs/heads/{branch}", timeout=180)


def _hash(p: Path):
    return hashlib.sha256(p.read_bytes()).hexdigest() if p.exists() else ""


def _program_items(root: Path):
    return [p for p in root.iterdir() if p.name not in KEEP]


def _python():
    exe = config.ROOT / ".venv" / "Scripts" / "python.exe"
    return str(exe) if exe.exists() else sys.executable


def apply(log=print) -> dict:
    """Download, replace program files, reinstall requirements if changed, verify, roll back on failure."""
    info = check()
    if not info["available"]:
        return {"updated": False, **info}
    root = config.ROOT
    backup = config.DATA / "backup_prev"
    old_req = _hash(root / "requirements.txt")
    data = _download_zip()
    with tempfile.TemporaryDirectory() as tmp:
        zipfile.ZipFile(io.BytesIO(data)).extractall(tmp)
        tops = [p for p in Path(tmp).iterdir() if p.is_dir()]
        src = tops[0] if len(tops) == 1 and not (Path(tmp) / "VERSION").exists() else Path(tmp)
        if not (src / "autotube").is_dir() or not (src / "VERSION").exists():
            raise RuntimeError("받은 업데이트 파일 구조가 올바르지 않습니다")
        # 1 backup current program files
        if backup.exists():
            shutil.rmtree(backup, ignore_errors=True)
        backup.mkdir(parents=True)
        for p in _program_items(root):
            (shutil.copytree if p.is_dir() else shutil.copy2)(p, backup / p.name)
        # 2 replace
        for p in _program_items(root):
            if p.is_dir():
                shutil.rmtree(p, ignore_errors=True)
            else:
                p.unlink(missing_ok=True)
        for p in src.iterdir():
            if p.name in KEEP:
                continue
            (shutil.copytree if p.is_dir() else shutil.copy2)(p, root / p.name)
    log(f"업데이트 파일 교체: {info['current']} → {info['latest']}")
    # 3 dependencies
    if _hash(root / "requirements.txt") != old_req:
        log("구성요소 설치 중…")
        subprocess.run([_python(), "-m", "pip", "install", "--disable-pip-version-check", "-q", "-r",
                        str(root / "requirements.txt")], cwd=str(root), capture_output=True,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    # 4 verify, else roll back
    r = subprocess.run([_python(), "-c", "import autotube.web, autotube.launch"], cwd=str(root), capture_output=True,
                       text=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if r.returncode != 0:
        for p in _program_items(root):
            if p.is_dir():
                shutil.rmtree(p, ignore_errors=True)
            else:
                p.unlink(missing_ok=True)
        for p in backup.iterdir():
            (shutil.copytree if p.is_dir() else shutil.copy2)(p, root / p.name)
        raise RuntimeError("새 버전 확인 실패 → 이전 버전으로 되돌렸습니다: " + r.stderr[-300:])
    hist = config.DATA / "update_history.json"
    rows = json.loads(hist.read_text(encoding="utf-8")) if hist.exists() else []
    from . import db
    rows.append({"at": db.now(), "from": info["current"], "to": info["latest"]})
    hist.write_text(json.dumps(rows[-30:], ensure_ascii=False, indent=1), encoding="utf-8")
    return {"updated": True, **info}
