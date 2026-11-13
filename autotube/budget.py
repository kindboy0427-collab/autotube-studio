# -*- coding: utf-8 -*-
"""Cost and YouTube-quota bookkeeping. All costs are ESTIMATES from settings['prices']."""
from __future__ import annotations
from . import config, db


class BudgetExceeded(RuntimeError):
    pass


def record(service, units, cost_usd, detail="", production_id=None):
    with db.connect() as con:
        con.execute("INSERT INTO usage(day,service,units,cost_usd,detail,production_id,created_at) VALUES(?,?,?,?,?,?,?)",
                    (db.kst_day(), service, float(units), float(cost_usd), str(detail)[:200], production_id, db.now()))
    if production_id and cost_usd:
        db.execute("UPDATE productions SET cost_usd=COALESCE(cost_usd,0)+? WHERE id=?", (float(cost_usd), production_id))


def spent_today():
    r = db.one("SELECT COALESCE(SUM(cost_usd),0) s FROM usage WHERE day=?", (db.kst_day(),))
    return round(r["s"], 4)


def spent_month():
    r = db.one("SELECT COALESCE(SUM(cost_usd),0) s FROM usage WHERE substr(day,1,7)=?", (db.kst_day()[:7],))
    return round(r["s"], 4)


def production_cost(pid):
    r = db.one("SELECT COALESCE(cost_usd,0) c FROM productions WHERE id=?", (pid,))
    return r["c"] if r else 0.0


def check(estimated_usd=0.0, production_id=None):
    s = config.load_settings()
    b = s["budget"]
    if b.get("daily_usd") and spent_today() + estimated_usd > b["daily_usd"]:
        raise BudgetExceeded(f"오늘 예산 ${b['daily_usd']} 초과 예상")
    if b.get("monthly_usd") and spent_month() + estimated_usd > b["monthly_usd"]:
        raise BudgetExceeded(f"이번 달 예산 ${b['monthly_usd']} 초과 예상")
    if production_id:
        cap = s["production"].get("max_cost_per_video_usd") or 0
        if cap and production_cost(production_id) + estimated_usd > cap:
            raise BudgetExceeded(f"영상 1편 예산 ${cap} 초과 예상")


def quota_used_today():
    r = db.one("SELECT COALESCE(SUM(units),0) s FROM usage WHERE day=? AND service='youtube_quota'", (db.kst_day(),))
    return int(r["s"])


def take_quota(kind, count=1):
    """Reserve YouTube Data API quota units before a call. Returns False if the day is full."""
    units = config.QUOTA_COST[kind] * count
    limit = config.load_settings().get("youtube_quota_per_day", 10000)
    if quota_used_today() + units > limit:
        return False
    record("youtube_quota", units, 0.0, kind)
    return True


def summary():
    return {"today_usd": spent_today(), "month_usd": spent_month(),
            "youtube_quota_today": quota_used_today(),
            "by_service_today": db.rows("SELECT service, ROUND(SUM(cost_usd),4) cost, SUM(units) units FROM usage "
                                        "WHERE day=? GROUP BY service ORDER BY cost DESC", (db.kst_day(),))}
