"""End-to-end: fake AI providers, REAL ffmpeg rendering -> playable MP4 + SRT + thumbnail."""
import json
import subprocess
from pathlib import Path

from PIL import Image

from autotube import config, db, media, producer
from tests.base import TempHome


def ff(args):
    subprocess.run([config.ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-y"] + args, check=True)


class FakeProviders(producer.Providers):
    calls = {"ai_video": 0, "ai_image": 0, "stock_video": 0, "tts": 0, "upload": 0, "voice": None, "channel": None}
    fail_ai_video = False

    @staticmethod
    def plan(topic, instructions, minutes, production_id=None, channel=None, script_format=None):
        FakeProviders.calls["channel"] = channel
        FakeProviders.calls["format"] = script_format
        scenes = []
        for i in range(6):
            scenes.append({"chapter": "도입" if i < 2 else "본론" if i < 5 else "마무리",
                           "narration": f"{i + 1}번째 장면입니다. 시청자가 궁금해할 내용을 짧게 설명합니다.",
                           "on_screen_text": "핵심 포인트" if i == 1 else "", "importance": 3 if i in (0, 2) else 1,
                           "pause_after": i == 3, "emotion": "curiosity",
                           "visual": {"prompt": "a mountain at sunrise", "stock_query": "mountain sunrise"}})
        return {"title": "테스트 롱폼 제목", "titles": ["테스트 롱폼 제목", "다른 각도", "세 번째"],
                "thumbnail": {"text": "충격 진실", "image_prompt": "dramatic sky"}, "description": "설명입니다",
                "tags": ["테스트"], "music_mood": "calm", "pinned_comment": "여러분 생각은?",
                "features_used": ["hook:question"], "scenes": scenes, "sources": ["NASA, 2024"],
                "_stats": {"scenes": 6, "chars": 200, "target_chars": 200, "est_minutes": 0.5}}

    @staticmethod
    def tts(text, out_path, production_id=None, voice=None, style=""):
        FakeProviders.calls["tts"] += 1
        if not isinstance(FakeProviders.calls.get("voices"), list):
            FakeProviders.calls["voices"] = []
        FakeProviders.calls["voices"].append(voice)
        FakeProviders.calls["voice"] = voice
        FakeProviders.calls["style"] = style
        ff(["-f", "lavfi", "-i", "sine=frequency=440:duration=1.6", "-ac", "1", out_path])
        return out_path, [], "fake"

    @staticmethod
    def ai_video(prompt, out_path, production_id=None):
        FakeProviders.calls["ai_video"] += 1
        if FakeProviders.fail_ai_video:
            raise RuntimeError("video model down")
        ff(["-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=24:duration=1", "-pix_fmt", "yuv420p", out_path])
        return out_path

    @staticmethod
    def ai_image(prompt, out_path, production_id=None):
        FakeProviders.calls["ai_image"] += 1
        Image.new("RGB", (1408, 768), (40, 90, 160)).save(out_path)
        return out_path

    @staticmethod
    def stock_video(q, d, used, out_path):
        FakeProviders.calls["stock_video"] += 1
        ff(["-f", "lavfi", "-i", "testsrc=size=1920x1080:rate=30:duration=1", "-pix_fmt", "yuv420p", out_path])
        return out_path, {"source": "pexels", "id": len(used), "author": "Tester"}

    @staticmethod
    def stock_photo(q, used, out_path):
        Image.new("RGB", (1600, 900), (120, 60, 30)).save(out_path)
        return out_path, {"source": "pexels", "id": 99, "author": "Photo"}

    @staticmethod
    def music(mood, seconds, out_path, production_id=None):
        ff(["-f", "lavfi", "-i", "sine=frequency=220:duration=4", out_path])
        return out_path, "fake"


class PipelineTest(TempHome):
    def setUp(self):
        super().setUp()
        config.save_settings({"production": {"resolution": "640x360", "fps": 24, "max_ai_video_clips": 2,
                                             "ai_image_ratio": 0.5, "auto_upload_private": True}})
        for k in FakeProviders.calls:
            FakeProviders.calls[k] = 0
        FakeProviders.fail_ai_video = False

    def test_full_render(self):
        pid = producer.create("테스트 주제", "", 1)
        self.assertEqual(producer.run(pid, FakeProviders), "ready")  # no YouTube token -> ready, not uploaded
        p = db.one("SELECT * FROM productions WHERE id=?", (pid,))
        video = Path(p["video_path"])
        self.assertTrue(video.is_file())
        dur = media.duration(video)
        self.assertAlmostEqual(dur, 6 * (1.6 + 0.35) + 1.2, delta=0.6)  # one pause_after scene
        self.assertEqual(FakeProviders.calls["ai_video"], 2)          # only the 2 most important scenes
        self.assertEqual(FakeProviders.calls["ai_image"], 2 + 1)      # half of the rest + thumbnail background
        self.assertEqual(FakeProviders.calls["stock_video"], 2)
        srt = Path(p["srt_path"]).read_text(encoding="utf-8")
        self.assertIn("1번째", srt)
        self.assertIn("-->", srt)
        self.assertTrue(Path(p["thumbnail_path"]).is_file())
        self.assertEqual(Image.open(p["thumbnail_path"]).size, (1280, 720))
        desc = Path(p["output_dir"], "description.txt").read_text(encoding="utf-8")
        self.assertIn("AI 도구", desc)
        self.assertIn("Pexels", desc)
        # has audio stream
        probe = subprocess.run([config.ffmpeg_bin(), "-hide_banner", "-i", str(video)], capture_output=True, text=True).stderr
        self.assertIn("Audio:", probe)
        self.assertIn("640x360", probe)
        self.assertIn("영상에 입힘", p["log"])

    def test_fallback_and_resume(self):
        FakeProviders.fail_ai_video = True
        pid = producer.create("두 번째", "", 1)
        self.assertEqual(producer.run(pid, FakeProviders), "ready")
        log = db.one("SELECT log FROM productions WHERE id=?", (pid,))["log"]
        self.assertIn("ai_image", log)  # AI video failed -> fell back to AI image
        before = dict(FakeProviders.calls)
        self.assertEqual(producer.run(pid, FakeProviders), "ready")  # rerun reuses cached assets
        self.assertEqual(FakeProviders.calls["tts"], before["tts"])
        self.assertEqual(FakeProviders.calls["stock_video"], before["stock_video"])

    def test_upload_called_when_connected(self):
        from autotube import youtube_upload
        from autotube import channels
        channels.save({"id": "space", "name": "우주", "voice_id": "VOICE1"})
        youtube_upload.token_file("space").write_text("{}", encoding="utf-8")
        got = {}

        class P(FakeProviders):
            @staticmethod
            def upload(video, title, desc, tags, privacy, thumbnail, srt, channel_id=None):
                got.update(title=title, privacy=privacy, thumbnail=thumbnail, channel_id=channel_id)
                return "abcDEF12345", []
        pid = producer.create("세 번째", "", 1, channel_id="space")
        self.assertEqual(producer.run(pid, P), "uploaded_private")
        self.assertEqual(got["privacy"], "private")
        self.assertEqual(got["channel_id"], "space")
        self.assertEqual(FakeProviders.calls["voice"], {"engine": "elevenlabs", "ref": "VOICE1"})  # v1.2 channel id still works
        self.assertEqual(FakeProviders.calls["channel"]["name"], "우주")
        no_chan = producer.create("네 번째", "", 1)  # a different channel's login must not be used
        self.assertEqual(producer.run(no_chan, P), "ready")
        self.assertEqual(db.one("SELECT youtube_id FROM productions WHERE id=?", (pid,))["youtube_id"], "abcDEF12345")

    def test_channel_voice_accent_pause_and_credits(self):
        from autotube import channels
        import json as _j
        channels.save({"id": "body", "name": "인체", "tone": "친근한 설명",
                       "voice": {"engine": "gemini", "ref": "voice_me", "accent": {"engine": "elevenlabs", "ref": "EL_ME"}}})
        FakeProviders.calls["voices"] = []
        pid = producer.create("인체의 신비", "", 1, channel_id="body", script_format="metaphor_doc")
        self.assertEqual(producer.run(pid, FakeProviders), "ready")
        self.assertEqual(FakeProviders.calls["format"], "metaphor_doc")
        vs = FakeProviders.calls["voices"]
        self.assertEqual(vs[0], {"engine": "elevenlabs", "ref": "EL_ME"})   # importance 3 -> accent voice
        self.assertEqual(vs[1], {"engine": "gemini", "ref": "voice_me"})
        self.assertIn("친근한 설명", FakeProviders.calls["style"])
        plan = _j.loads(db.one("SELECT plan FROM productions WHERE id=?", (pid,))["plan"])
        out = config.OUTPUT / pid
        desc = (out / "description.txt").read_text(encoding="utf-8")
        self.assertIn("NASA, 2024", desc)
        self.assertIn("AI 음성", desc)
        self.assertIn("Pexels", desc)
        dur3 = media.duration(out / "work" / "s003_voice_pad.wav")
        dur2 = media.duration(out / "work" / "s002_voice_pad.wav")
        self.assertGreater(dur3 - dur2, 1.0)  # pause_after scene holds ~1.2 s longer

    def test_visual_assignment(self):
        scenes = [{"importance": imp, "chapter": ch, "visual": {}} for imp, ch in
                  [(3, "a"), (1, "a"), (2, "b"), (1, "b"), (3, "c"), (1, "c"), (1, "c"), (3, "c")]]
        producer.assign_visual_types(scenes, 3, 0.5)
        types = [s["visual"]["type"] for s in scenes]
        self.assertEqual(types.count("ai_video"), 3)
        self.assertEqual(types[0], "ai_video")
        self.assertEqual(types.count("ai_image"), 2)
