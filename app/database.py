import sqlite3
import os
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "engine.db"


def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    conn = get_db()
    cur = conn.cursor()
    cur.executescript("""
CREATE TABLE IF NOT EXISTS niches (
  id INTEGER PRIMARY KEY, name TEXT UNIQUE, rubric_json TEXT, created_at TEXT
);
CREATE TABLE IF NOT EXISTS channels (
  id INTEGER PRIMARY KEY, yt_channel_id TEXT UNIQUE, handle TEXT, name TEXT,
  subs INTEGER, median_views INTEGER, video_count INTEGER,
  format_class TEXT,
  niche_id INTEGER, data_source TEXT,
  last_fetched TEXT
);
CREATE TABLE IF NOT EXISTS videos (
  id INTEGER PRIMARY KEY, yt_video_id TEXT, channel_id INTEGER,
  title TEXT, views INTEGER, published_at TEXT, duration_sec INTEGER,
  outlier_score REAL,
  is_short INTEGER DEFAULT 0,
  data_source TEXT,
  confidence TEXT,
  niche_id INTEGER, last_fetched TEXT,
  UNIQUE(yt_video_id), UNIQUE(channel_id, title)
);
CREATE TABLE IF NOT EXISTS frameworks (
  id INTEGER PRIMARY KEY, name TEXT, pattern TEXT, mechanics TEXT,
  example TEXT, source_doc TEXT, niche_id INTEGER
);
CREATE TABLE IF NOT EXISTS patterns (
  id INTEGER PRIMARY KEY, niche_id INTEGER, topic TEXT, frame TEXT,
  mechanics_json TEXT, evidence_video_ids TEXT, created_at TEXT
);
CREATE TABLE IF NOT EXISTS ideas (
  id INTEGER PRIMARY KEY, niche_id INTEGER, run_id INTEGER,
  title TEXT, one_line TEXT, frame_used TEXT, angle_note TEXT,
  structural_score REAL, demand_score REAL, staleness_mult REAL,
  freshness_gate INTEGER,
  final_score REAL, verdict TEXT, evidence_json TEXT,
  status TEXT DEFAULT 'new',
  created_at TEXT
);
CREATE TABLE IF NOT EXISTS validations (
  id INTEGER PRIMARY KEY, idea_id INTEGER, matched_yt_video_id TEXT,
  matched_title TEXT, matched_views INTEGER, matched_channel_subs INTEGER,
  matched_channel_median INTEGER, relative_perf REAL, format_match INTEGER,
  age_months REAL, check_type TEXT
);
CREATE TABLE IF NOT EXISTS shipped_results (
  id INTEGER PRIMARY KEY, idea_id INTEGER, video_url TEXT,
  views INTEGER, own_channel_median INTEGER, own_outlier_score REAL, logged_at TEXT
);
CREATE TABLE IF NOT EXISTS seeds (
  id INTEGER PRIMARY KEY, niche_id INTEGER, source TEXT, text TEXT,
  why_compelling TEXT, score INTEGER, url TEXT, fetched_at TEXT
);
CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY, niche_id INTEGER, started_at TEXT,
  quota_used INTEGER DEFAULT 0, ideas_generated INTEGER DEFAULT 0,
  ideas_survived INTEGER DEFAULT 0, finished_at TEXT, report_path TEXT
);
CREATE TABLE IF NOT EXISTS jobs (
  id INTEGER PRIMARY KEY, type TEXT, niche_id INTEGER,
  status TEXT DEFAULT 'queued',
  stage TEXT, progress_pct INTEGER DEFAULT 0, message TEXT,
  started_at TEXT, finished_at TEXT, error TEXT, result_json TEXT
);
CREATE TABLE IF NOT EXISTS api_cache (
  key TEXT PRIMARY KEY, response_json TEXT, fetched_at TEXT
);
CREATE TABLE IF NOT EXISTS hunts (
  id INTEGER PRIMARY KEY, started_at TEXT, finished_at TEXT,
  seed_keywords TEXT, region TEXT, max_channel_age_days INTEGER,
  min_breakout_views INTEGER, quota_used INTEGER DEFAULT 0,
  results_found INTEGER DEFAULT 0, status TEXT DEFAULT 'running'
);
CREATE TABLE IF NOT EXISTS discovered_channels (
  id INTEGER PRIMARY KEY, hunt_id INTEGER, yt_channel_id TEXT UNIQUE,
  name TEXT, handle TEXT, url TEXT, created_at_yt TEXT, age_days INTEGER,
  subs INTEGER, total_views INTEGER, video_count INTEGER,
  breakout_video_id TEXT, breakout_views INTEGER,
  views_to_subs_ratio REAL,
  faceless_judgment TEXT,
  faceless_confidence REAL, faceless_reason TEXT,
  niche_guess TEXT,
  status TEXT DEFAULT 'new',
  first_seen TEXT
);
CREATE TABLE IF NOT EXISTS discovered_videos (
  id INTEGER PRIMARY KEY, discovered_channel_id INTEGER, yt_video_id TEXT,
  title TEXT, views INTEGER, published_at TEXT, duration_sec INTEGER,
  rank INTEGER
);
CREATE TABLE IF NOT EXISTS channel_age_cache (
  yt_channel_id TEXT PRIMARY KEY, created_at_yt TEXT, checked_at TEXT
);
""")
    conn.commit()
    conn.close()
