# -*- coding: utf-8 -*-
"""More stock video sources.

Pixabay  - free, commercial use allowed; searches cached 24 h (API rule); credit shown in the description.
Vecteezy - official API. Free plan: 500 downloads/month (attribution required),
           Pay-As-You-Go: $1 per download. Thumbnails are checked against the narration BEFORE
           downloading, so no download is spent on footage that doesn't fit.
"""
from __future__ import annotations
import time

from . import budget, config, db, http


class Stock2Error(RuntimeError):
    pass


# ---------------- Pixabay ----------------
def pixabay_video(query, min_seconds, used, out_path, **_):
    key = config.get_secret("PIXABAY_API_KEY")
    if not key:
        raise Stock2Error("PIXABAY_API_KEY 없음")
    ck = "pixabay:" + query.strip().lower()[:100]
    hit = db.kv_get(ck)
    if not hit or time.time() - hit.get("t", 0) > 86400:
        res = http.get_json("https://pixabay.com/api/videos/", params={
            "key": key, "q": query[:100], "video_type": "all", "per_page": 30, "safesearch": "true"})
        hit = {"t": time.time(), "hits": res.get("hits", [])}
        db.kv_set(ck, hit)
    for v in hit["hits"]:
        vid = "pixabay:%s" % v.get("id")
        if vid in used:
            continue
        vs = v.get("videos") or {}
        f = next((vs[k] for k in ("medium", "large", "small") if (vs.get(k) or {}).get("url")
                  and (vs[k].get("width") or 0) >= 1280), None)
        if not f:
            continue
        used.add(vid)
        http.download(f["url"], out_path, timeout=300)
        return out_path, {"source": "Pixabay", "author": v.get("user"), "license": "Pixabay Content License",
                          "page": v.get("pageURL"), "title": v.get("tags")}
    raise Stock2Error(f"Pixabay 영상 없음: {query}")


# ---------------- Vecteezy ----------------
VBASE = "https://api.vecteezy.com/v2"


def _vz():
    key, acc = config.get_secret("VECTEEZY_API_KEY"), config.get_secret("VECTEEZY_ACCOUNT_ID")
    if not (key and acc):
        raise Stock2Error("VECTEEZY_API_KEY / VECTEEZY_ACCOUNT_ID 없음")
    return {"Authorization": "Bearer " + key}, acc


def _vz_search(query, hdr, acc):
    ck = "vecteezy:" + query.strip().lower()[:100]
    hit = db.kv_get(ck)
    if hit and time.time() - hit.get("t", 0) < 86400:
        return hit["r"]
    # no ai_generated filter: Vecteezy marks some real footage as AI and the filter drops the best matches
    params = {"term": query[:100], "content_type": "video", "per_page": 30, "license_type": "commercial",
              "sort_by": "relevance"}
    res = None
    for path in (f"{VBASE}/{acc}/resources", f"{VBASE}/account/{acc}/resources"):
        try:
            res = http.get_json(path, headers=hdr, params=params)
            break
        except http.HttpError as ex:
            if ex.status != 404:
                raise
    items = (res or {}).get("resources") or (res or {}).get("data") or []
    db.kv_set(ck, {"t": time.time(), "r": items})
    return items


def vecteezy_video(query, min_seconds, used, out_path, narration="", want="", production_id=None, **_):
    hdr, acc = _vz()
    s = config.load_settings()
    month = "vecteezy_dl:" + time.strftime("%Y-%m")
    cap = int(s["production"].get("vecteezy_downloads_per_month", 490))
    if int(db.kv_get(month, 0) or 0) >= cap:
        raise Stock2Error(f"Vecteezy 이번 달 다운로드 상한({cap}) 도달")
    from . import footcheck
    budget_checks = int(s["production"].get("vecteezy_checks_per_call", 5))  # keep a scene from checking 30 previews
    for r in _vz_search(query, hdr, acc):
        vid = "vecteezy:%s" % r.get("id")
        if vid in used:
            continue
        used.add(vid)
        if budget_checks <= 0:
            break
        budget_checks -= 1
        title = (r.get("title") or "").lower()
        if "vertical" in title:
            continue  # 9:16 clips look bad in a 16:9 documentary
        checked = False
        if narration:  # judge the free watermarked preview first: downloads are the limited/paid part
            prev = r.get("preview_url") or ""
            thumb = r.get("thumbnail_2x_url") or r.get("thumbnail_url") or ""
            src, kind = (prev, "video") if prev.lower().split("?")[0].endswith(".mp4") else (thumb, "image")
            if src:
                tpath = str(out_path) + (".prev.mp4" if kind == "video" else ".thumb.jpg")
                try:
                    http.download(src, tpath, timeout=60)
                    sc = footcheck.score(tpath, narration, want, kind=kind, production_id=production_id)
                except Exception:
                    sc = None
                if sc is not None:
                    if sc < float(s["production"].get("footage_min_score", 6)):
                        continue
                    checked = True
        try:
            types = [t.get("extension") for t in ((r.get("file_metadata") or {}).get("available_file_types") or [])]
            ftype = "mp4" if "mp4" in types else (types[0] if types else "mp4")  # most Vecteezy videos are .mov
            sizes = [z.get("id") for z in ((r.get("file_metadata") or {}).get("available_download_sizes") or [])]
            params = {"file_type": ftype}
            if s["production"].get("vecteezy_plan", "free") != "free" and "medium" in sizes:
                params["file_size"] = "medium"  # file_size is for paid plans only
            d = http.get_json(f"{VBASE}/{acc}/resources/{r['id']}/download", headers=hdr, params=params, retries=0)
        except http.HttpError as ex:
            if ex.status == 402:
                raise Stock2Error("Vecteezy 다운로드 한도/결제 필요") from ex
            continue
        url = d.get("url")
        if not url:
            continue
        http.download(url, out_path, timeout=600)
        db.kv_set(month, int(db.kv_get(month, 0) or 0) + 1)
        price = 0.0 if s["production"].get("vecteezy_plan", "free") == "free" else 1.0
        budget.record("vecteezy_download", 1, price, str(r.get("id")), production_id)
        page = (d.get("required_attribution_url") or f"https://www.vecteezy.com/video/{r['id']}").replace(
            "://api.vecteezy.com", "://www.vecteezy.com")
        return out_path, {"source": "Vecteezy", "author": "Vecteezy", "license": "Vecteezy License",
                          "page": page, "title": r.get("title"), "_prechecked": checked}
    raise Stock2Error(f"Vecteezy 영상 없음: {query}")
