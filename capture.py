"""抓取系统正在播放的声音（WASAPI loopback），切成语音片段。"""
import collections
import time

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
    """默认扬声器的回环录音：电脑放什么，就抓什么。

    设备被拔、默认设备被切、驱动重启时自动重连，不把上层拖死。
    """

    def __init__(self, block=0.1):
        self.block = block
        self.rms = 0.0
        self.threshold = 0.008
        self._use_48k = False
        self._hist = collections.deque(maxlen=60)
        self._floor = 0.004
        self.sample_rate = SR
        self.reconnects = 0
        self.on_level = None          # 每块音频回调一次（给 UI 画电平）
        self.on_reconnect = None      # 重连时回调一次（给 UI 报状态）
        self._resolve()

    def _resolve(self):
        """重新解析默认扬声器的回环设备（换耳机 / 拔 USB 之后必须重拿）。"""
        spk = sc.default_speaker()
        self.name = spk.name
        self.mic = sc.get_microphone(id=str(spk.name), include_loopback=True)
        return self.name

    def _blocks_once(self):
        if not self._use_48k:
            try:
                with self.mic.recorder(samplerate=SR, channels=2,
                                       blocksize=int(SR * self.block)) as r:
                    while True:
                        data = r.record(numframes=int(SR * self.block))
                        yield np.asarray(data, dtype=np.float32).mean(axis=1)
            except Exception as e:  # noqa: BLE001
                print(f"[cap] 16k 不支持（{e}），改用 48k 采后降采样", flush=True)
                self._use_48k = True
                self.sample_rate = 48000
        with self.mic.recorder(samplerate=48000, channels=2,
                               blocksize=int(48000 * self.block)) as r:
            while True:
                data = r.record(numframes=int(48000 * self.block))
                mono = np.asarray(data, dtype=np.float32).mean(axis=1)
                yield _resample(mono, 48000, SR)

    def blocks(self):
        """无限重连版录音流：只有 GeneratorExit 才会真的结束。"""
        while True:
            try:
                yield from self._blocks_once()
                return
            except GeneratorExit:
                raise
            except Exception as e:  # noqa: BLE001
                self.reconnects += 1
                print(f"[cap] 采集中断（{e}），第 {self.reconnects} 次重连", flush=True)
                if self.on_reconnect is not None:
                    try:
                        self.on_reconnect(self.reconnects)
                    except Exception:  # noqa: BLE001
                        pass
                time.sleep(0.8)
                try:
                    self._resolve()
                    self._use_48k = False       # 新设备可能支持 16k，重新试一次
                except Exception as e2:  # noqa: BLE001
                    print(f"[cap] 重新解析设备失败（{e2}）", flush=True)
                    time.sleep(1.5)

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

    def _push_floor(self, rms, active=False):
        """滚动跟踪底噪：取最近 6 秒的 20 分位，小声说话也能被识别。

        说话期间不采样：否则长句会把底噪一起抬上去，后半句被吃掉。
        """
        if active:
            return
        self._hist.append(rms)
        if len(self._hist) >= 12:
            s = sorted(self._hist)
            self._floor = max(0.0010, s[int(len(s) * 0.2)])
            self.threshold = max(0.0022, self._floor * 2.2)

    def segments(self, min_len=0.50, tail=0.30, max_len=9.0,
                 min_voiced=0.22, min_ratio=0.16):
        """按能量切句（带迟滞），并且只交付「确实有人在说话」的片段。

        - 门限随底噪自适应，声音小也能进来
        - 迟滞：起振用 1.15 倍门限，维持用 0.62 倍，避免小声被切断
        - 静音片段送进 Whisper 会瞎编（you / thank you），必须挡掉
        """
        buf, tags = [], []
        voiced_time = 0.0
        silence_time = 0.0
        active = False
        for block in self.blocks():
            rms = block_level(block)
            self.rms = rms
            self._push_floor(rms, active)
            if self.on_level is not None:
                try:
                    self.on_level(rms)
                except Exception:  # noqa: BLE001
                    pass
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
            # 说话前的长静音不进片段：否则一句 0.25 秒的「Yes」会被开头的静音
            # 稀释掉（voiced/total 太小），直接整句丢弃。
            if not active and voiced_time == 0.0 and silence_time >= 0.5:
                buf, tags = [], []
                silence_time = 0.0
                continue
            if (total >= min_len and silence_time >= tail) or total >= max_len:
                if voiced_time >= min_voiced and voiced_time / total >= min_ratio:
                    seg = self._trim(buf, tags)
                    if seg is not None:
                        yield self._normalize(seg)
                buf, tags, voiced_time, silence_time = [], [], 0.0, 0.0
                active = False

    def snapshots(self, tick=0.35, tail=0.45, max_len=5.0,
                  min_voiced=0.10, min_ratio=0.10):
        """流式快照：说话期间每 tick 秒交付「这句话到目前为止」的音频。

        yield (audio, final)
          final=False  说话中的中间快照，上层拿去做增量识别（说一个词也能出）
          final=True   停顿超过 tail、或说满 max_len，本句收尾

        与 segments() 的区别：不等一句话说完才交付。纯静音不产出任何快照，
        免得把静音送进 Whisper 让它编词。
        """
        buf, tags = [], []
        voiced_time = 0.0
        silence_time = 0.0
        active = False
        last_emit = 0.0
        for block in self.blocks():
            rms = block_level(block)
            self.rms = rms
            self._push_floor(rms, active)
            if self.on_level is not None:
                try:
                    self.on_level(rms)
                except Exception:  # noqa: BLE001
                    pass
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
            if not active and voiced_time == 0.0 and silence_time >= 0.5:
                buf, tags = [], []
                silence_time = 0.0
                last_emit = 0.0
                continue

            if active and voiced_time >= min_voiced and (total - last_emit) >= tick:
                seg = self._trim(buf, tags)
                if seg is not None:
                    last_emit = total
                    yield self._normalize(seg), False

            if (voiced_time > 0 and silence_time >= tail) or total >= max_len:
                if voiced_time >= min_voiced and voiced_time / max(total, 1e-6) >= min_ratio:
                    seg = self._trim(buf, tags)
                    if seg is not None:
                        yield self._normalize(seg), True
                buf, tags = [], []
                voiced_time = silence_time = 0.0
                last_emit = 0.0
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
