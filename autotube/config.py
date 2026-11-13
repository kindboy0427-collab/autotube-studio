# -*- coding: utf-8 -*-
"""Paths, settings and API keys.

Keys come from (1) environment variables, else (2) data/secrets.json entered in
the web settings page. Settings live in data/settings.json and are merged over
DEFAULTS, so new options appear automatically after an update.
"""
from __future__ import annotations
import copy
import json
import os
import shutil
import threading
from pathlib import Path

ROOT = Path(os.environ.get("AUTOTUBE_ROOT", Path(__file__).resolve().parents[1]))
DATA = ROOT / "data"
OUTPUT = ROOT / "output"
ASSETS = ROOT / "assets"
_LOCK = threading.RLock()

SECRET_KEYS = {
    "GEMINI_API_KEY": "🎬 제작용 Gemini 키 (유료 결제된 myvoice 프로젝트) — 내 목소리 내레이션·AI 이미지·AI 영상",
    "GEMINI_FREE_API_KEY": "📚 학습용 Gemini 키 (결제 안 한 무료 프로젝트) — 유튜브 영상 시청·분석·주제 추천, 하루 8시간 무료",
    "YOUTUBE_API_KEY": "YouTube Data API v3 (검색·통계·댓글)",
    "OPENAI_API_KEY": "OpenAI API (대본, 보조 음성)",
    "ELEVENLABS_API_KEY": "ElevenLabs (내 목소리 복제·감정 연기·배경음악)",
    "PEXELS_API_KEY": "Pexels (무료 스톡 영상·사진)",
    "GITHUB_TOKEN": "GitHub 토큰 — 지금은 필요 없음 (비워두세요. 업데이트 저장소를 비공개로 바꿀 때만 사용)",
    "APIFY_TOKEN": "Apify (자막으로 저렴하게 영상 분석 — Gemini 무료 한도 소진 시)",
}

DEFAULTS = {
    "version": 13,
    "language": "ko",
    # ---------- learning (AI watching) ----------
    "learning": {
        "enabled": True,
        "niches": [],                    # e.g. ["재테크", "역사 미스터리"]; empty = learn from production topics only
        "max_searches_per_day": 40,      # search.list = 100 quota units each
        "max_watch_per_day": 60,         # videos analysed per day (all methods)
        "gemini_full_watch_per_day": 10, # hybrid: best outliers watched by Gemini (picture+sound), the rest via captions
        "free_tier_watch_hours": 8.0,    # Gemini free tier limit for YouTube URLs
        "gemini_paid_tier": False,       # True = no hour cap (paid Gemini API tier)
        "min_duration_seconds": 240,     # long-form only
        "max_duration_seconds": 3600,
        "published_within_days": 180,
        "watch_batch": 4,                # videos per scheduler tick
        "watch_mode": "hybrid",          # hybrid: top N/day full watch by Gemini + rest captions (cheap, ~$0.003 each)
                                         # auto: Gemini until the free-tier hours run out, then captions
                                         # gemini: Gemini only / transcript: captions only (cheapest)
        "tick_minutes": 20,
    },
    # ---------- production ----------
    "production": {
        "target_minutes": 10,
        "chars_per_second": 8.5,         # Korean narration speed (chars incl. spaces / s); auto-calibrated per voice after each video
        "max_ai_video_clips": 6,         # Gemini Omni clips per video (most expensive part)
        "ai_image_ratio": 0.45,          # (old plans only) share of non-video scenes that use AI images
        "min_real_ratio": 0.6,           # at least 60 % of scenes use real footage/photos (archives, stock, people)
        "image_source": "ai",            # ai: Gemini makes images (paid) / manual: you make them (e.g. ChatGPT) from the prompt list
        "burn_subtitles": True,
        "upload_srt_caption": False,
        "auto_upload_private": True,
        "max_cost_per_video_usd": 15.0,
        "music": True,
        "self_review": True,             # second AI pass: editor + science fact review
        "script_format": "auto",         # auto / reversal_essay / catalog / staged_scenario / explainer
        "emphasis_pause_seconds": 1.2,   # silence after key lines (music swells under it)
        "align_subtitles": "auto",       # auto: local faster-whisper word timing if installed, else proportional
        "whisper_model": "small",
        "custom_voice_per_sentence": True,  # my/designed Gemini voice: one request per sentence (stops mid-clip voice switching)
        "custom_voice_style": "off",       # off: no style cue for my voice (cues can pull it away from my timbre) / light
        "voice_check": True,               # regenerate scenes whose voice pitch jumps (male<->female)
        "voice_fallback": "same_voice",  # same_voice: a channel with its own voice never switches to another voice mid-video
                                         # any: fall back to other engines (mixed voices)
        "resolution": "1920x1080",
        "fps": 30,
    },
    "providers": {
        "script": "auto",                # auto: Claude Pro(Claude Code) > ChatGPT Plus(Codex) > OpenAI API > Gemini API
        "tts": "auto",                   # auto(Gemini TTS > ElevenLabs > OpenAI > edge) / gemini / elevenlabs / openai / edge
        "gemini_tts_model": "gemini-3.8-flash-tts",      # GA 2026-09; flash-lite-tts is cheaper
        "gemini_tts_voice": "Kore",                       # prebuilt voice, or a voice_... id (your replicated voice)
        "gemini_text_model": "gemini-3.8-flash",
        "gemini_watch_model": "gemini-3.8-flash",
        "gemini_image_model": "gemini-3.1-flash-image",
        "gemini_video_model": "gemini-omni-1.1-flash",
        "openai_text_model": "gpt-6-luna",
        "openai_tts_model": "gpt-4o-mini-tts",   # OpenAI announced shutdown 2027-01-06
        "openai_tts_voice": "coral",
        "elevenlabs_model": "eleven_multilingual_v2",  # supports character timestamps
        "elevenlabs_voice_id": "JBFqnCBsd6RMkjVDRZzb",
        "edge_voice": "ko-KR-SunHiNeural",
    },
    # Editable cost estimates (USD) used only for budgeting; check real prices in each console.
    "prices": {
        "gemini_text_per_mtok_in": 0.30, "gemini_text_per_mtok_out": 2.50,
        "gemini_watch_per_mtok": 0.30,
        "gemini_image_each": 0.04,
        "gemini_video_per_second": 0.15,
        "openai_per_mtok_in": 0.40, "openai_per_mtok_out": 1.60,
        "elevenlabs_per_kchar": 0.10,
        "gemini_tts_per_minute": 0.0135,   # Flash TTS standard, 2026 price; Google doubles it from 2027-01-01 -> set 0.027
        "openai_tts_per_kchar": 0.015,
        "elevenlabs_music_per_minute": 0.30,
        "apify_transcript_each": 0.003,
    },
    "budget": {"daily_usd": 10.0, "monthly_usd": 150.0},
    "youtube_quota_per_day": 10000,
    # ---------- self-update (no manual download) ----------
    "update": {"auto": True, "repo": "kindboy0427-collab/autotube-studio", "branch": "main", "check_hours": 1},
    "web_port": 8770,
}

QUOTA_COST = {"search": 100, "videos": 1, "channels": 1, "playlist": 1, "comments": 1, "upload": 100,
              "captions": 400, "thumbnail": 50, "update": 50}


def _deep_merge(base, extra):
    out = copy.deepcopy(base)
    for k, v in (extra or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def ensure_dirs():
    for p in (DATA, OUTPUT, ASSETS / "music", ASSETS / "fonts"):
        p.mkdir(parents=True, exist_ok=True)


def _atomic_write(path: Path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def load_settings() -> dict:
    with _LOCK:
        p = DATA / "settings.json"
        saved = {}
        if p.exists():
            try:
                saved = json.loads(p.read_text(encoding="utf-8-sig"))
            except ValueError:
                saved = {}
        return _deep_merge(DEFAULTS, saved)


def save_settings(patch: dict) -> dict:
    with _LOCK:
        merged = _deep_merge(load_settings(), patch)
        _atomic_write(DATA / "settings.json", merged)
        return merged


def _secrets_file():
    return DATA / "secrets.json"


def get_secret(name: str) -> str:
    v = os.environ.get(name)
    if v:
        return v.strip()
    p = _secrets_file()
    if p.exists():
        try:
            return str(json.loads(p.read_text(encoding="utf-8")).get(name, "")).strip()
        except ValueError:
            return ""
    return ""


def set_secrets(values: dict):
    with _LOCK:
        p = _secrets_file()
        cur = {}
        if p.exists():
            try:
                cur = json.loads(p.read_text(encoding="utf-8"))
            except ValueError:
                cur = {}
        for k, v in values.items():
            if k not in SECRET_KEYS:
                continue
            v = (v or "").strip()
            if v:
                cur[k] = v
            elif k in cur and v == "":
                pass  # empty field = keep existing value
        _atomic_write(p, cur)


def delete_secret(name: str):
    with _LOCK:
        p = _secrets_file()
        if p.exists():
            cur = json.loads(p.read_text(encoding="utf-8"))
            cur.pop(name, None)
            _atomic_write(p, cur)


def secret_status() -> dict:
    return {k: {"set": bool(get_secret(k)), "from_env": bool(os.environ.get(k)), "label": label}
            for k, label in SECRET_KEYS.items()}


def ffmpeg_bin() -> str:
    found = shutil.which("ffmpeg")
    if found:
        return found
    try:
        import imageio_ffmpeg  # bundled binary fallback (pip install imageio-ffmpeg)
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return "ffmpeg"


def font_path() -> str:
    candidates = [
        os.environ.get("AUTOTUBE_FONT", ""),
        str(ASSETS / "fonts" / "font.ttf"),
        r"C:\Windows\Fonts\malgunbd.ttf", r"C:\Windows\Fonts\malgun.ttf",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Black.ttc",
        "/usr/share/fonts/truetype/nanum/NanumGothicBold.ttf",
    ]
    for c in candidates:
        if c and os.path.isfile(c):
            return c
    return ""


def font_name() -> str:
    p = font_path().lower()
    if "malgun" in p:
        return "Malgun Gothic"
    if "noto" in p:
        return "Noto Sans CJK KR"
    if "nanum" in p:
        return "NanumGothic"
    return "Arial"
