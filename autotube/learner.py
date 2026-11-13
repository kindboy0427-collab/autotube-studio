# -*- coding: utf-8 -*-
"""Self-learning: turn watched-video breakdowns + your own channel results into a playbook.

1. Every watched video gets a performance percentile (outlier score, views/day,
   engagement) within its niche.
2. Each production feature (hook type, retention device, pacing, ...) gets a
   'lift' = how much better videos using it perform than average, shrunk toward 0
   when the sample is small (so 2 lucky videos don't become a rule).
3. Your own uploaded videos feed back with higher weight (retention %, views).
4. Planning samples features by lift + uncertainty (Thompson-style) with
   exploration, and avoids repeating the last video's structure - variety keeps
   the channel from looking mass-produced.
"""
from __future__ import annotations
import json
import math
import random
from collections import defaultdict

from . import config, db

SHRINK = 5.0
OWN_SHRINK = 3.0
OWN_WEIGHT = 2.0
FAV_WEIGHT = 3      # a favourite reference counts like 3 strong videos
FAV_FLOOR = 0.9     # ...each at least at the 90th percentile


def _perf_value(v):
    o = max(v.get("outlier_score") or 0, 1e-4)
    vpd = max(v.get("views_per_day") or 0, 0.1)
    eng = v.get("engagement_rate") or 0
    return 0.6 * math.log10(o) + 0.3 * math.log10(vpd) + 10 * eng


def compute_percentiles():
    rows = db.rows("SELECT v.*, a.video_id aid FROM videos v JOIN analyses a ON a.video_id=v.id")
    groups = defaultdict(list)
    for r in rows:
        groups[r["niche"] or ""].append(r)
    big = [r for g in groups.values() if len(g) >= 10 for r in g]
    small = [r for g in groups.values() if len(g) < 10 for r in g]
    updates = []
    for bucket in [g for g in groups.values() if len(g) >= 10] + ([small] if small else []):
        scored = sorted(bucket, key=_perf_value)
        for i, r in enumerate(scored):
            updates.append(((i + 0.5) / len(scored), r["id"]))
    with db.connect() as con:
        con.executemany("UPDATE analyses SET perf_percentile=? WHERE video_id=?", updates)
    return len(updates), len(big)


def update_patterns():
    compute_percentiles()
    data = db.rows("SELECT a.features, a.perf_percentile p, v.title, COALESCE(v.favorite,0) fav FROM analyses a "
                   "JOIN videos v ON v.id=a.video_id WHERE a.perf_percentile IS NOT NULL")
    if not data:
        return 0
    expanded = []
    for d in data:  # taste references weigh more: the owner's judgement is a strong signal
        if d["fav"]:
            expanded += [dict(d, p=max(d["p"], FAV_FLOOR))] * FAV_WEIGHT
        else:
            expanded.append(d)
    data = expanded
    g = sum(d["p"] for d in data) / len(data)
    stats = defaultdict(lambda: {"n": 0, "sum": 0.0, "examples": []})
    for d in data:
        for f in json.loads(d["features"]):
            st = stats[f]
            st["n"] += 1
            st["sum"] += d["p"]
            if d["p"] >= 0.7 and len(st["examples"]) < 3 and d["title"][:80] not in st["examples"]:
                st["examples"].append(d["title"][:80])
    own = own_feature_stats()
    with db.connect() as con:
        for f, st in stats.items():
            lift = (st["sum"] - st["n"] * g) / (st["n"] + SHRINK)
            o = own.get(f, {"n": 0, "lift": 0.0})
            con.execute("""INSERT INTO patterns(feature,category,n,lift,mean_perf,own_n,own_lift,examples,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(feature) DO UPDATE SET n=excluded.n, lift=excluded.lift,
                mean_perf=excluded.mean_perf, own_n=excluded.own_n, own_lift=excluded.own_lift,
                examples=excluded.examples, updated_at=excluded.updated_at""",
                        (f, f.split(":")[0], st["n"], round(lift, 4), round(st["sum"] / st["n"], 4), o["n"],
                         round(o["lift"], 4), json.dumps(st["examples"], ensure_ascii=False), db.now()))
    return len(stats)


def own_feature_stats():
    """Lift of features in YOUR published videos (latest measurement per production)."""
    rows = db.rows("""SELECT p.id, p.features_used, perf.avg_view_percentage avp, perf.views FROM productions p
        JOIN performance perf ON perf.id=(SELECT id FROM performance WHERE production_id=p.id ORDER BY id DESC LIMIT 1)
        WHERE p.features_used IS NOT NULL""")
    if len(rows) < 2:
        return {}
    metric = [(r["avp"] if r["avp"] is not None else math.log10(max(r["views"] or 1, 1)) * 10, r) for r in rows]
    ranked = sorted(metric, key=lambda x: x[0])
    pct = {r["id"]: (i + 0.5) / len(ranked) for i, (_, r) in enumerate(ranked)}
    stats = defaultdict(lambda: [0, 0.0])
    for _, r in metric:
        for f in json.loads(r["features_used"] or "[]"):
            stats[f][0] += 1
            stats[f][1] += pct[r["id"]] - 0.5
    return {f: {"n": n, "lift": s / (n + OWN_SHRINK)} for f, (n, s) in stats.items()}


def score(p):
    return (p["lift"] or 0) + OWN_WEIGHT * (p.get("own_lift") or 0)


def playbook(top_per_cat=4):
    pats = db.rows("SELECT * FROM patterns WHERE n>=3")
    by = defaultdict(list)
    for p in pats:
        by[p["category"]].append(p)
    do, avoid = {}, {}
    for cat, items in by.items():
        items.sort(key=score, reverse=True)
        do[cat] = [{"feature": i["feature"], "lift": round(score(i), 3), "n": i["n"], "own_n": i["own_n"],
                    "examples": json.loads(i["examples"] or "[]")} for i in items[:top_per_cat] if score(i) > 0]
        avoid[cat] = [{"feature": i["feature"], "lift": round(score(i), 3), "n": i["n"]}
                      for i in items[-2:] if score(i) < -0.03]
    watched = db.one("SELECT COUNT(*) c FROM analyses")["c"]
    own = db.one("SELECT COUNT(DISTINCT production_id) c FROM performance")["c"]
    latest = db.one("SELECT content, created_at FROM playbook ORDER BY id DESC LIMIT 1")
    return {"watched_videos": watched, "own_videos_measured": own, "do": do, "avoid": avoid,
            "narrative": (latest or {}).get("content"), "narrative_at": (latest or {}).get("created_at")}


def relevant_examples(topic, k=6, channel_id=None):
    words = [w for w in topic.replace('"', " ").split() if len(w) >= 2][:6]
    found = db.rows("""SELECT a.*, v.title, v.views, v.views_per_day, v.duration_seconds, 1 fav FROM analyses a
        JOIN videos v ON v.id=a.video_id WHERE v.favorite=1 ORDER BY RANDOM() LIMIT 2""")  # owner's taste references
    if channel_id:  # then: best analysed videos from this channel's own field
        found += db.rows("""SELECT a.*, v.title, v.views, v.views_per_day, v.duration_seconds FROM analyses a
            JOIN videos v ON v.id=a.video_id WHERE v.niche=? ORDER BY a.perf_percentile DESC LIMIT ?""", (channel_id, max(1, k // 2)))
    if words:
        q = " OR ".join('"%s"' % w for w in words)
        try:
            found += db.rows("""SELECT a.*, v.title, v.views, v.views_per_day, v.duration_seconds FROM analyses_fts f
                JOIN analyses a ON a.video_id=f.video_id JOIN videos v ON v.id=a.video_id
                WHERE analyses_fts MATCH ? ORDER BY a.perf_percentile DESC LIMIT ?""", (q, k))
        except Exception:
            pass
    uniq = {}
    for f in found:
        uniq.setdefault(f["video_id"], f)
    found = list(uniq.values())[:k]
    if len(found) < k:
        seen = [f["video_id"] for f in found] or [""]
        found += db.rows("""SELECT a.*, v.title, v.views, v.views_per_day, v.duration_seconds FROM analyses a
            JOIN videos v ON v.id=a.video_id WHERE a.video_id NOT IN (%s) ORDER BY a.perf_percentile DESC LIMIT ?"""
                         % ",".join("?" * len(seen)), seen + [k - len(found)])
    out = []
    for r in found:
        a = json.loads(r["raw"])
        out.append({"taste_reference": bool(r.get("fav")), "title": r["title"], "views": r["views"], "minutes": round((r["duration_seconds"] or 0) / 60, 1),
                    "performance_percentile": r["perf_percentile"], "hook_first_15s": (a.get("hook") or {}).get("first_15s"),
                    "structure": [s.get("label") for s in (a.get("structure") or [])][:12],
                    "why_it_works": (a.get("why_it_works") or [])[:4], "reusable_ideas": (a.get("reusable_ideas") or [])[:4],
                    "viewer_comments": [c["text"] for c in json.loads(r["top_comments"] or "[]")[:4]]})
    return out


CATEGORY_PICKS = {"hook": 1, "retention": 5, "engagement": 2, "narration": 1, "emotion": 3, "title": 1,
                  "pace": 1, "shots": 1, "cta": 1, "opens_with": 1}


def choose_features(exploration=0.2, rng=None):
    rng = rng or random.Random()
    pats = db.rows("SELECT * FROM patterns")
    last = db.one("SELECT features_used FROM productions WHERE features_used IS NOT NULL ORDER BY created_at DESC LIMIT 1")
    last_feats = set(json.loads(last["features_used"])) if last else set()
    by = defaultdict(list)
    for p in pats:
        by[p["category"]].append(p)
    chosen, explored = [], []
    for cat, k in CATEGORY_PICKS.items():
        items = by.get(cat, [])
        if not items:
            continue
        sampled = []
        for p in items:
            sd = 0.25 / math.sqrt(p["n"] + p["own_n"] + 1)
            val = rng.gauss(score(p), sd)
            if cat == "hook" and p["feature"] in last_feats:
                val -= 0.2  # do not reuse the previous video's hook type
            sampled.append((val, p["feature"], p["n"]))
        sampled.sort(reverse=True)
        picks = [f for _, f, _ in sampled[:k]]
        if rng.random() < exploration and len(sampled) > k:
            least = min(sampled[k:], key=lambda x: x[2])[1]  # least-tested option
            picks[-1] = least
            explored.append(least)
        chosen += picks
    return chosen, explored


def synthesize_narrative(generate=None):
    """Weekly: LLM turns the numbers + top breakdowns into a written playbook."""
    from . import llm
    generate = generate or (lambda prompt: llm.generate(prompt, "유튜브 성장 전략가", json_mode=False, max_tokens=6000))
    pb = playbook()
    if pb["watched_videos"] < 10:
        return None
    ex = relevant_examples("", k=12)
    prompt = ("아래는 AI가 실제로 시청·분석한 유튜브 롱폼 {n}편에서 계산한 '성과를 올린 특징(lift)'과 상위 영상 분석이다. "
              "숫자는 상관관계이며 인과가 아니다. 이를 바탕으로 롱폼 제작 플레이북을 한국어로 작성하라: "
              "1) 첫 30초 공식 3가지 2) 이탈 방지 장치 사용법 3) 참여(댓글) 유도법 4) 제목·구성 원칙 5) 피해야 할 것. "
              "각 항목에 근거 특징과 표본 수를 괄호로 적어라.\n특징 통계: {stats}\n상위 영상: {ex}").format(
        n=pb["watched_videos"], stats=json.dumps({"do": pb["do"], "avoid": pb["avoid"]}, ensure_ascii=False)[:9000],
        ex=json.dumps(ex, ensure_ascii=False)[:12000])
    text = generate(prompt)
    with db.connect() as con:
        con.execute("INSERT INTO playbook(created_at,model,content,basis) VALUES(?,?,?,?)",
                    (db.now(), "llm", text[:20000], json.dumps({"watched": pb["watched_videos"]})))
    return text


def record_own_performance(stats_fn=None, analytics_fn=None):
    """Daily: measure your uploaded videos and feed results back into learning."""
    from . import youtube_upload
    stats_fn = stats_fn or youtube_upload.video_stats
    analytics_fn = analytics_fn or youtube_upload.analytics
    prods = db.rows("SELECT * FROM productions WHERE youtube_id IS NOT NULL")
    if not prods:
        return 0
    stats = {}
    for cid in {p.get("channel_id") for p in prods}:  # each channel has its own Google login
        ids = [p["youtube_id"] for p in prods if p.get("channel_id") == cid]
        try:
            stats.update(stats_fn(ids, cid) if cid else stats_fn(ids))
        except Exception as ex:
            db.event("warn", "performance", f"채널 {cid or '기본'} 성과 조회 실패: {str(ex)[:150]}")
    n = 0
    for p in prods:
        st = stats.get(p["youtube_id"])
        if not st:
            continue
        an = None
        if st.get("privacy") == "public":
            try:
                an = analytics_fn(p["youtube_id"], st.get("published"), p.get("channel_id")) if p.get("channel_id") \
                    else analytics_fn(p["youtube_id"], st.get("published"))
            except Exception:
                an = None
        with db.connect() as con:
            con.execute("""INSERT INTO performance(production_id,youtube_id,measured_at,views,likes,comments,
                avg_view_percentage,avg_view_duration,source) VALUES(?,?,?,?,?,?,?,?,?)""",
                        (p["id"], p["youtube_id"], db.now(), st["views"], st["likes"], st["comments"],
                         (an or {}).get("avg_view_percentage"), (an or {}).get("avg_view_duration"),
                         "analytics" if an else "public_stats"))
            con.execute("UPDATE productions SET privacy=? WHERE id=?", (st.get("privacy"), p["id"]))
        n += 1
    update_patterns()
    return n



MIX_KEYS = ("real_footage", "archival", "interview_clip", "motion_graphics", "stock", "ai_generated", "talking_head", "photo")


def visual_mix_target(min_pct=0.7, min_n=3):
    """Average screen-time mix of the best-performing analysed videos (watched with picture), + common shot ideas."""
    rows = db.rows("""SELECT a.raw, a.perf_percentile p, COALESCE(v.favorite,0) fav FROM analyses a JOIN videos v ON v.id=a.video_id
        WHERE (a.perf_percentile >= ? OR v.favorite=1) AND a.model != 'captions+llm'""", (min_pct,))
    mixes, ideas = [], []
    for r in rows:
        a = json.loads(r["raw"])
        m = a.get("visual_mix")
        if isinstance(m, dict):
            vals = {k: float(m.get(k) or 0) for k in MIX_KEYS}
            tot = sum(vals.values())
            if tot > 0:
                w = 3 if r["fav"] else 1
                mixes += [{k: v * 100 / tot for k, v in vals.items()}] * w
        ideas += [str(x)[:80] for x in (a.get("shot_ideas") or [])[:6]]
    if len(mixes) < min_n:
        return None
    avg = {k: round(sum(m[k] for m in mixes) / len(mixes)) for k in MIX_KEYS}
    return {"mix": avg, "n": len(mixes), "shot_ideas": ideas[:15]}
