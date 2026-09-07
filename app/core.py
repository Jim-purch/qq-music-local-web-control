"""Jukebox core: shared queue, play loop, history, play windows, WebSocket broadcast."""
import asyncio
import datetime as dt
import json
import threading

from . import player
from .db import get_db, init_db

state = {
    "queue": [],          # [{song_mid, title, singer, addedBy: {id, name}}]
    "current": None,      # {...item, requestedBy, startedAt, historyId}
    "paused": False,
    "allowPlay": True,
    "nowPlaying": None,
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
def enqueue(song: dict, user: dict) -> dict:
    item = {
        "songMid": song.get("songMid", ""),
        "title": song["title"],
        "singer": song.get("singer", ""),
        "addedBy": {"id": user["id"], "name": user["name"]},
    }
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


def kick():
    """Trigger the async play loop from any thread."""
    if _loop is None:
        return
    asyncio.run_coroutine_threadsafe(_loop_once(), _loop)


async def _loop_once():
    try:
        if state["current"] is None and state["queue"] and state["allowPlay"]:
            item = state["queue"].pop(0)
            await start_play(item, item["addedBy"])
    except Exception as e:
        print("[loop]", e)


async def start_play(item: dict, requested_by: dict):
    info = await player.search_and_play(f"{item['title']} {item.get('singer','')}".strip())
    started_at = dt.datetime.now().isoformat(timespec="seconds")
    conn = get_db()
    cur = conn.execute(
        "INSERT INTO play_history (song_mid, title, singer, started_at, requested_by) "
        "VALUES (?, ?, ?, ?, ?)",
        (item.get("songMid", ""), item["title"], item.get("singer", ""), started_at, requested_by["id"]),
    )
    conn.commit()
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
    broadcast("play:ended", {"title": cur["title"], "skippedBy": skipped_by})
    kick()


async def next_song(user=None):
    if state["current"]:
        await finish_current(skipped_by={"id": user["id"], "name": user["name"]} if user else None)
    kick()
    await _loop_once()


async def toggle_pause():
    await player.control("playPause")
    state["paused"] = not state["paused"]
    broadcast("play:paused", {"paused": state["paused"]})


async def prev_song(user=None):
    await player.control("prev")
    broadcast("play:prev", {"by": user["name"] if user else None})


# ---------------- now-playing poller: detects natural song end ----------------
_last_seen_title = ""


async def poll_now_playing():
    global _last_seen_title
    while True:
        try:
            np = await player.now_playing()
            state["nowPlaying"] = np
            if np:
                key = np["title"]
                cur = state["current"]
                if (cur and key and key != cur["title"] and _last_seen_title == cur["title"]):
                    await finish_current()  # player bar moved on by itself
                _last_seen_title = key
                broadcast("nowplaying", np)
        except Exception:
            pass
        await asyncio.sleep(3)


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
