# -*- coding: utf-8 -*-
"""Subtitle timing for engines that return no word timestamps (Gemini TTS, OpenAI TTS).

If faster-whisper is installed (optional: pip install faster-whisper), the narration is
listened to locally (free, offline) and the SCRIPT text is laid onto the recognised speech
timeline by character position - subtitles always show the script, never recognition errors.
Without it, timings are proportional to text length.
"""
from __future__ import annotations
import threading

from . import config

_MODEL = None
_LOCK = threading.Lock()


def _model():
    global _MODEL
    with _LOCK:
        if _MODEL is None:
            from faster_whisper import WhisperModel  # optional dependency
            name = config.load_settings()["production"].get("whisper_model", "small")
            _MODEL = WhisperModel(name, device="auto", compute_type="int8")
        return _MODEL


def available():
    import importlib
    import importlib.util
    importlib.invalidate_caches()
    return importlib.util.find_spec("faster_whisper") is not None


def _has_vad():
    try:
        import onnxruntime  # noqa: F401  (optional; no wheel yet for the newest Python)
        return True
    except Exception:
        return False


def install(log=lambda m: None):
    """One-click install into the app's own Python. Works on Python versions where onnxruntime
    has no wheel yet: installs faster-whisper without it and runs without the VAD filter."""
    import subprocess
    from . import updater
    py = updater._python()
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)

    def pip(*args):
        r = subprocess.run([py, "-m", "pip", "install", "--disable-pip-version-check", *args],
                           capture_output=True, text=True, creationflags=flags)
        return r.returncode == 0, (r.stdout + r.stderr)[-600:]
    log("설치 중: faster-whisper")
    ok, out = pip("faster-whisper")
    if not ok:  # usually onnxruntime missing for this Python -> install the rest without it
        log("onnxruntime 없이 설치 시도")
        ok1, out1 = pip("ctranslate2", "av", "tokenizers", "huggingface_hub", "tqdm")
        ok2, out2 = pip("--no-deps", "faster-whisper")
        ok, out = ok1 and ok2, out1 + out2
    if not ok:
        raise RuntimeError("설치 실패: " + out[-400:])
    log("설치 완료 — 첫 사용 때 음성 인식 모델(약 500MB)을 한 번 내려받습니다")
    return True


def recognise(audio_path):
    segs, _ = _model().transcribe(audio_path, language="ko", word_timestamps=True, vad_filter=_has_vad())
    out = []
    for seg in segs:
        for w in seg.words or []:
            t = (w.word or "").strip()
            if t:
                out.append((float(w.start), float(w.end), t))
    return out


def map_script(script, recognised, duration):
    """Spread script words over the recognised timeline by cumulative character share."""
    words = script.split()
    if not words:
        return []
    if not recognised:
        return None
    # timeline: cumulative recognised characters -> time
    pts, acc = [(0, recognised[0][0])], 0
    for s, e, t in recognised:
        acc += len(t)
        pts.append((acc, e))
    total_rec = acc or 1
    total_scr = sum(len(w) for w in words) or 1

    def at(frac):
        target = frac * total_rec
        for (c0, t0), (c1, t1) in zip(pts, pts[1:]):
            if target <= c1:
                return t0 if c1 == c0 else t0 + (t1 - t0) * (target - c0) / (c1 - c0)
        return pts[-1][1]

    out, done = [], 0
    for w in words:
        a = at(done / total_scr)
        done += len(w)
        b = at(done / total_scr)
        out.append((round(a, 3), round(max(b, a + 0.05), 3), w))
    if duration and out[-1][1] > duration:
        out[-1] = (out[-1][0], duration, out[-1][2])
    return out


def words_for(script, audio_path, duration):
    """Returns word timings or None (caller falls back to proportional)."""
    mode = config.load_settings()["production"].get("align_subtitles", "auto")
    if mode == "off" or not available():
        return None
    try:
        return map_script(script, recognise(audio_path), duration)
    except Exception:
        return None
