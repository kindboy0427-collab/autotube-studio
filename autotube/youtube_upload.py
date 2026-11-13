# -*- coding: utf-8 -*-
"""Your own channel (OAuth): private upload, publish, thumbnail, captions, analytics.

One-time setup: put the Google Cloud "Desktop app" OAuth client JSON at
data/client_secret.json, then run CONNECT_YOUTUBE.cmd (opens the browser once).
"""
from __future__ import annotations
import datetime as dt

from . import config, budget

SCOPES = ["https://www.googleapis.com/auth/youtube.upload",
          "https://www.googleapis.com/auth/youtube.force-ssl",
          "https://www.googleapis.com/auth/yt-analytics.readonly"]


class UploadError(RuntimeError):
    pass


def client_file():
    return config.DATA / "client_secret.json"


def token_file(channel_id=None):
    from . import channels
    return channels.token_file(channel_id)


def connected(channel_id=None):
    return token_file(channel_id).exists()


def authorize(channel_id=None):
    from google_auth_oauthlib.flow import InstalledAppFlow
    if not client_file().exists():
        raise UploadError("data/client_secret.json 이 없습니다 (Google Cloud 콘솔 → OAuth 클라이언트 → 데스크톱 앱)")
    flow = InstalledAppFlow.from_client_secrets_file(str(client_file()), SCOPES)
    creds = flow.run_local_server(port=0, prompt="consent")
    token_file(channel_id).write_text(creds.to_json(), encoding="utf-8")
    return True


def _creds(channel_id=None):
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    tf = token_file(channel_id)
    if not tf.exists():
        raise UploadError("이 채널의 YouTube 계정이 연결되지 않았습니다 (채널 탭 → 연결)")
    creds = Credentials.from_authorized_user_file(str(tf), SCOPES)
    if not creds.valid and creds.refresh_token:
        creds.refresh(Request())
        tf.write_text(creds.to_json(), encoding="utf-8")
    return creds


def _service(name="youtube", version="v3", channel_id=None):
    from googleapiclient.discovery import build
    return build(name, version, credentials=_creds(channel_id), cache_discovery=False)


def upload(video_path, title, description, tags, privacy="private", thumbnail=None, srt=None,
           category_id="27", language="ko", synthetic=True, channel_id=None):
    from googleapiclient.http import MediaFileUpload
    if not budget.take_quota("upload"):
        raise UploadError("오늘 YouTube 업로드 할당량 부족")
    yt = _service(channel_id=channel_id)
    body = {"snippet": {"title": title[:100], "description": description[:4900], "tags": tags[:30],
                        "categoryId": category_id, "defaultLanguage": language, "defaultAudioLanguage": language},
            "status": {"privacyStatus": privacy, "selfDeclaredMadeForKids": False,
                       "containsSyntheticMedia": bool(synthetic)}}
    req = yt.videos().insert(part="snippet,status", body=body,
                             media_body=MediaFileUpload(video_path, chunksize=8 * 1024 * 1024, resumable=True))
    resp = None
    while resp is None:
        _, resp = req.next_chunk()
    vid = resp["id"]
    notes = []
    if thumbnail and budget.take_quota("thumbnail"):
        try:
            yt.thumbnails().set(videoId=vid, media_body=MediaFileUpload(thumbnail)).execute()
        except Exception as ex:  # unverified channels cannot set custom thumbnails
            notes.append("썸네일 실패(채널 인증 필요할 수 있음): " + str(ex)[:120])
    if srt and budget.take_quota("captions"):
        try:
            yt.captions().insert(part="snippet", body={"snippet": {"videoId": vid, "language": language, "name": "한국어"}},
                                 media_body=MediaFileUpload(srt, mimetype="application/octet-stream")).execute()
        except Exception as ex:
            notes.append("자막 업로드 실패: " + str(ex)[:120])
    return vid, notes


def set_privacy(video_id, privacy="public", publish_at=None, synthetic=True, channel_id=None):
    if not budget.take_quota("update"):
        raise UploadError("오늘 YouTube 할당량 부족")
    status = {"privacyStatus": "private" if publish_at else privacy, "selfDeclaredMadeForKids": False,
              "containsSyntheticMedia": bool(synthetic)}
    if publish_at:
        status["publishAt"] = publish_at
    _service(channel_id=channel_id).videos().update(part="status", body={"id": video_id, "status": status}).execute()
    return True


def analytics(video_id, published_iso, channel_id=None):
    """Average % viewed / duration for YOUR video (needs yt-analytics scope)."""
    start = (published_iso or dt.date.today().isoformat())[:10]
    end = dt.date.today().isoformat()
    ya = _service("youtubeAnalytics", "v2", channel_id)
    res = ya.reports().query(ids="channel==MINE", startDate=start, endDate=end, filters=f"video=={video_id}",
                             metrics="views,averageViewPercentage,averageViewDuration,likes,comments").execute()
    rows = res.get("rows") or []
    if not rows:
        return None
    views, pct, dur, likes, comments = rows[0][:5]
    return {"views": int(views), "avg_view_percentage": float(pct), "avg_view_duration": float(dur),
            "likes": int(likes), "comments": int(comments)}


def video_stats(video_ids, channel_id=None):
    yt = _service(channel_id=channel_id)
    res = yt.videos().list(part="statistics,status,snippet", id=",".join(video_ids)).execute()
    out = {}
    for it in res.get("items", []):
        st = it.get("statistics", {})
        out[it["id"]] = {"views": int(st.get("viewCount", 0)), "likes": int(st.get("likeCount", 0)),
                         "comments": int(st.get("commentCount", 0)), "privacy": it["status"]["privacyStatus"],
                         "published": it["snippet"].get("publishedAt")}
    return out


if __name__ == "__main__":
    import sys
    config.ensure_dirs()
    cid = sys.argv[1] if len(sys.argv) > 1 else None
    authorize(cid)
    print("YouTube 채널 연결 완료:", token_file(cid))
