"""从 ModelScope 下载 Qwen2.5-3B-Instruct GGUF（q4_0，1.9GB）。

单独放一个文件是因为 download_models.py 只管识别模型。
"""
import os
import sys
import time
import urllib.parse
import urllib.request

BASE = os.path.dirname(os.path.abspath(__file__))
REPO = "Qwen/Qwen2.5-3B-Instruct-GGUF"
NAME = "qwen2.5-3b-instruct-q4_0.gguf"
DEST = os.path.join(BASE, "models", "qwen")
URL = ("https://www.modelscope.cn/api/v1/models/{repo}/repo"
       "?Revision=master&FilePath={file}")


def main():
    os.makedirs(DEST, exist_ok=True)
    out = os.path.join(DEST, NAME)
    if os.path.exists(out) and os.path.getsize(out) > 1_000_000_000:
        print(f"已存在 {os.path.getsize(out) / 1048576:.0f} MB，跳过")
        return
    url = URL.format(repo=urllib.parse.quote(REPO, safe="/"),
                     file=urllib.parse.quote(NAME))
    t0 = time.time()
    total = 0
    with urllib.request.urlopen(url, timeout=60) as r, open(out + ".part", "wb") as f:
        while True:
            chunk = r.read(1 << 20)
            if not chunk:
                break
            f.write(chunk)
            total += len(chunk)
            if total % (200 << 20) < (1 << 20):
                print(f"  {total / 1048576:.0f} MB  "
                      f"{total / 1048576 / max(0.1, time.time() - t0):.1f} MB/s", flush=True)
    os.replace(out + ".part", out)
    print(f"完成 {os.path.getsize(out) / 1048576:.0f} MB，用时 {time.time() - t0:.0f}s")


if __name__ == "__main__":
    sys.exit(main())
