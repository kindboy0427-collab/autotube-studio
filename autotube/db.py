# -*- coding: utf-8 -*-
"""SQLite storage. One file: data/autotube.sqlite3 (WAL, thread-safe connections)."""
from __future__ import annotations
import datetime as dt
import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from pathlib import Path

from . import config

KST = dt.timezone(dt.timedelta(hours=9))
_LOCK = threading.RLock()

SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS videos(
  id TEXT PRIMARY KEY, title TEXT, channel_id TEXT, channel_title TEXT, published TEXT,
  duration_seconds INTEGER, views INTEGER, likes INTEGER, comments INTEGER,
  subscribers INTEGER, views_per_day REAL, outlier_score REAL, engagement_rate REAL,
  description TEXT, tags TEXT, thumbnail_url TEXT, niche TEXT, query TEXT,
  status TEXT DEFAULT 'queued', discovered_at TEXT, watched_at TEXT, error TEXT);
CREATE INDEX IF NOT EXISTS ix_videos_status ON videos(status, outlier_score DESC);
CREATE TABLE IF NOT EXISTS analyses(
  video_id TEXT PRIMARY KEY, created_at TEXT, model TEXT, summary TEXT, topic TEXT,
  features TEXT NOT NULL, raw TEXT NOT NULL, perf_percentile REAL,
  top_comments TEXT, FOREIGN KEY(video_id) REFERENCES videos(id));
CREATE VIRTUAL TABLE IF NOT EXISTS analyses_fts USING fts5(video_id UNINDEXED, topic, summary, title);
CREATE TABLE IF NOT EXISTS patterns(
  feature TEXT PRIMARY KEY, category TEXT, n INTEGER, lift REAL, mean_perf REAL,
  own_n INTEGER DEFAULT 0, own_lift REAL DEFAULT 0, examples TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS playbook(
  id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT, model TEXT, content TEXT, basis TEXT);
CREATE TABLE IF NOT EXISTS productions(
  id TEXT PRIMARY KEY, created_at TEXT, updated_at TEXT, topic TEXT, instructions TEXT,
  status TEXT, progress REAL DEFAULT 0, step TEXT, plan TEXT, features_used TEXT,
  output_dir TEXT, video_path TEXT, thumbnail_path TEXT, srt_path TEXT,
  youtube_id TEXT, privacy TEXT, cost_usd REAL DEFAULT 0, error TEXT, log TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS performance(
  id INTEGER PRIMARY KEY AUTOINCREMENT, production_id TEXT, youtube_id TEXT, measured_at TEXT,
  views INTEGER, likes INTEGER, comments INTEGER, avg_view_percentage REAL, avg_view_duration REAL,
  ctr REAL, source TEXT);
CREATE TABLE IF NOT EXISTS usage(
  id INTEGER PRIMARY KEY AUTOINCREMENT, day TEXT, service TEXT, units REAL, cost_usd REAL,
  detail TEXT, production_id TEXT, created_at TEXT);
CREATE INDEX IF NOT EXISTS ix_usage_day ON usage(day, service);
CREATE TABLE IF NOT EXISTS events(
  id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT, level TEXT, area TEXT, message TEXT);
CREATE TABLE IF NOT EXISTS kv(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS channels(
  id TEXT PRIMARY KEY, name TEXT NOT NULL, niches TEXT NOT NULL DEFAULT '[]', audience TEXT DEFAULT '',
  tone TEXT DEFAULT '', voice_id TEXT DEFAULT '', target_minutes INTEGER DEFAULT 10,
  style_guide TEXT DEFAULT '', monthly_videos INTEGER DEFAULT 8, active INTEGER DEFAULT 1, created_at TEXT);
CREATE TABLE IF NOT EXISTS voices(
  id TEXT PRIMARY KEY, name TEXT NOT NULL, engine TEXT NOT NULL, ref TEXT NOT NULL,
  kind TEXT DEFAULT 'clone', note TEXT DEFAULT '', created_at TEXT);
"""

MIGRATIONS = [
    ("productions", "channel_id", "TEXT"),
    ("productions", "step_times", "TEXT"),
    ("productions", "finished_at", "TEXT"),
    ("productions", "script_format", "TEXT"),
    ("videos", "favorite", "INTEGER DEFAULT 0"),
    ("channels", "voice", "TEXT DEFAULT ''"),
]


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def kst_day(moment=None):
    return (moment or dt.datetime.now(KST)).astimezone(KST).date().isoformat()


def db_path() -> Path:
    return config.DATA / "autotube.sqlite3"


@contextmanager
def connect():
    config.ensure_dirs()
    con = sqlite3.connect(str(db_path()), timeout=30, check_same_thread=False)
    con.row_factory = sqlite3.Row
    try:
        yield con
        con.commit()
    finally:
        con.close()


def init():
    with _LOCK, connect() as con:
        con.executescript(SCHEMA)
        for table, col, typ in MIGRATIONS:
            cols = {r["name"] for r in con.execute(f"PRAGMA table_info({table})")}
            if col not in cols:
                con.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")


def event(level, area, message):
    with connect() as con:
        con.execute("INSERT INTO events(created_at,level,area,message) VALUES(?,?,?,?)",
                    (now(), level, area, str(message)[:900]))


def events(limit=50):
    with connect() as con:
        return [dict(r) for r in con.execute("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,))]


def kv_get(key, default=None):
    with connect() as con:
        r = con.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return json.loads(r["value"]) if r else default


def kv_set(key, value):
    with connect() as con:
        con.execute("INSERT OR REPLACE INTO kv(key,value) VALUES(?,?)", (key, json.dumps(value, ensure_ascii=False)))


def new_id():
    return dt.datetime.now(KST).strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]


def rows(sql, args=()):
    with connect() as con:
        return [dict(r) for r in con.execute(sql, args)]


def one(sql, args=()):
    with connect() as con:
        r = con.execute(sql, args).fetchone()
        return dict(r) if r else None


def execute(sql, args=()):
    with connect() as con:
        cur = con.execute(sql, args)
        return cur.rowcount
