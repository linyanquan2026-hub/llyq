"""实时翻译字幕 · 桌面版

抓系统声音 → Whisper 识别英文 → Qwen 翻译 → 悬浮字幕。
Esc 退出，F3 切换鼠标穿透，按住拖动。
"""
import ctypes
import os
import queue
import sys
import threading
import time
import tkinter as tk

if getattr(sys, "frozen", False):
    BASE = os.path.dirname(sys.executable)
else:
    BASE = os.path.dirname(os.path.abspath(__file__))
os.environ["LT_MODELS"] = os.path.join(BASE, "models")
sys.path.insert(0, BASE)

import capture  # noqa: E402
import engine  # noqa: E402

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

W, H = 960, 250
PAD = 14
LEFT_W = 244
GAP = 12
KEY = "#FF00FE"   # 透明键色（文字模式下被抠掉）
R_OUT = 22      # 外圆角
R_CARD = 18     # 卡片（外 22 - 缝 4 = 同心）


def rr(c, x0, y0, x1, y1, r, fill, tag=None):
    pts = [x0 + r, y0, x1 - r, y0, x1, y0, x1, y0 + r,
           x1, y1 - r, x1, y1, x1 - r, y1, x0 + r, y1,
           x0, y1, x0, y1 - r, x0, y0 + r, x0, y0]
    kw = {"smooth": True, "fill": fill, "outline": ""}
    if tag:
        kw["tags"] = tag
    return c.create_polygon(pts, **kw)


class Overlay:
    def __init__(self):
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
        self.minimal = False
        self._passthrough = False
        self._hwnd = None
        self._drag = (0, 0)

        self._build()
        self._animate_in()
        self._anim_level()

        for wdg in (self.root, self.c):
            wdg.bind("<Button-1>", self._drag_start)
            wdg.bind("<B1-Motion>", self._drag_move)
        self.root.bind("<Escape>", lambda e: self.quit())
        self.root.bind("<F3>", lambda e: self.toggle_click_through())
        self.root.bind("<F4>", lambda e: self.toggle_minimal())

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

        # 左卡片
        self._card(x0, y0, x0 + LEFT_W, y1, "L")
        lx = x0 + 22
        c.create_text(lx, y0 + 30, anchor="w", text="实时翻译", fill=C_INK,
                      font=("SimSun", 15, "bold"), tags="L")
        self.dot = c.create_oval(lx, y0 + 52, lx + 7, y0 + 59, fill=C_WARN,
                                 outline="", tags="L")
        self.t_status = c.create_text(lx + 14, y0 + 55, anchor="w", text="启动中",
                                      fill=C_WARM, font=("Microsoft YaHei UI", 9),
                                      tags="L")

        bx0, bx1 = lx, x0 + LEFT_W - 22
        rr(c, bx0, y0 + 76, bx1, y0 + 82, 3, C_CHIP, "L")
        self.bar_fg = rr(c, bx0, y0 + 76, bx0 + 2, y0 + 82, 3, C_ACCENT, "L")
        self._bar = (bx0, bx1)

        self.t_count = c.create_text(lx, y0 + 112, anchor="w", text="0", fill=C_INK,
                                     font=("Consolas", 19), tags="L")
        c.create_text(lx, y0 + 134, anchor="w", text="已翻译", fill=C_WARM,
                      font=("Microsoft YaHei UI", 8), tags="L")
        self.t_lat = c.create_text(lx + 104, y0 + 112, anchor="w", text="--",
                                   fill=C_ACCENT, font=("Consolas", 19), tags="L")
        c.create_text(lx + 104, y0 + 134, anchor="w", text="本句耗时", fill=C_WARM,
                      font=("Microsoft YaHei UI", 8), tags="L")

        self.t_device = c.create_text(lx, y1 - 42, anchor="w", text="", fill=C_WARM,
                                      font=("Microsoft YaHei UI", 8), tags="L")
        c.create_text(lx, y1 - 24, anchor="w", text="F3 穿透 · Esc 退出 · 可拖动",
                      fill="#B0A597", font=("Microsoft YaHei UI", 8), tags="L")

        # 右卡片
        rx0 = x0 + LEFT_W + GAP
        self._card(rx0, y0, x1, y1, "R")
        tx = rx0 + 26
        tw = x1 - tx - 26
        self.t_en = c.create_text(tx, y0 + 34, anchor="nw", text="", width=tw,
                                  fill=C_WARM, font=("Segoe UI", 10), tags="R")
        self.t_zh = c.create_text(tx, y0 + 68, anchor="nw", text="正在加载模型…",
                                  width=tw, fill=C_INK,
                                  font=("Microsoft YaHei UI", 21), tags="R")

        # 右上角：收起成纯文字 / 关闭
        self.btn_min = c.create_text(x1 - 52, y0 + 24, text="⌄", fill="#B0A597",
                                     font=("Segoe UI", 13), tags=("FULL", "BTN_MIN"))
        c.tag_bind("BTN_MIN", "<Button-1>", lambda e: self.toggle_minimal())
        self.btn_close = c.create_text(x1 - 26, y0 + 24, text="✕", fill="#B0A597",
                                       font=("Segoe UI", 12), tags=("FULL", "BTN_CLOSE"))
        c.tag_bind("BTN_CLOSE", "<Button-1>", lambda e: self.quit())

        self._build_minimal()

        # 所有常规元件打上 FULL 标签，切模式时整组隐藏
        for it in self.c.find_all():
            if "M" not in self.c.gettags(it):
                self.c.addtag_withtag("FULL", it)

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
            try:
                self.root.wm_attributes("-transparentcolor", "")
                self.root.attributes("-alpha", 0.97)
            except tk.TclError:
                pass
            c.configure(bg=C_BG)

    def _sync_min(self):
        c = self.c
        zh = c.itemcget(self.t_zh, "text")
        en = c.itemcget(self.t_en, "text")
        c.itemconfigure(self.m_zh, text=zh)
        c.itemconfigure(self.m_en, text=en)
        for it in (self.m_pill, self.m_zh, self.m_en, self.m_close, self.m_restore):
            c.itemconfigure(it, state="normal" if self.minimal else "hidden")
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
        self._level_shown += (self._level - self._level_shown) * 0.32
        bx0, bx1 = self._bar
        wpx = max(2, int((bx1 - bx0) * self._level_shown))
        self.c.coords(self.bar_fg, *self._rect_pts(bx0, PAD + 76, bx0 + wpx,
                                                   PAD + 82, 3))
        self.root.after(64, self._anim_level)

    def _rect_pts(self, x0, y0, x1, y1, r):
        r = min(r, (x1 - x0) / 2, (y1 - y0) / 2)
        return [x0 + r, y0, x1 - r, y0, x1, y0, x1, y0 + r,
                x1, y1 - r, x1, y1, x1 - r, y1, x0 + r, y1,
                x0, y1, x0, y1 - r, x0, y0 + r, x0, y0]

    # ------------------------------------------------------------ 交互
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

    # ------------------------------------------------------------ 状态
    def set_status(self, text, color, device=None):
        self.c.itemconfig(self.t_status, text=text, fill=color)
        self.c.itemconfig(self.dot, fill=color)
        if device:
            self.c.itemconfig(self.t_device, text=device)

    def set_level(self, rms):
        self._level = min(1.0, rms * 16.0)

    # ------------------------------------------------------------ 字幕
    def partial(self, zh):
        for job in (self._clear_job, self._fade_job):
            if job:
                self.root.after_cancel(job)
        self._clear_job = self._fade_job = None
        self.c.itemconfig(self.t_zh, text=zh)
        if self.minimal and zh:
            self._sync_min()
        if self._first_partial:
            # 晕染：先落一层浅墨，再收成深墨
            self._first_partial = False
            self.c.itemconfig(self.t_zh, fill="#B3A798")
            self.root.after(130, lambda: self.c.itemconfig(self.t_zh, fill=C_INK))

    def show(self, en, zh, latency=0.0):
        for job in (self._clear_job, self._fade_job):
            if job:
                self.root.after_cancel(job)
        self._clear_job = self._fade_job = None
        self._first_partial = True

        self._count += 1
        self.c.itemconfig(self.t_count, text=str(self._count))
        self.c.itemconfig(self.t_lat, text=f"{latency:.1f}s")
        self.c.itemconfig(self.t_en, text=en)
        self.c.itemconfig(self.t_zh, text=zh, fill=C_INK)
        if self.minimal:
            self._sync_min()
        self._clear_job = self.root.after(1100, self._fade_out)

    def _fade_out(self):
        steps = ["#1F1F1F", "#5C5C5C", "#8A7E72", "#B3A798", "#D8D0C2", C_CARD]
        for k, col in enumerate(steps):
            self.root.after(60 * k, lambda c=col: self._apply_fade(c))
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
        except queue.Empty:
            pass
        self.root.after(70, self.poll, q)

    def quit(self):
        self.root.destroy()


def worker(q):
    try:
        q.put(("status", "校准中", C_WARN, ""))
        cap = capture.Loopback()
        cap.calibrate()
        q.put(("status", "加载模型", C_WARN, cap.name))

        box = {}

        def _load(k, fn):
            try:
                box[k] = fn()
            except Exception as e:  # noqa: BLE001
                box[k] = e

        ta = threading.Thread(target=_load, args=("asr", engine.Asr), daemon=True)
        tm = threading.Thread(target=_load, args=("mt", engine.Mt), daemon=True)
        ta.start()
        tm.start()
        ta.join()
        tm.join()
        for k in ("asr", "mt"):
            if isinstance(box.get(k), Exception):
                raise box[k]
        asr, mt = box["asr"], box["mt"]
        q.put(("status", "监听中", C_OK, f"{cap.name}  ·  模型就绪"))

        last, pending = "", ""
        for seg in cap.segments():
            q.put(("level", cap.rms))
            t0 = time.time()
            en = asr.listen(seg)
            print(f"[dbg] seg={len(seg) / 16000:.1f}s rms={float((seg ** 2).mean() ** 0.5):.4f} en={en!r}",
                  flush=True)
            if not en:
                continue
            text = (pending + " " + en).strip() if pending else en
            if len(text.split()) < 4 and not text.endswith((".", "!", "?", ",")):
                pending = text
                continue
            pending = ""
            if len(text) < 3 or text.lower() == last:
                continue
            last = text.lower()
            print(f"[en] {text}", flush=True)
            zh = mt.translate(text, on_partial=lambda t: q.put(("partial", t)))
            if not zh:
                continue
            print(f"[zh] {zh}   ({time.time() - t0:.2f}s)", flush=True)
            q.put(("pair", text, zh, time.time() - t0))
    except Exception as e:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        q.put(("msg", f"出错了：{e}"))
        q.put(("status", "异常", C_ERR, ""))


def main():
    if sys.stdout is None:
        log = open(os.path.join(BASE, "app.log"), "a", encoding="utf-8", buffering=1)
        sys.stdout = sys.stderr = log

    q = queue.Queue()
    ov = Overlay()
    if "--minimal" in sys.argv:
        ov.root.after(600, ov.toggle_minimal)
    threading.Thread(target=worker, args=(q,), daemon=True).start()
    ov.poll(q)
    ov.root.mainloop()


if __name__ == "__main__":
    main()
