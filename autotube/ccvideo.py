# -*- coding: utf-8 -*-
"""Creative Commons (CC BY) YouTube footage: lectures, interviews, lab and factory footage whose
owners allow reuse - also commercial - with attribution. No revenue sharing; the credit goes into
the description automatically.

Only the few seconds a scene needs are downloaded (yt-dlp download ranges). Search results are
cached per query, and the number of searches per day is capped (search.list = 100 quota units).
"""
from __future__ import annotations
import json
import random
import re
from pathlib import Path

from . import budget, config, db, http, youtube_data


class CCError(RuntimeError):
    pass


def _cached_search(query, lang):
    key = f"ccsearch:{lang}:{query.strip().lower()[:120]}"
    hit = db.kv_get(key)
    if hit is not None:
        return hit
    s = config.load_settings()["production"]
    day = "ccsearch_count:" + db.kst_day()
    if int(db.kv_get(day, 0) or 0) >= int(s.get("cc_youtube_searches_per_day", 60)):
        raise CCError("오늘 CC 영상 검색 횟수 상한 도달")
    if not budget.take_quota("search"):
        raise CCError("오늘 YouTube 검색 할당량 소진")
    db.kv_set(day, int(db.kv_get(day, 0) or 0) + 1)
    res = http.get_json(youtube_data.BASE + "/search", params={
        "part": "snippet", "q": query, "type": "video", "videoLicense": "creativeCommon",
        "maxResults": 15, "relevanceLanguage": lang, "safeSearch": "strict", "videoEmbeddable": "true",
        "key": youtube_data._key()})
    ids = [it["id"]["videoId"] for it in res.get("items", []) if it.get("id", {}).get("videoId")]
    out = []
    if ids:
        det = http.get_json(youtube_data.BASE + "/videos", params={
            "part": "snippet,contentDetails,status", "id": ",".join(ids), "key": youtube_data._key()})
        for v in det.get("items", []):
            if (v.get("status") or {}).get("license") != "creativeCommon":
                continue  # double-check the license on the video itself
            dur = youtube_data.parse_duration((v.get("contentDetails") or {}).get("duration")) or 0
            if dur < 20 or dur > 3 * 3600:
                continue
            sn = v.get("snippet") or {}
            out.append({"id": v["id"], "title": sn.get("title", ""), "channel": sn.get("channelTitle", ""),
                        "duration": dur})
    db.kv_set(key, out)
    return out


def _download_range(video_id, start, end, out_path):
    try:
        from yt_dlp import YoutubeDL
        from yt_dlp.utils import download_range_func
    except ImportError as ex:
        raise CCError("yt-dlp 미설치 (pip install yt-dlp)") from ex
    out = Path(out_path)
    for f in out.parent.glob(out.stem + ".*"):
        if f != out:
            f.unlink(missing_ok=True)
    opts = {"format": "bv*[ext=mp4][height<=1080]/b[ext=mp4][height<=1080]/bv*[ext=mp4]/b[ext=mp4]",
            "outtmpl": str(out), "quiet": True, "no_warnings": True, "noplaylist": True,
            "download_ranges": download_range_func(None, [(start, end)]), "force_keyframes_at_cuts": True,
            "ffmpeg_location": str(Path(config.ffmpeg_bin()).parent), "retries": 2, "socket_timeout": 30}
    with YoutubeDL(opts) as y:
        y.download([f"https://www.youtube.com/watch?v={video_id}"])
    if not out.exists() or out.stat().st_size < 10_000:
        raise CCError("CC 영상 구간 다운로드 실패")


def find_video(query, seconds, used, out_path, person=""):
    """A CC BY YouTube segment for a scene -> (path, credit). person: look for a talk/interview of them."""
    queries = [f"{person} interview", f"{person} lecture"] if person else [query]
    for q in queries:
        for lang in ("en", "ko"):
            try:
                cands = _cached_search(q, lang)
            except CCError:
                raise
            except Exception:
                continue
            if person:
                last = person.split()[-1].lower()
                cands = [c for c in cands if last in (c["title"] + " " + c["channel"]).lower()]
            for c in cands:
                if c["id"] in used:
                    continue
                need = max(2.0, float(seconds)) + 1.0
                lo, hi = c["duration"] * 0.15, max(c["duration"] * 0.15, c["duration"] * 0.75 - need)
                start = random.uniform(lo, hi) if hi > lo else 0.0
                try:
                    _download_range(c["id"], start, start + need, out_path)
                except Exception:
                    used.add(c["id"])
                    continue
                used.add(c["id"])
                return out_path, {"source": "YouTube (CC BY)", "author": c["channel"],
                                  "license": "Creative Commons Attribution (CC BY)",
                                  "page": f"https://www.youtube.com/watch?v={c['id']}", "title": c["title"]}
    raise CCError(f"CC 영상 없음: {person or query}")
