# -*- coding: utf-8 -*-
"""How much one finished video really uses -> how often each channel can publish.

Per production: every API call is already logged in the usage table with the
production id (tokens, characters, images, video seconds, estimated $), plus the
time each step started. Learning (AI watching) costs are shared across channels.
"""
from __future__ import annotations
import datetime as dt
import json

from . import budget, channels, config, db

UNIT_NAMES = {
    "gemini_text": "토큰", "openai_text": "토큰", "gemini_watch": "토큰", "gemini_image": "장",
    "gemini_video": "초", "elevenlabs_tts": "글자", "openai_tts": "글자", "edge_tts": "글자(무료)",
    "elevenlabs_music": "분", "youtube_quota": "할당량 단위", "claude_subscription": "글자(요금제)",
    "codex_subscription": "글자(요금제)", "apify_transcript": "편",
}
LABELS = {
    "gemini_text": "대본·검수 (Gemini)", "openai_text": "대본·검수 (OpenAI)", "gemini_image": "AI 이미지",
    "gemini_video": "AI 영상 클립", "elevenlabs_tts": "내레이션 (ElevenLabs)", "openai_tts": "내레이션 (OpenAI)",
    "edge_tts": "내레이션 (무료 Edge)", "elevenlabs_music": "배경음악", "gemini_watch": "AI 영상 시청·분석",
    "youtube_quota": "YouTube API 할당량", "claude_subscription": "대본·검수 (Claude Pro 요금제, 추가비용 0)",
    "codex_subscription": "대본·검수 (ChatGPT Plus 요금제, 추가비용 0)", "apify_transcript": "자막 분석 (Apify)",
}


def _ts(s):
    try:
        return dt.datetime.fromisoformat(s)
    except (TypeError, ValueError):
        return None


def production_report(pid):
    p = db.one("SELECT * FROM productions WHERE id=?", (pid,))
    if not p:
        return None
    items = db.rows("SELECT service, SUM(units) units, ROUND(SUM(cost_usd),4) cost, COUNT(*) calls FROM usage "
                    "WHERE production_id=? GROUP BY service ORDER BY cost DESC", (pid,))
    for it in items:
        it["label"] = LABELS.get(it["service"], it["service"])
        it["unit"] = UNIT_NAMES.get(it["service"], "")
    times = json.loads(p.get("step_times") or "{}")
    order = ["plan", "voice", "visuals", "music", "render", "thumbnail", "upload", "done"]
    steps = []
    known = [(k, _ts(times[k])) for k in order if k in times and _ts(times[k])]
    for i, (k, t) in enumerate(known[:-1]):
        steps.append({"step": k, "seconds": round((known[i + 1][1] - t).total_seconds())})
    total_seconds = round((known[-1][1] - known[0][1]).total_seconds()) if len(known) >= 2 else None
    plan = json.loads(p.get("plan") or "{}")
    video_minutes = None
    if p.get("video_path"):
        try:
            from . import media
            video_minutes = round(media.duration(p["video_path"]) / 60, 2)
        except Exception:
            video_minutes = None
    cost = round(sum(i["cost"] or 0 for i in items), 4)
    return {"id": pid, "title": plan.get("title") or p["topic"], "channel_id": p.get("channel_id"), "status": p["status"],
            "cost_usd": cost, "items": items, "steps": steps, "total_seconds": total_seconds,
            "video_minutes": video_minutes, "scenes": len(plan.get("scenes", [])),
            "cost_per_minute": round(cost / video_minutes, 4) if video_minutes else None}


def learning_cost(days=30):
    since = (dt.datetime.now(db.KST) - dt.timedelta(days=days)).date().isoformat()
    r = db.one("SELECT COALESCE(SUM(cost_usd),0) c FROM usage WHERE day>=? AND production_id IS NULL "
               "AND service!='youtube_quota'", (since,))
    return round(r["c"], 4)


def cadence(monthly_budget=None):
    """Average cost/time of finished videos -> sustainable videos per month per channel."""
    s = config.load_settings()
    monthly_budget = monthly_budget if monthly_budget is not None else s["budget"]["monthly_usd"]
    done = db.rows("SELECT id, channel_id FROM productions WHERE status IN ('ready','uploaded_private','published') "
                   "ORDER BY created_at DESC LIMIT 30")
    reports = [r for r in (production_report(d["id"]) for d in done) if r]
    learn30 = learning_cost(30)
    if not reports:
        return {"measured_videos": 0, "learning_cost_30d": learn30, "monthly_budget": monthly_budget,
                "message": "완성된 영상이 아직 없습니다. 1편을 만들면 실제 사용량으로 계산합니다."}
    avg_cost = sum(r["cost_usd"] for r in reports) / len(reports)
    secs = [r["total_seconds"] for r in reports if r["total_seconds"]]
    avg_secs = sum(secs) / len(secs) if secs else None
    available = max(0.0, monthly_budget - learn30)
    affordable = int(available // avg_cost) if avg_cost > 0 else None
    chs = channels.all_channels(active_only=True)
    wanted = sum(c["monthly_videos"] for c in chs) or None
    per_channel = []
    if chs:
        share_usd = available / len(chs)  # equal money per channel; cheaper formats get more videos
        for c in chs:
            ch_reports = [r for r in reports if r["channel_id"] == c["id"]]
            ch_cost = (sum(r["cost_usd"] for r in ch_reports) / len(ch_reports)) if ch_reports else avg_cost
            plan_n = min(c["monthly_videos"], int(share_usd // ch_cost)) if ch_cost > 0 else c["monthly_videos"]
            per_channel.append({"id": c["id"], "name": c["name"], "avg_cost_usd": round(ch_cost, 3),
                                "measured": len(ch_reports), "wanted_per_month": c["monthly_videos"],
                                "budget_allows_per_month": plan_n,
                                "suggested_interval_days": round(30 / plan_n, 1) if plan_n else None})
    quota = s.get("youtube_quota_per_day", 10000)
    return {"measured_videos": len(reports), "avg_cost_usd": round(avg_cost, 3),
            "avg_production_minutes": round(avg_secs / 60, 1) if avg_secs else None,
            "learning_cost_30d": learn30, "monthly_budget": monthly_budget,
            "budget_for_videos": round(available, 2), "affordable_videos_per_month": affordable,
            "wanted_videos_per_month": wanted, "per_channel": per_channel,
            "youtube_quota_note": f"업로드는 할당량 {config.QUOTA_COST['upload']}단위/편 · 하루 {quota}단위 중 학습 검색이 나눠 씀",
            "recent": [{k: r[k] for k in ("id", "title", "cost_usd", "total_seconds", "video_minutes")} for r in reports[:10]]}


def summary_today():
    return budget.summary()
