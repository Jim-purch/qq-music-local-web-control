"""一键引导：克隆后无需手动建 venv/装依赖，直接 scripts/bootstrap.py 或 start.bat。

流程：检查 Python 版本 → 不存在 .venv 则创建 → 缺依赖则 pip 安装 → 启动服务。
仅用标准库，任何 Python 3.10+ 都能跑本脚本。
"""
import os
import subprocess
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VENV_PY = os.path.join(BASE, ".venv", "Scripts", "python.exe")
REQS = os.path.join(BASE, "requirements.txt")

# 桌面/网页后端都会用到的关键依赖（缺失任何一个就触发安装）
KEY_DEPS = "fastapi, uvicorn, playwright, websockets"
if sys.platform == "win32":
    KEY_DEPS += ", pycaw, comtypes, pywinauto, winrt"


def say(msg):
    print(msg, flush=True)


def run(cmd, **kw):
    return subprocess.run(cmd, cwd=BASE, **kw).returncode


def main():
    if sys.version_info < (3, 10):
        say(f"[错误] 需要 Python 3.10+，当前是 {sys.version.split()[0]}（{sys.executable}）")
        sys.exit(1)

    if not os.path.isfile(VENV_PY):
        say("› 未发现虚拟环境 .venv，正在创建（使用 " + sys.executable + "）...")
        if run([sys.executable, "-m", "venv", ".venv"]) != 0:
            say("[错误] 创建 .venv 失败")
            sys.exit(1)
        say("  完成")
    else:
        say("› 虚拟环境 .venv 已就绪")

    say("› 检查依赖...")
    probe = run([VENV_PY, "-c", f"import {KEY_DEPS}"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if probe != 0:
        say("  缺少依赖，开始安装（首次约需几分钟，来自 requirements.txt）...")
        pkgs = ["-r", REQS]
        if sys.platform == "win32":
            pkgs += ["pycaw", "comtypes"]  # README 标注的 Windows 必需项
        if run([VENV_PY, "-m", "pip", "install", "--disable-pip-version-check", *pkgs]) != 0:
            say("[错误] 依赖安装失败：请检查网络后重试，或手动执行：")
            say(f"    {VENV_PY} -m pip install -r requirements.txt pycaw comtypes")
            sys.exit(1)
        say("  依赖安装完成")
    else:
        say("  依赖齐全")

    if "--setup-only" in sys.argv:
        say("› 环境就绪（--setup-only，不启动服务）")
        return

    say("› 启动服务（Ctrl+C 停止）...\n")
    code = run([VENV_PY, "-m", "app.main"])
    sys.exit(code)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
