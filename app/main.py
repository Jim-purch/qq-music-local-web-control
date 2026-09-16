"""FastAPI server: HTTP API + WebSocket broadcast + static frontend."""
import asyncio
import json
import os
import secrets
import socket
import time
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect, HTTPException, Depends
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .player_select import player
from . import core, keepalive
from .db import get_db, init_db, rows_to_dicts

STATIC_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "static")

# sessions in-memory cache; cookie/header token -> {"id", "name"}
SESSIONS = {}
TOKEN_TTL = 365 * 24 * 3600


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    core.set_loop(asyncio.get_running_loop())
    keepalive.start()  # 应用启动即保持蓝牙链路，等播放时由 poller 自动停止
    asyncio.create_task(core.poll_now_playing())
    loop = asyncio.get_event_loop()  # window checker in a thread
    import threading
    threading.Thread(target=core.window_loop, daemon=True).start()
    yield
    keepalive.stop()
    await player.stop()


app = FastAPI(title="客厅点唱机", lifespan=lifespan)

# CORS 与局域网安全策略
from fastapi.middleware.cors import CORSMiddleware

_allowed_origins_env = os.environ.get("ALLOWED_ORIGINS", "").strip()
_cors_origins = [o.strip() for o in _allowed_origins_env.split(",") if o.strip()] if _allowed_origins_env else ["*"]

app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=True if _cors_origins != ["*"] else False,
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)


@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    try:
        response = await call_next(request)
    except HTTPException:
        raise
    except Exception:
        import traceback
        traceback.print_exc()
        return JSONResponse({"detail": "服务器内部错误，请稍后重试"}, status_code=500)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "SAMEORIGIN"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    return response


# ---------------- session helpers ----------------
def parse_cookies(request: Request) -> dict:
    out = {}
    for part in request.headers.get("cookie", "").split(";"):
        part = part.strip()
        if "=" in part:
            k, v = part.split("=", 1)
            out[k] = v
    return out


def get_token_from_request(request: Request) -> Optional[str]:
    # 1. Header: Authorization: Bearer <token>
    auth_header = request.headers.get("authorization", "")
    if auth_header.lower().startswith("bearer "):
        t = auth_header[7:].strip()
        if t:
            return t
    # 2. Header: X-Juke-Token: <token>
    custom_header = request.headers.get("x-juke-token", "").strip()
    if custom_header:
        return custom_header
    # 3. Cookie: juke_uid=<token>
    cookie_token = parse_cookies(request).get("juke_uid", "").strip()
    if cookie_token:
        return cookie_token
    return None


def current_user(request: Request) -> Optional[dict]:
    token = get_token_from_request(request)
    if not token:
        return None
    if token in SESSIONS:
        return SESSIONS[token]
    # Check DB session
    conn = get_db()
    now = int(time.time())
    row = conn.execute(
        "SELECT u.id, u.name, s.expires_at FROM sessions s JOIN users u ON s.user_id = u.id WHERE s.token = ?",
        (token,),
    ).fetchone()
    if row:
        if row["expires_at"] > now:
            user_data = {"id": row["id"], "name": row["name"]}
            SESSIONS[token] = user_data
            return user_data
        else:
            # expired, clean up
            conn.execute("DELETE FROM sessions WHERE token = ?", (token,))
            conn.commit()
    return None


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
    now = int(time.time())
    expires_at = now + TOKEN_TTL
    conn.execute(
        "INSERT OR REPLACE INTO sessions (token, user_id, expires_at) VALUES (?, ?, ?)",
        (token, row["id"], expires_at),
    )
    conn.commit()
    user_info = {"id": row["id"], "name": row["name"]}
    SESSIONS[token] = user_info
    response.set_cookie("juke_uid", token, max_age=TOKEN_TTL, samesite="lax", httponly=True)
    return {"user": user_info, "token": token}


@app.get("/api/me")
def me(request: Request):
    return {"user": current_user(request)}


@app.post("/api/logout")
def logout(request: Request, response: JSONResponse):
    token = get_token_from_request(request)
    if token:
        SESSIONS.pop(token, None)
        conn = get_db()
        conn.execute("DELETE FROM sessions WHERE token = ?", (token,))
        conn.commit()
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


# ---------------- 客户端播放队列（桌面后端：UIA 读客户端「播放队列」面板） ----------------
@app.get("/api/client_queue")
def client_queue_ep():
    return {"supported": getattr(player, "SUPPORTS_QUEUE_READ", False),
            "queue": core.state["clientQueue"]}


@app.post("/api/client_queue/refresh")
async def client_queue_refresh(request: Request):
    require_user(request)
    q = await core.sync_client_queue_now()
    if q is None and not getattr(player, "SUPPORTS_QUEUE_READ", False):
        raise HTTPException(409, "当前播放后端不支持读取客户端播放队列")
    return {"queue": q}


# ---------------- search（点歌/歌单页的搜索选择器共用） ----------------
@app.get("/api/search")
async def search_songs(request: Request, kw: str):
    require_user(request)
    kw = kw.strip()
    if not kw:
        raise HTTPException(400, "缺少关键词")
    from .music_search import fcg_search
    songs = await asyncio.to_thread(fcg_search, kw)
    return {"songs": songs}


_rec_cache = {"playlists": [], "at": 0.0}


@app.get("/api/recommendations")
async def get_recommendations(limit: int = 12):
    limit = max(1, min(30, limit))
    from .music_search import get_recommended_playlists
    # QQ 上游会在两个内容状态间轮换（偶发只回 1 个）：
    # 拿到完整列表后缓存 10 分钟，翻转期用缓存兜底，避免前端卡片忽多忽少。
    global _rec_cache
    now = time.time()
    if _rec_cache and now - _rec_cache["at"] < 600 and len(_rec_cache["playlists"]) >= limit:
        return {"playlists": _rec_cache["playlists"][:limit]}
    playlists = await asyncio.to_thread(get_recommended_playlists, limit)
    if playlists:
        if len(playlists) >= min(limit, 6) or not _rec_cache["playlists"]:
            _rec_cache = {"playlists": playlists, "at": now}
        else:
            playlists = _rec_cache["playlists"]  # 上游翻转期，沿用旧缓存
    return {"playlists": playlists[:limit]}


@app.get("/api/recommendations/{diss_id}/songs")
async def get_recommendation_songs(diss_id: str, limit: int = 30):
    limit = max(1, min(100, limit))
    from .music_search import get_playlist_songs
    songs = await asyncio.to_thread(get_playlist_songs, diss_id, limit)
    return {"songs": songs}


@app.post("/api/recommendations/{diss_id}/play")
async def play_recommendation(diss_id: str, request: Request, limit: int = 20, front: bool = False):
    user = require_user(request)
    if not player.is_running():
        raise HTTPException(409, "播放器未启动，请先点击「启动播放器」")
    if not core.state["allowPlay"]:
        raise HTTPException(409, "当前不在允许播放时段")
    from .music_search import get_playlist_songs
    songs = await asyncio.to_thread(get_playlist_songs, diss_id, limit)
    if not songs:
        raise HTTPException(404, "该推荐歌单暂无歌曲或获取失败")
    for s in (reversed(songs) if front else songs):
        core.enqueue({"songMid": s.get("songMid", ""), "title": s["title"], "singer": s.get("singer", "")}, user, front=front)
    return core.snapshot()


class RecAddToPlaylistBody(BaseModel):
    playlist_id: Optional[int] = None   # 加入已有歌单
    new_name: str = ""                  # 或新建歌单名
    description: str = ""


@app.post("/api/recommendations/{diss_id}/add_to_playlist")
async def rec_add_to_playlist(diss_id: str, body: RecAddToPlaylistBody, request: Request):
    """把 QQ 音乐整个歌单收藏进本地歌单：playlist_id 指向已有歌单，否则用 new_name 新建。"""
    user = require_user(request)
    from .music_search import get_playlist_songs_all
    # 先抓歌曲再动库：抓不到歌曲时不留空歌单
    songs = await asyncio.to_thread(get_playlist_songs_all, diss_id, 300)
    if not songs:
        raise HTTPException(404, "该歌单暂无歌曲或获取失败")

    conn = get_db()
    if body.playlist_id:
        pl = conn.execute("SELECT id, name FROM playlists WHERE id=?", (body.playlist_id,)).fetchone()
        if not pl:
            raise HTTPException(404, "目标歌单不存在")
        pl_id, pl_name = pl["id"], pl["name"]
    else:
        pl_name = body.new_name.strip()[:40]
        if not pl_name:
            raise HTTPException(400, "请选择已有歌单或填写新歌单名")
        cur = conn.execute(
            "INSERT INTO playlists (name, description, created_by) VALUES (?, ?, ?)",
            (pl_name, body.description, user["id"]))
        pl_id = cur.lastrowid

    # 目标歌单里已有的歌（按 songMid 去重），重复点击收藏不会塞进二份
    existing = {r["song_mid"] for r in
                conn.execute("SELECT song_mid FROM playlist_songs WHERE playlist_id=?", (pl_id,))
                if r["song_mid"]}
    pos = conn.execute(
        "SELECT COALESCE(MAX(position),0) FROM playlist_songs WHERE playlist_id=?", (pl_id,)).fetchone()[0]
    added = 0
    for s in songs:
        mid = (s.get("songMid") or "").strip()
        if mid and mid in existing:
            continue
        pos += 1
        conn.execute(
            "INSERT INTO playlist_songs (playlist_id, position, song_mid, title, singer, added_by) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (pl_id, pos, mid, s.get("title", ""), s.get("singer", ""), user["id"]))
        if mid:
            existing.add(mid)
        added += 1
    conn.commit()
    return {"playlistId": pl_id, "playlistName": pl_name,
            "fetched": len(songs), "added": added, "skipped": len(songs) - added}


# ---------------- 歌单广场（分类浏览 + 分页 + 歌单搜索） ----------------
_cat_cache = {"groups": [], "at": 0.0}


@app.get("/api/playlist_categories")
async def playlist_categories():
    """歌单分类分组（缓存 1 小时，分类几乎不变）。"""
    from .music_search import get_playlist_categories
    global _cat_cache
    now = time.time()
    if _cat_cache["groups"] and now - _cat_cache["at"] < 3600:
        return {"groups": _cat_cache["groups"]}
    groups = await asyncio.to_thread(get_playlist_categories)
    if groups:
        _cat_cache = {"groups": groups, "at": now}
    return {"groups": groups or _cat_cache["groups"]}


@app.get("/api/playlist_square")
async def playlist_square(category: int = 10000000, page: int = 1, per_page: int = 20, sort: int = 5):
    """按分类分页浏览歌单：{"playlists": [...], "total": 总数}。"""
    from .music_search import get_playlists_by_category
    return await asyncio.to_thread(get_playlists_by_category, category, page, per_page, sort)


@app.get("/api/playlist_search")
async def playlist_search_ep(kw: str, page: int = 1, per_page: int = 20):
    """搜索歌单：{"playlists": [...], "has_more": bool}。"""
    kw = kw.strip()
    if not kw:
        raise HTTPException(400, "缺少关键词")
    from .music_search import search_playlists
    return await asyncio.to_thread(search_playlists, kw, page, per_page)


# ---------------- jukebox state & queue ----------------
@app.get("/api/state")
def get_state():
    return core.snapshot()


class QueueAddBody(BaseModel):
    title: str
    singer: str = ""
    songMid: str = ""
    front: bool = False


@app.post("/api/queue/add")
async def queue_add(body: QueueAddBody, request: Request):
    user = require_user(request)
    if not body.title.strip():
        raise HTTPException(400, "缺少歌名")
    if not player.is_running():
        raise HTTPException(409, "播放器未启动，请先点击「启动播放器」")
    if not core.state["allowPlay"]:
        raise HTTPException(409, "当前不在允许播放时段")
    item = core.enqueue(body.model_dump(), user, front=body.front)
    return {"item": item}


# 同步 def 端点跑在线程池，对共享队列的「检查-弹出/插入」非原子；
# async 化后统一在事件循环上串行执行，多人并发删歌/调序不会互相踩。
@app.post("/api/queue/remove")
async def queue_remove(request: Request, index: int = 0):
    user = require_user(request)
    return {"removed": core.remove_at(index, user)}


@app.post("/api/queue/reorder")
async def queue_reorder(request: Request, start: int = 0, stop: int = 0):
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


class VolumeBody(BaseModel):
    volume: int


@app.post("/api/volume")
def set_volume(body: VolumeBody, request: Request):
    require_user(request)
    return {"volume": player.set_system_volume(body.volume)}


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
async def playlist_play(pid: int, request: Request, from_position: int = 0, front: bool = False):
    """把歌单加入队列。from_position=从第几首开始（含），front=插到队首（「从此播」语义）。"""
    user = require_user(request)
    conn = get_db()
    rows = conn.execute(
        "SELECT song_mid, title, singer FROM playlist_songs WHERE playlist_id=? ORDER BY position",
        (pid,)).fetchall()
    if from_position > 0:
        rows = rows[from_position:]
    # front 时逐首插到队首，倒序遍历保持歌单原始顺序
    for r in (reversed(rows) if front else rows):
        core.enqueue({"songMid": r["song_mid"], "title": r["title"], "singer": r["singer"]}, user,
                     front=front)
    return core.snapshot()


class PlaylistImportBody(BaseModel):
    name: str = ""
    description: str = ""
    songs: list[dict] = []


@app.get("/api/playlists/{pid}/export")
def playlist_export(pid: int):
    conn = get_db()
    pl = conn.execute("SELECT * FROM playlists WHERE id=?", (pid,)).fetchone()
    if not pl:
        raise HTTPException(404, "歌单不存在")
    songs = conn.execute(
        "SELECT title, singer, song_mid FROM playlist_songs WHERE playlist_id=? ORDER BY position",
        (pid,)
    ).fetchall()
    return {
        "version": 1,
        "type": "jukebox_playlist",
        "name": pl["name"],
        "description": pl["description"] or "",
        "exportedAt": time.strftime("%Y-%m-%d %H:%M:%S"),
        "songs": [{"title": s["title"], "singer": s["singer"], "songMid": s["song_mid"]} for s in songs]
    }


@app.post("/api/playlists/import")
def playlist_import(body: PlaylistImportBody, request: Request):
    user = require_user(request)
    name = (body.name or "").strip() or f"导入歌单_{time.strftime('%m%d_%H%M')}"
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO playlists (name, description, created_by) VALUES (?, ?, ?)",
        (name, body.description or "", user["id"])
    )
    new_pid = cur.lastrowid
    pos = 1
    for s in body.songs:
        title = (s.get("title") or "").strip()
        if not title:
            continue
        singer = (s.get("singer") or "").strip()
        song_mid = (s.get("songMid") or s.get("song_mid") or "").strip()
        cur.execute(
            "INSERT INTO playlist_songs (playlist_id, song_mid, title, singer, position) VALUES (?, ?, ?, ?, ?)",
            (new_pid, song_mid, title, singer, pos)
        )
        pos += 1
    conn.commit()
    return {"id": new_pid, "name": name, "importedCount": pos - 1}


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
