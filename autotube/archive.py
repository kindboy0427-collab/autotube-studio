# -*- coding: utf-8 -*-
"""Real footage & photos from free, properly licensed archives (no YouTube rips, no TV clips).

  Wikimedia Commons : photos + videos of real processes, labs, places, scientists (PD / CC0 / CC BY / CC BY-SA)
  NASA Image and Video Library : space, rockets, Earth (mostly public domain)

Every result carries author + license + source page -> written into the video description.
"""
from __future__ import annotations
import html
import re

from . import http

COMMONS = "https://commons.wikimedia.org/w/api.php"
NASA = "https://images-api.nasa.gov"
OK_LICENSES = ("public domain", "pd", "cc0", "cc by", "cc-by", "cc by-sa", "cc-by-sa")
BAD = ("nc", "nd", "fair use", "non-free")


class ArchiveError(RuntimeError):
    pass


def _clean(s):
    return html.unescape(re.sub(r"<[^>]+>", "", str(s or ""))).strip()[:120]


def _license_ok(name):
    n = (name or "").lower()
    return any(k in n for k in OK_LICENSES) and not any(re.search(rf"\b{b}\b", n) for b in BAD)


def commons_search(query, kind="image", limit=12):
    """kind: image | video. Returns [{url, page, author, license, w, h, mime}] with usable licenses only."""
    ft = "filetype:video" if kind == "video" else "filetype:bitmap"
    res = http.get_json(COMMONS, params={
        "action": "query", "format": "json", "generator": "search", "gsrnamespace": 6,
        "gsrsearch": f"{query} {ft}", "gsrlimit": limit, "prop": "imageinfo",
        "iiprop": "url|size|mime|extmetadata", "iiurlwidth": 1920}, timeout=30, retries=1)
    out = []
    for p in ((res.get("query") or {}).get("pages") or {}).values():
        ii = (p.get("imageinfo") or [{}])[0]
        meta = ii.get("extmetadata") or {}
        lic = _clean((meta.get("LicenseShortName") or {}).get("value"))
        if not _license_ok(lic):
            continue
        mime = ii.get("mime", "")
        if kind == "video" and not mime.startswith("video/"):
            continue
        if kind == "image" and not mime.startswith("image/"):
            continue
        w, h = ii.get("width") or 0, ii.get("height") or 0
        if kind == "image" and (w < 800 or h < 450):
            continue
        out.append({"url": ii.get("thumburl") if kind == "image" and ii.get("thumburl") else ii.get("url"),
                    "page": ii.get("descriptionurl"), "author": _clean((meta.get("Artist") or {}).get("value")) or "Unknown",
                    "license": lic, "w": w, "h": h, "mime": mime, "title": p.get("title", ""), "source": "Wikimedia Commons"})
    # landscape first
    out.sort(key=lambda x: (x["w"] < x["h"], -x["w"]))
    return out


def nasa_search(query, kind="video", limit=8):
    res = http.get_json(NASA + "/search", params={"q": query, "media_type": kind, "page_size": limit}, timeout=30, retries=1)
    out = []
    for it in ((res.get("collection") or {}).get("items") or [])[:limit]:
        d = (it.get("data") or [{}])[0]
        nid = d.get("nasa_id")
        if not nid:
            continue
        out.append({"nasa_id": nid, "title": d.get("title", ""), "author": d.get("photographer") or d.get("center") or "NASA",
                    "license": "Public domain (NASA)", "page": f"https://images.nasa.gov/details/{nid}", "source": "NASA"})
    return out


def nasa_asset(nasa_id, kind="video"):
    res = http.get_json(f"{NASA}/asset/{nasa_id}", timeout=30, retries=1)
    hrefs = [i.get("href", "") for i in ((res.get("collection") or {}).get("items") or [])]
    if kind == "video":
        mp4 = [h for h in hrefs if h.endswith(".mp4")]
        pref = [h for h in mp4 if "~mobile" in h or "~medium" in h] or [h for h in mp4 if "~orig" not in h] or mp4
        return pref[0].replace("http://", "https://") if pref else None
    jpg = [h for h in hrefs if h.lower().endswith((".jpg", ".png"))]
    pref = [h for h in jpg if "~large" in h] or [h for h in jpg if "~orig" in h] or jpg
    return pref[0].replace("http://", "https://") if pref else None


def find_video(query, used, out_path, space=False):
    """Real footage clip for a scene -> (path, credit)."""
    cands = []
    if space:
        try:
            cands += [dict(c, _nasa=True) for c in nasa_search(query, "video")]
        except Exception:
            pass
    try:
        cands += commons_search(query, "video")
    except Exception:
        pass
    for c in cands:
        key = c.get("page")
        if key in used:
            continue
        url = nasa_asset(c["nasa_id"], "video") if c.get("_nasa") else c["url"]
        if not url:
            continue
        http.download(url, out_path, timeout=300)
        used.add(key)
        return out_path, {k: c.get(k) for k in ("source", "author", "license", "page", "title")}
    raise ArchiveError(f"실제 자료 영상 없음: {query}")


def find_photo(query, used, out_path, space=False):
    cands = []
    if space:
        try:
            cands += [dict(c, _nasa=True) for c in nasa_search(query, "image")]
        except Exception:
            pass
    try:
        cands += commons_search(query, "image")
    except Exception:
        pass
    for c in cands:
        key = c.get("page")
        if key in used:
            continue
        url = nasa_asset(c["nasa_id"], "image") if c.get("_nasa") else c["url"]
        if not url:
            continue
        http.download(url, out_path, timeout=120)
        used.add(key)
        return out_path, {k: c.get(k) for k in ("source", "author", "license", "page", "title")}
    raise ArchiveError(f"실제 자료 사진 없음: {query}")


def find_person(name, used, out_path):
    """CC/PD portrait of a real, named person (scientist, CEO) for quote scenes."""
    for q in (f"{name} portrait", name):
        try:
            for c in commons_search(q, "image", limit=10):
                if c["page"] in used:
                    continue
                http.download(c["url"], out_path, timeout=120)
                used.add(c["page"])
                return out_path, {k: c.get(k) for k in ("source", "author", "license", "page", "title")}
        except ArchiveError:
            raise
        except Exception:
            continue
    raise ArchiveError(f"인물 사진 없음: {name}")
