# -*- coding: utf-8 -*-
"""Turn a topic/command into a full long-form plan using what the learner knows."""
from __future__ import annotations
import difflib
import json

from . import config, db, learner, llm

SYSTEM = ("너는 한국 유튜브 롱폼 전문 작가 겸 연출가다. 시청 유지율과 댓글 참여를 높이는 구조를 설계한다. "
          "사실이 불확실한 내용은 단정하지 말고, 확인되지 않은 수치·인용을 지어내지 않는다. "
          "다른 영상의 대사·구성을 베끼지 말고 원리만 응용한다. 출력은 JSON 하나.")

PLAN_PROMPT = """[주제/명령]
{topic}
{instructions}

[채널]
{channel}

[목표]
- 길이 약 {minutes}분 롱폼 (내레이션 총 {chars_min}~{chars_max}자, 한국어)
- 장면 {scenes_min}~{scenes_max}개, 장면당 내레이션 1~3문장(약 6~10초)
- 첫 30초 안에 시청자가 끝까지 볼 이유(약속)를 분명히 하고, 중간중간 다음 내용을 궁금하게 만드는 장치를 넣는다.
- 감정 설계: 시청자가 어떤 감정(궁금함·불안·경이로움 등)으로 클릭해 들어오는지 정하고, 그 감정으로 시작해
  마지막에는 이해했다는 만족감·희망 같은 좋은 감정으로 나가게 만든다. 겁만 주고 끝내지 않는다.
- 과학 내용은 정확해야 한다. 학계에서 확립된 사실과 가설·논쟁 중인 내용을 말로 구분하고,
  확인이 필요한 수치·연구 결과는 fact_check 목록에 적는다. 출처를 지어내지 않는다.

[AI가 학습한 성과 패턴 — 이번 영상에 반드시 반영할 특징]
{features}
(각 특징 설명: hook=도입 방식, retention=이탈 방지 장치, engagement=댓글 유도, emotion=감정 흐름, title=제목 유형, pace/shots=속도·컷 길이, cta=구독 요청 시점)

[학습된 플레이북 요약]
{playbook}

[비슷한 주제에서 성과가 좋았던 영상 분석(원리만 참고, 복제 금지)]
{examples}

[성과 좋은 영상들의 화면 구성 (학습 결과) — visual.source 배분을 이 비율에 가깝게]
{visual_mix}

[대본 틀 — 주인이 좋아하는 레퍼런스 영상들의 공통 구조]
{format_block}
화면 원칙(실제 다큐처럼): 실제로 존재하는 것(공정·장비·장소·실험·제품·자연현상)은 visual.source="real"로 하고 stock_query를
구체적으로(예: "silicon ingot growth", "wafer fab cleanroom") — 실제 자료 영상·사진을 찾아 쓴다. 상상·개념·미래·비유 장면만 "illustration".
원리 설명은 "diagram"(단순한 도식 이미지 + 화면 글자). 과학자·인물의 실제 발언을 인용하면 source="person", person=영문 이름,
quote에 발언(확실한 것만, 지어내기 금지 — fact_check에도 적기). 인용은 영상 전체에서 1~3개면 충분.
**실제 자료가 핵심이다: real + person 장면이 전체 장면의 {min_real}% 이상이어야 한다.** AI 일러스트는 실제로 찍을 수 없는 장면(원자 내부, 미래, 비유)에만.
공통 원칙: 30초 안에 통념을 뒤집거나 큰 질문을 던진다 / 시청자가 지금 어디쯤인지 알 수 있는 뼈대(사례 번호·단계·챕터 질문) /
막연한 설명 대신 구체적 숫자·날짜·이름(확인 필요하면 fact_check에) / 중반에 반례·반대 의견으로 긴장을 다시 만든다 /
희망 → 요약 → 열린 질문으로 끝내고 채널 고정 끝인사 / 짧은 문장, 핵심 문장 뒤에는 쉼(pause_after) /
사례·챕터가 바뀔 때 앞뒤를 잇는 연결 문장.

[통째로 학습한 참고 채널들의 분석 — 원리만 응용, 제목·대사 복제 금지]
{studies}

[이 채널의 제작 규칙(스타일 가이드) — 반드시 지킬 것]
{style_guide}

[내 채널 최근 영상 — 구성·제목이 겹치지 않게]
{recent}

아래 JSON으로만 답하라:
{{
 "titles": ["제목 후보 3개 (40자 이내, 서로 다른 각도)"],
 "title": "최종 추천 제목",
 "thumbnail": {{"text": "썸네일 큰 글자 12자 이내", "image_prompt": "English prompt for a 16:9 thumbnail background, cinematic, no text"}},
 "description": "영상 설명 (요약 3줄 + 챕터 안내 문구, 해시태그 3개)",
 "tags": ["태그 10~15개"],
 "music_mood": "English short description of background music mood",
 "hook_strategy": "첫 30초 설계 의도",
 "entry_emotion": "시청자가 클릭할 때의 감정", "exit_emotion": "다 보고 나갈 때 남길 감정",
 "format": "사용한 틀 키 (reversal_essay|catalog|staged_scenario|metaphor_doc|explainer)",
 "fact_check": ["공개 전 사람이 확인해야 할 사실·수치 주장 (없으면 빈 배열)"],
 "sources": ["설명란에 적을 참고 자료 — 실제로 존재한다고 확신하는 것만 (기관·논문·보도 이름과 연도). 모르면 빈 배열, 지어내기 금지"],
 "pinned_comment": "고정 댓글 (시청자 참여 질문)",
 "features_used": ["위 특징 목록 중 실제 반영한 것들"],
 "scenes": [
   {{"chapter": "챕터 이름", "narration": "실제로 읽을 한국어 문장",
     "on_screen_text": "화면에 띄울 짧은 강조 문구(없으면 빈칸)",
     "importance": 1~3 (3=훅·반전·챕터 시작처럼 시각적으로 가장 중요한 장면),
     "beat": "이 장면이 속한 틀의 비트 이름",
     "pause_after": true/false (핵심 문장 직후라 1~2초 음악만 흐르게 할 장면, 영상 전체에서 4~8개),
     "visual": {{"source": "real|illustration|person|diagram",
                 "prompt": "English, concrete cinematic description of what is on screen, no text, no logos",
                 "stock_query": "2-4 English words for stock/archive footage search (e.g. silicon wafer cleanroom)",
                 "person": "실존 인물이 화면에 나올 때만 영문 이름 (예: Gordon Moore), 아니면 빈칸"}},
     "quote": {{"text": "실제로 한 말의 한국어 번역(확실할 때만)", "by": "말한 사람", "when": "연도·출처"}} 또는 null,
     "emotion": "curiosity|tension|surprise|relief|inspiration|fear|humor|nostalgia"}}
 ]
}}"""


class PlanError(RuntimeError):
    pass


# Script skeletons distilled from the owner's favourite reference videos (structure only - never their words).
FORMATS = {
    "reversal_essay": {
        "name": "반전 논제형 에세이",
        "when": "'왜 ~할까?' 같은 통념이 있는 주제",
        "beats": [
            "질문: 누구나 한 번쯤 품어 본 질문으로 연다 (0~5%)",
            "통념 뒤집기: '어쩌면 정반대일 수도 있습니다'로 끝까지 볼 이유를 만든다 (~5%)",
            "사례 3개: 규모가 점점 커지게 배치(개인→사회→지구 등), 세 사례가 같은 패턴으로 수렴 (5~45%)",
            "1차 결론: 세 사례의 공통점을 한 문장으로 (45%) — pause_after",
            "반례: '하지만 반대 경우도 있습니다'로 스스로 결론을 흔든다 (50~75%)",
            "2차 결론: 원인이 하나 더 있었다 — 더 깊은 결론 (75%) — pause_after",
            "희망 사례: 무거움을 따뜻하게 푸는 사례 (80~95%)",
            "열린 질문 + 채널 고정 끝인사",
        ]},
    "catalog": {
        "name": "정리형 (번호·목록)",
        "when": "헷갈리는 개념·종류·순위를 정리해 주는 주제",
        "beats": [
            "왜 지금 알아야 하는지(개봉·뉴스·시즌 등 시의성)로 연다",
            "가장 익숙한 항목부터 시작해 점점 낯설고 흥미로운 항목으로",
            "항목마다: 정체 → 의외의 사실(숫자·날짜·숨은 의미 한 가지) → 다음 항목과의 연결 문장",
            "중간에 '의외로 우리가 사는 현실은…' 같은 관점 전환 1회",
            "마지막: 앞으로 어떻게 될지 예측 질문 + 고정 끝인사",
        ]},
    "staged_scenario": {
        "name": "단계형 시나리오 다큐",
        "when": "위험·미래·과정을 다루는 주제 (어떻게 일어나는가)",
        "beats": [
            "콜드 오픈: 권위 있는 경고·사실 2~3개 → '그런데 정확히 어떻게요?'",
            "세 줄 요약: 시청자에게 전체 지도를 먼저 준다",
            "1~4단계: 단계마다 실제 실험·사건·숫자('100번 중 79번')로 증명, '이미 시작됐다'는 긴장",
            "메커니즘: 왜 그렇게 되는지 비유로 설명",
            "다른 갈래: 대안 시나리오 짧게",
            "정말 일어날까: 찬반 전문가를 공정하게 (반대 의견 필수)",
            "희망: 실제로 진행 중인 대응",
            "단계 요약 + '지금 우리는 몇 단계?' 댓글 질문",
        ]},
    "metaphor_doc": {
        "name": "비유 관통형 다큐",
        "when": "큰 질문(미래·사회·인간)에 답하는 긴 영상",
        "beats": [
            "콜드 오픈: 강렬한 비유 하나(예: 권위자의 한마디를 비유로 풀기)로 시작해 주제와 연결",
            "핵심 질문 3개를 던지고 영상이 그 순서로 답한다고 약속",
            "챕터마다 질문형 제목, 실제 데이터·사건으로 답한다",
            "역사 비유: 과거에 비슷한 일이 있었던 사례(누가 살아남고 누가 사라졌나)",
            "반대 의견과 아직 모르는 것을 정직하게",
            "희망의 비유: 첫 비유와 대비되는 두 번째 비유",
            "마무리: 첫 비유를 다시 꺼내 수미상관 + 시청자에게 남는 질문 + 시리즈 안내",
        ]},
    "explainer": {
        "name": "원리 해설형",
        "when": "현상의 원리를 쉽게 풀어주는 주제",
        "beats": [
            "일상 속 의문으로 연다", "직관적인 오답을 먼저 보여준다", "원리를 단계적으로",
            "놀라운 응용 사례", "한 문장 요약 + 질문"]},
}


def format_block(fmt):
    if fmt in FORMATS:
        f = FORMATS[fmt]
        return f"이번 영상은 '{f['name']}' 틀로 쓴다. 각 비트를 순서대로 지킬 것:\n" + "\n".join("- " + b for b in f["beats"])
    options = "\n".join(f"- {k}: {v['name']} — {v['when']}" for k, v in FORMATS.items())
    return ("주제에 가장 맞는 틀을 아래에서 하나 골라 format 필드에 적고, 그 틀의 흐름으로 쓴다:\n" + options + "\n"
            "각 틀의 비트:\n" + "\n".join(f"[{k}] " + " → ".join(b.split(':')[0] for b in v["beats"]) for k, v in FORMATS.items()))


def _recent_titles():
    return [r["topic"] + " → " + json.loads(r["plan"]).get("title", "") for r in
            db.rows("SELECT topic, plan FROM productions WHERE plan IS NOT NULL ORDER BY created_at DESC LIMIT 8")]


def _visual_mix():
    try:
        vm = learner.visual_mix_target()
    except Exception:
        vm = None
    if not vm:
        return "(아직 데이터 부족 — 기본: 실제 자료 50% 이상, 도식 20%, 일러스트 30% 이하)"
    return json.dumps(vm, ensure_ascii=False)[:2500]


def _studies(cid):
    try:
        from . import channel_study
        notes = channel_study.guidance_for(cid)
        synth = channel_study.get_synth(cid)
    except Exception:
        notes, synth = [], None
    if not notes and not synth:
        return "(아직 없음)"
    block = {}
    if synth:
        block["종합(여러 채널 공통)"] = {"느낌": synth.get("feel"), "잘된것": synth.get("works"), "안된것": synth.get("fails")}
    if notes:
        block["채널별"] = notes
    return json.dumps(block, ensure_ascii=False)[:5000]


def cps_for(channel=None):
    """Narration speed measured on this channel's real voice (falls back to the setting)."""
    try:
        from . import producer
        v = producer.channel_voices(channel)[0]
        key = "cps:" + ((v or {}).get("ref") or (v or {}).get("engine") or "default")
        val = db.kv_get(key)
        if val:
            return float(val)
    except Exception:
        pass
    return float(config.load_settings()["production"]["chars_per_second"])


def build_prompt(topic, instructions, minutes, features, channel=None, script_format="auto"):
    cps = cps_for(channel)
    total = minutes * 60
    pb = learner.playbook()
    short_pb = {"do": {k: [x["feature"] for x in v] for k, v in pb["do"].items()},
                "avoid": {k: [x["feature"] for x in v] for k, v in pb["avoid"].items()},
                "narrative": (pb.get("narrative") or "")[:3000]}
    ch_text = "(채널 미지정)"
    if channel:
        ch_text = json.dumps({k: channel.get(k) for k in ("name", "niches", "audience", "tone")}, ensure_ascii=False)
    return PLAN_PROMPT.format(
        topic=topic, channel=ch_text, format_block=format_block(script_format),
        min_real=int(float(config.load_settings()["production"].get("min_real_ratio", 0.6)) * 100),
        studies=_studies((channel or {}).get("id")),
        visual_mix=_visual_mix(),
        style_guide=(channel or {}).get("style_guide") or "(아직 없음)", instructions=("[추가 지시]\n" + instructions) if instructions else "", minutes=minutes,
        chars_min=int(total * cps * 0.85), chars_max=int(total * cps * 1.1),
        scenes_min=int(total / 10), scenes_max=int(total / 6),
        features=json.dumps(features, ensure_ascii=False) if features else "(아직 학습 데이터 없음 — 일반적인 롱폼 원칙 적용)",
        playbook=json.dumps(short_pb, ensure_ascii=False)[:5000],
        examples=json.dumps(learner.relevant_examples(topic, 5, (channel or {}).get("id")), ensure_ascii=False)[:9000],
        recent=json.dumps(_recent_titles(), ensure_ascii=False))


def validate(plan, minutes, cps=None):
    if not isinstance(plan, dict):
        raise PlanError("기획 JSON 형식 오류")
    scenes = [s for s in plan.get("scenes", []) if isinstance(s, dict) and str(s.get("narration", "")).strip()]
    if len(scenes) < 5:
        raise PlanError("장면 수 부족")
    for s in scenes:
        s["narration"] = " ".join(str(s["narration"]).split())
        s.setdefault("visual", {})
        if not isinstance(s["visual"], dict):
            s["visual"] = {"prompt": str(s["visual"])}
        s["visual"].setdefault("prompt", s["narration"][:80])
        if s["visual"].get("source") not in ("real", "illustration", "person", "diagram"):
            s["visual"]["source"] = "illustration"
        s["visual"]["person"] = str(s["visual"].get("person") or "").strip()[:60]
        if s["visual"]["source"] == "person" and not s["visual"]["person"]:
            s["visual"]["source"] = "real"
        q = s.get("quote")
        s["quote"] = ({"text": str(q.get("text", ""))[:120], "by": str(q.get("by", ""))[:40], "when": str(q.get("when", ""))[:40]}
                      if isinstance(q, dict) and q.get("text") and q.get("by") else None)
        s["visual"].setdefault("stock_query", plan.get("title", "")[:30])
        try:
            s["importance"] = int(s.get("importance", 1))
        except (TypeError, ValueError):
            s["importance"] = 1
        s["on_screen_text"] = str(s.get("on_screen_text") or "")[:30]
        s["pause_after"] = s.get("pause_after") in (True, "true", "True", 1, "1")
    plan["scenes"] = scenes
    plan["title"] = str(plan.get("title") or (plan.get("titles") or ["제목 없음"])[0])[:100]
    plan.setdefault("titles", [plan["title"]])
    plan.setdefault("thumbnail", {"text": plan["title"][:12], "image_prompt": scenes[0]["visual"]["prompt"]})
    plan.setdefault("tags", [])
    plan.setdefault("description", "")
    plan["sources"] = [str(x)[:200] for x in (plan.get("sources") or []) if str(x).strip()][:15]
    if plan.get("format") not in FORMATS:
        plan["format"] = None
    total_chars = sum(len(s["narration"]) for s in scenes)
    cps = cps or float(config.load_settings()["production"]["chars_per_second"])
    target = minutes * 60 * cps
    plan["_stats"] = {"scenes": len(scenes), "chars": total_chars, "target_chars": int(target),
                      "est_minutes": round(total_chars / cps / 60, 1)}
    return plan


def too_similar(plan):
    prev = db.rows("SELECT plan FROM productions WHERE plan IS NOT NULL AND status!='failed' ORDER BY created_at DESC LIMIT 20")
    first = plan["scenes"][0]["narration"]
    for r in prev:
        p = json.loads(r["plan"])
        if difflib.SequenceMatcher(None, p.get("title", ""), plan["title"]).ratio() > 0.75:
            return "제목이 이전 영상과 너무 비슷함: " + p.get("title", "")
        sc = p.get("scenes") or [{}]
        if difflib.SequenceMatcher(None, sc[0].get("narration", ""), first).ratio() > 0.6:
            return "도입부가 이전 영상과 너무 비슷함"
    return None


REVIEW_PROMPT = """너는 깐깐한 유튜브 편집장 겸 과학 감수자다. 아래 롱폼 기획안을 처음 보는 시청자 입장에서 검수하라.
점검: (1) 첫 5초에 클릭한 감정을 바로 건드리는가 (2) 30초 안에 끝까지 볼 이유가 있는가
(3) 지루하거나 같은 말을 반복하는 장면 (4) 과학적으로 틀렸거나 과장된 문장, 단정하면 안 되는 내용
(5) 마지막에 좋은 감정으로 끝나는가 (6) 채널 제작 규칙 위반 (7) 화면 묘사(visual.prompt)가 단조로운지
(8) format에 적힌 대본 틀의 비트 순서를 지켰는지, 중반 반례·반대 의견이 있는지 (9) sources에 지어낸 자료가 없는지(의심되면 삭제).
문제를 고친 '완성된 기획안 JSON 전체'를 같은 형식으로 다시 출력하고, "review_notes"에 무엇을 고쳤는지 3~8줄 적어라.
fact_check 목록은 보완하되 지우지 마라.

[채널 제작 규칙]
{style_guide}

[기획안]
{plan}"""


def self_review(plan, channel, generate, minutes):
    """Second pass in a fresh context - the analysed video's 'verify in a new session' step."""
    slim = {k: v for k, v in plan.items() if not k.startswith("_")}
    out = generate(REVIEW_PROMPT.format(style_guide=(channel or {}).get("style_guide") or "(없음)",
                                        plan=json.dumps(slim, ensure_ascii=False)[:60000]))
    try:
        reviewed = validate(out, minutes, cps_for(channel))
    except PlanError:
        return plan
    if reviewed["_stats"]["chars"] < plan["_stats"]["chars"] * 0.75:  # reviewer must not gut the script
        return plan
    reviewed.setdefault("fact_check", plan.get("fact_check", []))
    if not reviewed.get("sources"):
        reviewed["sources"] = plan.get("sources", [])
    if not reviewed.get("format"):
        reviewed["format"] = plan.get("format")
    return reviewed


def make_plan(topic, instructions="", minutes=None, production_id=None, generate=None, channel=None, script_format=None):
    generate = generate or (lambda prompt: llm.generate_json(prompt, SYSTEM, production_id))
    minutes = minutes or (channel or {}).get("target_minutes") or config.load_settings()["production"]["target_minutes"]
    features, explored = learner.choose_features()
    script_format = script_format or config.load_settings()["production"].get("script_format", "auto")
    prompt = build_prompt(topic, instructions, minutes, features, channel, script_format)
    cps = cps_for(channel)
    plan = validate(generate(prompt), minutes, cps)
    if plan["_stats"]["chars"] < plan["_stats"]["target_chars"] * 0.85:
        more = generate(prompt + f"\n\n[수정 요청] 직전 초안의 내레이션이 {plan['_stats']['chars']}자로 너무 짧다. "
                                 f"목표 {plan['_stats']['target_chars']}자에 맞게 장면을 늘리고 내용을 깊게 써서 JSON 전체를 다시 출력하라.")
        try:
            plan2 = validate(more, minutes, cps)
            if plan2["_stats"]["chars"] > plan["_stats"]["chars"]:
                plan = plan2
        except PlanError:
            pass
    real_ratio = lambda pl: sum(1 for x in pl["scenes"] if x["visual"].get("source") in ("real", "person")) / max(1, len(pl["scenes"]))
    need = float(config.load_settings()["production"].get("min_real_ratio", 0.6))
    if real_ratio(plan) < need:
        try:
            plan2 = validate(generate(prompt + f"\n\n[수정 요청] 실제 자료(real/person) 장면이 {real_ratio(plan):.0%}로 부족하다. "
                                               f"{need:.0%} 이상이 되도록, 실제로 존재하는 공정·장비·장소·인물 장면으로 visual.source와 "
                                               "stock_query를 다시 배분해서 JSON 전체를 다시 출력하라."), minutes, cps)
            if real_ratio(plan2) > real_ratio(plan):
                plan = plan2
        except PlanError:
            pass
    reason = too_similar(plan)
    if reason:
        try:
            plan2 = validate(generate(prompt + f"\n\n[수정 요청] {reason}. 완전히 다른 각도의 제목과 도입으로 다시 작성하라."), minutes, cps)
            if not too_similar(plan2):
                plan = plan2
        except PlanError:
            pass
    if config.load_settings()["production"].get("self_review", True):
        plan = self_review(plan, channel, generate, minutes)
    plan["fact_check"] = [str(x)[:300] for x in (plan.get("fact_check") or [])][:30]
    plan["_features_requested"] = features
    plan["_features_explored"] = explored
    valid = set(features)
    plan["features_used"] = [f for f in plan.get("features_used", []) if f in valid] or features
    return plan
