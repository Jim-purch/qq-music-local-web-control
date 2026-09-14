"""播放后端选择：JUKEBOX_BACKEND=desktop|web（默认 Windows 用桌面客户端，其他平台用网页版）。

启动时做依赖防呆检查：桌面后端需要的库装在项目 venv 里，误用全局 Python
启动时给出明确提示，而不是跑起来之后点歌/音量/WebSocket 一个个静默失效。
"""
import importlib.util
import os
import sys

_DESKTOP_DEPS = ("winrt", "pywinauto", "pycaw", "comtypes", "websockets")

backend = os.environ.get("JUKEBOX_BACKEND", "").lower()

if backend != "web" and sys.platform == "win32":
    missing = [m for m in _DESKTOP_DEPS if importlib.util.find_spec(m) is None]
    if missing:
        print("\n[启动失败] 当前 Python 缺少桌面客户端后端依赖: " + ", ".join(missing))
        print("  当前解释器: " + sys.executable)
        print("  解决办法（任选其一）：")
        print("    1) 用项目虚拟环境启动（推荐）: .venv\\Scripts\\python.exe -m app.main")
        print("       或直接双击项目目录下的 start.bat")
        print("    2) 给当前解释器安装依赖: python -m pip install -r requirements.txt pycaw comtypes\n")
        raise SystemExit(1)
    from . import player_desktop as player
else:
    from . import player as player
