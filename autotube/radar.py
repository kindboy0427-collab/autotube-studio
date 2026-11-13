# -*- coding: utf-8 -*-
"""Topic radar - the 'material + timing' lesson from the analysed videos.

Signal: a SMALL channel (few subscribers) whose RECENT video got many views
means people want this topic and few creators cover it yet (an empty seat).
We look for those in each channel's field and let the AI turn them into
topic suggestions for that channel, with the viewer emotion to aim at.
"""
from __future__ import annotations
import datetime as dt
import json

from . import channels, db, http, llm, watcher

PROMPT = """너는 과학·교육 유튜브 채널의 기획 PD다. 아래는 최근 {days}일 안에 올라온 영상 중
'구독자에 비해 조회수가 유난히 높은' 영상들이다(= 사람들은 보고 싶은데 만드는 채널이 적은 주제).
[채널] {channel}
[신호 영상] {signals}
이 채널이 지금 만들면 좋을 영상 주제 5개를 제안하라. 원본 제목·구성을 베끼지 말고 같은 관심을 다른 각도로 풀 것.
과학적으로 정확하게 다룰 수 있는 주제만. JSON만 출력:
{{"ideas":[{{"topic":"영상 주제(명령으로 바로 쓸 수 있게)","why_now":"왜 지금인지","entry_emotion":"클릭할 때 감정",
"exit_emotion":"보고 나갈 때 감정","signal_ids":["근거 영상 id"]}}]}}"""


def signals(channel_id, days=14, limit=12, max_subs=20000):
    since = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return db.rows("""SELECT id, title, channel_title, subscribers, views, views_per_day, outlier_score, published
        FROM videos WHERE niche=? AND published>=? AND (subscribers IS NULL OR subscribers<=?)
        ORDER BY outlier_score DESC LIMIT ?""", (channel_id, since, max_subs, limit))


def refresh(channel_id, max_searches=2):
    """Spend a little search quota (2 searches x 100 units per niche) on this field's newest videos."""
    ch = channels.get(channel_id)
    if not ch:
        raise KeyError(channel_id)
    return sum(_discover_recent(niche, channel_id) for niche in ch["niches"][:max_searches])


def _discover_recent(query, channel_id):
    from . import youtube_data
    ids = youtube_data.search(query, published_within_days=14, max_results=50, duration="medium", order="viewCount")
    ids += youtube_data.search(query, published_within_days=14, max_results=50, duration="long", order="viewCount")
    known = {r["id"] for r in db.rows("SELECT id FROM videos WHERE id IN (%s)" % ",".join("?" * len(ids)), ids)} if ids else set()
    fresh = [i for i in dict.fromkeys(ids) if i not in known]
    if not fresh:
        return 0
    vids = youtube_data.videos(fresh)
    subs = youtube_data.channel_subscribers([v["channel_id"] for v in vids])
    added = 0
    with db.connect() as con:
        for v in vids:
            if not v["duration_seconds"] or v["duration_seconds"] < 180:
                continue
            age = watcher._age_days(v["published"]) or 14
            views = v["views"] or 0
            sub = subs.get(v["channel_id"])
            outlier = views / max(sub or 0, 1000) if sub is not None else views / age / 1000
            con.execute("""INSERT OR IGNORE INTO videos(id,title,channel_id,channel_title,published,duration_seconds,
                views,likes,comments,subscribers,views_per_day,outlier_score,engagement_rate,description,tags,
                thumbnail_url,niche,query,status,discovered_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'queued',?)""",
                        (v["id"], v["title"], v["channel_id"], v["channel_title"], v["published"], v["duration_seconds"],
                         v["views"], v["likes"], v["comments"], sub, round(views / age, 2), round(outlier, 4),
                         0, v["description"], json.dumps(v["tags"], ensure_ascii=False), v["thumbnail_url"],
                         channel_id, "radar:" + query, db.now()))
            added += 1
    return added


def suggest(channel_id, generate=None, days=14):
    ch = channels.get(channel_id)
    if not ch:
        raise KeyError(channel_id)
    sig = signals(channel_id, days)
    if not sig:
        return {"ideas": [], "signals": [], "message": "최근 신호 영상이 없습니다. [최신 영상 검색]을 먼저 실행하세요."}
    generate = generate or (lambda prompt: llm.generate_json(prompt, "과학 유튜브 기획 PD", max_tokens=4000))
    res = generate(PROMPT.format(days=days, channel=json.dumps({k: ch[k] for k in ("name", "niches", "audience", "tone")},
                                                                 ensure_ascii=False),
                                 signals=json.dumps(sig, ensure_ascii=False)[:9000]))
    if isinstance(res, str):
        res = http.parse_json_loose(res)
    valid = {s["id"] for s in sig}
    ideas = []
    for it in (res or {}).get("ideas", [])[:5]:
        if isinstance(it, dict) and it.get("topic"):
            it["signal_ids"] = [i for i in it.get("signal_ids", []) if i in valid]
            ideas.append(it)
    db.kv_set("radar:" + channel_id, {"at": db.now(), "ideas": ideas})
    return {"ideas": ideas, "signals": sig}
