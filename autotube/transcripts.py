# -*- coding: utf-8 -*-
"""Low-cost 'watching' through captions via Apify (about $0.003 per video).

Used when Gemini's free daily YouTube allowance is used up (watch_mode=auto) or
always (watch_mode=transcript). Captions carry the script, hook and structure but
not the picture, so the analysis is told to leave visual items empty.
"""
from __future__ import annotations
from . import budget, config, http

ACTOR = "johnvc~YoutubeTranscripts"  # charges only for successfully transcribed videos


class TranscriptError(RuntimeError):
    pass


def fetch(url: str) -> str:
    token = config.get_secret("APIFY_TOKEN")
    if not token:
        raise TranscriptError("APIFY_TOKEN 없음")
    price = config.load_settings()["prices"]["apify_transcript_each"]
    budget.check(price)
    items = http.post_json(f"https://api.apify.com/v2/acts/{ACTOR}/run-sync-get-dataset-items",
                           {"youtube_url": url, "languages": ["ko", "en"], "output_formats": ["text"],
                            "include_metadata": False, "include_extended_metadata": False},
                           headers={"Authorization": "Bearer " + token},
                           params={"timeout": 120, "memory": 256, "maxTotalChargeUsd": 0.01}, timeout=180, retries=1)
    item = items[0] if isinstance(items, list) and items else {}
    text = item.get("text") or item.get("non_timestamped") or ""
    if not text.strip():
        raise TranscriptError("자막 없음")
    budget.record("apify_transcript", 1, price, url)
    return text[:60000]


def watch_via_transcript(url, prompt, duration_seconds=0, fetcher=None, generate=None):
    """Same output format as gemini.watch_youtube, built from captions + a text model."""
    from . import llm
    fetcher = fetcher or fetch
    generate = generate or (lambda p: llm.generate(p, "유튜브 성과 분석가", json_mode=True, max_tokens=6000))
    text = fetcher(url)
    note = ("\n\n[중요] 영상 화면은 볼 수 없고 아래 자막(대본)만 있다. visual_style은 빈 배열, "
            "pacing.avg_shot_seconds는 null로 두고, 화면에 대한 추측은 하지 마라.\n[자막]\n")
    return generate(prompt.replace("이 영상을 처음부터 끝까지 화면과 소리를 모두 보고 듣고 분석하라.",
                                   "이 영상의 자막을 처음부터 끝까지 읽고 분석하라.") + note + text)
