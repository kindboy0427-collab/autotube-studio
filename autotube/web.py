# -*- coding: utf-8 -*-
"""Local web app: http://127.0.0.1:8770 (this PC only)."""
from __future__ import annotations
import json
import re
import secrets
import shutil
import threading
from pathlib import Path

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

from . import (align, budget, channels, config, db, learner, planner, producer, radar, scheduler, subscription, tts, usage,
               voices, watcher, youtube_data)

STATIC = Path(__file__).parent / "static"
TOKEN = secrets.token_urlsafe(24)
WORKER = producer.Worker()
SCHED = scheduler.Scheduler()


@asynccontextmanager
async def lifespan(app):
    config.ensure_dirs()
    db.init()
    watcher.seed_references()
    if not db.kv_get("concept_ai_risk_v2"):  # 'AI 위험 분석' = one series inside the science channel (owner, 2026-11-13)
        try:
            channels.migrate_ai_risk_to_concept("tech")
        except Exception:
            pass
        db.kv_set("concept_ai_risk_v2", True)
    if not getattr(app.state, "no_background", False):
        WORKER.start()
        SCHED.start()
        threading.Thread(target=_update_loop, name="updater", daemon=True).start()
    yield


app = FastAPI(title="AutoTube Studio", docs_url=None, redoc_url=None, lifespan=lifespan)


@app.middleware("http")
async def local_only(request: Request, call_next):
    host = request.headers.get("host", "").rsplit(":", 1)[0].strip("[]").lower()
    if host not in ("127.0.0.1", "localhost", "testserver"):
        return JSONResponse({"error": "localhost only"}, status_code=403)
    if request.method != "GET" and request.headers.get("x-token") != TOKEN and not (
            request.url.path == "/api/shutdown" and not request.headers.get("origin")):
        return JSONResponse({"error": "invalid token"}, status_code=403)
    return await call_next(request)


@app.get("/api/ping")
def ping():
    from . import updater
    return {"app": "autotube", "version": updater.local_version()}


@app.post("/api/shutdown")
def shutdown():
    """[프로그램 종료] button / launcher restart. Waits for a running production to be saved."""
    import os

    def bye():
        import time
        time.sleep(0.8)
        os._exit(0)
    threading.Thread(target=bye, daemon=True).start()
    return {"ok": True, "producing": WORKER.current}


def _update_loop():
    import time
    time.sleep(60)
    while True:
        try:
            update_tick()
        except Exception as ex:
            db.event("warn", "update", str(ex)[:200])
        hours = float(config.load_settings().get("update", {}).get("check_hours", 3))
        time.sleep(max(900, hours * 3600))


UPDATE_STATE = {"checked": None, "info": None, "applying": False, "message": ""}


def _restart_after_update():
    """Start a fresh launcher that waits for us to exit and starts the new engine."""
    import os
    import subprocess
    import sys
    from . import launch
    subprocess.Popen([launch._pythonw(), "-m", "autotube.launch", "--restart", "--background"], cwd=str(config.ROOT),
                     creationflags=launch.DETACHED | launch.NO_WINDOW)


def update_tick(force=False):
    """Called by the scheduler every few hours: if a new version exists and nothing is being produced, apply it."""
    from . import updater
    st = config.load_settings().get("update", {})
    UPDATE_STATE["info"] = updater.check()
    UPDATE_STATE["checked"] = db.now()
    info = UPDATE_STATE["info"]
    if not info.get("available") or UPDATE_STATE["applying"]:
        return info
    if not (force or st.get("auto", True)) or WORKER.current:
        return info
    UPDATE_STATE["applying"] = True
    try:
        res = updater.apply(lambda m: db.event("info", "update", m))
        if res.get("updated"):
            db.event("info", "update", f"자동 업데이트 {res['current']} → {res['latest']} 완료, 다시 시작합니다")
            UPDATE_STATE["message"] = f"v{res['latest']}로 업데이트됨 — 잠시 후 화면이 새로고침됩니다"
            _restart_after_update()
    except Exception as ex:
        db.event("warn", "update", f"업데이트 실패: {str(ex)[:300]}")
        UPDATE_STATE["message"] = "업데이트 실패: " + str(ex)[:200]
    finally:
        UPDATE_STATE["applying"] = False
    return UPDATE_STATE["info"]


@app.get("/api/update")
def update_status():
    from . import updater
    return {"current": updater.local_version(), **UPDATE_STATE}


@app.post("/api/update/check")
def update_check():
    threading.Thread(target=update_tick, kwargs={"force": True}, daemon=True).start()
    return {"ok": True}


@app.get("/", response_class=HTMLResponse)
def index():
    return (STATIC / "index.html").read_text(encoding="utf-8").replace("__TOKEN__", TOKEN)


@app.get("/static/{name}")
def static(name: str):
    if not re.fullmatch(r"[a-z]+\.(js|css)", name):
        raise HTTPException(404)
    return FileResponse(STATIC / name)


@app.get("/api/status")
def status():
    from . import youtube_upload
    counts = {r["status"]: r["c"] for r in db.rows("SELECT status, COUNT(*) c FROM videos GROUP BY status")}
    return {"keys": config.secret_status(), "budget": budget.summary(), "settings": config.load_settings(),
            "learning": {"queued": counts.get("queued", 0), "watched": counts.get("watched", 0),
                         "failed": counts.get("failed", 0), "state": SCHED.state, "last_tick": SCHED.last_tick,
                         "watch_hours_today": round(watcher.watched_seconds_today() / 3600, 2),
                         "patterns": db.one("SELECT COUNT(*) c FROM patterns WHERE n>=3")["c"]},
            "producing": WORKER.current,
            "youtube_connected": youtube_upload.connected() or any(youtube_upload.connected(c["id"]) for c in channels.all_channels()),
            "client_secret_present": youtube_upload.client_file().exists(),
            "ffmpeg": bool(shutil.which(config.ffmpeg_bin()) or Path(config.ffmpeg_bin()).exists()),
            "font": config.font_path(), "events": db.events(30), "subscriptions": subscription.status(),
            "voices_mine": db.one("SELECT COUNT(*) c FROM voices")["c"],
            "gemini_check": db.kv_get("gemini_key_check"),
            "channels_count": db.one("SELECT COUNT(*) c FROM channels WHERE active=1")["c"], "align_available": align.available(), "align_install": db.kv_get("align_install"),
            "formats": {k: v["name"] for k, v in planner.FORMATS.items()}}


class ImageUpload(BaseModel):
    items: list[dict] = Field(min_length=1, max_length=120)


@app.get("/api/productions/{pid}/images")
def production_images(pid: str):
    r = db.one("SELECT plan, output_dir, status FROM productions WHERE id=?", (pid,))
    if not r or not r["plan"]:
        raise HTTPException(404)
    plan = json.loads(r["plan"])
    out = Path(r["output_dir"] or (config.OUTPUT / pid))
    slots = producer.image_slots(plan)
    for sl in slots:
        f = producer.my_image(out, sl["n"])
        sl["file"] = f.name if f else None
    return {"slots": slots, "prompts": producer.prompts_text(plan), "status": r["status"]}


@app.post("/api/productions/{pid}/images")
def upload_images(pid: str, body: ImageUpload):
    import base64
    r = db.one("SELECT output_dir FROM productions WHERE id=?", (pid,))
    if not r:
        raise HTTPException(404)
    d = producer.my_images_dir(Path(r["output_dir"] or (config.OUTPUT / pid)))
    saved = []
    for it in body.items:
        n = int(it.get("n", -1))
        if not 0 <= n <= 999:
            continue
        ext = "." + re.sub(r"[^a-z]", "", str(it.get("ext", "png")).lower())[:4]
        if ext not in (".png", ".jpg", ".jpeg", ".webp"):
            ext = ".png"
        raw = base64.b64decode(str(it.get("b64", "")).split(",", 1)[-1])
        if len(raw) > 25 * 1024 * 1024:
            continue
        for old in d.glob(f"{n:02d}.*"):
            old.unlink(missing_ok=True)
        (d / f"{n:02d}{ext}").write_bytes(raw)
        work = Path(r["output_dir"] or "", "work")
        for f in list(work.glob(f"s{n - 1:03d}_aiimage.png")) + list(work.glob(f"s{n - 1:03d}_clip.mp4")) + \
                (list(work.glob("thumb_bg.png")) if n == 0 else []):
            f.unlink(missing_ok=True)  # replaced image -> rebuild that scene
        saved.append(n)
    return {"saved": saved}


@app.post("/api/productions/{pid}/images/continue")
def images_continue(pid: str):
    if not db.one("SELECT id FROM productions WHERE id=?", (pid,)):
        raise HTTPException(404)
    db.kv_set("imgready:" + pid, True)
    WORKER.submit(pid)
    return {"ok": True}


@app.get("/media/{pid}/my/{name}")
def my_image_file(pid: str, name: str):
    if not re.fullmatch(r"[\w-]+", pid) or not re.fullmatch(r"\d{2}\.(png|jpg|jpeg|webp)", name):
        raise HTTPException(404)
    f = (config.OUTPUT / pid / "my_images" / name).resolve()
    if not f.is_file():
        raise HTTPException(404)
    return FileResponse(f)


class NewProduction(BaseModel):
    topic: str = Field(min_length=2, max_length=500)
    instructions: str = Field(default="", max_length=3000)
    minutes: int = Field(default=10, ge=3, le=40)
    channel_id: str | None = Field(default=None, max_length=40)
    script_format: str | None = Field(default=None, max_length=30)
    image_source: str | None = Field(default=None, pattern="^(ai|manual)$")
    concept: str | None = Field(default=None, max_length=30)


@app.post("/api/productions")
def new_production(body: NewProduction):
    if body.channel_id and not channels.get(body.channel_id):
        raise HTTPException(400, "없는 채널입니다")
    pid = producer.create(body.topic.strip(), body.instructions.strip(), body.minutes, body.channel_id or None,
                          body.script_format if body.script_format in planner.FORMATS else None)
    if body.image_source:
        db.kv_set("imgsrc:" + pid, body.image_source)
    if body.concept and body.channel_id and channels.get_concept(body.channel_id, body.concept):
        db.kv_set("concept:" + pid, body.concept)
    WORKER.submit(pid)
    return {"id": pid}


@app.get("/api/productions")
def list_productions():
    out = []
    for r in db.rows("SELECT id,created_at,topic,status,progress,step,youtube_id,privacy,cost_usd,error,plan,channel_id "
                     "FROM productions ORDER BY created_at DESC LIMIT 100"):
        plan = json.loads(r.pop("plan") or "{}")
        r["title"] = plan.get("title")
        out.append(r)
    return out


@app.get("/api/productions/{pid}")
def get_production(pid: str):
    r = db.one("SELECT * FROM productions WHERE id=?", (pid,))
    if not r:
        raise HTTPException(404)
    r["plan"] = json.loads(r["plan"] or "{}")
    for sc in r["plan"].get("scenes", []):
        sc.pop("_voice", None)
    out = Path(r["output_dir"] or "")
    for name in ("description.txt", "pinned_comment.txt", "fact_check.txt"):
        f = out / name
        r[name.split(".")[0]] = f.read_text(encoding="utf-8") if f.is_file() else ""
    r["performance"] = db.rows("SELECT * FROM performance WHERE production_id=? ORDER BY id DESC LIMIT 30", (pid,))
    r["usage"] = usage.production_report(pid)
    return r


@app.post("/api/productions/{pid}/retry")
def retry(pid: str):
    if not db.one("SELECT id FROM productions WHERE id=?", (pid,)):
        raise HTTPException(404)
    WORKER.submit(pid)
    return {"ok": True}


@app.post("/api/productions/{pid}/replan")
def replan(pid: str):
    r = db.one("SELECT output_dir FROM productions WHERE id=?", (pid,))
    if not r:
        raise HTTPException(404)
    if r["output_dir"] and Path(r["output_dir"], "work").exists():
        shutil.rmtree(Path(r["output_dir"], "work"), ignore_errors=True)
    db.execute("UPDATE productions SET plan=NULL, video_path=NULL, youtube_id=NULL WHERE id=?", (pid,))
    WORKER.submit(pid)
    return {"ok": True}


@app.post("/api/productions/{pid}/revoice")
def revoice(pid: str):
    """Redo only the narration (keeps script, AI images/videos, stock, music) and re-render."""
    r = db.one("SELECT output_dir, youtube_id FROM productions WHERE id=?", (pid,))
    if not r:
        raise HTTPException(404)
    work = Path(r["output_dir"] or "", "work")
    if work.exists():
        for pat in ("s*_voice*", "s*_clip.mp4", "s*_base.mp4", "narration.wav", "mixed.wav", "video_only.mp4", "*_list.txt"):
            for f in work.glob(pat):
                f.unlink(missing_ok=True)
    for name in ("video.mp4", "subtitles.srt"):
        Path(r["output_dir"] or "", name).unlink(missing_ok=True)
    db.execute("UPDATE productions SET video_path=NULL, status='queued', step='voice' WHERE id=?", (pid,))
    WORKER.submit(pid)
    return {"ok": True, "note": "이미 유튜브에 올라간 영상은 그대로 두고 새로 올립니다" if r["youtube_id"] else ""}


@app.post("/api/productions/{pid}/upload")
def upload_now(pid: str):
    from . import youtube_upload
    r = db.one("SELECT * FROM productions WHERE id=?", (pid,))
    if not r or not r["video_path"]:
        raise HTTPException(400, "영상이 아직 없습니다")
    if r["youtube_id"]:
        return {"youtube_id": r["youtube_id"]}
    plan = json.loads(r["plan"])
    desc = Path(r["output_dir"], "description.txt").read_text(encoding="utf-8")
    vid, notes = youtube_upload.upload(r["video_path"], plan["title"], desc, plan.get("tags", []), privacy="private",
                                       thumbnail=r["thumbnail_path"], channel_id=r.get("channel_id"))
    db.execute("UPDATE productions SET youtube_id=?, privacy='private', status='uploaded_private' WHERE id=?", (vid, pid))
    return {"youtube_id": vid, "notes": notes}


class Publish(BaseModel):
    privacy: str = Field(default="public", pattern="^(public|unlisted|private)$")
    publish_at: str | None = None
    title: str | None = Field(default=None, max_length=100)


@app.post("/api/productions/{pid}/publish")
def publish(pid: str, body: Publish):
    from . import youtube_upload
    r = db.one("SELECT youtube_id, channel_id FROM productions WHERE id=?", (pid,))
    if not r or not r["youtube_id"]:
        raise HTTPException(400, "먼저 업로드하세요")
    youtube_upload.set_privacy(r["youtube_id"], body.privacy, body.publish_at, channel_id=r.get("channel_id"))
    db.execute("UPDATE productions SET privacy=?, status=? WHERE id=?",
               ("scheduled" if body.publish_at else body.privacy, "published" if body.privacy == "public" else "uploaded_private", pid))
    return {"ok": True}


@app.get("/media/{pid}/{name}")
def media_file(pid: str, name: str):
    if name not in ("video.mp4", "thumbnail.jpg", "subtitles.srt", "description.txt") or not re.fullmatch(r"[\w-]+", pid):
        raise HTTPException(404)
    f = (config.OUTPUT / pid / name).resolve()
    if not f.is_file() or config.OUTPUT.resolve() not in f.parents:
        raise HTTPException(404)
    return FileResponse(f)


@app.get("/api/learning")
def learning():
    pats = db.rows("SELECT * FROM patterns ORDER BY n DESC LIMIT 400")
    for p in pats:
        p["score"] = round(learner.score(p), 4)
    recent = db.rows("""SELECT v.id, v.title, v.channel_title, v.views, v.views_per_day, v.outlier_score, a.summary,
        a.perf_percentile, a.created_at FROM analyses a JOIN videos v ON v.id=a.video_id ORDER BY a.created_at DESC LIMIT 40""")
    return {"playbook": learner.playbook(), "patterns": pats, "recent": recent}


@app.post("/api/learning/run")
def learning_run():
    threading.Thread(target=SCHED.tick, daemon=True).start()
    return {"ok": True}


class AddVideo(BaseModel):
    url: str = Field(max_length=300)
    niche: str = Field(default="", max_length=100)
    favorite: bool = False


@app.post("/api/learning/add")
def add_video(body: AddVideo):
    m = re.search(r"(?:v=|youtu\.be/|shorts/|embed/)([A-Za-z0-9_-]{11})", body.url) or re.fullmatch(r"([A-Za-z0-9_-]{11})", body.url.strip())
    if not m:
        raise HTTPException(400, "유튜브 주소를 인식할 수 없습니다")
    try:
        vids = youtube_data.videos([m.group(1)])
    except Exception:
        vids = None
    if not vids:
        if not body.favorite:
            raise HTTPException(404, "영상 정보를 가져오지 못했습니다 (YouTube API 키 확인)")
        with db.connect() as con:  # a taste reference works even without a YouTube key
            con.execute("""INSERT INTO videos(id,title,outlier_score,niche,query,status,discovered_at,favorite)
                VALUES(?,?,999,?,'reference','queued',?,1) ON CONFLICT(id) DO UPDATE SET favorite=1, status='queued'""",
                        (m.group(1), "레퍼런스 " + m.group(1), body.niche, db.now()))
        return {"ok": True, "title": "레퍼런스 " + m.group(1)}
    v = vids[0]
    subs = youtube_data.channel_subscribers([v["channel_id"]]).get(v["channel_id"])
    with db.connect() as con:
        con.execute("""INSERT OR REPLACE INTO videos(id,title,channel_id,channel_title,published,duration_seconds,views,likes,
            comments,subscribers,views_per_day,outlier_score,engagement_rate,description,tags,thumbnail_url,niche,query,status,
            discovered_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'queued',?)""",
                    (v["id"], v["title"], v["channel_id"], v["channel_title"], v["published"], v["duration_seconds"],
                     v["views"], v["likes"], v["comments"], subs, 0, 999, 0, v["description"], "[]", v["thumbnail_url"],
                     body.niche, "manual", db.now()))
        if body.favorite:
            con.execute("UPDATE videos SET favorite=1 WHERE id=?", (v["id"],))
    return {"ok": True, "title": v["title"]}


class StudyReq(BaseModel):
    url: str = Field(min_length=3, max_length=300)
    channel_id: str = Field(default="", max_length=40)
    favorite: bool = False
    max_videos: int = Field(default=200, ge=20, le=500)


STUDY_STATE = {"running": None, "message": ""}


@app.post("/api/study")
def study_channel(body: StudyReq):
    from . import channel_study
    if STUDY_STATE["running"]:
        raise HTTPException(409, "다른 채널을 학습하는 중입니다. 잠시 후 다시 시도하세요")

    def job():
        STUDY_STATE.update(running=body.url, message="채널 영상 목록을 읽는 중…")
        try:
            r = channel_study.study(body.url, body.channel_id, body.max_videos, favorite=body.favorite)
            STUDY_STATE["message"] = f"{r['channel']['title']} 분석 완료 → 여러 채널 종합해서 빈자리 주제 만드는 중…"
            channel_study.synthesize(body.channel_id)
            STUDY_STATE["message"] = (f"완료: {r['channel']['title']} — 롱폼 {r['studied']}편 비교, {r['queued']}편 정밀 분석 대기, "
                                      "종합 분석·빈자리 주제 갱신 (채널 탭에서 확인)")
        except Exception as ex:
            STUDY_STATE["message"] = "실패: " + str(ex)[:300]
        finally:
            STUDY_STATE["running"] = None
    threading.Thread(target=job, daemon=True).start()
    return {"ok": True}


@app.post("/api/channels/{cid}/synth")
def channel_synth(cid: str):
    from . import channel_study
    try:
        return channel_study.synthesize("" if cid == "_all" else cid)
    except ValueError as ex:
        raise HTTPException(400, str(ex))


@app.get("/api/study")
def list_studies():
    from . import channel_study
    return {"studies": channel_study.all_studies(), **STUDY_STATE}


@app.post("/api/settings")
async def save_settings(request: Request):
    patch = await request.json()
    allowed = {"learning", "production", "providers", "prices", "budget", "youtube_quota_per_day", "update"}
    patch = {k: v for k, v in patch.items() if k in allowed}
    return config.save_settings(patch)


def _test_gemini_key():
    """Real check (not just 'a key is saved'): list one model with the key."""
    from . import http
    k = config.get_secret("GEMINI_API_KEY")
    if not k:
        res = {"ok": False, "message": "키 없음"}
    else:
        try:
            http.get_json("https://generativelanguage.googleapis.com/v1beta/models", headers={"x-goog-api-key": k},
                          params={"pageSize": 1}, retries=0, timeout=20)
            res = {"ok": True, "message": "정상"}
        except Exception as ex:
            body = getattr(ex, "body", "") or str(ex)
            try:
                body = json.loads(body).get("error", {}).get("message", body)
            except ValueError:
                pass
            res = {"ok": False, "message": str(body)[:200]}
    res["key_tail"] = k[-4:] if k else ""
    db.kv_set("gemini_key_check", res)
    return res


@app.post("/api/secrets")
async def save_secrets(request: Request):
    data = await request.json()
    config.set_secrets(data)
    out = config.secret_status()
    if data.get("GEMINI_API_KEY"):
        out["gemini_check"] = _test_gemini_key()
    return out


@app.post("/api/align/install")
def align_install():
    if db.kv_get("align_install", {}).get("state") == "running":
        return {"ok": True}

    def job():
        db.kv_set("align_install", {"state": "running", "message": "설치 중… (2~5분)"})
        try:
            align.install(lambda m: db.kv_set("align_install", {"state": "running", "message": m}))
            db.kv_set("align_install", {"state": "done", "message": "설치 완료"})
            db.event("info", "setup", "자막 싱크 정밀 맞춤(faster-whisper) 설치 완료")
        except Exception as ex:
            db.kv_set("align_install", {"state": "failed", "message": str(ex)[:300]})
            db.event("warn", "setup", "faster-whisper 설치 실패: " + str(ex)[:200])
    threading.Thread(target=job, daemon=True).start()
    return {"ok": True}


@app.post("/api/secrets/test")
def test_keys():
    return {"gemini": _test_gemini_key()}


@app.post("/api/youtube/connect")
def youtube_connect(channel_id: str | None = None):
    from . import youtube_upload
    if not youtube_upload.client_file().exists():
        raise HTTPException(400, "data 폴더에 client_secret.json 을 먼저 넣어주세요")
    if channel_id and not channels.get(channel_id):
        raise HTTPException(404)
    threading.Thread(target=youtube_upload.authorize, args=(channel_id,), daemon=True).start()
    return {"ok": True, "message": "브라우저에서 이 채널의 Google 계정으로 로그인·권한 허용을 진행하세요"}


# ---------------- channels ----------------
@app.get("/api/channels")
def list_channels():
    from . import youtube_upload
    out = []
    for c in channels.all_channels():
        c["connected"] = youtube_upload.connected(c["id"])
        c["yt_channel"] = youtube_upload.channel_info(c["id"]) if c["connected"] else None
        c["videos"] = db.one("SELECT COUNT(*) n FROM productions WHERE channel_id=?", (c["id"],))["n"]
        c["watched"] = db.one("SELECT COUNT(*) n FROM videos WHERE niche=? AND status='watched'", (c["id"],))["n"]
        c["radar"] = db.kv_get("radar:" + c["id"])
        from . import channel_study
        c["synth"] = channel_study.get_synth(c["id"])
        c["concepts"] = channels.concepts(c["id"])
        out.append(c)
    return {"channels": out, "presets": channels.PRESETS}


@app.post("/api/channels")
async def save_channel(request: Request):
    data = await request.json()
    if data.get("preset"):
        return channels.add_preset(data["preset"])
    if not str(data.get("name", "")).strip():
        raise HTTPException(400, "채널 이름이 필요합니다")
    return channels.save(data)


class Feedback(BaseModel):
    feedback: str = Field(min_length=2, max_length=2000)


@app.post("/api/channels/{cid}/concepts")
async def save_concept(cid: str, request: Request):
    if not channels.get(cid):
        raise HTTPException(404)
    data = await request.json()
    if data.get("delete"):
        channels.delete_concept(cid, data["delete"])
        return {"ok": True}
    if not str(data.get("name", "")).strip():
        raise HTTPException(400, "컨셉 이름이 필요합니다")
    return channels.save_concept(cid, data)


@app.post("/api/channels/{cid}/feedback")
def channel_feedback(cid: str, body: Feedback):
    if not channels.get(cid):
        raise HTTPException(404)
    return {"style_guide": channels.learn_feedback(cid, body.feedback)}


@app.post("/api/channels/{cid}/radar/refresh")
def radar_refresh(cid: str):
    if not channels.get(cid):
        raise HTTPException(404)
    return {"added": radar.refresh(cid)}


@app.post("/api/channels/{cid}/radar/suggest")
def radar_suggest(cid: str):
    if not channels.get(cid):
        raise HTTPException(404)
    return radar.suggest(cid)


# ---------------- voices ----------------
@app.get("/api/voices")
def list_voices():
    return voices.all_voices()


class VoiceUpload(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    engines: list[str] = Field(default_factory=lambda: ["gemini"])
    source_b64: str = Field(min_length=10)
    source_ext: str = Field(default=".wav", max_length=8)
    consent_b64: str = Field(default="")
    consent_ext: str = Field(default=".wav", max_length=8)


def _ext(e):
    e = "." + re.sub(r"[^a-z0-9]", "", e.lower().lstrip("."))[:5]
    return e if e != "." else ".wav"


@app.post("/api/voices/register")
def register_voice(body: VoiceUpload):
    src = voices.save_upload(body.source_b64, _ext(body.source_ext))
    con = voices.save_upload(body.consent_b64, _ext(body.consent_ext)) if body.consent_b64 else None
    results, errors = [], []
    try:
        if "gemini" in body.engines:
            if not con:
                errors.append("Gemini: 동의 녹음이 필요합니다")
            else:
                try:
                    results.append(voices.register_gemini(body.name + " (Gemini)", src, con))
                except Exception as ex:
                    errors.append("Gemini: " + str(ex)[:300])
        if "elevenlabs" in body.engines:
            try:
                results.append(voices.register_elevenlabs(body.name + " (ElevenLabs)", [src]))
            except Exception as ex:
                errors.append("ElevenLabs: " + str(ex)[:300])
    finally:
        for p in (src, con):
            if p:
                Path(p).unlink(missing_ok=True)
    return {"registered": results, "errors": errors}


class VoiceManual(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    engine: str = Field(pattern="^(gemini|elevenlabs)$")
    ref: str = Field(min_length=2, max_length=120)


@app.get("/api/voices/remote")
def remote_voices():
    """Voices already stored in your Gemini project (e.g. made in AI Studio) -> import with one click."""
    try:
        return {"voices": voices.list_gemini_remote()}
    except Exception as ex:
        raise HTTPException(400, str(ex)[:400])


@app.post("/api/voices/manual")
def manual_voice(body: VoiceManual):
    return voices.add_manual(body.name, body.engine, body.ref)


@app.post("/api/voices/{vid}/delete")
def delete_voice(vid: str):
    try:
        return {"note": voices.delete(vid)}
    except KeyError:
        raise HTTPException(404)


class Audition(BaseModel):
    text: str = Field(min_length=2, max_length=600)
    candidates: list[dict] = Field(min_length=1, max_length=6)
    tone: str = Field(default="", max_length=200)


@app.post("/api/voices/audition")
def audition(body: Audition):
    cands = [dict(c, tone=body.tone) for c in body.candidates]
    return {"results": voices.audition(body.text, cands)}


@app.get("/audition/{name}")
def audition_file(name: str):
    if not re.fullmatch(r"a\d{14}_\d\.mp3", name):
        raise HTTPException(404)
    f = config.OUTPUT / "auditions" / name
    if not f.is_file():
        raise HTTPException(404)
    return FileResponse(f)


# ---------------- usage ----------------
@app.get("/api/usage")
def usage_overview(monthly_budget: float | None = None):
    return {"cadence": usage.cadence(monthly_budget), "today": budget.summary()}


def main():
    import sys
    import time
    import socket
    import uvicorn
    config.ensure_dirs()
    db.init()
    port = config.load_settings().get("web_port", 8770)
    for _ in range(40):  # after an update the old engine may still be closing
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", port)) != 0:
                break
        time.sleep(0.5)
    print(f"AutoTube Studio → http://127.0.0.1:{port}")
    if "--no-browser" not in sys.argv:
        import webbrowser
        threading.Timer(2.5, lambda: webbrowser.open(f"http://127.0.0.1:{port}")).start()
    uvicorn.run(app, host="127.0.0.1", port=port, access_log=False, log_config=None)


if __name__ == "__main__":
    main()
