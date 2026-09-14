const $ = (s) => document.querySelector(s);
const $$ = (s) => [...document.querySelectorAll(s)];

let me = null;
let queue = [];
let searchCachedSongs = [];

const AUTH_TOKEN_KEY = 'juke_token';
const AUTH_USER_KEY = 'juke_user';
const SEARCH_STATE_KEY = 'juke_search_state';

async function api(path, opts = {}) {
  const token = localStorage.getItem(AUTH_TOKEN_KEY);
  const headers = { 'Content-Type': 'application/json', ...(opts.headers || {}) };
  if (token && !headers['Authorization']) {
    headers['Authorization'] = `Bearer ${token}`;
  }
  const res = await fetch(path, {
    ...opts,
    headers,
    body: opts.body ? JSON.stringify(opts.body) : undefined
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || data.error || `请求失败 (${res.status})`);
  return data;
}
function esc(s) { return String(s ?? '').replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c])); }

// ---------- identity gate ----------
async function checkMe() {
  try {
    const { user } = await api('/api/me');
    me = user;
    if (user) {
      localStorage.setItem(AUTH_USER_KEY, JSON.stringify(user));
      $('#whoami').textContent = user.name;
      $('#whoamiHint').textContent = `当前身份：${user.name}`;
      $('#gate').classList.add('hidden');
    } else {
      localStorage.removeItem(AUTH_TOKEN_KEY);
      localStorage.removeItem(AUTH_USER_KEY);
      $('#gate').classList.remove('hidden');
    }
  } catch (err) {
    console.error('checkMe failed', err);
    $('#gate').classList.remove('hidden');
  }
}
$('#gateBtn').onclick = async () => {
  const name = $('#gateName').value.trim();
  if (!name) return $('#gateErr').textContent = '先写下昵称吧';
  try {
    const res = await api('/api/register', { method: 'POST', body: { name, pin: $('#gatePin').value.trim() } });
    me = res.user;
    if (res.token) localStorage.setItem(AUTH_TOKEN_KEY, res.token);
    if (res.user) localStorage.setItem(AUTH_USER_KEY, JSON.stringify(res.user));
    $('#whoami').textContent = me.name;
    $('#whoamiHint').textContent = `当前身份：${me.name}`;
    $('#gate').classList.add('hidden');
  } catch (e) { $('#gateErr').textContent = e.message; }
};
$('#gateName').addEventListener('keydown', e => { if (e.key === 'Enter') $('#gateBtn').click(); });
$('#btnLogout').onclick = async () => {
  try { await api('/api/logout', { method: 'POST' }); } catch {}
  localStorage.removeItem(AUTH_TOKEN_KEY);
  localStorage.removeItem(AUTH_USER_KEY);
  location.reload();
};

// ---------- nav ----------
function goto(tabName) {
  $$('.side-nav button').forEach(b => b.classList.toggle('active', b.dataset.tab === tabName));
  $$('.tab').forEach(t => t.classList.toggle('active', t.id === `tab-${tabName}`));
  if (tabName === 'search') restoreSearchState();
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
  else if (e.key.toLowerCase() === 'n') triggerNextSong();
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
    if (msg.event === 'nowplaying' && msg.data) onNowPlayingMsg(msg.data);
    if (msg.event === 'play:switching') {
      showSwitching(msg.data || {});
    }
    if (msg.event === 'play:started') {
      hideSwitching();
    }
    // 点歌失败广播（版权/下架/客户端卡住，服务端已自动跳下一首）
    if (msg.event === 'play:error') {
      hideSwitching();
      if (msg.data?.item) {
        const it = msg.data.item;
        const by = it.addedBy ? `（${it.addedBy.name} 点的）` : '';
        showErr(new Error(`「${it.title}」没能播放，已自动跳过${by}`));
      }
    }
  };
  ws.onclose = () => setTimeout(connectWS, 3000);
}

// ---------- now playing progress（本地秒级走表 + 后端校准） ----------
let npTick = { pos: 0, dur: 0, playing: false, at: 0 };

function fmtTime(sec) {
  sec = Math.max(0, Math.floor(sec || 0));
  const m = Math.floor(sec / 60), s = sec % 60;
  return `${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`;
}

function syncNpTick(np) {
  npTick = {
    pos: np?.positionSec || 0,
    dur: np?.durationSec || 0,
    playing: !!np?.playing,
    at: Date.now()
  };
}

function paintNpProgress() {
  let pos = npTick.pos + (npTick.playing ? (Date.now() - npTick.at) / 1000 : 0);
  if (npTick.dur > 0) pos = Math.min(pos, npTick.dur);
  const pct = npTick.dur > 0 ? (pos / npTick.dur) * 100 : 0;
  const fill = $('#npProgressFill');
  if (fill) fill.style.width = pct.toFixed(1) + '%';
  const cur = $('#npCurTime'); const total = $('#npTotalTime');
  if (cur) cur.textContent = fmtTime(pos);
  if (total) total.textContent = fmtTime(npTick.dur);
  // 队列顶栏的迷你进度
  const qTime = $('#qNpTime');
  if (qTime) qTime.textContent = `${fmtTime(pos)} / ${fmtTime(npTick.dur)}`;
}

setInterval(() => {
  if (npTick.dur > 0 && npTick.playing) paintNpProgress();
}, 1000);

function applyState(s) {
  queue = s.queue || [];
  const cur = s.current, np = s.nowPlaying;
  const shown = cur || np;

  // 如果后端明确处于 switching 状态，触发切歌态展示
  if (s.switching) {
    showSwitching(s.switching);
  } else if (isSwitching && cur) {
    // 新歌已经成功起播，解除切歌态
    hideSwitching();
  }

  if (!isSwitching) {
    $('#npTitle').textContent = shown ? shown.title : '队列空空的';
    $('#npSinger').textContent = shown ? [shown.singer, shown.album].filter(Boolean).join(' · ') : '';
    $('#npBy').textContent = cur ? `${cur.requestedBy.name} 点的` : '';
    $('#npState').textContent = !cur ? '待命中'
      : s.paused ? '已暂停' : s.allowPlay ? '正在播' : '时段外·暂停中';
    document.querySelector('.np-card').classList.toggle('playing', !!cur && !s.paused);
  }

  $('#btnPlay').textContent = s.paused ? '▶' : '❚❚';
  $('#windowNotice').classList.toggle('hidden', s.allowPlay);
  if (!s.allowPlay) $('#windowNotice').textContent = '现在不在允许播放的时段，到点会自动继续。';
  // 进度与时长元数据（来自 SMTC / 网页 audio）
  syncNpTick(np);
  paintNpProgress();
  // 队列顶栏正在播放卡片
  updateQueueNpCard(cur, np, s);
  renderQueue();
}

function updateQueueNpCard(cur, np, s) {
  const card = $('#queueNowPlaying');
  if (!card) return;
  if (isSwitching) {
    const nextSongTitle = (s && s.switching?.title) || (queue[0] ? queue[0].title : '');
    $('#qNpTitle').textContent = nextSongTitle ? `⟳ 准备播放：${nextSongTitle}` : '⟳ 正在切歌…';
    $('#qNpSinger').textContent = '客户端正在调起播放';
    return;
  }
  const shown = cur || np;
  $('#qNpTitle').textContent = shown ? shown.title : '队列空空的';
  $('#qNpSinger').textContent = shown
    ? [shown.singer || (shown.requestedBy ? `${shown.requestedBy.name} 点的` : ''), shown.album].filter(Boolean).join(' · ')
    : '';
}

// websocket 推送的 nowplaying 事件也用于校准进度
function onNowPlayingMsg(np) {
  if (np) { syncNpTick(np); paintNpProgress(); }
}

function renderQueue() {
  const ol = $('#queueList');
  if (!queue.length) { ol.innerHTML = '<div class="q-empty">队列空着——按 / 去点歌。</div>'; return; }
  ol.innerHTML = queue.map((s, i) => {
    const isTarget = isSwitching && i === 0;
    return `
    <li draggable="true" data-i="${i}" class="${isTarget ? 'is-switching' : ''}">
      <span class="q-idx">${isTarget ? '⟳' : i + 1}</span>
      <div class="q-title"><b>${esc(s.title)}</b><span>${esc(s.singer || '')}${isTarget ? ' <em class="switching-hint">正在切入…</em>' : ''}</span></div>
      <span class="q-adder" title="${esc(s.addedBy.name)}">${esc(s.addedBy.name)}</span>
      <span class="queue-btns">
        <button class="mini-btn q-up" data-i="${i}" ${i === 0 ? 'disabled' : ''} title="置顶（下一首播放）">↑</button>
        <button class="mini-btn q-toPl" data-i="${i}" title="加入歌单">+歌单</button>
        <button class="mini-btn q-del" data-i="${i}" title="移出队列">✕</button>
      </span>
    </li>`;
  }).join('');
  ol.querySelectorAll('.q-del').forEach(b => b.onclick = async (e) => {
    e.stopPropagation();
    await api(`/api/queue/remove?index=${b.dataset.i}`, { method: 'POST' }).catch(showErr);
  });
  ol.querySelectorAll('.q-up').forEach(b => b.onclick = async (e) => {
    e.stopPropagation();
    // ↑ = 置顶：移到队首，成为下一首播放
    const i = +b.dataset.i;
    await api(`/api/queue/reorder?start=${i}&stop=0`, { method: 'POST' }).catch(showErr);
  });
  ol.querySelectorAll('.q-toPl').forEach(b => b.onclick = (e) => {
    e.stopPropagation();
    const s = queue[+b.dataset.i];
    const rect = b.getBoundingClientRect();
    showPlaylistPicker(rect.left, rect.bottom + 4, s);
  });
  attachQueueDrag(ol);
}

// ---------- queue drag & drop 排序 ----------
function attachQueueDrag(ol) {
  let dragIdx = -1;
  ol.querySelectorAll('li').forEach(li => {
    li.addEventListener('dragstart', (e) => {
      dragIdx = +li.dataset.i;
      li.classList.add('dragging');
      e.dataTransfer.effectAllowed = 'move';
      try { e.dataTransfer.setData('text/plain', `queue:${dragIdx}`); } catch {}
    });
    li.addEventListener('dragend', () => {
      li.classList.remove('dragging');
      ol.querySelectorAll('li').forEach(x => x.classList.remove('drag-over-top', 'drag-over-bottom'));
    });
    li.addEventListener('dragover', (e) => {
      if (dragIdx < 0) return;
      e.preventDefault();
      e.dataTransfer.dropEffect = 'move';
      const rect = li.getBoundingClientRect();
      const before = e.clientY < rect.top + rect.height / 2;
      li.classList.toggle('drag-over-top', before);
      li.classList.toggle('drag-over-bottom', !before);
    });
    li.addEventListener('dragleave', () => li.classList.remove('drag-over-top', 'drag-over-bottom'));
    li.addEventListener('drop', async (e) => {
      e.preventDefault();
      if (dragIdx < 0 || dragIdx === +li.dataset.i) return;
      const rect = li.getBoundingClientRect();
      let stop = +li.dataset.i;
      if (e.clientY >= rect.top + rect.height / 2) stop += 1;
      // 从小索引往大拖时，pop 之后索引偏移 -1
      if (dragIdx < stop) stop -= 1;
      await api(`/api/queue/reorder?start=${dragIdx}&stop=${stop}`, { method: 'POST' }).catch(showErr);
      dragIdx = -1;
    });
  });
}

// ---------- 歌单选择浮层（把歌曲收藏进某个歌单） ----------
let plPickerEl = null;
async function showPlaylistPicker(x, y, song) {
  closePlaylistPicker();
  const pop = document.createElement('div');
  pop.className = 'pl-picker-pop';
  pop.innerHTML = '<h3>加入哪个歌单？</h3><div class="pl-pop-empty">加载中…</div>';
  document.body.appendChild(pop);
  plPickerEl = pop;
  // 边界防溢出
  const pw = 240, ph = 300;
  pop.style.left = Math.min(x, window.innerWidth - pw - 12) + 'px';
  pop.style.top = Math.min(y, window.innerHeight - ph - 12) + 'px';
  pop.addEventListener('click', (e) => e.stopPropagation());
  setTimeout(() => document.addEventListener('click', closePlaylistPicker, { once: true }), 0);
  try {
    const { playlists } = await api('/api/playlists');
    pop.innerHTML = '<h3>加入哪个歌单？</h3>' + (playlists.length
      ? playlists.map(p => `<button class="pl-pop-item" data-id="${p.id}">${esc(p.name)}（${p.songCount}）</button>`).join('')
      : '<div class="pl-pop-empty">还没有歌单，先去「歌单」页建一个。</div>');
    pop.querySelectorAll('.pl-pop-item').forEach(btn => btn.onclick = async (e) => {
      e.stopPropagation();
      try {
        await api(`/api/playlists/${btn.dataset.id}/songs`, {
          method: 'POST',
          body: { title: song.title, singer: song.singer || '', songMid: song.songMid || '' }
        });
        closePlaylistPicker();
      } catch (err) { showErr(err); }
    });
  } catch (e) {
    pop.innerHTML = `<div class="pl-pop-empty">${esc(e.message)}</div>`;
  }
}
function closePlaylistPicker() {
  if (plPickerEl) { plPickerEl.remove(); plPickerEl = null; }
}

// ---------- 切歌状态管理与视觉反馈 ----------
let isSwitching = false;
let switchingTimer = null;

function showSwitching(target = {}) {
  isSwitching = true;
  const banner = $('#switchingBanner');
  const textEl = $('#switchingText');
  const nextSongTitle = target.title || (queue[0] ? queue[0].title : '');
  const singer = target.singer || (queue[0] ? queue[0].singer : '');

  if (banner && textEl) {
    if (nextSongTitle) {
      textEl.innerHTML = `正在切歌：准备播放 <b>${esc(nextSongTitle)}</b>${singer ? ` · ${esc(singer)}` : ''}，正在调起 QQ 音乐…`;
    } else {
      textEl.textContent = '正在为您切歌，已向客户端发送指令，请稍候…';
    }
    banner.classList.remove('hidden');
  }

  // 切歌按钮禁用 & 加上动画
  const btnNext = $('#btnNext');
  const btnQueueNext = $('#btnQueueNext');
  if (btnNext) {
    btnNext.classList.add('btn-switching');
    btnNext.setAttribute('title', '切歌中，请稍候…');
  }
  if (btnQueueNext) {
    btnQueueNext.classList.add('btn-switching');
    btnQueueNext.setAttribute('title', '切歌中…');
  }

  // 卡片呼吸光晕 & 标签
  const npCard = $('#npCard');
  const queueCard = $('#queueNowPlaying');
  if (npCard) npCard.classList.add('is-switching');
  if (queueCard) queueCard.classList.add('is-switching');

  const npState = $('#npState');
  if (npState) {
    npState.textContent = '⟳ 切歌中…';
    npState.classList.add('switching-tag');
  }

  if (nextSongTitle) {
    const npTitle = $('#npTitle');
    if (npTitle) npTitle.textContent = `即将播放：${nextSongTitle}`;
    const npSinger = $('#npSinger');
    if (npSinger) npSinger.textContent = singer;
  }

  // 15 秒安全超时兜底
  if (switchingTimer) clearTimeout(switchingTimer);
  switchingTimer = setTimeout(() => {
    hideSwitching();
  }, 15000);
}

function hideSwitching() {
  isSwitching = false;
  if (switchingTimer) { clearTimeout(switchingTimer); switchingTimer = null; }
  const banner = $('#switchingBanner');
  if (banner) banner.classList.add('hidden');

  const btnNext = $('#btnNext');
  const btnQueueNext = $('#btnQueueNext');
  if (btnNext) {
    btnNext.classList.remove('btn-switching');
    btnNext.setAttribute('title', '切歌（N）');
  }
  if (btnQueueNext) {
    btnQueueNext.classList.remove('btn-switching');
    btnQueueNext.setAttribute('title', '播放下一首');
  }

  const npCard = $('#npCard');
  const queueCard = $('#queueNowPlaying');
  if (npCard) npCard.classList.remove('is-switching');
  if (queueCard) queueCard.classList.remove('is-switching');

  const npState = $('#npState');
  if (npState) npState.classList.remove('switching-tag');
}

async function triggerNextSong() {
  if (isSwitching) return; // 正在切歌中，防止重复疯狂点击
  const nextTarget = queue[0] ? { title: queue[0].title, singer: queue[0].singer } : {};
  showSwitching(nextTarget);
  try {
    await api('/api/control/next', { method: 'POST' });
  } catch (err) {
    hideSwitching();
    showErr(err);
  }
}

// ---------- controls & volume ----------
$('#btnPlay').onclick = () => api('/api/control/pause', { method: 'POST' }).catch(showErr);
$('#btnNext').onclick = triggerNextSong;
$('#btnPrev').onclick = () => api('/api/control/prev', { method: 'POST' }).catch(showErr);
const btnQueueNext = $('#btnQueueNext');
if (btnQueueNext) btnQueueNext.onclick = triggerNextSong;

const volSlider = $('#volSlider');
async function loadVol() {
  try {
    const { volume } = await api('/api/volume');
    if (volume >= 0) { volSlider.value = volume; $('#volNum').textContent = volume; }
  } catch {}
}
volSlider.oninput = () => { $('#volNum').textContent = volSlider.value; };
volSlider.onchange = () => api('/api/volume', { method: 'POST', body: { volume: +volSlider.value } }).catch(showErr);

// ---------- search（点歌页 / 歌单页共用的「搜索 → 点选」选择器） ----------
const SEARCH_CACHE_KEY = 'juke_search_state';
let currentSearchSongs = [];

function updateSearchClearBtn() {
  const clearBtn = $('#searchClearBtn');
  if (!clearBtn) return;
  const kw = $('#searchInput') ? $('#searchInput').value.trim() : '';
  const hasResults = currentSearchSongs.length > 0;
  clearBtn.classList.toggle('hidden', !kw && !hasResults);
}

function saveSearchState() {
  const kw = $('#searchInput') ? $('#searchInput').value : '';
  const msgEl = $('#searchMsg');
  const msgText = msgEl ? msgEl.textContent : '';
  const msgClass = msgEl ? msgEl.className : '';
  updateSearchClearBtn();
  try {
    sessionStorage.setItem(SEARCH_CACHE_KEY, JSON.stringify({
      keyword: kw,
      songs: currentSearchSongs,
      msgText,
      msgClass
    }));
  } catch {}
}

function clearSearchState() {
  currentSearchSongs = [];
  try { sessionStorage.removeItem(SEARCH_CACHE_KEY); } catch {}
  if ($('#searchInput')) $('#searchInput').value = '';
  if ($('#searchResults')) $('#searchResults').innerHTML = '';
  const msg = $('#searchMsg');
  if (msg) { msg.className = 'msg'; msg.textContent = ''; }
  updateSearchClearBtn();
}

function restoreSearchState() {
  try {
    const raw = sessionStorage.getItem(SEARCH_CACHE_KEY);
    if (!raw) {
      updateSearchClearBtn();
      return;
    }
    const cache = JSON.parse(raw);
    if ($('#searchInput') && typeof cache.keyword === 'string') {
      $('#searchInput').value = cache.keyword;
    }
    if (cache.msgText && $('#searchMsg')) {
      $('#searchMsg').textContent = cache.msgText;
      $('#searchMsg').className = cache.msgClass || 'msg';
    }
    if (Array.isArray(cache.songs) && cache.songs.length > 0) {
      currentSearchSongs = cache.songs;
      attachSearchResultsPicker(cache.songs);
    }
    updateSearchClearBtn();
  } catch (e) {
    console.warn('还原搜索状态失败', e);
    updateSearchClearBtn();
  }
}

function attachSearchResultsPicker(songs) {
  renderPicker($('#searchResults'), songs, [
    {
      label: '点播',
      onPick: async (s) => {
        const msg = $('#searchMsg');
        try {
          await api('/api/queue/add', { method: 'POST', body: { title: s.title, singer: s.singer, songMid: s.songMid } });
          msg.className = 'msg ok'; msg.textContent = `「${s.title}」已加入共享队列`;
          saveSearchState();
        } catch (err) {
          msg.className = 'msg err'; msg.textContent = err.message;
          saveSearchState();
        }
      }
    },
    {
      label: '+歌单',
      title: '加入歌单',
      onPick: (s, btn) => {
        const rect = btn.getBoundingClientRect();
        showPlaylistPicker(rect.left, rect.bottom + 4, { title: s.title, singer: s.singer || '', songMid: s.songMid || '' });
      }
    }
  ]);
}

async function searchSongs(kw) {
  const { songs } = await api(`/api/search?kw=${encodeURIComponent(kw)}`);
  return songs;
}

function renderPicker(box, songs, actions) {
  box.innerHTML = '';
  if (!songs.length) { box.innerHTML = '<div class="q-empty">没搜到，换个关键词试试。</div>'; return; }
  for (const s of songs) {
    const row = document.createElement('div');
    row.className = 'pl-song';
    row.innerHTML = `
      <div class="ps-title"><b>${esc(s.title)}</b><span>${esc(s.singer || '')}</span></div>
      <span class="q-adder"></span>
      <span class="ps-btns">
        ${actions.map((a, i) => `<button class="mini-btn" data-ai="${i}"${a.title ? ` title="${esc(a.title)}"` : ''}>${esc(a.label)}</button>`).join('')}
      </span>`;
    row.querySelectorAll('.ps-btns button').forEach(b => {
      b.onclick = (e) => { e.stopPropagation(); actions[+b.dataset.ai].onPick(s, b); };
    });
    box.appendChild(row);
  }
}

$('#searchForm').onsubmit = async (e) => {
  e.preventDefault();
  const kw = $('#searchInput').value.trim();
  if (!kw) return;
  const msg = $('#searchMsg');
  msg.className = 'msg'; msg.textContent = '搜索中…';
  try {
    const songs = await searchSongs(kw);
    currentSearchSongs = songs;
    attachSearchResultsPicker(songs);
    msg.className = 'msg ok'; msg.textContent = `找到 ${songs.length} 首，点一下加入`;
    saveSearchState();
  } catch (err) {
    msg.className = 'msg err'; msg.textContent = err.message;
    currentSearchSongs = [];
    saveSearchState();
  }
};

if ($('#searchClearBtn')) {
  $('#searchClearBtn').onclick = () => {
    clearSearchState();
  };
}
if ($('#searchInput')) {
  $('#searchInput').addEventListener('input', () => {
    saveSearchState();
  });
}

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
      <button class="mini-btn pl-export" title="导出 JSON">导出</button>
      <button class="mini-btn pl-open">打开</button>`;
    item.querySelector('.pl-open').onclick = () => openPlaylist(p.id, p.name);
    item.querySelector('.pl-play').onclick = async () => {
      try { await api(`/api/playlists/${p.id}/play?front=1`, { method: 'POST' }); }
      catch (e) { showErr(e); }
    };
    item.querySelector('.pl-export').onclick = () => exportPlaylistJson(p.id, p.name);
    div.appendChild(item);
  }
}

function downloadJson(filename, data) {
  const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = filename;
  a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 3000);
}

async function exportPlaylistJson(pid, name) {
  try {
    const data = await api(`/api/playlists/${pid}/export`);
    downloadJson(`${data.name || name || 'playlist'}.json`, data);
  } catch (e) { showErr(e); }
}

$('#plForm').onsubmit = async (e) => {
  e.preventDefault();
  const name = $('#plName').value.trim();
  if (!name) return;
  await api('/api/playlists', { method: 'POST', body: { name } }).catch(showErr);
  $('#plName').value = '';
  loadPlaylists();
};

// ---------- 歌单 JSON 导入 ----------
const btnImportPl = $('#btnImportPl');
if (btnImportPl) btnImportPl.onclick = () => $('#plFileInput').click();
const plFileInput = $('#plFileInput');
if (plFileInput) plFileInput.onchange = async () => {
  const file = plFileInput.files[0];
  plFileInput.value = '';
  if (!file) return;
  try {
    const data = JSON.parse(await file.text());
    // 支持单个歌单或 {playlists: [...]} 批量格式
    const items = Array.isArray(data) ? data
      : data.type === 'jukebox_playlist' ? [data]
      : Array.isArray(data.playlists) ? data.playlists : [];
    if (!items.length) throw new Error('文件格式不对：没找到歌单数据');
    for (const it of items) {
      await api('/api/playlists/import', {
        method: 'POST',
        body: { name: it.name || '', description: it.description || '', songs: it.songs || [] }
      });
    }
    loadPlaylists();
  } catch (e) { showErr(e); }
};

async function openPlaylist(id, name) {
  const { songs } = await api(`/api/playlists/${id}`);
  $('#plList').classList.add('hidden');
  const d = $('#plDetail');
  d.classList.remove('hidden');
  d.innerHTML = `
    <button class="back-link" id="plBack">← 返回歌单列表</button>
    <h2 class="sec-title">${esc(name)}
      <button class="mini-btn pl-export-btn" id="plExportBtn" title="导出 JSON">导出 JSON</button>
    </h2>
    <div class="pl-add">
      <input id="plSongInput" placeholder="回车搜索歌曲，点选加入歌单" autocomplete="off">
      <button class="btn-primary" id="plSongBtn">搜索</button>
    </div>
    <div id="plPick"></div>
    <div id="plSongs"></div>`;
  $('#plBack').onclick = loadPlaylists;
  $('#plExportBtn').onclick = () => exportPlaylistJson(id, name);
  $('#plSongBtn').onclick = async () => {
    const raw = $('#plSongInput').value.trim();
    if (!raw) return;
    try {
      const songs = await searchSongs(raw);
      renderPicker($('#plPick'), songs, [
        {
          label: '加入歌单',
          onPick: async (s) => {
            try {
              await api(`/api/playlists/${id}/songs`, { method: 'POST', body: { title: s.title, singer: s.singer, songMid: s.songMid } });
              $('#plPick').innerHTML = '';
              $('#plSongInput').value = '';
              openPlaylist(id, name);
            } catch (e) { showErr(e); }
          }
        },
        {
          label: '+队列',
          title: '加入播放队列',
          onPick: async (s, btn) => {
            try {
              await api('/api/queue/add', { method: 'POST', body: { title: s.title, singer: s.singer, songMid: s.songMid } });
              btn.textContent = '已入队';
              setTimeout(() => { btn.textContent = '+队列'; }, 1500);
            } catch (e) { showErr(e); }
          }
        }
      ]);
    } catch (e) { showErr(e); }
  };
  const box = $('#plSongs');
  if (!songs.length) { box.innerHTML = '<div class="q-empty">歌单还空着。</div>'; return; }
  box.innerHTML = songs.map((s, i) => `
    <div class="pl-song" draggable="true" data-i="${i}">
      <span class="ps-idx">${i + 1}</span>
      <div class="ps-title"><b>${esc(s.title)}</b><span>${esc(s.singer || '')}</span></div>
      <span class="q-adder">${s.adder ? esc(s.adder) : ''}</span>
      <span class="ps-btns">
        <button class="mini-btn ps-enq" data-i="${i}" title="加入播放列表">+队列</button>
        <button class="mini-btn ps-up" data-i="${i}" ${i === 0 ? 'disabled' : ''} title="上移">↑</button>
        <button class="mini-btn ps-play" data-i="${i}" title="从这首开始连播整个歌单">从此播</button>
        <button class="mini-btn ps-del" data-i="${i}" title="移出歌单">✕</button>
      </span>
    </div>`).join('');
  box.querySelectorAll('.ps-del').forEach(b => b.onclick = async (e) => {
    e.stopPropagation();
    const s = songs[+b.dataset.i];
    await api(`/api/playlists/${id}/songs/${s.id}/delete`, { method: 'POST' }).catch(showErr);
    openPlaylist(id, name);
  });
  box.querySelectorAll('.ps-play').forEach(b => b.onclick = async (e) => {
    e.stopPropagation();
    // 从这首（含）起把歌单余下部分插到共享队列队首：正在播的歌不打断，切歌后立即接歌单
    const i = +b.dataset.i;
    try { await api(`/api/playlists/${id}/play?from_position=${i}&front=1`, { method: 'POST' }); }
    catch (e) { showErr(e); }
  });
  box.querySelectorAll('.ps-enq').forEach(b => b.onclick = async (e) => {
    e.stopPropagation();
    const s = songs[+b.dataset.i];
    try {
      await api('/api/queue/add', { method: 'POST', body: { title: s.title, singer: s.singer, songMid: s.song_mid || s.songMid || '' } });
    } catch (err) { showErr(err); }
  });
  box.querySelectorAll('.ps-up').forEach(b => b.onclick = async (e) => {
    e.stopPropagation();
    const i = +b.dataset.i;
    await api(`/api/playlists/${id}/reorder?start=${i}&stop=${i - 1}`, { method: 'POST' }).catch(showErr);
    openPlaylist(id, name);
  });
  attachPlaylistDrag(id, name, box, songs);
}

// ---------- 歌单内拖拽排序 + 拖到播放列表 ----------
function attachPlaylistDrag(pid, pname, box, songs) {
  let dragIdx = -1;
  const queueCol = document.querySelector('.queue-col');
  box.querySelectorAll('.pl-song').forEach(row => {
    row.addEventListener('dragstart', (e) => {
      dragIdx = +row.dataset.i;
      row.classList.add('dragging');
      e.dataTransfer.effectAllowed = 'copyMove';
      const s = songs[dragIdx];
      try { e.dataTransfer.setData('text/plain', `plsong:${JSON.stringify({ title: s.title, singer: s.singer, songMid: s.song_mid || s.songMid || '' })}`); } catch {}
    });
    row.addEventListener('dragend', () => {
      row.classList.remove('dragging');
      box.querySelectorAll('.pl-song').forEach(x => x.classList.remove('drag-over-top', 'drag-over-bottom'));
      if (queueCol) queueCol.classList.remove('queue-drop-hint');
    });
    row.addEventListener('dragover', (e) => {
      if (dragIdx < 0) return;
      e.preventDefault();
      const rect = row.getBoundingClientRect();
      const before = e.clientY < rect.top + rect.height / 2;
      row.classList.toggle('drag-over-top', before);
      row.classList.toggle('drag-over-bottom', !before);
    });
    row.addEventListener('dragleave', () => row.classList.remove('drag-over-top', 'drag-over-bottom'));
    row.addEventListener('drop', async (e) => {
      if (dragIdx < 0) return;
      e.preventDefault();
      e.stopPropagation();
      const rect = row.getBoundingClientRect();
      let stop = +row.dataset.i;
      if (e.clientY >= rect.top + rect.height / 2) stop += 1;
      if (dragIdx < stop) stop -= 1;
      if (stop !== dragIdx) {
        await api(`/api/playlists/${pid}/reorder?start=${dragIdx}&stop=${stop}`, { method: 'POST' }).catch(showErr);
        openPlaylist(pid, pname);
      }
      dragIdx = -1;
    });
  });
  // 拖到右侧队列栏：整行松手即入队
  if (queueCol) {
    queueCol.addEventListener('dragover', (e) => {
      if (dragIdx < 0) return;
      e.preventDefault();
      queueCol.classList.add('queue-drop-hint');
    });
    queueCol.addEventListener('dragleave', () => queueCol.classList.remove('queue-drop-hint'));
    queueCol.addEventListener('drop', async (e) => {
      if (dragIdx < 0) return;
      e.preventDefault();
      queueCol.classList.remove('queue-drop-hint');
      const s = songs[dragIdx];
      dragIdx = -1;
      try {
        await api('/api/queue/add', { method: 'POST', body: { title: s.title, singer: s.singer, songMid: s.song_mid || s.songMid || '' } });
      } catch (err) { showErr(err); }
    });
  }
}

// ---------- stats ----------
let statPeriod = 'day';
let statsCache = { topSongs: [], history: [] };
$$('.stat-period .chip').forEach(b => b.onclick = () => {
  $$('.stat-period .chip').forEach(x => x.classList.toggle('active', x === b));
  statPeriod = b.dataset.p; loadStats();
});

const statSearchInput = $('#statSearchInput');
if (statSearchInput) statSearchInput.addEventListener('input', () => renderStatsLists());

function matchStatsFilter(title, singer) {
  const kw = (statSearchInput?.value || '').trim().toLowerCase();
  if (!kw) return true;
  return `${title} ${singer || ''}`.toLowerCase().includes(kw);
}

async function loadStats() {
  const [s, h] = await Promise.all([
    api(`/api/stats?period=${statPeriod}`), api('/api/history?limit=30')
  ]).catch(() => [null, null]);
  if (!s) return;
  const fmt = (sec) => sec >= 3600 ? `${Math.round(sec / 3600)} 小时` : `${Math.round((sec || 0) / 60)} 分钟`;
  const hour = new Date().getHours();
  const maxU = Math.max(1, ...s.byUser.map(u => u.plays));
  const maxH = Math.max(1, ...s.byHour.map(x => x.plays));
  statsCache = { topSongs: s.topSongs || [], history: (h && h.history) || [] };
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
    <div id="topSongsList"></div>`;
  $('#historyList').innerHTML = '';
  renderStatsLists(fmt);
}

function renderStatsLists(fmt) {
  const fmtFn = fmt || ((sec) => sec >= 3600 ? `${Math.round(sec / 3600)} 小时` : `${Math.round((sec || 0) / 60)} 分钟`);
  const topBox = $('#topSongsList');
  if (topBox) {
    const rows = statsCache.topSongs.filter(x => matchStatsFilter(x.title, x.singer));
    topBox.innerHTML = rows.map(x => `
      <div class="hist-row"><span class="h-time">×${x.plays}</span>
        <span class="h-title"><b>${esc(x.title)}</b> · ${esc(x.singer || '')}</span>
        <span class="h-by">${fmtFn(x.seconds)}</span>
        <span class="h-btns">
          <button class="mini-btn stat-enq" data-title="${esc(x.title)}" data-singer="${esc(x.singer || '')}">点播</button>
          <button class="mini-btn stat-toPl" data-title="${esc(x.title)}" data-singer="${esc(x.singer || '')}">+歌单</button>
        </span>
      </div>`).join('') || '<div class="q-empty">没有匹配的歌曲。</div>';
    bindStatRowActions(topBox);
  }
  const histBox = $('#historyList');
  if (histBox) {
    const rows = statsCache.history.filter(x => matchStatsFilter(x.title, x.singer));
    histBox.innerHTML = rows.map(x => `
      <div class="hist-row">
        <span class="h-time">${x.started_at.slice(5, 16).replace('T', ' ')}</span>
        <span class="h-title"><b>${esc(x.title)}</b> · ${esc(x.singer || '')}</span>
        <span class="h-by">${x.requestedName ? esc(x.requestedName) : ''}</span>
        ${x.skipped_by ? '<span class="h-cut">被切</span>' : ''}
        <span class="h-btns">
          <button class="mini-btn stat-enq" data-title="${esc(x.title)}" data-singer="${esc(x.singer || '')}" data-mid="${esc(x.song_mid || '')}">重播</button>
          <button class="mini-btn stat-toPl" data-title="${esc(x.title)}" data-singer="${esc(x.singer || '')}" data-mid="${esc(x.song_mid || '')}">+歌单</button>
        </span>
      </div>`).join('') || '<div class="q-empty">没有匹配的记录。</div>';
    bindStatRowActions(histBox);
  }
}

function bindStatRowActions(box) {
  box.querySelectorAll('.stat-enq').forEach(b => b.onclick = async () => {
    try {
      await api('/api/queue/add', {
        method: 'POST',
        body: { title: b.dataset.title, singer: b.dataset.singer || '', songMid: b.dataset.mid || '' }
      });
    } catch (e) { showErr(e); }
  });
  box.querySelectorAll('.stat-toPl').forEach(b => b.onclick = (e) => {
    const rect = b.getBoundingClientRect();
    showPlaylistPicker(rect.left, rect.bottom + 4, {
      title: b.dataset.title, singer: b.dataset.singer || '', songMid: b.dataset.mid || ''
    });
  });
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

// ---------- QQ 音乐歌单广场（分类浏览 + 歌单搜索 + 分页加载） ----------
let recPlaylistsCache = [];   // 已加载的累积列表（卡片渲染与弹窗查找共用）
const recState = { mode: 'square', category: 10000000, kw: '', page: 1, hasMore: true, loading: false };
const REC_PAGE_SIZE = 20;

async function loadRecCategories() {
  const box = $('#recCats');
  if (!box) return;
  try {
    const { groups } = await api('/api/playlist_categories');
    if (!groups || !groups.length) return;
    box.innerHTML = groups.map(g =>
      `<span class="rec-cat-group">${esc(g.group)}</span>` +
      g.items.map(c => `<button class="rec-chip" data-cid="${c.id}">${esc(c.name)}</button>`).join('')
    ).join('');
    box.querySelectorAll('.rec-chip').forEach(chip => chip.onclick = () => {
      recState.mode = 'square';
      recState.category = Number(chip.dataset.cid);
      const inp = $('#recSearchInput');
      if (inp) inp.value = '';
      reloadRecGrid();
    });
    markActiveChip();
  } catch (e) { /* 分类拉取失败不阻塞歌单本体 */ }
}

function markActiveChip() {
  document.querySelectorAll('#recCats .rec-chip').forEach(c =>
    c.classList.toggle('active', recState.mode === 'square' && Number(c.dataset.cid) === recState.category));
}

async function reloadRecGrid() {
  recState.page = 1;
  recState.hasMore = true;
  recPlaylistsCache = [];
  const grid = $('#recGrid');
  if (grid) grid.innerHTML = '<div class="rec-loading">正在拉取 QQ 音乐歌单…</div>';
  await loadRecMore();
}

async function loadRecMore() {
  const grid = $('#recGrid');
  if (!grid || recState.loading || !recState.hasMore) return;
  recState.loading = true;
  updateRecMoreBtn();
  try {
    let data;
    if (recState.mode === 'search') {
      data = await api(`/api/playlist_search?kw=${encodeURIComponent(recState.kw)}&page=${recState.page}&per_page=${REC_PAGE_SIZE}`);
      recState.hasMore = !!data.has_more;
    } else {
      data = await api(`/api/playlist_square?category=${recState.category}&page=${recState.page}&per_page=${REC_PAGE_SIZE}`);
      recState.hasMore = recPlaylistsCache.length + (data.playlists || []).length < (data.total || 0);
    }
    recPlaylistsCache = recPlaylistsCache.concat(data.playlists || []);
    recState.page += 1;
    renderRecGrid();
  } catch (e) {
    recState.hasMore = false;
    if (!recPlaylistsCache.length) grid.innerHTML = `<div class="rec-error">歌单拉取失败：${esc(e.message)}</div>`;
  } finally {
    recState.loading = false;
    updateRecMoreBtn();
    markActiveChip();
  }
}

function updateRecMoreBtn() {
  const btn = $('#btnRecMore');
  if (!btn) return;
  btn.classList.toggle('hidden', !recState.hasMore);
  btn.textContent = recState.loading ? '加载中…' : `加载更多（已显示 ${recPlaylistsCache.length} 个）`;
}

function renderRecGrid() {
  const grid = $('#recGrid');
  if (!grid) return;
  if (!recPlaylistsCache.length) { grid.innerHTML = '<div class="rec-error">没有找到歌单，换个分类或关键词试试。</div>'; return; }
  grid.innerHTML = recPlaylistsCache.map(p => `
    <div class="rec-card" data-dissid="${esc(p.dissid)}" title="点击查看歌曲">
      <img class="rec-cover" src="${esc(p.cover)}" alt="" loading="lazy" referrerpolicy="no-referrer">
      <div class="rec-body">
        <div class="rec-name">${esc(p.title)}</div>
        <div class="rec-meta">${fmtListen(p.listennum)}</div>
        <div class="rec-actions">
          <button class="mini-btn rec-play" data-dissid="${esc(p.dissid)}">播放全部</button>
          <button class="mini-btn rec-enq" data-dissid="${esc(p.dissid)}">加入队列</button>
        </div>
      </div>
    </div>`).join('');
  grid.querySelectorAll('.rec-play').forEach(b => b.onclick = (e) => {
    e.stopPropagation();
    playRecommendation(b.dataset.dissid, false);
  });
  grid.querySelectorAll('.rec-enq').forEach(b => b.onclick = (e) => {
    e.stopPropagation();
    playRecommendation(b.dataset.dissid, true);
  });
  grid.querySelectorAll('.rec-card').forEach(card => card.onclick = () => openRecModal(card.dataset.dissid));
}

function fmtListen(n) {
  n = Number(n) || 0;
  if (n >= 100000000) return `${(n / 100000000).toFixed(1)}亿 次播放`;
  if (n >= 10000) return `${(n / 10000).toFixed(1)}万 次播放`;
  return n ? `${n} 次播放` : '';
}

async function playRecommendation(dissid, front) {
  try {
    await api(`/api/recommendations/${dissid}/play?front=${front ? 1 : 0}`, { method: 'POST' });
    closeRecModal();
  } catch (e) { showErr(e); }
}

async function openRecModal(dissid) {
  const pl = recPlaylistsCache.find(p => String(p.dissid) === String(dissid));
  const mask = document.createElement('div');
  mask.className = 'modal-mask';
  mask.innerHTML = `
    <div class="modal-card">
      <div class="modal-head">
        <h2>${esc(pl ? pl.title : '歌单')}</h2>
        <span>
          <button class="mini-btn rec-play-modal">播放全部</button>
          <button class="mini-btn rec-enq-modal">加入队列</button>
          <button class="mini-btn rec-close">✕</button>
        </span>
      </div>
      <div class="modal-songs"><div class="rec-loading">加载歌曲中…</div></div>
    </div>`;
  document.body.appendChild(mask);
  mask.addEventListener('click', (e) => { if (e.target === mask) mask.remove(); });
  mask.querySelector('.rec-close').onclick = () => mask.remove();
  mask.querySelector('.rec-play-modal').onclick = () => playRecommendation(dissid, false);
  mask.querySelector('.rec-enq-modal').onclick = () => playRecommendation(dissid, true);
  try {
    const { songs } = await api(`/api/recommendations/${dissid}/songs?limit=30`);
    const box = mask.querySelector('.modal-songs');
    box.innerHTML = songs.length ? songs.map((s, i) => `
      <div class="modal-song">
        <span class="ps-idx">${i + 1}</span>
        <div class="ps-title"><b>${esc(s.title)}</b><span>${esc(s.singer || '')}</span></div>
        <button class="mini-btn modal-enq-one" data-i="${i}" title="加入播放队列">+队列</button>
        <button class="mini-btn modal-toPl-one" data-i="${i}">+歌单</button>
      </div>`).join('') : '<div class="rec-error">这首歌单暂时拉不到歌曲。</div>';
    box.querySelectorAll('.modal-enq-one').forEach(b => b.onclick = async () => {
      const s = songs[+b.dataset.i];
      try {
        await api('/api/queue/add', { method: 'POST', body: { title: s.title, singer: s.singer, songMid: s.songMid } });
        b.textContent = '已入队';
        setTimeout(() => { b.textContent = '+队列'; }, 1500);
      } catch (e) { showErr(e); }
    });
    box.querySelectorAll('.modal-toPl-one').forEach(b => b.onclick = () => {
      const s = songs[+b.dataset.i];
      const rect = b.getBoundingClientRect();
      showPlaylistPicker(rect.left, rect.bottom + 4, { title: s.title, singer: s.singer, songMid: s.songMid });
    });
  } catch (e) {
    mask.querySelector('.modal-songs').innerHTML = `<div class="rec-error">${esc(e.message)}</div>`;
  }
}

const btnRefreshRec = $('#btnRefreshRec');
if (btnRefreshRec) btnRefreshRec.onclick = () => reloadRecGrid();

const btnRecMore = $('#btnRecMore');
if (btnRecMore) btnRecMore.onclick = () => loadRecMore();

const recSearchInput = $('#recSearchInput');
if (recSearchInput) recSearchInput.addEventListener('keydown', (e) => {
  if (e.key !== 'Enter') return;
  e.preventDefault();
  const kw = recSearchInput.value.trim();
  if (!kw) {
    // 清空搜索词 → 回到当前分类广场
    if (recState.mode !== 'square') { recState.mode = 'square'; reloadRecGrid(); }
    return;
  }
  recState.mode = 'search';
  recState.kw = kw;
  reloadRecGrid();
});

// ---------- boot ----------
(async () => {
  await checkMe();
  restoreSearchState();
  refreshKbdUI();
  connectWS();
  loadVol();
  refreshPlayerStatus();
  setInterval(refreshPlayerStatus, 15000);
  loadRecCategories();
  reloadRecGrid();
  try { applyState(await api('/api/state')); } catch {}
})();
