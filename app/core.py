"""Jukebox core: shared queue, play loop, history, play windows, WebSocket broadcast."""
import asyncio
import datetime as dt
import json
import threading

from .player_select import player
from .db import get_db, init_db

state = {
    "queue": [],          # [{song_mid, title, singer, addedBy: {id, name}}]
    "current": None,      # {...item, requestedBy, startedAt, historyId}
    "paused": False,
    "allowPlay": True,
    "nowPlaying": None,
    "switching": None,    # 正在切歌的目标歌：{"title", "singer", "requestedBy"}
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


async def poll_now_playing():
    """轮询 SMTC 正在播放，检测自然切歌。

    判定「当前歌已结束」：
    - 快速路径：先见过 current 的标题（_last_seen_title == cur.title），之后标题变了；
    - 兜底路径：标题连续 3 次轮询（约 9 秒）都不是 current——覆盖「不可播曲目被客户端
      瞬间跳过」的场景（那种歌永远不会被 _last_seen_title 记到，不兜底队列会永久卡死）。
    """
    global _last_seen_title, _mismatch_polls
    while True:
        try:
            np = await player.now_playing()
            state["nowPlaying"] = np
            if np:
                key = np["title"]
                cur = state["current"]
                if cur and key and key != cur["title"]:
                    _mismatch_polls += 1
                    if _last_seen_title == cur["title"] or _mismatch_polls >= 3:
                        await finish_current()  # player bar moved on by itself
                        _mismatch_polls = 0
                else:
                    _mismatch_polls = 0
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
    inside = False
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
