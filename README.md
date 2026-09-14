# 办公室点唱机 · QQ 音乐局域网点播台

在局域网内用浏览器控制一台电脑上的 **QQ 音乐桌面客户端**：共享点歌队列、切歌、音量、歌单、播放统计与播放时段控制。Python 实现，适合 Windows 办公室部署——谁点的歌，记得清清楚楚。

## 工作原理

```
办公室任意浏览器 ──HTTP/WebSocket──▶ 本服务 (FastAPI)
                                        │
                                        ├─ Windows SMTC ──▶ QQ音乐桌面客户端
                                        │    播放/暂停/切歌/正在播放（系统级，不碰 UI）
                                        │
                                        ├─ UIA + PostMessage ──▶ 客户端搜索框输入 + 双击结果
                                        │    点歌（不动真实鼠标键盘、不抢前台）
                                        │
                                        ├─ fcg 接口 ──▶ 歌曲元数据（songmid/歌名/歌手）
                                        │
                                        └─ SQLite (data/jukebox.db)
                                             用户 / 歌单 / 播放历史 / 播放时段
```

- 播放发生在**部署机的 QQ 音乐客户端**里，声音从这台电脑出；音质/VIP 曲库即客户端本身的水准
- QQ 音乐在客户端里登录一次长期有效，服务不管 cookie
- 点歌走客户端自身搜索页，改版风险集中在 `app/player_desktop.py` 的元素定位逻辑
- 想退回旧的「Playwright 驱动网页版」方案：设 `JUKEBOX_BACKEND=web`

## 快速开始（Windows）

要求：Windows 10/11 + Python 3.10+ + 已安装 QQ 音乐 Windows 客户端（已登录）

**最简单**：双击项目目录下的 `start.bat`。首次运行会自动创建虚拟环境 `.venv` 并安装全部依赖（几分钟），之后每次直接启动服务。

手动方式（等价）：

```powershell
git clone https://github.com/Jim-purch/qq-music-local-web-control.git
cd qq-music-local-web-control
python scripts/bootstrap.py        # 建 .venv + 装依赖 + 启动（加 --setup-only 只装不启）
# 或分步：
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt pycaw comtypes
.venv\Scripts\python.exe -m app.main
```

注意 `.venv` 不入库（.gitignore 已排除），克隆后靠 start.bat / bootstrap.py 现场构建；误用全局 Python 启动会被依赖防呆检查拦下并给出提示。仅网页版后端（`JUKEBOX_BACKEND=web`）才需要 `playwright install chromium`。

然后：

1. 浏览器打开 `http://localhost:4680`（或终端打印的局域网地址，如 `http://192.168.x.x:4680`）
2. 首次使用输入昵称（可选设口令，用于换设备登录同一昵称）
3. 「设置 → 启动播放器」拉起 QQ 音乐客户端（未登录的话在客户端里扫码，会员账号可播 VIP 曲目）
4. 「点歌」页搜索点歌，全办公室共享一个队列；可配置「播放时段」控制何时允许放歌

键盘快捷键：`空格` 播放/暂停 · `N` 下一首 · `P` 上一首 · `/` 跳到点歌框

## 功能一览

- **共享队列（点唱机模式）**：所有人往同一个队列里添歌，标注点歌人；可上移/移除
- **切歌 / 暂停 / 音量**：即时生效，所有打开的页面通过 WebSocket 同步；音量直接控 Windows 系统音量
- **歌单**：本服务自建歌单（独立于 QQ 音乐账号歌单），记录每首歌的存入者，支持调序、单曲播放、整单播放
- **播放历史与统计**：每首歌何时开始/结束、谁点的、是否被切；按日/周/月统计总时长、各人贡献、时段分布、Top 歌曲
- **播放时段控制**：按星期+时间段配置允许播放窗口（如工作日 12:00–13:30），时段外自动暂停、到点自动续播
- **用户身份**：昵称 + cookie 识别，昵称可被口令保护

## 隐私与安全须知

- **`data/` 目录包含 QQ 音乐登录 cookie，已被 .gitignore 排除，切勿提交或分享**
- 服务无鉴权，仅适合**可信局域网**使用；不要暴露到公网（如需远程，建议套 Tailscale/WireGuard 私有网络）
- 统计数据（听歌习惯）存在本机 SQLite，不出局域网
- Windows 首次启动若弹出防火墙提示，允许"专用网络"即可

## 配置

| 环境变量 | 默认 | 说明 |
|---|---|---|
| `PORT` | 4680 | 服务端口 |
| `JUKEBOX_BACKEND` | Windows 自动 `desktop`，其他平台 `web` | `desktop`=QQ音乐桌面客户端；`web`=Playwright 网页版（备用） |

桌面客户端方案的通道细节、已死通道（深链/COM SDK）实测记录见 [docs/desktop-client-plan.md](docs/desktop-client-plan.md)。网页版后端的选择器配置见 `app/selectors.json`。

## 目录结构

```
app/
  main.py             # FastAPI 路由 + WebSocket 广播
  core.py             # 队列/播放循环/时段控制的业务逻辑
  player_select.py    # 后端选择（desktop/web）
  player_desktop.py   # 桌面客户端后端：SMTC 控制 + UIA 点歌 + fcg 搜索
  player.py           # 网页版后端（Playwright 驱动 y.qq.com，备用）+ 系统音量
  db.py               # SQLite
  selectors.json      # 网页版选择器（仅 web 后端用）
static/               # 前端（桌面优先，窄屏自适应单列）
data/                 # 运行时数据（git 忽略）
```

## 桌面客户端方案（已实施）

播放内核为 **QQ 音乐 Windows 桌面客户端**（音质/曲库/VIP 完整，登录由客户端自己管）：SMTC 系统媒体会话做播放控制与正在播放读取，UIA + PostMessage 做点歌（客户端搜索页输入关键词并双击结果行，全程不碰真实键鼠）。可行性与死通道（`qqmusic://` 深链、QQMusicSvr COM SDK）实测记录见 [docs/desktop-client-plan.md](docs/desktop-client-plan.md)。

## 并发设计（多人同时点歌）

队列是共享的，先点先播；点歌动作本身**严格串行**（客户端只有一个搜索框），核心机制：

- **播放转换锁**（`core.py: _play_lock`）：一次「取队首 → 客户端搜索 → 起播」全程持锁，多人同时点歌/切歌时后来的自动排队，不会有两套自动化在搜索框里互相踩踏
- **失败不卡队列**：某首歌搜不到/放不了 → WebSocket 广播 `play:error`（前端弹提示）并自动试下一首
- **不可播曲目兜底**：客户端自动跳过的歌（VIP/下架）由轮询器 mismatch 计数兜底判定结束，队列永不卡死
- **并发删歌/调序安全**：队列变更端点统一在事件循环上执行，检查与修改原子化

## 已知限制

- 点歌依赖客户端搜索页的 UI 结构（UIA 元素名与布局），客户端大改版时需更新 `app/player_desktop.py`
- 自动切歌依赖 SMTC 标题轮询（3 秒间隔），一首自然结束后最多延迟数秒接播下一首
- 不可播曲目（版权/下架）客户端会自动跳过，服务端通过标题轮询兜底
- 系统音量控制在 Windows 用 pycaw、macOS 用 osascript，其他平台未适配
- SMTC 会话只在有播放活动时存在：客户端空置时「正在播放」显示为空属正常
