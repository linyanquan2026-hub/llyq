"""从 ModelScope 拉模型（HF 在国内下不动，ModelScope 直连很快）。

whisper: systran/faster-whisper-small.en  (CTranslate2 格式)
opus  : Helsinki-NLP/opus-mt-en-zh        (MarianMT 格式)
"""
import json
import os
import sys
import time
import urllib.parse
import urllib.request

BASE = os.path.dirname(os.path.abspath(__file__))
MODELS = os.path.join(BASE, "models")
API = "https://www.modelscope.cn/api/v1/models/{repo}/repo?Revision=master&FilePath={file}"
LIST = "https://www.modelscope.cn/api/v1/models/{repo}/repo/files?Revision=master&Root="

TARGETS = [
    ("systran/faster-whisper-small.en", "whisper_ct2",
     {"config.json", "model.bin", "tokenizer.json", "vocabulary.txt", "preprocessor_config.json"}),
    ("Helsinki-NLP/opus-mt-en-zh", "opus_en_zh",
     {"config.json", "source.spm", "target.spm", "vocab.json",
      "tokenizer_config.json", "generation_config.json", "special_tokens_map.json", "pytorch_model.bin"}),
]


def list_files(repo):
    with urllib.request.urlopen(LIST.format(repo=urllib.parse.quote(repo, safe="/")), timeout=30) as r:
        data = json.load(r)
    return [f["Name"] for f in data["Data"]["Files"]]


def download(repo, dest, want):
    os.makedirs(dest, exist_ok=True)
    try:
        available = set(list_files(repo))
    except Exception as e:
        print(f"[warn] 列目录失败({e})，按常用文件表下", flush=True)
        available = set(want)
    files = sorted((available & want) or available)
    for name in files:
        out = os.path.join(dest, name)
        if os.path.exists(out) and os.path.getsize(out) > 0:
            print(f"  跳过 {name}（已存在）", flush=True)
            continue
        url = API.format(repo=urllib.parse.quote(repo, safe="/"), file=urllib.parse.quote(name))
        t0 = time.time()
        with urllib.request.urlopen(url, timeout=60) as r, open(out + ".part", "wb") as f:
            total = 0
            while True:
                chunk = r.read(1 << 20)
                if not chunk:
                    break
                f.write(chunk)
                total += len(chunk)
                if total % (20 << 20) < (1 << 20):
                    print(f"  {name}: {total / 1048576:.0f} MB  {total / 1048576 / max(0.1, time.time() - t0):.1f} MB/s", flush=True)
        os.replace(out + ".part", out)
        print(f"  完成 {name}  {os.path.getsize(out) / 1048576:.1f} MB  {time.time() - t0:.1f}s", flush=True)


def main():
    only = sys.argv[1] if len(sys.argv) > 1 else None
    for repo, sub, want in TARGETS:
        if only and only not in sub:
            continue
        print(f"== {repo} -> models/{sub}", flush=True)
        download(repo, os.path.join(MODELS, sub), want)
    print("全部完成", flush=True)


if __name__ == "__main__":
    main()
