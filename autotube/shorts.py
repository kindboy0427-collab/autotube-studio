# -*- coding: utf-8 -*-
"""Long-form -> 2-3 Shorts that send viewers to the full video (v1.10).

Nothing new is generated: the Shorts are cut from the finished picture track and the mixed audio
(same voice, footage, music), turned 9:16, with a top caption and subtitles. The AI (subscription)
picks which stretch of consecutive scenes works on its own; code keeps the length inside the limits.
Uploaded private with a link to the full video; you publish them yourself.
"""
from __future__ import annotations
import json
import re
from pathlib import Path

from . import config, db, media

SYSTEM = "너는 유튜브 쇼츠 편집자다. 롱폼에서 혼자서도 이해되고 끝까지 보게 되는 구간을 고른다. 출력은 JSON 하나."

PROMPT = """아래는 완성된 롱폼 영상의 장면 목록이다 (번호 · 초 · 챕터 · 내레이션).
이 중에서 쇼츠로 잘라 올릴 구간 {n}개를 골라라.

[영상 제목] {title}

[장면]
{scenes}

규칙:
- 구간 = 연속된 장면 묶음. 길이 {min_s}~{max_s}초 (장면 초를 더해서 확인).
- 앞 내용을 몰라도 이해되는 구간. 첫 장면이 질문·반전·놀라운 사실로 바로 시작하는 곳.
- 끝은 '한 가지를 확실히 알게 된' 지점이거나, 전체 영상이 궁금해지는 지점.
- 구간끼리 겹치지 않게, 서로 다른 내용으로.
- title: 쇼츠 제목 40자 이내(해시태그 없이). top_text: 화면 위에 계속 떠 있을 문구 14자 이내(질문형 추천).

JSON: {{"shorts": [{{"from": 장면번호, "to": 장면번호, "title": "...", "top_text": "...", "why": "한 줄"}}]}}"""


def _dir(out):
    return Path(out) / "shorts"


def load(out):
    f = _dir(out) / "shorts.json"
    if f.is_file():
        try:
            return json.loads(f.read_text(encoding="utf-8"))
        except ValueError:
            return []
    return []


def save(out, items):
    _dir(out).mkdir(parents=True, exist_ok=True)
    (_dir(out) / "shorts.json").write_text(json.dumps(items, ensure_ascii=False, indent=1), encoding="utf-8")


def _fit(a, b, durs, min_s, max_s):
    """Grow/shrink the scene range [a, b] until its length is inside the limits (None if impossible)."""
    n = len(durs)
    a, b = max(0, min(a, n - 1)), max(0, min(b, n - 1))
    if b < a:
        a, b = b, a
    length = lambda: sum(durs[a:b + 1])
    while length() > max_s and b > a:
        b -= 1
    while length() < min_s and b < n - 1 and length() + durs[b + 1] <= max_s:
        b += 1
    while length() < min_s and a > 0 and length() + durs[a - 1] <= max_s:
        a -= 1
    return (a, b) if min_s <= length() <= max_s else None


def heuristic(scenes, durs, n, min_s, max_s):
    """No AI available: start at the strongest scenes (hook / twist / chapter start), keep them apart."""
    starts = sorted(range(len(scenes)), key=lambda i: (-(scenes[i].get("importance") or 1),
                                                       0 if i == 0 or scenes[i].get("chapter") != scenes[i - 1].get("chapter") else 1, i))
    picked = []
    for s in starts:
        r = _fit(s, s, durs, min_s, max_s)
        if r and all(r[1] < p[0] or r[0] > p[1] for p in picked):
            picked.append(r)
        if len(picked) >= n:
            break
    out = []
    for a, b in sorted(picked):
        chap = scenes[a].get("chapter") or ""
        out.append({"from": a, "to": b, "title": "", "top_text": (scenes[a].get("on_screen_text") or chap)[:14], "why": "중요 장면에서 시작"})
    return out


def pick(plan, scenes, durs, n, min_s, max_s, generate=None):
    """[{from, to, title, top_text, why}] with 0-based scene indexes, lengths checked, no overlaps."""
    raw = []
    if generate:
        lines = "\n".join(f"{i + 1} · {durs[i]:.1f}초 · {s.get('chapter', '')} · {s.get('narration', '')}"
                          for i, s in enumerate(scenes))
        try:
            out = generate(PROMPT.format(n=n, title=plan.get("title", ""), scenes=lines[:30000], min_s=int(min_s), max_s=int(max_s)))
            for x in out.get("shorts") or []:
                try:
                    raw.append({"from": int(x["from"]) - 1, "to": int(x["to"]) - 1, "title": str(x.get("title") or "")[:60],
                                "top_text": str(x.get("top_text") or "")[:20], "why": str(x.get("why") or "")[:100]})
                except (KeyError, TypeError, ValueError):
                    continue
        except Exception:
            raw = []
    if not raw:
        raw = heuristic(scenes, durs, n, min_s, max_s)
    picked = []
    for x in raw:
        r = _fit(x["from"], x["to"], durs, min_s, max_s)
        if not r or any(not (r[1] < p["from"] or r[0] > p["to"]) for p in picked):
            continue
        picked.append(dict(x, **{"from": r[0], "to": r[1]}))
        if len(picked) >= n:
            break
    return picked


def _title(x, plan):
    t = (x.get("title") or "").strip() or (plan.get("title") or "")
    t = re.sub(r"#\S+", "", t).strip()
    return (t[:88] + " #Shorts").strip()


def make(pid, plan, scenes, out, work, generate=None, settings=None, log=None):
    """Render the Shorts of one finished production. Returns the list saved to shorts/shorts.json."""
    ps = settings or config.load_settings()["production"]
    n = int(ps.get("shorts_per_video", 3) or 0)
    if n <= 0:
        return []
    existing = load(out)
    if existing and all((_dir(out) / x["file"]).is_file() for x in existing):
        return existing
    durs = [float(s.get("_dur") or 0) for s in scenes]
    min_s, max_s = float(ps.get("shorts_min_seconds", 20)), float(ps.get("shorts_max_seconds", 58))
    if sum(durs) < min_s:
        return []
    audio = Path(work) / "mixed.wav"
    if not audio.is_file():
        audio = Path(work) / "narration.wav"
    video = Path(work) / "video_only.mp4"
    if not video.is_file() or not audio.is_file():
        return []
    picks = pick(plan, scenes, durs, n, min_s, max_s, generate)
    d = _dir(out)
    d.mkdir(parents=True, exist_ok=True)
    W, H = (int(v) for v in str(ps.get("shorts_resolution", "1080x1920")).split("x"))
    starts = [sum(durs[:i]) for i in range(len(durs))]
    items = []
    for k, x in enumerate(picks, 1):
        a, b = x["from"], x["to"]
        start, length = starts[a], sum(durs[a:b + 1])
        offsets = [(starts[i] - start, scenes[i].get("_voice", {}).get("words") or
                    media.proportional_words(scenes[i].get("narration", ""), durs[i])) for i in range(a, b + 1)]
        srt = d / f"short{k}.srt"
        media.build_srt(offsets, str(srt), max_chars=34, max_seconds=6.0)
        f = d / f"short{k}.mp4"
        _, burned = media.vertical_short(str(video), str(audio), start, length, str(f), srt=str(srt),
                                         top_text=x.get("top_text") or "", size=(W, H), cwd=str(d))
        items.append({"n": k, "file": f.name, "title": _title(x, plan), "top_text": x.get("top_text") or "",
                      "why": x.get("why") or "", "from_scene": a + 1, "to_scene": b + 1, "start": round(start, 2),
                      "seconds": round(length, 1), "subtitles": burned, "youtube_id": None, "privacy": None})
        if log:
            log(f"쇼츠 {k}: 장면 {a + 1}~{b + 1} ({length:.0f}초) — {items[-1]['title']}")
    save(out, items)
    return items


def description(out, long_id):
    """Link to the full video + the long-form credits (chapter timestamps removed - they mean nothing in a Short)."""
    desc = ""
    f = Path(out) / "description.txt"
    if f.is_file():
        desc = "\n".join(l for l in f.read_text(encoding="utf-8").splitlines() if not re.match(r"^\d{1,2}:\d{2}(:\d{2})? ", l))
    head = f"▶ 전체 영상: https://youtu.be/{long_id}\n\n" if long_id else ""
    return (head + re.sub(r"\n{3,}", "\n\n", desc)).strip()


def upload(pid, out, plan, long_id, channel_id=None, uploader=None, only=None):
    """Private upload of the Shorts that are not on YouTube yet. Returns notes."""
    if uploader is None:
        from . import youtube_upload
        uploader = youtube_upload.upload
    items = load(out)
    notes = []
    for x in items:
        if x.get("youtube_id") or (only and x["n"] != only):
            continue
        f = _dir(out) / x["file"]
        if not f.is_file():
            continue
        try:
            vid, n2 = uploader(str(f), x["title"], description(out, long_id), list(plan.get("tags", []))[:28] + ["Shorts"],
                               privacy="private", channel_id=channel_id)
            x["youtube_id"], x["privacy"] = vid, "private"
            notes.append(f"쇼츠 {x['n']} 비공개 업로드: https://youtu.be/{vid}")
        except Exception as ex:
            notes.append(f"쇼츠 {x['n']} 업로드 실패: {str(ex)[:120]}")
            break  # usually quota / login: the rest would fail the same way
    save(out, items)
    return notes


def set_privacy(out, n, privacy, channel_id=None, setter=None):
    if setter is None:
        from . import youtube_upload
        setter = youtube_upload.set_privacy
    items = load(out)
    done = []
    for x in items:
        if x.get("youtube_id") and (n is None or x["n"] == n):
            setter(x["youtube_id"], privacy, channel_id=channel_id)
            x["privacy"] = privacy
            done.append(x["n"])
    save(out, items)
    return done
