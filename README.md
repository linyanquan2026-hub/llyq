# 实时翻译字幕（全离线）

把电脑正在播放的**英文声音**抓下来，实时识别并翻成中文，显示在屏幕下方的悬浮字幕条上。

```
系统声音(WASAPI 回环) → 流式快照 → Whisper tiny.en(定词) / small.en(定句) → Qwen2.5-3B → 悬浮字幕
                        能量VAD     两次解码一致才认(LocalAgreement-2)        逐句重译
```

全离线：不联网、不花钱、声音不出本机。

## 逐词模式（默认开）

不等一句话说完：说话过程中每 0.45 秒把「到目前为止」的音频送去识别一次，
两次解码都对得上的词立即翻成中文上屏，端到端约 **0.3–0.6 秒**；
一句说完再用 small.en 重解、整句重译，换成通顺的一整句。

两层模型的原因：small.en 单次解码固定约 1.5 秒（Whisper 编码器固定吃 30 秒窗口，
跟音频长短无关），拿来做流式永远慢一拍；tiny.en 同样条件只要 0.2 秒。

配置（`%APPDATA%\LiveTranslate\config.json`）：

| 键 | 默认 | 作用 |
|---|---|---|
| `word_mode` | `true` | `false` = 退回老的一整句模式 |
| `word_tick` | `0.45` | 流式识别间隔（秒） |
| `word_delta` | `false` | `true` = 逐词拼接（只增不改、中文生硬）；默认整句重译（通顺、个别词会改） |

## 用法

**方式一（推荐）**：双击 `dist\LiveTranslate\LiveTranslate.exe`
**方式二**：双击 `启动.vbs`（走本机 Python，不打包）

- **Esc** 退出
- **F3** 切换鼠标穿透（穿透后点击落到下面窗口，适合全屏看视频；再按 F3 恢复）
- 按住字幕条可以拖动

启动到能翻字幕约 6 秒；逐词模式第一个词约 0.5 秒上屏，整句定稿 2 秒内。

## 目录

```
live-translate\
  app.py               主程序（悬浮窗 + 调度）
  capture.py           WASAPI 回环录音 + 能量分句
  engine.py            Whisper 识别 + Qwen 翻译（HTTP 调 llama-server）
  download_models.py   从 ModelScope 拉识别模型
  selftest.py          wav 离线自测
  selftest_capture.py  回环录音自测
  models\
    whisper_ct2\       faster-whisper small.en（CTranslate2，int8，464MB，定句用）
    whisper_tiny_ct2\  faster-whisper tiny.en（75MB，逐词定词用）
    qwen\              Qwen2.5-3B-Instruct q4_0 GGUF（1.9GB）
  llama\               llama.cpp b11887 官方 Windows CPU 二进制
  tools\               流式自测：stream_check.py（喂 wav）/ stream_live_check.py（真放音）
  dist\LiveTranslate\  打包好的 exe（模型 + llama 已复制进去，约 2.6GB）
```

## 架构要点

- 识别：`faster-whisper`（CTranslate2，int8，CPU）；small.en 单次约 1.5 秒（定句），tiny.en 约 0.2 秒（定词）
- 定词：`engine.StreamAsr`，两次解码词级公共前缀一致才提交（LocalAgreement-2），避免中间字幕来回改
- 翻译：**llama-server 子进程 + HTTP**，不是 python 绑定。
  `Mt.__init__` 会在 8123/8124/8125 上拉起 `llama/llama-server.exe`，等 `/health` 就绪；
  程序退出时 atexit 杀掉它。
- 为什么不用 llama-cpp-python：PyPI 没有 Windows 轮子，要本地编译 CMake/MSVC，太脆。
- 为什么不用 torch 版 OPUS：PyInstaller 分析 transformers 会卡十几分钟，而且包大 700MB。

## 换语言 / 换模型

- 日文：`engine.Asr.listen` 里 `language="en"` → `"ja"`，识别模型换多语种 `small`
- 换更大模型：把 `models\qwen` 换成 7B GGUF，`engine.Mt.__init__` 里的文件名改一下即可（16GB 内存会紧）

## 已知限制

- 只翻**英文→中文**
- 识别错 → 翻译跟着错（这是级联误差，改不了，只能换更大的识别模型）
- 背景音乐大时会误触发识别
- 纯 CPU：识别占 2–3 核，翻译再占 2–3 核

## 踩过的坑（改代码前先看）

1. `pypi.org` / 清华源在这台机器上不通，用**阿里云源**；HuggingFace 大文件会被重置，模型走 **ModelScope**
2. huggingface_hub 的 Xet 协议在镜像上会卡死 → `HF_HUB_DISABLE_XET=1`
3. Whisper 对**静音片段会瞎编**（"you"/"thank you"）→ 分句按"有声占比"过滤，识别结果按 `no_speech_prob`/`avg_logprob` 过滤
4. PyInstaller 打包前**必须关掉正在跑的 exe**，否则 `_internal` 被锁，构建报 PermissionError，dist 里留着旧版
5. CT2 转换版 Marian（opus_en_zh_ct2）能跑但**译文退化成重复字**，已弃用
6. 重新打包后 models/ 和 llama/ 会被清掉，要重新拷进 `dist\LiveTranslate\`
