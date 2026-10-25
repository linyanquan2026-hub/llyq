"""抓取系统正在播放的声音（WASAPI loopback），切成语音片段。"""
import collections

import numpy as np

import soundcard as sc

SR = 16000


def block_level(block):
    """块内分段取峰值：人声有起伏，噪声平坦。比整块 RMS 更适合小声。"""
    n = len(block)
    k = max(1, n // 3)
    best = 0.0
    for i in range(0, n - k + 1, k):
        v = float(np.sqrt(np.mean(block[i:i + k] ** 2)))
        if v > best:
            best = v
    return best


def _resample(x: np.ndarray, src: int, dst: int) -> np.ndarray:
    if src == dst:
        return x
    if src % dst == 0:                       # 盒式降采样，语音够用
        k = src // dst
        n = len(x) // k * k
        return x[:n].reshape(-1, k).mean(axis=1)
    idx = np.linspace(0, len(x) - 1, int(len(x) * dst / src))
    return np.interp(idx, np.arange(len(x)), x)


class Loopback:
    """默认扬声器的回环录音：电脑放什么，就抓什么。"""

    def __init__(self, block=0.1):
        spk = sc.default_speaker()
        self.name = spk.name
        self.mic = sc.get_microphone(id=str(spk.name), include_loopback=True)
        self.block = block
        self.rms = 0.0
        self.threshold = 0.008
        self._use_48k = False
        self._hist = collections.deque(maxlen=60)
        self._floor = 0.004
        self.sample_rate = SR

    def blocks(self):
        if not self._use_48k:
            try:
                with self.mic.recorder(samplerate=SR, channels=2,
                                       blocksize=int(SR * self.block)) as r:
                    while True:
                        data = r.record(numframes=int(SR * self.block))
                        yield np.asarray(data, dtype=np.float32).mean(axis=1)
            except Exception as e:
                print(f"[cap] 16k 不支持（{e}），改用 48k 采后降采样", flush=True)
                self._use_48k = True
                self.sample_rate = 48000
        with self.mic.recorder(samplerate=48000, channels=2,
                               blocksize=int(48000 * self.block)) as r:
            while True:
                data = r.record(numframes=int(48000 * self.block))
                mono = np.asarray(data, dtype=np.float32).mean(axis=1)
                yield _resample(mono, 48000, SR)

    def calibrate(self, seconds=1.0):
        """测一段环境噪声，把门限设在噪声的 2.2 倍。"""
        vals = []
        gen = self.blocks()
        try:
            for _ in range(max(6, int(seconds / self.block))):
                b = next(gen)
                vals.append(float(np.sqrt(np.mean(b * b))))
        finally:
            gen.close()
        for v in vals:
            self._push_floor(v)
        noise = float(np.percentile(vals, 20)) if vals else 0.002
        self._floor = max(0.0012, noise)
        self.threshold = max(0.0022, self._floor * 2.2)
        print(f"[cap] 设备={self.name} 底噪={noise:.4f} 门限={self.threshold:.4f}", flush=True)
        return noise

    def _push_floor(self, rms):
        """滚动跟踪底噪：取最近 6 秒的 20 分位，小声说话也能被识别。"""
        self._hist.append(rms)
        if len(self._hist) >= 12:
            s = sorted(self._hist)
            self._floor = max(0.0010, s[int(len(s) * 0.2)])
            self.threshold = max(0.0022, self._floor * 2.2)

    def segments(self, min_len=0.55, tail=0.30, max_len=9.0,
                 min_voiced=0.30, min_ratio=0.22):
        """按能量切句（带迟滞），并且只交付「确实有人在说话」的片段。

        - 门限随底噪自适应，声音小也能进来
        - 迟滞：起振用 1.25 倍门限，维持用 0.75 倍，避免小声被切断
        - 静音片段送进 Whisper 会瞎编（you / thank you），必须挡掉
        """
        buf, tags = [], []
        voiced_time = 0.0
        silence_time = 0.0
        active = False
        for block in self.blocks():
            rms = block_level(block)
            self.rms = rms
            self._push_floor(rms)
            hi = self.threshold * 1.15
            lo = self.threshold * 0.62
            if not active and rms > hi:
                active = True
            elif active and rms < lo:
                active = False
            is_voiced = active
            buf.append(block)
            tags.append(is_voiced)
            blk = len(block) / SR
            if is_voiced:
                voiced_time += blk
                silence_time = 0.0
            else:
                silence_time += blk

            total = len(buf) * blk
            if (total >= min_len and silence_time >= tail) or total >= max_len:
                if voiced_time >= min_voiced and voiced_time / total >= min_ratio:
                    seg = self._trim(buf, tags)
                    if seg is not None:
                        yield self._normalize(seg)
                buf, tags, voiced_time, silence_time = [], [], 0.0, 0.0
                active = False

    @staticmethod
    def _normalize(seg, target=0.85, floor=0.004, max_gain=20.0):
        """声音小的时候拉一把音量（最高 +26dB）。

        Whisper 对低电平很不敏感：系统音量小、或者说话轻声时，
        分句能切到、识别却返回空字符串，必须在这里补增益。
        """
        peak = float(np.max(np.abs(seg))) if seg.size else 0.0
        if floor < peak < target:
            seg = seg * min(target / peak, max_gain)
        return seg.astype(np.float32)

    @staticmethod
    def _trim(buf, tags, pad=0.12):
        idx = [i for i, v in enumerate(tags) if v]
        if not idx:
            return None
        lo = max(0, idx[0] - 1)
        hi = min(len(buf), idx[-1] + 2)
        out = np.concatenate(buf[lo:hi])
        return out if len(out) > 0 else None
