"""QQ 音乐 Windows 桌面客户端控制通道 PoC。

验证 docs/desktop-client-plan.md 中的三条通道，在目标 Windows 机（已装并登录
QQ 音乐客户端）上按顺序跑，全通则桌面客户端方案成立：

  1. SMTC   系统媒体会话读取/控制（需 pip install winsdk，仅 Windows）
  2. 深链   qqmusic:// playSonglist 指定 songmid 直接播放
  3. 搜索   公共 fcg 接口 keyword -> songmid（无需 Key，跨平台可跑）

用法：
  python scripts/win_desktop_poc.py check              # 列出 SMTC 会话与正在播放
  python scripts/win_desktop_poc.py control next       # SMTC 发切歌/播放暂停命令
  python scripts/win_desktop_poc.py search <关键词>     # 搜索并编号列出结果
  python scripts/win_desktop_poc.py deeplink <songmid> # 深链播放指定歌曲
  python scripts/win_desktop_poc.py play <关键词> [序号] # 搜索 + 深链，端到端
"""
import argparse
import asyncio
import json
import sys
import urllib.parse
import urllib.request

SEARCH_URL = "https://u.y.qq.com/cgi-bin/musicu.fcg"
SEARCH_PAYLOAD = {
    "comm": {"ct": 19, "cv": 1859, "uin": 0},
    "request": {
        "method": "DoSearchForQQMusicDesktop",
        "module": "music.search.SearchCgiService",
        "param": {"search_type": 0, "query": "", "page_num": 1, "num_per_page": 10},
    },
}


def deeplink_url(songmid: str) -> str:
    # 协议参数不是标准 JSON：内层是 [["type":"0","songmid":"..."]]，保持社区文档原样
    p = '{"song":[["type":"0","songmid":"%s"]],"action":"play"}' % songmid
    return "qqmusic://qq.com/media/playSonglist?p=" + urllib.parse.quote(p, safe="")


# ---------------- 通道三：公共搜索（跨平台） ----------------
def search(keyword: str) -> list:
    payload = json.loads(json.dumps(SEARCH_PAYLOAD))
    payload["request"]["param"]["query"] = keyword
    req = urllib.request.Request(
        SEARCH_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Referer": "https://y.qq.com/",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        },
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    songs = data["request"]["data"]["body"]["song"]["list"]
    out = []
    for s in songs:
        out.append({
            "mid": s.get("mid") or s.get("file", {}).get("media_mid", ""),
            "title": s.get("name") or s.get("title", ""),
            "singer": "/".join(x.get("name", "") for x in s.get("singer", [])),
        })
    return [s for s in out if s["mid"]]


def cmd_search(args):
    songs = search(args.keyword)
    if not songs:
        print("搜索无结果或接口不可用")
        return 1
    for i, s in enumerate(songs):
        print(f"{i}. {s['title']} - {s['singer']}  mid={s['mid']}")
    return 0


# ---------------- 通道二：qqmusic:// 深链（仅 Windows） ----------------
def cmd_deeplink(args):
    url = deeplink_url(args.songmid)
    print(url)
    if sys.platform != "win32":
        print("深链唤起仅限 Windows（os.startfile），请在 Windows 机上运行")
        return 1
    import os
    os.startfile(url)  # noqa  ShellExecute 处理协议唤起
    print("已唤起，观察客户端是否直接开始播放（这正是要验证的点）")
    return 0


def cmd_play(args):
    songs = search(args.keyword)
    if not songs:
        print("搜索无结果或接口不可用")
        return 1
    for i, s in enumerate(songs):
        print(f"{i}. {s['title']} - {s['singer']}  mid={s['mid']}")
    idx = args.index if args.index is not None else 0
    if idx >= len(songs):
        print(f"序号超范围（共 {len(songs)} 条）")
        return 1
    print(f"选择第 {idx} 首：{songs[idx]['title']} - {songs[idx]['singer']}")
    args.songmid = songs[idx]["mid"]
    return cmd_deeplink(args)


# ---------------- 通道一：SMTC（仅 Windows，需 winsdk） ----------------
def _session_cmd(session, action: str):
    table = {
        "play": session.try_play_async,
        "pause": session.try_pause_async,
        "playPause": session.try_toggle_play_pause_async,
        "next": session.try_skip_next_async,
        "prev": session.try_skip_previous_async,
    }
    if action not in table:
        raise ValueError(f"未知命令：{action}")
    return table[action]()


def _qqmusic_session(manager):
    # 优先 QQ 音乐会话；找不到再退回当前会话，便于单播放器机器测试
    for session in manager.get_sessions():
        src = (session.source_app_user_model_id or "").lower()
        if "qqmusic" in src or "qq.music" in src or "tencent" in src:
            return session, src
    session = manager.get_current_session()
    return session, (session.source_app_user_model_id or "") if session else ""


async def _run_smtc(action: str | None):
    try:
        from winsdk.windows.media.control import (
            GlobalSystemMediaTransportControlsSessionManager as MediaManager)
    except ImportError:
        print("缺少 winsdk：pip install winsdk（仅 Windows）")
        return 1
    manager = await MediaManager.request_async()
    session, src = _qqmusic_session(manager)
    if session is None:
        print("没有活动的媒体会话——先在 QQ 音乐客户端里放一首歌再跑 check")
        return 1
    info = await session.try_get_media_properties_async()
    status = session.get_playback_info().playback_status
    tl = session.get_timeline_properties()
    print(f"会话: {src}")
    print(f"正在播放: {info.title} - {info.artist}  状态: {status}")
    print(f"进度: {tl.position.seconds // 60}:{tl.position.seconds % 60:02d}"
          f" / {tl.end_time.seconds // 60}:{tl.end_time.seconds % 60:02d}")
    if action:
        await _session_cmd(session, action)
        print(f"已发送命令：{action}（观察是否生效）")
    return 0


def cmd_check(_args):
    return asyncio.run(_run_smtc(None))


def cmd_control(args):
    return asyncio.run(_run_smtc(args.action))


def main():
    ap = argparse.ArgumentParser(description="QQ 音乐桌面客户端控制通道 PoC")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check").set_defaults(func=cmd_check)
    p = sub.add_parser("control")
    p.add_argument("action", choices=["play", "pause", "playPause", "next", "prev"])
    p.set_defaults(func=cmd_control)
    p = sub.add_parser("search")
    p.add_argument("keyword")
    p.set_defaults(func=cmd_search)
    p = sub.add_parser("deeplink")
    p.add_argument("songmid")
    p.set_defaults(func=cmd_deeplink)
    p = sub.add_parser("play")
    p.add_argument("keyword")
    p.add_argument("index", type=int, nargs="?", default=None)
    p.set_defaults(func=cmd_play)
    args = ap.parse_args()
    try:
        sys.exit(args.func(args))
    except Exception as e:
        print(f"失败：{e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
