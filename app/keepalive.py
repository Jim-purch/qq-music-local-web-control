"""蓝牙音箱保活：暂停/空闲时循环播放人耳不可闻的低幅噪声，保持 Windows 音频端点
处于活动状态，蓝牙 A2DP 链路因此持续有数据流，避免音箱长时间无声后自动断连省电。

仅 Windows 有效（winsound）；其他平台为 no-op。
噪声文件运行时生成在 data/bt_keepalive.wav（不入库）。
"""
import os
import random
import struct
import sys
import wave

_wav_path = None
_started = False


def _ensure_wav() -> str:
    """生成 5 秒约 -50dBFS 的白噪声（mono 22.05kHz 16bit）。白噪声无周期性，
    循环点不会产生咔哒声；-50dB 在办公室环境完全不可闻，但足以让 Windows 音频
    端点和有「静音检测休眠」的音箱都判定为仍在工作。"""
    global _wav_path
    if _wav_path and os.path.exists(_wav_path):
        return _wav_path
    base = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
    os.makedirs(base, exist_ok=True)
    path = os.path.join(base, "bt_keepalive.wav")
    rate = 22050
    frames = bytearray()
    for _ in range(rate * 5):
        frames += struct.pack("<h", random.randint(-100, 100))
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(bytes(frames))
    _wav_path = path
    return path


def start():
    """开始保活（幂等）。"""
    global _started
    if sys.platform != "win32" or _started:
        return
    try:
        import winsound
        winsound.PlaySound(_ensure_wav(),
                           winsound.SND_FILENAME | winsound.SND_LOOP | winsound.SND_ASYNC)
        _started = True
        print("[keepalive] 蓝牙保活已开启（静音流）")
    except Exception as e:
        print("[keepalive] 启动失败:", e)


def stop():
    """停止保活（幂等）。"""
    global _started
    if sys.platform != "win32" or not _started:
        return
    try:
        import winsound
        winsound.PlaySound(None, winsound.SND_PURGE)
    except Exception:
        pass
    _started = False
