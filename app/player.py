"""QQ Music web player automation via Playwright (persistent Chromium profile).

Works on Windows and macOS. The browser profile in ./data/chrome-profile keeps
the QQ Music login cookies; users scan the QR code once.
"""
import asyncio
import json
import os
import sys
import time
import urllib.parse
from typing import Optional

from playwright.async_api import async_playwright, BrowserContext, Page

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROFILE_DIR = os.path.join(BASE_DIR, "data", "chrome-profile")
SELECTORS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "selectors.json")
START_URL = "https://y.qq.com"

with open(SELECTORS_PATH, encoding="utf-8") as f:
    SELECTORS = json.load(f)

_pw = None
_browser: Optional[BrowserContext] = None
_page: Optional[Page] = None
_browser_closed = True  # launch_persistent_context returns a BrowserContext, which has no is_connected()
_lock = asyncio.Lock()


def _on_browser_closed(*_):
    global _browser_closed
    _browser_closed = True


def _connected() -> bool:
    return bool(_browser and not _browser_closed)


async def _first_locator(names: str):
    for sel in SELECTORS[names]:
        loc = _page.locator(sel).first
        try:
            if await loc.count() and await loc.is_visible():
                return loc
        except Exception:
            continue
    return None


async def start():
    """Launch a persistent, visible Chromium and open y.qq.com."""
    global _pw, _browser, _page, _browser_closed
    async with _lock:
        if _connected():
            return
        if _pw:  # stale playwright from a closed browser
            try:
                await _pw.stop()
            except Exception:
                pass
        _pw = await async_playwright().start()
        # Prefer installed Chrome/Edge on Windows; fall back to bundled Chromium.
        launch_kwargs = dict(
            user_data_dir=PROFILE_DIR,
            headless=False,
            viewport={"width": 1280, "height": 900},
            args=["--autoplay-policy=no-user-gesture-required", "--no-first-run"],
        )
        for channel in ("chrome", "msedge"):
            try:
                _browser = await _pw.chromium.launch_persistent_context(channel=channel, **launch_kwargs)
                break
            except Exception:
                continue
        if not _browser:
            _browser = await _pw.chromium.launch_persistent_context(**launch_kwargs)
        _browser_closed = False
        _browser.on("close", _on_browser_closed)
        _page = _browser.pages[0] if _browser.pages else await _browser.new_page()
        try:
            await _page.goto(START_URL, wait_until="domcontentloaded", timeout=60000)
        except Exception:
            pass  # offline start is fine; poller will retry


def is_running() -> bool:
    return _connected()


async def _ensure_page():
    """Make sure the browser is up and _page points to a live page.

    Handles: browser closed by user (relaunch), tab closed (re-pick page).
    """
    global _page
    if not _connected():
        await start()
    if _browser and (_page is None or _page.is_closed()):
        _page = _browser.pages[0] if _browser.pages else await _browser.new_page()


def _page_alive() -> bool:
    return bool(_page and not _page.is_closed())


async def open_login_page():
    await _ensure_page()
    try:
        await _page.goto("https://y.qq.com/portal/profile.html", wait_until="domcontentloaded", timeout=60000)
    except Exception:
        pass
    try:
        await _page.bring_to_front()
    except Exception:
        pass


async def is_logged_in() -> bool:
    # Cookie check is far more reliable than DOM selectors on y.qq.com.
    if _connected() and _browser:
        try:
            cookies = await _browser.cookies("https://y.qq.com")
            for c in cookies:
                if c["name"] in ("uin", "qqmusic_uin") and any(ch.isdigit() for ch in c["value"]):
                    return True
        except Exception:
            pass
    if not _page_alive():
        return False
    for sel in SELECTORS["loginAvatar"]:
        try:
            loc = _page.locator(sel).first
            if await loc.count():
                return True
        except Exception:
            continue
    return False


async def search_and_play(keyword: str, index: int = 0) -> dict:
    """Search on y.qq.com and click the Nth song. Returns {title, singer}."""
    await _ensure_page()
    try:
        await _page.bring_to_front()
    except Exception:
        pass
    url = "https://y.qq.com/n/ryqq/search?w=%s&t=song" % urllib.parse.quote(keyword)
    try:
        await _page.goto(url, wait_until="domcontentloaded", timeout=60000)
    except Exception:
        pass

    # wait for the result list to appear
    deadline = time.time() + 20
    items = []
    while time.time() < deadline:
        for sel in SELECTORS["searchResultSongItem"]:
            try:
                items = await _page.locator(sel).all()
            except Exception:
                items = []
            if items:
                break
        if items:
            break
        await asyncio.sleep(0.5)
    if not items:
        raise RuntimeError("搜索结果未出现，请检查网络或更新 app/selectors.json")

    if index >= len(items):
        raise RuntimeError("搜索结果不足（第 %d 首不存在）" % (index + 1))
    item = items[index]

    async def _txt(names):
        for sel in SELECTORS[names]:
            try:
                loc = item.locator(sel).first
                if await loc.count():
                    t = (await loc.inner_text() or "").strip()
                    if t:
                        return t
            except Exception:
                continue
        return ""

    title = await _txt("songItemTitle") or keyword
    singer = await _txt("songItemSinger")

    try:
        await item.locator("a").first.click(timeout=5000)
    except Exception:
        await item.click()
    await asyncio.sleep(1.5)
    try:
        btn = await _first_locator("btnPlayPause")
        if btn:
            await btn.click(timeout=3000)
            await asyncio.sleep(0.3)
    except Exception:
        pass
    return {"title": title, "singer": singer}


async def control(action: str):
    """action in playPause / next / prev."""
    await _ensure_page()
    try:
        await _page.bring_to_front()
    except Exception:
        pass
    btn = await _first_locator("btnPlayPause" if action == "playPause" else
                               "btnNext" if action == "next" else "btnPrev")
    if not btn:
        raise RuntimeError("找不到播放器按钮：%s（selectors.json 需更新）" % action)
    await btn.click(timeout=5000)
    await asyncio.sleep(0.3)


async def now_playing() -> Optional[dict]:
    if not _page_alive():
        return None
    for sel in SELECTORS["playBarSongName"]:
        try:
            loc = _page.locator(sel).first
            if await loc.count():
                title = (await loc.inner_text() or "").strip()
                if title:
                    singer = ""
                    for s2 in SELECTORS["playBarSinger"]:
                        try:
                            l2 = _page.locator(s2).first
                            if await l2.count():
                                singer = (await l2.inner_text() or "").strip()
                                break
                        except Exception:
                            continue
                    return {"title": title, "singer": singer}
        except Exception:
            continue
    return None


async def stop():
    global _browser, _pw, _browser_closed
    _browser_closed = True
    if _browser:
        try:
            await _browser.close()
        except Exception:
            pass
    if _pw:
        try:
            await _pw.stop()
        except Exception:
            pass
    _browser = None
    _pw = None


# ---------------- system volume ----------------
# Windows: pycaw (Core Audio). macOS: osascript. Added lazily so missing
# optional deps don't break other features.
def _win_volume():
    from ctypes import cast, POINTER
    from comtypes import CLSCTX_ALL
    from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume
    devices = AudioUtilities.GetSpeakers()
    interface = devices.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
    return cast(interface, POINTER(IAudioEndpointVolume))


def get_system_volume() -> int:
    try:
        if sys.platform == "win32":
            vol = _win_volume()
            return round(vol.GetMasterVolumeLevelScalar() * 100)
        import subprocess
        out = subprocess.check_output(
            ["osascript", "-e", "output volume of (get volume settings)"], text=True)
        return int(out.strip())
    except Exception as e:
        print("[volume get]", e)
        return -1


def set_system_volume(value: int) -> int:
    value = max(0, min(100, round(value)))
    try:
        if sys.platform == "win32":
            _win_volume().SetMasterVolumeLevelScalar(value / 100.0, None)
        else:
            import subprocess
            subprocess.run(["osascript", "-e", f"set volume output volume {value}"], check=True)
    except Exception as e:
        print("[volume set]", e)
    return value
