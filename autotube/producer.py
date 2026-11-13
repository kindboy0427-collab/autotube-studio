# -*- coding: utf-8 -*-
"""Topic/command -> finished long-form video (+ thumbnail, subtitles, private upload).

Steps (each scene asset is cached on disk, so '다시 시도' resumes where it stopped):
 1 plan     : script & scene list from learned patterns (planner)
 2 voice    : narration per scene in the channel's voice (Gemini TTS / my cloned voice / ElevenLabs)
 3 visuals  : AI video for the most important scenes, AI images / stock for the rest
 4 music    : ElevenLabs Music or your library, ducked under the voice
 5 render   : FFmpeg assembly 1080p, burned Korean subtitles
 6 thumbnail: AI background + big text
 7 upload   : private upload with 'altered/synthetic content' flag; you publish after review
"""
from __future__ import annotations
import json
import queue
import threading
import traceback
from pathlib import Path

from . import align, budget, channels, config, db, gemini, media, music, planner, stock, tts

STEPS = ["plan", "voice", "visuals", "music", "render", "thumbnail", "upload", "done"]
PROGRESS = {"plan": 5, "voice": 20, "visuals": 40, "music": 75, "render": 80, "thumbnail": 92, "upload": 95, "done": 100}


class Providers:
    """Injectable for tests. Defaults call the real services."""
    plan = staticmethod(planner.make_plan)
    tts = staticmethod(tts.synthesize)
    ai_video = staticmethod(gemini.generate_video)
    ai_image = staticmethod(gemini.generate_image)
    stock_video = staticmethod(stock.find_video)
    stock_photo = staticmethod(stock.find_photo)
    music = staticmethod(music.get_track)

    @staticmethod
    def upload(*a, **k):
        from . import youtube_upload
        return youtube_upload.upload(*a, **k)


def create(topic, instructions="", minutes=None, channel_id=None, script_format=None):
    pid = db.new_id()
    with db.connect() as con:
        con.execute("INSERT INTO productions(id,created_at,updated_at,topic,instructions,status,progress,step,channel_id,script_format) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?)", (pid, db.now(), db.now(), topic, instructions, "queued", 0, "plan", channel_id,
                                                  script_format if script_format in planner.FORMATS else None))
    db.kv_set("minutes:" + pid, minutes)
    return pid


def _set(pid, **kw):
    kw["updated_at"] = db.now()
    if "step" in kw:  # remember when each step started -> time per step in the usage report
        row = db.one("SELECT step, step_times FROM productions WHERE id=?", (pid,)) or {}
        times = json.loads(row.get("step_times") or "{}")
        if row.get("step") != kw["step"] or kw["step"] not in times:
            times[kw["step"]] = kw["updated_at"]
        kw["step_times"] = json.dumps(times)
    cols = ",".join(f"{k}=?" for k in kw)
    db.execute(f"UPDATE productions SET {cols} WHERE id=?", list(kw.values()) + [pid])


def _log(pid, msg):
    db.execute("UPDATE productions SET log=COALESCE(log,'')||? WHERE id=?", (f"[{db.now()[11:19]}] {msg}\n", pid))


def assign_visual_types(scenes, max_ai_video, ai_image_ratio):
    """Code (not the LLM) decides the expensive mix: AI video for the most important scenes."""
    order = sorted(range(len(scenes)), key=lambda i: (-scenes[i].get("importance", 1), i))
    video_idx = set()
    chapters_seen = set()
    for i, s in enumerate(scenes):  # hook + chapter openers first
        ch = s.get("chapter")
        if (i == 0 or ch not in chapters_seen) and len(video_idx) < max_ai_video and s.get("importance", 1) >= 2:
            video_idx.add(i)
        chapters_seen.add(ch)
    for i in order:
        if len(video_idx) >= max_ai_video:
            break
        if scenes[i].get("importance", 1) >= 3:
            video_idx.add(i)
    rest = [i for i in range(len(scenes)) if i not in video_idx]
    n_img = int(round(len(rest) * ai_image_ratio))
    img_idx = set(sorted(rest, key=lambda i: (-scenes[i].get("importance", 1), i))[:n_img])
    for i, s in enumerate(scenes):
        s["visual"]["type"] = "ai_video" if i in video_idx else "ai_image" if i in img_idx else "stock_video"
    return scenes


def _visual(pid, s, i, d, folder, used, providers, credits):
    """Produce the raw visual for scene i with a fallback chain; returns (kind, path)."""
    kind = s["visual"]["type"]
    prompt = s["visual"].get("prompt", "")
    q = s["visual"].get("stock_query") or prompt[:40]
    chain = {"ai_video": ["ai_video", "ai_image", "stock_video", "stock_photo"],
             "ai_image": ["ai_image", "stock_video", "stock_photo"],
             "stock_video": ["stock_video", "stock_photo", "ai_image"]}[kind]
    errors = []
    for k in chain:
        try:
            if k == "ai_video":
                p = str(folder / f"s{i:03d}_aivideo.mp4")
                if not Path(p).exists():
                    providers.ai_video(prompt + ", cinematic, 16:9", p, production_id=pid)
                return k, p
            if k == "ai_image":
                p = str(folder / f"s{i:03d}_aiimage.png")
                if not Path(p).exists():
                    providers.ai_image(prompt + ", cinematic lighting, highly detailed, no text, no watermark", p, production_id=pid)
                return k, p
            if k == "stock_video":
                p = str(folder / f"s{i:03d}_stock.mp4")
                if not Path(p).exists():
                    _, meta = providers.stock_video(q, d, used, p)
                    credits.append(meta)
                return k, p
            if k == "stock_photo":
                p = str(folder / f"s{i:03d}_photo.jpg")
                if not Path(p).exists():
                    _, meta = providers.stock_photo(q, used, p)
                    credits.append(meta)
                return k, p
        except budget.BudgetExceeded as ex:
            errors.append(f"{k}: {ex}")
            continue  # cheaper fallback next
        except Exception as ex:
            errors.append(f"{k}: {str(ex)[:100]}")
    _log(pid, f"장면 {i + 1} 시각자료 실패 → 제목 카드 사용 ({'; '.join(errors)[:300]})")
    return "card", None


def channel_voices(channel):
    """(main, accent) voice specs for a channel. v1.2 channels only had an ElevenLabs id in voice_id."""
    if not channel:
        return None, None
    raw = channel.get("voice") or ""
    spec = None
    if raw:
        try:
            spec = json.loads(raw) if isinstance(raw, str) else raw
        except ValueError:
            spec = None
    if isinstance(spec, dict) and spec.get("engine"):
        main = {"engine": spec["engine"], "ref": spec.get("ref", "")}
        acc = spec.get("accent") if isinstance(spec.get("accent"), dict) and spec["accent"].get("engine") else None
        return main, acc
    if channel.get("voice_id"):
        return tts.parse_voice(channel["voice_id"]), None
    return None, None


def kinds_all(work):
    k = set()
    for p in Path(work).glob("s*_*"):
        n = p.name
        if "_aivideo" in n:
            k.add("ai_video")
        elif "_aiimage" in n:
            k.add("ai_image")
    return k


ENGINE_NAME = {"gemini": "Google Gemini TTS", "elevenlabs": "ElevenLabs", "openai": "OpenAI TTS", "edge": "Microsoft Edge TTS"}


def description_credits(plan, credits, work, voice_used, ai_kinds):
    """Credibility block like the reference channels: sources, AI disclosure, media & music credits."""
    parts = []
    src = plan.get("sources") or []
    if src:
        parts.append("📚 참고 자료\n" + "\n".join("- " + x for x in src))
    ai = []
    if ai_kinds & {"ai_video", "ai_image"}:
        ai.append("일부 장면은 AI로 만든 재연·가상 이미지입니다")
    eng = sorted(ENGINE_NAME.get(e, e) for e in voice_used if e)
    if eng:
        ai.append("나레이션은 AI 음성입니다 (" + ", ".join(eng) + ")")
    parts.append("※ " + ". ".join(ai + ["대본 작성에 AI 도구를 활용했습니다"]) + ".")
    media_c = []
    if credits:
        authors = sorted({c.get("author") for c in credits if c.get("author")})[:15]
        media_c.append("자료 화면: Pexels" + (" (" + ", ".join(authors) + ")" if authors else ""))
    mo = Path(work) / "music_origin.txt"
    if mo.exists():
        origin, _, path = mo.read_text(encoding="utf-8").partition("\t")
        if origin == "elevenlabs":
            media_c.append("음악: ElevenLabs Music으로 생성")
        elif origin == "library" and path:
            media_c.append("음악: " + Path(path).stem)
    if media_c:
        parts.append("\n".join(media_c))
    return "\n\n" + "\n\n".join(parts)


def run(pid, providers=Providers):
    s = config.load_settings()
    ps = s["production"]
    prod = db.one("SELECT * FROM productions WHERE id=?", (pid,))
    channel = channels.get(prod.get("channel_id"))
    db.execute("UPDATE productions SET step_times=NULL WHERE id=? AND status IN ('queued') AND plan IS NULL", (pid,))
    out = (config.OUTPUT / pid).resolve()
    work = out / "work"
    work.mkdir(parents=True, exist_ok=True)
    _set(pid, status="running", output_dir=str(out), error=None)
    try:
        # 1 plan ------------------------------------------------------------
        _set(pid, step="plan", progress=PROGRESS["plan"])
        if prod.get("plan"):
            plan = json.loads(prod["plan"])
        else:
            minutes = db.kv_get("minutes:" + pid) or (channel or {}).get("target_minutes") or ps["target_minutes"]
            kw = {"script_format": prod["script_format"]} if prod.get("script_format") else {}
            plan = providers.plan(prod["topic"], prod.get("instructions") or "", minutes, production_id=pid, channel=channel, **kw)
            assign_visual_types(plan["scenes"], ps["max_ai_video_clips"], ps["ai_image_ratio"])
            _set(pid, plan=json.dumps(plan, ensure_ascii=False), features_used=json.dumps(plan.get("features_used", [])))
            _log(pid, f"기획 완료: {plan['title']} · 장면 {len(plan['scenes'])}개 · 예상 {plan['_stats']['est_minutes']}분")
        (out / "plan.json").write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
        scenes = plan["scenes"]

        # 2 voice -----------------------------------------------------------
        _set(pid, step="voice", progress=PROGRESS["voice"])
        voice_used = set()
        main_voice, accent_voice = channel_voices(channel)
        tone = (channel or {}).get("tone", "")
        for i, sc in enumerate(scenes):
            mp3 = work / f"s{i:03d}_voice.mp3"
            meta = work / f"s{i:03d}_voice.json"
            if not (mp3.exists() and meta.exists()):
                use = accent_voice if (accent_voice and sc.get("importance", 1) >= 3) else main_voice
                _, words, engine = providers.tts(sc["narration"], str(mp3), production_id=pid, voice=use,
                                                 style=tts.style_for(sc.get("emotion", ""), tone))
                dur = media.duration(mp3)
                if not words:
                    words = align.words_for(sc["narration"], str(mp3), dur) or media.proportional_words(sc["narration"], dur)
                meta.write_text(json.dumps({"duration": dur, "words": words, "engine": engine}, ensure_ascii=False), encoding="utf-8")
            info = json.loads(meta.read_text(encoding="utf-8"))
            sc["_voice"] = {"duration": info["duration"], "words": info["words"]}
            voice_used.add(info.get("engine"))
            _set(pid, progress=PROGRESS["voice"] + 20 * (i + 1) / len(scenes))
        _log(pid, "내레이션 완료 (" + ",".join(sorted(x for x in voice_used if x)) + ")")

        # 3 visuals ---------------------------------------------------------
        _set(pid, step="visuals", progress=PROGRESS["visuals"])
        used, credits, clips, kinds = set(), [], [], {}
        for i, sc in enumerate(scenes):
            d = sc["_voice"]["duration"] + 0.35 + (float(ps.get("emphasis_pause_seconds", 1.2)) if sc.get("pause_after") else 0)
            sc["_dur"] = d
            clip = work / f"s{i:03d}_clip.mp4"
            if not clip.exists():
                kind, raw = _visual(pid, sc, i, d, work, used, providers, credits)
                tmp = work / f"s{i:03d}_base.mp4"
                if kind in ("ai_video", "stock_video"):
                    media.video_clip(raw, d, str(tmp))
                elif kind in ("ai_image", "stock_photo"):
                    media.image_clip(raw, d, str(tmp), motion=i)
                else:
                    media.color_clip(d, str(tmp), text=sc.get("on_screen_text") or plan["title"])
                if sc.get("on_screen_text") and kind != "card":
                    media.overlay_text(str(tmp), sc["on_screen_text"], str(clip))
                else:
                    tmp.replace(clip)
                kinds[kind] = kinds.get(kind, 0) + 1
            clips.append(str(clip))
            _set(pid, progress=PROGRESS["visuals"] + 35 * (i + 1) / len(scenes))
        if kinds:
            _log(pid, "시각자료: " + ", ".join(f"{k} {v}" for k, v in kinds.items()))
        cred_file = work / "credits.json"
        if credits:
            old = json.loads(cred_file.read_text(encoding="utf-8")) if cred_file.exists() else []
            cred_file.write_text(json.dumps(old + credits, ensure_ascii=False), encoding="utf-8")
        credits = json.loads(cred_file.read_text(encoding="utf-8")) if cred_file.exists() else []

        # 4 music + narration track -----------------------------------------
        _set(pid, step="music", progress=PROGRESS["music"])
        padded = []
        for i, sc in enumerate(scenes):
            wav = work / f"s{i:03d}_voice_pad.wav"
            if not wav.exists():
                media.pad_audio(str(work / f"s{i:03d}_voice.mp3"), sc["_dur"], str(wav))
            padded.append(str(wav))
        narration = work / "narration.wav"
        media.concat(padded, str(narration), str(work / "audio_list.txt"))
        audio = narration
        if ps.get("music"):
            total = sum(sc["_dur"] for sc in scenes)
            track = work / "music.mp3"
            src = str(track) if track.exists() else None
            if not src:
                src, origin = providers.music(plan.get("music_mood", ""), total, str(track), production_id=pid)
                _log(pid, f"배경음악: {origin}")
                (work / "music_origin.txt").write_text(f"{origin}\t{src or ''}", encoding="utf-8")
            if src:
                audio = work / "mixed.wav"
                media.mix_music(str(narration), src, str(audio))

        # 5 render ----------------------------------------------------------
        _set(pid, step="render", progress=PROGRESS["render"])
        video_only = work / "video_only.mp4"
        media.concat(clips, str(video_only), str(work / "video_list.txt"))
        offsets, t = [], 0.0
        for sc in scenes:
            offsets.append((t, sc["_voice"]["words"]))
            t += sc["_dur"]
        srt = out / "subtitles.srt"
        media.build_srt(offsets, str(srt))
        final = out / "video.mp4"
        _, burned = media.final_mux(str(video_only), str(audio), str(final), srt=str(srt),
                                    burn=ps.get("burn_subtitles", True), cwd=str(out))
        _log(pid, f"렌더 완료: {round(media.duration(final) / 60, 1)}분 · 자막 {'영상에 입힘' if burned else '별도 파일'}")
        _set(pid, video_path=str(final), srt_path=str(srt))

        # 6 thumbnail -------------------------------------------------------
        _set(pid, step="thumbnail", progress=PROGRESS["thumbnail"])
        bg = work / "thumb_bg.png"
        if not bg.exists():
            try:
                providers.ai_image(plan["thumbnail"].get("image_prompt", "") + ", dramatic, high contrast, no text",
                                   str(bg), production_id=pid)
            except Exception as ex:
                _log(pid, f"썸네일 배경 AI 실패 → 장면 이미지 사용 ({str(ex)[:80]})")
                firsts = sorted(work.glob("s*_aiimage.png")) or sorted(work.glob("s*_photo.jpg"))
                bg = firsts[0] if firsts else None
        thumb = out / "thumbnail.jpg"
        media.thumbnail(str(bg) if bg else None, plan["thumbnail"].get("text", plan["title"][:12]), str(thumb))
        _set(pid, thumbnail_path=str(thumb))

        # description with chapters, AI disclosure and credits ----------------
        chapters, t, last = [], 0.0, None
        for sc in scenes:
            if sc.get("chapter") and sc["chapter"] != last:
                m, s2 = divmod(int(t), 60)
                chapters.append(f"{m:02d}:{s2:02d} {sc['chapter']}")
                last = sc["chapter"]
            t += sc["_dur"]
        if chapters and not chapters[0].startswith("00:00"):
            chapters.insert(0, "00:00 시작")
        desc = plan.get("description", "").strip()
        if len(chapters) >= 3:
            desc += "\n\n" + "\n".join(chapters)
        desc += description_credits(plan, credits, work, voice_used, kinds_all(work))
        (out / "description.txt").write_text(desc, encoding="utf-8")
        (out / "pinned_comment.txt").write_text(plan.get("pinned_comment", ""), encoding="utf-8")
        (out / "fact_check.txt").write_text("\n".join("- " + x for x in plan.get("fact_check", [])) or "(없음)", encoding="utf-8")

        # 7 upload ----------------------------------------------------------
        _set(pid, step="upload", progress=PROGRESS["upload"])
        from . import youtube_upload
        cid = prod.get("channel_id")
        if ps.get("auto_upload_private") and youtube_upload.connected(cid) and not prod.get("youtube_id"):
            vid, notes = providers.upload(str(final), plan["title"], desc, plan.get("tags", []), privacy="private",
                                          thumbnail=str(thumb), srt=str(srt) if ps.get("upload_srt_caption") else None,
                                          channel_id=cid)
            _set(pid, youtube_id=vid, privacy="private")
            _log(pid, f"비공개 업로드 완료: https://youtu.be/{vid} " + " ".join(notes))
            status = "uploaded_private"
        else:
            status = "ready"
            if ps.get("auto_upload_private") and not youtube_upload.connected(cid):
                _log(pid, "YouTube 채널 미연결 → 파일만 생성 (채널 탭에서 연결 후 '업로드' 버튼)")
        _set(pid, status=status, step="done", progress=100, finished_at=db.now())
        db.event("info", "produce", f"제작 완료: {plan['title']} (${budget.production_cost(pid):.2f} 추정)")
        return status
    except Exception as ex:
        _set(pid, status="failed", error=f"{type(ex).__name__}: {str(ex)[:500]}")
        _log(pid, "오류: " + traceback.format_exc()[-1200:])
        db.event("error", "produce", f"{pid} 실패: {str(ex)[:300]}")
        return "failed"


class Worker:
    """One production at a time in the background."""

    def __init__(self):
        self.q = queue.Queue()
        self.thread = threading.Thread(target=self._loop, name="producer", daemon=True)
        self.current = None

    def start(self):
        for r in db.rows("SELECT id FROM productions WHERE status IN ('queued','running') ORDER BY created_at"):
            self.q.put(r["id"])
        self.thread.start()

    def submit(self, pid):
        db.execute("UPDATE productions SET status='queued', error=NULL WHERE id=?", (pid,))
        self.q.put(pid)

    def _loop(self):
        while True:
            pid = self.q.get()
            self.current = pid
            try:
                run(pid)
            finally:
                self.current = None
