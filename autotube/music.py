# -*- coding: utf-8 -*-
"""Background music: free tracks in assets/music (YouTube Audio Library, Kevin MacLeod CC BY), credited automatically."""
from __future__ import annotations
import random

from . import config, http, budget


def from_library(mood: str = ""):
    """Pick the track whose file name shares the most mood words with the script's music mood
    (file names like calm_ambient_wonder__Floating_Cities.mp3). YouTube Audio Library files you add
    are picked the same way - put mood words at the start of their names."""
    import re
    folder = config.ASSETS / "music"
    files = [p for p in folder.glob("*") if p.suffix.lower() in (".mp3", ".wav", ".m4a", ".ogg")]
    if not files:
        return None
    want = set(re.findall(r"[a-z]+", (mood or "").lower()))
    syn = {"documentary": "calm", "cinematic": "calm", "ambient": "ambient", "dramatic": "tense", "suspense": "tense",
           "ominous": "dark", "uplifting": "hopeful", "inspiring": "hopeful", "curious": "curious", "wonder": "wonder"}
    want |= {syn[w] for w in list(want) if w in syn}
    def score(p):
        tags = set(p.stem.split("__")[0].lower().replace("-", "_").split("_"))
        return len(want & tags)
    best = max(score(p) for p in files)
    pool = [p for p in files if score(p) == best] if best else files
    return str(random.choice(pool))


def credit_for(path):
    """Attribution line for a library track (assets/music/credits.json), or None if none is needed."""
    import json
    from pathlib import Path
    f = config.ASSETS / "music" / "credits.json"
    try:
        return json.loads(f.read_text(encoding="utf-8")).get(Path(path).name)
    except Exception:
        return None


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
    # default: free tracks from assets/music (YouTube Audio Library + CC BY). ElevenLabs Music only if chosen.
    if config.load_settings()["production"].get("music_source", "library") == "elevenlabs" and config.get_secret("ELEVENLABS_API_KEY"):
        try:
            return elevenlabs_music(mood or "calm cinematic documentary", seconds, out_path, production_id), "elevenlabs"
        except budget.BudgetExceeded:
            raise
        except Exception:
            pass
    lib = from_library(mood)
    return (lib, "library") if lib else (None, "none")
