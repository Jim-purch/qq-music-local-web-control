"""SQLite schema + connection. WAL mode, all data lives in ./data/."""
import sqlite3
import json
import os
import threading

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
os.makedirs(DATA_DIR, exist_ok=True)
DB_PATH = os.path.join(DATA_DIR, "jukebox.db")

_local = threading.local()


def get_db() -> sqlite3.Connection:
    """One connection per thread (uvicorn threadpool for sync routes)."""
    conn = getattr(_local, "conn", None)
    if conn is None:
        conn = sqlite3.connect(DB_PATH, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        _local.conn = conn
    return conn


def init_db():
    conn = get_db()
    conn.executescript(
        """
    CREATE TABLE IF NOT EXISTS users (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      name TEXT UNIQUE NOT NULL,
      pin TEXT,
      created_at TEXT DEFAULT (datetime('now','localtime'))
    );
    CREATE TABLE IF NOT EXISTS playlists (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      name TEXT NOT NULL,
      description TEXT DEFAULT '',
      created_by INTEGER REFERENCES users(id),
      created_at TEXT DEFAULT (datetime('now','localtime'))
    );
    CREATE TABLE IF NOT EXISTS playlist_songs (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      playlist_id INTEGER REFERENCES playlists(id),
      position INTEGER NOT NULL,
      song_mid TEXT DEFAULT '',
      title TEXT NOT NULL,
      singer TEXT DEFAULT '',
      duration_sec INTEGER,
      added_by INTEGER REFERENCES users(id),
      added_at TEXT DEFAULT (datetime('now','localtime'))
    );
    CREATE TABLE IF NOT EXISTS play_history (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      song_mid TEXT DEFAULT '',
      title TEXT NOT NULL,
      singer TEXT DEFAULT '',
      started_at TEXT NOT NULL,
      ended_at TEXT,
      played_sec INTEGER DEFAULT 0,
      completed INTEGER DEFAULT 0,
      requested_by INTEGER REFERENCES users(id),
      skipped_by INTEGER REFERENCES users(id)
    );
    CREATE TABLE IF NOT EXISTS play_windows (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      name TEXT NOT NULL,
      days TEXT NOT NULL,
      start_time TEXT NOT NULL,
      end_time TEXT NOT NULL,
      enabled INTEGER DEFAULT 1
    );
    """
    )
    conn.commit()


def rows_to_dicts(rows):
    return [dict(r) for r in rows]


def to_json(value, default=None):
    try:
        return json.loads(value) if value else default
    except (json.JSONDecodeError, TypeError):
        return default
