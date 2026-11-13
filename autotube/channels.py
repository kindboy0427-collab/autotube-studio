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
    {"id": "history", "name": "과학사·발명", "niches": ["과학자 이야기", "위대한 발명", "과학사 미스터리"],
     "audience": "이야기를 좋아하는 교양 시청자", "tone": "인물의 극복 서사 중심 스토리텔링"},
]


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
