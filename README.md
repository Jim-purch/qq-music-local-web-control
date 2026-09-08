# 办公室点唱机 · QQ 音乐局域网点播台

在局域网内用浏览器控制一台电脑上的 QQ 音乐网页版：共享点歌队列、切歌、音量、歌单、播放统计与播放时段控制。Python 实现，适合 Windows 办公室部署——谁点的歌，记得清清楚楚。

## 工作原理

```
办公室任意浏览器 ──HTTP/WebSocket──▶ 本服务 (FastAPI)
                                        │
                                        ├─ Playwright
                                        │    └─ 驱动一个常驻 Chrome/Edge 窗口打开 y.qq.com
                                        │        搜索 → 点歌 → 播放/切歌
                                        │
                                        └─ SQLite (data/jukebox.db)
                                             用户 / 歌单 / 播放历史 / 播放时段
```

- 播放发生在**部署机**的浏览器窗口里，声音从这台电脑出
- QQ 音乐登录态保存在 `data/chrome-profile`，扫码一次长期有效
- 网页版 y.qq.com 的 DOM 变化时，只需更新 `app/selectors.json`

## 快速开始（Windows）

要求：Windows 10/11 + Python 3.10+ + 已安装 Chrome 或 Edge

```powershell
git clone https://github.com/Jim-purch/qq-music-local-web-control.git
cd qq-music-local-web-control
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
pip install pycaw comtypes        # Windows 系统音量控制（必需）
playwright install chromium       # 若本机没有 Chrome/Edge 才需要
python -m app.main
```

然后：

1. 浏览器打开 `http://localhost:4680`（或终端打印的局域网地址，如 `http://192.168.x.x:4680`）
2. 首次使用输入昵称（可选设口令，用于换设备登录同一昵称）
3. 「设置 → 启动播放器」，在弹出的 Chrome/Edge 里登录 QQ 音乐（会员账号可播 VIP 曲目）
4. 「点歌」页搜索点歌，全办公室共享一个队列

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

选择器配置见 `app/selectors.json`；QQ 音乐网页版改版导致按钮找不到时，更新对应 CSS 选择器即可。

## 目录结构

```
app/
  main.py        # FastAPI 路由 + WebSocket 广播
  core.py        # 队列/播放循环/时段控制的业务逻辑
  player.py      # Playwright 控制 y.qq.com + 系统音量（Windows: pycaw）
  db.py          # SQLite
  selectors.json # 网页选择器（改版时更新这里）
static/          # 前端（桌面优先，窄屏自适应单列）
data/            # 运行时数据（git 忽略）
```

## 桌面客户端方案（调研中）

正在评估把播放内核从网页版换成 **QQ 音乐 Windows 桌面客户端**（音质/曲库更好，且不用维护网页选择器）：SMTC 系统媒体会话做播放控制，`qqmusic://` 深链做点歌。可行性、风险与整合方案见 [docs/desktop-client-plan.md](docs/desktop-client-plan.md)；在目标 Windows 机上先用 `scripts/win_desktop_poc.py` 做三步 PoC 验证再动工。

## 已知限制

- 网页版播放音质/曲目范围受 QQ 音乐网页端限制
- 自动切歌依赖播放条标题轮询，极端情况下可能延迟数秒
- 系统音量控制在 Windows 用 pycaw、macOS 用 osascript，其他平台未适配
