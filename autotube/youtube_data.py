# -*- coding: utf-8 -*-
"""YouTube Data API v3 (API key): search, statistics, channel size, top comments.

Every call reserves quota first (budget.take_quota); when the day's quota is used up
the learner simply waits for the next day.
"""
from __future__ import annotations
import datetime as dt
import re

from . import config, http, budget

BASE = "https://www.googleapis.com/youtube/v3"


class QuotaExhausted(RuntimeError):
    pass


class YouTubeError(RuntimeError):
    pass


def _key():
    k = config.get_secret("YOUTUBE_API_KEY")
    if not k:
        raise YouTubeError("YOUTUBE_API_KEY가 설정되지 않았습니다")
    return k


def parse_duration(v):
    m = re.fullmatch(r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", v or "")
    if not m:
        return None
    d, h, mi, s = (int(x or 0) for x in m.groups())
    return d * 86400 + h * 3600 + mi * 60 + s


def search(query, published_within_days=180, max_results=50, duration="long", order="viewCount", region="KR", lang="ko"):
    if not budget.take_quota("search"):
        raise QuotaExhausted("오늘 YouTube 검색 할당량 소진")
    after = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=published_within_days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    res = http.get_json(BASE + "/search", params={
        "part": "snippet", "q": query, "type": "video", "order": order, "maxResults": max_results,
        "videoDuration": duration, "publishedAfter": after, "regionCode": region, "relevanceLanguage": lang,
        "key": _key()})
    return [it["id"]["videoId"] for it in res.get("items", []) if it.get("id", {}).get("videoId")]


def videos(ids):
    out = []
    ids = [i for i in dict.fromkeys(ids) if re.fullmatch(r"[A-Za-z0-9_-]{11}", i or "")]
    for i in range(0, len(ids), 50):
        if not budget.take_quota("videos"):
            raise QuotaExhausted("오늘 YouTube 할당량 소진")
        res = http.get_json(BASE + "/videos", params={"part": "snippet,contentDetails,statistics",
                                                      "id": ",".join(ids[i:i + 50]), "key": _key()})
        for it in res.get("items", []):
            sn, st = it.get("snippet", {}), it.get("statistics", {})

            def num(k):
                try:
                    return int(st[k])
                except (KeyError, TypeError, ValueError):
                    return None
            thumbs = sn.get("thumbnails", {})
            out.append({"id": it["id"], "title": sn.get("title", ""), "channel_id": sn.get("channelId"),
                        "channel_title": sn.get("channelTitle", ""), "published": sn.get("publishedAt"),
                        "duration_seconds": parse_duration((it.get("contentDetails") or {}).get("duration")),
                        "views": num("viewCount"), "likes": num("likeCount"), "comments": num("commentCount"),
                        "description": (sn.get("description") or "")[:3000], "tags": sn.get("tags", [])[:30],
                        "thumbnail_url": (thumbs.get("maxres") or thumbs.get("high") or {}).get("url", "")})
    return out


def channel_subscribers(channel_ids):
    out = {}
    ids = [c for c in dict.fromkeys(channel_ids) if c]
    for i in range(0, len(ids), 50):
        if not budget.take_quota("channels"):
            raise QuotaExhausted("오늘 YouTube 할당량 소진")
        res = http.get_json(BASE + "/channels", params={"part": "statistics", "id": ",".join(ids[i:i + 50]), "key": _key()})
        for it in res.get("items", []):
            st = it.get("statistics", {})
            try:
                out[it["id"]] = int(st.get("subscriberCount")) if not st.get("hiddenSubscriberCount") else None
            except (TypeError, ValueError):
                out[it["id"]] = None
    return out


def top_comments(video_id, n=15):
    if not budget.take_quota("comments"):
        return []
    try:
        res = http.get_json(BASE + "/commentThreads", params={"part": "snippet", "videoId": video_id, "order": "relevance",
                                                              "maxResults": n, "textFormat": "plainText", "key": _key()})
    except http.HttpError:
        return []  # comments disabled etc.
    out = []
    for it in res.get("items", []):
        sn = it.get("snippet", {}).get("topLevelComment", {}).get("snippet", {})
        text = " ".join(str(sn.get("textDisplay", "")).split())[:300]
        if text:
            out.append({"text": text, "likes": int(sn.get("likeCount") or 0)})
    return out
