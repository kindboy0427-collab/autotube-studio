# -*- coding: utf-8 -*-
"""Multiple channels, one per field. Each channel has its own niche, audience, tone,
narration voice, YouTube login and a short 'style guide' (the 'skill' manual)
that grows from your review feedback."""
from __future__ import annotations
import json
import re

from . import config, db

PRESETS = [
    {"id": "space", "name": "우주·천문", "niches": ["우주 미스터리", "블랙홀", "천문학 발견", "우주 탐사"],
     "audience": "우주 이야기에 호기심 많은 20~40대", "tone": "경이로움을 주는 차분한 다큐 내레이션"},
    {"id": "body", "name": "인체·생물", "niches": ["인체의 신비", "생물 진화", "동물 과학", "뇌과학"],
     "audience": "몸과 생명에 관심 있는 일반인", "tone": "친근하고 쉬운 설명, 놀라운 사실 중심 (진단·치료 조언 금지)"},
    {"id": "physics", "name": "물리·화학 원리", "niches": ["물리학 원리", "화학 반응", "일상 속 과학", "과학 실험 원리"],
     "audience": "학생·학부모·과학 입문자", "tone": "질문으로 시작해 원리를 단계적으로 풀어주는 강의형"},
    {"id": "tech", "name": "기술·미래과학", "niches": ["반도체 기술", "AI 기술 원리", "미래 기술", "에너지 기술"],
     "audience": "기술 뉴스에 관심 있는 직장인", "tone": "시의성 있는 이슈를 과학 원리로 풀어주는 해설형"},
    {"id": "earth", "name": "지구·기후과학", "niches": ["지구과학 미스터리", "기후 변화 과학", "지진 화산 원리", "바다 과학"],
     "audience": "자연현상의 원리가 궁금한 일반인", "tone": "실제 위성·관측 자료로 보여주는 차분한 다큐"},
    {"id": "history", "name": "과학사·발명", "niches": ["과학자 이야기", "위대한 발명", "과학사 미스터리"],
     "audience": "이야기를 좋아하는 교양 시청자", "tone": "인물의 극복 서사 중심 스토리텔링"},
]


AI_RISK = {
    "name": "AI 위험 분석",
    "niches": ["AI 위험", "AI 안전 연구", "AI 일자리 변화", "AI 기술 원리", "초지능 시나리오", "반도체 AI 경쟁"],
    "audience": "AI 뉴스에 불안과 호기심을 함께 느끼는 20~50대 직장인",
    "tone": "공포가 아니라 분석: 차분하고 신뢰감 있는 다큐 내레이션, 데이터와 실제 연구로 말하기",
    "style_guide": "\n".join([
        "- 포지션은 'AI 위험 분석가': '결국 멸망한다'처럼 단정하지 않는다. 위험의 근거와 통제 가능성을 함께 다룬다",
        "- 모든 영상에 반대 의견(낙관론·회의론) 파트를 1개 이상 넣는다",
        "- 실제 연구·실험·발표를 근거로 쓴다 (연구기관·논문·연도). 확실하지 않은 수치는 fact_check로",
        "- 인물 인용은 확인된 발언만, 사진+자막 카드로. 방송·유튜브 인터뷰 클립은 쓰지 않는다",
        "- 화면은 실제 자료 중심: 데이터센터·반도체 공정·연구소·로봇·관련 인물 사진, 원리는 도식으로",
        "- 감정선: 불안으로 클릭 → 이해와 '우리가 할 수 있는 것'으로 마무리. 겁만 주고 끝내지 않는다",
        "- 대본 틀은 반전 논제형 또는 단계형 시나리오를 우선 사용",
        "- 제목은 질문형·통념 뒤집기, 낚시성 과장 금지 (영상 내용과 제목이 일치)",
        "- 투자 추천처럼 들리는 표현 금지 (특정 종목·매수 언급 X)",
        "- 끝인사 고정: 'AI가 바꾸는 세상, 오늘도 분석해 봤습니다'",
    ]),
}


def apply_ai_risk_concept(cid="tech"):
    """One-time switch of the tech channel to the 'AI 위험 분석' concept (keeps voice, YouTube login and old rules)."""
    ch = get(cid)
    if not ch:
        return None
    old = [ln for ln in (ch.get("style_guide") or "").splitlines() if ln.strip() and ln not in AI_RISK["style_guide"]]
    guide = AI_RISK["style_guide"] + ("\n" + "\n".join(old) if old else "")
    return save({"id": cid, "name": AI_RISK["name"], "niches": AI_RISK["niches"], "audience": AI_RISK["audience"],
                 "tone": AI_RISK["tone"], "style_guide": "\n".join(guide.splitlines()[:25])})


def _row(r):
    r["niches"] = json.loads(r.get("niches") or "[]")
    return r


def all_channels(active_only=False):
    sql = "SELECT * FROM channels" + (" WHERE active=1" if active_only else "") + " ORDER BY created_at"
    return [_row(r) for r in db.rows(sql)]


def get(cid):
    if not cid:
        return None
    r = db.one("SELECT * FROM channels WHERE id=?", (cid,))
    return _row(r) if r else None


FIELDS = ("name", "niches", "audience", "tone", "voice_id", "voice", "target_minutes", "style_guide", "monthly_videos", "active")


def save(data: dict):
    cid = re.sub(r"[^a-z0-9_-]", "", (data.get("id") or "").lower())[:30] or db.new_id()[-6:]
    cur = get(cid) or {}
    row = {**cur, **{k: v for k, v in data.items() if k in FIELDS}}
    niches = row.get("niches") or []
    if isinstance(niches, str):
        niches = [x.strip() for x in niches.split(",") if x.strip()]
    with db.connect() as con:
        voice = row.get("voice") or ""
        if isinstance(voice, dict):
            voice = json.dumps(voice, ensure_ascii=False)
        con.execute("""INSERT INTO channels(id,name,niches,audience,tone,voice_id,voice,target_minutes,style_guide,monthly_videos,active,created_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name, niches=excluded.niches,
            audience=excluded.audience, tone=excluded.tone, voice_id=excluded.voice_id, voice=excluded.voice, target_minutes=excluded.target_minutes,
            style_guide=excluded.style_guide, monthly_videos=excluded.monthly_videos, active=excluded.active""",
                    (cid, row.get("name") or cid, json.dumps(niches, ensure_ascii=False), row.get("audience") or "",
                     row.get("tone") or "", row.get("voice_id") or "", voice, int(row.get("target_minutes") or 10),
                     row.get("style_guide") or "", int(row.get("monthly_videos") or 8), int(row.get("active", 1)),
                     cur.get("created_at") or db.now()))
    return get(cid)


def add_preset(pid):
    p = next((x for x in PRESETS if x["id"] == pid), None)
    if not p:
        raise KeyError(pid)
    return save(dict(p))


def all_niches():
    out = []
    for c in all_channels(active_only=True):
        out += [(n, c["id"]) for n in c["niches"]]
    return out


def token_file(cid):
    return config.DATA / (f"youtube_token_{cid}.json" if cid else "youtube_token.json")


def learn_feedback(cid, feedback, generate=None):
    """Turn review notes ('음악이 너무 크다', '도입이 길다') into short permanent rules.
    This is the 'skill' manual idea from the analysed videos: keep it short - goal, order, standards."""
    from . import llm
    ch = get(cid)
    if not ch:
        raise KeyError(cid)
    generate = generate or (lambda prompt: llm.generate(prompt, "편집 규칙 정리 담당", json_mode=False, max_tokens=2000))
    prompt = ("아래는 유튜브 채널의 제작 규칙(스타일 가이드)과 방금 영상을 검토한 사람의 피드백이다. "
              "피드백을 반영해 규칙을 갱신하라. 규칙은 '- '로 시작하는 짧은 문장, 최대 25줄. "
              "중복은 합치고, 서로 충돌하면 새 피드백을 따른다. 규칙 목록만 출력.\n\n[현재 규칙]\n"
              + (ch["style_guide"] or "(없음)") + "\n\n[새 피드백]\n" + feedback.strip())
    text = generate(prompt).strip()
    lines = [ln.strip() for ln in text.splitlines() if ln.strip().startswith("-")][:25]
    if not lines:
        lines = [ln for ln in (ch["style_guide"] or "").splitlines() if ln.strip()] + ["- " + feedback.strip()[:200]]
    save({"id": cid, "style_guide": "\n".join(lines)})
    return "\n".join(lines)
