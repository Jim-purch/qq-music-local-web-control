const $ = (s) => document.querySelector(s);
const $$ = (s) => [...document.querySelectorAll(s)];

let me = null;
let queue = [];

// ---------- api ----------
async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { 'Content-Type': 'application/json' },
    ...opts,
    body: opts.body ? JSON.stringify(opts.body) : undefined
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `请求失败 (${res.status})`);
  return data;
}

// ---------- identity gate ----------
async function checkMe() {
  const { user } = await api('/api/me');
  me = user;
  if (!user) $('#gate').classList.remove('hidden');
  else $('#whoami').textContent = user.name;
}
$('#gateBtn').onclick = async () => {
  const name = $('#gateName').value.trim();
  if (!name) return $('#gateErr').textContent = '先写下昵称吧';
  try {
    const { user } = await api('/api/register', { method: 'POST', body: { name, pin: $('#gatePin').value.trim() } });
    me = user;
    $('#whoami').textContent = user.name;
    $('#gate').classList.add('hidden');
  } catch (e) { $('#gateErr').textContent = e.message; }
};
$('#gateName').addEventListener('keydown', e => { if (e.key === 'Enter') $('#gateBtn').click(); });
$('#btnLogout').onclick = async () => { await api('/api/logout', { method: 'POST' }); location.reload(); };

// ---------- tabs ----------
$$('.tabs button').forEach(b => b.onclick = () => {
  $$('.tabs button').forEach(x => x.classList.toggle('active', x === b));
  $$('.tab').forEach(t => t.classList.toggle('active', t.id === `tab-${b.dataset.tab}`));
  if (b.dataset.tab === 'stats') loadStats();
  if (b.dataset.tab === 'playlists') loadPlaylists();
  if (b.dataset.tab === 'settings') { loadWindows(); refreshPlayerStatus(); }
});

// ---------- websocket ----------
function connectWS() {
  const ws = new WebSocket(`ws://${location.host}`);
  ws.onmessage = (ev) => {
    const msg = JSON.parse(ev.data);
    if (msg.state) applyState(msg.state);
    if (msg.event === 'play:started' || msg.event === 'play:ended' || msg.event === 'queue:changed') {
      // state already applied via msg.state
    }
  };
  ws.onclose = () => setTimeout(connectWS, 3000);
}

function applyState(s) {
  queue = s.queue || [];
  // now playing card
  const cur = s.current;
  const np = s.nowPlaying;
  const card = $('.np-card');
  $('#npTitle').textContent = cur ? cur.title : (np ? np.title : '队列空空的');
  $('#npSinger').textContent = cur ? cur.singer : (np ? np.singer : '');
  $('#npBy').textContent = cur ? `${cur.requestedBy.name} 点的` : '';
  $('#npState').textContent = !cur ? '待命中'
    : s.paused ? '已暂停'
    : s.allowPlay ? '正在播' : '时段外·暂停中';
  card.classList.toggle('playing', !!cur && !s.paused);
  $('#btnPlay').textContent = s.paused ? '▶' : '❚❚';
  $('#windowNotice').classList.toggle('hidden', s.allowPlay);
  if (!s.allowPlay) $('#windowNotice').textContent = '现在不在允许播放的时段，到点会自动继续。';
  renderQueue();
}

function renderQueue() {
  const ol = $('#queueList');
  if (!queue.length) { ol.innerHTML = '<div class="q-empty">队列空着——去「点歌」页叫一首。</div>'; return; }
  ol.innerHTML = queue.map((s, i) => `
    <li>
      <div class="q-title"><b>${esc(s.title)}</b><span>${esc(s.singer || '')}</span></div>
      <span class="q-adder">${esc(s.addedBy.name)}</span>
      <button data-i="${i}" class="q-up" ${i === 0 ? 'disabled' : ''}>↑</button>
      <button data-i="${i}" class="q-del">✕</button>
    </li>`).join('');
  ol.querySelectorAll('.q-del').forEach(b => b.onclick = async () => {
    await api('/api/queue/remove', { method: 'POST', body: { index: +b.dataset.i } });
  });
  ol.querySelectorAll('.q-up').forEach(b => b.onclick = async () => {
    await api('/api/queue/reorder', { method: 'POST', body: { from: +b.dataset.i, to: +b.dataset.i - 1 } });
  });
}

function esc(s) { return String(s ?? '').replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c])); }

// ---------- controls ----------
$('#btnPlay').onclick = () => api('/api/control/pause', { method: 'POST' }).catch(showErr);
$('#btnNext').onclick = () => api('/api/control/next', { method: 'POST' }).catch(showErr);
$('#btnPrev').onclick = () => api('/api/control/prev', { method: 'POST' }).catch(showErr);

const volSlider = $('#volSlider');
async function loadVol() {
  const { volume } = await api('/api/volume');
  volSlider.value = volume; $('#volNum').textContent = volume;
}
let volTimer = null;
volSlider.oninput = () => { $('#volNum').textContent = volSlider.value; clearTimeout(volTimer); };
volSlider.onchange = () => api('/api/volume', { method: 'POST', body: { volume: +volSlider.value } }).catch(showErr);

// ---------- search / add ----------
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
    $$('.tabs button')[0].click();
  } catch (err) { msg.className = 'msg err'; msg.textContent = err.message; }
};

// ---------- playlists ----------
async function loadPlaylists() {
  const { playlists } = await api('/api/playlists');
  const div = $('#plList');
  div.innerHTML = playlists.length ? '' : '<div class="q-empty">还没有歌单。</div>';
  for (const p of playlists) {
    const item = document.createElement('div');
    item.className = 'pl-item';
    item.innerHTML = `
      <div class="pl-name"><b>${esc(p.name)}</b><span>${p.songCount} 首 · ${p.creator ? esc(p.creator) + ' 建' : ''}</span></div>
      <button class="pl-play">整单播放</button><button class="pl-open">打开</button>`;
    item.querySelector('.pl-open').onclick = () => openPlaylist(p.id, p.name);
    item.querySelector('.pl-play').onclick = async () => {
      try { await api(`/api/playlists/${p.id}/play`, { method: 'POST' }); $$('.tabs button')[0].click(); }
      catch (e) { alert(e.message); }
    };
    div.appendChild(item);
  }
}
$('#plForm').onsubmit = async (e) => {
  e.preventDefault();
  const name = $('#plName').value.trim();
  if (!name) return;
  await api('/api/playlists', { method: 'POST', body: { name } });
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
  $('#plBack').onclick = () => { d.classList.add('hidden'); $('#plList').classList.remove('hidden'); };
  $('#plSongBtn').onclick = async () => {
    const raw = $('#plSongInput').value.trim();
    if (!raw) return;
    const parts = raw.split(/\s+/);
    const body = { title: parts[0], singer: parts.slice(1).join(' ') };
    try { await api(`/api/playlists/${id}/songs`, { method: 'POST', body }); openPlaylist(id, name); }
    catch (e) { alert(e.message); }
  };
  const box = $('#plSongs');
  if (!songs.length) { box.innerHTML = '<div class="q-empty">歌单还空着。</div>'; return; }
  box.innerHTML = songs.map((s, i) => `
    <div class="pl-song">
      <div class="ps-title"><b>${esc(s.title)}</b><span>${esc(s.singer || '')}</span></div>
      <span class="q-adder">${s.adder ? esc(s.adder) : ''}</span>
      <button data-i="${i}" class="ps-play">播</button>
      <button data-i="${i}" class="ps-del">✕</button>
    </div>`).join('');
  box.querySelectorAll('.ps-del').forEach(b => b.onclick = async () => {
    const s = songs[+b.dataset.i];
    await api(`/api/playlists/${id}/songs/${s.id}/delete`, { method: 'POST' });
    openPlaylist(id, name);
  });
  box.querySelectorAll('.ps-play').forEach(b => b.onclick = async () => {
    const s = songs[+b.dataset.i];
    try { await api('/api/queue/add', { method: 'POST', body: s }); $$('.tabs button')[0].click(); }
    catch (e) { alert(e.message); }
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
    api(`/api/stats?period=${statPeriod}`),
    api('/api/history?limit=30')
  ]);
  const fmt = (sec) => sec >= 3600 ? `${Math.round(sec / 3600)} 小时` : `${Math.round(sec / 60)} 分钟`;
  const maxU = Math.max(1, ...s.byUser.map(u => u.plays));
  const hour = new Date().getHours();
  const maxH = Math.max(1, ...s.byHour.map(x => x.plays));
  $('#statsBody').innerHTML = `
    <div class="stat-hero">
      <div><div class="num">${s.total.plays}</div><div class="lbl">播放次数</div></div>
      <div><div class="num">${fmt(s.total.seconds)}</div><div class="lbl">总时长</div></div>
    </div>
    <h2 class="sec-title">谁点的</h2>
    ${s.byUser.map(u => `
      <div class="stat-bar-row">
        <span class="sb-name">${esc(u.name)}</span>
        <div class="sb-track"><div class="sb-fill" style="width:${(u.plays / maxU) * 100}%"></div></div>
        <span class="sb-val">${u.plays} 首 · ${fmt(u.seconds || 0)}</span>
      </div>`).join('') || '<div class="q-empty">还没有记录。</div>'}
    <h2 class="sec-title">几点在听</h2>
    <div class="stat-bar-row"><span class="sb-name">${hour}点</span>
      <div class="sb-track"><div class="sb-fill" style="width:${((s.byHour.find(x => x.hour === hour)?.plays || 0) / maxH) * 100}%"></div></div>
      <span class="sb-val">现在</span></div>
    ${s.byHour.filter(x => x.hour !== hour).sort((a, b) => b.plays - a.plays).slice(0, 5).map(x => `
      <div class="stat-bar-row"><span class="sb-name">${x.hour}点</span>
        <div class="sb-track"><div class="sb-fill" style="width:${(x.plays / maxH) * 100}%"></div></div>
        <span class="sb-val">${x.plays} 次</span></div>`).join('') || ''}
    <h2 class="sec-title">最常放</h2>
    ${s.topSongs.map(x => `
      <div class="hist-row"><span class="h-time">×${x.plays}</span>
        <span class="h-title"><b>${esc(x.title)}</b> · ${esc(x.singer || '')}</span>
        <span class="h-by">${fmt(x.seconds || 0)}</span></div>`).join('') || '<div class="q-empty">还没有记录。</div>'}`;
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
      : '启动后会打开一个 Chrome 窗口登录 QQ 音乐网页版，登录一次即可长期生效。';
  } catch { /* server down */ }
}
$('#btnStartPlayer').onclick = async () => {
  try { await api('/api/player/start', { method: 'POST' }); refreshPlayerStatus(); }
  catch (e) { alert(e.message); }
};
$('#btnOpenLogin').onclick = () => api('/api/player/login', { method: 'POST' }).catch(showErr);

// ---------- settings: windows ----------
async function loadWindows() {
  const { windows } = await api('/api/windows');
  const DAY = { 0: '日', 1: '一', 2: '二', 3: '三', 4: '四', 5: '五', 6: '六' };
  $('#winList').innerHTML = windows.map(w => {
    const days = w.days.split(',').map(d => DAY[d] || d).join('');
    return `
    <div class="win-item ${w.enabled ? '' : 'off'}">
      <div class="wi-info"><b>${esc(w.name)}</b><br><span>周${days} · ${w.start_time}–${w.end_time}</span></div>
      <button data-id="${w.id}" class="w-toggle">${w.enabled ? '停用' : '启用'}</button>
      <button data-id="${w.id}" class="w-del">删除</button>
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
  } catch (err) { alert(err.message); }
};

function showErr(e) { console.error(e); alert(e.message); }

// ---------- boot ----------
(async () => {
  await checkMe();
  connectWS();
  loadVol();
  refreshPlayerStatus();
  setInterval(refreshPlayerStatus, 15000);
  try { applyState((await api('/api/state'))); } catch {}
  if (me) $('#whoamiHint').textContent = `当前身份：${me.name}`;
})();
