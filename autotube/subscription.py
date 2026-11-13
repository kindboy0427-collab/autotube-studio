# -*- coding: utf-8 -*-
"""Use the subscriptions you already pay for - through their OFFICIAL command-line agents.

- Claude Pro  -> Claude Code headless mode:  claude -p ... --output-format json
- ChatGPT Plus -> Codex non-interactive mode: codex exec ... -o last_message.txt

Both are the vendors' own automation tools, so this is normal, permitted use (unlike
driving the ChatGPT app with screen automation). They spend your plan's included
usage; when a plan's usage window is full we pause that provider for a while and
the caller falls back to the next one.

One-time setup on the PC (each opens a browser to log in with your plan account):
    claude        (Claude Code, log in with your Claude Pro account)
    codex login   (Codex CLI, choose "Sign in with ChatGPT")
"""
from __future__ import annotations
import datetime as dt
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from . import budget, db

LIMIT_WORDS = ("usage limit", "rate limit", "limit reached", "quota", "too many requests", "5-hour", "weekly limit",
               "사용량", "한도")
COOLDOWN_MINUTES = 60


class SubscriptionError(RuntimeError):
    pass


class SubscriptionLimit(SubscriptionError):
    pass


def _bin(name):
    return shutil.which(name) or shutil.which(name + ".cmd") or shutil.which(name + ".exe")


def available(name):
    if not _bin({"claude_code": "claude", "codex": "codex"}[name]):
        return False
    until = db.kv_get("cooldown:" + name)
    if until and dt.datetime.fromisoformat(until) > dt.datetime.now(dt.timezone.utc):
        return False
    return True


def status():
    out = {}
    for name in ("claude_code", "codex"):
        until = db.kv_get("cooldown:" + name)
        out[name] = {"installed": bool(_bin({"claude_code": "claude", "codex": "codex"}[name])),
                     "paused_until": until if until and dt.datetime.fromisoformat(until) > dt.datetime.now(dt.timezone.utc) else None}
    return out


def _cooldown(name, minutes=COOLDOWN_MINUTES):
    until = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=minutes)).isoformat(timespec="seconds")
    db.kv_set("cooldown:" + name, until)
    db.event("info", "subscription", f"{name} 요금제 사용량 한도 → {minutes}분 쉬고 다음 AI로 진행")


def _clean_env(drop):
    env = os.environ.copy()
    for k in drop:  # make sure the CLI bills the subscription login, not an API key
        env.pop(k, None)
    return env


def _check_limit(name, text):
    low = (text or "").lower()
    if any(w in low for w in LIMIT_WORDS):
        _cooldown(name)
        raise SubscriptionLimit(f"{name}: 요금제 사용량 한도")


def run_claude_code(prompt: str, system: str = "", production_id=None, timeout=1200, runner=subprocess.run) -> str:
    exe = _bin("claude")
    if not exe:
        raise SubscriptionError("Claude Code 미설치")
    args = [exe, "-p", "위 입력의 지시를 그대로 수행하고, 요구된 출력만 내보내라.", "--output-format", "json", "--max-turns", "1"]
    if system:
        args += ["--append-system-prompt", system]
    env = _clean_env(["ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX"])
    with tempfile.TemporaryDirectory(prefix="autotube_claude_") as tmp:
        p = runner(args, input=prompt, cwd=tmp, env=env, capture_output=True, text=True, encoding="utf-8",
                   errors="replace", timeout=timeout)
    raw = (p.stdout or "").strip()
    try:
        data = json.loads(raw) if raw else {}
    except ValueError:
        data = {}
    text = data.get("result") if isinstance(data, dict) else None
    if p.returncode != 0 or data.get("is_error") or not text:
        _check_limit("claude_code", (p.stderr or "") + raw)
        raise SubscriptionError("Claude Code 실행 실패: " + ((p.stderr or raw)[-300:] or "빈 응답"))
    budget.record("claude_subscription", len(prompt) + len(text), 0.0, "Claude Pro (Claude Code)", production_id)
    return text


def run_codex(prompt: str, system: str = "", production_id=None, timeout=1200, runner=subprocess.run) -> str:
    exe = _bin("codex")
    if not exe:
        raise SubscriptionError("Codex 미설치")
    env = _clean_env(["OPENAI_API_KEY", "CODEX_API_KEY"])
    with tempfile.TemporaryDirectory(prefix="autotube_codex_") as tmp:
        out = Path(tmp) / "last_message.txt"
        args = [exe, "exec", "--skip-git-repo-check", "--sandbox", "read-only", "--ephemeral", "-o", str(out), "-"]
        full = (("[역할]\n" + system + "\n\n") if system else "") + prompt + \
            "\n\n[주의] 파일을 만들거나 명령을 실행하지 말고, 요구된 출력만 최종 답변으로 내보내라."
        p = runner(args, input=full, cwd=tmp, env=env, capture_output=True, text=True, encoding="utf-8",
                   errors="replace", timeout=timeout)
        text = out.read_text(encoding="utf-8") if out.exists() else ""
    if p.returncode != 0 or not text.strip():
        _check_limit("codex", (p.stderr or "") + (p.stdout or ""))
        raise SubscriptionError("Codex 실행 실패: " + ((p.stderr or p.stdout or "")[-300:] or "빈 응답"))
    budget.record("codex_subscription", len(full) + len(text), 0.0, "ChatGPT Plus (Codex)", production_id)
    return text
