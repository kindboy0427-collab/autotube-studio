"""v1.10: title+thumbnail package, opening candidates, Shorts from the long-form, scene-by-scene retention."""
import json
import random
import re
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

from autotube import config, db, hooks, media, package, producer, retention, shorts
from tests.base import TempHome
from tests.test_pipeline import FakeProviders


def scene(text, **kw):
    s = {"chapter": kw.pop("chapter", "도입"), "narration": text, "importance": 1, "emotion": "curiosity",
         "visual": {"source": "real", "prompt": "lab", "stock_query": "lab"}}
    s.update(kw)
    return s


class PackageTest(TempHome):
    def test_lint_rules(self):
        self.assertTrue(any("반복" in x for x in package.lint("블랙홀은 왜 검을까", "블랙홀 검을까")))
        self.assertTrue(any("12자" in x for x in package.lint("짧은 제목", "이것은 아주 긴 썸네일 글자입니다")))
        self.assertTrue(any("휴대폰" in x for x in package.lint("가" * 50, "충격")))
        self.assertTrue(any("남발" in x for x in package.lint("정말???", "와")))
        self.assertEqual(package.lint("블랙홀은 왜 검을까", "빛도 못 나온다"), [])

    def test_best_pair_and_ab_texts(self):
        plan = {"title": "블랙홀은 왜 검을까", "titles": ["블랙홀은 왜 검을까", "빛이 사라지는 곳의 비밀"],
                "thumbnail": {"text": "블랙홀 검을까", "image_prompt": "black hole"},
                "scenes": [scene("빛도 빠져나오지 못하는 곳이 있습니다.")] * 3}
        seen = []

        def gen(prompt):
            seen.append(prompt)
            return {"thumb_options": ["빛도 못 나온다", "우주의 구멍", "블랙홀 검을까"],
                    "pairs": [{"title": "블랙홀은 왜 검을까", "thumb": "블랙홀 검을까", "curiosity": 9, "clarity": 9,
                               "complement": 9, "honesty": 9, "mobile": 9, "note": "반복"},
                              {"title": "블랙홀은 왜 검을까", "thumb": "빛도 못 나온다", "curiosity": 8, "clarity": 8,
                               "complement": 9, "honesty": 9, "mobile": 9, "note": "보완"},
                              {"title": "빛이 사라지는 곳의 비밀", "thumb": "우주의 구멍", "curiosity": 6, "clarity": 5,
                               "complement": 7, "honesty": 8, "mobile": 8}]}
        package.check(plan, "블랙홀", gen)
        # the AI loved the repeating pair, but the hard rule (no repetition) costs more than taste points
        self.assertEqual(plan["thumbnail"]["text"], "빛도 못 나온다")
        self.assertEqual(plan["title"], "블랙홀은 왜 검을까")
        self.assertIn("우주의 구멍", plan["thumbnail"]["alt_texts"])
        self.assertNotIn("블랙홀 검을까", plan["thumbnail"]["alt_texts"])
        self.assertTrue(plan["package"]["before"]["issues"])
        self.assertIn("블랙홀은 왜 검을까", seen[0])

    def test_no_ai_keeps_plan(self):
        plan = {"title": "좋은 제목", "titles": ["좋은 제목", "다른 제목"], "thumbnail": {"text": "새 정보"}, "scenes": []}
        package.check(plan, "x", None)
        self.assertEqual(plan["title"], "좋은 제목")


class HookTest(TempHome):
    def _plan(self):
        sc = [scene("오늘은 블랙홀에 대해 알아보겠습니다. 블랙홀은 아주 무거운 천체입니다.", importance=3),
              scene("블랙홀은 별이 죽으면서 생깁니다. 그 과정은 이렇습니다.")]
        sc += [scene(f"본론 {i}번째 이야기입니다. 중력이 모든 것을 끌어당깁니다.", chapter="본론") for i in range(8)]
        return {"title": "블랙홀은 왜 검을까", "thumbnail": {"text": "빛도 못 나온다"}, "scenes": sc,
                "hook_strategy": "정보 전달", "fact_check": []}

    def _writer(self, prompts):
        def gen(prompt):
            prompts.append(prompt)
            if "서로 다른 방식으로" in prompt:
                return {"candidates": [
                    {"type": "mini_story", "promise": "빛이 갇히는 이유", "fact_check": ["1783년 미첼 확인"],
                     "scenes": [{"narration": "1783년, 한 신부가 이상한 계산을 했습니다. 너무 무거워서 빛조차 나오지 못하는 별.",
                                 "visual": {"source": "illustration", "prompt": "candle-lit desk"}, "importance": 3},
                                {"narration": "그 별은 정말 있었습니다. 오늘은 빛이 어떻게 갇히는지 끝까지 따라가 봅니다.",
                                 "visual": {"source": "real", "prompt": "telescope"}}]},
                    {"type": "question", "promise": "", "scenes": [{"narration": "짧음"}]},  # too short -> dropped
                    {"type": "question", "promise": "궁금증", "scenes": [
                        {"narration": "왜 블랙홀은 검을까요? 빛을 내지 않는 걸까요, 빛을 삼키는 걸까요? 끝까지 보시면 압니다."}]}]}
            return {}
        return gen

    def _judge(self, scores):
        def judge(prompt):
            out = []
            for m in re.finditer(r"\[([A-Z])\] (.+)", prompt):
                key = next((k for k in scores if k in m.group(2)), None)
                base = scores.get(key, 5)
                out.append({"id": m.group(1), **{k: base for k in hooks.WEIGHTS}, "why": key or ""})
            return {"scores": out}
        return judge

    def test_better_opening_replaces_original(self):
        plan, prompts = self._plan(), []
        n_before = len(plan["scenes"])
        hooks.improve_opening(plan, self._writer(prompts), k=5, cps=8.5,
                              judge=self._judge({"오늘은 블랙홀": 5, "1783년": 9, "왜 블랙홀은": 6}), rng=random.Random(1))
        self.assertTrue(plan["hook"]["replaced"])
        self.assertTrue(plan["scenes"][0]["narration"].startswith("1783년"))
        self.assertEqual(plan["scenes"][-1]["narration"], "본론 7번째 이야기입니다. 중력이 모든 것을 끌어당깁니다.")
        self.assertEqual(len(plan["scenes"]), n_before - plan["hook"]["opening_scenes"] + 2)
        self.assertIn("1783년 미첼 확인", plan["fact_check"])
        self.assertTrue(plan["hook_strategy"].startswith("mini_story"))
        self.assertEqual(len(plan["hook"]["candidates"]), 2)  # original + mini_story (the 2 too-short ones are dropped)
        self.assertEqual(plan["scenes"][0]["visual"]["source"], "illustration")
        self.assertIn("빛도 못 나온다", prompts[0])

    def test_original_kept_when_not_clearly_better(self):
        plan = self._plan()
        hooks.improve_opening(plan, self._writer([]), k=5, cps=8.5,
                              judge=self._judge({"오늘은 블랙홀": 7, "1783년": 7.2, "왜 블랙홀은": 6}), rng=random.Random(2))
        self.assertFalse(plan["hook"]["replaced"])
        self.assertTrue(plan["scenes"][0]["narration"].startswith("오늘은"))
        self.assertTrue(next(c for c in plan["hook"]["candidates"] if c["type"] == "original")["chosen"])

    def test_judge_is_blind(self):
        plan, seen = self._plan(), []

        def judge(prompt):
            seen.append(prompt)
            return {"scores": []}
        hooks.improve_opening(plan, self._writer([]), k=5, judge=judge)
        self.assertNotIn("original", seen[0])
        self.assertNotIn("원래", seen[0].split("[후보]")[1])
        self.assertIn("error", plan["hook"])
        self.assertTrue(plan["scenes"][0]["narration"].startswith("오늘은"))

    def test_writer_failure_keeps_plan(self):
        plan = self._plan()

        def boom(prompt):
            raise RuntimeError("limit")
        hooks.improve_opening(plan, boom)
        self.assertIn("limit", plan["hook"]["error"])


class RetentionTest(TempHome):
    def _timeline(self):
        tl, t = [], 0.0
        for i in range(10):
            attrs = {"beat": "반례" if i in (4, 7) else "사례", "emotion": "curiosity", "source": "real", "type": "real",
                     "importance": 1, "quote": i == 6, "on_screen_text": False, "pause_after": False, "chapter_start": False}
            tl.append({"i": i, "start": t, "end": t + 30, "chapter": "c", "narration": f"s{i}", "attrs": attrs})
            t += 30
        return tl

    def _curve(self, steep=(4, 7)):
        """Retention that falls 1 point per scene, 6 points in the 'steep' scenes."""
        pts, r = [(0.0, 1.0)], 1.0
        for i in range(10):
            r -= 0.06 if i in steep else 0.01
            pts.append(((i + 1) / 10, r))
        out = []
        for k in range(1, 101):
            x = k / 100
            for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
                if x0 <= x <= x1:
                    out.append((x, y0 + (y1 - y0) * (x - x0) / (x1 - x0)))
                    break
        return out

    def test_scene_drops_and_lessons(self):
        rows = retention.per_scene(self._curve(), self._timeline())
        self.assertEqual(len(rows), 10)
        drops = {i: d for i, _, _, _, _, d in rows}
        self.assertGreater(drops[4], drops[3] * 4)
        for k in range(3):  # three videos with the same pattern
            pid = f"p{k}"
            db.execute("INSERT INTO productions(id,created_at,topic,status,plan) VALUES(?,?,?,?,?)",
                       (pid, db.now(), "t", "published", json.dumps({"scenes": [{"narration": f"문장 {i}"} for i in range(10)]})))
            self.assertEqual(retention.record(pid, self._curve(), self._timeline()), 10)
        L = retention.lessons()
        self.assertEqual(L["videos"], 3)
        self.assertEqual(L["worse"][0]["what"], "비트=반례")
        self.assertGreater(L["worse"][0]["vs_avg"], 50)
        self.assertIn("문장 4", [x["narration"] for x in L["worst_examples"]] + [x["narration"] for x in L["worst_examples"]])
        block = retention.prompt_block()
        self.assertIn("비트=반례", block)
        self.assertIn("30초 시점", block)
        mine = retention.for_production("p0")
        self.assertEqual(set(mine["worst"][:2]), {4, 7})

    def test_not_enough_data(self):
        self.assertIn("데이터 부족", retention.prompt_block())

    def test_measure_all_uses_saved_timeline(self):
        out = config.OUTPUT / "p1"
        out.mkdir(parents=True)
        scenes = [dict(scene(f"문장 {i}"), _dur=30.0) for i in range(10)]
        retention.save_timeline(out, scenes)
        db.execute("INSERT INTO productions(id,created_at,topic,status,youtube_id,privacy,output_dir,plan) VALUES(?,?,?,?,?,?,?,?)",
                   ("p1", db.now(), "t", "published", "YT1", "public", str(out), json.dumps({"scenes": scenes})))
        db.execute("INSERT INTO productions(id,created_at,topic,status,youtube_id,privacy) VALUES(?,?,?,?,?,?)",
                   ("p2", db.now(), "t", "uploaded_private", "YT2", "private"))
        got = []
        n = retention.measure_all(lambda vid, cid: got.append(vid) or self._curve())
        self.assertEqual((n, got), (1, ["YT1"]))
        tl = retention.load_timeline(out)
        self.assertEqual(tl[3]["start"], 90.0)
        self.assertTrue(tl[0]["attrs"]["chapter_start"])

    def test_youtube_curve_query(self):
        from autotube import youtube_upload
        svc = MagicMock()
        svc.reports().query().execute.return_value = {"rows": [[k / 100, 1 - k / 200] for k in range(1, 101)]}
        with patch.object(youtube_upload, "_service", return_value=svc):
            c = youtube_upload.retention_curve("VID", "ch")
        self.assertEqual(len(c), 100)
        kw = svc.reports().query.call_args.kwargs
        self.assertEqual(kw["dimensions"], "elapsedVideoTimeRatio")
        self.assertIn("audienceType==ORGANIC", kw["filters"])


class ShortsTest(TempHome):
    def test_fit_and_heuristic(self):
        durs = [10, 10, 10, 10, 10, 10]
        self.assertEqual(shorts._fit(0, 0, durs, 20, 35), (0, 1))
        self.assertEqual(shorts._fit(0, 5, durs, 20, 35), (0, 2))
        self.assertIsNone(shorts._fit(0, 0, [50, 50], 20, 35))
        sc = [scene(f"{i}", importance=3 if i in (0, 3) else 1) for i in range(6)]
        got = shorts.heuristic(sc, durs, 2, 20, 30)
        self.assertEqual([(g["from"], g["to"]) for g in got], [(0, 1), (3, 4)])

    def test_ai_pick_is_checked(self):
        sc = [scene(f"장면 {i}") for i in range(8)]
        durs = [8.0] * 8

        def gen(prompt):
            self.assertIn("8 · 8.0초", prompt)
            return {"shorts": [{"from": 1, "to": 8, "title": "너무 김", "top_text": "?"},   # 64 s -> trimmed
                               {"from": 2, "to": 3, "title": "겹침", "top_text": "?"},     # overlaps -> dropped
                               {"from": 6, "to": 8, "title": "좋음 #태그", "top_text": "왜?"}]}
        got = shorts.pick({"title": "t"}, sc, durs, 3, 20, 40, gen)
        self.assertEqual([(g["from"], g["to"]) for g in got], [(0, 4), (5, 7)])
        self.assertEqual(shorts._title(got[1], {"title": "t"}), "좋음 #Shorts")


class FakeShortProviders(FakeProviders):
    uploads = []

    @staticmethod
    def shorts_pick(prompt):
        return {"shorts": [{"from": 1, "to": 3, "title": "첫 쇼츠", "top_text": "이게 가능해?", "why": "질문"},
                           {"from": 4, "to": 6, "title": "둘째 쇼츠", "top_text": "결론", "why": "정리"}]}

    @staticmethod
    def upload(path, title, desc, tags, privacy="private", channel_id=None, **kw):
        FakeShortProviders.uploads.append({"path": path, "title": title, "desc": desc, "tags": tags, "privacy": privacy})
        return f"VID{len(FakeShortProviders.uploads)}", []


class ShortsPipelineTest(TempHome):
    def setUp(self):
        super().setUp()
        config.save_settings({"production": {"resolution": "640x360", "fps": 24, "max_ai_video_clips": 1, "voice_check": False,
                                             "auto_upload_private": True, "shorts_min_seconds": 4, "shorts_max_seconds": 8,
                                             "shorts_resolution": "360x640"}})
        FakeShortProviders.uploads = []

    def test_render_shorts_timeline_and_upload(self):
        pid = producer.create("쇼츠 주제", "", 1)
        with patch("autotube.youtube_upload.connected", return_value=True):
            self.assertEqual(producer.run(pid, FakeShortProviders), "uploaded_private")
        p = db.one("SELECT * FROM productions WHERE id=?", (pid,))
        out = Path(p["output_dir"])
        items = shorts.load(out)
        self.assertEqual([(x["from_scene"], x["to_scene"]) for x in items], [(1, 3), (4, 6)])
        for x in items:
            f = out / "shorts" / x["file"]
            self.assertTrue(f.is_file())
            probe = subprocess.run([config.ffmpeg_bin(), "-hide_banner", "-i", str(f)], capture_output=True, text=True).stderr
            self.assertIn("360x640", probe)
            self.assertIn("Audio:", probe)
            self.assertAlmostEqual(media.duration(f), x["seconds"], delta=0.4)
            self.assertTrue(x["title"].endswith("#Shorts"))
        self.assertTrue((out / "shorts" / "short1.srt").read_text(encoding="utf-8").strip())
        # long-form first, then both Shorts private with a link to it, without chapter timestamps
        self.assertEqual(len(FakeShortProviders.uploads), 3)
        long_id = p["youtube_id"]
        sh = FakeShortProviders.uploads[1]
        self.assertIn(f"https://youtu.be/{long_id}", sh["desc"])
        self.assertFalse(re.search(r"^\d\d:\d\d ", sh["desc"], re.M))
        self.assertIn("Shorts", sh["tags"])
        self.assertEqual(sh["privacy"], "private")
        self.assertEqual([x["youtube_id"] for x in shorts.load(out)], ["VID2", "VID3"])
        self.assertIn("쇼츠 1", p["log"])
        tl = retention.load_timeline(out)
        self.assertEqual(len(tl), 6)
        self.assertAlmostEqual(tl[-1]["end"], media.duration(out / "video.mp4"), delta=0.6)
        # publish one Short
        set_calls = []
        shorts.set_privacy(out, 2, "public", setter=lambda vid, pr, channel_id=None: set_calls.append((vid, pr)))
        self.assertEqual(set_calls, [("VID3", "public")])
        self.assertEqual(shorts.load(out)[1]["privacy"], "public")

    def test_shorts_failure_never_stops_the_video(self):
        class Broken(FakeShortProviders):
            @staticmethod
            def shorts_pick(prompt):
                return {"shorts": [{"from": 1, "to": 3}]}
        pid = producer.create("x", "", 1)
        with patch("autotube.media.vertical_short", side_effect=media.MediaError("boom")):
            self.assertEqual(producer.run(pid, Broken), "ready")
        self.assertIn("쇼츠 만들기 실패", db.one("SELECT log FROM productions WHERE id=?", (pid,))["log"])

    def test_off_switch(self):
        config.save_settings({"production": {"shorts_per_video": 0}})
        pid = producer.create("x", "", 1)
        self.assertEqual(producer.run(pid, FakeShortProviders), "ready")
        self.assertEqual(shorts.load(Path(db.one("SELECT output_dir FROM productions WHERE id=?", (pid,))["output_dir"])), [])


class PolishWiringTest(TempHome):
    def test_planner_runs_package_then_hooks(self):
        from autotube import planner
        config.save_settings({"production": {"min_real_ratio": 0, "self_review": False}})
        order = []

        def gen(prompt):
            if "제목·썸네일 글자 조합" in prompt:
                order.append("package")
                return {"thumb_options": ["빛의 감옥"], "pairs": [
                    {"title": "블랙홀 이야기", "thumb": "빛의 감옥", "curiosity": 9, "clarity": 9, "complement": 9, "honesty": 9, "mobile": 9}]}
            if "서로 다른 방식으로" in prompt:
                order.append("hooks")
                self.assertIn("빛의 감옥", prompt)  # the opening is written for the chosen thumbnail
                return {"candidates": []}
            order.append("plan")
            return {"title": "블랙홀 이야기", "titles": ["블랙홀 이야기"], "thumbnail": {"text": "블랙홀 이야기"},
                    "scenes": [scene("문장입니다 " * 10) for _ in range(12)]}
        plan = planner.make_plan("블랙홀", minutes=1, generate=gen)
        self.assertEqual(order[-2:], ["package", "hooks"])
        self.assertEqual(plan["thumbnail"]["text"], "빛의 감옥")
        self.assertIn("error", plan["hook"])

    def test_off_switches(self):
        from autotube import planner
        config.save_settings({"production": {"package_check": False, "hook_candidates": 0}})
        plan = {"title": "t", "scenes": [scene("a")], "_stats": {}}
        self.assertNotIn("package", planner.polish(plan, "t", lambda p: 1 / 0))
