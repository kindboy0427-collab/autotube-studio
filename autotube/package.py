# -*- coding: utf-8 -*-
"""Title + thumbnail checked as ONE package (v1.10).

On the home feed a viewer sees the thumbnail and the title together for about a second. The two must
not say the same thing twice: the thumbnail gives the feeling / the one striking word, the title gives
what the video is about. Code checks the hard rules (length, repetition, shouting); a fresh AI pass
writes thumbnail-text options and scores every title x thumbnail pair.
"""
from __future__ import annotations
import difflib
import json
import re

TITLE_FRONT = 25        # the core words should sit in the first ~25 characters (mobile cuts long titles)
TITLE_MAX = 45
THUMB_MAX = 12

SYSTEM = ("너는 유튜브 홈 화면 패키징(제목+썸네일) 전문가다. 클릭을 부르지만 영상이 실제로 지키는 약속만 한다. "
          "출력은 JSON 하나.")

PROMPT = """아래 롱폼 영상의 제목·썸네일 글자 조합을 정하라.

[영상]
주제: {topic}
제목 후보: {titles}
지금 썸네일 글자: {thumb}
시청자가 클릭할 때의 감정: {entry}
도입부(첫 30초): {opening}
챕터: {chapters}
썸네일 배경 그림: {image}

[원칙]
- 썸네일 글자와 제목은 서로 다른 정보를 준다. 썸네일 = 감정·충격·한 단어(12자 이내, 2~4단어), 제목 = 무슨 영상인지.
- 제목의 핵심 단어는 앞 {front}자 안에. 전체 {tmax}자 이내.
- 영상이 실제로 답하지 않는 약속(낚시) 금지. 도입부가 그 약속을 30초 안에 건드려야 한다.
- 과학 채널이다: 과장·단정은 피하고 궁금증으로 끈다.

[할 일]
1) 썸네일 글자 후보 3개를 새로 쓴다(지금 것과 다르게, 서로 다른 각도).
2) 제목 후보 전체 × 썸네일 글자 전체(지금 것 포함) 조합마다 0~10점으로 채점한다:
   curiosity(궁금해서 누르고 싶은가), clarity(1초 만에 무슨 영상인지), complement(둘이 겹치지 않고 보완하는가),
   honesty(영상이 실제로 약속을 지키는가), mobile(휴대폰에서 읽히는가).

JSON:
{{"thumb_options": ["...", "...", "..."],
 "pairs": [{{"title": "...", "thumb": "...", "curiosity": 0, "clarity": 0, "complement": 0, "honesty": 0, "mobile": 0,
            "note": "한 줄 평"}}]}}"""

WEIGHTS = {"curiosity": 0.3, "clarity": 0.2, "complement": 0.2, "honesty": 0.2, "mobile": 0.1}


def _norm(t):
    return re.sub(r"[\s\W_]+", "", t or "").lower()


def lint(title, thumb):
    """Hard rules. Returns a list of Korean problem notes (empty = fine)."""
    issues = []
    title, thumb = (title or "").strip(), (thumb or "").strip()
    if len(title) > TITLE_MAX:
        issues.append(f"제목 {len(title)}자 — 휴대폰에서 잘림 ({TITLE_MAX}자 이내 권장)")
    if thumb and len(thumb) > THUMB_MAX:
        issues.append(f"썸네일 글자 {len(thumb)}자 — {THUMB_MAX}자 이내라야 작은 화면에서 읽힘")
    if thumb and len(thumb.split()) > 4:
        issues.append("썸네일 글자 단어가 너무 많음 (4단어 이하)")
    nt, nh = _norm(title), _norm(thumb)
    if nh and len(nh) >= 2:
        words = [_norm(w) for w in thumb.split() if len(_norm(w)) >= 2]
        repeated = words and all(w in nt for w in words)
        if repeated or nh in nt or difflib.SequenceMatcher(None, nh, nt[:len(nh) + 4]).ratio() > 0.7:
            issues.append("썸네일 글자가 제목을 그대로 반복 — 서로 다른 정보를 줘야 함")
    if re.search(r"[!?]{3,}|ㄷㄷ|ㅋㅋ", title + " " + thumb):
        issues.append("느낌표·물음표 남발/초성 — 과학 채널 신뢰도 하락")
    if re.search(r"(충격|경악|소름|역대급)", title) and re.search(r"(충격|경악|소름|역대급)", thumb):
        issues.append("제목과 썸네일 모두 자극어 — 하나만 쓸 것")
    return issues


def _total(p):
    s = 0.0
    for k, w in WEIGHTS.items():
        try:
            s += w * max(0.0, min(10.0, float(p.get(k) or 0)))
        except (TypeError, ValueError):
            pass
    return round(s, 2)


def check(plan, topic="", generate=None):
    """Pick the best title x thumbnail-text pair; keep the rest as A/B candidates. Mutates and returns plan."""
    titles = [str(t).strip()[:100] for t in (plan.get("titles") or []) if str(t).strip()]
    if plan.get("title") and plan["title"] not in titles:
        titles.insert(0, plan["title"])
    thumb = (plan.get("thumbnail") or {}).get("text") or ""
    info = {"before": {"title": plan.get("title"), "thumb": thumb, "issues": lint(plan.get("title"), thumb)}}
    pairs = []
    if generate:
        opening = " ".join(s.get("narration", "") for s in plan.get("scenes", [])[:4])[:600]
        chapters = []
        for s in plan.get("scenes", []):
            if s.get("chapter") and s["chapter"] not in chapters:
                chapters.append(s["chapter"])
        try:
            out = generate(PROMPT.format(topic=topic, titles=json.dumps(titles, ensure_ascii=False),
                                         thumb=thumb, entry=plan.get("entry_emotion", ""), opening=opening,
                                         chapters=" / ".join(chapters[:12]), front=TITLE_FRONT, tmax=TITLE_MAX,
                                         image=(plan.get("thumbnail") or {}).get("image_prompt", "")[:200]))
            opts = [str(x).strip()[:THUMB_MAX + 4] for x in (out.get("thumb_options") or []) if str(x).strip()]
            info["thumb_options"] = opts[:3]
            for p in out.get("pairs") or []:
                if not isinstance(p, dict) or not p.get("title") or not p.get("thumb"):
                    continue
                t, h = str(p["title"]).strip()[:100], str(p["thumb"]).strip()
                issues = lint(t, h)
                score = _total(p) - 1.5 * len(issues)  # hard-rule breaks cost more than any taste point
                pairs.append({"title": t, "thumb": h, "score": round(score, 2), "issues": issues,
                              "note": str(p.get("note") or "")[:120]})
        except Exception as ex:
            info["error"] = str(ex)[:200]
    if not pairs:  # no AI: rules only - keep the plan's pair unless it breaks a rule another option does not
        cands = [(t, thumb) for t in titles] or [(plan.get("title", ""), thumb)]
        pairs = [{"title": t, "thumb": h, "score": -len(lint(t, h)), "issues": lint(t, h), "note": ""} for t, h in cands]
    pairs.sort(key=lambda p: -p["score"])
    best = pairs[0]
    plan["title"] = best["title"]
    plan.setdefault("thumbnail", {})["text"] = best["thumb"][:THUMB_MAX + 4]
    if best["title"] not in titles:
        titles.insert(0, best["title"])
    plan["titles"] = titles[:5]
    alts = []
    for p in pairs[1:]:  # other thumbnail words for the YouTube Test & Compare candidates
        if p["thumb"] != best["thumb"] and p["thumb"] not in alts and not p["issues"]:
            alts.append(p["thumb"])
    for o in info.get("thumb_options", []):
        if o != best["thumb"] and o not in alts and not lint(best["title"], o):
            alts.append(o)
    plan["thumbnail"]["alt_texts"] = alts[:3]
    info.update({"chosen": {"title": best["title"], "thumb": best["thumb"], "score": best["score"], "note": best["note"]},
                 "pairs": pairs[:8], "issues": best["issues"]})
    plan["package"] = info
    return plan
