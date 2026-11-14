# -*- coding: utf-8 -*-
"""Google Gemini API: text, YouTube watching, image (Nano Banana) and video (Omni Flash).

Endpoints follow the public Gemini API docs (v1beta). Model IDs are settings, so a
renamed model only needs a settings change.
"""
from __future__ import annotations
import base64
import re
import time

from . import config, http, budget

BASE = "https://generativelanguage.googleapis.com/v1beta"


class GeminiError(RuntimeError):
    pass


def _key():
    k = config.get_secret("GEMINI_API_KEY")
    if not k:
        raise GeminiError("GEMINI_API_KEY가 설정되지 않았습니다")
    return k


def _headers():
    """Paid key. Blocked unless the owner allowed paid Gemini (settings providers.gemini_paid_allowed)."""
    if not config.load_settings()["providers"].get("gemini_paid_allowed", False):
        raise GeminiError("유료 Gemini 사용이 꺼져 있습니다 (추가 결제 방지). 설정에서 허용해야 사용됩니다")
    return {"x-goog-api-key": _key()}


def _learning_headers():
    """Watching/analysis uses the free-tier key when one is set (billing stays for TTS, images, video)."""
    free = config.get_secret("GEMINI_FREE_API_KEY")
    return {"x-goog-api-key": free} if free else _headers()


def free_key_set():
    return bool(config.get_secret("GEMINI_FREE_API_KEY"))


def _texts_from_interaction(res: dict) -> str:
    out = []
    for step in res.get("steps", []) or []:
        if step.get("type") in ("model_output", "output"):
            for c in step.get("content", []) or []:
                if c.get("type") == "text" and c.get("text"):
                    out.append(c["text"])
    if not out:  # other response shapes (outputs / generateContent style)
        for o in res.get("outputs", []) or []:
            if o.get("type") == "text" and o.get("text"):
                out.append(o["text"])
        for cand in res.get("candidates", []) or []:
            for p in (cand.get("content") or {}).get("parts", []) or []:
                if p.get("text") and not p.get("thought"):
                    out.append(p["text"])
    return "".join(out)


def _usage_tokens(res, fallback_in, fallback_out):
    u = res.get("usage") or res.get("usageMetadata") or {}
    tin = u.get("total_input_tokens") or u.get("input_tokens") or u.get("promptTokenCount") or fallback_in
    tout = u.get("total_output_tokens") or u.get("output_tokens") or u.get("candidatesTokenCount") or fallback_out
    return int(tin or 0), int(tout or 0)


def generate_text(prompt: str, system: str = "", model: str | None = None, production_id=None, service="gemini_text") -> str:
    s = config.load_settings()
    model = model or s["providers"]["gemini_text_model"]
    body = {"model": model, "input": prompt}
    if system:
        body["system_instruction"] = system
    learning = production_id is None and free_key_set()  # learning/radar work -> free key, production -> paid key
    budget.check(0.0 if learning else 0.05, production_id)
    res = http.post_json(BASE + "/interactions", body, headers=_learning_headers() if learning else _headers(), timeout=600)
    text = _texts_from_interaction(res)
    if not text.strip():
        raise GeminiError("빈 응답")
    p = s["prices"]
    tin, tout = _usage_tokens(res, len(prompt) // 2, len(text) // 2)
    cost = 0.0 if learning else tin / 1e6 * p["gemini_text_per_mtok_in"] + tout / 1e6 * p["gemini_text_per_mtok_out"]
    budget.record(service + ("_free" if learning else ""), tin + tout, cost, model, production_id)
    return text


def watch_youtube(url: str, prompt: str, duration_seconds: int = 0, model: str | None = None) -> str:
    """Gemini watches a PUBLIC YouTube video (picture + sound) from its URL. No download."""
    s = config.load_settings()
    model = model or s["providers"]["gemini_watch_model"]
    est_tokens = max(60, duration_seconds) * 300  # ~258 frame + 32 audio tokens per second
    cost = 0.0 if free_key_set() else est_tokens / 1e6 * s["prices"]["gemini_watch_per_mtok"]
    budget.check(cost)
    body = {"model": model, "input": [{"type": "text", "text": prompt}, {"type": "video", "uri": url}]}
    # retries=0: on the free tier every attempt (even a failed one) can use one of the ~20 daily requests
    res = http.post_json(BASE + "/interactions", body, headers=_learning_headers(), timeout=900,
                         retries=0 if free_key_set() else 1)
    text = _texts_from_interaction(res)
    if not text.strip():
        raise GeminiError("영상 분석 결과가 비어 있습니다")
    tin, tout = _usage_tokens(res, est_tokens, len(text) // 2)
    paid = 0.0 if free_key_set() else tin / 1e6 * s["prices"]["gemini_watch_per_mtok"]
    budget.record("gemini_watch_free" if free_key_set() else "gemini_watch", tin + tout, paid, url)
    return text


def generate_image(prompt: str, out_path: str, aspect="16:9", production_id=None) -> str:
    s = config.load_settings()
    model = s["providers"]["gemini_image_model"]
    budget.check(s["prices"]["gemini_image_each"], production_id)
    body = {"contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"responseModalities": ["IMAGE"], "imageConfig": {"aspectRatio": aspect}}}
    res = http.post_json(f"{BASE}/models/{model}:generateContent", body, headers=_headers(), timeout=300)
    data = None
    for cand in res.get("candidates", []) or []:
        for p in (cand.get("content") or {}).get("parts", []) or []:
            inline = p.get("inlineData") or p.get("inline_data")
            if inline and not p.get("thought"):
                data = inline.get("data")  # keep the LAST non-thought image part
    if not data:
        raise GeminiError("이미지가 생성되지 않았습니다 (안전 필터 또는 모델 응답)")
    with open(out_path, "wb") as f:
        f.write(base64.b64decode(data))
    budget.record("gemini_image", 1, s["prices"]["gemini_image_each"], model, production_id)
    return out_path


def generate_video(prompt: str, out_path: str, aspect="16:9", resolution="720p", production_id=None,
                   assumed_seconds=8, poll_seconds=5, max_wait=900) -> str:
    """Gemini Omni Flash text-to-video (3-10 s clip, with audio which we mute later)."""
    s = config.load_settings()
    model = s["providers"]["gemini_video_model"]
    cost = s["prices"]["gemini_video_per_second"] * assumed_seconds
    budget.check(cost, production_id)
    body = {"model": model, "input": prompt + " No on-screen text. No dialogue.",
            "response_format": {"type": "video", "aspect_ratio": aspect, "resolution": resolution, "delivery": "uri"},
            "background": False, "store": False, "stream": False}
    res = http.post_json(BASE + "/interactions", body, headers=_headers(), timeout=max_wait, retries=1)
    video = None
    for step in res.get("steps", []) or []:
        if step.get("type") in ("model_output", "output"):
            for c in step.get("content", []) or []:
                if c.get("type") == "video":
                    video = c
    if not video:
        raise GeminiError("영상이 생성되지 않았습니다 (안전 필터 또는 모델 응답)")
    if video.get("data"):
        with open(out_path, "wb") as f:
            f.write(base64.b64decode(video["data"]))
    elif video.get("uri"):
        m = re.search(r"files/([A-Za-z0-9_-]+)", video["uri"])
        if not m:
            http.download(video["uri"], out_path, headers=_headers())
        else:
            fid = m.group(1)
            waited = 0
            while waited < max_wait:
                info = http.get_json(f"{BASE}/files/{fid}", headers=_headers())
                state = info.get("state")
                if state == "ACTIVE":
                    break
                if state == "FAILED":
                    raise GeminiError("영상 파일 처리 실패")
                time.sleep(poll_seconds)
                waited += poll_seconds
            http.download(f"{BASE}/files/{fid}:download?alt=media", out_path, headers=_headers())
    else:
        raise GeminiError("영상 데이터가 없습니다")
    budget.record("gemini_video", assumed_seconds, cost, model, production_id)
    return out_path
