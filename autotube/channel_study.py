# -*- coding: utf-8 -*-
"""Learn a whole YouTube channel, not just single videos.

Give a channel link (@handle, /channel/UC..., or a video link from it):
 1 read its uploads list (cheap: ~1 quota unit per 50 videos, no search)
 2 compare every long-form video with the CHANNEL'S OWN median views
   -> which titles/topics/lengths broke out *for this audience* (the strongest signal)
 3 write a channel report (titles, topics, length, cadence, what to borrow) with the script AI
 4 queue the top breakouts (+ a few flops for contrast) for full watching/analysis
"""
from __future__ import annotations
import datetime as dt
import json
import re
import statistics

from . import config, db, http, youtube_data
from .youtube_data import BASE, QuotaExhausted, YouTubeError, _key


def resolve(text: str) -> str:
    """Channel URL / @handle / video URL -> channel id (UC...)."""
    t = text.strip()
    m = re.search(r"(UC[A-Za-z0-9_-]{22})", t)
    if m:
        return m.group(1)
    m = re.search(r"(?:youtube\.com/)?@([A-Za-z0-9._\-ㄱ-힝]+)", t)
    if m:
        if not youtube_data.budget.take_quota("channels"):
            raise QuotaExhausted("오늘 YouTube 할당량 소진")
        res = http.get_json(BASE + "/channels", params={"part": "id", "forHandle": "@" + m.group(1), "key": _key()})
        items = res.get("items") or []
        if items:
            return items[0]["id"]
        raise YouTubeError("채널을 찾지 못했습니다: @" + m.group(1))
    m = re.search(r"(?:v=|youtu\.be/|shorts/|embed/)([A-Za-z0-9_-]{11})", t)
    if m:
        v = youtube_data.videos([m.group(1)])
        if v:
            return v[0]["channel_id"]
    raise YouTubeError("채널 주소를 인식할 수 없습니다 (예: https://www.youtube.com/@채널이름)")


def channel_meta(cid):
    if not youtube_data.budget.take_quota("channels"):
        raise QuotaExhausted("오늘 YouTube 할당량 소진")
    res = http.get_json(BASE + "/channels", params={"part": "snippet,statistics,contentDetails", "id": cid, "key": _key()})
    items = res.get("items") or []
    if not items:
        raise YouTubeError("채널 정보를 가져오지 못했습니다")
    it = items[0]
    st = it.get("statistics", {})
    return {"id": cid, "title": it["snippet"]["title"], "description": it["snippet"].get("description", "")[:500],
            "subscribers": int(st["subscriberCount"]) if st.get("subscriberCount") and not st.get("hiddenSubscriberCount") else None,
            "video_count": int(st.get("videoCount") or 0),
            "uploads": (it.get("contentDetails") or {}).get("relatedPlaylists", {}).get("uploads")}


def upload_ids(playlist_id, limit=200):
    ids, token = [], None
    while len(ids) < limit:
        if not youtube_data.budget.take_quota("playlist"):
            raise QuotaExhausted("오늘 YouTube 할당량 소진")
        params = {"part": "contentDetails", "playlistId": playlist_id, "maxResults": 50, "key": _key()}
        if token:
            params["pageToken"] = token
        res = http.get_json(BASE + "/playlistItems", params=params)
        ids += [i["contentDetails"]["videoId"] for i in res.get("items", []) if i.get("contentDetails", {}).get("videoId")]
        token = res.get("nextPageToken")
        if not token:
            break
    return ids[:limit]


def _age_days(published):
    try:
        t = dt.datetime.fromisoformat(published.replace("Z", "+00:00"))
        return max(1.0, (dt.datetime.now(dt.timezone.utc) - t).total_seconds() / 86400)
    except Exception:
        return 365.0


REPORT_PROMPT = """너는 유튜브 채널 전략 분석가다. 아래는 한 채널의 롱폼 영상 목록이다.
'배수'는 그 영상 조회수 ÷ 이 채널 영상 조회수 중앙값이다(1.0 = 평소, 3.0 = 평소의 3배).
이 채널 시청자에게 무엇이 먹혔고 무엇이 안 먹혔는지 비교해서, 우리가 '원리만' 가져다 쓸 수 있게 정리하라.
대사·제목을 그대로 베끼라고 하지 말 것. 근거 없는 추측은 '추정'이라고 표시.

[채널] {title} · 구독자 {subs} · 분석한 롱폼 {n}편 · 중앙값 조회수 {median:,}

[평소보다 크게 터진 영상]
{top}

[평소보다 안 된 영상]
{bottom}

[업로드 간격·길이 통계]
{stats}

JSON으로만 답하라:
{{"one_line": "이 채널이 잘 되는 이유 한 문장",
 "title_patterns": ["터진 제목들의 공통 패턴 3~6개"],
 "topic_patterns": ["잘 되는 주제·소재 3~6개"],
 "avoid": ["안 된 영상들의 공통점 2~5개"],
 "length_and_cadence": "적정 길이와 업로드 주기에 대한 관찰",
 "borrow": ["우리 채널이 가져다 쓸 원리 3~6개"],
 "gaps": ["이 채널이 아직 안 다룬, 우리가 먼저 할 만한 빈자리 주제 3~5개"]}}"""


def study(text, app_channel_id="", max_videos=200, queue_top=12, queue_bottom=3, favorite=False, generate=None):
    from . import llm
    cid = resolve(text)
    meta = channel_meta(cid)
    ids = upload_ids(meta["uploads"], max_videos) if meta.get("uploads") else []
    vids = youtube_data.videos(ids)
    s = config.load_settings()["learning"]
    longs = [v for v in vids if (v.get("duration_seconds") or 0) >= s.get("min_duration_seconds", 240) and v.get("views") is not None]
    if len(longs) < 4:
        raise YouTubeError(f"분석할 롱폼 영상이 너무 적습니다 ({len(longs)}편)")
    med = statistics.median(v["views"] for v in longs) or 1
    for v in longs:
        v["ratio"] = round(v["views"] / med, 2)
        v["vpd"] = v["views"] / _age_days(v.get("published") or "")
    ranked = sorted(longs, key=lambda v: v["ratio"], reverse=True)
    top, bottom = ranked[:20], ranked[-12:]
    dates = sorted(dt.datetime.fromisoformat(v["published"].replace("Z", "+00:00")) for v in longs if v.get("published"))
    gaps = [(b - a).days for a, b in zip(dates, dates[1:])]
    stats = {"median_minutes": round(statistics.median(v["duration_seconds"] for v in longs) / 60, 1),
             "top_median_minutes": round(statistics.median(v["duration_seconds"] for v in top) / 60, 1),
             "median_upload_gap_days": statistics.median(gaps) if gaps else None, "first": str(dates[0].date()) if dates else "",
             "last": str(dates[-1].date()) if dates else ""}
    fmt = lambda vs: "\n".join(f"- [{v['ratio']}배] {v['title']} ({round(v['duration_seconds'] / 60)}분, 조회 {v['views']:,})" for v in vs)
    generate = generate or (lambda p: llm.generate_json(p, "채널 전략 분석가"))
    report = {}
    try:
        report = generate(REPORT_PROMPT.format(title=meta["title"], subs=meta["subscribers"] or "비공개", n=len(longs),
                                               median=int(med), top=fmt(top), bottom=fmt(bottom),
                                               stats=json.dumps(stats, ensure_ascii=False)))
    except Exception as ex:
        report = {"one_line": "보고서 작성 실패: " + str(ex)[:150]}
    # queue breakouts (+ a few flops for contrast) for full analysis
    pick = ranked[:queue_top] + ([v for v in ranked[-queue_bottom:] if v not in ranked[:queue_top]] if queue_bottom else [])
    added = 0
    with db.connect() as con:
        for v in pick:
            if con.execute("SELECT 1 FROM videos WHERE id=?", (v["id"],)).fetchone():
                if favorite:
                    con.execute("UPDATE videos SET favorite=1 WHERE id=?", (v["id"],))
                continue
            con.execute("""INSERT INTO videos(id,title,channel_id,channel_title,published,duration_seconds,views,likes,comments,
                subscribers,views_per_day,outlier_score,engagement_rate,description,tags,thumbnail_url,niche,query,status,
                discovered_at,favorite) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'queued',?,?)""",
                        (v["id"], v["title"], cid, meta["title"], v["published"], v["duration_seconds"], v["views"], v["likes"],
                         v["comments"], meta["subscribers"], v["vpd"], v["ratio"],
                         ((v["likes"] or 0) + (v["comments"] or 0)) / max(v["views"], 1), v["description"],
                         json.dumps(v["tags"], ensure_ascii=False), v["thumbnail_url"], app_channel_id, "channel:" + cid,
                         db.now(), 1 if favorite and v in ranked[:queue_top] else 0))
            added += 1
    out = {"channel": {k: meta[k] for k in ("id", "title", "subscribers", "video_count")}, "studied": len(longs),
           "median_views": int(med), "stats": stats, "report": report, "queued": added, "favorite": favorite,
           "app_channel_id": app_channel_id, "at": db.now(),
           "top": [{"id": v["id"], "title": v["title"], "ratio": v["ratio"], "views": v["views"]} for v in top[:10]]}
    db.kv_set("study:" + cid, out)
    db.event("info", "study", f"채널 통째 학습: {meta['title']} — 롱폼 {len(longs)}편 비교, {added}편 정밀 분석 대기열")
    return out


def all_studies():
    rows = db.rows("SELECT key, value FROM kv WHERE key LIKE 'study:%' ORDER BY key")
    out = [json.loads(r["value"]) for r in rows]
    return sorted(out, key=lambda x: x.get("at", ""), reverse=True)


def guidance_for(app_channel_id, limit=3):
    """Short channel-study notes injected into planning for this app channel."""
    notes = []
    for s in all_studies():
        if app_channel_id and s.get("app_channel_id") not in (app_channel_id, ""):
            continue
        r = s.get("report") or {}
        notes.append({"channel": s["channel"]["title"], "why": r.get("one_line"), "titles": r.get("title_patterns"),
                      "topics": r.get("topic_patterns"), "avoid": r.get("avoid"), "borrow": r.get("borrow")})
        if len(notes) >= limit:
            break
    return notes


SYNTH_PROMPT = """너는 과학·교육 유튜브 총괄 PD다. 여러 참고 채널을 통째로 분석한 결과와, 분야 전체에서 '작은 채널인데 터진' 신호 영상,
그리고 우리 채널이 지금까지 학습한 성과 패턴을 종합해서 전략을 세워라.
목표: 참고 채널들과 '비슷한 느낌'(톤·구성·속도)은 살리되, 어느 한 채널의 복제가 아니라 공통 원리만 섞고,
잘 되는 것과 안 되는 것을 분리한 뒤, 그 데이터로 '아직 아무도 제대로 안 다룬 빈자리 주제'를 만든다.

[우리 채널] {channel}
[참고 채널 분석들]
{studies}
[분야 신호 영상 — 구독자 대비 조회수 높음]
{signals}
[우리가 학습한 성과 패턴 (do / avoid)]
{playbook}
[이미 만든 우리 영상 — 겹치지 않게]
{recent}

규칙: 참고 채널 영상과 같은 주제·제목은 빈자리가 아니다. 근거(evidence)는 위 데이터에 있는 것만 적는다. 과학적으로 정확히 다룰 수 있는 주제만.
JSON으로만 답하라:
{{"feel": "참고 채널들의 공통된 느낌을 우리 채널에 옮기는 방법 (톤·구성·속도·화면) 3~4문장",
 "works": ["여러 채널에서 공통으로 잘 된 요소 4~8개 (데이터 근거 포함)"],
 "fails": ["공통으로 안 된 요소 3~6개"],
 "gap_topics": [{{"topic": "영상 주제 (명령으로 바로 쓸 수 있게)", "why_gap": "왜 빈자리인지",
   "evidence": ["근거 1~3개"], "format": "reversal_essay|catalog|staged_scenario|metaphor_doc|explainer",
   "entry_emotion": "", "exit_emotion": ""}}]}}  (gap_topics는 8개)"""


def synthesize(app_channel_id="", generate=None):
    """Blend every studied channel + field signals + our own playbook -> works / fails / gap topics."""
    from . import channels, learner, llm, radar
    studies = [s for s in all_studies() if not app_channel_id or s.get("app_channel_id") in (app_channel_id, "")]
    if not studies:
        raise ValueError("먼저 참고 채널을 1개 이상 '채널 통째로 학습' 하세요")
    ch = channels.get(app_channel_id) if app_channel_id else None
    slim = [{"channel": s["channel"]["title"], "subscribers": s["channel"].get("subscribers"), "median_views": s["median_views"],
             "report": s.get("report"), "top": [f"{t['title']} ({t['ratio']}배)" for t in (s.get("top") or [])[:8]]}
            for s in studies[:8]]
    sig = radar.signals(app_channel_id, days=60, limit=15) if app_channel_id else []
    pb = learner.playbook()
    short_pb = {"do": {k: [x["feature"] for x in v] for k, v in pb["do"].items()},
                "avoid": {k: [x["feature"] for x in v] for k, v in pb["avoid"].items()}}
    recent = [r["topic"] for r in db.rows("SELECT topic FROM productions WHERE (channel_id=? OR ?='') ORDER BY created_at DESC LIMIT 15",
                                          (app_channel_id, app_channel_id))]
    generate = generate or (lambda p: llm.generate_json(p, "과학 유튜브 총괄 PD", max_tokens=6000))
    res = generate(SYNTH_PROMPT.format(
        channel=json.dumps({k: (ch or {}).get(k) for k in ("name", "niches", "audience", "tone")}, ensure_ascii=False) if ch else "(모든 채널 공통)",
        studies=json.dumps(slim, ensure_ascii=False)[:14000], signals=json.dumps(
            [{"title": x["title"], "channel": x["channel_title"], "subs": x["subscribers"], "views": x["views"]} for x in sig],
            ensure_ascii=False)[:4000] if sig else "(없음)",
        playbook=json.dumps(short_pb, ensure_ascii=False)[:3000], recent=json.dumps(recent, ensure_ascii=False)))
    if isinstance(res, str):
        res = http.parse_json_loose(res)
    res = res if isinstance(res, dict) else {}
    from . import planner
    gaps = []
    for g in (res.get("gap_topics") or [])[:10]:
        if isinstance(g, dict) and g.get("topic"):
            if g.get("format") not in planner.FORMATS:
                g["format"] = ""
            gaps.append(g)
    out = {"at": db.now(), "app_channel_id": app_channel_id, "based_on": [s["channel"]["title"] for s in studies[:8]],
           "feel": res.get("feel", ""), "works": res.get("works") or [], "fails": res.get("fails") or [], "gap_topics": gaps}
    db.kv_set("synth:" + (app_channel_id or "_all"), out)
    db.event("info", "study", f"종합 분석 완료 ({', '.join(out['based_on'])}) → 빈자리 주제 {len(gaps)}개")
    return out


def get_synth(app_channel_id=""):
    return db.kv_get("synth:" + (app_channel_id or "_all")) or (db.kv_get("synth:_all") if app_channel_id else None)
