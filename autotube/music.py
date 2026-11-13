# -*- coding: utf-8 -*-
"""Background music: ElevenLabs Music (paid plans) > your own files in assets/music."""
from __future__ import annotations
import random

from . import config, http, budget


def from_library(mood: str = ""):
    folder = config.ASSETS / "music"
    files = [p for p in folder.glob("*") if p.suffix.lower() in (".mp3", ".wav", ".m4a", ".ogg")]
    if not files:
        return None
    tagged = [p for p in files if mood and mood.lower() in p.stem.lower()]
    return str(random.choice(tagged or files))


def elevenlabs_music(prompt: str, seconds: float, out_path: str, production_id=None):
    s = config.load_settings()
    minutes = max(0.5, seconds / 60)
    cost = minutes * s["prices"]["elevenlabs_music_per_minute"]
    budget.check(cost, production_id)
    ms = int(min(seconds, 300) * 1000)  # one track up to 5 min; looped by the mixer
    content = http.request("POST", "https://api.elevenlabs.io/v1/music",
                           body={"prompt": prompt + ", instrumental only, no vocals, background for narration",
                                 "music_length_ms": ms},
                           headers={"xi-api-key": config.get_secret("ELEVENLABS_API_KEY")}, timeout=600, raw=True)
    with open(out_path, "wb") as f:
        f.write(content)
    budget.record("elevenlabs_music", minutes, cost, prompt[:60], production_id)
    return out_path


def get_track(mood: str, seconds: float, out_path: str, production_id=None):
    if config.get_secret("ELEVENLABS_API_KEY"):
        try:
            return elevenlabs_music(mood or "calm cinematic documentary", seconds, out_path, production_id), "elevenlabs"
        except budget.BudgetExceeded:
            raise
        except Exception:
            pass
    lib = from_library(mood)
    return (lib, "library") if lib else (None, "none")
