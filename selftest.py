"""离线自测：读一个 wav → 识别 → 翻译，不依赖扬声器。

用法：python selftest.py test_en.wav
"""
import sys
import time
import wave

import numpy as np

sys.path.insert(0, r"C:\deepseek\live-translate")
import engine  # noqa: E402


def load_wav(path):
    with wave.open(path, "rb") as w:
        ch, sw, sr, n = w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()
        raw = w.readframes(n)
    if sw == 2:
        data = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    elif sw == 4:
        data = np.frombuffer(raw, dtype=np.int32).astype(np.float32) / 2147483648.0
    else:
        data = np.frombuffer(raw, dtype=np.uint8).astype(np.float32) / 128.0 - 1.0
    if ch > 1:
        data = data.reshape(-1, ch).mean(axis=1)
    if sr != 16000:
        from capture import _resample
        data = _resample(data, sr, 16000)
    return data, sr


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else r"C:\deepseek\live-translate\test_en.wav"
    audio, sr = load_wav(path)
    print(f"音频 {len(audio) / 16000:.2f}s（原始 {sr}Hz）")

    t0 = time.time()
    asr = engine.Asr()
    print(f"识别模型加载 {time.time() - t0:.1f}s")

    t0 = time.time()
    en = asr.listen(audio)
    t_asr = time.time() - t0
    print(f"[ASR {t_asr:.2f}s] {en}")

    t0 = time.time()
    mt = engine.Mt()
    print(f"翻译模型加载 {time.time() - t0:.1f}s")

    t0 = time.time()
    zh = mt.translate(en)
    print(f"[MT {time.time() - t0:.2f}s] {zh}")


if __name__ == "__main__":
    main()
