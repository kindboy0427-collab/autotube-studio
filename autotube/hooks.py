# -*- coding: utf-8 -*-
"""Opening (first ~30 s) chosen from several candidates (v1.10).

The script writer gives one opening. Here the AI writes 5 more, each with a DIFFERENT opening type,
then a separate fresh AI pass scores all of them (the original included) without knowing which one
was first. Code - not the AI - adds up the scores, and the original is only replaced when another
opening is clearly better. Runs on the Claude Pro / ChatGPT Plus subscription (no extra cost).
"""
from __future__ import annotations
import difflib
import json
import random

from . import db, learner, planner

# Opening types (our own list; ids match the learner's hook:* features where they overlap)
TYPES = {
    "myth_flip": "통념 뒤집기 — 모두가 믿는 것을 말한 직후 '사실은 반대일 수 있습니다'",
    "result_first": "결과부터 — 가장 놀라운 장면·결과를 먼저 보여 주고 '어떻게 이렇게 됐을까요?'",
    "question": "질문 — 시청자가 한 번쯤 품어 본 질문을 그대로 던진다",
    "stakes": "내 일 — 이게 시청자의 몸·돈·일상·미래와 어떻게 연결되는지 먼저",
    "number": "숫자 하나 — 대본에 이미 있는 확실한 숫자 하나로 연다",
    "mini_story": "10초 이야기 — 한 사람·한 사건의 짧은 장면으로 시작",
    "open_loop": "수수께끼 — 영상 끝에서 풀리는 질문을 열어 둔다 ('마지막에 이유가 나옵니다')",
    "contradiction": "모순 — 함께 있을 수 없어 보이는 두 사실을 나란히",
    "thought_test": "머릿속 실험 — '지금 ~을 떠올려 보세요' 시청자가 직접 해 보게",
    "countdown": "시간 — '앞으로 N년', '지금 이 순간에도' 같은 시간 압박(사실일 때만)",
}

SYSTEM = ("너는 한국 과학·교육 유튜브의 도입부 전문 작가다. 첫 30초에 시청자를 붙잡되, 영상이 실제로 지키는 약속만 한다. "
          "확인되지 않은 수치·인용은 지어내지 않는다. 출력은 JSON 하나.")

WRITE_PROMPT = """아래 롱폼 대본의 '첫 {n}개 장면(약 30초 도입부)'을 서로 다른 방식으로 {k}번 다시 써라.

[영상]
제목: {title}
썸네일 글자: {thumb}
시청자가 클릭할 때의 감정: {entry}
챕터 흐름: {chapters}

[지금 도입부 — 이것과 다른 방식으로]
{opening}

[도입부 다음에 바로 이어지는 장면 — 새 도입부의 마지막 문장이 여기로 자연스럽게 넘어가야 함]
{next}

[이 채널에서 성과가 좋았던 도입 방식(학습 결과)]
{learned}

[이 채널 실제 시청 유지 분석]
{retention}

[도입 방식 — 후보마다 아래에서 서로 다른 것 하나씩]
{types}

규칙:
- 후보마다 장면 {n_min}~{n_max}개, 장면당 1~3문장, 전체 {chars_min}~{chars_max}자.
- 첫 문장(5초 안)에 제목·썸네일이 약속한 것을 바로 건드린다. 30초 안에 '끝까지 보면 무엇을 얻는지'가 분명해야 한다.
- 대본 뒷부분에서 실제로 다루는 내용만 약속한다. 새 숫자·사실을 넣었다면 fact_check에 적는다(지어내기 금지).
- visual은 실제 화면으로 구체적으로(source: real|illustration|person|diagram).

JSON:
{{"candidates": [{{"type": "도입 방식 id", "promise": "끝까지 보면 얻는 것 한 줄",
   "fact_check": [],
   "scenes": [{{"chapter": "{chapter0}", "narration": "...", "on_screen_text": "", "importance": 3, "beat": "도입",
               "pause_after": false, "emotion": "curiosity",
               "visual": {{"source": "real", "prompt": "English, concrete", "stock_query": "2-4 English words", "person": ""}},
               "quote": null}}]}}]}}"""

JUDGE_PROMPT = """너는 처음 이 영상을 보는 시청자이자 깐깐한 유튜브 편집자다. 아래 도입부 후보들은 같은 영상의 첫 30초다.
각 후보를 0~10점으로 채점하라 (후보끼리 비교해서 점수를 벌려라).

[영상] 제목: {title} / 썸네일 글자: {thumb}
[도입부 뒤에 이어지는 내용] {next}
[대본 전체에서 다루는 것] {chapters}

채점 항목:
- grab: 첫 5초에 손을 멈추게 하는가
- promise: 30초 안에 끝까지 볼 이유가 분명한가
- match: 제목·썸네일이 약속한 것을 바로 건드리는가
- specific: 막연하지 않고 구체적인가(장면이 그려지는가)
- honest: 영상이 실제로 지키는 약속인가, 과장·단정이 없는가
- flow: 뒤 장면으로 자연스럽게 넘어가는가

[후보]
{cands}

JSON: {{"scores": [{{"id": "A", "grab": 0, "promise": 0, "match": 0, "specific": 0, "honest": 0, "flow": 0, "why": "한 줄"}}]}}"""

WEIGHTS = {"grab": 0.25, "promise": 0.2, "match": 0.15, "specific": 0.15, "honest": 0.15, "flow": 0.1}
MARGIN = 0.5  # another opening must beat the original by this much (out of 10) to replace it


def opening_size(scenes, cps, seconds=28):
    """How many scenes make up the first ~30 seconds (1..4)."""
    t, n = 0.0, 0
    for s in scenes:
        t += len(s.get("narration", "")) / max(cps, 1)
        n += 1
        if t >= seconds or n >= 4:
            break
    return max(1, min(n, len(scenes) - 1))


def _learned_hooks():
    try:
        pb = learner.playbook()
        do = [x["feature"] for x in pb["do"].get("hook", [])] + [x["feature"] for x in pb["do"].get("opens_with", [])]
        avoid = [x["feature"] for x in pb["avoid"].get("hook", [])]
        if not do and not avoid:
            return "(아직 데이터 부족)"
        return json.dumps({"잘된 것": do, "피할 것": avoid}, ensure_ascii=False)
    except Exception:
        return "(아직 데이터 부족)"


def _retention_text():
    try:
        from . import retention
        return retention.prompt_block()
    except Exception:
        return "(아직 없음)"


def _total(sc):
    s = 0.0
    for k, w in WEIGHTS.items():
        try:
            s += w * max(0.0, min(10.0, float(sc.get(k) or 0)))
        except (TypeError, ValueError):
            pass
    return round(s, 2)


def _too_similar_to_past(first_line):
    for r in db.rows("SELECT plan FROM productions WHERE plan IS NOT NULL AND status!='failed' ORDER BY created_at DESC LIMIT 20"):
        try:
            sc = (json.loads(r["plan"]).get("scenes") or [{}])[0].get("narration", "")
        except ValueError:
            continue
        if sc and difflib.SequenceMatcher(None, sc, first_line).ratio() > 0.6:
            return True
    return False


def improve_opening(plan, generate, k=5, cps=8.5, judge=None, rng=None):
    """Write k alternative openings, score them all blind, keep the best. Mutates and returns plan."""
    judge = judge or generate
    rng = rng or random.Random()
    scenes = plan["scenes"]
    n = opening_size(scenes, cps)
    orig = scenes[:n]
    chars = sum(len(s["narration"]) for s in orig)
    chapters = []
    for s in scenes:
        if s.get("chapter") and s["chapter"] not in chapters:
            chapters.append(s["chapter"])
    nxt = " ".join(s["narration"] for s in scenes[n:n + 2])[:400]
    thumb = (plan.get("thumbnail") or {}).get("text", "")
    info = {"opening_scenes": n, "replaced": False, "candidates": []}
    try:
        out = generate(WRITE_PROMPT.format(
            n=n, k=k, title=plan.get("title", ""), thumb=thumb, entry=plan.get("entry_emotion", ""),
            chapters=" / ".join(chapters[:12]), opening=json.dumps([{"narration": s["narration"]} for s in orig], ensure_ascii=False),
            next=nxt, learned=_learned_hooks(), retention=_retention_text(),
            types="\n".join(f"- {t}: {d}" for t, d in TYPES.items()),
            n_min=max(1, n - 1), n_max=n + 1, chars_min=int(chars * 0.8), chars_max=int(chars * 1.25) + 20,
            chapter0=orig[0].get("chapter", "")))
    except Exception as ex:
        info["error"] = "후보 작성 실패: " + str(ex)[:150]
        plan["hook"] = info
        return plan
    cands = [{"type": "original", "scenes": orig, "promise": plan.get("hook_strategy", ""), "fact_check": []}]
    for c in (out.get("candidates") or [])[:k + 2]:
        if not isinstance(c, dict):
            continue
        sc = [s for s in (c.get("scenes") or []) if isinstance(s, dict) and str(s.get("narration", "")).strip()]
        if not sc or len(sc) > n + 2:
            continue
        sc = [planner.normalize_scene(dict(s), plan.get("title", "")) for s in sc]
        total = sum(len(s["narration"]) for s in sc)
        if total < chars * 0.5 or total > chars * 1.8 + 40:
            continue
        if _too_similar_to_past(sc[0]["narration"]):
            continue
        for s in sc:
            s.setdefault("chapter", orig[0].get("chapter", ""))
            s.setdefault("emotion", "curiosity")
        cands.append({"type": str(c.get("type") or "")[:30], "scenes": sc, "promise": str(c.get("promise") or "")[:150],
                      "fact_check": [str(x)[:300] for x in (c.get("fact_check") or [])][:5]})
    if len(cands) < 2:
        info["error"] = "쓸 만한 후보가 없어 원래 도입부 유지"
        plan["hook"] = info
        return plan
    # blind judging: shuffled letters, the judge does not know which one is the original
    order = list(range(len(cands)))
    rng.shuffle(order)
    letters = {}
    blocks = []
    for pos, ci in enumerate(order):
        L = chr(ord("A") + pos)
        letters[L] = ci
        blocks.append(f"[{L}] " + " ".join(s["narration"] for s in cands[ci]["scenes"]))
    try:
        res = judge(JUDGE_PROMPT.format(title=plan.get("title", ""), thumb=thumb, next=nxt,
                                        chapters=" / ".join(chapters[:12]), cands="\n".join(blocks)))
    except Exception as ex:
        info["error"] = "채점 실패: " + str(ex)[:150]
        plan["hook"] = info
        return plan
    for s in res.get("scores") or []:
        ci = letters.get(str(s.get("id", "")).strip().strip("[]").upper()[:1])
        if ci is not None:
            cands[ci]["score"] = _total(s)
            cands[ci]["why"] = str(s.get("why") or "")[:120]
    scored = [c for c in cands if "score" in c]
    if not scored or "score" not in cands[0]:
        info["error"] = "채점 결과를 읽지 못해 원래 도입부 유지"
        plan["hook"] = info
        return plan
    best = max(scored, key=lambda c: c["score"])
    if best is not cands[0] and best["score"] >= cands[0]["score"] + MARGIN:
        plan["scenes"] = best["scenes"] + scenes[n:]
        plan["hook_strategy"] = f"{best['type']}: {TYPES.get(best['type'], '')} — {best['promise']}".strip()
        plan["fact_check"] = list(plan.get("fact_check") or []) + best["fact_check"]
        info["replaced"] = True
    else:
        best = cands[0]
    info["candidates"] = [{"type": c["type"], "first_line": c["scenes"][0]["narration"][:140], "score": c.get("score"),
                           "why": c.get("why", ""), "chosen": c is best} for c in sorted(cands, key=lambda c: -(c.get("score") or 0))]
    plan["hook"] = info
    return plan
