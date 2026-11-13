# -*- coding: utf-8 -*-
"""'내 목소리' — register the owner's voice with Gemini (voice replication) and/or
ElevenLabs (instant voice clone), keep a local list, and audition voices side by side.

Gemini needs two recordings by the SAME adult speaker:
  source  : 10~30 s of clean natural speech (we trim to 28 s)
  consent : the speaker reading the exact consent sentence of a supported locale
Both are converted to 24 kHz mono 16-bit WAV, as Google recommends.
Recordings stay on this PC and go only to the service you choose.
"""
from __future__ import annotations
import base64
import json
import subprocess
import time
import urllib.request
import uuid
from pathlib import Path

from . import config, db, http, tts

CONSENT = {
    "ko-KR": "나는 이 음성의 소유자이며 구글이 이 음성을 사용하여 음성 합성 모델을 생성할 것을 허용합니다.",
    "en-US": "I am the owner of this voice and I consent to Google using this voice to create a synthetic voice model.",
}

SAMPLE_SCRIPT = ("안녕하세요. 오늘은 우리가 매일 보면서도 잘 모르는 이야기를 해 보려고 합니다. "
                 "밤하늘의 별은 왜 반짝일까요? 사실 별빛이 흔들리는 건 별 때문이 아니라, 우리 머리 위의 공기 때문입니다. "
                 "놀랍지 않나요? 지금부터 그 이유를 하나씩 따라가 보겠습니다.")


class VoiceError(RuntimeError):
    pass


def voices_dir():
    p = config.DATA / "voices"
    p.mkdir(parents=True, exist_ok=True)
    return p


def to_wav(src, out, max_seconds=None):
    cmd = [config.ffmpeg_bin(), "-y", "-loglevel", "error", "-i", str(src), "-ac", "1", "-ar", "24000",
           "-sample_fmt", "s16", "-af", "silenceremove=start_periods=1:start_threshold=-45dB"]
    if max_seconds:
        cmd += ["-t", str(max_seconds)]
    cmd.append(str(out))
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise VoiceError("녹음 파일을 읽을 수 없습니다: " + r.stderr[-200:])
    return str(out)


def _seconds(path):
    from . import media
    return float(media.duration(path))


def save_upload(b64data, suffix):
    raw = base64.b64decode(b64data.split(",", 1)[-1])
    if len(raw) > 40 * 1024 * 1024:
        raise VoiceError("파일이 너무 큽니다 (40MB 이하)")
    p = voices_dir() / f"upload_{uuid.uuid4().hex[:8]}{suffix}"
    p.write_bytes(raw)
    return p


def _add(name, engine, ref, kind="clone", note=""):
    vid = "v" + uuid.uuid4().hex[:8]
    with db.connect() as con:
        con.execute("INSERT INTO voices(id,name,engine,ref,kind,note,created_at) VALUES(?,?,?,?,?,?,?)",
                    (vid, name, engine, ref, kind, note, db.now()))
    return get(vid)


def get(vid):
    return db.one("SELECT * FROM voices WHERE id=?", (vid,))


def all_voices():
    mine = db.rows("SELECT * FROM voices ORDER BY created_at")
    for v in mine:
        v["spec"] = {"engine": v["engine"], "ref": v["ref"]}
    prebuilt = [{"id": "gemini:" + n, "name": f"Gemini 기본 목소리 {n}", "engine": "gemini", "ref": n, "kind": "prebuilt",
                 "spec": {"engine": "gemini", "ref": n}} for n in tts.GEMINI_PREBUILT]
    edge = [{"id": "edge:default", "name": "무료 Edge 목소리 (테스트용)", "engine": "edge", "ref": "", "kind": "prebuilt",
             "spec": {"engine": "edge", "ref": ""}}]
    return {"mine": mine, "prebuilt": prebuilt + edge, "consent": CONSENT, "sample_script": SAMPLE_SCRIPT}


def register_gemini(name, source_path, consent_path):
    key = config.get_secret("GEMINI_API_KEY")
    if not key:
        raise VoiceError("Gemini API 키가 필요합니다")
    d = voices_dir()
    src = to_wav(source_path, d / f"g_src_{uuid.uuid4().hex[:6]}.wav", max_seconds=28)
    con = to_wav(consent_path, d / f"g_consent_{uuid.uuid4().hex[:6]}.wav", max_seconds=30)
    if _seconds(src) < 10:
        raise VoiceError("목소리 샘플은 최소 10초 이상이어야 합니다")
    b64 = lambda p: base64.b64encode(Path(p).read_bytes()).decode()
    model = config.load_settings()["providers"]["gemini_tts_model"]
    body = {"store": True, "voice": {"model": model, "type": "replicated", "display_name": name[:60],
                                    "replicated": {"source_audio": {"mime_type": "audio/wav", "data": b64(src)},
                                                   "consent_audio": {"mime_type": "audio/wav", "data": b64(con)}}}}
    try:
        res = http.post_json(tts.GEMINI_BASE + "/voices", body, headers={"x-goog-api-key": key}, timeout=300, retries=0)
    except http.HttpError as ex:
        detail = _google_error(ex)
        raise VoiceError(f"Gemini 등록 실패 ({ex.status}): {detail} — 동의 문장을 정확히 읽었는지, "
                         "두 녹음이 같은 사람·같은 마이크인지 확인하세요.") from ex
    ref = res.get("id") or res.get("name", "").split("/")[-1] or (res.get("voice") or {}).get("id")
    if not ref:
        raise VoiceError("Gemini가 목소리 ID를 돌려주지 않았습니다: " + json.dumps(res)[:200])
    return _add(name, "gemini", ref, "clone", "Gemini 음성 복제 (1년 보관)")


def _google_error(ex):
    try:
        e = json.loads(getattr(ex, "body", "") or "{}").get("error", {})
        return (e.get("message") or str(ex))[:400]
    except ValueError:
        return (getattr(ex, "body", "") or str(ex))[:400]


def list_gemini_remote():
    """GET /v1beta/voices — replicated + designed voices stored in this API key's project."""
    key = config.get_secret("GEMINI_API_KEY")
    if not key:
        raise VoiceError("Gemini API 키가 필요합니다")
    out, seen = [], {r["ref"] for r in db.rows("SELECT ref FROM voices")}
    for typ in ("replicated", "prompted"):
        try:
            res = http.get_json(tts.GEMINI_BASE + "/voices", headers={"x-goog-api-key": key},
                                params={"type": typ, "pageSize": 100}, retries=1)
        except http.HttpError as ex:
            if typ == "replicated":
                raise VoiceError("Gemini 목소리 목록을 못 가져왔습니다: " + _google_error(ex)) from ex
            continue
        for v in res.get("voices") or res.get("items") or []:
            ref = v.get("id") or str(v.get("name", "")).split("/")[-1]
            if not ref or not ref.startswith(("voice_", "voicekey_")):
                continue
            out.append({"ref": ref, "name": v.get("display_name") or v.get("displayName") or ref,
                        "type": v.get("type") or typ, "created": v.get("create_time") or v.get("createTime") or "",
                        "imported": ref in seen})
    uniq = {}
    for v in out:
        uniq.setdefault(v["ref"], v)
    return list(uniq.values())


def _multipart(fields, files):
    boundary = "----autotube" + uuid.uuid4().hex
    out = b""
    for k, v in fields.items():
        out += f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode()
    for k, path in files:
        out += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"; filename=\"{Path(path).name}\"\r\n"
                "Content-Type: audio/wav\r\n\r\n").encode() + Path(path).read_bytes() + b"\r\n"
    out += f"--{boundary}--\r\n".encode()
    return out, "multipart/form-data; boundary=" + boundary


def register_elevenlabs(name, sample_paths):
    key = config.get_secret("ELEVENLABS_API_KEY")
    if not key:
        raise VoiceError("ElevenLabs API 키가 필요합니다")
    d = voices_dir()
    wavs = [to_wav(p, d / f"e_{uuid.uuid4().hex[:6]}.wav", max_seconds=300) for p in sample_paths]
    data, ctype = _multipart({"name": name[:60], "remove_background_noise": "true",
                              "description": "AutoTube Studio - owner's own voice"},
                             [("files", w) for w in wavs])
    try:
        res = http.request("POST", "https://api.elevenlabs.io/v1/voices/add", data=data,
                           headers={"xi-api-key": key, "Content-Type": ctype}, timeout=300, retries=0)
    except http.HttpError as ex:
        raise VoiceError("ElevenLabs 등록 실패 (요금제에 목소리 복제가 포함되어 있는지 확인): " + str(ex)[:300]) from ex
    ref = res.get("voice_id")
    if not ref:
        raise VoiceError("ElevenLabs가 목소리 ID를 돌려주지 않았습니다")
    return _add(name, "elevenlabs", ref, "clone", "ElevenLabs 즉시 복제")


def add_manual(name, engine, ref):
    """Paste an existing id (e.g. a voice designed in AI Studio or the ElevenLabs website)."""
    if engine not in ("gemini", "elevenlabs") or not ref.strip():
        raise VoiceError("엔진과 ID를 확인하세요")
    return _add(name, engine, ref.strip(), "manual", "직접 입력")


def delete(vid, remote=True):
    v = get(vid)
    if not v:
        raise KeyError(vid)
    note = ""
    if remote and v["kind"] == "clone":
        try:
            if v["engine"] == "gemini":
                http.request("DELETE", f"{tts.GEMINI_BASE}/voices/{v['ref']}",
                             headers={"x-goog-api-key": config.get_secret("GEMINI_API_KEY")}, retries=0)
            elif v["engine"] == "elevenlabs":
                http.request("DELETE", f"https://api.elevenlabs.io/v1/voices/{v['ref']}",
                             headers={"xi-api-key": config.get_secret("ELEVENLABS_API_KEY")}, retries=0)
        except Exception as ex:
            note = "서비스 쪽 삭제 실패(사이트에서 직접 삭제하세요): " + str(ex)[:120]
    db.execute("DELETE FROM voices WHERE id=?", (vid,))
    return note


def audition(text, specs, synth=None):
    """Same lines, several voices -> files to compare by ear. Returns [{label, file|error}]."""
    synth = synth or tts.synthesize
    folder = config.OUTPUT / "auditions"
    folder.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d%H%M%S")
    out = []
    for i, sp in enumerate(specs[:6]):
        voice = tts.parse_voice(sp.get("spec") or sp)
        label = sp.get("label") or (voice or {}).get("engine", "?")
        name = f"a{stamp}_{i}.mp3"
        try:
            style = tts.style_for(sp.get("emotion", "curiosity"), sp.get("tone", ""))
            synth(text, str(folder / name), voice=voice, style=style, order=[voice["engine"]])
            out.append({"label": label, "file": name})
        except Exception as ex:
            out.append({"label": label, "error": str(ex)[:200]})
    return out
