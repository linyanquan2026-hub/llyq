"""配置持久化：%APPDATA%\\LiveTranslate\\config.json

关掉再开不丢：字号、双语开关、窗口位置、文字模式、历史条数、线程数。
写盘用「临时文件 + os.replace」，掉电也不会留下半个 JSON。
%APPDATA% 不可写时（受限账户、域策略）依次退到 %LOCALAPPDATA%、程序目录，
绝不因为存不了配置就把界面卡住。
"""
import json
import os
import sys
import time

APP = "LiveTranslate"
# 打包后 __file__ 在 _internal 里，程序目录要以 exe 所在目录为准
_BASE = (os.path.dirname(sys.executable) if getattr(sys, "frozen", False)
         else os.path.dirname(os.path.abspath(__file__)))
_PICKED = []
_SAID = set()

DEFAULTS = {
    "font_zh": 21,       # 译文字号
    "font_en": 10,       # 英文字号
    "bilingual": True,   # 是否显示英文原文
    "minimal": False,    # 文字模式（F4）
    "pos": None,         # [x, y]，None = 默认底部居中
    "history": 6,        # 字幕历史保留条数
    "asr_threads": 6,
    "mt_threads": 6,
}


def candidates():
    """按优先级列出候选配置文件路径。"""
    override = os.environ.get("LT_CONFIG")
    if override:
        return [override]
    out = []
    for var in ("APPDATA", "LOCALAPPDATA"):
        base = os.environ.get(var)
        if base:
            out.append(os.path.join(base, APP, "config.json"))
    out.append(os.path.join(_BASE, "config.json"))     # 最后兜底：程序目录
    return out


def _writable(p):
    d = os.path.dirname(p)
    try:
        os.makedirs(d, exist_ok=True)
        probe = os.path.join(d, ".lt_probe")
        with open(probe, "w", encoding="utf-8") as f:
            f.write("ok")
        os.remove(probe)
        return True
    except OSError:
        return False


def path():
    """第一次调用时挑一个真能写的路径，之后一直用它。"""
    if _PICKED:
        return _PICKED[0]
    cands = candidates()
    for p in cands:
        if _writable(p):
            _PICKED.append(p)
            return p
    _PICKED.append(cands[-1])          # 都写不了也只能先记下来
    return _PICKED[-1]


def _ok(k, v):
    """手改坏的 JSON 不许把程序带崩：类型不对就丢，用默认值。"""
    want = type(DEFAULTS[k])
    if want is bool:
        return isinstance(v, bool)
    if k == "pos":
        return v is None or (isinstance(v, list) and len(v) == 2
                             and all(isinstance(n, int) for n in v))
    return isinstance(v, want) and not isinstance(v, bool)


def load():
    cfg = dict(DEFAULTS)
    try:
        with open(path(), "r", encoding="utf-8-sig") as f:  # 记事本会写 BOM
            raw = json.load(f)
        if isinstance(raw, dict):
            for k, v in raw.items():
                if k in DEFAULTS and _ok(k, v):
                    cfg[k] = v
    except FileNotFoundError:
        pass
    except Exception as e:  # noqa: BLE001
        print(f"[cfg] 读取失败，用默认值：{e}", flush=True)
    return cfg


def save(cfg):
    """写盘：固定临时名 + os.replace。

    不能用 tempfile.mkstemp——它在个别机器上（杀毒软件扫描新建文件）
    会卡住好几秒，而这个函数是界面线程调的，一卡就是"未响应"。
    """
    p = path()
    d = os.path.dirname(p)
    tmp = p + f".{os.getpid()}.tmp"
    try:
        os.makedirs(d, exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({k: cfg.get(k, DEFAULTS[k]) for k in DEFAULTS},
                      f, ensure_ascii=False, indent=2)
        for attempt in range(4):          # 杀毒可能短暂锁住刚写的临时文件
            try:
                os.replace(tmp, p)
                break
            except OSError:
                if attempt == 3:
                    raise
                time.sleep(0.06)
        if p not in _SAID:
            _SAID.add(p)
            print(f"[cfg] 写入 {p}", flush=True)
        return True
    except Exception as e:  # noqa: BLE001
        print(f"[cfg] 保存失败：{e}", flush=True)
        try:
            os.remove(tmp)
        except OSError:
            pass
        return False
