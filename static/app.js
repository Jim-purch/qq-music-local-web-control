const $ = (s) => document.querySelector(s);
const $$ = (s) => [...document.querySelectorAll(s)];

let me = null;
let queue = [];

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { 'Content-Type': 'application/json' },
    ...opts,
    body: opts.body ? JSON.stringify(opts.body) : undefined
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || data.error || `请求失败 (${res.status})`);
  return data;
}
function esc(s) { return String(s ?? '').replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c])); }

// ---------- identity gate ----------
async function checkMe() {
  const { user } = await api('/api/me');
  me = user;
  if (!user) $('#gate').classList.remove('hidden');
  else { $('#whoami').textContent = user.name; $('#whoamiHint').textContent = `当前身份：${user.name}`; }
}
$('#gateBtn').onclick = async () => {
  const name = $('#gateName').value.trim();
  if (!name) return $('#gateErr').textContent = '先写下昵称吧';
  try {
    const { user } = await api('/api/register', { method: 'POST', body: { name, pin: $('#gatePin').value.trim() } });
    me = user;
    $('#whoami').textContent = user.name;
    $('#whoamiHint').textContent = `当前身份：${user.name}`;
    $('#gate').classList.add('hidden');
  } catch (e) { $('#gateErr').textContent = e.message; }
};
$('#gateName').addEventListener('keydown', e => { if (e.key === 'Enter') $('#gateBtn').click(); });
$('#btnLogout').onclick = async () => { await api('/api/logout', { method: 'POST' }); location.reload(); };

// ---------- nav ----------
function goto(tabName) {
  $$('.side-nav button').forEach(b => b.classList.toggle('active', b.dataset.tab === tabName));
  $$('.tab').forEach(t => t.classList.toggle('active', t.id === `tab-${tabName}`));
  if (tabName === 'stats') loadStats();
  if (tabName === 'playlists') loadPlaylists();
  if (tabName === 'settings') { loadWindows(); refreshPlayerStatus(); }
}
$$('.side-nav button').forEach(b => b.onclick = () => goto(b.dataset.tab));

// keyboard shortcuts — off by default, per-browser toggle (localStorage)
let kbdEnabled = localStorage.getItem('juke_kbd') === '1';

function refreshKbdUI() {
  const toggle = $('#kbdToggle');
  if (toggle) toggle.checked = kbdEnabled;
  const hint = $('.kbd-hint');
  if (hint) hint.classList.toggle('hidden', !kbdEnabled);
}

function setKbd(on) {
  kbdEnabled = on;
  localStorage.setItem('juke_kbd', on ? '1' : '0');
  refreshKbdUI();
}

document.addEventListener('keydown', e => {
  if (!kbdEnabled || e.target.matches('input, textarea')) return;
  if (e.code === 'Space') { e.preventDefault(); api('/api/control/pause', { method: 'POST' }).catch(showErr); }
  else if (e.key.toLowerCase() === 'n') api('/api/control/next', { method: 'POST' }).catch(showErr);
  else if (e.key.toLowerCase() === 'p') api('/api/control/prev', { method: 'POST' }).catch(showErr);
  else if (e.key === '/') { e.preventDefault(); goto('search'); $('#searchInput').focus(); }
});

// ---------- websocket ----------
function connectWS() {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  const ws = new WebSocket(`${proto}://${location.host}/ws`);
  ws.onmessage = (ev) => {
    const msg = JSON.parse(ev.data);
    if (msg.state) applyState(msg.state);
  };
  ws.onclose = () => setTimeout(connectWS, 3000);
}

function applyState(s) {
  queue = s.queue || [];
  const cur = s.current, np = s.nowPlaying;
  $('#npTitle').textContent = cur ? cur.title : (np ? np.title : '队列空空的');
  $('#npSinger').textContent = cur ? cur.singer : (np ? np.singer : '');
  $('#npBy').textContent = cur ? `${cur.requestedBy.name} 点的` : '';
  $('#npState').textContent = !cur ? '待命中'
    : s.paused ? '已暂停' : s.allowPlay ? '正在播' : '时段外·暂停中';
  document.querySelector('.np-card').classList.toggle('playing', !!cur && !s.paused);
  $('#btnPlay').textContent = s.paused ? '▶' : '❚❚';
  $('#windowNotice').classList.toggle('hidden', s.allowPlay);
  if (!s.allowPlay) $('#windowNotice').textContent = '现在不在允许播放的时段，到点会自动继续。';
  renderQueue();
}

function renderQueue() {
  const ol = $('#queueList');
  if (!queue.length) { ol.innerHTML = '<div class="q-empty">队列空着——按 / 去点歌。</div>'; return; }
  ol.innerHTML = queue.map((s, i) => `
    <li>
      <span class="q-idx">${i + 1}</span>
      <div class="q-title"><b>${esc(s.title)}</b><span>${esc(s.singer || '')}</span></div>
      <span class="q-adder" title="${esc(s.addedBy.name)}">${esc(s.addedBy.name)}</span>
      <button class="mini-btn q-up" data-i="${i}" ${i === 0 ? 'disabled' : ''}>↑</button>
      <button class="mini-btn q-del" data-i="${i}">✕</button>
    </li>`).join('');
  ol.querySelectorAll('.q-del').forEach(b => b.onclick = async () => {
    await api(`/api/queue/remove?index=${b.dataset.i}`, { method: 'POST' }).catch(showErr);
  });
  ol.querySelectorAll('.q-up').forEach(b => b.onclick = async () => {
    const i = +b.dataset.i;
    await api(`/api/queue/reorder?start=${i}&stop=${i - 1}`, { method: 'POST' }).catch(showErr);
  });
}

// ---------- controls & volume ----------
$('#btnPlay').onclick = () => api('/api/control/pause', { method: 'POST' }).catch(showErr);
$('#btnNext').onclick = () => api('/api/control/next', { method: 'POST' }).catch(showErr);
$('#btnPrev').onclick = () => api('/api/control/prev', { method: 'POST' }).catch(showErr);

const volSlider = $('#volSlider');
async function loadVol() {
  try {
    const { volume } = await api('/api/volume');
    if (volume >= 0) { volSlider.value = volume; $('#volNum').textContent = volume; }
  } catch {}
}
volSlider.oninput = () => { $('#volNum').textContent = volSlider.value; };
volSlider.onchange = () => api('/api/volume', { method: 'POST', body: { volume: +volSlider.value } }).catch(showErr);

// ---------- search ----------
$('#searchForm').onsubmit = async (e) => {
  e.preventDefault();
  const kw = $('#searchInput').value.trim();
  if (!kw) return;
  const msg = $('#searchMsg');
  msg.className = 'msg'; msg.textContent = '已加入队列，正在播放…';
  try {
    await api('/api/queue/add', { method: 'POST', body: { title: kw } });
    msg.className = 'msg ok'; msg.textContent = `「${kw}」已入队`;
    $('#searchInput').value = '';
    goto('now');
  } catch (err) { msg.className = 'msg err'; msg.textContent = err.message; }
};

// ---------- playlists ----------
async function loadPlaylists() {
  const { playlists } = await api('/api/playlists');
  const div = $('#plList');
  div.classList.remove('hidden');
  $('#plDetail').classList.add('hidden');
  div.innerHTML = playlists.length ? '' : '<div class="q-empty">还没有歌单。</div>';
  for (const p of playlists) {
    const item = document.createElement('div');
    item.className = 'pl-item';
    item.innerHTML = `
      <div class="pl-name"><b>${esc(p.name)}</b><span>${p.songCount} 首${p.creator ? ' · ' + esc(p.creator) + ' 建' : ''}</span></div>
      <button class="mini-btn pl-play">整单播放</button>
      <button class="mini-btn pl-open">打开</button>`;
    item.querySelector('.pl-open').onclick = () => openPlaylist(p.id, p.name);
    item.querySelector('.pl-play').onclick = async () => {
      try { await api(`/api/playlists/${p.id}/play`, { method: 'POST' }); goto('now'); }
      catch (e) { showErr(e); }
    };
    div.appendChild(item);
  }
}
$('#plForm').onsubmit = async (e) => {
  e.preventDefault();
  const name = $('#plName').value.trim();
  if (!name) return;
  await api('/api/playlists', { method: 'POST', body: { name } }).catch(showErr);
  $('#plName').value = '';
  loadPlaylists();
};

async function openPlaylist(id, name) {
  const { songs } = await api(`/api/playlists/${id}`);
  $('#plList').classList.add('hidden');
  const d = $('#plDetail');
  d.classList.remove('hidden');
  d.innerHTML = `
    <button class="back-link" id="plBack">← 返回歌单列表</button>
    <h2 class="sec-title">${esc(name)}</h2>
    <div class="pl-add">
      <input id="plSongInput" placeholder="加一首：歌名 + 空格 + 歌手（可选）">
      <button class="btn-primary" id="plSongBtn">加入</button>
    </div>
    <div id="plSongs"></div>`;
  $('#plBack').onclick = loadPlaylists;
  $('#plSongBtn').onclick = async () => {
    const raw = $('#plSongInput').value.trim();
    if (!raw) return;
    const parts = raw.split(/\s+/);
    try {
      await api(`/api/playlists/${id}/songs`, { method: 'POST', body: { title: parts[0], singer: parts.slice(1).join(' ') } });
      openPlaylist(id, name);
    } catch (e) { showErr(e); }
  };
  const box = $('#plSongs');
  if (!songs.length) { box.innerHTML = '<div class="q-empty">歌单还空着。</div>'; return; }
  box.innerHTML = songs.map((s, i) => `
    <div class="pl-song">
      <span class="ps-idx">${i + 1}</span>
      <div class="ps-title"><b>${esc(s.title)}</b><span>${esc(s.singer || '')}</span></div>
      <span class="q-adder">${s.adder ? esc(s.adder) : ''}</span>
      <button class="mini-btn ps-up" data-i="${i}" ${i === 0 ? 'disabled' : ''}>↑</button>
      <button class="mini-btn ps-play" data-i="${i}">播</button>
      <button class="mini-btn ps-del" data-i="${i}">✕</button>
    </div>`).join('');
  box.querySelectorAll('.ps-del').forEach(b => b.onclick = async () => {
    const s = songs[+b.dataset.i];
    await api(`/api/playlists/${id}/songs/${s.id}/delete`, { method: 'POST' }).catch(showErr);
    openPlaylist(id, name);
  });
  box.querySelectorAll('.ps-play').forEach(b => b.onclick = async () => {
    const s = songs[+b.dataset.i];
    try { await api('/api/queue/add', { method: 'POST', body: { title: s.title, singer: s.singer, songMid: s.song_mid } }); goto('now'); }
    catch (e) { showErr(e); }
  });
  box.querySelectorAll('.ps-up').forEach(b => b.onclick = async () => {
    const i = +b.dataset.i;
    await api(`/api/playlists/${id}/reorder?start=${i}&stop=${i - 1}`, { method: 'POST' }).catch(showErr);
    openPlaylist(id, name);
  });
}

// ---------- stats ----------
let statPeriod = 'day';
$$('.stat-period .chip').forEach(b => b.onclick = () => {
  $$('.stat-period .chip').forEach(x => x.classList.toggle('active', x === b));
  statPeriod = b.dataset.p; loadStats();
});
async function loadStats() {
  const [s, h] = await Promise.all([
    api(`/api/stats?period=${statPeriod}`), api('/api/history?limit=30')
  ]).catch(() => [null, null]);
  if (!s) return;
  const fmt = (sec) => sec >= 3600 ? `${Math.round(sec / 3600)} 小时` : `${Math.round((sec || 0) / 60)} 分钟`;
  const hour = new Date().getHours();
  const maxU = Math.max(1, ...s.byUser.map(u => u.plays));
  const maxH = Math.max(1, ...s.byHour.map(x => x.plays));
  $('#statsBody').innerHTML = `
    <div class="stat-hero">
      <div><div class="num">${s.total.plays}</div><div class="lbl">播放次数</div></div>
      <div><div class="num">${fmt(s.total.seconds)}</div><div class="lbl">总时长</div></div>
    </div>
    <h2 class="sec-title">谁点的</h2>
    ${s.byUser.map(u => `
      <div class="stat-bar-row"><span class="sb-name">${esc(u.name)}</span>
        <div class="sb-track"><div class="sb-fill" style="width:${(u.plays / maxU) * 100}%"></div></div>
        <span class="sb-val">${u.plays} 首 · ${fmt(u.seconds)}</span></div>`).join('') || '<div class="q-empty">还没有记录。</div>'}
    <h2 class="sec-title">几点在听</h2>
    ${s.byHour.slice().sort((a, b) => b.plays - a.plays).slice(0, 8).map(x => `
      <div class="stat-bar-row"><span class="sb-name">${x.hour}点${x.hour === hour ? '（现在）' : ''}</span>
        <div class="sb-track"><div class="sb-fill" style="width:${(x.plays / maxH) * 100}%"></div></div>
        <span class="sb-val">${x.plays} 次</span></div>`).join('') || '<div class="q-empty">还没有记录。</div>'}
    <h2 class="sec-title">最常放</h2>
    ${s.topSongs.map(x => `
      <div class="hist-row"><span class="h-time">×${x.plays}</span>
        <span class="h-title"><b>${esc(x.title)}</b> · ${esc(x.singer || '')}</span>
        <span class="h-by">${fmt(x.seconds)}</span></div>`).join('') || '<div class="q-empty">还没有记录。</div>'}`;
  $('#historyList').innerHTML = h.history.map(x => `
    <div class="hist-row">
      <span class="h-time">${x.started_at.slice(5, 16)}</span>
      <span class="h-title"><b>${esc(x.title)}</b> · ${esc(x.singer || '')}</span>
      <span class="h-by">${x.requestedName ? esc(x.requestedName) : ''}</span>
      ${x.skipped_by ? '<span class="h-cut">被切</span>' : ''}
    </div>`).join('') || '<div class="q-empty">还没有记录。</div>';
}

// ---------- settings: player ----------
async function refreshPlayerStatus() {
  try {
    const st = await api('/api/player/status');
    const chip = $('#playerStatus');
    chip.textContent = st.running ? (st.loggedIn ? '播放器在线' : '未登录') : '未连接';
    chip.classList.toggle('on', st.running && st.loggedIn);
    $('#btnOpenLogin').classList.toggle('hidden', !st.running || st.loggedIn);
    $('#playerHint').textContent = st.running
      ? (st.loggedIn ? '播放器运行中，登录态正常。' : '播放器已启动但未登录，点下方按钮扫码。')
      : '启动后会打开一个 Chrome/Edge 窗口登录 QQ 音乐网页版，登录一次即可长期生效。';
  } catch {}
}
const kbdToggleEl = $('#kbdToggle');
if (kbdToggleEl) kbdToggleEl.onchange = () => setKbd(kbdToggleEl.checked);

$('#btnStartPlayer').onclick = async () => {
  try { await api('/api/player/start', { method: 'POST' }); refreshPlayerStatus(); }
  catch (e) { showErr(e); }
};
$('#btnOpenLogin').onclick = () => api('/api/player/login', { method: 'POST' }).catch(showErr);

// ---------- settings: windows ----------
async function loadWindows() {
  const { windows } = await api('/api/windows');
  const DAY = { 0: '日', 1: '一', 2: '二', 3: '三', 4: '四', 5: '五', 6: '六' };
  $('#winList').innerHTML = windows.map(w => {
    const days = w.days.split(',').map(d => DAY[d.trim()] || d).join('');
    return `
    <div class="win-item ${w.enabled ? '' : 'off'}">
      <div class="wi-info"><b>${esc(w.name)}</b><br><span>周${days} · ${w.start_time}–${w.end_time}</span></div>
      <button class="mini-btn w-toggle" data-id="${w.id}">${w.enabled ? '停用' : '启用'}</button>
      <button class="mini-btn w-del" data-id="${w.id}">删除</button>
    </div>`;
  }).join('') || '<div class="q-empty">暂无时段限制（全天可播）。</div>';
  $$('#winList .w-toggle').forEach(b => b.onclick = async () => {
    await api(`/api/windows/${b.dataset.id}/toggle`, { method: 'POST' }); loadWindows();
  });
  $$('#winList .w-del').forEach(b => b.onclick = async () => {
    await api(`/api/windows/${b.dataset.id}/delete`, { method: 'POST' }); loadWindows();
  });
}
$('#winForm').onsubmit = async (e) => {
  e.preventDefault();
  const days = $$('input[name=wd]:checked').map(x => x.value).join(',');
  if (!days) return alert('至少选一天');
  try {
    await api('/api/windows', { method: 'POST', body: {
      name: $('#winName').value.trim() || '时段',
      days, startTime: $('#winStart').value, endTime: $('#winEnd').value
    }});
    $('#winName').value = ''; loadWindows();
  } catch (err) { showErr(err); }
};

function showErr(e) { console.error(e); alert(e.message || e); }

// ---------- boot ----------
(async () => {
  await checkMe();
  refreshKbdUI();
  connectWS();
  loadVol();
  refreshPlayerStatus();
  setInterval(refreshPlayerStatus, 15000);
  try { applyState(await api('/api/state')); } catch {}
})();
