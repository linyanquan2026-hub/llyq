"""验证系统声音回环：边放 wav 边录，看能不能抓到。"""
import subprocess
import sys
import time

import numpy as np

sys.path.insert(0, r"C:\deepseek\live-translate")
from capture import Loopback  # noqa: E402

WAV = r"C:\deepseek\live-translate\test_en.wav"

ps = (
    "Add-Type -AssemblyName presentationCore;"
    f"$p = New-Object System.Windows.Media.MediaPlayer;"
    f"$p.Open([uri]'{WAV}'); Start-Sleep -Milliseconds 300; $p.Play(); Start-Sleep -Seconds 11"
)
proc = subprocess.Popen(["powershell", "-NoProfile", "-Command", ps])

cap = Loopback()
print("设备:", cap.name)
time.sleep(1.0)

gen = cap.blocks()
frames = []
t0 = time.time()
while time.time() - t0 < 4.0:
    frames.append(next(gen))
gen.close()

audio = np.concatenate(frames)
rms = float(np.sqrt(np.mean(audio ** 2)))
peak = float(np.max(np.abs(audio)))
print(f"录到 {len(audio) / 16000:.2f}s  采样率={cap.sample_rate}")
print(f"RMS={rms:.4f}  PEAK={peak:.4f}")
print("结论:", "回环正常，有声音" if rms > 0.005 else "没抓到声音（检查是否在播放 / 默认扬声器）")
proc.wait()
