"""Request/response shapes of each external API (network mocked)."""
import base64
import json
import os
from pathlib import Path
from unittest.mock import patch

from autotube import config, gemini, http, llm, stock, tts, youtube_data, budget
from tests.base import TempHome


class Providers(TempHome):
    def setUp(self):
        super().setUp()
        config.set_secrets({"GEMINI_API_KEY": "g", "YOUTUBE_API_KEY": "y", "ELEVENLABS_API_KEY": "e", "PEXELS_API_KEY": "p"})

    def test_gemini_watch_uses_youtube_url(self):
        sent = {}

        def fake(url, body, headers=None, **kw):
            sent.update(url=url, body=body, headers=headers)
            return {"steps": [{"type": "thought", "content": [{"type": "thought", "text": "x"}]},
                              {"type": "model_output", "content": [{"type": "text", "text": '{"summary":"ok"}'}]}],
                    "usage": {"total_input_tokens": 60000, "total_output_tokens": 800}}
        with patch("autotube.http.post_json", side_effect=fake):
            out = gemini.watch_youtube("https://www.youtube.com/watch?v=abcdefghijk", "분석", 600)
        self.assertEqual(out, '{"summary":"ok"}')
        self.assertTrue(sent["url"].endswith("/v1beta/interactions"))
        self.assertEqual(sent["body"]["input"][1], {"type": "video", "uri": "https://www.youtube.com/watch?v=abcdefghijk"})
        self.assertEqual(sent["headers"]["x-goog-api-key"], "g")
        self.assertGreater(budget.spent_today(), 0)

    def test_gemini_image_skips_thought_parts(self):
        png = base64.b64encode(b"REALIMAGE").decode()
        res = {"candidates": [{"content": {"parts": [{"inlineData": {"data": base64.b64encode(b"DRAFT").decode()}, "thought": True},
                                                     {"inlineData": {"mimeType": "image/png", "data": png}}]}}]}
        out = Path(self.tmp) / "i.png"
        with patch("autotube.http.post_json", return_value=res) as m:
            gemini.generate_image("cat", str(out))
        self.assertEqual(out.read_bytes(), b"REALIMAGE")
        self.assertIn(":generateContent", m.call_args[0][0])
        self.assertEqual(m.call_args[0][1]["generationConfig"]["responseModalities"], ["IMAGE"])

    def test_gemini_video_inline_and_uri(self):
        out = Path(self.tmp) / "v.mp4"
        inline = {"steps": [{"type": "model_output", "content": [{"type": "video", "data": base64.b64encode(b"MP4").decode()}]}]}
        with patch("autotube.http.post_json", return_value=inline) as m:
            gemini.generate_video("a dog", str(out))
        self.assertEqual(out.read_bytes(), b"MP4")
        self.assertEqual(m.call_args[0][1]["model"], "gemini-omni-1.1-flash")
        uri = {"steps": [{"type": "model_output", "content": [{"type": "video", "uri": "https://x/v1beta/files/abc123"}]}]}
        states = iter([{"state": "PROCESSING"}, {"state": "ACTIVE"}])
        with patch("autotube.http.post_json", return_value=uri), patch("autotube.http.get_json", side_effect=lambda *a, **k: next(states)), \
                patch("autotube.http.download", side_effect=lambda url, path, headers=None: Path(path).write_bytes(b"URI") or path) as dl, \
                patch("time.sleep"):
            gemini.generate_video("a dog", str(out))
        self.assertIn("files/abc123:download", dl.call_args[0][0])

    def test_elevenlabs_words(self):
        text = "안녕 하세요"
        res = {"audio_base64": base64.b64encode(b"MP3").decode(),
               "alignment": {"characters": list(text), "character_start_times_seconds": [i * 0.1 for i in range(len(text))],
                             "character_end_times_seconds": [i * 0.1 + 0.1 for i in range(len(text))]}}
        with patch("autotube.http.post_json", return_value=res) as m:
            _, words = tts.elevenlabs(text, str(Path(self.tmp) / "a.mp3"))
        self.assertEqual([w[2] for w in words], ["안녕", "하세요"])
        self.assertIn("/with-timestamps", m.call_args[0][0])

    def test_tts_fallback_order(self):
        boom = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("401"))
        with patch.dict(tts.PROVIDERS, {"gemini": boom, "elevenlabs": boom,
                                        "edge": lambda t, p, production_id=None, **k: (p, [(0, 1, "x")])}):
            _, words, engine = tts.synthesize("x", "o.mp3")
        self.assertEqual(engine, "edge")

    def test_youtube_parsing(self):
        res = {"items": [{"id": "abcdefghijk", "snippet": {"title": "T", "channelId": "C", "channelTitle": "CH",
                                                           "publishedAt": "2026-10-01T00:00:00Z", "thumbnails": {"high": {"url": "u"}}},
                          "contentDetails": {"duration": "PT1H2M3S"}, "statistics": {"viewCount": "1000", "likeCount": "10"}}]}
        with patch("autotube.http.get_json", return_value=res):
            v = youtube_data.videos(["abcdefghijk", "bad id"])[0]
        self.assertEqual(v["duration_seconds"], 3723)
        self.assertEqual(v["views"], 1000)
        self.assertIsNone(v["comments"])

    def test_llm_prefers_openai_and_falls_back(self):
        config.set_secrets({"OPENAI_API_KEY": "o"})
        self.assertEqual(llm.provider(), "openai")
        with patch("autotube.http.post_json", side_effect=http.HttpError(500, "x")), \
                patch("autotube.gemini.generate_text", return_value='{"a":1}') as g:
            self.assertEqual(llm.generate_json("p"), {"a": 1})
        g.assert_called_once()

    def test_parse_json_loose(self):
        self.assertEqual(http.parse_json_loose('```json\n{"a":[1,2]}\n```'), {"a": [1, 2]})
        self.assertEqual(http.parse_json_loose('설명 {"a":{"b":"잘린'), {"a": {"b": "잘린"}})

    def test_pexels_choice(self):
        res = {"videos": [{"id": 1, "duration": 3, "video_files": [{"file_type": "video/mp4", "width": 1920, "link": "L1"}]},
                          {"id": 2, "duration": 12, "video_files": [{"file_type": "video/mp4", "width": 1920, "link": "L2"},
                                                                     {"file_type": "video/mp4", "width": 640, "link": "small"}]}]}
        with patch("autotube.http.get_json", return_value=res), patch("autotube.http.download") as dl:
            used = set()
            stock.find_video("q", 8, used, "o.mp4")
        self.assertEqual(dl.call_args[0][0], "L2")
        self.assertEqual(used, {2})


class FreeLearningKey(TempHome):
    def test_watch_uses_free_key_and_costs_nothing(self):
        config.set_secrets({"GEMINI_API_KEY": "paid", "GEMINI_FREE_API_KEY": "free"})
        sent = {}

        def fake(url, body, headers=None, **kw):
            sent["key"] = headers["x-goog-api-key"]
            return {"steps": [{"type": "model_output", "content": [{"type": "text", "text": '{"summary":"ok"}'}]}]}
        with patch("autotube.http.post_json", side_effect=fake):
            gemini.watch_youtube("https://www.youtube.com/watch?v=abcdefghijk", "분석", 600)
        self.assertEqual(sent["key"], "free")
        self.assertEqual(budget.spent_today(), 0)
