"""首次运行自动下载模型（从 ModelScope，国内直连快）。

被 app.py 调用：程序启动时检查 models 是否齐全，缺就在小窗口里下载，
下完继续正常启动。也可以命令行强制跑：LiveTranslate.exe --download-models

为什么走 ModelScope：HuggingFace 在国内大文件经常被重置，Xet 协议还会卡死。
"""
import os
import time
import urllib.error
import urllib.parse
import urllib.request

URL = ("https://www.modelscope.cn/api/v1/models/{repo}/repo"
       "?Revision=master&FilePath={file}")

# (ModelScope 仓库, 落到 models 下的子目录, 需要的文件, 大致字节数)
SPECS = [
    ("systran/faster-whisper-small.en", "whisper_ct2",
     ["config.json", "model.bin", "tokenizer.json", "vocabulary.txt"],
     484 * 1024 * 1024),
    # 逐词模式的快模型：只用来判断「词来了」，整句收尾仍用 small.en
    ("systran/faster-whisper-tiny.en", "whisper_tiny_ct2",
     ["config.json", "model.bin", "tokenizer.json", "vocabulary.txt"],
     78 * 1024 * 1024),
    ("Qwen/Qwen2.5-3B-Instruct-GGUF", "qwen",
     ["qwen2.5-3b-instruct-q4_0.gguf"],
     1905 * 1024 * 1024),
]

# 键写成「子目录/文件名」：两个 whisper 模型都有 model.bin，不能只按文件名卡大小
MIN_SIZE = {
    "whisper_ct2/model.bin": 400 << 20,
    "whisper_tiny_ct2/model.bin": 50 << 20,
    "qwen/qwen2.5-3b-instruct-q4_0.gguf": 1500 << 20,
}


def _ok(path, name, sub=""):
    """文件在、而且不是个半截货（模型文件按已知大小卡一下）。"""
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        return False
    need = MIN_SIZE.get(f"{sub}/{name}" if sub else name)
    return True if need is None else os.path.getsize(path) >= need


def missing(models_dir):
    """返回还缺哪些文件：[{repo, sub, files, bytes}]；齐全就返回空列表。"""
    out = []
    for repo, sub, files, approx in SPECS:
        d = os.path.join(models_dir, sub)
        lack = [f for f in files if not _ok(os.path.join(d, f), f, sub)]
        if lack:
            out.append({"repo": repo, "sub": sub, "files": lack, "bytes": approx})
    return out


def total_bytes(models_dir):
    return sum(s[3] for s in SPECS) if missing(models_dir) else 0


def _url(repo, name):
    return URL.format(repo=urllib.parse.quote(repo, safe="/"),
                      file=urllib.parse.quote(name))


def _download_one(repo, name, dest_path, done_before, grand_total, report, stop):
    """断点续传式下载单个文件；report(已传总字节, 总字节, 文件名, 速度)。"""
    part = dest_path + ".part"
    have = os.path.getsize(part) if os.path.exists(part) else 0
    req = urllib.request.Request(_url(repo, name))
    if have:
        req.add_header("Range", f"bytes={have}-")
    t0 = time.time()
    try:
        resp = urllib.request.urlopen(req, timeout=60)
    except urllib.error.HTTPError as e:
        if e.code in (416, 501) and have:      # 服务器不认 Range，从头来
            have = 0
            os.remove(part)
            resp = urllib.request.urlopen(urllib.request.Request(_url(repo, name)),
                                          timeout=60)
        else:
            raise
    if have and getattr(resp, "status", 200) != 206:
        have = 0                                   # 没给 206 就别续了
    mode = "ab" if have else "wb"
    got = have
    os.makedirs(os.path.dirname(dest_path), exist_ok=True)
    with resp, open(part, mode) as f:
        while True:
            if stop is not None and stop.is_set():
                raise RuntimeError("已取消")
            chunk = resp.read(1 << 20)
            if not chunk:
                break
            f.write(chunk)
            got += len(chunk)
            if report is not None:
                el = max(0.2, time.time() - t0)
                report(done_before + got, grand_total, name, got / el)
    os.replace(part, dest_path)
    return done_before + got


def download_all(models_dir, report=None, stop=None):
    """把缺的模型都下下来。report(已传, 总字节, 文件名, 字节/秒)，可从别的线程调。"""
    todo = missing(models_dir)
    if not todo:
        return True
    grand_total = sum(t["bytes"] for t in todo)
    done = 0
    for t in todo:
        for name in t["files"]:
            dest = os.path.join(models_dir, t["sub"], name)
            if _ok(dest, name):
                continue
            done = _download_one(t["repo"], name, dest, done, grand_total,
                                 report, stop)
    return not missing(models_dir)
