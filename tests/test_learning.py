"""Watching (fake Gemini), feature extraction, pattern learning, planning and web API."""
import json
import random
from unittest.mock import patch

from autotube import config, db, learner, planner, watcher, budget
from tests.base import TempHome


def analysis(hook, devices, n=0):
    return {"topic": f"주제 {n}", "summary": "요약 문장입니다.", "format": "documentary",
            "hook": {"type": hook, "first_15s": "충격적인 질문으로 시작", "promise": "끝까지 보면 답"},
            "structure": [{"start": "00:00", "label": "도입", "purpose": "훅"}],
            "retention_devices": devices, "engagement_triggers": ["comment_question", "made_up_tag"],
            "pacing": {"avg_shot_seconds": 2.5, "narration_speed": "fast", "energy": "high"},
            "narration_style": ["storytelling"], "visual_style": ["stock_footage"], "emotional_arc": ["curiosity", "surprise"],
            "title_pattern": ["question"], "cta_timing": "end", "why_it_works": ["궁금증"], "weaknesses": ["중반 늘어짐"],
            "reusable_ideas": ["질문형 도입"]}


def add_video(i, outlier, niche="역사"):
    with db.connect() as con:
        con.execute("""INSERT INTO videos(id,title,channel_id,channel_title,published,duration_seconds,views,likes,comments,
            subscribers,views_per_day,outlier_score,engagement_rate,niche,status,discovered_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,'queued',?)""",
                    (f"vid{i:08d}", f"영상 {i}", "c", "채널", "2026-10-01T00:00:00Z", 900, int(outlier * 10000), 100, 10,
                     10000, outlier * 300, outlier, 0.01, niche, db.now()))


class LearningTest(TempHome):
    def test_feature_extraction_filters_vocab(self):
        f = watcher.features_from(analysis("question", ["open_loop", "nonsense"]))
        self.assertIn("hook:question", f)
        self.assertIn("retention:open_loop", f)
        self.assertNotIn("retention:nonsense", f)
        self.assertNotIn("engagement:made_up_tag", f)
        self.assertIn("shots:fast_cuts", f)
        self.assertIn("opens_with:curiosity", f)

    def test_watch_store_and_learn_lift(self):
        config.save_settings({"learning": {"watch_batch": 30, "max_watch_per_day": 100, "gemini_paid_tier": True, "watch_mode": "gemini"}})
        rng = random.Random(1)
        for i in range(24):
            add_video(i, outlier=rng.uniform(0.1, 1) + (3 if i % 2 == 0 else 0))

        def fake_watch(url, prompt, dur):
            self.assertIn("화면과 소리를 모두", prompt)
            n = int(url[-8:].replace("vid", ""))
            good = n % 2 == 0  # good videos use question hooks + open loops
            return json.dumps(analysis("question" if good else "bold_claim", ["open_loop"] if good else ["recap"], n),
                              ensure_ascii=False)
        with patch("autotube.youtube_data.top_comments", return_value=[{"text": "2편 주세요", "likes": 5}]):
            n = watcher.watch_next(30, watcher=fake_watch)
        self.assertEqual(n, 24)
        learner.update_patterns()
        pats = {p["feature"]: p for p in db.rows("SELECT * FROM patterns")}
        self.assertGreater(pats["hook:question"]["lift"], 0.1)
        self.assertLess(pats["hook:bold_claim"]["lift"], -0.1)
        pb = learner.playbook()
        self.assertIn("hook:question", [x["feature"] for x in pb["do"]["hook"]])
        ex = learner.relevant_examples("주제", 3)
        self.assertEqual(len(ex), 3)
        self.assertEqual(ex[0]["viewer_comments"], ["2편 주세요"])
        feats, _ = learner.choose_features(exploration=0, rng=random.Random(3))
        self.assertIn("hook:question", feats)

    def test_free_tier_hour_cap(self):
        config.save_settings({"learning": {"free_tier_watch_hours": 0.5, "gemini_paid_tier": False}})
        for i in range(5):
            add_video(i, 1.0)
        with patch("autotube.youtube_data.top_comments", return_value=[]):
            n = watcher.watch_next(5, watcher=lambda u, p, d: json.dumps(analysis("question", []), ensure_ascii=False))
        self.assertEqual(n, 2)  # 900 s each, 1800 s allowed

    def test_planner_validates_and_expands(self):
        config.save_settings({"production": {"min_real_ratio": 0}})
        calls = []

        def gen(prompt):
            calls.append(prompt)
            k = 6 if len(calls) == 1 else 40
            return {"title": "제목", "titles": ["제목"], "features_used": ["hook:question", "invented"],
                    "scenes": [{"narration": "가" * 60, "visual": "a city", "importance": "3"} for _ in range(k)]}
        plan = planner.make_plan("주제", "", minutes=5, generate=gen)
        self.assertEqual(len(calls), 3)  # draft, too short -> expand once, then editor/fact self-review
        self.assertIn("편집장 겸 과학 감수자", calls[2])
        self.assertEqual(len(plan["scenes"]), 40)
        self.assertEqual(plan["scenes"][0]["visual"]["prompt"], "a city")
        self.assertEqual(plan["scenes"][0]["importance"], 3)
        self.assertNotIn("invented", plan["features_used"])
        self.assertIn("AI가 학습한 성과 패턴", calls[0])

    def test_budget_blocks(self):
        config.save_settings({"budget": {"daily_usd": 1.0, "monthly_usd": 100}})
        budget.record("gemini_text", 1, 0.9)
        with self.assertRaises(budget.BudgetExceeded):
            budget.check(0.2)
        config.save_settings({"youtube_quota_per_day": 150})
        self.assertTrue(budget.take_quota("search"))
        self.assertFalse(budget.take_quota("search"))

    def test_own_performance_feedback(self):
        with db.connect() as con:
            for i, (feats, pct) in enumerate([(["hook:question"], 62.0), (["hook:bold_claim"], 31.0), (["hook:question"], 58.0)]):
                con.execute("INSERT INTO productions(id,created_at,topic,status,features_used,youtube_id) VALUES(?,?,?,?,?,?)",
                            (f"p{i}", db.now(), "t", "published", json.dumps(feats), f"yt{i:09d}"))
        stats = {f"yt{i:09d}": {"views": 100 * (i + 1), "likes": 1, "comments": 1, "privacy": "public", "published": "2026-11-01"} for i in range(3)}
        pcts = {"yt000000000": 62.0, "yt000000001": 31.0, "yt000000002": 58.0}
        n = learner.record_own_performance(lambda ids: stats, lambda vid, pub: {"avg_view_percentage": pcts[vid], "avg_view_duration": 100})
        self.assertEqual(n, 3)
        own = learner.own_feature_stats()
        self.assertGreater(own["hook:question"]["lift"], own["hook:bold_claim"]["lift"])


class WebTest(TempHome):
    def test_api(self):
        from fastapi.testclient import TestClient
        from autotube import web
        web.app.state.no_background = True
        with patch.object(web.WORKER, "submit") as submit:
            c = TestClient(web.app)
            self.assertIn("AutoTube", c.get("/").text)
            st = c.get("/api/status").json()
            self.assertFalse(st["keys"]["GEMINI_API_KEY"]["set"])
            self.assertEqual(c.post("/api/productions", json={"topic": "조선 왕의 음식"}).status_code, 403)  # no token
            h = {"X-Token": web.TOKEN}
            r = c.post("/api/productions", json={"topic": "조선 왕의 음식", "minutes": 8}, headers=h)
            self.assertEqual(r.status_code, 200)
            submit.assert_called_once()
            self.assertEqual(len(c.get("/api/productions").json()), 1)
            c.post("/api/secrets", json={"GEMINI_API_KEY": "g-123"}, headers=h)
            self.assertTrue(c.get("/api/status").json()["keys"]["GEMINI_API_KEY"]["set"])
            c.post("/api/settings", json={"learning": {"niches": ["역사"]}, "web_port": 1}, headers=h)
            s = config.load_settings()
            self.assertEqual(s["learning"]["niches"], ["역사"])
            self.assertEqual(s["web_port"], 8770)  # not editable via API
            self.assertEqual(c.get("/media/x/../../secrets.json").status_code, 404)
            self.assertEqual(c.get("/", headers={"host": "evil.com"}).status_code, 403)
            self.assertIn("playbook", c.get("/api/learning").json())


class HybridWatch(TempHome):
    def test_top_outliers_full_watch_rest_captions(self):
        config.save_settings({"learning": {"watch_batch": 10, "max_watch_per_day": 100, "watch_mode": "hybrid",
                                           "gemini_full_watch_per_day": 3}})
        for i in range(8):
            add_video(i, outlier=10 - i)
        used = []

        def full(url, prompt, dur):
            used.append(("full", url[-8:]))
            return json.dumps(analysis("question", ["open_loop"], 1), ensure_ascii=False)

        def captions(v, prompt):
            used.append(("cap", v["id"][-8:]))
            return analysis("question", ["open_loop"], 1)
        with patch("autotube.youtube_data.top_comments", return_value=[]), \
                patch("autotube.watcher.watch_video", side_effect=lambda v, w, counts_gemini_hours=True:
                      full("x" + v["id"], "", 0) if counts_gemini_hours else captions(v, "")):
            watcher.watch_next(8, watcher=full, transcript_watcher=captions)
        self.assertEqual([k for k, _ in used].count("full"), 3)
        self.assertEqual([k for k, _ in used].count("cap"), 5)
        self.assertEqual(used[0][0], "full")  # strongest outliers get the full watch
