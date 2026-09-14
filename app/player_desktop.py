"""QQ 音乐 Windows 桌面客户端后端：SMTC 传输控制 + UIA 点歌 + fcg 搜索。

通道分工（docs/desktop-client-plan.md 实测结论）：
  播放/暂停/上一首/下一首/正在播放  → Windows SMTC（系统级，不碰客户端 UI）
  点歌（关键词 → 客户端搜索 → 播放） → pywinauto UIA（客户端原生 UIAutomation 树完整可靠）
  歌曲元数据（songMid/歌名/歌手）    → y.qq.com 公共 fcg 接口（无需登录）
  音量                               → pycaw 系统主音量（沿用 player.py）

接口与 app/player.py 完全一致，通过 app/player_select.py 按环境切换。
"""
import asyncio
import ctypes
import os
import subprocess
import threading
import time
from ctypes import wintypes
from typing import Optional

# ---------------- QQ 音乐客户端进程管理 ----------------
def _install_dir() -> Optional[str]:
    try:
        import winreg
        for path in (r"SOFTWARE\WOW6432Node\Tencent\QQMusic", r"SOFTWARE\Tencent\QQMusic"):
            try:
                k = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, path)
                val, _ = winreg.QueryValueEx(k, "Install")
                if val and os.path.isdir(val):
                    return val
            except OSError:
                continue
    except Exception:
        pass
    for guess in (r"C:\Program Files (x86)\Tencent\QQMusic", r"C:\Program Files\Tencent\QQMusic"):
        if os.path.isfile(os.path.join(guess, "QQMusic.exe")):
            return guess
    return None


# Toolhelp32 进程枚举（免 psutil 依赖）
_TH32CS_SNAPPROCESS = 0x2


class _PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
                ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", ctypes.c_long),
                ("dwFlags", wintypes.DWORD), ("szExeFile", ctypes.c_wchar * 260)]


def _pids_of(name_lower: str) -> list:
    k32 = ctypes.windll.kernel32
    snap = k32.CreateToolhelp32Snapshot(_TH32CS_SNAPPROCESS, 0)
    if snap == -1:
        return []
    out = []
    try:
        e = _PROCESSENTRY32W()
        e.dwSize = ctypes.sizeof(_PROCESSENTRY32W)
        if k32.Process32FirstW(snap, ctypes.byref(e)):
            while True:
                if e.szExeFile.lower() == name_lower:
                    out.append(e.th32ProcessID)
                if not k32.Process32NextW(snap, ctypes.byref(e)):
                    break
    finally:
        k32.CloseHandle(snap)
    return out


def is_running() -> bool:
    return bool(_pids_of("qqmusic.exe"))


async def start():
    """确保客户端在运行（未运行则拉起），窗口就绪。"""
    if is_running():
        return
    install = _install_dir()
    if not install:
        raise RuntimeError("未找到 QQ 音乐客户端（请先安装 Windows 版 QQ 音乐）")
    subprocess.Popen([os.path.join(install, "QQMusic.exe")], cwd=install)
    for _ in range(30):
        if is_running():
            break
        await asyncio.sleep(1)
    # 等主窗口可交互
    for _ in range(15):
        try:
            if _main_window() is not None:
                return
        except Exception:
            pass
        await asyncio.sleep(1)


async def is_logged_in() -> bool:
    # 登录态由客户端自己维护（弹窗扫码）；进程在跑即视为可用。
    return is_running()


async def open_login_page():
    """把客户端窗口带到前台，用户需要时在客户端里登录。"""
    await asyncio.to_thread(_focus_window)


async def stop():
    """暂停播放但不退出客户端（避免误关别人的听歌现场）。"""
    try:
        await _smtc_control("pause")
    except Exception:
        pass


# ---------------- 通道一：SMTC（播放控制 + 正在播放） ----------------
_smtc_mgr = None
_cached_session = None


def _import_smtc():
    from winrt.windows.media.control import (
        GlobalSystemMediaTransportControlsSessionManager as MediaManager)
    return MediaManager


async def _smtc_session():
    global _smtc_mgr, _cached_session
    try:
        if _cached_session is not None:
            # 简单嗅探一下是否依然有效
            src = (_cached_session.source_app_user_model_id or "").lower()
            if "qqmusic" in src or "qq.music" in src or "tencent" in src:
                return _cached_session
    except Exception:
        _cached_session = None

    if _smtc_mgr is None:
        try:
            _smtc_mgr = await _import_smtc().request_async()
        except Exception:
            return None

    try:
        for s in _smtc_mgr.get_sessions():
            src = (s.source_app_user_model_id or "").lower()
            if "qqmusic" in src or "qq.music" in src or "tencent" in src:
                _cached_session = s
                return s
    except Exception:
        _smtc_mgr = None
        _cached_session = None
    return None


async def now_playing() -> Optional[dict]:
    try:
        s = await _smtc_session()
        if s is None:
            return None
        info = await s.try_get_media_properties_async()
        if not info.title:
            return None
        tl = s.get_timeline_properties()
        pb = s.get_playback_info()
        # playback_status: 4=播放 5=暂停 6=停止（Windows.Media.Control 枚举值）
        return {
            "title": info.title,
            "singer": info.artist or "",
            "album": info.album_title or "",
            "playing": int(str(pb.playback_status)) == 4 if pb.playback_status is not None else None,
            "positionSec": int(tl.position.total_seconds()),
            "durationSec": int(tl.end_time.total_seconds()),
        }
    except Exception:
        return None


async def control(action: str):
    """action in playPause / next / prev / play / pause."""
    await _smtc_control(action)


async def _smtc_control(action: str):
    s = await _smtc_session()
    if s is None:
        raise RuntimeError("没有 QQ 音乐的媒体会话——先在客户端里放一首歌")
    table = {
        "play": s.try_play_async,
        "pause": s.try_pause_async,
        "playPause": s.try_toggle_play_pause_async,
        "next": s.try_skip_next_async,
        "prev": s.try_skip_previous_async,
    }
    if action not in table:
        raise ValueError(f"未知命令：{action}")
    await table[action]()


# ---------------- 通道三：fcg 搜索（关键词 → 歌曲元数据） ----------------
from .music_search import fcg_search  # 共享实现，main.py 的 /api/search 也用它


def _pick_song(songs: list, keyword: str, song_mid: str = "") -> dict:
    """优先按 songMid 精确匹配；其次按歌手匹配；兜底取第一首。"""
    if not songs:
        return {}
    if song_mid:
        for s in songs:
            if s.get("songMid") == song_mid:
                return s
    parts = [p for p in keyword.split() if p]
    if len(parts) >= 2:
        singer_kw = parts[-1]
        for s in songs:
            if singer_kw in s.get("singer", ""):
                return s
    return songs[0]


# ---------------- 通道二：UIA 点歌（客户端搜索 → 双击结果行） ----------------
def _main_window():
    from pywinauto import Desktop
    pids = _pids_of("qqmusic.exe")
    # 主窗口标题随播放歌曲变化（如「搁浅 - 周杰伦」），按面积取最大窗口
    best, best_area = None, 0
    for pid in pids:
        try:
            for w in Desktop(backend="uia").windows(process=pid):
                r = w.rectangle()
                area = r.width() * r.height()
                if area > best_area and r.height() > 400:
                    best, best_area = w, area
        except Exception:
            continue
    return best


def _focus_window():
    w = _main_window()
    if w is None:
        raise RuntimeError("找不到 QQ 音乐主窗口")
    try:
        if w.is_minimized():
            w.restore()
        w.set_focus()
    except Exception:
        pass
    return w


def _find_elements(w, predicate, timeout=15, poll=0.8):
    """轮询遍历 UIA 树找满足条件的元素（树大，控制在秒级超时内）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            hits = [e for e in w.descendants() if predicate(e)]
            if hits:
                return hits
        except Exception:
            pass
        time.sleep(poll)
    return []


# ---- 无鼠标、不抢前台的输入：全部走 PostMessage ----
# （真实光标/键盘会被部署环境钳制，且 SendInput 需要前台窗口会打扰用户）
_WND_MSG = ctypes.windll.user32
WM_LBUTTONDOWN, WM_LBUTTONUP, WM_LBUTTONDBLCLK = 0x0201, 0x0202, 0x0203
WM_CHAR, WM_KEYDOWN, WM_KEYUP = 0x0102, 0x0100, 0x0101
VK_BACK, VK_RETURN = 0x08, 0x0D


class _POINT(ctypes.Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]


def _post_click(hwnd, sx, sy):
    pt = _POINT(x=sx, y=sy)
    _WND_MSG.ScreenToClient(hwnd, ctypes.byref(pt))
    lp = ((pt.y & 0xFFFF) << 16) | (pt.x & 0xFFFF)
    for msg in (WM_LBUTTONDOWN, WM_LBUTTONUP):
        _WND_MSG.PostMessageW(hwnd, msg, 1 if msg == WM_LBUTTONDOWN else 0, lp)
        time.sleep(0.05)


def _post_dblclick(hwnd, sx, sy):
    pt = _POINT(x=sx, y=sy)
    _WND_MSG.ScreenToClient(hwnd, ctypes.byref(pt))
    lp = ((pt.y & 0xFFFF) << 16) | (pt.x & 0xFFFF)
    for msg in (WM_LBUTTONDOWN, WM_LBUTTONUP, WM_LBUTTONDBLCLK, WM_LBUTTONUP):
        _WND_MSG.PostMessageW(hwnd, msg, 1 if msg != WM_LBUTTONUP else 0, lp)
        time.sleep(0.05)


def _post_backspaces(hwnd, n=60):
    # 自绘编辑框不认 WM_CHAR(8)，退格要走 WM_KEYDOWN(VK_BACK)
    for _ in range(n):
        _WND_MSG.PostMessageW(hwnd, WM_KEYDOWN, VK_BACK, 0)
        _WND_MSG.PostMessageW(hwnd, WM_KEYUP, VK_BACK, 0)
    time.sleep(0.2)


def _post_type(hwnd, text):
    for ch in text:
        _WND_MSG.PostMessageW(hwnd, WM_CHAR, ord(ch), 0)
        time.sleep(0.04)
    time.sleep(0.2)
    _WND_MSG.PostMessageW(hwnd, WM_KEYDOWN, VK_RETURN, 0)
    _WND_MSG.PostMessageW(hwnd, WM_KEYUP, VK_RETURN, 0)


# UIA 操作串行锁：客户端只有一个搜索框，两套自动化并发必然互相踩踏。
# core 层已有 _play_lock 串行化，这里再挡一层，防止未来新调用路径绕过 core。
_uia_lock = threading.Lock()


def _uia_search_and_click(keyword: str, want: dict):
    """在客户端搜索关键词并双击匹配的结果行（不移动真实光标、不抢前台）。"""
    with _uia_lock:
        _uia_search_and_click_locked(keyword, want)


def _uia_search_and_click_locked(keyword: str, want: dict):
    w = _focus_window()
    hwnd = w.handle

    # 1. 组合最精确的搜索词：歌名 + 歌手（确保搜索结果第一首就是用户选的歌）
    title = want.get("title", "").strip()
    singer = want.get("singer", "").split("/")[0].strip()
    search_text = f"{title} {singer}".strip() if singer else title
    if not search_text:
        search_text = keyword

    # 2. 搜索框（客户端唯一的 Edit）：投递点击聚焦 → 退格清空 → 逐字输入 → 回车
    edits = _find_elements(w, lambda e: e.element_info.control_type == "Edit", timeout=8)
    if not edits:
        raise RuntimeError("找不到客户端搜索框（窗口可能未就绪）")
    er = edits[0].rectangle()
    _post_click(hwnd, (er.left + er.right) // 2, (er.top + er.bottom) // 2)
    time.sleep(0.4)
    _post_backspaces(hwnd)
    _post_type(hwnd, search_text)

    # 3. 等待搜索结果出现（列表上方会出现「找到...首歌曲」或「播放全部」按钮）
    # 查找搜索结果列表顶部的「播放」按钮（通常在 Y=250~450 之间，文字为"播放"）
    time.sleep(1.0)
    play_all_btns = _find_elements(
        w,
        lambda e: e.element_info.name == "播放"
        and e.rectangle().top > 250
        and e.rectangle().top < 450,
        timeout=10,
    )

    if play_all_btns:
        # 点击搜索结果顶部的「播放全部」，客户端将立即开始播放第 1 首匹配歌曲
        pr = play_all_btns[0].rectangle()
        _post_click(hwnd, (pr.left + pr.right) // 2, (pr.top + pr.bottom) // 2)
    else:
        # 备选保底：如果未找到播放按钮，点击第一行左侧播放位置（大约在搜索框下方 300 像素左右）
        wr = w.rectangle()
        _post_click(hwnd, wr.left + 350, wr.top + 360)
        time.sleep(0.5)
        _post_dblclick(hwnd, wr.left + 380, wr.top + 360)


async def search_and_play(target, index: int = 0) -> dict:
    """搜索并播放歌曲。

    target 可以是关键词字符串（如 '人间 王菲'），也可以是包含 title, singer, songMid 的字典。
    """
    if isinstance(target, dict):
        keyword = f"{target.get('title','')} {target.get('singer','')}".strip()
        song_mid = target.get("songMid", "")
    else:
        keyword = str(target).strip()
        song_mid = ""

    songs = await asyncio.to_thread(fcg_search, keyword)
    if not songs:
        if isinstance(target, dict):
            want = {"title": target.get("title", keyword), "singer": target.get("singer", ""), "songMid": song_mid}
        else:
            parts = [p for p in keyword.split() if p]
            title = parts[0] if parts else keyword
            singer = parts[1] if len(parts) >= 2 else ""
            want = {"title": title, "singer": singer, "songMid": ""}
    else:
        want = _pick_song(songs, keyword, song_mid) if index == 0 else (
            songs[index] if index < len(songs) else songs[0])

    await asyncio.to_thread(_uia_search_and_click, keyword, want)
    # 等 SMTC 确认起播（客户端可能自动跳过不可播曲目，poller 会兜底）
    for _ in range(10):
        np = await now_playing()
        if np and np["title"] == want["title"]:
            break
        await asyncio.sleep(1)
    return {"title": want["title"], "singer": want["singer"]}


# ---------------- 系统音量（沿用 pycaw 实现） ----------------
from .player import get_system_volume, set_system_volume  # noqa: E402
