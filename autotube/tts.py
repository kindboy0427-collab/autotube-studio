# -*- coding: utf-8 -*-
"""Narration engines.

  gemini     : Gemini 3.8 Flash TTS - main narrator (cheap, directable per scene, replicated 'my voice')
  elevenlabs : ElevenLabs - cloned 'my voice' / expressive acting
  openai     : OpenAI TTS (backup)
  edge       : Microsoft Edge read-aloud voice (free, unofficial; tests / last resort only)

Each call returns (audio_path, words) where words = [(start_s, end_s, text), ...]
for subtitle timing. Engines without timing data get aligned/proportional timings later.
A 'voice' is a dict {"engine": ..., "ref": voice id or prebuilt name}.
"""
from __future__ import annotations
import asyncio
import base64
import os
import re
import subprocess
import tempfile
import wave

from . import config, http, budget

GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta"
GEMINI_PREBUILT = ["Kore", "Charon", "Puck", "Aoede", "Fenrir", "Leda", "Orus", "Zephyr"]

# scene emotion -> delivery direction for Gemini TTS (speech_metadata.style)
EMOTION_STYLE = {
    "curiosity": "curious and intriguing, slightly lowered voice as if sharing a secret",
    "tension": "tense and restrained, slower pace, weight on key words",
    "surprise": "genuinely surprised, a short pause before the key fact",
    "relief": "relieved and warm, relaxed pace",
    "inspiration": "awe-struck and uplifting, warm and expansive",
    "fear": "quiet and ominous, measured pace",
    "humor": "lightly playful, a smile in the voice",
    "nostalgia": "soft and reflective",
}


class TTSError(RuntimeError):
    pass


ALL = ("gemini", "elevenlabs", "openai", "edge")


def available():
    return {"gemini": bool(config.get_secret("GEMINI_API_KEY")),
            "elevenlabs": bool(config.get_secret("ELEVENLABS_API_KEY")),
            "openai": bool(config.get_secret("OPENAI_API_KEY")),
            "edge": True}


def choose():
    pref = config.load_settings()["providers"]["tts"]
    avail = available()
    if pref != "auto" and avail.get(pref):
        return [pref] + [p for p in ALL if p != pref and avail[p]]
    return [p for p in ALL if avail[p]]


def style_for(emotion="", tone=""):
    parts = []
    if tone:
        parts.append("Korean documentary narrator. Channel tone: " + tone)
    if emotion and emotion in EMOTION_STYLE:
        parts.append(EMOTION_STYLE[emotion])
    return "; ".join(parts)


def _to_output(src, out_path):
    """Normalise any audio the engine returned into out_path (mp3) with ffmpeg."""
    cmd = [config.ffmpeg_bin(), "-y", "-loglevel", "error", "-i", src, "-ac", "1", "-ar", "44100", "-b:a", "160k", out_path]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise TTSError("오디오 변환 실패: " + r.stderr[-300:])


def _audio_seconds(path):
    try:
        from . import media
        return float(media.duration(path))
    except Exception:
        return 0.0


def _gemini_audio_item(res):
    for step in res.get("steps", []) or []:
        if step.get("type") in ("model_output", "output"):
            for c in step.get("content", []) or []:
                if c.get("type") == "audio" and c.get("data"):
                    return c
    for o in res.get("outputs", []) or []:
        if o.get("type") == "audio" and o.get("data"):
            return o
    for cand in res.get("candidates", []) or []:  # generateContent-style response
        for p in (cand.get("content") or {}).get("parts", []) or []:
            inline = p.get("inlineData") or p.get("inline_data")
            if inline and inline.get("data"):
                return {"data": inline["data"], "mime_type": inline.get("mimeType") or inline.get("mime_type")}
    return None


def split_sentences(text, min_chars=12):
    """Sentence chunks for custom voices: Gemini 3.8 can switch to another timbre at a sentence
    boundary, so each sentence is generated on its own (every chunk is a 'first sentence')."""
    parts = [p.strip() for p in re.split(r"(?<=[.?!。？！…])\s+", text.strip()) if p.strip()]
    out = []
    for p in parts:
        if out and (len(out[-1]) < min_chars or len(p) < min_chars // 2):
            out[-1] += " " + p
        else:
            out.append(p)
    return out or [text.strip()]


def is_custom_voice(voice):
    return str(voice or "").startswith(("voice_", "voicekey_"))


def _gemini_once(text, voice, style, key, model):
    item = {"type": "text", "text": text}
    if style:
        item["annotations"] = [{"type": "speech_metadata", "style": style}]
    body = {"model": model,
            "input": [{"type": "user_input", "content": [item]}],
            "response_format": {"type": "audio"},
            "generation_config": {"speech_config": [{"voice": voice}]}}
    res = http.post_json(GEMINI_BASE + "/interactions", body, headers={"x-goog-api-key": key}, timeout=300)
    a = _gemini_audio_item(res)
    if not a:
        raise TTSError("Gemini 음성 데이터 없음")
    return base64.b64decode(a["data"]), str(a.get("mime_type") or a.get("mimeType") or "").lower()


def _write_piece(raw, mime, path):
    if raw[:4] == b"RIFF" or "wav" in mime or "mpeg" in mime or "mp3" in mime or raw[:3] == b"ID3":
        with open(path, "wb") as f:
            f.write(raw)
    else:  # raw 16-bit PCM (audio/L16;rate=24000)
        m = re.search(r"rate=(\d+)", mime)
        with wave.open(path, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(int(m.group(1)) if m else 24000)
            w.writeframes(raw)


def _join(pieces, out_path, gap=0.18):
    """Concatenate audio pieces with a short natural pause between sentences."""
    if len(pieces) == 1:
        return _to_output(pieces[0], out_path)
    cmd = [config.ffmpeg_bin(), "-y", "-loglevel", "error"]
    for p in pieces:
        cmd += ["-i", p]
    chains, labels = [], []
    for i in range(len(pieces)):
        pad = f",apad=pad_dur={gap}" if i < len(pieces) - 1 else ""
        chains.append(f"[{i}:a]aresample=44100,aformat=channel_layouts=mono{pad}[a{i}]")
        labels.append(f"[a{i}]")
    graph = ";".join(chains) + ";" + "".join(labels) + f"concat=n={len(pieces)}:v=0:a=1[out]"
    cmd += ["-filter_complex", graph, "-map", "[out]", "-b:a", "160k", out_path]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise TTSError("오디오 연결 실패: " + r.stderr[-300:])


def gemini(text, out_path, production_id=None, voice_id=None, style=""):
    s = config.load_settings()
    pv = s["providers"]
    key = config.get_secret("GEMINI_API_KEY")
    if not key:
        raise TTSError("GEMINI_API_KEY 없음")
    voice = voice_id or pv["gemini_tts_voice"]
    est_min = len(text) / s["production"]["chars_per_second"] / 60
    budget.check(est_min * s["prices"]["gemini_tts_per_minute"], production_id)
    custom = is_custom_voice(voice)
    if custom:
        # a persona/tone description can pull the model away from the replicated timbre:
        # keep only the short delivery cue, and generate sentence by sentence
        style = style.split("; ")[-1] if style and "Channel tone" in style else style
        if s["production"].get("custom_voice_style", "light") == "off":
            style = ""
        chunks = split_sentences(text) if s["production"].get("custom_voice_per_sentence", True) else [text]
    else:
        chunks = [text]
    tmpdir = tempfile.mkdtemp(prefix="tts_")
    pieces = []
    try:
        for i, chunk in enumerate(chunks):
            raw, mime = _gemini_once(chunk, voice, style, key, pv["gemini_tts_model"])
            p = os.path.join(tmpdir, f"p{i:03d}.wav")
            _write_piece(raw, mime, p)
            pieces.append(p)
        _join(pieces, out_path)
    finally:
        for p in pieces:
            try:
                os.remove(p)
            except OSError:
                pass
        try:
            os.rmdir(tmpdir)
        except OSError:
            pass
    minutes = (_audio_seconds(out_path) or est_min * 60) / 60
    budget.record("gemini_tts", round(minutes, 3), minutes * s["prices"]["gemini_tts_per_minute"],
                  pv["gemini_tts_model"] + ":" + voice + (f" x{len(chunks)}" if len(chunks) > 1 else ""), production_id)
    return out_path, []


def _words_from_chars(chars, starts, ends):
    words, cur, ws, we = [], "", None, None
    for ch, a, b in zip(chars, starts, ends):
        if ch.isspace():
            if cur:
                words.append((ws, we, cur))
            cur, ws = "", None
            continue
        if ws is None:
            ws = a
        cur += ch
        we = b
    if cur:
        words.append((ws, we, cur))
    return words


def elevenlabs(text, out_path, production_id=None, voice_id=None, style=""):
    s = config.load_settings()
    pv = dict(s["providers"])
    if voice_id:
        pv["elevenlabs_voice_id"] = voice_id
    cost = len(text) / 1000 * s["prices"]["elevenlabs_per_kchar"]
    budget.check(cost, production_id)
    res = http.post_json(f"https://api.elevenlabs.io/v1/text-to-speech/{pv['elevenlabs_voice_id']}/with-timestamps",
                         {"text": text, "model_id": pv["elevenlabs_model"],
                          "voice_settings": {"stability": 0.45, "similarity_boost": 0.8, "style": 0.2}},
                         headers={"xi-api-key": config.get_secret("ELEVENLABS_API_KEY")},
                         params={"output_format": "mp3_44100_128"}, timeout=300)
    audio = res.get("audio_base64")
    if not audio:
        raise TTSError("ElevenLabs 음성 데이터 없음")
    with open(out_path, "wb") as f:
        f.write(base64.b64decode(audio))
    al = res.get("alignment") or res.get("normalized_alignment") or {}
    words = _words_from_chars(al.get("characters", []), al.get("character_start_times_seconds", []),
                              al.get("character_end_times_seconds", []))
    budget.record("elevenlabs_tts", len(text), cost, pv["elevenlabs_model"], production_id)
    return out_path, words


def openai_tts(text, out_path, production_id=None, voice_id=None, style=""):
    s = config.load_settings()
    pv = s["providers"]
    cost = len(text) / 1000 * s["prices"]["openai_tts_per_kchar"]
    budget.check(cost, production_id)
    content = http.request("POST", "https://api.openai.com/v1/audio/speech",
                           body={"model": pv["openai_tts_model"], "input": text, "voice": pv["openai_tts_voice"],
                                 "instructions": "한국어 다큐멘터리 내레이터처럼 또렷하고 자연스럽게, 문장 끝을 흐리지 말 것",
                                 "response_format": "mp3"},
                           headers={"Authorization": "Bearer " + config.get_secret("OPENAI_API_KEY")},
                           timeout=300, raw=True)
    with open(out_path, "wb") as f:
        f.write(content)
    budget.record("openai_tts", len(text), cost, pv["openai_tts_model"], production_id)
    return out_path, []


def edge(text, out_path, production_id=None, voice_id=None, style=""):
    try:
        import edge_tts
    except ImportError as ex:
        raise TTSError("edge-tts 미설치 (pip install edge-tts)") from ex
    voice = config.load_settings()["providers"]["edge_voice"]

    async def run():
        try:
            com = edge_tts.Communicate(text, voice, boundary="WordBoundary")
        except TypeError:
            com = edge_tts.Communicate(text, voice)
        words = []
        with open(out_path, "wb") as f:
            async for chunk in com.stream():
                if chunk["type"] == "audio":
                    f.write(chunk["data"])
                elif chunk["type"] == "WordBoundary":
                    start = chunk["offset"] / 1e7
                    words.append((start, start + chunk["duration"] / 1e7, chunk["text"]))
        return words

    words = asyncio.run(run())
    budget.record("edge_tts", len(text), 0.0, voice, production_id)
    return out_path, words


PROVIDERS = {"gemini": gemini, "elevenlabs": elevenlabs, "openai": openai_tts, "edge": edge}


def parse_voice(value):
    """Channel voice setting -> {"engine","ref"} or None. Accepts a dict, JSON text,
    'gemini:voice_x' / 'elevenlabs:abc', or a bare ElevenLabs id (v1.2 channels)."""
    import json
    if not value:
        return None
    if isinstance(value, dict):
        return value if value.get("engine") in PROVIDERS else None
    v = str(value).strip()
    if v.startswith("{"):
        try:
            return parse_voice(json.loads(v))
        except ValueError:
            return None
    if ":" in v and v.split(":", 1)[0] in PROVIDERS:
        e, r = v.split(":", 1)
        return {"engine": e, "ref": r.strip()}
    return {"engine": "elevenlabs", "ref": v}


def synthesize(text, out_path, production_id=None, order=None, voice_id=None, voice=None, style=""):
    """voice: {"engine","ref"}. With production.voice_fallback='same_voice' a chosen voice never
    silently switches to another engine/voice mid-video (the step fails and '다시 시도' resumes)."""
    voice = parse_voice(voice) if voice else (parse_voice(voice_id) if voice_id else None)
    if voice and not order:
        if config.load_settings()["production"].get("voice_fallback", "same_voice") == "same_voice":
            order = [voice["engine"]]
        else:
            order = [voice["engine"]] + [p for p in choose() if p != voice["engine"]]
    errors = []
    for name in order or choose():
        try:
            kw = {"style": style}
            if voice and name == voice["engine"] and voice.get("ref"):
                kw["voice_id"] = voice["ref"]
            path, words = PROVIDERS[name](text, out_path, production_id, **kw)
            return path, words, name
        except budget.BudgetExceeded:
            raise
        except Exception as ex:  # try next provider
            errors.append(f"{name}: {str(ex)[:120]}")
    raise TTSError("모든 음성 엔진 실패 → " + " | ".join(errors))
