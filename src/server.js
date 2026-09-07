import express from 'express';
import http from 'node:http';
import os from 'node:os';
import path from 'node:path';
import crypto from 'node:crypto';
import { fileURLToPath } from 'node:url';
import { WebSocketServer } from 'ws';
import db from './db.js';
import * as player from './player.js';
import * as core from './core.js';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const app = express();
app.use(express.json());
app.use(express.static(path.join(__dirname, '..', 'public')));

const PORT = process.env.PORT || 4680;

// ---------- session (cookie) ----------
const SESSIONS = {}; // token -> {userId, name}
const TOKEN_TTL = 365 * 24 * 3600 * 1000;

function parseCookies(req) {
  return Object.fromEntries((req.headers.cookie || '').split(';').map(s => s.trim()).filter(Boolean).map(s => {
    const i = s.indexOf('='); return [s.slice(0, i), decodeURIComponent(s.slice(i + 1))];
  }));
}
function currentUser(req) {
  const token = parseCookies(req).juke_uid;
  return token ? SESSIONS[token] : null;
}
function auth(req, res, next) {
  const u = currentUser(req);
  if (!u) return res.status(401).json({ error: '请先在设置里输入昵称' });
  req.user = u; next();
}

// ---------- auth routes ----------
app.post('/api/register', (req, res) => {
  const name = String(req.body.name || '').trim();
  const pin = String(req.body.pin || '').trim() || null;
  if (!name || name.length > 20) return res.status(400).json({ error: '昵称需为 1-20 个字符' });
  let row = db.prepare('SELECT * FROM users WHERE name = ?').get(name);
  if (!row) {
    db.prepare('INSERT INTO users (name, pin) VALUES (?, ?)').run(name, pin);
    row = db.prepare('SELECT * FROM users WHERE name = ?').get(name);
  } else if (row.pin && row.pin !== pin) {
    return res.status(403).json({ error: '这个昵称已被使用，需要口令' });
  }
  const token = crypto.randomBytes(24).toString('hex');
  SESSIONS[token] = { userId: row.id, name: row.name };
  res.setHeader('Set-Cookie', `juke_uid=${token}; Path=/; Max-Age=${TOKEN_TTL / 1000}; SameSite=Lax`);
  res.json({ user: { id: row.id, name: row.name } });
});
app.get('/api/me', (req, res) => res.json({ user: currentUser(req) }));
app.post('/api/logout', (req, res) => {
  const token = parseCookies(req).juke_uid;
  if (token) delete SESSIONS[token];
  res.setHeader('Set-Cookie', 'juke_uid=; Path=/; Max-Age=0');
  res.json({ ok: true });
});

// ---------- player ----------
app.post('/api/player/start', auth, async (req, res) => {
  try {
    await player.start();
    res.json({ ok: true, loggedIn: await player.isLoggedIn() });
  } catch (e) { res.status(500).json({ error: e.message }); }
});
app.get('/api/player/status', async (req, res) => {
  res.json({
    running: player.isRunning(),
    loggedIn: player.isRunning() ? await player.isLoggedIn() : false
  });
});
app.post('/api/player/login', auth, async (req, res) => {
  try { await player.openLoginPage(); res.json({ ok: true }); }
  catch (e) { res.status(500).json({ error: e.message }); }
});

// ---------- jukebox ----------
app.get('/api/state', (req, res) => res.json(core.snapshot()));

app.post('/api/queue/add', auth, async (req, res) => {
  const { title, singer, songMid } = req.body;
  if (!title) return res.status(400).json({ error: '缺少歌名' });
  if (!player.isRunning()) return res.status(409).json({ error: '播放器未启动，请先点击“启动播放器”' });
  if (!core.state.allowPlay) return res.status(409).json({ error: '当前不在允许播放时段' });
  const item = core.enqueue({ title, singer: singer || '', songMid }, req.user);
  res.json({ item });
});
app.post('/api/queue/remove', auth, (req, res) => {
  const removed = core.removeAt(Number(req.body.index), req.user);
  res.json({ removed });
});
app.post('/api/queue/reorder', auth, (req, res) => {
  core.reorder(Number(req.body.from), Number(req.body.to));
  res.json(core.snapshot());
});
app.post('/api/control/:action', auth, async (req, res) => {
  try {
    const a = req.params.action;
    if (a === 'next') await core.next(req.user);
    else if (a === 'prev') await core.prev(req.user);
    else if (a === 'pause') await core.togglePause();
    else return res.status(400).json({ error: 'unknown action' });
    res.json(core.snapshot());
  } catch (e) { res.status(500).json({ error: e.message }); }
});

// ---------- volume ----------
app.get('/api/volume', async (req, res) => res.json({ volume: await player.getSystemVolume() }));
app.post('/api/volume', auth, async (req, res) =>
  res.json({ volume: await player.setSystemVolume(Number(req.body.volume)) }));

// ---------- playlists ----------
app.get('/api/playlists', (req, res) => {
  const rows = db.prepare(`
    SELECT p.*, u.name AS creator,
           (SELECT COUNT(*) FROM playlist_songs ps WHERE ps.playlist_id = p.id) AS songCount
    FROM playlists p LEFT JOIN users u ON u.id = p.created_by ORDER BY p.id DESC`).all();
  res.json({ playlists: rows });
});
app.post('/api/playlists', auth, (req, res) => {
  const name = String(req.body.name || '').trim();
  if (!name) return res.status(400).json({ error: '歌单名不能为空' });
  const r = db.prepare('INSERT INTO playlists (name, description, created_by) VALUES (?, ?, ?)')
    .run(name, String(req.body.description || ''), req.user.userId);
  res.json({ id: Number(r.lastInsertRowid) });
});
app.get('/api/playlists/:id', (req, res) => {
  const songs = db.prepare(`
    SELECT ps.*, u.name AS adder FROM playlist_songs ps
    LEFT JOIN users u ON u.id = ps.added_by
    WHERE ps.playlist_id = ? ORDER BY ps.position`).all(req.params.id);
  res.json({ songs });
});
app.post('/api/playlists/:id/songs', auth, (req, res) => {
  const { title, singer, songMid } = req.body;
  if (!title) return res.status(400).json({ error: '缺少歌名' });
  const r = db.prepare(`
    INSERT INTO playlist_songs (playlist_id, position, song_mid, title, singer, added_by)
    VALUES (?, (SELECT COALESCE(MAX(position), 0) + 1 FROM playlist_songs WHERE playlist_id = ?), ?, ?, ?, ?)
  `).run(req.params.id, req.params.id, songMid || '', title, singer || '', req.user.userId);
  res.json({ id: Number(r.lastInsertRowid) });
});
app.post('/api/playlists/:id/reorder', auth, (req, res) => {
  const { from, to } = req.body;
  const songs = db.prepare('SELECT id FROM playlist_songs WHERE playlist_id = ? ORDER BY position').all(req.params.id);
  const [x] = songs.splice(from, 1);
  songs.splice(to, 0, x);
  songs.forEach((s, i) => db.prepare('UPDATE playlist_songs SET position = ? WHERE id = ?').run(i + 1, s.id));
  res.json({ ok: true });
});
app.post('/api/playlists/:id/songs/:songId/delete', auth, (req, res) => {
  db.prepare('DELETE FROM playlist_songs WHERE id = ? AND playlist_id = ?').run(req.params.songId, req.params.id);
  res.json({ ok: true });
});
// push a playlist (whole or one song) into the play queue
app.post('/api/playlists/:id/play', auth, (req, res) => {
  const songs = db.prepare('SELECT * FROM playlist_songs WHERE playlist_id = ? ORDER BY position').all(req.params.id);
  for (const s of songs) core.enqueue(s, req.user);
  res.json(core.snapshot());
});

// ---------- history & stats ----------
app.get('/api/history', (req, res) => {
  const limit = Math.min(200, Number(req.query.limit) || 50);
  const rows = db.prepare(`
    SELECT h.*, ru.name AS requestedName, su.name AS skippedName
    FROM play_history h
    LEFT JOIN users ru ON ru.id = h.requested_by
    LEFT JOIN users su ON su.id = h.skipped_by
    ORDER BY h.started_at DESC LIMIT ?`).all(limit);
  res.json({ history: rows });
});
app.get('/api/stats', (req, res) => {
  const period = req.query.period === 'month' ? "started_at >= datetime('now','localtime','-30 days')"
    : req.query.period === 'week' ? "started_at >= datetime('now','localtime','-7 days')"
    : "started_at >= datetime('now','localtime','-1 day')";
  const byUser = db.prepare(`
    SELECT COALESCE(ru.name, '未知') AS name, COUNT(*) AS plays, SUM(h.played_sec) AS seconds
    FROM play_history h LEFT JOIN users ru ON ru.id = h.requested_by
    WHERE ${period} GROUP BY ru.id ORDER BY plays DESC`).all();
  const byHour = db.prepare(`
    SELECT CAST(strftime('%H', started_at) AS INTEGER) AS hour, COUNT(*) AS plays
    FROM play_history WHERE ${period} GROUP BY hour`).all();
  const topSongs = db.prepare(`
    SELECT title, singer, COUNT(*) AS plays, SUM(played_sec) AS seconds
    FROM play_history WHERE ${period} GROUP BY song_mid ORDER BY plays DESC LIMIT 10`).all();
  const total = db.prepare(`SELECT COUNT(*) AS plays, COALESCE(SUM(played_sec),0) AS seconds FROM play_history WHERE ${period}`).get();
  res.json({ byUser, byHour, topSongs, total });
});

// ---------- play windows ----------
app.get('/api/windows', (req, res) =>
  res.json({ windows: db.prepare('SELECT * FROM play_windows ORDER BY id').all() }));
app.post('/api/windows', auth, (req, res) => {
  const { name, days, startTime, endTime } = req.body;
  if (!name || !days || !startTime || !endTime) return res.status(400).json({ error: '字段不完整' });
  const r = db.prepare('INSERT INTO play_windows (name, days, start_time, end_time) VALUES (?, ?, ?, ?)')
    .run(name, String(days), startTime, endTime);
  core.refreshWindows();
  res.json({ id: Number(r.lastInsertRowid) });
});
app.post('/api/windows/:id/toggle', auth, (req, res) => {
  db.prepare('UPDATE play_windows SET enabled = 1 - enabled WHERE id = ?').run(req.params.id);
  core.refreshWindows();
  res.json({ ok: true });
});
app.post('/api/windows/:id/delete', auth, (req, res) => {
  db.prepare('DELETE FROM play_windows WHERE id = ?').run(req.params.id);
  core.refreshWindows();
  res.json({ ok: true });
});

// ---------- websocket broadcast ----------
const server = http.createServer(app);
const wss = new WebSocketServer({ server });
wss.on('connection', ws => {
  ws.send(JSON.stringify({ event: 'init', state: core.snapshot() }));
});
core.onChange(msg => {
  const s = JSON.stringify(msg);
  for (const ws of wss.clients) if (ws.readyState === 1) ws.send(s);
});

// ---------- boot ----------
setInterval(core.refreshWindows, 30 * 1000);
core.refreshWindows();
core.startPolling();

server.listen(PORT, '0.0.0.0', () => {
  const nets = os.networkInterfaces();
  const lan = Object.values(nets).flat().find(n => n && n.family === 'IPv4' && !n.internal);
  console.log(`\n🎸 Jukebox 已启动`);
  console.log(`   本机:   http://localhost:${PORT}`);
  if (lan) console.log(`   局域网: http://${lan.address}:${PORT}\n`);
});
