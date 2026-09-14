# 桌面客户端控制方案调研 · QQ 音乐 Windows 版

> 调研日期：2026-09。目标：把播放内核从「Playwright 驱动 y.qq.com 网页版」换成「局域网网页控制本机 QQ 音乐桌面客户端」，部署形态不变——一台蓝牙连办公室音箱的 Windows 电脑当服务器，同事用局域网网页点歌。
>
> **✅ 2026-09-08 已实施落地**（实测结论见文末「PoC 实测结果」），代码：`app/player_desktop.py`，`JUKEBOX_BACKEND=desktop` 默认启用（Windows）。

## 结论

**可行，但没有单一官方接口，需要三条通道拼装：**

| 能力 | 通道 | 可靠性 |
|---|---|---|
| 播放/暂停/上一首/下一首 | Windows SMTC 系统媒体会话 | 高（系统级 API，有官方文档，不碰客户端 UI，无需前台窗口） |
| 正在播放（歌名/歌手/进度/状态） | 同上 SMTC 媒体属性 | 高 |
| 点歌（搜索 → 播指定歌曲） | fcg 搜索拿元数据 → UIA 驱动客户端搜索框 + PostMessage 双击结果行 | 中（依赖客户端 UI 结构，但控件名稳定且有 UIA 名称） |
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
- [URL Scheme 汇总（含 playSonglist 格式）](https://gist.github.com/zhuziyi1989/3f96a73c45a87778e44cb551ebd2) · [博客园示例](https://www.cnblogs.com/xiao1993/p/18489299)
- [QQ音乐移动 WEB 开放平台](https://y.qq.com/m/api/open/index.html) · [QQ音乐开发者平台](https://developer.y.qq.com/)

## PoC 实测结果（2026-09-08，QQ 音乐 22.61，Windows 11）

按上表预想跑完三步 PoC，**两条预想通道死亡、一条意外通道死亡，最终用备选通道落地**：

| 通道 | 结果 | 原因 |
|---|---|---|
| SMTC 读取/控制 | ✅ 完全可用 | 播放中有 `QQMusic.exe` 会话；pause/play/next/prev 全生效（next/prev 对单曲队列表现为重播，多曲队列正常切歌）。注意：**没有任何播放时 get_sessions() 为空**，属正常 |
| fcg 搜索 | ✅ 完全可用 | `DoSearchForQQMusicDesktop` 无需登录，songmid 与付费标记（`pay.pay_play`）都能拿到 |
| `qqmusic://` 深链 | ❌ 死亡 | 本机协议未注册（`HKCR\qqmusic` 不存在），`os.startfile` 直接弹「选择打开方式」；手工注册到 `QQMusic.exe --args="%1"` 后也只是唤起窗口不播放（`playSonglist` 对 PC 客户端 22.61 无效） |
| QQMusicSvr COM SDK | ❌ 死亡（一半） | 注册表有完整的官方 COM 注册（`QQMusicSvr.QQMusicPlayer` 等 5 个类，LocalServer32 → `QQMusicSvr.exe`，含 IQQPlayer/IQQControl/IQLyric 及事件连接点）。实测：Svr 需 `-Embedding` 预启动才存活；COM 激活后 `Play/Pause/PlayNext/GetPlaySongInfo` 全部返回 S_OK **但零效果**，`AddSong` 任何参数格式（字符串/数组/嵌套）都静默失败——客户端侧的 `csQQMusicComApiWnd2017` 窗口（WM_COPYDATA 接收端）虽存在但已不理睬这代协议。结论：**官方 COM 桥在 22.61 已实质废弃** |
| 本地 HTTPS 端口 5283 (sctun) | ❌ 放弃 | 客户端内部隧道（证书 `ql.njmapp.com`），无文档的私有协议，投入产出比太低 |
| **UIA + PostMessage（最终方案）** | ✅ 落地 | 客户端 TXGuiFoundation 自绘 UI 的 **UIAutomation 树完整可靠**（搜索框是唯一 Edit；结果行的歌名是 Hyperlink、歌手是「歌手：xxx」链接，坐标可精确定位）。输入有三坑，见下 |

### UIA 通道的三个关键坑（已绕过，改动在 `app/player_desktop.py`）

1. **窗口标题随播放歌曲变化**（空播时「QQ音乐」，播放中「搁浅 - 周杰伦」）——不能按标题找窗口，按「QQMusic.exe 进程 + 面积最大的窗口」找。
2. **SendInput 键盘进不去后台窗口**：`element.set_focus()` 是 UIA 合成焦点，`send_keys` 的 SendInput 键进的是真实前台窗口——文字全丢。**必须 PostMessage**：`WM_CHAR` 逐字 + `WM_KEYDOWN(VK_RETURN)` 提交。
3. **自绘输入框不认 Ctrl+A 和 WM_CHAR(8)**：清空旧关键词要用 `WM_KEYDOWN(VK_BACK)` × N，Ctrl+A 是无效的。

双击结果行同样走 PostMessage（`WM_LBUTTONDOWN/UP/DBLCLK/UP` 四连发，ScreenToClient 换算），全程**不移动真实光标、不抢前台**——部署机有人在用也不受影响。

### 实施落点

- `app/player_desktop.py`：桌面后端，与 `app/player.py`（网页版）接口完全一致
- `app/player_select.py`：后端切换。环境变量 `JUKEBOX_BACKEND=web` 强制网页版；默认 Windows 自动用桌面客户端版
- 依赖（requirements.txt 已加）：`pywinauto`、`winrt-runtime`、`winrt-Windows.Foundation[.Collections]`、`winrt-Windows.Media.Control`（均仅 win32）
- 注意：Python 3.14 上老 `winsdk` 编译不过，必须用 `winrt-*` 新包（导入路径 `winrt.windows.media.control`）
- comtypes 若要用 QQMusicSvr 类型库生成代码，中文系统需把 `comtypes/tools/codegenerator/codegenerator.py` 里硬编码的 `coding: mbcs` 补丁成 utf-8（venv 内已改，仅供后续逆向参考）
