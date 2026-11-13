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
    try:
        import faster_whisper  # noqa: F401
        return True
    except Exception:
        return False


def recognise(audio_path):
    segs, _ = _model().transcribe(audio_path, language="ko", word_timestamps=True, vad_filter=True)
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
