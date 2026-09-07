"""FastAPI server: HTTP API + WebSocket broadcast + static frontend."""
import asyncio
import json
import os
import secrets
import socket
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect, HTTPException, Depends
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import player, core
from .db import get_db, init_db, rows_to_dicts

STATIC_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "static")

# sessions in-memory; cookie token -> {"id", "name"}
SESSIONS = {}
TOKEN_TTL = 365 * 24 * 3600


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    core.set_loop(asyncio.get_running_loop())
    asyncio.create_task(core.poll_now_playing())
    loop = asyncio.get_event_loop()  # window checker in a thread
    import threading
    threading.Thread(target=core.window_loop, daemon=True).start()
    yield
    await player.stop()


app = FastAPI(title="客厅点唱机", lifespan=lifespan)


# ---------------- session helpers ----------------
def parse_cookies(request: Request) -> dict:
    out = {}
    for part in request.headers.get("cookie", "").split(";"):
        part = part.strip()
        if "=" in part:
            k, v = part.split("=", 1)
            out[k] = v
    return out


def current_user(request: Request) -> Optional[dict]:
    token = parse_cookies(request).get("juke_uid")
    return SESSIONS.get(token)


def require_user(request: Request) -> dict:
    u = current_user(request)
    if not u:
        raise HTTPException(401, "请先在设置里输入昵称")
    return u


# ---------------- auth ----------------
class RegisterBody(BaseModel):
    name: str
    pin: str = ""


@app.post("/api/register")
def register(body: RegisterBody, response: JSONResponse):
    name = body.name.strip()
    pin = body.pin.strip()
    if not name or len(name) > 20:
        raise HTTPException(400, "昵称需为 1-20 个字符")
    conn = get_db()
    row = conn.execute("SELECT * FROM users WHERE name=?", (name,)).fetchone()
    if not row:
        cur = conn.execute("INSERT INTO users (name, pin) VALUES (?, ?)", (name, pin or None))
        conn.commit()
        row = conn.execute("SELECT * FROM users WHERE id=?", (cur.lastrowid,)).fetchone()
    elif row["pin"] and row["pin"] != pin:
        raise HTTPException(403, "这个昵称已被使用，需要口令")
    token = secrets.token_hex(24)
    SESSIONS[token] = {"id": row["id"], "name": row["name"]}
    response.set_cookie("juke_uid", token, max_age=TOKEN_TTL, samesite="lax")
    return {"user": SESSIONS[token]}


@app.get("/api/me")
def me(request: Request):
    return {"user": current_user(request)}


@app.post("/api/logout")
def logout(request: Request, response: JSONResponse):
    token = parse_cookies(request).get("juke_uid")
    if token:
        SESSIONS.pop(token, None)
    response.delete_cookie("juke_uid")
    return {"ok": True}


# ---------------- player ----------------
@app.post("/api/player/start")
async def player_start(request: Request):
    require_user(request)
    await player.start()
    return {"ok": True, "loggedIn": await player.is_logged_in()}


@app.get("/api/player/status")
async def player_status():
    running = player.is_running()
    return {"running": running, "loggedIn": await player.is_logged_in() if running else False}


@app.post("/api/player/login")
async def player_login(request: Request):
    require_user(request)
    await player.open_login_page()
    return {"ok": True}


# ---------------- jukebox state & queue ----------------
@app.get("/api/state")
def get_state():
    return core.snapshot()


class QueueAddBody(BaseModel):
    title: str
    singer: str = ""
    songMid: str = ""


@app.post("/api/queue/add")
async def queue_add(body: QueueAddBody, request: Request):
    user = require_user(request)
    if not body.title.strip():
        raise HTTPException(400, "缺少歌名")
    if not player.is_running():
        raise HTTPException(409, "播放器未启动，请先点击「启动播放器」")
    if not core.state["allowPlay"]:
        raise HTTPException(409, "当前不在允许播放时段")
    item = core.enqueue(body.model_dump(), user)
    return {"item": item}


@app.post("/api/queue/remove")
def queue_remove(request: Request, index: int = 0):
    user = require_user(request)
    return {"removed": core.remove_at(index, user)}


@app.post("/api/queue/reorder")
def queue_reorder(request: Request, start: int = 0, stop: int = 0):
    require_user(request)
    core.reorder(start, stop)
    return core.snapshot()


@app.post("/api/control/{action}")
async def control_action(action: str, request: Request):
    user = require_user(request)
    if action == "next":
        await core.next_song(user)
    elif action == "prev":
        await core.prev_song(user)
    elif action == "pause":
        await core.toggle_pause()
    else:
        raise HTTPException(400, "unknown action")
    return core.snapshot()


# ---------------- volume ----------------
@app.get("/api/volume")
def get_volume():
    return {"volume": player.get_system_volume()}


@app.post("/api/volume")
def set_volume(request: Request, volume: int):
    require_user(request)
    return {"volume": player.set_system_volume(volume)}


# ---------------- playlists ----------------
@app.get("/api/playlists")
def list_playlists():
    conn = get_db()
    rows = conn.execute(
        "SELECT p.*, u.name AS creator, "
        "(SELECT COUNT(*) FROM playlist_songs ps WHERE ps.playlist_id=p.id) AS songCount "
        "FROM playlists p LEFT JOIN users u ON u.id=p.created_by ORDER BY p.id DESC"
    ).fetchall()
    return {"playlists": rows_to_dicts(rows)}


class PlaylistBody(BaseModel):
    name: str
    description: str = ""


@app.post("/api/playlists")
def create_playlist(body: PlaylistBody, request: Request):
    user = require_user(request)
    name = body.name.strip()
    if not name:
        raise HTTPException(400, "歌单名不能为空")
    conn = get_db()
    cur = conn.execute(
        "INSERT INTO playlists (name, description, created_by) VALUES (?, ?, ?)",
        (name, body.description, user["id"]))
    conn.commit()
    return {"id": cur.lastrowid}


@app.get("/api/playlists/{pid}")
def playlist_detail(pid: int):
    conn = get_db()
    rows = conn.execute(
        "SELECT ps.*, u.name AS adder FROM playlist_songs ps "
        "LEFT JOIN users u ON u.id=ps.added_by WHERE ps.playlist_id=? ORDER BY ps.position",
        (pid,)).fetchall()
    return {"songs": rows_to_dicts(rows)}


@app.post("/api/playlists/{pid}/songs")
def playlist_add_song(pid: int, body: QueueAddBody, request: Request):
    user = require_user(request)
    if not body.title.strip():
        raise HTTPException(400, "缺少歌名")
    conn = get_db()
    conn.execute(
        "INSERT INTO playlist_songs (playlist_id, position, song_mid, title, singer, added_by) "
        "VALUES (?, (SELECT COALESCE(MAX(position),0)+1 FROM playlist_songs WHERE playlist_id=?), "
        "?, ?, ?, ?)",
        (pid, pid, body.songMid, body.title, body.singer, user["id"]))
    conn.commit()
    return {"ok": True}


@app.post("/api/playlists/{pid}/reorder")
def playlist_reorder(pid: int, request: Request, start: int = 0, stop: int = 0):
    require_user(request)
    conn = get_db()
    rows = conn.execute(
        "SELECT id FROM playlist_songs WHERE playlist_id=? ORDER BY position", (pid,)).fetchall()
    ids = [r["id"] for r in rows]
    if 0 <= start < len(ids):
        x = ids.pop(start)
        stop = max(0, min(len(ids), stop))
        ids.insert(stop, x)
        for i, sid in enumerate(ids):
            conn.execute("UPDATE playlist_songs SET position=? WHERE id=?", (i + 1, sid))
        conn.commit()
    return {"ok": True}


@app.post("/api/playlists/{pid}/songs/{sid}/delete")
def playlist_del_song(pid: int, sid: int, request: Request):
    require_user(request)
    conn = get_db()
    conn.execute("DELETE FROM playlist_songs WHERE id=? AND playlist_id=?", (sid, pid))
    conn.commit()
    return {"ok": True}


@app.post("/api/playlists/{pid}/play")
def playlist_play(pid: int, request: Request):
    user = require_user(request)
    conn = get_db()
    rows = conn.execute(
        "SELECT song_mid, title, singer FROM playlist_songs WHERE playlist_id=? ORDER BY position",
        (pid,)).fetchall()
    for r in rows:
        core.enqueue({"songMid": r["song_mid"], "title": r["title"], "singer": r["singer"]}, user)
    return core.snapshot()


# ---------------- history & stats ----------------
@app.get("/api/history")
def history(limit: int = 50):
    limit = max(1, min(200, limit))
    conn = get_db()
    rows = conn.execute(
        "SELECT h.*, ru.name AS requestedName, su.name AS skippedName FROM play_history h "
        "LEFT JOIN users ru ON ru.id=h.requested_by "
        "LEFT JOIN users su ON su.id=h.skipped_by "
        "ORDER BY h.started_at DESC LIMIT ?", (limit,)).fetchall()
    return {"history": rows_to_dicts(rows)}


_PERIOD_SQL = {
    "day": "started_at >= datetime('now','localtime','-1 day')",
    "week": "started_at >= datetime('now','localtime','-7 days')",
    "month": "started_at >= datetime('now','localtime','-30 days')",
}


@app.get("/api/stats")
def stats(period: str = "day"):
    cond = _PERIOD_SQL.get(period, _PERIOD_SQL["day"])
    conn = get_db()
    by_user = conn.execute(
        "SELECT COALESCE(ru.name,'未知') AS name, COUNT(*) AS plays, SUM(h.played_sec) AS seconds "
        "FROM play_history h LEFT JOIN users ru ON ru.id=h.requested_by "
        f"WHERE {cond} GROUP BY ru.id ORDER BY plays DESC").fetchall()
    by_hour = conn.execute(
        f"SELECT CAST(strftime('%H', started_at) AS INTEGER) AS hour, COUNT(*) AS plays "
        f"FROM play_history WHERE {cond} GROUP BY hour").fetchall()
    top = conn.execute(
        f"SELECT title, singer, COUNT(*) AS plays, SUM(played_sec) AS seconds "
        f"FROM play_history WHERE {cond} GROUP BY song_mid ORDER BY plays DESC LIMIT 10").fetchall()
    total = conn.execute(
        f"SELECT COUNT(*) AS plays, COALESCE(SUM(played_sec),0) AS seconds "
        f"FROM play_history WHERE {cond}").fetchone()
    return {"byUser": rows_to_dicts(by_user), "byHour": rows_to_dicts(by_hour),
            "topSongs": rows_to_dicts(top), "total": dict(total)}


# ---------------- play windows ----------------
@app.get("/api/windows")
def list_windows():
    conn = get_db()
    return {"windows": rows_to_dicts(conn.execute("SELECT * FROM play_windows ORDER BY id").fetchall())}


class WindowBody(BaseModel):
    name: str
    days: str
    startTime: str
    endTime: str


@app.post("/api/windows")
def create_window(body: WindowBody, request: Request):
    require_user(request)
    if not body.name or not body.days or not body.startTime or not body.endTime:
        raise HTTPException(400, "字段不完整")
    conn = get_db()
    conn.execute("INSERT INTO play_windows (name, days, start_time, end_time) VALUES (?, ?, ?, ?)",
                 (body.name, body.days, body.startTime, body.endTime))
    conn.commit()
    core.refresh_windows()
    return {"ok": True}


@app.post("/api/windows/{wid}/toggle")
def toggle_window(wid: int, request: Request):
    require_user(request)
    conn = get_db()
    conn.execute("UPDATE play_windows SET enabled = 1 - enabled WHERE id=?", (wid,))
    conn.commit()
    core.refresh_windows()
    return {"ok": True}


@app.post("/api/windows/{wid}/delete")
def delete_window(wid: int, request: Request):
    require_user(request)
    conn = get_db()
    conn.execute("DELETE FROM play_windows WHERE id=?", (wid,))
    conn.commit()
    core.refresh_windows()
    return {"ok": True}


# ---------------- websocket ----------------
@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    core._clients.add(ws)
    try:
        await ws.send_text(json.dumps({"event": "init", "state": core.snapshot()}, ensure_ascii=False))
        while True:
            await ws.receive_text()  # keepalive; ignore client messages
    except WebSocketDisconnect:
        pass
    finally:
        core._clients.discard(ws)


app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")


def _lan_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", "4680"))
    print("\n🎸 客厅点唱机已启动")
    print("   本机:   http://localhost:%d" % port)
    print("   局域网: http://%s:%d\n" % (_lan_ip(), port))
    uvicorn.run(app, host="0.0.0.0", port=port)
