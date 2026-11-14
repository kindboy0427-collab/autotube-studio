# -*- coding: utf-8 -*-
"""Does this footage actually show what the narration is talking about?

Two frames of a candidate clip (or the photo) are shown to Gemini (paid key, about $0.0005 per
check) with the scene's narration and intended visual. Footage that does not fit - a lecturer at a
blackboard for a factory scene, an unrelated landscape, a text slide - is rejected and the next
candidate is tried. Fails open (accepts) when the check itself cannot run, so production never stalls.
"""
from __future__ import annotations
import base64
import subprocess

from . import budget, config, db, gemini, http

PROMPT = """You are the picture editor of a Korean science documentary.
Narration of this scene (Korean): {narr}
What the scene should show: {want}
{person}
Look at the frame(s) from a candidate clip and score 0-10 as documentary B-roll for THIS narration:
9-10 shows exactly what is said; 6-8 shows the same main subject/object/place (e.g. a silicon wafer while the narration
explains how wafers are made) even if not the exact action; 4-5 same field but a different subject (generic lab, generic chip);
0-3 unrelated (another industry, medicine, crypto, abstract orbs), a lecturer/blackboard/slides/talking head when the
scene is not about that person, mostly text/logos/black frames, or very low quality.
Answer ONLY JSON: {{"score": <0-10>, "shows": "<what is visible, short>"}}"""


def _frames(path, kind):
    ff = config.ffmpeg_bin()
    outs = []
    if kind == "image":
        r = subprocess.run([ff, "-v", "error", "-i", str(path), "-vf", "scale=512:-2", "-f", "image2", "-vcodec", "mjpeg", "-"],
                           capture_output=True)
        return [r.stdout] if r.stdout else []
    from . import media
    d = media.duration(path)
    for t in (d * 0.3, d * 0.75):
        r = subprocess.run([ff, "-v", "error", "-ss", f"{t:.2f}", "-i", str(path), "-frames:v", "1", "-vf", "scale=512:-2",
                            "-f", "image2", "-vcodec", "mjpeg", "-"], capture_output=True)
        if r.stdout:
            outs.append(r.stdout)
    return outs


def score(path, narration, want, kind="video", person="", production_id=None):
    """0-10, or None when the check could not run."""
    s = config.load_settings()
    if not s["production"].get("footage_fit_check", True) or not config.get_secret("GEMINI_API_KEY"):
        return None
    try:
        imgs = _frames(path, kind)
        if not imgs:
            return 0
        parts = [{"text": PROMPT.format(narr=narration[:400], want=(want or "")[:300],
                                        person=f"The scene is about this person: {person}" if person else "")}]
        parts += [{"inline_data": {"mime_type": "image/jpeg", "data": base64.b64encode(b).decode()}} for b in imgs]
        model = s["providers"].get("gemini_check_model") or s["providers"]["gemini_text_model"]
        res = http.post_json(f"{gemini.BASE}/models/{model}:generateContent",
                             {"contents": [{"parts": parts}],
                              "generationConfig": {"responseMimeType": "application/json", "temperature": 0}},
                             headers=gemini._headers(), timeout=60, retries=1)
        txt = "".join(p.get("text", "") for c in res.get("candidates", []) for p in (c.get("content") or {}).get("parts", [])
                      if not p.get("thought"))
        data = http.parse_json_loose(txt)
        budget.record("gemini_footcheck", 1, float(s["prices"].get("gemini_footcheck_each", 0.0005)), str(path)[-60:],
                      production_id)
        return float(data.get("score", 0))
    except budget.BudgetExceeded:
        return None
    except Exception as ex:
        db.event("warn", "footcheck", f"화면 검수 실패(통과 처리): {str(ex)[:200]}")
        return None


def fits(path, narration, want, kind="video", person="", production_id=None):
    sc = score(path, narration, want, kind, person, production_id)
    return sc is None or sc >= float(config.load_settings()["production"].get("footage_min_score", 6))
