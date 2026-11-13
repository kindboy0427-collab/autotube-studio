"""v1.3: Gemini TTS, my-voice registration, audition, script formats, taste references, alignment."""
import base64
import io
import json
import subprocess
import wave
from pathlib import Path
from unittest.mock import patch

from autotube import align, config, db, learner, planner, tts, voices, watcher
from tests.base import TempHome


def pcm_b64(seconds=1.0, rate=24000):
    return base64.b64encode(b"\x00\x10" * int(rate * seconds)).decode()


def make_wav(path, seconds):
    subprocess.run([config.ffmpeg_bin(), "-y", "-loglevel", "error", "-f", "lavfi", "-i",
                    f"sine=frequency=300:duration={seconds}", str(path)], check=True)
    return path


class GeminiTTS(TempHome):
    def setUp(self):
        super().setUp()
        config.set_secrets({"GEMINI_API_KEY": "g"})

    def test_request_shape_pcm_and_cost(self):
        config.save_settings({"production": {"custom_voice_style": "light"}})
        sent = {}

        def fake(url, body, headers=None, **kw):
            sent.update(url=url, body=body, headers=headers)
            return {"steps": [{"type": "model_output", "content": [
                {"type": "audio", "mime_type": "audio/L16;codec=pcm;rate=24000", "data": pcm_b64(2.0)}]}]}
        out = Path(self.tmp) / "n.mp3"
        with patch("autotube.http.post_json", side_effect=fake):
            path, words, engine = tts.synthesize("안녕하세요 반갑습니다", str(out), voice={"engine": "gemini", "ref": "voice_abc"},
                                                 style=tts.style_for("surprise", "차분한 다큐"))
        self.assertEqual(engine, "gemini")
        self.assertTrue(sent["url"].endswith("/v1beta/interactions"))
        b = sent["body"]
        self.assertEqual(b["model"], "gemini-3.8-flash-tts")
        self.assertEqual(b["generation_config"]["speech_config"], [{"voice": "voice_abc"}])
        self.assertEqual(b["response_format"], {"type": "audio"})
        ann = b["input"][0]["content"][0]["annotations"][0]
        self.assertEqual(ann["type"], "speech_metadata")
        self.assertIn("surprised", ann["style"])
        from autotube import media
        self.assertAlmostEqual(media.duration(out), 2.0, delta=0.2)
        row = db.one("SELECT units, cost_usd FROM usage WHERE service='gemini_tts'")
        self.assertAlmostEqual(row["cost_usd"], 2.0 / 60 * 0.0135, places=4)

    def test_same_voice_never_switches_engine(self):
        with patch("autotube.http.post_json", side_effect=RuntimeError("down")), \
                patch.object(tts, "edge", side_effect=AssertionError("must not fall back to another voice")):
            with self.assertRaises(tts.TTSError):
                tts.synthesize("문장", str(Path(self.tmp) / "x.mp3"), voice={"engine": "gemini", "ref": "voice_me"})

    def test_auto_order_prefers_gemini(self):
        self.assertEqual(tts.choose()[0], "gemini")

    def test_parse_voice_legacy(self):
        self.assertEqual(tts.parse_voice("ABC123"), {"engine": "elevenlabs", "ref": "ABC123"})
        self.assertEqual(tts.parse_voice("gemini:voice_x"), {"engine": "gemini", "ref": "voice_x"})
        self.assertEqual(tts.parse_voice('{"engine":"gemini","ref":"Kore"}'), {"engine": "gemini", "ref": "Kore"})


class MyVoice(TempHome):
    def setUp(self):
        super().setUp()
        config.set_secrets({"GEMINI_API_KEY": "g", "ELEVENLABS_API_KEY": "e"})

    def test_register_gemini_converts_and_sends_both_clips(self):
        src = make_wav(Path(self.tmp) / "me.m4a.wav", 40)
        con = make_wav(Path(self.tmp) / "consent.wav", 8)
        sent = {}

        def fake(url, body, headers=None, **kw):
            sent.update(url=url, body=body)
            return {"id": "voice_123"}
        with patch("autotube.http.post_json", side_effect=fake):
            v = voices.register_gemini("내 목소리", src, con)
        self.assertEqual(v["ref"], "voice_123")
        self.assertTrue(sent["url"].endswith("/v1beta/voices"))
        rep = sent["body"]["voice"]["replicated"]
        self.assertEqual(sent["body"]["voice"]["type"], "replicated")
        self.assertTrue(sent["body"]["store"])
        w = wave.open(io.BytesIO(base64.b64decode(rep["source_audio"]["data"])))
        self.assertEqual((w.getframerate(), w.getnchannels(), w.getsampwidth()), (24000, 1, 2))
        self.assertLessEqual(w.getnframes() / 24000, 28.1)  # trimmed to the 10~30 s window
        self.assertIn("consent_audio", rep)
        self.assertEqual(voices.all_voices()["mine"][0]["spec"], {"engine": "gemini", "ref": "voice_123"})

    def test_register_gemini_rejects_short_sample(self):
        src = make_wav(Path(self.tmp) / "s.wav", 4)
        con = make_wav(Path(self.tmp) / "c.wav", 6)
        with self.assertRaises(voices.VoiceError):
            voices.register_gemini("짧음", src, con)

    def test_register_elevenlabs_multipart(self):
        src = make_wav(Path(self.tmp) / "me.wav", 30)
        sent = {}

        def fake(method, url, data=None, headers=None, **kw):
            sent.update(url=url, data=data, headers=headers)
            return {"voice_id": "EL1"}
        with patch("autotube.http.request", side_effect=fake):
            v = voices.register_elevenlabs("내 목소리", [src])
        self.assertEqual(v["ref"], "EL1")
        self.assertTrue(sent["url"].endswith("/v1/voices/add"))
        self.assertIn("multipart/form-data", sent["headers"]["Content-Type"])
        self.assertIn(b'name="files"', sent["data"])

    def test_audition_collects_errors_per_voice(self):
        def synth(text, out, voice=None, style="", order=None):
            if voice["engine"] == "edge":
                raise RuntimeError("offline")
            Path(out).write_bytes(b"ID3")
        r = voices.audition("문장", [{"label": "A", "spec": {"engine": "gemini", "ref": "Kore"}},
                                    {"label": "B", "spec": {"engine": "edge", "ref": ""}}], synth=synth)
        self.assertIn("file", r[0])
        self.assertIn("offline", r[1]["error"])


class Formats(TempHome):
    def test_prompt_contains_chosen_format_beats(self):
        p = planner.build_prompt("블랙홀", "", 10, [], None, "staged_scenario")
        self.assertIn("단계형 시나리오", p)
        self.assertIn("콜드 오픈", p)
        auto = planner.build_prompt("블랙홀", "", 10, [], None, "auto")
        for k in planner.FORMATS:
            self.assertIn(k, auto)

    def test_validate_keeps_sources_and_pause(self):
        plan = {"title": "t", "format": "catalog", "sources": ["NASA 2024", ""],
                "scenes": [{"narration": f"문장 {i}", "pause_after": "true" if i == 2 else False} for i in range(6)]}
        v = planner.validate(plan, 3)
        self.assertEqual(v["sources"], ["NASA 2024"])
        self.assertTrue(v["scenes"][2]["pause_after"])
        self.assertFalse(v["scenes"][0]["pause_after"])
        self.assertIsNone(planner.validate({**plan, "format": "zzz"}, 3)["format"])


class References(TempHome):
    def test_seed_once_and_watched_first(self):
        self.assertEqual(watcher.seed_references(), 4)
        self.assertEqual(watcher.seed_references(), 0)
        db.execute("INSERT INTO videos(id,title,status,outlier_score) VALUES('zzzzzzzzzzz','other','queued',99999)")
        picked = [v["id"] for v in watcher._pick_queue(4)]
        self.assertEqual(set(picked), {r["id"] for r in watcher.REFERENCES})

    def test_favourite_weighs_more_and_shows_in_examples(self):
        for i, fav in enumerate([1, 0, 0, 0]):
            vid = f"vid{i:08d}"
            db.execute("INSERT INTO videos(id,title,status,favorite,outlier_score,views_per_day) VALUES(?,?,?,?,?,?)",
                       (vid, f"영상{i}", "watched", fav, 1.0 + i, 10))
            feats = ["hook:reversal"] if fav else ["hook:question"]
            db.execute("INSERT INTO analyses(video_id,created_at,features,raw,summary,topic) VALUES(?,?,?,?,?,?)",
                       (vid, db.now(), json.dumps(feats), json.dumps({"summary": "s"}), "s", "t"))
        learner.update_patterns()
        pats = {r["feature"]: r for r in db.rows("SELECT * FROM patterns")}
        self.assertEqual(pats["hook:reversal"]["n"], learner.FAV_WEIGHT)
        self.assertGreater(pats["hook:reversal"]["lift"], pats["hook:question"]["lift"])
        ex = learner.relevant_examples("무관한 주제", 3)
        self.assertTrue(ex[0]["taste_reference"])


class Align(TempHome):
    def test_map_script_shows_script_words(self):
        rec = [(0.0, 0.5, "블랙홀은"), (0.6, 1.0, "사실")]
        out = align.map_script("블랙홀은 사실", rec, 1.2)
        self.assertEqual([w for _, _, w in out], ["블랙홀은", "사실"])
        self.assertLessEqual(out[-1][1], 1.2)

    def test_words_for_without_whisper_returns_none(self):
        with patch.object(align, "available", return_value=False):
            self.assertIsNone(align.words_for("문장", "x.mp3", 1.0))


class CustomVoiceChunks(TempHome):
    def setUp(self):
        super().setUp()
        config.set_secrets({"GEMINI_API_KEY": "g"})

    def test_my_voice_is_generated_sentence_by_sentence(self):
        config.save_settings({"production": {"custom_voice_style": "light"}})
        bodies = []

        def fake(url, body, headers=None, **kw):
            bodies.append(body)
            return {"steps": [{"type": "model_output", "content": [
                {"type": "audio", "mime_type": "audio/L16;rate=24000", "data": pcm_b64(1.0)}]}]}
        text = "밤하늘의 별은 왜 반짝일까요? 사실 별빛이 흔들리는 건 별 때문이 아닙니다. 우리 머리 위, 바로 이 공기 때문이죠."
        out = Path(self.tmp) / "me.mp3"
        with patch("autotube.http.post_json", side_effect=fake):
            tts.synthesize(text, str(out), voice={"engine": "gemini", "ref": "voice_me"},
                           style=tts.style_for("curiosity", "경이로운 다큐"))
        self.assertEqual(len(bodies), 3)
        for b in bodies:
            self.assertEqual(b["generation_config"]["speech_config"], [{"voice": "voice_me"}])
            style = b["input"][0]["content"][0]["annotations"][0]["style"]
            self.assertNotIn("Channel tone", style)  # no persona that could pull away from my timbre
        from autotube import media
        self.assertAlmostEqual(media.duration(out), 3 + 2 * 0.18, delta=0.25)

    def test_my_voice_has_no_style_by_default(self):
        bodies = []
        with patch("autotube.http.post_json", side_effect=lambda u, b, **k: bodies.append(b) or {"steps": [{"type": "model_output",
                   "content": [{"type": "audio", "mime_type": "audio/L16;rate=24000", "data": pcm_b64(1.0)}]}]}):
            tts.synthesize("문장입니다.", str(Path(self.tmp) / "d.mp3"), voice={"engine": "gemini", "ref": "voice_me"},
                           style=tts.style_for("surprise", "다큐"))
        self.assertNotIn("annotations", bodies[0]["input"][0]["content"][0])

    def test_prebuilt_voice_single_request(self):
        n = []
        with patch("autotube.http.post_json", side_effect=lambda *a, **k: n.append(1) or {"steps": [{"type": "model_output",
                   "content": [{"type": "audio", "mime_type": "audio/L16;rate=24000", "data": pcm_b64(1.0)}]}]}):
            tts.synthesize("첫 문장입니다. 두 번째 문장입니다.", str(Path(self.tmp) / "k.mp3"), voice={"engine": "gemini", "ref": "Kore"})
        self.assertEqual(len(n), 1)

    def test_split_merges_tiny_pieces(self):
        self.assertEqual(tts.split_sentences("네. 오늘은 블랙홀 이야기를 합니다. 시작할까요?"),
                         ["네. 오늘은 블랙홀 이야기를 합니다.", "시작할까요?"])


class RemoteVoices(TempHome):
    def test_lists_and_marks_imported(self):
        config.set_secrets({"GEMINI_API_KEY": "g"})
        voices.add_manual("old", "gemini", "voice_old")

        def fake(url, headers=None, params=None, **kw):
            if params["type"] == "replicated":
                return {"voices": [{"id": "voice_me", "display_name": "Kim"}, {"id": "voice_old", "display_name": "old"}]}
            return {"voices": [{"name": "voices/voice_doc", "displayName": "다큐 톤"}]}
        with patch("autotube.http.get_json", side_effect=fake):
            r = {v["ref"]: v for v in voices.list_gemini_remote()}
        self.assertEqual(r["voice_me"]["name"], "Kim")
        self.assertTrue(r["voice_old"]["imported"])
        self.assertIn("voice_doc", r)

    def test_register_error_shows_google_message(self):
        from autotube import http
        config.set_secrets({"GEMINI_API_KEY": "g"})
        src = make_wav(Path(self.tmp) / "s.wav", 20)
        con = make_wav(Path(self.tmp) / "c.wav", 6)
        err = http.HttpError(400, "Bad Request", '{"error":{"message":"Consent phrase does not match"}}')
        with patch("autotube.http.post_json", side_effect=err):
            with self.assertRaises(voices.VoiceError) as cm:
                voices.register_gemini("me", src, con)
        self.assertIn("Consent phrase does not match", str(cm.exception))


class VoiceGuards(TempHome):
    def _tone(self, path, hz, vol=0.5):
        subprocess.run([config.ffmpeg_bin(), "-y", "-loglevel", "error", "-f", "lavfi", "-i",
                        f"sine=frequency={hz}:duration=2", "-af", f"volume={vol}", str(path)], check=True)
        return path

    def test_pitch_outlier_detects_voice_switch(self):
        from autotube import voicecheck
        low = [voicecheck.median_pitch(self._tone(Path(self.tmp) / f"m{i}.wav", 120)) for i in range(4)]
        high = voicecheck.median_pitch(self._tone(Path(self.tmp) / "f.wav", 230))
        self.assertAlmostEqual(low[0], 120, delta=12)
        self.assertEqual(voicecheck.outliers(low + [high]), [4])

    def test_normalize_evens_loudness(self):
        from autotube import voicecheck
        quiet = self._tone(Path(self.tmp) / "q.wav", 200, 0.05)
        loud = self._tone(Path(self.tmp) / "l.wav", 200, 0.9)
        def peak(p):
            r = subprocess.run([config.ffmpeg_bin(), "-i", str(p), "-af", "volumedetect", "-f", "null", "-"],
                               capture_output=True, text=True)
            import re
            return float(re.search(r"mean_volume: (-?[\d.]+)", r.stderr).group(1))
        a = voicecheck.normalize(quiet, Path(self.tmp) / "qa.mp3")
        b = voicecheck.normalize(loud, Path(self.tmp) / "la.mp3")
        self.assertLess(abs(peak(a) - peak(b)), 2.0)

    def test_default_to_my_voice_when_channel_has_none(self):
        from autotube import producer
        voices.add_manual("MyVoice_KR_EN", "gemini", "voice_kim")
        self.assertEqual(producer.channel_voices({"id": "x", "voice": "", "voice_id": ""})[0], {"engine": "gemini", "ref": "voice_kim"})
