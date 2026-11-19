# -*- coding: utf-8 -*-
"""Where viewers leave - scene by scene (v1.10).

YouTube Analytics gives the audience-retention curve of YOUR public video in 100 points
(elapsedVideoTimeRatio 0.01..1.00 -> audienceWatchRatio). Every finished video saves a
timeline.json (start/end second of each scene + what the scene was: beat, emotion, picture source,
quote, on-screen text, chapter card). Putting the two together tells which KIND of scene loses
viewers on this channel - not just "this video had 41 % average view".

The lessons go back into the next script (planner) and into the opening judge (hooks).
"""
from __future__ import annotations
import json
from collections import defaultdict
from pathlib import Path

from . import db

SHRINK = 3.0      # small samples are pulled toward the channel average
MIN_N = 4         # an attribute value needs this many scenes before it becomes a lesson
INTRO_SECONDS = 30


SCHEMA = """CREATE TABLE IF NOT EXISTS scene_retention(
  production_id TEXT, scene INTEGER, start REAL, end REAL, r_start REAL, r_end REAL,
  drop_per_min REAL, attrs TEXT, measured_at TEXT, PRIMARY KEY(production_id, scene))"""


def _ensure():
    db.execute(SCHEMA)


# ---------------------------------------------------------------- timeline
def scene_attrs(sc, i, scenes, ps=None):
    """What a scene WAS - the things the next script can choose differently."""
    v = sc.get("visual") or {}
    chap = sc.get("chapter") or ""
    first_of_chapter = i == 0 or scenes[i - 1].get("chapter") != chap
    return {"beat": (sc.get("beat") or "")[:40], "emotion": sc.get("emotion") or "",
            "source": v.get("source") or "", "type": v.get("type") or "",
            "importance": int(sc.get("importance") or 1), "quote": bool(sc.get("quote")),
            "on_screen_text": bool(sc.get("on_screen_text")), "pause_after": bool(sc.get("pause_after")),
            "chapter_start": bool(first_of_chapter and chap)}


def save_timeline(out, scenes):
    """Called at render time: exact seconds of every scene in the final video."""
    t, rows = 0.0, []
    for i, sc in enumerate(scenes):
        d = float(sc.get("_dur") or 0)
        rows.append({"i": i, "start": round(t, 3), "end": round(t + d, 3), "chapter": sc.get("chapter") or "",
                     "narration": sc.get("narration", "")[:120], "attrs": scene_attrs(sc, i, scenes)})
        t += d
    Path(out, "timeline.json").write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
    return rows


def load_timeline(output_dir):
    f = Path(output_dir or "", "timeline.json")
    if f.is_file():
        try:
            return json.loads(f.read_text(encoding="utf-8"))
        except ValueError:
            return None
    return None


# ---------------------------------------------------------------- curve -> scenes
def _at(curve, sec, total):
    """Retention (0..~1) at a second, linear between the 100 analytics points."""
    if not curve or total <= 0:
        return None
    x = max(0.0, min(1.0, sec / total))
    pts = sorted(curve)
    if x <= pts[0][0]:
        return pts[0][1]
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        if x0 <= x <= x1:
            return y0 + (y1 - y0) * ((x - x0) / (x1 - x0) if x1 > x0 else 0)
    return pts[-1][1]


def per_scene(curve, timeline):
    """[(scene, start, end, r_start, r_end, drop_per_min)] - drop = share of the viewers still there who left, per minute."""
    if not curve or not timeline:
        return []
    total = timeline[-1]["end"]
    out = []
    for row in timeline:
        a, b = row["start"], row["end"]
        if b - a < 0.5:
            continue
        ra, rb = _at(curve, a, total), _at(curve, b, total)
        if ra is None or rb is None or ra <= 0:
            continue
        drop = max(0.0, (ra - rb) / ra) / ((b - a) / 60)
        out.append((row["i"], a, b, round(ra, 4), round(rb, 4), round(drop, 4)))
    return out


def record(production_id, curve, timeline=None):
    """Store one measurement (latest wins). Returns how many scenes were stored."""
    _ensure()
    if timeline is None:
        p = db.one("SELECT output_dir FROM productions WHERE id=?", (production_id,)) or {}
        timeline = load_timeline(p.get("output_dir"))
    rows = per_scene(curve, timeline or [])
    if not rows:
        return 0
    attrs = {r["i"]: r["attrs"] for r in timeline}
    with db.connect() as con:
        con.execute("DELETE FROM scene_retention WHERE production_id=?", (production_id,))
        con.executemany("INSERT INTO scene_retention VALUES(?,?,?,?,?,?,?,?,?)",
                        [(production_id, i, a, b, ra, rb, d, json.dumps(attrs.get(i, {}), ensure_ascii=False), db.now())
                         for i, a, b, ra, rb, d in rows])
    db.kv_set("intro_retention:" + production_id, _at(curve, INTRO_SECONDS, timeline[-1]["end"]))
    return len(rows)


def measure_all(curve_fn=None):
    """Daily: pull the retention curve of every public upload that has a timeline."""
    from . import youtube_upload
    curve_fn = curve_fn or youtube_upload.retention_curve
    n = 0
    for p in db.rows("SELECT id, youtube_id, channel_id, output_dir FROM productions "
                     "WHERE youtube_id IS NOT NULL AND privacy='public'"):
        tl = load_timeline(p.get("output_dir"))
        if not tl:
            continue
        try:
            curve = curve_fn(p["youtube_id"], p.get("channel_id"))
        except Exception as ex:
            db.event("warn", "retention", f"{p['youtube_id']} 시청 유지 그래프 조회 실패: {str(ex)[:150]}")
            continue
        if curve and record(p["id"], curve, tl):
            n += 1
    return n


# ---------------------------------------------------------------- lessons
def _label(key, val):
    names = {"beat": "비트", "emotion": "감정", "source": "화면", "type": "화면 종류", "importance": "중요도",
             "quote": "인용 카드", "on_screen_text": "화면 글자", "pause_after": "강조 쉼", "chapter_start": "챕터 시작"}
    if isinstance(val, bool):
        return f"{names.get(key, key)} {'있음' if val else '없음'}"
    return f"{names.get(key, key)}={val}"


def lessons(min_n=MIN_N):
    """Which kinds of scenes keep / lose viewers on YOUR channel, relative to your average scene."""
    _ensure()
    rows = db.rows("SELECT production_id, scene, drop_per_min, attrs, start FROM scene_retention")
    vids = {r["production_id"] for r in rows}
    if len(rows) < min_n * 2:
        return {"videos": len(vids), "scenes": len(rows), "worse": [], "better": [], "worst_examples": [],
                "intro_30s": None}
    # the opening always drops fastest: compare scenes after the first 30 s only
    body = [r for r in rows if r["start"] >= INTRO_SECONDS] or rows
    mean = sum(r["drop_per_min"] for r in body) / len(body)
    stats = defaultdict(lambda: [0, 0.0])
    for r in body:
        for k, v in json.loads(r["attrs"] or "{}").items():
            if k == "beat" and not v:
                continue
            stats[(k, v)][0] += 1
            stats[(k, v)][1] += r["drop_per_min"] - mean
    eff = []
    for (k, v), (n, s) in stats.items():
        if n >= min_n:
            eff.append({"what": _label(k, v), "key": k, "value": v, "n": n,
                        "vs_avg": round(s / (n + SHRINK) / mean * 100, 1) if mean > 0 else 0.0})
    eff.sort(key=lambda e: e["vs_avg"])
    better = [e for e in eff if e["vs_avg"] <= -8][:6]
    worse = [e for e in reversed(eff) if e["vs_avg"] >= 8][:6]
    worst = sorted(body, key=lambda r: -r["drop_per_min"])[:5]
    ex = []
    for r in worst:
        p = db.one("SELECT plan FROM productions WHERE id=?", (r["production_id"],)) or {}
        try:
            sc = json.loads(p.get("plan") or "{}").get("scenes", [])[r["scene"]]
            ex.append({"narration": sc.get("narration", "")[:100], "beat": sc.get("beat"),
                       "drop_vs_avg": round((r["drop_per_min"] / mean - 1) * 100) if mean > 0 else None})
        except (IndexError, ValueError):
            pass
    intros = [v for v in (db.kv_get("intro_retention:" + pid) for pid in vids) if v is not None]
    return {"videos": len(vids), "scenes": len(rows), "avg_drop_per_min": round(mean, 4), "worse": worse,
            "better": better, "worst_examples": ex,
            "intro_30s": round(sum(intros) / len(intros), 3) if intros else None}


def prompt_block():
    """Text for the script prompt (empty-safe)."""
    try:
        L = lessons()
    except Exception:
        return "(아직 없음)"
    if not L["worse"] and not L["better"]:
        return f"(아직 데이터 부족 — 공개 영상 {L['videos']}편, 장면 {L['scenes']}개 측정됨)"
    out = [f"공개 영상 {L['videos']}편 · 장면 {L['scenes']}개의 실제 시청 유지 그래프 분석 (평균 장면 대비 이탈 %)"]
    if L.get("intro_30s") is not None:
        out.append(f"- 30초 시점 평균 유지율 {L['intro_30s'] * 100:.0f}%")
    if L["worse"]:
        out.append("- 시청자가 더 많이 나간 장면 유형(줄이거나 고쳐 쓸 것): " +
                   ", ".join(f"{e['what']} (+{e['vs_avg']:.0f}%, {e['n']}장면)" for e in L["worse"]))
    if L["better"]:
        out.append("- 시청자를 붙잡은 장면 유형(더 활용할 것): " +
                   ", ".join(f"{e['what']} ({e['vs_avg']:.0f}%, {e['n']}장면)" for e in L["better"]))
    if L["worst_examples"]:
        out.append("- 가장 많이 나간 실제 문장(이런 식으로 쓰지 말 것): " +
                   " / ".join(f"\"{x['narration']}\"" for x in L["worst_examples"][:3]))
    return "\n".join(out)


def for_production(pid):
    """Scene list with drop vs channel average, for the production page."""
    _ensure()
    rows = db.rows("SELECT scene, start, end, r_start, r_end, drop_per_min FROM scene_retention "
                   "WHERE production_id=? ORDER BY scene", (pid,))
    if not rows:
        return None
    body = [r for r in rows if r["start"] >= INTRO_SECONDS] or rows
    mean = sum(r["drop_per_min"] for r in body) / len(body) or 1e-9
    for r in rows:
        r["vs_avg"] = round((r["drop_per_min"] / mean - 1) * 100)
    worst = sorted(body, key=lambda r: -r["drop_per_min"])[:3]
    return {"scenes": rows, "worst": [r["scene"] for r in worst], "intro_30s": db.kv_get("intro_retention:" + pid)}
