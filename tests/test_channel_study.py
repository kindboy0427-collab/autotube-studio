"""Whole-channel learning: compare each video with the channel's own median."""
import json
from unittest.mock import patch

from autotube import channel_study, config, db, planner
from tests.base import TempHome


def yt_fake(url, params=None, **kw):
    if url.endswith("/channels"):
        if params.get("forHandle"):
            return {"items": [{"id": "UC" + "a" * 22}]}
        return {"items": [{"id": "UC" + "a" * 22, "snippet": {"title": "과학채널", "description": ""},
                           "statistics": {"subscriberCount": "50000", "videoCount": "30"},
                           "contentDetails": {"relatedPlaylists": {"uploads": "UUx"}}}]}
    if url.endswith("/playlistItems"):
        return {"items": [{"contentDetails": {"videoId": f"vid{i:08d}"}} for i in range(30)]}
    if url.endswith("/videos"):
        ids = params["id"].split(",")
        return {"items": [{"id": v, "snippet": {"title": f"제목 {v}", "channelId": "UC" + "a" * 22,
                                                "publishedAt": f"2026-0{1 + i % 9}-1{i % 9}T00:00:00Z"},
                           "contentDetails": {"duration": "PT12M" if i % 5 else "PT50S"},
                           "statistics": {"viewCount": str(100000 if i in (1, 2) else 10000 + i * 100)}}
                          for i, v in enumerate(ids)]}
    raise AssertionError(url)


class Study(TempHome):
    def test_study_ranks_against_channel_median_and_queues(self):
        config.set_secrets({"YOUTUBE_API_KEY": "y"})
        seen = {}

        def gen(prompt):
            seen["prompt"] = prompt
            return {"one_line": "질문형 제목이 터짐", "title_patterns": ["왜 ~일까"], "borrow": ["반전"], "gaps": ["양자"]}
        with patch("autotube.http.get_json", side_effect=yt_fake):
            r = channel_study.study("https://www.youtube.com/@science", "tech", queue_top=5, queue_bottom=2, generate=gen)
        self.assertEqual(r["channel"]["title"], "과학채널")
        self.assertEqual(r["studied"], 24)  # shorts (50 s) excluded
        self.assertEqual({t["id"] for t in r["top"][:2]}, {"vid00000001", "vid00000002"})
        self.assertGreater(r["top"][0]["ratio"], 5)
        self.assertIn("평소보다 크게 터진", seen["prompt"])
        self.assertEqual(r["queued"], 7)
        q = db.rows("SELECT * FROM videos WHERE query LIKE 'channel:%'")
        self.assertEqual(len(q), 7)
        self.assertTrue(all(v["niche"] == "tech" for v in q))
        notes = json.loads(planner._studies("tech"))["채널별"]
        self.assertEqual(notes[0]["why"], "질문형 제목이 터짐")
        self.assertEqual(planner._studies("other"), "(아직 없음)")

    def test_resolve_formats(self):
        self.assertEqual(channel_study.resolve("https://www.youtube.com/channel/UC" + "b" * 22), "UC" + "b" * 22)


class Synth(TempHome):
    def test_synthesize_blends_studies_into_gap_topics(self):
        config.set_secrets({"YOUTUBE_API_KEY": "y"})
        with patch("autotube.http.get_json", side_effect=yt_fake):
            channel_study.study("@a", "", queue_top=2, queue_bottom=0,
                                generate=lambda p: {"one_line": "반전", "borrow": ["반례"], "gaps": ["양자"]})
        sent = {}

        def gen(prompt):
            sent["p"] = prompt
            return {"feel": "차분한 반전 에세이", "works": ["질문형 제목"], "fails": ["목록 나열"],
                    "gap_topics": [{"topic": "반도체 공장은 왜 사막에?", "why_gap": "아무도 안 다룸", "evidence": ["과학채널 터진 영상"],
                                    "format": "reversal_essay"}, {"topic": "형식 틀림", "format": "zzz"}]}
        out = channel_study.synthesize("", generate=gen)
        self.assertIn("과학채널", sent["p"])
        self.assertEqual(out["gap_topics"][0]["format"], "reversal_essay")
        self.assertEqual(out["gap_topics"][1]["format"], "")
        self.assertEqual(channel_study.get_synth("tech")["feel"], "차분한 반전 에세이")  # falls back to the common synthesis
        self.assertIn("잘된것", planner._studies("tech"))

    def test_synthesize_needs_studies(self):
        with self.assertRaises(ValueError):
            channel_study.synthesize("", generate=lambda p: {})
