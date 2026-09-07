// Core jukebox logic: shared queue, play loop, history, play windows.
import db from './db.js';
import * as player from './player.js';

const state = {
  queue: [],            // {songMid, title, singer, addedBy: {id,name}}
  playing: false,
  current: null,        // {songMid,title,singer,requestedBy,startedAt,historyId}
  paused: false,
  allowPlay: true,      // gated by play windows
  nowPlayingInfo: null  // polled from player bar
};

let listeners = [];
export function onChange(fn) { listeners.push(fn); }
function emit(event, data) { for (const fn of listeners) fn({ event, data, state: snapshot() }); }

export function snapshot() {
  return {
    queue: state.queue,
    current: state.current,
    paused: state.paused,
    allowPlay: state.allowPlay,
    nowPlaying: state.nowPlayingInfo
  };
}

// ---------- queue ----------
export function enqueue(song, user) {
  const item = { songMid: song.songMid || null, title: song.title, singer: song.singer, addedBy: { id: user.id, name: user.name } };
  state.queue.push(item);
  emit('queue:changed', { item });
  kick();
  return item;
}

export function removeAt(i, user) {
  const [removed] = state.queue.splice(i, 1);
  emit('queue:changed', { removed, by: user.name });
  return removed;
}

export function reorder(from, to) {
  if (from < 0 || from >= state.queue.length) return;
  const [x] = state.queue.splice(from, 1);
  state.queue.splice(Math.max(0, Math.min(state.queue.length, to)), 0, x);
  emit('queue:changed', {});
}

// ---------- play loop ----------
let kicking = false;
function kick() { if (!kicking) { kicking = true; setImmediate(loop); } }

async function loop() {
  kicking = false;
  try {
    if (!state.current && state.queue.length && state.allowPlay) {
      const item = state.queue.shift();
      await startPlay(item, item.addedBy);
    }
  } catch (e) {
    console.error('[loop]', e.message);
  }
}

async function startPlay(item, requestedBy) {
  const info = await player.searchAndPlay(`${item.title} ${item.singer}`.trim());
  const now = new Date();
  const startedAt = now.toISOString();
  const res = db.prepare(
    `INSERT INTO play_history (song_mid, title, singer, started_at, requested_by)
     VALUES (?, ?, ?, ?, ?)`
  ).run(item.songMid || '', item.title, item.singer, startedAt, requestedBy.id);
  state.current = { ...item, requestedBy, startedAt, historyId: Number(res.lastInsertRowid) };
  state.playing = true;
  state.paused = false;
  emit('play:started', state.current);
}

async function finishCurrent({ skippedBy = null } = {}) {
  if (!state.current) return;
  const c = state.current;
  const playedSec = Math.round((Date.now() - new Date(c.startedAt).getTime()) / 1000);
  db.prepare(
    `UPDATE play_history SET ended_at = ?, played_sec = ?, completed = ?, skipped_by = ?
     WHERE id = ?`
  ).run(new Date().toISOString(), playedSec, skippedBy ? 0 : 1, skippedBy, c.historyId);
  state.current = null;
  state.playing = false;
  emit('play:ended', { ...c, skippedBy });
  kick();
}

// ---------- controls ----------
export async function next(user) {
  if (!state.current && !state.queue.length) return;
  if (state.current) await finishCurrent({ skippedBy: user ? { id: user.id, name: user.name } : null });
  else kick();
  if (state.current === null && state.queue.length) await loop();
}

export async function togglePause() {
  await player.control('playPause');
  state.paused = !state.paused;
  emit('play:paused', { paused: state.paused });
}

export async function prev(user) { await player.control('prev'); emit('play:prev', { by: user?.name }); }

// ---------- now-playing polling (also detects natural song end) ----------
let lastSeenKey = '';
export function startPolling() {
  setInterval(async () => {
    try {
      const np = await player.nowPlaying();
      state.nowPlayingInfo = np;
      if (!np) return;
      const key = np.title;
      if (state.current && key && key !== state.current.title && lastSeenKey === state.current.title) {
        // player bar moved to a new song by itself → natural end
        await finishCurrent({});
      }
      lastSeenKey = key;
      emit('nowplaying', np);
    } catch { /* page busy */ }
  }, 3000);
}

// ---------- play windows ----------
export function refreshWindows() {
  const now = new Date();
  const day = String(now.getDay());
  const hm = now.getHours() * 60 + now.getMinutes();
  const rows = db.prepare('SELECT * FROM play_windows WHERE enabled = 1').all();
  const inAny = rows.some(w => {
    if (!w.days.split(',').includes(day)) return false;
    const [sh, sm] = w.start_time.split(':').map(Number);
    const [eh, em] = w.end_time.split(':').map(Number);
    const s = sh * 60 + sm, e = eh * 60 + em;
    return s <= e ? (hm >= s && hm < e) : (hm >= s || hm < e); // overnight window
  });
  const changed = inAny !== state.allowPlay;
  state.allowPlay = inAny;
  if (changed) {
    emit('window:changed', { allowPlay: inAny });
    if (inAny) kick();
    else if (state.current && !state.paused) togglePause().catch(() => {});
  }
}

export { state };
