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
import re
import threading
import traceback
from pathlib import Path

from . import align, archive, budget, ccvideo, channels, config, stock2, db, footcheck, gemini, media, music, planner, stock, tts, voicecheck

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
    archive_video = staticmethod(archive.find_video)
    archive_photo = staticmethod(archive.find_photo)
    person_photo = staticmethod(archive.find_person)
    person_video = staticmethod(archive.find_person_video)
    cc_video = staticmethod(ccvideo.find_video)
    pixabay_video = staticmethod(stock2.pixabay_video)
    vecteezy_video = staticmethod(stock2.vecteezy_video)
    fits = staticmethod(footcheck.fits)
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
    """Code (not the LLM) decides the expensive mix.
    Scenes with visual.source (v1.8 plans): real -> archive/stock footage, person -> portrait + quote,
    diagram -> simple explainer image, illustration -> AI image (AI video for the most important ones).
    Older plans without source keep the original rule."""
    if any((s.get("visual") or {}).get("source") for s in scenes):
        illu = [i for i, s in enumerate(scenes) if s["visual"].get("source", "illustration") == "illustration"]
        vids = sorted(illu, key=lambda i: (-scenes[i].get("importance", 1), i))
        vids = {i for i in vids if scenes[i].get("importance", 1) >= 2}
        vids = set(sorted(vids, key=lambda i: (-scenes[i].get("importance", 1), i))[:max_ai_video])
        for i, s in enumerate(scenes):
            src = s["visual"].get("source", "illustration")
            s["visual"]["type"] = ("ai_video" if i in vids else "ai_image") if src == "illustration" else \
                {"real": "real", "person": "person", "diagram": "diagram"}[src]
        return scenes
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


IMAGE_SLOT_TYPES = ("ai_image", "ai_video", "diagram")
CHAINS = {
    "ai_video": ["ai_video", "my_image", "ai_image", "stock_video", "stock_photo"],
    "ai_image": ["my_image", "ai_image", "vecteezy_video", "pixabay_video", "stock_video", "stock_photo"],
    "diagram": ["my_image", "ai_image", "stock_photo"],
    "real": ["archive_video", "cc_video", "vecteezy_video", "pixabay_video", "stock_video", "archive_photo", "stock_photo", "ai_image"],
    "person": ["person_video", "cc_person_video", "person_photo", "archive_photo", "stock_photo", "ai_image"],
    "stock_video": ["vecteezy_video", "pixabay_video", "stock_video", "cc_video", "stock_photo", "ai_image"],
}


def my_images_dir(out):
    p = Path(out) / "my_images"
    p.mkdir(parents=True, exist_ok=True)
    return p


def my_image(out, n):
    """User-supplied image for slot n (scene number, 0 = thumbnail)."""
    for f in sorted(my_images_dir(out).glob(f"{n:02d}.*")):
        if f.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp"):
            return f
    return None


def image_slots(plan):
    slots = [{"n": 0, "what": "썸네일 배경", "prompt": plan["thumbnail"].get("image_prompt", "")}]
    for i, s in enumerate(plan["scenes"]):
        if s["visual"].get("type") in IMAGE_SLOT_TYPES:
            style = visual_style_suffix(s["visual"]["type"], config.load_settings()["production"])
            slots.append({"n": i + 1, "what": s.get("chapter", ""), "narration": s["narration"],
                          "prompt": f"{s['visual'].get('prompt', '')}, {style}"})
    return slots


def prompts_text(plan):
    slots = image_slots(plan)
    lines = [f"아래 이미지 {len(slots)}장을 순서대로 만들고, 순서대로 저장해서 zip 파일로 만들어 주세요.",
             "",
             "규칙:",
             "1. 맨 위 번호부터 아래 번호까지 한 장씩 순서대로 만들 것. 중간에 멈추거나 건너뛰지 말고 끝까지.",
             "2. 모두 16:9 가로, 글자·로고·워터마크 없이.",
             "3. 각 이미지는 대괄호 안의 번호를 파일 이름으로 저장 (예: [00] → 00.png, [03] → 03.png). 번호가 띄엄띄엄이어도 그 번호 그대로.",
             "4. 전부 다 만든 뒤, 저장한 이미지 파일들을 순서대로 하나의 zip 파일(images.zip)로 묶어서 다운로드 링크로 줄 것.",
             ""]
    for sl in slots:
        lines.append(f"[{sl['n']:02d}] {sl['what']}")
        lines.append(sl["prompt"])
        lines.append("")
    return "\n".join(lines)


def _space(channel, plan):
    words = " ".join((channel or {}).get("niches", []) + [plan.get("title", "")]).lower()
    return any(w in words for w in ("우주", "천문", "space", "nasa", "로켓", "행성", "블랙홀", "은하"))


def _topic_flags(channel, plan):
    words = " ".join((channel or {}).get("niches", []) + [plan.get("title", ""), (channel or {}).get("name", "")]).lower()
    return {"space": _space(channel, plan),
            "earth": any(w in words for w in ("지구", "기후", "날씨", "바다", "해양", "지진", "화산", "태풍", "빙하", "earth", "climate")),
            "history": any(w in words for w in ("역사", "과학사", "발명", "과학자", "history", "냉전", "원자", "핵"))}


def _visual(pid, s, i, d, folder, used, providers, credits, manual=False, out=None, space=False, flags=None):
    """Produce the raw visual for scene i with a fallback chain; returns (kind, path)."""
    kind = s["visual"]["type"]
    prompt = s["visual"].get("prompt", "")
    q = s["visual"].get("stock_query") or prompt[:40]
    chain = list(CHAINS.get(kind, CHAINS["ai_image"]))
    if manual:  # images come from the owner (ChatGPT etc.) -> never pay for AI images/videos
        chain = [k for k in chain if k not in ("ai_image", "ai_video")] or ["stock_video", "stock_photo"]
    errors = []
    for k in chain:
        try:
            if k == "my_image":
                f = my_image(out, i + 1) if out else None
                if not f:
                    continue
                p = folder / f"s{i:03d}_aiimage.png"
                if not p.exists():
                    from PIL import Image
                    Image.open(f).convert("RGB").save(p)
                return "ai_image", str(p)
            if k == "ai_video":
                p = str(folder / f"s{i:03d}_aivideo.mp4")
                if not Path(p).exists():
                    providers.ai_video(prompt + ", " + visual_style_suffix(kind, config.load_settings()["production"]) + ", 16:9",
                                       p, production_id=pid)
                return k, p
            if k == "ai_image":
                p = str(folder / f"s{i:03d}_aiimage.png")
                if not Path(p).exists():
                    style = visual_style_suffix(kind, config.load_settings()["production"])
                    providers.ai_image(prompt + ", " + style, p, production_id=pid)
                return k, p
            if k == "person_video":
                p = str(folder / f"s{i:03d}_person.mp4")
                if not Path(p).exists():
                    _, meta = providers.person_video(s["visual"].get("person", ""), used, p)
                    credits.append(meta)
                return "stock_video", p
            if k in ("archive_video", "stock_video", "cc_video", "cc_person_video", "pixabay_video", "vecteezy_video"):
                if not getattr(providers, "cc_video" if k.startswith("cc_") else k, None):
                    continue
                p = str(folder / f"s{i:03d}_{'archive' if k == 'archive_video' else 'cc' if k.startswith('cc_') else 'stock'}.mp4")
                if not Path(p).exists():
                    credits.append(_get_footage(k, q, d, used, p, providers, flags or {"space": space}, s, pid))
                return "stock_video", p
            if k in ("archive_photo", "stock_photo", "person_photo"):
                p = str(folder / f"s{i:03d}_{k}.jpg")
                if not Path(p).exists():
                    credits.append(_get_footage(k, q, d, used, p, providers, flags or {"space": space}, s, pid))
                return "stock_photo", p
        except budget.BudgetExceeded as ex:
            errors.append(f"{k}: {ex}")
            continue  # cheaper fallback next
        except Exception as ex:
            errors.append(f"{k}: {str(ex)[:100]}")
    _log(pid, f"장면 {i + 1} 시각자료 실패 → 제목 카드 사용 ({'; '.join(errors)[:300]})")
    return "card", None


def explain(ex):
    """Plain-Korean reason + what to do, for the production list and the activity log."""
    m = f"{type(ex).__name__}: {ex}"
    rules = [
        ("prepayment credits are depleted", "Gemini 선불 크레딧이 다 떨어졌습니다 → AI Studio(aistudio.google.com) 결제에서 충전하면 [이어서 다시 시도]로 계속됩니다"),
        ("402", "결제가 필요한 서비스가 거절했습니다(크레딧 부족) → 해당 서비스 결제 확인"),
        ("Invalid API key", "API 키가 잘못됐습니다 → 설정 → API 키에서 다시 넣어주세요"),
        ("401", "API 키 인증 실패 → 설정 → API 키 확인"),
        ("Color spaces", "영상 자료 하나의 색 형식을 변환하지 못했습니다 → [이어서 다시 시도]하면 그 장면만 다른 방식으로 만듭니다"),
        ("하루 요청 한도", "음성 하루 한도 소진 → 한도 초기화 후 자동으로 이어서 제작"),
        ("429", "요청이 너무 많아 잠시 막혔습니다 → 잠시 후 [이어서 다시 시도]"),
        ("No space left", "디스크 공간이 부족합니다 → C/F 드라이브 여유 공간 확인"),
        ("렌더 길이 이상", "영상이 중간에 잘려서 업로드를 막았습니다 → [이어서 다시 시도]"),
        ("TTSError", "내레이션(목소리) 만들기 실패 → 음성 서비스 키·크레딧 확인 후 [이어서 다시 시도]"),
        ("BudgetExceeded", "설정한 예산 한도에 걸려 멈췄습니다 → 설정의 1편/하루/월 예산 확인"),
    ]
    for key, msg in rules:
        if key in m:
            return msg
    return "예상하지 못한 오류 → [이어서 다시 시도], 반복되면 작업 기록의 원문을 확인"


def _qa_scenes(pid, scenes, work):
    """Indices of scenes whose real/stock footage does not fit the narration (score < footage_min_score - 1)."""
    from concurrent.futures import ThreadPoolExecutor
    lim = float(config.load_settings()["production"].get("footage_min_score", 6)) - 1

    def one(i):
        sc = scenes[i]
        if sc["visual"].get("type") in ("ai_image", "ai_video", "diagram"):
            return None  # generated for this narration
        clip = work / f"s{i:03d}_clip.mp4"
        if not clip.exists():
            return None
        sco = footcheck.score(clip, sc["narration"], sc["visual"].get("prompt", ""), kind="video", production_id=pid)
        return i if sco is not None and sco < lim else None
    with ThreadPoolExecutor(max_workers=4) as ex:
        return [i for i in ex.map(one, range(len(scenes))) if i is not None]


def _scene_queries(pid, scenes):
    """3-5 English stock-footage search terms per scene (specific -> broad), one LLM call per video, cached."""
    key = "queries:" + pid
    got = db.kv_get(key)
    if isinstance(got, list) and len(got) == len(scenes):
        return got
    try:
        from . import llm
        items = [{"n": i, "narration": sc["narration"][:200], "visual": (sc["visual"].get("prompt") or "")[:160]}
                 for i, sc in enumerate(scenes)]
        res = llm.generate_json(
            "For each scene of a Korean science documentary, write 3-5 short ENGLISH search terms for real stock "
            "footage (Vecteezy/Pexels/Pixabay/Wikimedia/NASA). Order them from specific to broad, 1-4 words each, "
            "concrete visible things (e.g. 'silicon wafer', 'cleanroom worker', 'chip factory'), no abstract words. "
            'Return JSON: {"scenes": [{"n": 0, "queries": ["..."]}]}. Scenes: ' + json.dumps(items, ensure_ascii=False),
            production_id=pid)
        out = [[] for _ in scenes]
        for it in (res or {}).get("scenes", []):
            n = int(it.get("n", -1))
            if 0 <= n < len(out):
                out[n] = [str(x)[:60] for x in (it.get("queries") or [])][:5]
        db.kv_set(key, out)
        return out
    except Exception as ex:
        _log(pid, f"장면별 검색어 생성 실패(기본 검색어 사용): {str(ex)[:120]}")
        return [[] for _ in scenes]


def _get_footage(k, q, d, used, p, providers, flags, sc, pid, tries=None):
    """Fetch footage of kind k and keep it only if it shows what the narration talks about.
    Rejected candidates stay in `used`, so the next try returns a different file."""
    person = sc["visual"].get("person", "") if k in ("cc_person_video", "person_photo") else ""
    want = sc["visual"].get("prompt") or sc["visual"].get("stock_query") or ""
    fetch = {
        "archive_video": lambda: providers.archive_video(q, used, p, **(flags or {})),
        "stock_video": lambda: providers.stock_video(q, d, used, p),
        "pixabay_video": lambda: providers.pixabay_video(q, d, used, p),
        "vecteezy_video": lambda: providers.vecteezy_video(q, d, used, p, narration=sc["narration"], want=want,
                                                           production_id=pid),
        "cc_video": lambda: providers.cc_video(q, d, used, p),
        "cc_person_video": lambda: providers.cc_video(q, d, used, p, person=person),
        "archive_photo": lambda: providers.archive_photo(q, used, p, **(flags or {})),
        "stock_photo": lambda: providers.stock_photo(q, used, p),
        "person_photo": lambda: providers.person_photo(person, used, p),
    }[k]
    check = getattr(providers, "fits", None)
    tries = tries or int(config.load_settings()["production"].get("footage_tries", 3))
    words = (q or "").split()
    queries = [q] + ([" ".join(words[:2])] if len(words) > 2 and k not in ("person_photo", "cc_person_video") else [])
    if k not in ("person_photo", "cc_person_video"):
        queries = list(dict.fromkeys((sc["visual"].get("queries") or []) + queries))  # best terms first, no repeats
    last = None
    for qq in queries[:max(2, tries)]:
        q = qq
        try:
            return _try_footage(fetch, 1 if len(queries) > 2 else tries, check, p, sc, want, k, person, pid)
        except Exception as ex:
            last = ex
    raise last or RuntimeError(f"{k}: 자료 없음")


def _try_footage(fetch, tries, check, p, sc, want, k, person, pid):
    for _ in range(tries):
        _, meta = fetch()
        if meta.pop("_prechecked", False) or not check or check(p, sc["narration"], want, "image" if k.endswith("photo") else "video", person, pid):
            return meta
        Path(p).unlink(missing_ok=True)
    raise RuntimeError(f"{k}: 내레이션과 맞는 화면 없음 ({tries}개 검사)")


def _shot_list(i, sc, d, kind, raw, work, used, providers, credits, flags, ps):
    """Split a scene into shots of at most max_shot_seconds. The first shot is the scene's own visual;
    the others are extra REAL video (archives first, then stock video) for the same subject.
    Video first: an image only fills a gap when no footage is found."""
    import math
    max_shot = float(ps.get("max_shot_seconds", 4.0))
    n = max(1, math.ceil(d / max_shot - 0.15))
    first = ("video" if kind in ("ai_video", "stock_video") else "image", raw)
    if n == 1:
        return [first]
    v = sc["visual"]
    queries = [q for q in (v.get("stock_query"), v.get("query"), (v.get("prompt") or "")[:40]) if q]
    shots = [first]
    for j in range(1, n):
        p = str(work / f"s{i:03d}_extra{j}.mp4")
        if Path(p).exists():
            shots.append(("video", p))
            continue
        got = False
        for q in queries:
            # extra cuts: only the fast sources, fewer candidates (the scene's main shot already got the thorough search)
            for src in ps.get("extra_shot_sources", ["vecteezy_video", "pixabay_video", "stock_video"]):
                if not getattr(providers, src, None):
                    continue
                try:
                    credits.append(_get_footage(src, q, d / n, used, p, providers, flags, sc, None,
                                                tries=int(ps.get("extra_shot_tries", 2))))
                    got = True
                    break
                except Exception:
                    continue
            if got:
                break
        if got:
            shots.append(("video", p))
        elif first[0] == "image":
            shots.append(first)  # same image, different camera move (better than one 10 s still)
        else:
            break  # a video scene simply keeps its own footage longer
    return shots


def visual_style_suffix(kind, ps):
    """One look per channel: 'photo' | 'animation' | 'mixed' (diagrams animated, the rest photographic)."""
    style = (ps or {}).get("visual_style", "photo")
    photo = "photorealistic documentary photograph, real-world look, natural colors, no cartoon, no illustration, no text, no watermark"
    anim = "polished 2D motion-graphics animation style, clean flat vector shapes, consistent color palette, no text, no watermark"
    if style == "animation":
        return anim
    if style == "mixed":
        return anim if kind == "diagram" else photo
    return "clean minimal infographic, flat colors, no text" if kind == "diagram" else photo


def channel_voices(channel):
    """(main, accent) voice specs for a channel. v1.2 channels only had an ElevenLabs id in voice_id."""
    if not channel:
        return default_own_voice(), None
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
    return default_own_voice(), None


def default_own_voice():
    """No voice chosen for the channel -> use the owner's registered voice if there is one."""
    # newest registered voice wins (the owner's latest choice, e.g. ElevenLabs over an older Gemini clone)
    r = db.one("SELECT engine, ref FROM voices WHERE kind IN ('clone','manual') ORDER BY created_at DESC LIMIT 1")
    return {"engine": r["engine"], "ref": r["ref"]} if r else None


def voice_label(v):
    if not v:
        return "기본 설정 (설정 탭의 음성 엔진 순서)"
    try:
        r = db.one("SELECT name FROM voices WHERE ref=?", (v.get("ref", ""),))
    except Exception:
        r = None
    return (r["name"] if r else v.get("ref") or "") + f" [{v.get('engine')}]"


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
    pex = [c for c in credits if (c.get("source") or "pexels") == "pexels"]
    arc = [c for c in credits if c.get("source") in ("Wikimedia Commons", "NASA")]
    if pex:
        authors = sorted({c.get("author") for c in pex if c.get("author")})[:15]
        media_c.append("자료 화면: Pexels" + (" (" + ", ".join(authors) + ")" if authors else ""))
    if arc:
        seen, lines = set(), []
        for c in arc:
            if c.get("page") in seen:
                continue
            seen.add(c.get("page"))
            lines.append(f"- {c.get('author') or 'Unknown'} / {c.get('license')} / {c.get('source')} {c.get('page') or ''}".strip())
        media_c.append("사진·영상 출처 (원본 자르기·확대·색 보정 적용):\n" + "\n".join(lines[:40]))
    mo = Path(work) / "music_origin.txt"
    if mo.exists():
        origin, _, path = mo.read_text(encoding="utf-8").partition("\t")
        if origin == "elevenlabs":
            media_c.append("음악: ElevenLabs Music으로 생성")
        elif origin == "library" and path:
            cr = music.credit_for(path)
            if cr:
                media_c.append("음악: " + cr)
    if media_c:
        parts.append("\n".join(media_c))
    return "\n\n" + "\n\n".join(parts)


def run(pid, providers=Providers):
    s = config.load_settings()
    ps = s["production"]
    prod = db.one("SELECT * FROM productions WHERE id=?", (pid,))
    if not prod:  # discarded while waiting in the queue
        return "deleted"
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
            cpt = channels.get_concept(prod.get("channel_id"), db.kv_get("concept:" + pid)) if db.kv_get("concept:" + pid) else None
            if cpt:
                kw["concept"] = cpt
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
        _log(pid, "내레이션 목소리: " + voice_label(main_voice) + (f" (강조 장면: {voice_label(accent_voice)})" if accent_voice else ""))

        def synth(i, sc):
            mp3 = work / f"s{i:03d}_voice.mp3"
            raw = work / f"s{i:03d}_voice_raw.mp3"
            use = accent_voice if (accent_voice and sc.get("importance", 1) >= 3) else main_voice
            ctx = (" ".join(x["narration"] for x in scenes[max(0, i - 2):i]),
                   " ".join(x["narration"] for x in scenes[i + 1:i + 3]))
            try:
                _, words, engine = providers.tts(sc["narration"], str(raw), production_id=pid, voice=use,
                                                 style=tts.style_for(sc.get("emotion", ""), tone), context=ctx)
            except TypeError:  # test doubles without the context argument
                _, words, engine = providers.tts(sc["narration"], str(raw), production_id=pid, voice=use,
                                                 style=tts.style_for(sc.get("emotion", ""), tone))
            if re.search(r"\d", sc["narration"]):
                words = []  # numbers were read out in words -> subtitles are re-timed from the original text (digits stay digits)
            try:
                voicecheck.normalize(raw, mp3)  # same loudness in every scene
            except Exception:
                raw.replace(mp3)
            return words, engine

        def save_meta(i, sc, words, engine):
            mp3 = work / f"s{i:03d}_voice.mp3"
            dur = media.duration(mp3)
            if not words:
                words = align.words_for(sc["narration"], str(mp3), dur) or media.proportional_words(sc["narration"], dur)
            (work / f"s{i:03d}_voice.json").write_text(json.dumps({"duration": dur, "words": words, "engine": engine},
                                                                  ensure_ascii=False), encoding="utf-8")

        new_idx = []
        for i, sc in enumerate(scenes):
            if not ((work / f"s{i:03d}_voice.mp3").exists() and (work / f"s{i:03d}_voice.json").exists()):
                words, engine = synth(i, sc)
                save_meta(i, sc, words, engine)
                new_idx.append(i)
            _set(pid, progress=PROGRESS["voice"] + 18 * (i + 1) / len(scenes))
        # voice-switch guard: a scene whose pitch is far from the rest (male<->female) is regenerated
        if ps.get("voice_check", True) and new_idx:
            pitches = [voicecheck.median_pitch(work / f"s{i:03d}_voice.mp3") for i in range(len(scenes))]
            bad = voicecheck.outliers(pitches)
            for attempt in range(2):
                if not bad:
                    break
                _log(pid, f"목소리가 다르게 나온 장면 {', '.join(str(b + 1) for b in bad)} → 다시 생성 ({attempt + 1}차)")
                for b in bad:
                    words, engine = synth(b, scenes[b])
                    save_meta(b, scenes[b], words, engine)
                    pitches[b] = voicecheck.median_pitch(work / f"s{b:03d}_voice.mp3")
                bad = [b for b in voicecheck.outliers(pitches) if b in bad]
            if bad:
                _log(pid, f"⚠ 장면 {', '.join(str(b + 1) for b in bad)}은 여전히 목소리가 달라 보입니다 — 완성 후 확인하세요")
        for i, sc in enumerate(scenes):
            info = json.loads((work / f"s{i:03d}_voice.json").read_text(encoding="utf-8"))
            sc["_voice"] = {"duration": info["duration"], "words": info["words"]}
            voice_used.add(info.get("engine"))
        _log(pid, "내레이션 완료 (" + ",".join(sorted(x for x in voice_used if x)) + ")")
        # learn the real speaking speed of this voice -> next scripts get the right length
        spoken = sum(sc["_voice"]["duration"] for sc in scenes)
        chars = sum(len(sc["narration"]) for sc in scenes)
        if spoken > 20:
            key = "cps:" + ((main_voice or {}).get("ref") or (main_voice or {}).get("engine") or "default")
            measured = chars / spoken
            old = db.kv_get(key)
            db.kv_set(key, round(measured if not old else 0.6 * measured + 0.4 * float(old), 2))
            minutes_req = db.kv_get("minutes:" + pid) or 0
            if minutes_req and spoken < minutes_req * 60 * 0.8:
                _log(pid, f"⚠ 실제 길이 {spoken / 60:.1f}분 (요청 {minutes_req}분): 이 목소리의 말 속도({measured:.1f}자/초)를 "
                          "학습했습니다. 다음 영상부터 길이가 맞춰집니다. 이 영상은 [대본부터 새로 만들기]로 다시 만들면 맞춰집니다.")

        # 3 visuals ---------------------------------------------------------
        _set(pid, step="visuals", progress=PROGRESS["visuals"])
        manual = (db.kv_get("imgsrc:" + pid) or ps.get("image_source", "ai")) == "manual"
        if manual:
            (out / "image_prompts.txt").write_text(prompts_text(plan), encoding="utf-8")
            missing = [sl["n"] for sl in image_slots(plan) if not my_image(out, sl["n"])]
            if missing and not db.kv_get("imgready:" + pid):
                _log(pid, f"이미지 {len(image_slots(plan))}장 프롬프트 준비 완료 → ChatGPT 등으로 만들어 넣어주세요 (비어 있는 번호: "
                          f"{', '.join(f'{n:02d}' for n in missing[:30])}). 넣은 뒤 [이미지 넣었어요 → 계속]")
                _set(pid, status="waiting_images")
                return "waiting_images"
        flags = _topic_flags(channel, plan)
        space = flags["space"]
        used, credits, clips, kinds = set(), [], [], {}
        lock = threading.Lock()
        for i, q in enumerate(_scene_queries(pid, scenes)):
            if q:
                scenes[i]["visual"]["queries"] = q
        for i, sc in enumerate(scenes):
            sc["_dur"] = sc["_voice"]["duration"] + 0.35 + (float(ps.get("emphasis_pause_seconds", 1.2)) if sc.get("pause_after") else 0)

        def build(i, sc):
            try:
                return _build(i, sc)
            except Exception as ex:  # one bad source file must not stop the whole video
                _log(pid, f"장면 {i + 1} 화면 만들기 실패 → 제목 카드로 대체 ({explain(ex)})")
                clip = work / f"s{i:03d}_clip.mp4"
                media.color_clip(sc["_dur"], str(clip), text=sc.get("on_screen_text") or plan["title"])
                return str(clip)

        def _build(i, sc):
            """One scene's clip. Scenes are built in parallel: footage search and AI checks are network-bound."""
            d = sc["_dur"]
            clip = work / f"s{i:03d}_clip.mp4"
            if clip.exists():
                try:  # a half-written clip from a crashed run would silently cut the whole video there
                    ok = abs(media.duration(clip) - d) <= 0.25  # same length as THIS narration (a new voice changes it)
                except Exception:
                    ok = False
                if not ok:
                    _log(pid, f"장면 {i + 1} 화면 파일이 깨져 있어 다시 만듭니다")
                    clip.unlink(missing_ok=True)
            if not clip.exists():
                kind, raw = _visual(pid, sc, i, d, work, used, providers, credits, manual=manual, out=out, space=space, flags=flags)
                tmp = work / f"s{i:03d}_base.mp4"
                shots = _shot_list(i, sc, d, kind, raw, work, used, providers, credits, flags, ps) \
                    if kind != "card" else []
                if len(shots) > 1:
                    # no shot longer than ~4 s: extra real footage (archives/stock video) cut into the scene
                    parts, each = [], d / len(shots)
                    for j, (sk, sp) in enumerate(shots):
                        part = work / f"s{i:03d}_shot{j}.mp4"
                        if sk == "video":
                            media.video_clip(sp, each, str(part))
                        else:
                            media.image_clip(sp, each, str(part), motion=i + j)
                        parts.append(str(part))
                    media.concat(parts, str(tmp), str(work / f"s{i:03d}_parts.txt"))
                elif kind in ("ai_video", "stock_video"):
                    media.video_clip(raw, d, str(tmp))
                elif kind in ("ai_image", "stock_photo"):
                    media.image_clip(raw, d, str(tmp), motion=i)
                else:
                    media.color_clip(d, str(tmp), text=sc.get("on_screen_text") or plan["title"])
                if sc.get("on_screen_text") and kind != "card":
                    media.overlay_text(str(tmp), sc["on_screen_text"], str(tmp.with_name(tmp.stem + "_t.mp4")))
                    tmp.with_name(tmp.stem + "_t.mp4").replace(tmp)
                if sc.get("quote") and kind != "card":
                    q = sc["quote"]
                    media.quote_card(str(tmp), q["text"], f"— {q['by']}" + (f", {q['when']}" if q.get("when") else ""),
                                     str(tmp.with_name(tmp.stem + "_q.mp4")))
                    tmp.with_name(tmp.stem + "_q.mp4").replace(tmp)
                tmp.replace(clip)
                with lock:
                    kinds[kind] = kinds.get(kind, 0) + 1

            return str(work / f"s{i:03d}_clip.mp4")

        from concurrent.futures import ThreadPoolExecutor
        done = [0]
        results = [None] * len(scenes)
        with ThreadPoolExecutor(max_workers=max(1, int(ps.get("visual_workers", 4)))) as ex:
            futs = {ex.submit(build, i, sc): i for i, sc in enumerate(scenes)}
            from concurrent.futures import as_completed
            for f in as_completed(futs):
                results[futs[f]] = f.result()
                done[0] += 1
                _set(pid, progress=PROGRESS["visuals"] + 35 * done[0] / len(scenes))
        clips = results
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
        got, want = media.duration(final), media.duration(audio)
        if got < want - 3:  # never upload a cut-off video
            final.unlink(missing_ok=True)
            raise media.MediaError(f"렌더 길이 이상: 영상 {got:.0f}초 / 내레이션 {want:.0f}초 → 업로드 중단, [이어서 다시 시도]로 깨진 장면만 다시 만듭니다")
        _log(pid, f"렌더 완료: {round(got / 60, 1)}분 · 자막 {'영상에 입힘' if burned else '별도 파일'}")
        # self-review: AI looks at every real-footage scene; mismatches are rebuilt once as AI visuals
        if ps.get("self_qa", True) and not db.kv_get("qa_done:" + pid) and not manual:
            bad = _qa_scenes(pid, scenes, work)
            db.kv_set("qa_done:" + pid, True)
            if bad:
                _log(pid, f"자체 검수: 내레이션과 안 맞는 장면 {len(bad)}개 → AI 화면으로 다시 만듭니다 ({', '.join(str(i + 1) for i in bad)})")
                row = json.loads(db.one("SELECT plan FROM productions WHERE id=?", (pid,))["plan"])
                for i in bad:
                    row["scenes"][i]["visual"]["type"] = "ai_video" if row["scenes"][i].get("importance", 1) >= 3 else "ai_image"
                    for f in work.glob(f"s{i:03d}_*"):
                        if "voice" not in f.name:
                            f.unlink(missing_ok=True)
                _set(pid, plan=json.dumps(row, ensure_ascii=False))
                for f in ("video_only.mp4", "video_list.txt"):
                    (work / f).unlink(missing_ok=True)
                final.unlink(missing_ok=True)
                return run(pid, providers)
            _log(pid, "자체 검수: 모든 장면 통과")
        _set(pid, video_path=str(final), srt_path=str(srt))

        # 6 thumbnail -------------------------------------------------------
        _set(pid, step="thumbnail", progress=PROGRESS["thumbnail"])
        bg = work / "thumb_bg.png"
        mine = my_image(out, 0)
        if mine and not bg.exists():
            from PIL import Image
            Image.open(mine).convert("RGB").save(bg)
        if not bg.exists() and manual:
            firsts = sorted(work.glob("s*_aiimage.png")) or sorted(work.glob("s*_photo*.jpg"))
            bg = firsts[0] if firsts else None
        elif not bg.exists():
            try:
                providers.ai_image(plan["thumbnail"].get("image_prompt", "") + ", dramatic, high contrast, no text",
                                   str(bg), production_id=pid)
            except Exception as ex:
                _log(pid, f"썸네일 배경 AI 실패 → 장면 이미지 사용 ({str(ex)[:80]})")
                firsts = sorted(work.glob("s*_aiimage.png")) or sorted(work.glob("s*_photo.jpg"))
                bg = firsts[0] if firsts else None
        thumb = out / "thumbnail.jpg"
        ttext = plan["thumbnail"].get("text", plan["title"][:12])
        media.thumbnail(str(bg) if bg else None, ttext, str(thumb))
        alts = sorted(work.glob("s*_aiimage.png"))[:2]
        for k, alt_bg in enumerate([bg, bg] + alts):  # candidates to pick from (YouTube Test & Compare)
            try:
                media.thumbnail(str(alt_bg) if alt_bg else None, ttext, str(out / f"thumbnail_alt{k + 1}.jpg"), variant=k + 1)
            except Exception:
                pass
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
    except tts.DailyQuotaError as ex:
        # not a failure: everything made so far is kept, and the worker resumes it after the reset
        db.kv_set("quota_resume:" + pid, ex.resume_at.isoformat())
        _set(pid, status="waiting_quota", error=str(ex))
        _log(pid, f"⏸ {ex} (오늘 음성 요청 {tts.requests_today()}회 사용)")
        db.event("warn", "produce", f"{pid}: {ex}")
        return "waiting_quota"
    except Exception as ex:
        _set(pid, status="failed", error=explain(ex) + f"  [원문: {type(ex).__name__}: {str(ex)[:300]}]")
        _log(pid, "오류: " + traceback.format_exc()[-1200:])
        db.event("error", "produce", f"{pid} 실패: {explain(ex)}")
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
        threading.Thread(target=self._resume_loop, name="quota-resume", daemon=True).start()

    def resume_due(self, now=None):
        """Re-queue productions paused by the daily voice quota once the quota has reset."""
        import datetime as dt
        now = now or dt.datetime.now(dt.timezone.utc)
        done = []
        for r in db.rows("SELECT id FROM productions WHERE status='waiting_quota' ORDER BY created_at"):
            at = db.kv_get("quota_resume:" + r["id"])
            try:
                due = not at or dt.datetime.fromisoformat(at) <= now
            except ValueError:
                due = True
            if due:
                _log(r["id"], "▶ 음성 한도가 초기화되어 이어서 제작합니다")
                self.submit(r["id"])
                done.append(r["id"])
        return done

    def _resume_loop(self):
        import time
        while True:
            time.sleep(300)
            try:
                self.resume_due()
            except Exception as ex:
                db.event("warn", "produce", f"한도 대기 재개 확인 실패: {str(ex)[:200]}")

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
