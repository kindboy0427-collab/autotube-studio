# -*- coding: utf-8 -*-
"""The 'AI watches YouTube' half.

discover(): search long-form videos per niche, keep stats, score outliers
            (views far above channel size = algorithm favoured it).
watch_next(): Gemini watches queued public videos (picture + audio) from the URL
              and returns a structured breakdown stored in the knowledge base.
Nothing is downloaded; only public videos are analysed.
"""
from __future__ import annotations
import datetime as dt
import json
import random
import re

from . import config, db, gemini, http, transcripts, youtube_data

VOCAB = {
    "format": ["documentary", "explainer", "listicle", "story", "interview", "commentary", "tutorial", "review",
               "news", "compilation", "other"],
    "hook": ["question", "shocking_fact", "story_open", "preview_payoff", "bold_claim", "problem", "visual_spectacle",
             "direct_promise", "other"],
    "retention": ["open_loop", "cliffhanger", "countdown", "pattern_interrupt", "dense_b_roll", "on_screen_text",
                  "sound_effects", "music_shift", "chapters", "recap", "foreshadowing", "humor", "conflict",
                  "stakes_raise", "visual_reveal", "rhetorical_questions", "personal_story", "data_visual"],
    "engagement": ["comment_question", "controversy", "relatable_pain", "subscribe_ask_early", "subscribe_ask_late",
                   "pinned_comment_prompt", "community_reference", "none"],
    "narration": ["calm_documentary", "energetic", "conversational", "authoritative", "storytelling", "humorous"],
    "visual": ["stock_footage", "ai_generated", "animation_2d", "motion_graphics", "talking_head", "screen_recording",
               "map_graphics", "archival", "real_footage"],
    "emotion": ["curiosity", "tension", "surprise", "relief", "inspiration", "fear", "humor", "nostalgia", "anger"],
    "title": ["number_list", "question", "how_to", "secret_reveal", "warning", "comparison", "personal_story",
              "superlative", "curiosity_gap", "timely_news"],
    "pace": ["slow", "medium", "fast"],
}

WATCH_PROMPT = """당신은 유튜브 롱폼 성과 분석가다. 이 영상을 처음부터 끝까지 화면과 소리를 모두 보고 듣고 분석하라.
영상 제목: {title}
채널: {channel} (구독자 {subs}) · 조회수 {views} · 게시 {published} · 길이 {minutes}분
목표: 이 영상이 시청 유지율·참여·알고리즘 추천을 얻은(또는 못 얻은) 이유를 구조적으로 뽑아, 다른 주제의 새 영상에 응용할 수 있는 일반 원리로 만든다.
원본 내용·대사를 복제하지 말고, 관찰한 것만 쓰고 추측은 '추정'이라고 표시하라.
아래 JSON 하나만 출력하라. 영문 태그 값은 반드시 제시된 목록에서만 고른다.
{{
 "topic": "주제 한 줄",
 "summary": "무엇을 어떻게 보여주는지 3문장",
 "format": {format},
 "hook": {{"type": {hook}, "first_15s": "첫 15초에 실제로 무엇을 보여주고 말하는지", "promise": "시청자에게 약속한 보상"}},
 "structure": [{{"start": "mm:ss", "label": "구간 이름", "purpose": "이 구간의 역할"}}],
 "retention_devices": {retention} 중 실제 사용된 것들의 배열,
 "engagement_triggers": {engagement} 중 사용된 것들의 배열,
 "pacing": {{"avg_shot_seconds": 숫자, "narration_speed": {pace}, "energy": {pace}}},
 "narration_style": {narration} 중 1~2개 배열,
 "visual_style": {visual} 중 사용된 것들의 배열,
 "visual_mix": {{"real_footage": 0, "archival": 0, "interview_clip": 0, "motion_graphics": 0, "stock": 0, "ai_generated": 0, "talking_head": 0, "photo": 0}}
   (화면에 나온 시간 비율 %, 합 100. real_footage=실제 촬영 현장·공정·실험, archival=옛 자료·뉴스 화면, interview_clip=인물 발언 영상,
    motion_graphics=도식·애니메이션·자막 그래픽, photo=정지 사진에 줌·패닝),
 "shot_ideas": ["이 영상이 쓴 화면 연출 방식 3~6개를 일반화해서 (예: '공정 순서를 실사 클로즈업으로 이어붙임', '인물 사진 + 발언 자막 카드')"],
 "emotional_arc": {emotion} 중 등장 순서대로 배열,
 "title_pattern": {titlep} 중 해당하는 것들의 배열,
 "cta_timing": "none|early|middle|end",
 "why_it_works": ["성과 요인 가설 3~5개"],
 "weaknesses": ["이탈을 부를 만한 약점 1~3개"],
 "reusable_ideas": ["다른 주제에도 쓸 수 있는 일반화된 제작 아이디어 3~5개"]
}}"""


def _num(v):
    return f"{v:,}" if isinstance(v, int) else "비공개"


def _age_days(published):
    try:
        t = dt.datetime.fromisoformat(published.replace("Z", "+00:00"))
        return max(1.0, (dt.datetime.now(dt.timezone.utc) - t).total_seconds() / 86400)
    except (AttributeError, ValueError):
        return None


def discover(query: str, niche: str = "", order: str = "viewCount") -> int:
    s = config.load_settings()["learning"]
    ids = []
    for duration in ("long", "medium"):
        ids += youtube_data.search(query, s["published_within_days"], 50, duration=duration, order=order)
    known = {r["id"] for r in db.rows("SELECT id FROM videos WHERE id IN (%s)" % ",".join("?" * len(ids)), ids)} if ids else set()
    fresh = [i for i in dict.fromkeys(ids) if i not in known]
    if not fresh:
        return 0
    vids = [v for v in youtube_data.videos(fresh)
            if v["duration_seconds"] and s["min_duration_seconds"] <= v["duration_seconds"] <= s["max_duration_seconds"]]
    subs = youtube_data.channel_subscribers([v["channel_id"] for v in vids])
    added = 0
    with db.connect() as con:
        for v in vids:
            age = _age_days(v["published"]) or 365
            views = v["views"] or 0
            sub = subs.get(v["channel_id"])
            vpd = views / age
            outlier = views / max(sub or 0, 1000) if sub is not None else vpd / 1000
            eng = ((v["likes"] or 0) + 3 * (v["comments"] or 0)) / views if views else 0
            con.execute("""INSERT OR IGNORE INTO videos(id,title,channel_id,channel_title,published,duration_seconds,
                views,likes,comments,subscribers,views_per_day,outlier_score,engagement_rate,description,tags,
                thumbnail_url,niche,query,status,discovered_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'queued',?)""",
                        (v["id"], v["title"], v["channel_id"], v["channel_title"], v["published"], v["duration_seconds"],
                         v["views"], v["likes"], v["comments"], sub, round(vpd, 2), round(outlier, 4), round(eng, 5),
                         v["description"], json.dumps(v["tags"], ensure_ascii=False), v["thumbnail_url"], niche, query, db.now()))
            added += 1
    db.event("info", "discover", f"'{query}' 검색 → 새 롱폼 {added}개 대기열 추가")
    return added


def gemini_watches_today():
    return db.kv_get("gemini_watch_count:" + db.kst_day(), 0)


def watched_seconds_today():
    return db.kv_get("watch_seconds:" + db.kst_day(), 0)


# The owner's favourite reference videos (taste references - structure only, see planner.FORMATS).
REFERENCES = [
    {"id": "xpgBqE_OgRo", "title": "AI가 인간을 배신하는 진짜 이유", "channel_title": "빨강도깨비", "duration_seconds": 559, "views": 251639},
    {"id": "IDO2Y8MC-00", "title": "마블이 숨겨둔 지구번호의 비밀", "channel_title": "빨강도깨비", "duration_seconds": 528, "views": 302554},
    {"id": "3WvCed5mL_w", "title": "AI는 인류를 어떻게 끝낼까? 실제 인류 멸망 시나리오", "channel_title": "레드라인 뉴스룸",
     "duration_seconds": 739, "views": 30479},
    {"id": "T8chVAMZ_8c", "title": "AI가 사람을 대체한다면? 억만장자들이 숨기는 진짜 미래", "channel_title": "특이점 노트",
     "duration_seconds": 1280, "views": 34944},
]


def seed_references():
    """Queue the built-in taste references once (they are watched first and weighted higher)."""
    if db.kv_get("seeded_references"):
        return 0
    n = 0
    with db.connect() as con:
        for r in REFERENCES:
            if con.execute("SELECT 1 FROM videos WHERE id=?", (r["id"],)).fetchone():
                con.execute("UPDATE videos SET favorite=1 WHERE id=?", (r["id"],))
                continue
            con.execute("""INSERT INTO videos(id,title,channel_title,duration_seconds,views,outlier_score,niche,query,status,
                discovered_at,favorite) VALUES(?,?,?,?,?,?,?,?,?,?,1)""",
                        (r["id"], r["title"], r["channel_title"], r["duration_seconds"], r["views"], 999, "", "reference",
                         "queued", db.now()))
            n += 1
    db.kv_set("seeded_references", True)
    return n


def _pick_queue(n):
    """Favourite references first, then 70% highest outliers (what works) + 30% random (contrast)."""
    fav = db.rows("SELECT * FROM videos WHERE status='queued' AND favorite=1 ORDER BY discovered_at LIMIT ?", (n,))
    if len(fav) >= n:
        return fav
    n -= len(fav)
    top = db.rows("SELECT * FROM videos WHERE status='queued' AND COALESCE(favorite,0)=0 ORDER BY outlier_score DESC LIMIT ?", (n * 3,))
    if not top:
        return fav
    k_top = max(1, round(n * 0.7))
    chosen = top[:k_top]
    rest = db.rows("SELECT * FROM videos WHERE status='queued' AND COALESCE(favorite,0)=0 AND id NOT IN (%s) ORDER BY RANDOM() LIMIT ?"
                   % ",".join("?" * len(chosen)), [c["id"] for c in chosen] + [n - k_top])
    return fav + chosen + rest


def _clean_list(values, vocab):
    if isinstance(values, str):
        values = [values]
    return sorted({str(v).strip().lower() for v in (values or []) if str(v).strip().lower() in vocab})


def features_from(a: dict) -> list:
    f = []
    f += ["format:" + x for x in _clean_list(a.get("format"), VOCAB["format"])]
    f += ["hook:" + x for x in _clean_list((a.get("hook") or {}).get("type"), VOCAB["hook"])]
    f += ["retention:" + x for x in _clean_list(a.get("retention_devices"), VOCAB["retention"])]
    f += ["engagement:" + x for x in _clean_list(a.get("engagement_triggers"), VOCAB["engagement"])]
    f += ["narration:" + x for x in _clean_list(a.get("narration_style"), VOCAB["narration"])]
    f += ["visual:" + x for x in _clean_list(a.get("visual_style"), VOCAB["visual"])]
    arc = [str(x).lower() for x in (a.get("emotional_arc") or []) if str(x).lower() in VOCAB["emotion"]]
    f += ["emotion:" + x for x in sorted(set(arc))]
    if arc:
        f.append("opens_with:" + arc[0])
    f += ["title:" + x for x in _clean_list(a.get("title_pattern"), VOCAB["title"])]
    pace = a.get("pacing") or {}
    f += ["pace:" + x for x in _clean_list(pace.get("narration_speed"), VOCAB["pace"])]
    try:
        shot = float(pace.get("avg_shot_seconds"))
        f.append("shots:" + ("fast_cuts" if shot < 3 else "medium_cuts" if shot < 7 else "slow_cuts"))
    except (TypeError, ValueError):
        pass
    cta = str(a.get("cta_timing", "")).lower()
    if cta in ("none", "early", "middle", "end"):
        f.append("cta:" + cta)
    return sorted(set(f))


def watch_video(v: dict, watcher=None, counts_gemini_hours=True) -> dict:
    """Analyse one video. `watcher` is injectable for tests (defaults to Gemini)."""
    watcher = watcher or gemini.watch_youtube
    url = "https://www.youtube.com/watch?v=" + v["id"]
    prompt = WATCH_PROMPT.format(
        title=v["title"], channel=v["channel_title"], subs=_num(v.get("subscribers")), views=_num(v.get("views")),
        published=(v.get("published") or "")[:10], minutes=round((v.get("duration_seconds") or 0) / 60, 1),
        **{k: json.dumps(VOCAB[k2], ensure_ascii=False) for k, k2 in (
            ("format", "format"), ("hook", "hook"), ("retention", "retention"), ("engagement", "engagement"),
            ("narration", "narration"), ("visual", "visual"), ("emotion", "emotion"), ("titlep", "title"), ("pace", "pace"))})
    text = watcher(url, prompt, v.get("duration_seconds") or 0)
    a = http.parse_json_loose(text)
    if not isinstance(a, dict) or not a.get("summary"):
        raise ValueError("분석 JSON 형식 오류")
    comments = []
    try:
        comments = youtube_data.top_comments(v["id"])
    except Exception:
        comments = []
    feats = features_from(a)
    model = config.load_settings()["providers"]["gemini_watch_model"] if counts_gemini_hours else "captions+llm"
    with db.connect() as con:
        con.execute("INSERT OR REPLACE INTO analyses(video_id,created_at,model,summary,topic,features,raw,top_comments) "
                    "VALUES(?,?,?,?,?,?,?,?)",
                    (v["id"], db.now(), model, str(a.get("summary"))[:1500], str(a.get("topic", ""))[:200],
                     json.dumps(feats), json.dumps(a, ensure_ascii=False), json.dumps(comments, ensure_ascii=False)))
        con.execute("DELETE FROM analyses_fts WHERE video_id=?", (v["id"],))
        con.execute("INSERT INTO analyses_fts(video_id,topic,summary,title) VALUES(?,?,?,?)",
                    (v["id"], str(a.get("topic", "")), str(a.get("summary", "")), v["title"]))
        con.execute("UPDATE videos SET status='watched', watched_at=?, error=NULL WHERE id=?", (db.now(), v["id"]))
    if counts_gemini_hours:
        db.kv_set("watch_seconds:" + db.kst_day(), watched_seconds_today() + (v.get("duration_seconds") or 0))
    return a


def _transcript_ok():
    from . import llm
    return bool(config.get_secret("APIFY_TOKEN")) and bool(llm.order())


def watch_next(n=None, watcher=None, transcript_watcher=None) -> int:
    s = config.load_settings()["learning"]
    n = n or s["watch_batch"]
    done_today = db.one("SELECT COUNT(*) c FROM videos WHERE status='watched' AND substr(watched_at,1,10)=?",
                        (dt.datetime.now(dt.timezone.utc).date().isoformat(),))["c"]
    n = min(n, max(0, s["max_watch_per_day"] - done_today))
    count = 0
    mode = s.get("watch_mode", "auto")
    transcript_watcher = transcript_watcher or transcripts.watch_via_transcript
    full_limit = int(s.get("gemini_full_watch_per_day", 10))
    free = gemini.free_key_set()
    if free:
        # free tier = ~20 requests/day per model (reset at midnight US Pacific), failed attempts included.
        # Spend them only on full watches, never hammer after the quota is gone.
        from . import tts
        qday = tts.quota_day()
        if db.kv_get("free_watch_blocked:" + qday):
            return 0
        full_limit = 10 ** 6
        free_cap = int(s.get("free_watch_requests_per_day", 18))
    for v in _pick_queue(n):
        use = watcher
        if free and watcher is gemini.watch_youtube:
            used = int(db.kv_get("free_watch_req:" + qday, 0) or 0)
            if used >= free_cap:
                db.event("info", "watch", f"무료 시청 오늘 {used}회 사용 → 한도 초기화(오후 5시경) 후 이어서 시청")
                break
            db.kv_set("free_watch_req:" + qday, used + 1)
        over = (not free and not s.get("gemini_paid_tier")
                and watched_seconds_today() + (v.get("duration_seconds") or 0) > s["free_tier_watch_hours"] * 3600)
        captions_ok = _transcript_ok() or transcript_watcher is not transcripts.watch_via_transcript
        if mode == "hybrid":
            # queue order = taste references, then strongest outliers -> they get the full Gemini watch
            if gemini_watches_today() >= full_limit or over:
                if not captions_ok:
                    db.event("info", "watch", "오늘 Gemini 전체 시청 한도 도달 — 나머지 자막 분석은 Apify 키가 있어야 합니다")
                    break
                use = transcript_watcher
        elif mode == "transcript" or (over and mode == "auto" and captions_ok):
            use = transcript_watcher
        elif over:
            db.event("info", "watch", "Gemini 무료 등급 하루 시청 한도 도달 — 내일 이어서 시청 (Apify 키를 넣으면 자막으로 계속 분석)")
            break
        try:
            watch_video(v, use, counts_gemini_hours=use is not transcript_watcher)
            if use is not transcript_watcher:
                db.kv_set("gemini_watch_count:" + db.kst_day(), gemini_watches_today() + 1)
            count += 1
        except Exception as ex:
            msg = str(ex)[:300]
            body = getattr(ex, "body", "") or ""
            if free and getattr(ex, "status", None) == 429 and re.search(r"per[_ ]?day|PerDay", body, re.I):
                # daily quota gone: keep the video queued, stop until the reset (no retry loop)
                db.kv_set("free_watch_blocked:" + qday, True)
                db.execute("UPDATE videos SET error=? WHERE id=?", ("무료 하루 한도 소진 → 내일 시청", v["id"]))
                db.event("info", "watch", "Gemini 무료 시청 하루 한도 소진 → 한도 초기화(오후 5시경) 후 이어서 시청")
                break
            tries = int(db.kv_get("watch_fail:" + v["id"], 0) or 0) + 1
            db.kv_set("watch_fail:" + v["id"], tries)
            permanent = "형식" in msg or any(f"HTTP {c}" in msg for c in (400, 403, 404)) or tries >= 2
            db.execute("UPDATE videos SET status=?, error=? WHERE id=?",
                       ("failed" if permanent else "queued", msg, v["id"]))
            db.event("warn", "watch", f"{v['id']} 분석 실패: {msg}" + (" → 건너뜀" if permanent else " → 한 번만 다시 시도"))
            if "BudgetExceeded" in type(ex).__name__ or "예산" in msg:
                break
    if count:
        db.event("info", "watch", f"AI가 영상 {count}개 시청·분석 완료")
    return count


def seed_queries():
    """(query, group) pairs. group = channel id for channel niches, so learning stays per channel."""
    from . import channels
    s = config.load_settings()["learning"]
    pairs = [(n, cid) for n, cid in channels.all_niches()]
    pairs += [(n, "") for n in (s.get("niches") or [])]
    pairs += [(r["topic"], r.get("channel_id") or "") for r in
              db.rows("SELECT topic, channel_id FROM productions ORDER BY created_at DESC LIMIT 5")]
    seen, out = set(), []
    angles = ["", " 원리", " 이유", " 몰랐던 사실", " 다큐", " 쉽게 설명", " 최신 연구"]
    random.shuffle(angles)
    for i, (q, g) in enumerate(pairs):
        q = q.strip()
        if q and q not in seen:
            seen.add(q)
            out.append((q + angles[i % len(angles)], g))
    return out
