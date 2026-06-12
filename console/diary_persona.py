import json
import math
import os
import tkinter as tk
from tkinter import ttk, messagebox

from . import PROJECT_ROOT


class DiaryPersonaTab(ttk.Frame):
    def __init__(self, parent):
        super().__init__(parent)
        self.build()

    def build(self):
        left_frame = ttk.LabelFrame(self, text="日记", padding=5)
        left_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=5, pady=5)

        self.diary_list = tk.Listbox(left_frame, width=18)
        self.diary_list.pack(side=tk.LEFT, fill=tk.Y)
        self.diary_list.bind("<<ListboxSelect>>", self._on_diary_select)

        self.diary_text = tk.Text(left_frame, height=20, wrap=tk.WORD)
        self.diary_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=5)

        diary_dir = os.path.join(PROJECT_ROOT, "diary")
        try:
            diary_files = sorted(
                [f for f in os.listdir(diary_dir) if f.endswith(".md")], reverse=True)
            for f in diary_files:
                self.diary_list.insert(tk.END, f)
        except Exception as e:
            self.diary_list.insert(tk.END, f"(错误: {e})")

        right_frame = ttk.LabelFrame(self, text="Persona / 维度", padding=5)
        right_frame.pack(side=tk.RIGHT, fill=tk.BOTH, expand=True, padx=5, pady=5)

        dim_frame = ttk.LabelFrame(right_frame, text="7维画像", padding=5)
        dim_frame.pack(fill=tk.BOTH, expand=True, pady=3)

        self.radar_canvas = tk.Canvas(dim_frame, width=220, height=220, bg="#fafafa", highlightthickness=0)
        self.radar_canvas.pack(side=tk.LEFT, padx=5)

        self.dim_vars = {}
        slider_frame = ttk.Frame(dim_frame)
        slider_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        for name in ["幽默", "温情", "包容", "创造力", "直率", "严肃", "任务导向"]:
            row = ttk.Frame(slider_frame)
            row.pack(fill=tk.X, pady=1)
            ttk.Label(row, text=name, width=6, anchor=tk.W).pack(side=tk.LEFT)
            var = tk.IntVar(value=50)
            ttk.Scale(row, from_=0, to=100, variable=var, orient=tk.HORIZONTAL,
                      command=lambda v, n=name: self._on_slider(n, int(float(v)))).pack(side=tk.LEFT, fill=tk.X, expand=True)
            ttk.Label(row, textvariable=var, width=3).pack(side=tk.LEFT)
            self.dim_vars[name] = var

        dim_btn_row = ttk.Frame(dim_frame)
        dim_btn_row.pack(fill=tk.X, pady=(5, 0))
        ttk.Button(dim_btn_row, text="保存维度", command=self._save_dimensions).pack(side=tk.LEFT, padx=2)
        ttk.Button(dim_btn_row, text="从选中日记校准", command=self._calibrate_from_diary).pack(side=tk.LEFT, padx=2)
        ttk.Button(dim_btn_row, text="自动校准(7天)", command=self._auto_calibrate).pack(side=tk.LEFT, padx=2)

        self._load_dimensions()

        persona_files = [
            ("基座人格", os.path.join(PROJECT_ROOT, "persona", "prompt_v1.txt")),
        ]
        for label, path in persona_files:
            frm = ttk.LabelFrame(right_frame, text=label, padding=5)
            frm.pack(fill=tk.BOTH, expand=True, pady=3)
            txt = tk.Text(frm, height=4, wrap=tk.WORD)
            txt.pack(fill=tk.BOTH, expand=True)
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8") as f:
                    content = f.read()[:1500]
                txt.insert(tk.END, content)
            else:
                txt.insert(tk.END, "(文件不存在)")
            txt.config(state=tk.DISABLED)

        rolling_path = os.path.join(PROJECT_ROOT, "memory", "rolling_summary.md")
        roll_frame = ttk.LabelFrame(right_frame, text="7日滚动总结", padding=5)
        roll_frame.pack(fill=tk.BOTH, expand=True, pady=3)

        btn_row = ttk.Frame(roll_frame)
        btn_row.pack(fill=tk.X, pady=(0, 3))
        ttk.Button(btn_row, text="刷新", command=self._refresh_rolling).pack(side=tk.LEFT, padx=2)
        ttk.Button(btn_row, text="压缩", command=self._compress_rolling).pack(side=tk.LEFT, padx=2)

        self.roll_txt = tk.Text(roll_frame, height=4, wrap=tk.WORD)
        self.roll_txt.pack(fill=tk.BOTH, expand=True)
        self._load_rolling()

    def _load_dimensions(self):
        try:
            from persona.dimensions import get_dimensions
            dims = get_dimensions()
            for d in dims:
                name = d["name"]
                if name in self.dim_vars:
                    self.dim_vars[name].set(d["value"])
            self._draw_radar()
        except Exception as e:
            print(f"[console] 加载维度失败: {e}")

    def _save_dimensions(self):
        try:
            from persona.dimensions import set_dimension
            for name, var in self.dim_vars.items():
                set_dimension(name, var.get())
            self._draw_radar()
            messagebox.showinfo("保存", "维度已保存到 state.json")
        except Exception as e:
            messagebox.showerror("保存失败", str(e))

    def _on_slider(self, name, value):
        self._draw_radar()

    def _draw_radar(self):
        canvas = self.radar_canvas
        canvas.delete("all")
        names = ["幽默", "温情", "包容", "创造力", "直率", "严肃", "任务导向"]
        n = len(names)
        cx, cy, r = 110, 110, 85
        for level in range(1, 6):
            lr = r * level / 5
            pts = []
            for i in range(n):
                angle = -math.pi / 2 + 2 * math.pi * i / n
                pts.extend([cx + lr * math.cos(angle), cy + lr * math.sin(angle)])
            canvas.create_polygon(pts, outline="#ddd", fill="", width=1)
        for i in range(n):
            angle = -math.pi / 2 + 2 * math.pi * i / n
            canvas.create_line(cx, cy, cx + r * math.cos(angle), cy + r * math.sin(angle), fill="#ddd")
        pts = []
        for i, name in enumerate(names):
            val = self.dim_vars.get(name, tk.IntVar(value=50)).get()
            angle = -math.pi / 2 + 2 * math.pi * i / n
            dr = r * val / 100
            pts.extend([cx + dr * math.cos(angle), cy + dr * math.sin(angle)])
        canvas.create_polygon(pts, outline="#536af5", fill="#d1d8ff", width=2)
        for i, name in enumerate(names):
            angle = -math.pi / 2 + 2 * math.pi * i / n
            val = self.dim_vars.get(name, tk.IntVar(value=50)).get()
            lx = cx + (r + 18) * math.cos(angle) - 15
            ly = cy + (r + 18) * math.sin(angle) - 8
            canvas.create_text(lx, ly, text=f"{name}\n{val}", font=("Microsoft YaHei", 7), fill="#333")

    def _calibrate_from_diary(self):
        sel = self.diary_list.curselection()
        if not sel:
            messagebox.showinfo("提示", "请先在左侧选中一篇日记")
            return
        fname = self.diary_list.get(sel[0])
        path = os.path.join(PROJECT_ROOT, "diary", fname)
        if not os.path.exists(path):
            return
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
        positive = sum(content.count(w) for w in ["开心", "高兴", "好", "爱", "笑", "温暖", "漂亮", "舒服", "棒"])
        negative = sum(content.count(w) for w in ["难过", "累", "哭", "崩溃", "痛", "烦", "焦虑", "怕", "讨厌"])
        if positive > negative * 1.5:
            if "温情" in self.dim_vars:
                self.dim_vars["温情"].set(min(95, self.dim_vars["温情"].get() + 3))
            if "幽默" in self.dim_vars:
                self.dim_vars["幽默"].set(min(95, self.dim_vars["幽默"].get() + 2))
        elif negative > positive * 1.5:
            if "包容" in self.dim_vars:
                self.dim_vars["包容"].set(min(95, self.dim_vars["包容"].get() + 3))
            if "严肃" in self.dim_vars:
                self.dim_vars["严肃"].set(max(5, self.dim_vars["严肃"].get() - 2))
        self._draw_radar()
        self._save_dimensions()
        messagebox.showinfo("校准完成", f"{fname}\n积极词{positive} 消极词{negative}\n维度已自动微调并保存。")

    def _auto_calibrate(self):
        try:
            from persona.dimensions import apply_calibration
            applied = apply_calibration()
            if applied:
                self._load_dimensions()
                msg = "\n".join(f"{k}: {v['from']} → {v['to']}" for k, v in applied.items())
                messagebox.showinfo("自动校准完成", f"以下维度已调整:\n{msg}")
            else:
                messagebox.showinfo("自动校准", "最近7天卡片不足，无法校准。")
        except Exception as e:
            messagebox.showerror("校准失败", str(e))

    def _load_rolling(self):
        rolling_path = os.path.join(PROJECT_ROOT, "memory", "rolling_summary.md")
        self.roll_txt.config(state=tk.NORMAL)
        self.roll_txt.delete("1.0", tk.END)
        if os.path.exists(rolling_path):
            with open(rolling_path, "r", encoding="utf-8") as f:
                self.roll_txt.insert(tk.END, f.read()[:2000])
        else:
            self.roll_txt.insert(tk.END, "(文件不存在)")
        self.roll_txt.config(state=tk.DISABLED)

    def _refresh_rolling(self):
        self._load_rolling()

    def _compress_rolling(self):
        rolling_path = os.path.join(PROJECT_ROOT, "memory", "rolling_summary.md")
        if not os.path.exists(rolling_path):
            messagebox.showinfo("提示", "rolling_summary.md 不存在")
            return
        with open(rolling_path, "r", encoding="utf-8") as f:
            raw = f.read().strip()
        if not raw:
            messagebox.showinfo("提示", "文件为空")
            return

        import threading
        def do_compress():
            try:
                from delegate_tools import delegate, atomic_write_text
                compress_prompt = (
                    "你是一个记忆压缩器。下面是主人最近几天的日记式生活叙事，"
                    "请把它压缩成一段 500 字以内的中文滚动总结（一段话，不分点）。"
                    "保留关键事件、重要承诺、情绪变化、关键日期，丢弃流水账。\n\n"
                    f"{raw}"
                )
                compressed = delegate(compress_prompt, "")
                if compressed and 20 < len(compressed) < 800:
                    atomic_write_text(rolling_path, compressed.strip() + "\n")
                else:
                    atomic_write_text(rolling_path, compressed[:500].strip() + "\n" if compressed else raw)
            except Exception as e:
                print(f"[console] 压缩失败: {e}")
            self.after(0, self._load_rolling)
        threading.Thread(target=do_compress, daemon=True).start()
        messagebox.showinfo("提示", "压缩已提交，稍后自动刷新")

    def _on_diary_select(self, event):
        sel = self.diary_list.curselection()
        if not sel:
            return
        fname = self.diary_list.get(sel[0])
        path = os.path.join(PROJECT_ROOT, "diary", fname)
        self.diary_text.config(state=tk.NORMAL)
        self.diary_text.delete("1.0", tk.END)
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                self.diary_text.insert(tk.END, f.read()[:5000])
        self.diary_text.config(state=tk.DISABLED)
