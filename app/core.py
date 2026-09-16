"""Jukebox core: shared queue, play loop, history, play windows, WebSocket broadcast."""
import asyncio
import datetime as dt
import json
import threading
import time

from .player_select import player
from . import keepalive
from .db import get_db, init_db

state = {
    "queue": [],          # [{song_mid, title, singer, addedBy: {id, name}}]
    "current": None,      # {...item, requestedBy, startedAt, historyId}
    "paused": False,
    "allowPlay": True,
    "nowPlaying": None,
    "switching": None,    # 正在切歌的目标歌：{"title", "singer", "requestedBy"}
    "clientQueue": None,  # 客户端播放队列：{"songs":[{title,singer}], "currentIndex", "total"}；仅桌面后端
}

_clients = set()  # asyncio websockets
_loop: asyncio.AbstractEventLoop = None


def set_loop(loop):
    global _loop
    _loop = loop


def snapshot():
    return {k: state[k] for k in state}


def broadcast(event, data=None):
    """Thread-safe broadcast to all connected websockets."""
    if _loop is None:
        return
    msg = json.dumps({"event": event, "data": data, "state": snapshot()}, ensure_ascii=False)
    for ws in list(_clients):
        asyncio.run_coroutine_threadsafe(_safe_send(ws, msg), _loop)


async def _safe_send(ws, msg):
    try:
        await ws.send_text(msg)
    except Exception:
        pass


# ---------------- queue ----------------
def enqueue(song: dict, user: dict, front: bool = False) -> dict:
    item = {
        "songMid": song.get("songMid", ""),
        "title": song["title"],
        "singer": song.get("singer", ""),
        "addedBy": {"id": user["id"], "name": user["name"]},
    }
    if front:
        state["queue"].insert(0, item)
    else:
        state["queue"].append(item)
    broadcast("queue:changed", {"item": item})
    kick()
    return item


def remove_at(index: int, user: dict):
    if 0 <= index < len(state["queue"]):
        removed = state["queue"].pop(index)
        broadcast("queue:changed", {"removed": removed, "by": user["name"]})
        return removed
    return None


def reorder(start: int, stop: int):
    q = state["queue"]
    if not (0 <= start < len(q)):
        return
    item = q.pop(start)
    stop = max(0, min(len(q), stop if stop < 0 else stop))
    q.insert(stop, item)
    broadcast("queue:changed", {})


# ---------------- play loop ----------------
_kick_lock = threading.Lock()
# 播放转换锁：点歌是「客户端搜索框输入 → 等结果 → 双击」的独占操作（约 5~15 秒），
# 多人同时点歌/切歌时必须严格串行，否则两套自动操作在同一个搜索框里互相踩踏。
_play_lock = asyncio.Lock()


def kick():
    """Trigger the async play loop from any thread."""
    if _loop is None:
        return
    asyncio.run_coroutine_threadsafe(_loop_once(), _loop)


async def _loop_once():
    async with _play_lock:
        if state["current"] is not None or not state["queue"] or not state["allowPlay"]:
            return
        item = state["queue"].pop(0)
        state["switching"] = {
            "title": item["title"],
            "singer": item.get("singer", ""),
            "requestedBy": item.get("addedBy")
        }
        broadcast("play:switching", state["switching"])
        try:
            await start_play(item, item["addedBy"])
        except Exception as e:
            # 搜索/播放失败（网络、找不到结果、客户端卡住）
            print("[loop]", e)
            state["switching"] = None
            broadcast("play:error", {"item": item, "error": str(e)})

            # 针对歌曲可能由于临时网络抖动、搜索失败或客户端正在响应，
            # 避免直接丢弃歌曲并秒切后续全部队列，先给予合理缓冲重试/等待，失败后记录并在队列中保留或暂缓自动连续快切
            retried = item.get("_fail_count", 0)
            if retried < 1:
                item["_fail_count"] = retried + 1
                # 放回队首稍后再试一次，避免用户点的一组歌直接全军覆没
                state["queue"].insert(0, item)
                broadcast("queue:changed", {})
                await asyncio.sleep(3)
                kick()
            else:
                # 重试仍失败，间隔 3 秒再切下一首，防止瞬间连跳多首把整张歌单清空
                await asyncio.sleep(3)
                kick()
        finally:
            state["switching"] = None


async def start_play(item: dict, requested_by: dict):
    info = await player.search_and_play(item)
    started_at = dt.datetime.now().isoformat(timespec="seconds")
    conn = get_db()
    cur = conn.execute(
        "INSERT INTO play_history (song_mid, title, singer, started_at, requested_by) "
        "VALUES (?, ?, ?, ?, ?)",
        (item.get("songMid", ""), item["title"], item.get("singer", ""), started_at, requested_by["id"]),
    )
    conn.commit()
    state["switching"] = None
    state["current"] = {
        **item,
        "requestedBy": requested_by,
        "startedAt": started_at,
        "historyId": cur.lastrowid,
    }
    state["paused"] = False
    broadcast("play:started", state["current"])


async def finish_current(skipped_by=None):
    cur = state["current"]
    if not cur:
        return
    played = int((dt.datetime.now() - dt.datetime.fromisoformat(cur["startedAt"])).total_seconds())
    conn = get_db()
    conn.execute(
        "UPDATE play_history SET ended_at=?, played_sec=?, completed=?, skipped_by=? WHERE id=?",
        (dt.datetime.now().isoformat(timespec="seconds"), played, 0 if skipped_by else 1,
         skipped_by["id"] if skipped_by else None, cur["historyId"]),
    )
    conn.commit()
    state["current"] = None
    if state["queue"]:
        next_item = state["queue"][0]
        state["switching"] = {
            "title": next_item["title"],
            "singer": next_item.get("singer", ""),
            "requestedBy": next_item.get("addedBy")
        }
    else:
        state["switching"] = None
    broadcast("play:ended", {"title": cur["title"], "skippedBy": skipped_by, "switching": state["switching"]})
    kick()


async def next_song(user=None):
    if state["current"]:
        await finish_current(skipped_by={"id": user["id"], "name": user["name"]} if user else None)
    elif state["queue"]:
        next_item = state["queue"][0]
        state["switching"] = {
            "title": next_item["title"],
            "singer": next_item.get("singer", ""),
            "requestedBy": next_item.get("addedBy")
        }
        broadcast("play:switching", state["switching"])
        kick()
    else:
        kick()


async def toggle_pause():
    await player.control("playPause")
    state["paused"] = not state["paused"]
    broadcast("play:paused", {"paused": state["paused"]})


async def prev_song(user=None):
    """上一首 = 重播最近一首已结束的歌（被切/播完都算）；没有历史则重播当前。

    不透传 SMTC 的 skip_previous：客户端自己的队列是「当前歌的搜索结果列表」，
    透传会切到无关歌曲，随后又被轮询器判为切歌，表现为乱跳/无效。
    """
    conn = get_db()
    row = conn.execute(
        "SELECT song_mid, title, singer FROM play_history WHERE ended_at IS NOT NULL "
        "ORDER BY ended_at DESC, id DESC LIMIT 1").fetchone()
    if row is None and state["current"] is None:
        broadcast("play:prev", {"by": user["name"] if user else None})
        return
    if row is None:  # 没有历史但正在播：重播当前
        cur = state["current"]
        target = {"songMid": cur.get("songMid", ""), "title": cur["title"], "singer": cur.get("singer", "")}
    else:
        target = {"songMid": row["song_mid"], "title": row["title"], "singer": row["singer"]}
    interrupted = None
    if state["current"]:
        cur = state["current"]
        interrupted = {
            "songMid": cur.get("songMid", ""),
            "title": cur["title"],
            "singer": cur.get("singer", ""),
            "addedBy": cur.get("addedBy") or cur.get("requestedBy")
        }
        await finish_current(skipped_by={"id": user["id"], "name": user["name"]} if user else None)
    # 被中断的当前歌插回队首（稍后接着播它）
    if interrupted and interrupted["title"] != target["title"]:
        enqueue(interrupted, interrupted.get("addedBy") or (user or {"id": 0, "name": "系统"}), front=True)
    # 历史歌插到最前面立即开播
    enqueue(target, user or {"id": 0, "name": "系统"}, front=True)
    kick()
    broadcast("play:prev", {"by": user["name"] if user else None, "title": target["title"]})


# ---------------- now-playing poller: detects natural song end ----------------
_last_seen_title = ""
_mismatch_polls = 0
_tracked_cur_started = None  # 已跟踪的 current.startedAt（换歌时重置进度基线）
_last_pos = -1               # 上一轮 current 的播放进度（秒）；-1=无记录
_restart_suspect = False     # 上一轮疑似同名重播（进度回退到开头）

# 提前切换提前量（秒）：从触发切歌到客户端真正换曲需要 fcg 搜索 + UIA 自动操作，
# 合计约 4~12 秒。旧歌进入最后这段时间就提前铺开下一首的搜索播放，
# 客户端无缝接上新歌——没有暂停空档，也不会漏播客户端内部列表的歌。
# 副作用：旧歌的最后几秒会被新歌覆盖（相当于提前几秒切歌）。短于提前量的歌不适用。
SWITCH_LEAD_SEC = 10


async def _pause_client_after_end():
    """自然播完立刻暂停客户端：阻止 QQ 音乐按内部列表自动连播。
    队列里下一首搜出来后由 search_and_play 的「播放全部」点击自行恢复播放；
    队列空则保持暂停（keepalive 静音流保住蓝牙链路）。"""
    try:
        await player.control("pause")
        state["paused"] = True
        broadcast("play:paused", {"paused": True})
    except Exception as e:
        print("[poll] 播完后暂停客户端失败:", e)


def _update_keepalive(np):
    """根据真实播放状态驱动蓝牙保活：只有确认正在播放才停静音流，
    暂停/空闲/客户端无会话都保持（避免长时间暂停导致蓝牙音箱断连）。
    切歌间隙（switching）不动它，避免 5~15 秒的搜索间隙反复启停。"""
    if np and np.get("playing") is True:
        keepalive.stop()
        return
    if np and np.get("playing") is None:
        # 网页版后端不回报播放状态：用 core 状态近似判断
        if state["current"] and not state["paused"]:
            keepalive.stop()
            return
    if not state["switching"]:
        keepalive.start()


# ---------------- 客户端播放队列同步（仅桌面后端） ----------------
# 客户端自己连播/被人直接操作时，把它的播放队列读出来同步到网页展示。
# 点唱机自己点的歌不同步：那时客户端队列就是搜索结果，没有展示价值（清空展示）。
_q_sync_meta = {"title": None, "at": 0.0}
_q_sync_task = None
_q_periodic_at = 0.0


def _client_queue_sig(q):
    if not q:
        return None
    return (tuple((s.get("title", ""), s.get("singer", "")) for s in q.get("songs", [])),
            q.get("currentIndex"))


def _apply_client_queue(q):
    if q and q.pop("jukeboxSearchResidue", False):
        # 点唱机自己点歌产生的搜索结果残留，不是用户真想听的队列，不上站
        q = None
    changed = _client_queue_sig(state["clientQueue"]) != _client_queue_sig(q)
    state["clientQueue"] = q
    if changed:
        broadcast("clientQueue:changed", q)


async def _do_sync_client_queue():
    if state["switching"] or not getattr(player, "SUPPORTS_QUEUE_READ", False):
        return
    try:
        q = await player.client_queue()
    except Exception as e:
        print("[clientQueue]", e)
        return
    _apply_client_queue(q)


def schedule_client_queue_sync():
    global _q_sync_task
    if _loop is None or not getattr(player, "SUPPORTS_QUEUE_READ", False):
        return
    if _q_sync_task and not _q_sync_task.done():
        return
    _q_sync_task = asyncio.run_coroutine_threadsafe(_do_sync_client_queue(), _loop)


def _advance_client_queue_index(title: str) -> int:
    """在缓存的客户端队列里定位新歌：优先按线性推进（当前位后 1~3 首），
    找不到再全局找（客户端里手动跳歌）；都没有说明队列换了，返回 -1。"""
    cq = state["clientQueue"]
    if not cq:
        return -1
    songs = cq.get("songs", [])
    start = cq.get("currentIndex", -1) or 0
    for d in (1, 2, 3):
        i = start + d
        if 0 <= i < len(songs) and songs[i].get("title") == title:
            return i
    for i, s in enumerate(songs):
        if s.get("title") == title:
            return i
    return -1


def maybe_sync_client_queue(np):
    """由 poll_now_playing 驱动：检测到客户端自行换歌 → 同步其播放队列。

    - 点唱机点的歌（np.title == current.title）跳过，并清掉旧展示；
    - 新歌能在缓存队列里定位到 → 只本地推进 currentIndex（零开销，高亮实时）；
    - 定位不到 → 队列多半被换掉了，触发一次完整 UIA 读取（带 5 分钟抑制 + 10 分钟周期兜底）。
    """
    global _q_sync_meta, _q_periodic_at
    if not getattr(player, "SUPPORTS_QUEUE_READ", False):
        return
    now = time.time()
    title = (np or {}).get("title")
    cur = state["current"]
    if title and cur and title == cur["title"]:
        if state["clientQueue"] is not None:
            _apply_client_queue(None)  # 点唱机接管播放，客户端队列展示退场
        _q_sync_meta = {"title": title, "at": now}
        return
    if state["switching"]:
        return
    if not title:
        return
    cq = state["clientQueue"]
    if cq:
        i = _advance_client_queue_index(title)
        if i >= 0:
            if i != cq.get("currentIndex"):
                cq["currentIndex"] = i
                broadcast("clientQueue:changed", cq)
            _q_sync_meta = {"title": title, "at": now}
            if now - _q_periodic_at > 600:  # 兜底周期完整刷新：抓客户端里手动增删的歌
                _q_periodic_at = now
                schedule_client_queue_sync()
            return
    # 客户端正在吃点唱机搜索残留列表：读出来也是残留，直接跳过
    if getattr(player, "is_recent_jukebox_search_title", None) and \
            player.is_recent_jukebox_search_title(title):
        return
    if _q_sync_meta["title"] == title and now - _q_sync_meta["at"] < 300:
        return  # 这首歌已完整同步过
    _q_sync_meta = {"title": title, "at": now}
    _q_periodic_at = now
    schedule_client_queue_sync()


async def sync_client_queue_now():
    """手动刷新入口（API）：立即读一次客户端队列。切歌期间返回 None。"""
    if state["switching"]:
        return None
    await _do_sync_client_queue()
    return state["clientQueue"]


async def poll_now_playing():
    """轮询 SMTC 正在播放，驱动切歌。

    - 提前切换（主路径）：队列有下一首时，旧歌进入最后 SWITCH_LEAD_SEC 秒就
      finish_current + kick，把 fcg 搜索 + UIA 自动操作的耗时铺进旧歌尾巴，
      客户端无缝换曲（不暂停）。
    - 标题变化（兜底）：客户端已自己跳歌（队列空时播完连播、不可播曲目被跳过）→
      先暂停客户端刹住内部列表，再 finish_current。
    - 同名重播（兜底）：标题没变但进度从 >30s 回退到 <10s——单曲循环播完重播、
      或客户端跳到同名另一版本（如试听版→完整版），标题不变，连播检测抓不到。
    """
    global _last_seen_title, _mismatch_polls, _tracked_cur_started, _last_pos, _restart_suspect
    while True:
        try:
            np = await player.now_playing()
            state["nowPlaying"] = np
            _update_keepalive(np)
            maybe_sync_client_queue(np)
            if np:
                key = np["title"]
                cur = state["current"]
                # 换歌了：重置进度基线，避免拿上一首的进度误判新歌「回退」
                if cur and cur.get("startedAt") != _tracked_cur_started:
                    _tracked_cur_started = cur.get("startedAt")
                    _last_pos = -1
                    _restart_suspect = False
                if cur and key and key != cur["title"]:
                    _mismatch_polls += 1
                    if _last_seen_title == cur["title"] or _mismatch_polls >= 3:
                        await _pause_client_after_end()  # 先刹住客户端内部列表的自动连播
                        await finish_current()  # player bar moved on by itself
                        _mismatch_polls = 0
                else:
                    _mismatch_polls = 0
                    pos = np.get("positionSec") or 0
                    if cur and key == cur["title"]:
                        dur = np.get("durationSec") or 0
                        # 提前切换：旧歌只剩 lead 秒且队列有下一首 → 立即进入切歌流程。
                        # 不暂停客户端：旧歌把尾巴放完，「播放全部」点击时客户端直接换曲。
                        if (np.get("playing") is True and dur > SWITCH_LEAD_SEC
                                and state["queue"] and not state["switching"]
                                and 0 < dur - pos <= SWITCH_LEAD_SEC):
                            await finish_current()
                        elif _last_pos > 30 and pos < 10:
                            _restart_suspect = True
                        elif _restart_suspect:
                            # 上一轮疑似重播，本轮同名仍在播：确认重播，按播完处理
                            _restart_suspect = False
                            await _pause_client_after_end()
                            await finish_current()
                        else:
                            _restart_suspect = False
                        _last_pos = pos
                _last_seen_title = key
                broadcast("nowplaying", np)
        except Exception as e:
            pass
        await asyncio.sleep(2)


# ---------------- play windows ----------------
def refresh_windows():
    now = dt.datetime.now()
    day = str(now.weekday() + 1 if now.weekday() < 6 else 0)  # 0=Sun
    # python weekday(): Mon=0..Sun=6 → our storage uses 0=Sun,1=Mon..6=Sat
    day = str((now.weekday() + 1) % 7)
    minutes = now.hour * 60 + now.minute
    conn = get_db()
    rows = conn.execute("SELECT * FROM play_windows WHERE enabled=1").fetchall()
    # 未配置任何启用时段 = 全天可播（与设置页「暂无时段限制（全天可播）」文案一致）；
    # 全新部署没有 data/jukebox.db，若按禁播处理会导致点歌全部 409。
    inside = not rows
    for w in rows:
        if day not in [d.strip() for d in w["days"].split(",")]:
            continue
        sh, sm = map(int, w["start_time"].split(":"))
        eh, em = map(int, w["end_time"].split(":"))
        s, e = sh * 60 + sm, eh * 60 + em
        if s <= e:
            inside = inside or (s <= minutes < e)
        else:
            inside = inside or (minutes >= s or minutes < e)  # overnight
    changed = inside != state["allowPlay"]
    state["allowPlay"] = inside
    if changed:
        broadcast("window:changed", {"allowPlay": inside})
        if inside:
            kick()
        elif state["current"] and not state["paused"]:
            asyncio.run_coroutine_threadsafe(toggle_pause(), _loop)


def window_loop():
    import time as _t
    while True:
        try:
            refresh_windows()
        except Exception as e:
            print("[windows]", e)
        _t.sleep(30)
