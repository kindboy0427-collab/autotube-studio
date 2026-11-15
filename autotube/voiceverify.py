# -*- coding: utf-8 -*-
"""Does the narration really say what the script says?

Each generated scene is transcribed by Gemini's FREE tier (audio in, text out, no cost) and compared with the text
that was sent to the voice. A scene that comes out garbled ("곳이" -> "고디", dropped words, wrong names) is
regenerated with another seed. Fails open: no free key / quota -> the scene is accepted unchecked.
"""
from __future__ import annotations
import base64
import collections
import difflib
import re
import time

from . import config, http
from .spoken import to_spoken_ko

MODEL = "gemini-3.1-flash-lite"
URL = "https://generativelanguage.googleapis.com/v1beta/models/" + MODEL + ":generateContent"
PROMPT = ("Transcribe this Korean narration exactly as spoken, word for word. Write numbers as digits. "
          "Output only the transcript, no commentary, no timestamps.")


def _norm(s):
    return re.sub(r"[^0-9A-Za-z가-힣]", "", s or "").lower()


def transcribe(mp3_path):
    key = config.get_secret("GEMINI_FREE_API_KEY")
    if not key:
        return None
    data = base64.b64encode(open(mp3_path, "rb").read()).decode()
    body = {"contents": [{"parts": [{"text": PROMPT}, {"inline_data": {"mime_type": "audio/mpeg", "data": data}}]}],
            "generationConfig": {"temperature": 0}}
    for attempt in range(3):
        try:
            r = http.post_json(URL, body, headers={"x-goog-api-key": key}, timeout=120, retries=0)
            parts = (r.get("candidates") or [{}])[0].get("content", {}).get("parts", [])
            return "".join(p.get("text", "") for p in parts).strip() or None
        except Exception as ex:
            if "429" in str(ex) or "503" in str(ex):
                time.sleep(20 * (attempt + 1))
                continue
            return None
    return None


def score(narration, mp3_path):
    """0..1 similarity between the intended spoken text and what the audio actually says; None = could not check."""
    heard = transcribe(mp3_path)
    if not heard:
        return None, ""
    # Only the Korean words are compared (English names come back from the transcriber in Hangul, which would
    # look like errors): share of the intended Korean syllable pairs that were actually heard.
    want = re.sub(r"[^가-힣]", "", to_spoken_ko(narration))
    got = re.sub(r"[^가-힣]", "", to_spoken_ko(heard))
    if len(want) < 4:
        return None, heard
    pairs = [want[k:k + 2] for k in range(len(want) - 1)]
    pool = collections.Counter(got[k:k + 2] for k in range(len(got) - 1))
    hit = 0
    for pr in pairs:
        if pool[pr] > 0:
            pool[pr] -= 1
            hit += 1
    return hit / len(pairs), heard
