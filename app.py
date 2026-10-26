"""实时翻译字幕 · 桌面版

抓系统声音 → Whisper 识别英文 → Qwen 翻译 → 悬浮字幕。

采集 / 识别 / 翻译各跑一个线程，中间用有界队列：
翻译慢的时候只丢旧句子，绝不把采集拖住（旧版就卡在这里，每句丢 1–2 秒音频）。

Esc 退出，F3 鼠标穿透，F4 文字模式，可拖动。
"""
import collections
import ctypes
import os
import queue
import sys
import threading
import time
import tkinter as tk
import traceback

if getattr(sys, "frozen", False):
    BASE = os.path.dirname(sys.executable)
else:
    BASE = os.path.dirname(os.path.abspath(__file__))
os.environ["LT_MODELS"] = os.path.join(BASE, "models")
sys.path.insert(0, BASE)

import capture  # noqa: E402
import config  # noqa: E402
import engine  # noqa: E402
import fetch_models  # noqa: E402

GWL_EXSTYLE = -20
WS_EX_LAYERED = 0x00080000
WS_EX_TRANSPARENT = 0x00000020

# ---- 暖色体系：奶油底 + 赤陶强调 + 深墨字 + 暖棕辅 ----
C_BG = "#EFEAE0"        # 窗口底色（比卡片深一点，卡片"浮"在上面）
C_CARD = "#FAF9F5"      # 卡片
C_CARD_EDGE = "#E4DED2"  # 软边（代替描边）
C_CHIP = "#EDE7DB"      # 槽
C_INK = "#1F1F1F"       # 主文字
C_WARM = "#8A7E72"      # 次级文字
C_ACCENT = "#D97757"    # 赤陶
C_OK = "#6F8B67"        # 暖绿（状态灯）
C_WARN = "#C9A227"
C_ERR = "#C05B4D"
C_MUTE = "#B0A597"      # 很淡的说明字

W, H = 960, 250
PAD = 14
LEFT_W = 244
GAP = 12
KEY = "#FF00FE"   # 透明键色（文字模式下被抠掉）
R_OUT = 22      # 外圆角
R_CARD = 18     # 卡片（外 22 - 缝 4 = 同心）

# 阶段 → 状态灯颜色：一眼看出机器在干什么
STAGE_COLOR = {"采集中": C_OK, "识别": C_WARN, "翻译": C_ACCENT,
               "重连": C_ERR, "空闲": C_MUTE}

_TICK = [time.time()]        # 主线程最后一次心跳的时间


def _fmt_gb(b):
    return f"{b / (1024 ** 3):.2f} GB"


class DownloadWin:
    """首次启动的下载窗口：进度条 + 速率 + 剩余时间 + 取消。"""

    W, H = 560, 210

    def __init__(self, need):
        self.root = tk.Tk()
        self.root.title("首次启动 · 下载模型")
        self.root.configure(bg=C_BG)
        self.root.resizable(False, False)
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        self.root.geometry(f"{self.W}x{self.H}+{(sw - self.W) // 2}+{(sh - self.H) // 2}")
        self.root.attributes("-topmost", True)

        c = tk.Canvas(self.root, width=self.W, height=self.H, bg=C_BG,
                      highlightthickness=0, bd=0)
        c.pack()
        rr(c, 10, 10, self.W - 10, self.H - 10, 16, C_CARD)
        c.create_text(34, 44, anchor="w", text="首次启动：正在下载模型", fill=C_INK,
                      font=("Microsoft YaHei UI", 13, "bold"))
        c.create_text(34, 68, anchor="w",
                      text=f"识别 + 翻译模型约 {_fmt_gb(sum(n['bytes'] for n in need))}，"
                           f"只下一次，之后完全离线", fill=C_WARM,
                      font=("Microsoft YaHei UI", 9))
        self.bx0, self.by0, self.bx1, self.by1 = 34, 92, self.W - 34, 104
        rr(c, self.bx0, self.by0, self.bx1, self.by1, 6, C_CHIP)
        self.bar = rr(c, self.bx0, self.by0, self.bx0 + 4, self.by1, 6, C_ACCENT)
        self.t_pct = c.create_text(34, 128, anchor="w", text="0%", fill=C_INK,
                                   font=("Consolas", 12))
        self.t_info = c.create_text(self.W - 34, 128, anchor="e", text="连接中…",
                                    fill=C_WARM, font=("Microsoft YaHei UI", 9))
        self.t_file = c.create_text(34, 150, anchor="w", text="", fill=C_MUTE,
                                    font=("Consolas", 8))
        self.t_btn = c.create_text(self.W - 34, 176, anchor="e", text="取消",
                                   fill=C_WARM, font=("Microsoft YaHei UI", 9),
                                   tags=("BTN_CANCEL",))
        c.tag_bind("BTN_CANCEL", "<Button-1>", lambda e: self.cancel())

        self.stop = threading.Event()
        self.jobs = queue.Queue()
        self.ok = False
        self._last = (0, 1, "", 0.0)
        self._t0 = time.time()

    def cancel(self):
        self.stop.set()

    def _tick(self):
        try:
            while True:
                item = self.jobs.get_nowait()
                if item[0] == "done":
                    self.ok = bool(item[1])
                    self.root.destroy()
                    return
                self._last = item
        except queue.Empty:
            pass
        done, total, name, speed = self._last
        frac = 0.0 if not total else min(1.0, done / total)
        c = self.root.children["!canvas"]
        c.coords(self.bar, *self._rect_pts(self.bx0, self.by0,
                                           max(self.bx0 + 4, self.bx0 + (self.bx1 - self.bx0) * frac),
                                           self.by1, 6))
        c.itemconfig(self.t_pct, text=f"{frac * 100:.1f}%")
        mb = speed / 1048576
        eta = "" if mb < 0.05 or not total else \
            f" · 剩余 {max(1, int((total - done) / speed / 60))} 分钟"
        c.itemconfig(self.t_info, text=f"{_fmt_gb(done)} / {_fmt_gb(total)}"
                                       f" · {mb:.1f} MB/s{eta}")
        c.itemconfig(self.t_file, text=_cut(name, 56))
        self.root.after(150, self._tick)

    def _rect_pts(self, x0, y0, x1, y1, r):
        r = max(1, min(r, (x1 - x0) / 2, (y1 - y0) / 2))
        return [x0 + r, y0, x1 - r, y0, x1, y0, x1, y0 + r,
                x1, y1 - r, x1, y1, x1 - r, y1, x0 + r, y1,
                x0, y1, x0, y1 - r, x0, y0 + r, x0, y0]

    def run(self, models_dir):
        def work():
            try:
                ok = fetch_models.download_all(
                    models_dir,
                    report=lambda d, t, n, s: self.jobs.put((d, t, n, s)),
                    stop=self.stop)
                self.jobs.put(("done", ok))
            except Exception as e:  # noqa: BLE001
                print(f"[dl] 下载失败：{e}", flush=True)
                self.jobs.put(("done", False))

        threading.Thread(target=work, name="download", daemon=True).start()
        self.root.after(150, self._tick)
        self.root.mainloop()
        return self.ok


def _download_ui(need):
    win = DownloadWin(need)
    return win.run(os.path.join(BASE, "models"))


def _watchdog():
    """主线程 4 秒没动静就把它卡在哪个函数写进日志——界面卡死能留证据。"""
    last = 0.0
    while True:
        time.sleep(1.0)
        idle = time.time() - _TICK[0]
        if idle > 4.0 and time.time() - last > 10.0:
            last = time.time()
            print(f"[watchdog] 主线程 {idle:.1f}s 没有心跳，抓栈如下：", flush=True)
            main_id = threading.main_thread().ident
            for tid, fr in list(sys._current_frames().items()):
                print(f"--- 线程 {tid}{'（主线程）' if tid == main_id else ''} ---",
                      flush=True)
                traceback.print_stack(fr, file=sys.stdout)


def rr(c, x0, y0, x1, y1, r, fill, tag=None):
    pts = [x0 + r, y0, x1 - r, y0, x1, y0, x1, y0 + r,
           x1, y1 - r, x1, y1, x1 - r, y1, x0 + r, y1,
           x0, y1, x0, y1 - r, x0, y0 + r, x0, y0]
    kw = {"smooth": True, "fill": fill, "outline": ""}
    if tag:
        kw["tags"] = tag
    return c.create_polygon(pts, **kw)


def _cut(s, n):
    s = (s or "").replace("\n", " ")
    return s if len(s) <= n else s[:n - 1] + "…"


class Saver:
    """后台写配置：界面线程只管丢一份过来，慢盘/杀毒都卡不到界面。

    这机器上 config.save 曾在 mkstemp 里卡 5 秒，直接把窗口卡成"未响应"。
    """

    def __init__(self):
        self._cv = threading.Condition()
        self._cfg = None
        self._stop = False
        self._thread = threading.Thread(target=self._run, name="saver", daemon=True)
        self._thread.start()

    def put(self, cfg):
        with self._cv:
            self._cfg = dict(cfg)
            self._cv.notify()

    def _run(self):
        while True:
            with self._cv:
                while self._cfg is None and not self._stop:
                    self._cv.wait(0.5)
                if self._stop and self._cfg is None:
                    return
                cfg, self._cfg = self._cfg, None
            config.save(cfg)

    def flush(self, timeout=2.0):
        """退出前保证最后一版写下去，但最多等 timeout 秒，绝不无限等。"""
        end = time.time() + timeout
        while time.time() < end:
            with self._cv:
                if self._cfg is None:
                    return True
            time.sleep(0.02)
        return False

    def stop(self):
        self.flush()
        with self._cv:
            self._stop = True
            self._cv.notify()


def push_latest(q, item):
    """有界队列放最新一条：满了就挤掉最旧的，绝不阻塞生产者。"""
    try:
        q.put_nowait(item)
        return False
    except queue.Full:
        pass
    try:
        q.get_nowait()
    except queue.Empty:
        pass
    try:
        q.put_nowait(item)
    except queue.Full:
        pass
    return True


class Overlay:
    def __init__(self, q, cfg):
        self.q = q
        self.cfg = cfg
        self.pipe = None
        self.hist = collections.deque(maxlen=max(4, int(cfg.get("history", 6))))
        self.show_hist = False
        self._fade_steps = []
        self._st = {"state": "空闲", "asr": None, "mt": None,
                    "qs": 0, "qm": 0, "drop": 0}
        self.saver = Saver()

        self.root = tk.Tk()
        self.root.title("实时翻译")
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.configure(bg=C_BG)
        self.root.attributes("-alpha", 0.0)

        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        self.x = (sw - W) // 2
        self.y = sh - H - 46
        pos = cfg.get("pos")
        if pos and isinstance(pos, list) and -W + 120 < pos[0] < sw - 120 and 0 <= pos[1] < sh - 60:
            self.x, self.y = int(pos[0]), int(pos[1])
        self.root.geometry(f"{W}x{H}+{self.x}+{self.y + 10}")

        self.c = tk.Canvas(self.root, width=W, height=H, bg=C_BG,
                           highlightthickness=0, bd=0)
        self.c.pack()

        self._clear_job = None
        self._fade_job = None
        self._level = 0.0
        self._level_shown = 0.0
        self._count = 0
        self._first_partial = True
        self.minimal = bool(cfg.get("minimal", False)) and "--full" not in sys.argv
        self._passthrough = False
        self._hwnd = None
        self._drag = (0, 0)

        self._build()
        self._apply_fonts()
        self._apply_bilingual()
        self._animate_in()
        self._anim_level()

        for wdg in (self.root, self.c):
            wdg.bind("<Button-1>", self._drag_start)
            wdg.bind("<B1-Motion>", self._drag_move)
        self.root.bind("<Escape>", lambda e: self.quit())
        self.root.bind("<F3>", lambda e: self.toggle_click_through())
        self.root.bind("<F4>", lambda e: self.toggle_minimal())
        # Alt+F4 / 任务栏关闭也要走 quit()，否则位置字号不落盘
        self.root.protocol("WM_DELETE_WINDOW", self.quit)

        if self.minimal:
            self.root.after(600, self._force_minimal)

    # ------------------------------------------------------------ 绘制
    def _card(self, x0, y0, x1, y1, tag):
        """软阴影三层 + 卡片：没有描边，靠明度差分层。"""
        rr(self.c, x0 + 1, y0 + 2, x1 + 1, y1 + 2, R_CARD + 1, C_CARD_EDGE, tag)
        rr(self.c, x0, y0 + 1, x1, y1 + 1, R_CARD, "#F3EFE6", tag)
        return rr(self.c, x0, y0, x1, y1, R_CARD, C_CARD, tag)

    def _build(self):
        c = self.c
        x0, y0 = PAD, PAD
        x1, y1 = W - PAD, H - PAD

        # ---------------- 左卡片：状态 / 电平 / 统计
        self._card(x0, y0, x0 + LEFT_W, y1, "L")
        lx = x0 + 22
        c.create_text(lx, y0 + 28, anchor="w", text="实时翻译", fill=C_INK,
                      font=("SimSun", 15, "bold"), tags="L")
        self.dot = c.create_oval(lx, y0 + 43, lx + 7, y0 + 50, fill=C_WARN,
                                 outline="", tags="L")
        self.t_status = c.create_text(lx + 14, y0 + 47, anchor="w", text="启动中",
                                      fill=C_WARM, font=("Microsoft YaHei UI", 9),
                                      tags="L")

        bx0, bx1 = lx, x0 + LEFT_W - 22
        rr(c, bx0, y0 + 64, bx1, y0 + 70, 3, C_CHIP, "L")
        self.bar_fg = rr(c, bx0, y0 + 64, bx0 + 2, y0 + 70, 3, C_ACCENT, "L")
        self._bar = (bx0, bx1, y0 + 64, y0 + 70)

        # 实时任务状态：机器此刻在干什么
        self.t_stage = c.create_text(lx, y0 + 86, anchor="w", text="空闲",
                                     fill=C_WARM, font=("Microsoft YaHei UI", 8),
                                     tags="L")
        self.t_queue = c.create_text(lx, y0 + 100, anchor="w", text="队列 采0 译0",
                                     fill=C_MUTE, font=("Microsoft YaHei UI", 8),
                                     tags="L")

        self.t_count = c.create_text(lx, y0 + 128, anchor="w", text="0", fill=C_INK,
                                     font=("Consolas", 18), tags="L")
        c.create_text(lx, y0 + 148, anchor="w", text="已翻译", fill=C_WARM,
                      font=("Microsoft YaHei UI", 8), tags="L")
        self.t_lat = c.create_text(lx + 104, y0 + 128, anchor="w", text="--",
                                   fill=C_ACCENT, font=("Consolas", 18), tags="L")
        c.create_text(lx + 104, y0 + 148, anchor="w", text="本句耗时", fill=C_WARM,
                      font=("Microsoft YaHei UI", 8), tags="L")

        self.t_device = c.create_text(lx, y1 - 38, anchor="w", text="", fill=C_WARM,
                                      font=("Microsoft YaHei UI", 8), tags="L")
        c.create_text(lx, y1 - 20, anchor="w", text="F3 穿透 · F4 文字 · Esc 退出",
                      fill=C_MUTE, font=("Microsoft YaHei UI", 8), tags="L")

        # ---------------- 右卡片：字幕
        rx0 = x0 + LEFT_W + GAP
        self._card(rx0, y0, x1, y1, "R")
        self.tx = rx0 + 26
        self.tw = x1 - self.tx - 26
        self.t_en = c.create_text(self.tx, y0 + 34, anchor="nw", text="", width=self.tw,
                                  fill=C_WARM, font=("Segoe UI", cfg_en(self.cfg)),
                                  tags="R")
        self.t_zh = c.create_text(self.tx, y0 + 68, anchor="nw", text="正在加载模型…",
                                  width=self.tw, fill=C_INK,
                                  font=("Microsoft YaHei UI", cfg_zh(self.cfg)),
                                  tags="R")

        # 右上角控制条：字号 / 双语 / 历史 / 收起 / 关闭
        self._chip(x1 - 244, y0 + 14, "A−", "BTN_FMIN", lambda: self.font_step(-1))
        self._chip(x1 - 208, y0 + 14, "A+", "BTN_FPLUS", lambda: self.font_step(1))
        self.chip_bil = self._chip(x1 - 166, y0 + 14, "双", "BTN_BIL", self.toggle_bilingual)
        self.chip_his = self._chip(x1 - 122, y0 + 14, "史", "BTN_HIS", self.toggle_history)

        self.btn_min = c.create_text(x1 - 52, y0 + 24, text="⌄", fill=C_MUTE,
                                     font=("Segoe UI", 13), tags=("FULL", "BTN_MIN"))
        c.tag_bind("BTN_MIN", "<Button-1>", lambda e: self.toggle_minimal())
        self.btn_close = c.create_text(x1 - 26, y0 + 24, text="✕", fill=C_MUTE,
                                       font=("Segoe UI", 12), tags=("FULL", "BTN_CLOSE"))
        c.tag_bind("BTN_CLOSE", "<Button-1>", lambda e: self.quit())

        self._build_minimal()

        # 所有常规元件打上 FULL 标签，切模式时整组隐藏
        for it in self.c.find_all():
            if "M" not in self.c.gettags(it):
                self.c.addtag_withtag("FULL", it)
        self._sync_chips()

    def _chip(self, x, y, label, tag, cmd):
        """小圆角按钮：底色槽 + 文字，点击范围比纯文字大。"""
        w, h = 32, 19
        rr(self.c, x, y, x + w, y + h, 9, C_CHIP, (tag,))
        t = self.c.create_text(x + w / 2, y + h / 2, text=label, fill=C_WARM,
                               font=("Microsoft YaHei UI", 9), tags=(tag,))
        self.c.tag_bind(tag, "<Button-1>", lambda e: cmd())
        return {"box": (x, y, x + w, y + h), "tag": tag, "text": t}

    def _sync_chips(self):
        """开关类按钮：开着用赤陶字，关着用淡棕。"""
        for chip, on in ((self.chip_bil, self.cfg.get("bilingual", True)),
                         (self.chip_his, self.show_hist)):
            self.c.itemconfig(chip["text"], fill=C_ACCENT if on else C_MUTE)

    def _build_minimal(self):
        """文字模式：抠掉底色只留译文；下面垫一层暗底，避免 ClearType 彩边。"""
        c = self.c
        cx = W // 2
        self.m_pill = rr(c, 90, H - 156, W - 90, H - 26, 16, "#141414")
        c.itemconfigure(self.m_pill, state="hidden")
        c.addtag_withtag("M", self.m_pill)
        self.m_en = c.create_text(cx, H - 98, text="", anchor="s", justify="center",
                                  width=W - 220, fill="#CFC7B8",
                                  font=("Segoe UI", 12), state="hidden", tags="M")
        self.m_zh = c.create_text(cx, H - 52, text="", anchor="s", justify="center",
                                  width=W - 220, fill="#FFFFFF",
                                  font=("Microsoft YaHei UI", 23, "bold"),
                                  state="hidden", tags="M")
        self.m_close = c.create_text(W - 28, 26, text="✕", anchor="center",
                                     fill="#D9D2C4", font=("Segoe UI", 13),
                                     state="hidden", tags="M")
        self.m_restore = c.create_text(W - 56, 26, text="⌃", anchor="center",
                                       fill="#8E8678", font=("Segoe UI", 13),
                                       state="hidden", tags="M")
        c.tag_bind("M", "<Button-1>", self._on_min_click)

    def _on_min_click(self, event):
        if event.x > W - 44 and event.y < 52:
            self.quit()
        elif event.x > W - 72 and event.y < 52:
            self.toggle_minimal()

    # ------------------------------------------------------------ 字号 / 双语 / 历史
    def _apply_fonts(self):
        fz, fe = cfg_zh(self.cfg), cfg_en(self.cfg)
        self.c.itemconfig(self.t_zh, font=("Microsoft YaHei UI", fz))
        self.c.itemconfig(self.t_en, font=("Segoe UI", fe))
        self.c.itemconfig(self.m_zh, font=("Microsoft YaHei UI", fz + 2, "bold"))
        self.c.itemconfig(self.m_en, font=("Segoe UI", fe + 2))

    def font_step(self, d):
        fz = max(14, min(30, int(self.cfg["font_zh"]) + d))
        fe = max(8, min(16, int(self.cfg["font_en"]) + d))
        self.cfg["font_zh"], self.cfg["font_en"] = fz, fe
        self._apply_fonts()
        if self.show_hist:
            self._render_hist()
        self.saver.put(self.cfg)

    def toggle_bilingual(self):
        self.cfg["bilingual"] = not self.cfg.get("bilingual", True)
        self._apply_bilingual()
        self._sync_chips()
        self.saver.put(self.cfg)

    def _apply_bilingual(self):
        on = bool(self.cfg.get("bilingual", True))
        if not self.show_hist:
            self.c.itemconfigure(self.t_en, state="normal" if on else "hidden")
        if not self.minimal:
            self.c.itemconfigure(self.m_en, state="hidden")

    def toggle_history(self):
        self.show_hist = not self.show_hist
        if self.show_hist:
            self._render_hist()
        else:
            self.c.delete("HIS")
            self.c.itemconfigure(self.t_en, state="normal" if self.cfg.get("bilingual", True)
                                 else "hidden")
            self.c.itemconfigure(self.t_zh, state="normal")
        self._sync_chips()

    def _render_hist(self):
        """历史面板顶替实时字幕位：最近几条，旧的在上。"""
        c = self.c
        c.delete("HIS")
        c.itemconfigure(self.t_en, state="hidden")
        c.itemconfigure(self.t_zh, state="hidden")
        rows = list(self.hist)[-4:]
        if not rows:
            c.create_text(self.tx, PAD + 44, anchor="nw", text="还没有字幕",
                          width=self.tw, fill=C_MUTE,
                          font=("Microsoft YaHei UI", 10), tags=("FULL", "HIS"))
            return
        y = PAD + 40
        for en, zh in rows:
            c.create_text(self.tx, y, anchor="nw", text=_cut(en, 52), width=self.tw,
                          fill="#A99C8E", font=("Segoe UI", 9), tags=("FULL", "HIS"))
            c.create_text(self.tx, y + 18, anchor="nw", text=_cut(zh, 24), width=self.tw,
                          fill=C_INK, font=("Microsoft YaHei UI", cfg_zh(self.cfg) - 6),
                          tags=("FULL", "HIS"))
            y += 46
        if self.minimal:
            self.c.itemconfigure("HIS", state="hidden")

    # ------------------------------------------------------------ 动效
    def _animate_in(self):
        """淡入 0.42s + 缓缓上移，左右错开 70ms。"""
        self.c.move("L", 0, 10)
        self.c.move("R", 0, 10)

        def rise(tag, steps=6, delay=0):
            for i in range(steps):
                self.root.after(delay + i * 34,
                                lambda i=i, tag=tag: self.c.move(tag, 0, -10 / steps))

        rise("L", delay=0)
        rise("R", delay=70)
        for i in range(14):
            self.root.after(30 * i, lambda a=min(0.97, 0.97 * (i + 1) / 14):
                            self.root.attributes("-alpha", a))
        for delay, off in ((0, 10), (0.16, 6), (0.32, 3), (0.48, 1), (0.62, 0)):
            self.root.after(int(delay * 620), lambda o=off: self.root.geometry(
                f"{W}x{H}+{self.x}+{self.y + o}"))

    def _anim_level(self):
        _TICK[0] = time.time()          # 主线程心跳，看门狗用它判断是否卡死
        self._level_shown += (self._level - self._level_shown) * 0.32
        bx0, bx1, by0, by1 = self._bar
        wpx = max(2, int((bx1 - bx0) * self._level_shown))
        self.c.coords(self.bar_fg, *self._rect_pts(bx0, by0, bx0 + wpx, by1, 3))
        self.root.after(64, self._anim_level)

    def _rect_pts(self, x0, y0, x1, y1, r):
        r = min(r, (x1 - x0) / 2, (y1 - y0) / 2)
        return [x0 + r, y0, x1 - r, y0, x1, y0, x1, y0 + r,
                x1, y1 - r, x1, y1, x1 - r, y1, x0 + r, y1,
                x0, y1, x0, y1 - r, x0, y0 + r, x0, y0]

    # ------------------------------------------------------------ 交互
    def _force_minimal(self):
        if not self.minimal:
            self.toggle_minimal()

    def _drag_start(self, e):
        self._drag = (e.x_root - self.root.winfo_x(), e.y_root - self.root.winfo_y())

    def _drag_move(self, e):
        self.x = e.x_root - self._drag[0]
        self.y = e.y_root - self._drag[1]
        self.root.geometry(f"+{self.x}+{self.y}")

    def _get_hwnd(self):
        if self._hwnd is None:
            u = ctypes.windll.user32
            self._hwnd = u.GetParent(self.root.winfo_id()) or self.root.winfo_id()
        return self._hwnd

    def toggle_click_through(self):
        hwnd = self._get_hwnd()
        style = ctypes.windll.user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        if self._passthrough:
            style &= ~WS_EX_TRANSPARENT
            self.c.itemconfig(self.t_device, text="穿透已关")
        else:
            style |= WS_EX_TRANSPARENT | WS_EX_LAYERED
            self.c.itemconfig(self.t_device, text="穿透已开（F3 关）")
        ctypes.windll.user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style)
        self._passthrough = not self._passthrough

    def toggle_minimal(self):
        self.minimal = not self.minimal
        c = self.c
        if self.minimal:
            c.itemconfigure("FULL", state="hidden")
            c.itemconfigure("M", state="normal")
            c.configure(bg=KEY)
            try:
                self.root.wm_attributes("-transparentcolor", KEY)
                self.root.attributes("-alpha", 0.93)
            except tk.TclError:
                pass
            self._sync_min()
        else:
            c.itemconfigure("M", state="hidden")
            c.itemconfigure("FULL", state="normal")
            if self.show_hist:
                self._render_hist()
            try:
                self.root.wm_attributes("-transparentcolor", "")
                self.root.attributes("-alpha", 0.97)
            except tk.TclError:
                pass
            c.configure(bg=C_BG)
        self.cfg["minimal"] = self.minimal

    def _sync_min(self):
        c = self.c
        zh = c.itemcget(self.t_zh, "text")
        en = c.itemcget(self.t_en, "text") if self.cfg.get("bilingual", True) else ""
        c.itemconfigure(self.m_zh, text=zh)
        c.itemconfigure(self.m_en, text=en)
        for it in (self.m_pill, self.m_zh, self.m_close, self.m_restore):
            c.itemconfigure(it, state="normal" if self.minimal else "hidden")
        c.itemconfigure(self.m_en, state="normal" if self.minimal and en else "hidden")
        if not self.minimal:
            return
        # 底衬跟着文字大小走，别做成一块黑板
        c.update_idletasks()
        boxes = [b for b in (c.bbox(self.m_zh), c.bbox(self.m_en)) if b]
        if not boxes:
            return
        x0 = min(b[0] for b in boxes) - 34
        x1 = max(b[2] for b in boxes) + 34
        y0 = min(b[1] for b in boxes) - 22
        y1 = max(b[3] for b in boxes) + 22
        x0, x1 = max(8, x0), min(W - 8, x1)
        c.coords(self.m_pill, *self._rect_pts(x0, y0, x1, y1, 16))

    # ------------------------------------------------------------ 状态
    def set_status(self, text, color, device=None):
        self.c.itemconfig(self.t_status, text=text, fill=color)
        self.c.itemconfig(self.dot, fill=color)
        if device:
            self.c.itemconfig(self.t_device, text=device)

    def _render_stage(self):
        """实时任务状态：正在干什么 + 各段耗时 + 队列积压。"""
        st = self._st
        parts = [st.get("state") or "空闲"]
        if st.get("asr"):
            parts.append(f"识别 {st['asr']:.1f}s")
        if st.get("mt"):
            parts.append(f"翻译 {st['mt']:.1f}s")
        self.c.itemconfig(self.t_stage, text=" · ".join(parts))
        q = f"队列 采{st.get('qs', 0)} 译{st.get('qm', 0)}"
        if st.get("drop"):
            q += f" · 丢{st['drop']}"
        self.c.itemconfig(self.t_queue, text=q)
        if st.get("state") in STAGE_COLOR:
            self.c.itemconfig(self.dot, fill=STAGE_COLOR[st["state"]])

    def set_level(self, rms):
        self._level = min(1.0, rms * 16.0)

    # ------------------------------------------------------------ 字幕
    def _cancel_fades(self):
        """淡出的每一帧都要能取消，否则新句子会被涂成卡片色（白字白底）。"""
        for job in [self._clear_job, self._fade_job] + self._fade_steps:
            if job:
                try:
                    self.root.after_cancel(job)
                except Exception:  # noqa: BLE001
                    pass
        self._clear_job = self._fade_job = None
        self._fade_steps = []

    def partial(self, zh):
        self._cancel_fades()
        self._first_partial = False
        self.c.itemconfig(self.t_zh, text=zh)
        if self.show_hist:
            self._render_hist()
        elif self.minimal and zh:
            self._sync_min()

    def show(self, en, zh, latency=0.0):
        self._cancel_fades()
        self._first_partial = True

        self._count += 1
        self.c.itemconfig(self.t_count, text=str(self._count))
        self.c.itemconfig(self.t_lat, text=f"{latency:.1f}s")
        self.c.itemconfig(self.t_en, text=en)
        self.c.itemconfig(self.t_zh, text=zh, fill=C_INK)

        if len(self.hist) == 0 or self.hist[-1][1] != zh:
            self.hist.append((en, zh))
        if self.show_hist:
            self._render_hist()
        elif self.minimal:
            self._sync_min()
        self._clear_job = self.root.after(1100, self._fade_out)

    def _fade_out(self):
        steps = ["#1F1F1F", "#5C5C5C", "#8A7E72", "#B3A798", "#D8D0C2", C_CARD]
        for k, col in enumerate(steps):
            self._fade_steps.append(
                self.root.after(60 * k, lambda c=col: self._apply_fade(c)))
        self._clear_job = self.root.after(60 * len(steps) + 40, self._clear)

    def _apply_fade(self, col):
        self.c.itemconfig(self.t_zh, fill=col)
        self.c.itemconfig(self.t_en, fill=col)
        if self.minimal:
            mcol = "#FFFFFF" if col == "#1F1F1F" else "#B9B0A0"
            self.c.itemconfig(self.m_zh, fill=mcol)

    def _clear(self):
        self.c.itemconfig(self.t_zh, text="", fill=C_INK)
        self.c.itemconfig(self.t_en, text="", fill=C_WARM)
        self.c.itemconfig(self.m_zh, text="", fill="#FFFFFF")
        self.c.itemconfig(self.m_en, text="")

    def msg(self, text):
        self.c.itemconfig(self.t_zh, text=text, fill=C_WARM)

    def poll(self, q):
        try:
            while True:
                item = q.get_nowait()
                kind = item[0]
                if kind == "partial":
                    self.partial(item[1])
                elif kind == "pair":
                    self.show(item[1], item[2], item[3] if len(item) > 3 else 0.0)
                elif kind == "msg":
                    self.msg(item[1])
                elif kind == "status":
                    self.set_status(item[1], item[2], item[3] if len(item) > 3 else None)
                elif kind == "level":
                    self.set_level(item[1])
                elif kind == "stat":
                    self._st.update(item[1])
                    self._render_stage()
        except queue.Empty:
            pass
        self.root.after(70, self.poll, q)

    def quit(self):
        try:
            self.cfg["pos"] = [int(self.x), int(self.y)]
            self.cfg["minimal"] = self.minimal
            self.saver.put(self.cfg)
            self.saver.flush(2.0)
        except Exception:  # noqa: BLE001
            pass
        try:
            self.root.withdraw()          # 先让窗口消失，别让用户对着卡住的窗口
        except Exception:  # noqa: BLE001
            pass
        if self.pipe is not None:
            threading.Thread(target=self.pipe.stop, daemon=True).start()
        self.root.destroy()


def cfg_zh(cfg):
    return int(cfg.get("font_zh", 21))


def cfg_en(cfg):
    return int(cfg.get("font_en", 10))


class Pipeline:
    """采集 → 识别 → 翻译，三段各一个线程，中间都是有界队列。

    设计要点：谁慢谁丢，采集永远不被阻塞。
    """

    def __init__(self, q, cfg):
        self.q = q
        self.cfg = cfg
        self.cap = None
        self.asr = None
        self.mt = None
        self.seg_q = queue.Queue(maxsize=3)     # 识别积压最多 3 句
        self.txt_q = queue.Queue(maxsize=1)     # 待翻译只留最新一句
        self._stop = False
        self._last = ""
        self._pending = ""
        self._drop = 0
        self._lvl_ts = 0.0

    # -------------------------------------------------------------- 上报
    def stat(self, **kw):
        if "drop" not in kw and self._drop:
            kw["drop"] = self._drop
        push_latest(self.q, ("stat", kw))

    def status(self, text, color, device=None):
        self.q.put(("status", text, color, device))

    # -------------------------------------------------------------- 启动
    def start(self):
        threading.Thread(target=self._boot, name="boot", daemon=True).start()

    def _boot(self):
        try:
            self.status("校准中", C_WARN)
            self.cap = capture.Loopback()
            self.cap.on_level = self._level_cb
            self.cap.on_reconnect = self._reconnect_cb
            self.cap.calibrate()
            self.status("加载模型", C_WARN, self.cap.name)

            box = {}

            def _load(k, fn):
                try:
                    box[k] = fn()
                except Exception as e:  # noqa: BLE001
                    box[k] = e

            ta = threading.Thread(
                target=_load, args=("asr", lambda: engine.Asr(threads=int(self.cfg["asr_threads"]))),
                daemon=True)
            tm = threading.Thread(
                target=_load, args=("mt", lambda: engine.Mt(threads=int(self.cfg["mt_threads"]))),
                daemon=True)
            ta.start()
            tm.start()
            ta.join()
            tm.join()
            for k in ("asr", "mt"):
                if isinstance(box.get(k), Exception):
                    raise box[k]
            self.asr, self.mt = box["asr"], box["mt"]

            self.status("监听中", C_OK, f"{self.cap.name}  ·  模型就绪")
            self.stat(state="采集中", asr=None, mt=None)
            for fn in (self._cap_loop, self._asr_loop, self._mt_loop):
                threading.Thread(target=fn, daemon=True).start()
        except Exception as e:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            self.q.put(("msg", f"出错了：{e}"))
            self.status("异常", C_ERR)

    def _level_cb(self, rms):
        now = time.time()
        if now - self._lvl_ts < 0.08:       # UI 70ms 刷一次，别把队列喂爆
            return
        self._lvl_ts = now
        self.q.put(("level", rms))

    def _reconnect_cb(self, n):
        self.status(f"重连中 ({n})", C_ERR)
        self.stat(state="重连")

    # -------------------------------------------------------------- 采集
    def _cap_loop(self):
        try:
            for seg in self.cap.segments():
                if self._stop:
                    return
                if push_latest(self.seg_q, (seg, time.time())):
                    self._drop += 1
                    self.stat(drop=self._drop)
        except Exception as e:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            self.q.put(("msg", f"采集出错：{e}"))
            self.status("采集异常", C_ERR)

    # -------------------------------------------------------------- 识别
    def _asr_loop(self):
        while not self._stop:
            try:
                seg, ts = self.seg_q.get(timeout=0.5)
            except queue.Empty:
                self.stat(qs=0)
                continue
            lag = time.time() - ts
            if lag > 3.0:                    # 积压太久，追上去也跟不上字幕，丢掉
                self._drop += 1
                self.stat(qs=self.seg_q.qsize(), drop=self._drop)
                continue
            self.stat(state="识别", qs=self.seg_q.qsize())
            t0 = time.time()
            try:
                en = self.asr.listen(seg)
            except Exception as e:  # noqa: BLE001
                import traceback
                traceback.print_exc()
                self.stat(state="采集中", asr=None)
                self.q.put(("msg", f"识别失败：{e}"))
                continue
            cost = time.time() - t0
            print(f"[dbg] seg={len(seg) / 16000:.1f}s lag={lag:.2f}s asr={cost:.2f}s en={en!r}",
                  flush=True)
            self.stat(state="采集中", asr=cost, qs=self.seg_q.qsize())
            if not en:
                continue

            text = (self._pending + " " + en).strip() if self._pending else en
            if len(text.split()) < 4 and not text.endswith((".", "!", "?", ",")):
                self._pending = text
                continue
            self._pending = ""
            if len(text) < 3 or text.lower() == self._last:
                continue
            self._last = text.lower()
            print(f"[en] {text}", flush=True)
            if push_latest(self.txt_q, (text, ts)):
                self._drop += 1
                self.stat(drop=self._drop)

    # -------------------------------------------------------------- 翻译
    def _mt_loop(self):
        while not self._stop:
            try:
                text, ts = self.txt_q.get(timeout=0.5)
            except queue.Empty:
                self.stat(qm=0)
                continue
            self.stat(state="翻译", qm=self.txt_q.qsize())
            try:
                zh = self.mt.translate(text, on_partial=lambda t: self.q.put(("partial", t)))
            except Exception as e:  # noqa: BLE001
                import traceback
                traceback.print_exc()
                self.q.put(("msg", f"翻译服务异常：{e}"))
                self.status("翻译服务断开，重连中", C_ERR)
                if self.mt.ensure_alive():
                    self.status("监听中", C_OK, f"{self.cap.name}  ·  已重连")
                else:
                    self.status("翻译服务丢失", C_ERR)
                self.stat(state="采集中", mt=None)
                continue
            cost = time.time() - ts
            print(f"[zh] {zh}   ({cost:.2f}s)", flush=True)
            self.stat(state="采集中", mt=cost, qm=self.txt_q.qsize())
            if not zh:
                continue
            self.q.put(("pair", text, zh, cost))

    # -------------------------------------------------------------- 退出
    def stop(self):
        self._stop = True
        try:
            if self.mt is not None:
                self.mt.close()
        except Exception:  # noqa: BLE001
            pass


def _selftest():
    """--selftest：不开采集，依次点一遍界面按钮并计时，用来判断界面会不会卡。"""
    cfg = config.load()
    print(f"[selftest] 配置路径 {config.path()}", flush=True)
    q = queue.Queue()
    ov = Overlay(q, cfg)
    ov.root.update()
    steps = [("A+ 字号", lambda: ov.font_step(1)), ("A+ 字号", lambda: ov.font_step(1)),
             ("A− 字号", lambda: ov.font_step(-1)),
             ("双 双语", ov.toggle_bilingual), ("双 双语", ov.toggle_bilingual),
             ("史 历史", ov.toggle_history), ("史 历史", ov.toggle_history),
             ("⌄ 文字模式", ov.toggle_minimal), ("⌄ 文字模式", ov.toggle_minimal)]
    for name, fn in steps:
        t0 = time.time()
        fn()
        ov.root.update()
        print(f"[selftest] {name} 耗时 {(time.time() - t0) * 1000:.0f}ms", flush=True)
    t0 = time.time()
    ov.quit()
    print(f"[selftest] 退出耗时 {(time.time() - t0) * 1000:.0f}ms", flush=True)
    print("[selftest] OK", flush=True)


def main():
    if sys.stdout is None:
        log = open(os.path.join(BASE, "app.log"), "a", encoding="utf-8", buffering=1)
        sys.stdout = sys.stderr = log

    if "--selftest" in sys.argv:
        _selftest()
        return

    models = os.path.join(BASE, "models")
    lack = fetch_models.missing(models)
    if lack:
        print(f"[dl] 缺模型：{sum(len(n['files']) for n in lack)} 个文件，"
              f"约 {sum(n['bytes'] for n in lack) / (1024 ** 3):.2f} GB", flush=True)
        if not _download_ui(lack):
            print("[dl] 模型没下完，程序退出", flush=True)
            return
        print("[dl] 模型就绪", flush=True)
    elif "--download-models" in sys.argv:
        print("[dl] 模型齐全，不用下载", flush=True)

    threading.Thread(target=_watchdog, name="watchdog", daemon=True).start()

    cfg = config.load()
    print(f"[cfg] 载入 字号={cfg['font_zh']}/{cfg['font_en']} 双语={cfg['bilingual']} "
          f"文字模式={cfg['minimal']} 位置={cfg['pos']} ← {config.path()}", flush=True)
    q = queue.Queue()
    ov = Overlay(q, cfg)
    print("[ui] 窗口已建，开始起流水线", flush=True)
    pipe = Pipeline(q, cfg)
    ov.pipe = pipe
    pipe.start()
    ov.poll(q)
    print("[ui] 进入事件循环", flush=True)
    ov.root.mainloop()
    print("[ui] 事件循环结束，退出", flush=True)


if __name__ == "__main__":
    main()
