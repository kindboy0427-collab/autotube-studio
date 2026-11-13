"""Channels, style-guide learning, topic radar, caption watching and the usage/cadence report."""
import datetime as dt
import json
from unittest.mock import patch

from autotube import budget, channels, config, db, planner, radar, transcripts, usage, watcher
from tests.base import TempHome
from tests.test_learning import add_video, analysis


class ChannelTest(TempHome):
    def test_presets_and_seeds_are_per_channel(self):
        channels.add_preset("space")
        channels.add_preset("physics")
        channels.save({"id": "physics", "active": 0})
        seeds = watcher.seed_queries()
        groups = {g for _, g in seeds}
        self.assertEqual(groups, {"space"})  # paused channel is not learned
        self.assertTrue(any("블랙홀" in q for q, _ in seeds))

    def test_feedback_becomes_short_rules(self):
        channels.add_preset("space")
        rules = channels.learn_feedback("space", "배경음악이 너무 크다", generate=lambda p: "- 배경음악은 내레이션보다 훨씬 작게\n- 도입은 질문으로\n잡담")
        self.assertEqual(rules.splitlines(), ["- 배경음악은 내레이션보다 훨씬 작게", "- 도입은 질문으로"])
        self.assertEqual(channels.get("space")["style_guide"], rules)

    def test_plan_prompt_uses_channel_guide_and_emotion(self):
        ch = channels.add_preset("space")
        ch = channels.save({"id": "space", "style_guide": "- 숫자는 화면 글자로"})
        prompt = planner.build_prompt("블랙홀 증발", "", 10, [], ch)
        self.assertIn("숫자는 화면 글자로", prompt)
        self.assertIn("감정 설계", prompt)
        self.assertIn("fact_check", prompt)
        self.assertIn("우주·천문", prompt)

    def test_self_review_cannot_gut_script(self):
        base = planner.validate({"title": "t", "scenes": [{"narration": "가" * 80} for _ in range(20)]}, 5)
        short = planner.self_review(base, None, lambda p: {"title": "t2", "scenes": [{"narration": "짧다"} for _ in range(6)]}, 5)
        self.assertEqual(short["title"], "t")  # rejected: reviewer removed most of the script
        good = planner.self_review(base, None, lambda p: {"title": "고친 제목", "fact_check": ["수치 확인"],
                                                            "scenes": [{"narration": "나" * 82} for _ in range(20)]}, 5)
        self.assertEqual(good["title"], "고친 제목")
        self.assertEqual(good["fact_check"], ["수치 확인"])


class RadarTest(TempHome):
    def test_small_channel_recent_hits_are_signals(self):
        channels.add_preset("space")
        recent = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
        old = "2025-01-01T00:00:00Z"
        with db.connect() as con:
            for vid, pub, subs, out in [("aaaaaaaaaaa", recent, 800, 120.0), ("bbbbbbbbbbb", recent, 900000, 3.0),
                                        ("ccccccccccc", old, 500, 200.0)]:
                con.execute("INSERT INTO videos(id,title,published,subscribers,views,outlier_score,niche,status) VALUES(?,?,?,?,?,?,?,?)",
                            (vid, "영상 " + vid[0], pub, subs, 100000, out, "space", "queued"))
        sig = radar.signals("space")
        self.assertEqual([s["id"] for s in sig], ["aaaaaaaaaaa"])
        res = radar.suggest("space", generate=lambda p: {"ideas": [{"topic": "작은 블랙홀이 지구를 지나간다면", "why_now": "x",
                                                                     "entry_emotion": "불안", "exit_emotion": "안도",
                                                                     "signal_ids": ["aaaaaaaaaaa", "invented"]}]})
        self.assertEqual(res["ideas"][0]["signal_ids"], ["aaaaaaaaaaa"])
        self.assertEqual(db.kv_get("radar:space")["ideas"][0]["topic"], "작은 블랙홀이 지구를 지나간다면")


class TranscriptWatchTest(TempHome):
    def test_auto_switches_to_captions_after_free_hours(self):
        config.save_settings({"learning": {"free_tier_watch_hours": 0.25, "gemini_paid_tier": False, "watch_mode": "auto"}})
        for i in range(3):
            add_video(i, 1.0)
        used = []

        def gem(u, p, d):
            used.append("gemini")
            return json.dumps(analysis("question", ["open_loop"]), ensure_ascii=False)

        def cap(u, p, d):
            used.append("captions")
            return json.dumps(analysis("story_open", []), ensure_ascii=False)
        with patch("autotube.youtube_data.top_comments", return_value=[]):
            n = watcher.watch_next(3, watcher=gem, transcript_watcher=cap)
        self.assertEqual(n, 3)
        self.assertEqual(used, ["gemini", "captions", "captions"])  # 900 s fits once in 0.25 h, then captions
        self.assertEqual(watcher.watched_seconds_today(), 900)     # caption analyses don't use Gemini hours

    def test_caption_prompt_and_apify_call(self):
        config.set_secrets({"APIFY_TOKEN": "apify_x"})
        sent = {}

        def fake_post(url, body, headers=None, params=None, **kw):
            sent.update(url=url, body=body, headers=headers, params=params)
            return [{"text": "자막 본문입니다", "success": True}]
        with patch("autotube.http.post_json", side_effect=fake_post):
            self.assertEqual(transcripts.fetch("https://youtu.be/x"), "자막 본문입니다")
        self.assertIn("johnvc~YoutubeTranscripts/run-sync-get-dataset-items", sent["url"])
        self.assertEqual(sent["headers"]["Authorization"], "Bearer apify_x")
        self.assertEqual(sent["body"]["languages"], ["ko", "en"])
        self.assertLessEqual(sent["params"]["maxTotalChargeUsd"], 0.01)
        prompts = []
        transcripts.watch_via_transcript("u", watcher.WATCH_PROMPT[:200], 0, fetcher=lambda u: "대본",
                                         generate=lambda p: prompts.append(p) or "{}")
        self.assertIn("자막(대본)만", prompts[0])


class UsageTest(TempHome):
    def test_report_and_cadence(self):
        channels.add_preset("space")
        channels.add_preset("physics")
        t0 = dt.datetime(2026, 11, 13, 1, 0, tzinfo=dt.timezone.utc)
        times = {k: (t0 + dt.timedelta(minutes=m)).isoformat() for k, m in
                 [("plan", 0), ("voice", 3), ("visuals", 8), ("music", 30), ("render", 31), ("thumbnail", 40), ("upload", 41), ("done", 42)]}
        with db.connect() as con:
            for pid, ch in (("p1", "space"), ("p2", "physics")):
                con.execute("INSERT INTO productions(id,created_at,topic,status,channel_id,step_times,plan) VALUES(?,?,?,?,?,?,?)",
                            (pid, db.now(), "t", "uploaded_private", ch, json.dumps(times), json.dumps({"title": "제목", "scenes": [{}] * 60})))
        budget.record("gemini_video", 48, 7.2, "x", "p1")
        budget.record("elevenlabs_tts", 4200, 0.42, "x", "p1")
        budget.record("gemini_text", 50000, 0.1, "x", "p1")
        budget.record("gemini_video", 8, 1.2, "x", "p2")
        budget.record("edge_tts", 4000, 0.0, "x", "p2")
        budget.record("gemini_watch", 900000, 0.27, "learning")       # learning, no production
        budget.record("youtube_quota", 100, 0, "search")
        rep = usage.production_report("p1")
        self.assertAlmostEqual(rep["cost_usd"], 7.72, places=3)
        self.assertEqual(rep["items"][0]["label"], "AI 영상 클립")
        self.assertEqual(rep["total_seconds"], 42 * 60)
        self.assertEqual([s["step"] for s in rep["steps"]][:2], ["plan", "voice"])
        cad = usage.cadence(monthly_budget=50)
        self.assertEqual(cad["measured_videos"], 2)
        self.assertAlmostEqual(cad["avg_cost_usd"], (7.72 + 1.2) / 2, places=2)
        self.assertEqual(cad["learning_cost_30d"], 0.27)
        self.assertEqual(cad["affordable_videos_per_month"], int((50 - 0.27) // 4.46))
        sp = {c["id"]: c for c in cad["per_channel"]}
        self.assertAlmostEqual(sp["space"]["avg_cost_usd"], 7.72, places=2)
        self.assertEqual(sp["space"]["budget_allows_per_month"], int((50 - 0.27) / 2 // 7.72))   # 3 expensive videos
        self.assertEqual(sp["physics"]["budget_allows_per_month"], 8)                          # cheap: capped by goal
        self.assertIsNotNone(sp["physics"]["suggested_interval_days"])

    def test_web_channel_and_usage_endpoints(self):
        from fastapi.testclient import TestClient
        from autotube import web
        web.app.state.no_background = True
        c = TestClient(web.app)
        h = {"X-Token": web.TOKEN}
        self.assertEqual(c.post("/api/channels", json={"preset": "space"}, headers=h).json()["name"], "우주·천문")
        self.assertEqual(c.post("/api/channels", json={"id": "", "name": ""}, headers=h).status_code, 400)
        d = c.get("/api/channels").json()
        self.assertEqual(d["channels"][0]["id"], "space")
        self.assertFalse(d["channels"][0]["connected"])
        with patch.object(web.WORKER, "submit"):
            self.assertEqual(c.post("/api/productions", json={"topic": "블랙홀", "channel_id": "nope"}, headers=h).status_code, 400)
            self.assertEqual(c.post("/api/productions", json={"topic": "블랙홀", "channel_id": "space"}, headers=h).status_code, 200)
        self.assertEqual(c.get("/api/productions").json()[0]["channel_id"], "space")
        self.assertIn("cadence", c.get("/api/usage").json())
