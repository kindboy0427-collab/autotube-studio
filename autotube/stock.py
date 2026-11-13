# -*- coding: utf-8 -*-
"""Free stock footage/photos from Pexels (license allows free commercial use; attribution appreciated)."""
from __future__ import annotations
from . import config, http


class StockError(RuntimeError):
    pass


def _headers():
    k = config.get_secret("PEXELS_API_KEY")
    if not k:
        raise StockError("PEXELS_API_KEY 없음")
    return {"Authorization": k}


def find_video(query: str, min_seconds: float, used: set, out_path: str):
    res = http.get_json("https://api.pexels.com/videos/search", headers=_headers(),
                        params={"query": query, "orientation": "landscape", "size": "medium", "per_page": 15})
    best = None
    for v in res.get("videos", []):
        if v.get("id") in used:
            continue
        files = [f for f in v.get("video_files", []) if f.get("file_type") == "video/mp4" and (f.get("width") or 0) >= 1280]
        if not files:
            continue
        f = min(files, key=lambda x: abs((x.get("width") or 0) - 1920))
        score = (v.get("duration", 0) >= min_seconds, -abs((f.get("width") or 0) - 1920))
        if best is None or score > best[0]:
            best = (score, v, f)
    if not best:
        raise StockError(f"스톡 영상 없음: {query}")
    _, v, f = best
    http.download(f["link"], out_path, timeout=300)
    used.add(v["id"])
    return out_path, {"source": "pexels", "id": v["id"], "url": v.get("url"), "author": (v.get("user") or {}).get("name")}


def find_photo(query: str, used: set, out_path: str):
    res = http.get_json("https://api.pexels.com/v1/search", headers=_headers(),
                        params={"query": query, "orientation": "landscape", "per_page": 15})
    for p in res.get("photos", []):
        if p.get("id") in used:
            continue
        http.download(p["src"].get("large2x") or p["src"]["original"], out_path, timeout=120)
        used.add(p["id"])
        return out_path, {"source": "pexels", "id": p["id"], "url": p.get("url"), "author": p.get("photographer")}
    raise StockError(f"스톡 사진 없음: {query}")
