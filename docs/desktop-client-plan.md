# 桌面客户端控制方案调研 · QQ 音乐 Windows 版

> 调研日期：2026-09。目标：把播放内核从「Playwright 驱动 y.qq.com 网页版」换成「局域网网页控制本机 QQ 音乐桌面客户端」，部署形态不变——一台蓝牙连办公室音箱的 Windows 电脑当服务器，同事用局域网网页点歌。

## 结论

**可行，但没有单一官方接口，需要三条通道拼装：**

| 能力 | 通道 | 可靠性 |
|---|---|---|
| 播放/暂停/上一首/下一首 | Windows SMTC 系统媒体会话 | 高（系统级 API，有官方文档，不碰客户端 UI，无需前台窗口） |
| 正在播放（歌名/歌手/进度/状态） | 同上 SMTC 媒体属性 | 高 |
| 点歌（搜索 → 播指定歌曲） | 搜索接口拿 `songMid` → `qqmusic://` 深链唤起客户端 | **中，唯一待实测点**：深链在移动端已验证，PC 客户端注册了 `qqmusic://` 协议，但 `playSonglist` 是否直接起播无文档保证 |
| 音量 | 现有 pycaw（系统主音量，蓝牙音箱走默认输出设备） | 高，代码已有 |
| 登录/VIP | 客户端自行登录一次，长期有效 | 高，不再自己管 cookie |

## 背景：桌面客户端没有公开的本地控制 API

社区项目只有两类：网页 API 代理（Rain120/qq-music-api、jsososo 等，拿歌曲元数据用）和模拟按键类（hahapigs/qq-music-controller 是 macOS Alfred 工作流）。没有成熟的局域网遥控方案，腾讯也没开放客户端本地 RPC。官方渠道只有 [QQ音乐开发者平台](https://developer.y.qq.com/)（偏硬件/SDK）和[移动 WEB 开放平台](https://y.qq.com/m/api/open/index.html)（JS API，`player.play(songMid)`，仅限移动端 webview）。

## 通道一：SMTC（System Media Transport Controls）

新版 QQ 音乐 Windows 客户端接入了 SMTC——按音量键弹出的系统媒体浮窗里能看到它（封面、进度、播放控制）。任何进程都能通过 WinRT 的 `GlobalSystemMediaTransportControlsSessionManager` 枚举媒体会话并控制，Python 用 [winsdk](https://pypi.org/project/winsdk/)：

```python
import asyncio
from winsdk.windows.media.control import (
    GlobalSystemMediaTransportControlsSessionManager as MediaManager)

async def main():
    manager = await MediaManager.request_async()
    for session in manager.get_sessions():
        info = await session.try_get_media_properties_async()
        print(session.source_app_user_model_id, info.title, info.artist)

asyncio.run(main())
```

控制方法（session 对象上）：`try_play_async()` / `try_pause_async()` / `try_toggle_play_pause_async()` / `try_skip_next_async()` / `try_skip_previous_async()`。状态读取：`get_playback_info()`（播放/暂停态）、`get_timeline_properties()`（进度/时长，用于判断一首歌播完）。

要点：

- 通过 `source_app_user_model_id` 匹配 QQ 音乐的会话（多会话机器上别控错别的播放器）
- SMTC 只有较新版本客户端支持，需实测；旧版拿不到会话
- 全部异步调用，FastAPI 的 asyncio 环境直接可用
- 不需要窗口前台，部署机有人在用也不受影响

## 通道二：qqmusic:// 深链（点歌核心）

社区逆向整理的协议（移动端验证可用，PC 端需实测）：

```
qqmusic://qq.com/media/playSonglist?p={"song":[["type":"0","songmid":"0039MnYb0qxYhV"]],"action":"play"}
```

- `songmid`：歌曲 mid，从搜索接口或 y.qq.com 歌曲页 URL 取得
- `action: "play"` 立即播放；可传多首
- Windows 客户端注册了 `qqmusic://` 协议（y.qq.com 上「打开客户端」按钮即靠它唤起），`os.startfile(url)` 或 `subprocess` 调 `start` 即可触发
- **关键风险**：PC 版对 `playSonglist` 带参起播的支持没有文档保证，客户端大版本更新后也可能变。这是整个方案唯一必须先实测的点
- `playSonglist` 语义大概是**替换播放**，不是追加。正好适配我们的模型：服务器持有权威队列，一次推一首

## 通道三：搜索接口（keyword → songMid）

两个现成来源，按需选用：

1. **官方开放平台**（本仓库作者环境里有 QQ 音乐 skill 用的就是它）：`POST https://a.y.qq.com/discover/search`，Bearer 鉴权（qmk- Key，y.qq.com/n/ryqq_v2/qqmusic_skills 申请），返回 `songMid/songName/singerName`。有 QPS 限制，点歌场景够用
2. **公共 fcg 接口**（y.qq.com 前端自己在用，无需 Key/登录）：`POST https://u.y.qq.com/cgi-bin/musicu.fcg`，`music.search.SearchCgiService / DoSearchForQQMusicDesktop`。PoC 脚本里实现了这个

## 与现有代码的整合

`core.py` / `main.py` 只依赖 `player.py` 的窄接口，**换后端是局部改动**，队列/统计/时段控制/前端/WebSocket/数据库全部不动：

| player.py 接口 | 桌面客户端实现 |
|---|---|
| `start()` | 拉起 QQMusic.exe（`os.startfile` / `subprocess`） |
| `is_running()` | 进程存在 + SMTC 会话存在 |
| `is_logged_in()` | SMTC 能取到媒体属性即视为在用（登录态归客户端管，可放宽为恒 True + 人工确认） |
| `search_and_play(keyword)` | fcg/开放平台搜索 → 深链播放，返回 `{title, singer}` |
| `control(playPause/next/prev)` | SMTC `try_toggle_play_pause_async` / `try_skip_next_async` / `try_skip_previous_async` |
| `now_playing()` | SMTC `try_get_media_properties_async` |
| `stop()` | 暂停即可（不杀客户端，避免误关别人的听歌现场） |
| 音量 get/set | pycaw 保留 |

队列推进逻辑：`core.py` 的播放循环目前轮询 `now_playing()` 判切歌；换成「SMTC 轮询 timeline/状态，发现一首结束（状态变 paused 且进度≈时长，或曲目变化）→ 推下一首深链」。深链播放失败（版权/下架）需要兜底：跳过并提示点歌人。

## 换方案的价值与代价

收益：

- **音质**：桌面端到无损/Hi-Res，网页端受限（这是换的最大动机）
- **VIP 曲库**：客户端完整，网页版有曲目范围限制
- **摆脱 selectors.json**：不用再追 y.qq.com 改版维护 CSS 选择器（现方案最大的长期维护负担）
- 不依赖 Playwright + 常驻 Chrome，客户端自动更新、登录持久

代价/风险：

- 整条链路非官方：深链是社区逆向协议，客户端大版本可能弄坏；坏了的表现是「点歌无效」，需要监控和提示
- 开发调试只能在 Windows：SMTC / `os.startfile` / 注册协议均无 macOS 等价物（现有 macOS 开发流程受影响，建议 `sys.platform` 分支保留网页版后端作开发/演示用）
- 切歌检测从 DOM 轮询换成 SMTC 轮询，边界情况（深链被吞、播放中弹窗）需实测调参

## Windows 上 10 分钟 PoC（先验证再动工）

仓库带了一个验证脚本 `scripts/win_desktop_poc.py`，在目标 Windows 机（已装 QQ 音乐客户端并登录）上按顺序跑：

```powershell
# 1. SMTC 通道：客户端放一首歌，然后列出系统媒体会话（应看到 QQ音乐 + 歌名）
python scripts/win_desktop_poc.py check

# 2. 深链通道：直接播一首指定 mid 的歌（晴天）
python scripts/win_desktop_poc.py deeplink 0039MnYb0qxYhV

# 3. 端到端：关键词搜索 + 选择 + 深链播放（命中 1、2、3 全通则方案成立）
python scripts/win_desktop_poc.py play 晴天 周杰伦

# 附：SMTC 切歌命令验证
python scripts/win_desktop_poc.py control next
```

判定标准：

- `check` 能看到 QQ音乐的会话 → SMTC 可用（基本必过，新版客户端都接入）
- `deeplink` 后客户端**直接开始播放** → 点歌通道成立，方案落地
- `deeplink` 只唤起客户端不播放 → 退路：pywinauto UI 自动化（能用但脆弱，维护成本≈现在的 selectors.json；此时建议重新权衡是否值得换）
- `control next` 能切歌 → 传输控制成立

## 参考

- [winsdk (PyPI)](https://pypi.org/project/winsdk/) · [winrt-Windows.Media.Control](https://pypi.org/project/winrt-Windows.Media.Control/)
- [GSMTC 官方文档 (Microsoft Learn)](https://learn.microsoft.com/en-us/uwp/api/windows.media.control.globalsystemmediatransportcontrolssessionmanager)
- [Python 读取 SMTC 示例 (Stack Overflow)](https://stackoverflow.com/questions/65011660/how-can-i-get-the-title-of-the-currently-playing-media-in-windows-10-with-python)
- [WindowsMediaController（SMTC 封装库）](https://github.com/DubyaDude/WindowsMediaController) · [SMTC 应用支持列表](https://github.com/ModernFlyouts-Community/ModernFlyouts/blob/main/docs/GSMTC-Support-And-Popular-Apps.md)
- [URL Scheme 汇总（含 playSonglist 格式）](https://gist.github.com/zhuziyi1989/3f96a73c45a87778b560e44cb551ebd2) · [博客园示例](https://www.cnblogs.com/xiao1993/p/18489299)
- [QQ音乐移动 WEB 开放平台](https://y.qq.com/m/api/open/index.html) · [QQ音乐开发者平台](https://developer.y.qq.com/)
