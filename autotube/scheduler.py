# -*- coding: utf-8 -*-
"""Background learning loop: discover -> watch -> learn, plus daily/weekly jobs."""
from __future__ import annotations
import datetime as dt
import threading
import time

from . import config, db, learner, watcher


class Scheduler:
    def __init__(self):
        self.thread = threading.Thread(target=self._loop, name="learner", daemon=True)
        self.stop = threading.Event()
        self.last_tick = None
        self.state = "대기"

    def start(self):
        self.thread.start()

    def tick(self):
        s = config.load_settings()
        L = s["learning"]
        if not L.get("enabled"):
            self.state = "학습 꺼짐"
            return
        if not (config.get_secret("GEMINI_API_KEY") and config.get_secret("YOUTUBE_API_KEY")):
            self.state = "GEMINI/YOUTUBE 키 필요"
            return
        day = db.kst_day()
        searches = db.kv_get("searches:" + day, 0)
        queued = db.one("SELECT COUNT(*) c FROM videos WHERE status='queued'")["c"]
        # 1) keep the queue filled
        if queued < L["watch_batch"] * 3 and searches < L["max_searches_per_day"]:
            seeds = watcher.seed_queries()
            if not seeds:
                self.state = "학습할 분야 없음 (설정 → 학습 분야 입력 또는 영상 제작)"
            idx = db.kv_get("seed_index", 0)
            for k in range(min(2, len(seeds))):
                q, niche = seeds[(idx + k) % len(seeds)]
                order = ["viewCount", "relevance", "date"][(idx + k) % 3]
                try:
                    watcher.discover(q, niche, order=order)
                except Exception as ex:
                    db.event("warn", "discover", f"검색 실패: {str(ex)[:200]}")
                    break
                finally:
                    db.kv_set("searches:" + day, db.kv_get("searches:" + day, 0) + 2)
            db.kv_set("seed_index", idx + 2)
        # 2) watch a batch
        self.state = "영상 시청·분석 중"
        n = watcher.watch_next()
        # 3) learn
        if n:
            learner.update_patterns()
        # 4) daily own-performance feedback
        if db.kv_get("perf_day") != day:
            try:
                if db.one("SELECT COUNT(*) c FROM productions WHERE youtube_id IS NOT NULL")["c"]:
                    learner.record_own_performance()
            except Exception as ex:
                db.event("warn", "performance", f"내 영상 성과 측정 실패: {str(ex)[:200]}")
            db.kv_set("perf_day", day)
        # 5) weekly written playbook
        week = dt.date.today().isocalendar()[:2]
        if db.kv_get("playbook_week") != list(week):
            try:
                if learner.synthesize_narrative():
                    db.event("info", "learn", "주간 플레이북 갱신")
                db.kv_set("playbook_week", list(week))
            except Exception as ex:
                db.event("warn", "learn", f"플레이북 작성 실패: {str(ex)[:200]}")
        self.state = "대기 (다음 학습 예정)"

    def _loop(self):
        time.sleep(5)
        while not self.stop.is_set():
            try:
                self.tick()
            except Exception as ex:
                db.event("error", "scheduler", str(ex)[:300])
                self.state = "오류 — 이벤트 로그 확인"
            self.last_tick = db.now()
            minutes = config.load_settings()["learning"].get("tick_minutes", 20)
            self.stop.wait(max(60, minutes * 60))
