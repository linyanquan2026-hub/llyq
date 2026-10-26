"""识别 + 翻译引擎（全离线）。

识别：faster-whisper small.en（CTranslate2，int8，CPU）
翻译：Qwen2.5-3B-Instruct q4_0 GGUF，走本地 llama-server（HTTP + SSE 流式）
模型放在 models/ 下，只下一次。
"""
import os
import re
import time

# 必须在 import huggingface_hub 之前设置，否则镜像不生效
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
os.environ.setdefault("HF_HUB_DISABLE_XET", "1")
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")

import numpy as np

BASE = os.path.dirname(os.path.abspath(__file__))
MODELS = os.environ.get("LT_MODELS") or os.path.join(BASE, "models")

_WS = re.compile(r"\s+")

# Whisper 在静音/噪声上最爱编的几句，直接丢掉
_HALLU = {
    "you", "thank you", "thanks", "thank you.", "thanks for watching",
    "thanks for watching!", "bye", "bye.", "bye bye", "okay", "ok",
    "hmm", "uh", "um", "...", "。", "subtitle", "subtitles by",
    "please subscribe", "like and subscribe",
}


def _clean_zh(s: str) -> str:
    s = s.strip()
    # Marian/Qwen 有时把中文逐字分开，去掉汉字之间的空格
    s = re.sub(r"(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])", "", s)
    # 中文标点两侧的空格也要吃掉，否则字幕里出现「你好 ，世界」这种缝
    s = re.sub(r"\s*([，。！？、；：」』）】…—])\s*", r"\1", s)
    s = re.sub(r"\s*([「『（【])\s*", r"\1", s)
    s = _WS.sub(" ", s).strip()
    return _collapse(s)


def _collapse(s: str) -> str:
    """把 "你好你好你好" 压回 "你好"。"""
    if len(s) < 6:
        return s
    for n in range(1, len(s) // 3 + 1):
        unit = s[:n]
        if unit * (len(s) // n) == s and len(s) // n >= 3:
            return unit
    return s


class Asr:
    def __init__(self, model="small.en", threads=6):
        from faster_whisper import WhisperModel
        t0 = time.time()
        local = os.path.join(MODELS, "whisper_ct2")
        if os.path.exists(os.path.join(local, "model.bin")):
            self.model = WhisperModel(local, device="cpu", compute_type="int8", cpu_threads=threads)
        else:
            self.model = WhisperModel(
                model,
                device="cpu",
                compute_type="int8",
                cpu_threads=threads,
                download_root=os.path.join(MODELS, "whisper"),
            )
        print(f"[asr] 就绪 {time.time() - t0:.1f}s", flush=True)

    def listen(self, audio: np.ndarray, sr: int = 16000) -> str:
        audio = np.asarray(audio, dtype=np.float32).flatten()
        if audio.size < sr * 0.3:
            return ""
        segs, _ = self.model.transcribe(
            audio,
            language="en",
            beam_size=1,
            best_of=1,
            temperature=0.0,
            condition_on_previous_text=False,
            vad_filter=False,
            without_timestamps=True,
        )
        parts = []
        for s in segs:
            if getattr(s, "no_speech_prob", 0.0) > 0.55:
                continue
            if getattr(s, "avg_logprob", 0.0) < -1.0:
                continue
            t = s.text.strip()
            if not t:
                continue
            if t.lower().strip(" .!?,") in _HALLU:
                continue
            if len(re.findall(r"[A-Za-z]", t)) < 3:
                continue
            parts.append(t)
        return " ".join(parts).strip()


SYS_PROMPT = (
    "你是专业的英译中字幕翻译。规则："
    "1) 全部译成简体中文，译文中禁止出现英文单词；"
    "2) 口语、俚语、粗话译成对应的中文口语，不要直译；"
    "3) 只输出译文，不要解释、不要加引号。"
)


class Mt:
    """Qwen2.5-3B 翻译，走本地 llama-server（HTTP，不依赖 torch）。

    模型：models/qwen/qwen2.5-3b-instruct-q4_0.gguf
    服务：优先 llama_vk（Vulkan 走显卡，首字 0.5 秒），不行退回 llama（CPU）
    """

    PORT_CANDIDATES = (8123, 8124, 8125, 8126)

    def __init__(self, threads=6):
        import atexit
        import urllib.request

        self._threads = threads
        self._urllib = urllib.request
        self.proc = None
        self.port = None
        t0 = time.time()

        # 打包后 __file__ 在 _internal 里，程序目录以 models 的父目录为准
        prog_dirs = []
        for d in (os.path.dirname(MODELS), BASE):
            if d and d not in prog_dirs:
                prog_dirs.append(d)
        self._model = os.path.join(MODELS, "qwen", "qwen2.5-3b-instruct-q4_0.gguf")
        if not os.path.exists(self._model):
            raise FileNotFoundError(f"缺少翻译模型：{self._model}")

        exes = [os.path.join(d, sub, "llama-server.exe")
                for d in prog_dirs for sub in ("llama_vk", "llama")]
        seen = []
        for e in exes:
            if os.path.exists(e) and e not in seen:
                seen.append(e)
        self._exes = seen
        if not self._exes:
            raise FileNotFoundError("缺少 llama-server（llama_vk / llama 目录都找不到）")

        self._launch()
        print(f"[mt] 就绪 {time.time() - t0:.1f}s（:{self.port}）", flush=True)
        atexit.register(self.close)

    def _launch(self):
        """起一个 llama-server：先试复用已有实例，再 GPU，最后 CPU。"""
        import subprocess

        for port in self.PORT_CANDIDATES:
            if self._alive(port):
                self.port = port
                self.proc = None
                print(f"[mt] 复用已在跑的 llama-server :{port}", flush=True)
                return

        for exe in self._exes:
            gpu = "llama_vk" in exe
            for port in self.PORT_CANDIDATES:
                try:
                    args = [exe, "-m", self._model, "-c", "512", "-t", str(self._threads),
                            "--port", str(port), "--host", "127.0.0.1", "--no-webui", "--no-warmup"]
                    if gpu:
                        args[-1:-1] = ["-ngl", "99"]     # 显卡全量卸载
                    self.proc = subprocess.Popen(
                        args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                        creationflags=0x08000000,            # CREATE_NO_WINDOW
                    )
                except Exception as e:  # noqa: BLE001
                    print(f"[mt] {exe} :{port} 起不来（{e}）", flush=True)
                    continue
                self.port = port
                if self._wait_ready(45):
                    print(f"[mt] {'GPU' if gpu else 'CPU'} :{port} 就绪", flush=True)
                    return
                print(f"[mt] {'GPU' if gpu else 'CPU'} :{port} 超时，换下一个", flush=True)
                self.close()
        raise RuntimeError("llama-server 起不来（端口被占或模型损坏）")

    def ensure_alive(self, seconds=30):
        """健康检查；服务掉了就重启。返回是否可用。"""
        if self.port and self._alive(self.port):
            return True
        print("[mt] 翻译服务不在，重启中…", flush=True)
        self.close()
        try:
            self._launch()
        except Exception as e:  # noqa: BLE001
            print(f"[mt] 重启失败：{e}", flush=True)
            return False
        return self._alive(self.port) if self.port else False

    # ------------------------------------------------------------------ 内部
    def _url(self, port, path):
        return f"http://127.0.0.1:{port}{path}"

    def _alive(self, port):
        try:
            with self._urllib.urlopen(self._url(port, "/health"), timeout=1.5) as r:
                return r.status == 200
        except Exception:  # noqa: BLE001
            return False

    def _wait_ready(self, seconds):
        t0 = time.time()
        while time.time() - t0 < seconds:
            if self._alive(self.port):
                return True
            time.sleep(0.4)
        return False

    def close(self):
        if self.proc is not None:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=5)
            except Exception:  # noqa: BLE001
                try:
                    self.proc.kill()
                except Exception:  # noqa: BLE001
                    pass
            self.proc = None

    def _ask(self, text, strict=False, on_partial=None):
        """请求翻译。给了 on_partial 就走流式，边生成边回调（首字约 0.3 秒上屏）。"""
        import json
        sys_prompt = SYS_PROMPT + ("译文里出现任何英文字母都算错。" if strict else "")
        stream = on_partial is not None
        body = json.dumps({
            "messages": [{"role": "system", "content": sys_prompt},
                         {"role": "user", "content": text}],
            "max_tokens": 200, "temperature": 0.2, "stream": stream,
        }).encode()
        req = self._urllib.Request(self._url(self.port, "/v1/chat/completions"), data=body,
                                   headers={"Content-Type": "application/json"})
        if not stream:
            with self._urllib.urlopen(req, timeout=90) as r:
                j = json.load(r)
            return j["choices"][0]["message"]["content"].strip()

        acc = ""
        with self._urllib.urlopen(req, timeout=90) as r:
            for raw in r:
                line = raw.decode("utf-8", "ignore").strip()
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if payload == "[DONE]":
                    break
                try:
                    j = json.loads(payload)
                    delta = j["choices"][0]["delta"].get("content") or ""
                except Exception:  # noqa: BLE001
                    continue
                if delta:
                    acc += delta
                    try:
                        on_partial(_clean_zh(acc))
                    except Exception:  # noqa: BLE001
                        pass
        return acc.strip()

    # ------------------------------------------------------------------ 对外
    def translate(self, text: str, on_partial=None) -> str:
        text = text.strip()
        if not text:
            return ""
        try:
            out = _clean_zh(self._ask(text, on_partial=on_partial))
        except Exception as e:  # noqa: BLE001
            # 服务挂了（显存不足 / 驱动复位 / 端口被抢）：重连一次再问
            print(f"[mt] 请求失败（{e}），重连后重试", flush=True)
            if not self.ensure_alive():
                raise
            out = _clean_zh(self._ask(text, on_partial=on_partial))
        if re.search(r"[A-Za-z]{2,}", out):          # 还留着英文，重试一次
            try:
                out2 = _clean_zh(self._ask(text, strict=True))
                if out2 and not re.search(r"[A-Za-z]{2,}", out2):
                    if on_partial:
                        on_partial(out2)
                    return out2
            except Exception:  # noqa: BLE001
                pass
        return out
