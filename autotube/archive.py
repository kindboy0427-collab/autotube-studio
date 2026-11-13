# -*- coding: utf-8 -*-
"""Real footage & photos from free, properly licensed archives (no YouTube rips, no TV clips).

  NASA Image and Video Library : space, rockets, Earth (US federal works -> public domain in the US)
  Wikimedia Commons : photos + videos of real processes, labs, places, scientists (PD / CC0 / CC BY / CC BY-SA),
                      incl. mirrored NOAA / USGS / ESA (CC BY-SA IGO, credit required) collections
  Internet Archive  : public-domain films (Prelinger archives, US government films) for history/industry shots

Never used: NASA/agency logos & insignia (endorsement rules), non-free licenses (NC/ND).

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


LOGO_WORDS = ("logo", "insignia", "meatball", "emblem", "seal of", "wordmark", "patch")


def _license_ok(name):
    n = (name or "").lower()
    return any(k in n for k in OK_LICENSES) and not any(re.search(rf"\b{b}\b", n) for b in BAD)


def commons_search(query, kind="image", limit=12):
    """kind: image | video. Returns [{url, page, author, license, w, h, mime}] with usable licenses only."""
    ft = "filetype:video" if kind == "video" else "filetype:bitmap"
    params = {"action": "query", "format": "json", "generator": "search", "gsrnamespace": 6,
              "gsrsearch": f"{query} {ft}", "gsrlimit": limit}
    if kind == "video":  # videoinfo gives ready-made 720p/480p transcodes instead of huge originals
        params.update({"prop": "videoinfo", "viprop": "url|size|mime|extmetadata|derivatives"})
    else:
        params.update({"prop": "imageinfo", "iiprop": "url|size|mime|extmetadata", "iiurlwidth": 1920})
    res = http.get_json(COMMONS, params=params, timeout=30, retries=1)
    out = []
    for p in ((res.get("query") or {}).get("pages") or {}).values():
        ii = (p.get("videoinfo") or p.get("imageinfo") or [{}])[0]
        meta = ii.get("extmetadata") or {}
        lic = _clean((meta.get("LicenseShortName") or {}).get("value"))
        if not _license_ok(lic):
            continue
        if any(w in (p.get("title") or "").lower() for w in LOGO_WORDS):
            continue
        mime = ii.get("mime", "")
        if kind == "video" and not mime.startswith("video/"):
            continue
        if kind == "image" and not mime.startswith("image/"):
            continue
        w, h = ii.get("width") or 0, ii.get("height") or 0
        if kind == "image" and (w < 800 or h < 450):
            continue
        url = ii.get("thumburl") if kind == "image" and ii.get("thumburl") else ii.get("url")
        if kind == "video":
            ders = [dv for dv in (ii.get("derivatives") or []) if "webm" in str(dv.get("type", "")) or str(dv.get("src", "")).endswith((".webm", ".mp4"))]
            ders = sorted(ders, key=lambda dv: abs((dv.get("height") or 0) - 720))
            if ders:
                url = ders[0]["src"]
            elif (ii.get("size") or 0) > 400 * 1024 * 1024:
                continue  # original too large to download
        out.append({"url": url,
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


IA = "https://archive.org"


def ia_search(query, limit=8):
    """Internet Archive public-domain films (Prelinger + US government)."""
    q = (f"({query}) AND mediatype:movies AND (collection:prelinger OR licenseurl:*publicdomain* "
         f"OR collection:FedFlix OR collection:nasa)")
    res = http.get_json(IA + "/advancedsearch.php", params={"q": q, "fl[]": ["identifier", "title", "creator", "licenseurl"],
                                                             "rows": limit, "output": "json"}, timeout=30, retries=1)
    out = []
    for d in (res.get("response") or {}).get("docs", []):
        out.append({"ia_id": d["identifier"], "title": d.get("title", ""), "author": (d.get("creator") or "Unknown") if
                    isinstance(d.get("creator"), str) else ", ".join(d.get("creator") or [])[:80],
                    "license": "Public domain",
                    "page": f"{IA}/details/{d['identifier']}", "source": "Internet Archive"})
    return out


def ia_file(ia_id):
    meta = http.get_json(f"{IA}/metadata/{ia_id}", timeout=30, retries=1)
    files = meta.get("files") or []
    mp4 = [f for f in files if str(f.get("name", "")).lower().endswith(".mp4")]
    mp4.sort(key=lambda f: int(f.get("size") or 0))
    small = [f for f in mp4 if 2_000_000 < int(f.get("size") or 0) < 300_000_000] or mp4
    return f"{IA}/download/{ia_id}/{small[0]['name']}" if small else None


AGENCY_HINTS = {"earth": ("NOAA", "USGS"), "space": ("NASA", "ESA")}


def find_video(query, used, out_path, space=False, earth=False, history=False):
    """Real footage clip for a scene -> (path, credit). Sources tried by topic."""
    cands = []
    if space:
        try:
            cands += [dict(c, _nasa=True) for c in nasa_search(query, "video")]
        except Exception:
            pass
    try:
        cands += commons_search(query, "video")
        if earth:
            for hint in AGENCY_HINTS["earth"]:
                cands += commons_search(f"{query} {hint}", "video", limit=6)
    except Exception:
        pass
    if history or not cands:
        try:
            cands += [dict(c, _ia=True) for c in ia_search(query)]
        except Exception:
            pass
    for c in cands:
        key = c.get("page")
        if key in used:
            continue
        try:
            url = nasa_asset(c["nasa_id"], "video") if c.get("_nasa") else ia_file(c["ia_id"]) if c.get("_ia") else c["url"]
        except Exception:
            continue
        if not url:
            continue
        http.download(url, out_path, timeout=600)
        used.add(key)
        return out_path, {k: c.get(k) for k in ("source", "author", "license", "page", "title")}
    raise ArchiveError(f"실제 자료 영상 없음: {query}")


def find_photo(query, used, out_path, space=False, earth=False, history=False):
    cands = []
    if space:
        try:
            cands += [dict(c, _nasa=True) for c in nasa_search(query, "image")]
        except Exception:
            pass
    try:
        cands += commons_search(query, "image")
        if earth:
            for hint in AGENCY_HINTS["earth"]:
                cands += commons_search(f"{query} {hint}", "image", limit=6)
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


def find_person_video(name, used, out_path):
    """Freely licensed footage of the person (lectures, talks on Commons) - only if the file is clearly about them."""
    last = (name.split() or [""])[-1].lower()
    for c in commons_search(name, "video", limit=10):
        if c["page"] in used or last not in (c.get("title") or "").lower():
            continue
        http.download(c["url"], out_path, timeout=300)
        used.add(c["page"])
        return out_path, {k: c.get(k) for k in ("source", "author", "license", "page", "title")}
    raise ArchiveError(f"인물 영상 없음: {name}")


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
