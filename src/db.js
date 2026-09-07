import { DatabaseSync } from 'node:sqlite';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const dataDir = path.join(__dirname, '..', 'data');
fs.mkdirSync(dataDir, { recursive: true });
const db = new DatabaseSync(path.join(dataDir, 'jukebox.db'));

db.exec(`
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT UNIQUE NOT NULL,
  pin TEXT,                -- optional pin to reclaim name on another device
  created_at TEXT DEFAULT (datetime('now', 'localtime'))
);

CREATE TABLE IF NOT EXISTS playlists (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  description TEXT DEFAULT '',
  created_by INTEGER REFERENCES users(id),
  created_at TEXT DEFAULT (datetime('now', 'localtime'))
);

CREATE TABLE IF NOT EXISTS playlist_songs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  playlist_id INTEGER REFERENCES playlists(id),
  position INTEGER NOT NULL,            -- manual ordering
  song_mid TEXT NOT NULL,
  title TEXT NOT NULL,
  singer TEXT NOT NULL,
  duration_sec INTEGER,
  added_by INTEGER REFERENCES users(id),
  added_at TEXT DEFAULT (datetime('now', 'localtime'))
);

CREATE TABLE IF NOT EXISTS play_history (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  song_mid TEXT,
  title TEXT NOT NULL,
  singer TEXT NOT NULL,
  started_at TEXT NOT NULL,
  ended_at TEXT,
  played_sec INTEGER DEFAULT 0,        -- seconds actually played
  completed INTEGER DEFAULT 0,         -- 1 if played to the end
  requested_by INTEGER REFERENCES users(id),  -- who added/triggered this play
  skipped_by INTEGER REFERENCES users(id)     -- who cut it short, if anyone
);

CREATE TABLE IF NOT EXISTS play_windows (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  days TEXT NOT NULL,                  -- e.g. "1,2,3,4,5" (Mon-Fri), 0=Sun
  start_time TEXT NOT NULL,            -- "18:00"
  end_time TEXT NOT NULL,              -- "23:00"
  enabled INTEGER DEFAULT 1
);

CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value TEXT
);
`);

export default db;
