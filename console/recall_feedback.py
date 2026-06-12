import json
import os
import sys
import tkinter as tk
from tkinter import ttk, messagebox
from datetime import datetime

from . import PROJECT_ROOT


class RecallFeedbackTab(ttk.Frame):
    TRACE_PATH = os.path.join(PROJECT_ROOT, "memory", "retrieval_traces.jsonl")

    def __init__(self, parent):
        super().__init__(parent)
        self._build()

    def _build(self):
        top = ttk.Frame(self)
        top.pack(fill=tk.X, padx=10, pady=5)
        ttk.Button(top, text="刷新列表", command=self._load).pack(side=tk.LEFT, padx=4, ipadx=6, ipady=2)
        ttk.Button(top, text="删除选中", command=self._delete_selected).pack(side=tk.LEFT, padx=4, ipadx=6, ipady=2)
        ttk.Button(top, text="清空全部", command=self._delete_all).pack(side=tk.LEFT, padx=4, ipadx=6, ipady=2)
        ttk.Button(top, text="应用反馈微调", command=self._apply).pack(side=tk.LEFT, padx=4, ipadx=6, ipady=2)
        self.auto_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(top, text="5轮自动微调", variable=self.auto_var).pack(side=tk.LEFT, padx=10)
        self.stats_label = tk.Label(top, text="", fg="#666", font=("Microsoft YaHei", 10))
        self.stats_label.pack(side=tk.RIGHT, padx=15)

        panes = ttk.PanedWindow(self, orient=tk.HORIZONTAL)
        panes.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)

        left = ttk.Frame(panes)
        panes.add(left, weight=1)
        self.tree = ttk.Treeview(left, columns=("ts", "query", "va", "count", "tag"), show="headings", height=20)
        self.tree.heading("ts", text="时间")
        self.tree.heading("query", text="查询")
        self.tree.heading("va", text="VA档")
        self.tree.heading("count", text="卡片数")
        self.tree.heading("tag", text="源")
        self.tree.column("ts", width=95)
        self.tree.column("query", width=110)
        self.tree.column("va", width=45)
        self.tree.column("count", width=45)
        self.tree.column("tag", width=55)
        self.tree.tag_configure("preflight", background="#71a2cb", foreground="white")
        self.tree.tag_configure("trigger", background="#2a39f4", foreground="white")
        self.tree.tag_configure("bark", background="#536af5", foreground="white")
        self.tree.configure(selectmode="extended")
        self.tree.pack(fill=tk.BOTH, expand=True)
        self.tree.bind("<<TreeviewSelect>>", self._on_select)

        right = ttk.Frame(panes)
        panes.add(right, weight=2)
        columns = ("title", "cat", "score", "kw", "sem", "imp", "anc", "dif", "rec", "dec", "fir", "wat", "gro", "fb")
        self.card_tree = ttk.Treeview(right, columns=columns, show="headings", height=18)
        self.card_tree.heading("title", text="标题")
        self.card_tree.heading("cat", text="分类")
        self.card_tree.heading("score", text="总分")
        for col in ["kw", "sem", "imp", "anc", "dif", "rec", "dec", "fir", "wat", "gro", "fb"]:
            self.card_tree.heading(col, text=col)
        for col in columns:
            self.card_tree.column(col, width=45 if col not in ("title", "fb") else (80 if col == "title" else 50))
        self.card_tree.pack(fill=tk.BOTH, expand=True)

        bottom = ttk.Frame(right)
        bottom.pack(fill=tk.X, pady=5)
        ttk.Button(bottom, text="✓ 准确", command=lambda: self._mark("good")).pack(side=tk.LEFT, padx=4, ipadx=8, ipady=2)
        ttk.Button(bottom, text="✗ 不准", command=lambda: self._mark("bad")).pack(side=tk.LEFT, padx=4, ipadx=8, ipady=2)
        ttk.Button(bottom, text="清除标记", command=lambda: self._mark(None)).pack(side=tk.LEFT, padx=4, ipadx=8, ipady=2)
        self.fb_status = tk.Label(bottom, text="", fg="#666", font=("Microsoft YaHei", 10))
        self.fb_status.pack(side=tk.LEFT, padx=10)

        qa_frame = ttk.LabelFrame(right, text="对话上下文", padding=5)
        qa_frame.pack(fill=tk.BOTH, expand=True, pady=5)
        self.qa_text = tk.Text(qa_frame, height=6, wrap=tk.WORD, font=("", 9))
        self.qa_text.pack(fill=tk.BOTH, expand=True)
        self.qa_text.config(state=tk.DISABLED)

        wt_frame = ttk.LabelFrame(right, text="检索权重微调", padding=5)
        wt_frame.pack(fill=tk.X, pady=5)
        wt_canvas = tk.Canvas(wt_frame, height=100, highlightthickness=0, bg="#fafafa")
        wt_scroll = ttk.Scrollbar(wt_frame, orient=tk.HORIZONTAL, command=wt_canvas.xview)
        wt_inner = ttk.Frame(wt_canvas)
        wt_canvas.create_window((0, 0), window=wt_inner, anchor=tk.NW)
        wt_canvas.configure(xscrollcommand=wt_scroll.set)
        wt_canvas.pack(fill=tk.X)
        wt_scroll.pack(fill=tk.X)

        self._wt_vars = {}
        all_labels = {
            "w_semantic": ("语义", 0.0, 3.0, 0.02), "w_keyword": ("关键词", 0.0, 3.0, 0.02),
            "w_importance": ("重要度", 0.0, 3.0, 0.01), "w_anchor": ("锚定×.03", 0.0, 0.50, 0.002),
            "w_recency": ("时间×.03", 0.0, 1.0, 0.005), "w_diffusion": ("扩散×.03", 0.0, 0.50, 0.002),
            "w_decay": ("衰减×.03", 0.0, 0.50, 0.002), "w_presence_penalty": ("出现扣", 0.0, 0.30, 0.005),
            "w_repetition_penalty": ("重复扣", 0.0, 0.40, 0.005), "w_frequency_penalty": ("频次扣", 0.0, 0.10, 0.002),
            "frequency_penalty_cap": ("扣分上限", 0.0, 0.50, 0.005), "teleport_rate": ("传送", 0.0, 0.50, 0.005),
        }
        for key, (label, vmin, vmax, step) in all_labels.items():
            f = ttk.Frame(wt_inner)
            f.pack(side=tk.LEFT, padx=2)
            ttk.Label(f, text=label, font=("", 7)).pack()
            var = tk.DoubleVar()
            ttk.Spinbox(f, textvariable=var, from_=vmin, to=vmax, increment=step, width=6).pack()
            self._wt_vars[key] = var
        wt_inner.update_idletasks()
        wt_canvas.configure(scrollregion=wt_canvas.bbox("all"))

        btn_row = ttk.Frame(wt_frame)
        btn_row.pack(fill=tk.X, pady=(3, 0))
        ttk.Button(btn_row, text="加载当前值", command=self._load_weights).pack(side=tk.LEFT, padx=3)
        ttk.Button(btn_row, text="保存并生效", command=self._save_weights).pack(side=tk.LEFT, padx=3)
        ttk.Button(btn_row, text="恢复默认", command=self._reset_weights).pack(side=tk.LEFT, padx=3)
        self._wt_status = tk.Label(btn_row, text="", fg="#666", font=("", 8))
        self._wt_status.pack(side=tk.LEFT, padx=8)

        self._traces = {}
        self._load()

    def _load(self):
        self._traces = {}
        for item in self.tree.get_children():
            self.tree.delete(item)
        for item in self.card_tree.get_children():
            self.card_tree.delete(item)

        if not os.path.exists(self.TRACE_PATH):
            self.stats_label.config(text="暂无检索记录")
            return

        good = bad = 0
        try:
            with open(self.TRACE_PATH, "r", encoding="utf-8") as f:
                for line in f:
                    try:
                        t = json.loads(line.strip())
                        if "cards" not in t:
                            continue
                        tid = t["trace_id"]
                        self._traces[tid] = t
                        cards = t["cards"]
                        for c in cards:
                            if c.get("feedback") == "good":
                                good += 1
                            elif c.get("feedback") == "bad":
                                bad += 1
                        tag = t.get("tag", "")
                        TAG_LABELS = {"preflight": "[家]", "trigger": "[工位]", "bark": "[bark]", "phantom_cli": "[手动]"}
                        tag_label = TAG_LABELS.get(tag, tag)
                        self.tree.insert("", 0, values=(
                            t.get("ts", "")[:16], t.get("query", "")[:50],
                            t.get("va_tier", "?"), len(cards), tag_label,
                        ), iid=tid, tags=(tag,) if tag else ())
                    except json.JSONDecodeError:
                        pass
                    except KeyError:
                        pass
        except Exception as e:
            messagebox.showerror("召回反馈异常", f"读取 trace 文件失败:\n{e}")
            self.stats_label.config(text=f"加载失败: {e}")
            return

        self.stats_label.config(text=f"✓{good}张  ✗{bad}张  |  共{len(self._traces)}轮检索")

    def _on_select(self, event):
        sel = self.tree.selection()
        if not sel:
            return
        tid = sel[0]
        trace = self._traces.get(tid)
        if not trace:
            return

        for item in self.card_tree.get_children():
            self.card_tree.delete(item)
        for c in trace.get("cards", []):
            p = c.get("probes", {})
            fb = c.get("feedback", "")
            fb_str = "✓" if fb == "good" else ("✗" if fb == "bad" else "")
            self.card_tree.insert("", tk.END, values=(
                c.get("title", "")[:20], c.get("category", ""),
                f"{c.get('score', 0):.2f}",
                f"{p.get('keyword', 0):.2f}", f"{p.get('semantic', 0):.2f}",
                f"{p.get('importance', 0):.2f}", f"{p.get('anchor', 0):.2f}",
                f"{p.get('diffusion', 0):.2f}", f"{p.get('recency', 0):.2f}",
                f"{p.get('decay', 0):.2f}", f"{p.get('fire', 0):.2f}",
                f"{p.get('water', 0):.2f}", f"{p.get('growth', 0):.2f}",
                fb_str), iid=c["id"])
        self._show_qa_context(trace)

    def _mark(self, fb_val):
        sel_tid = self.tree.selection()
        sel_cid = self.card_tree.selection()
        if not sel_tid or not sel_cid:
            self.fb_status.config(text="请先选中检索轮次和卡片")
            return

        tid = sel_tid[0]
        card_id = sel_cid[0]
        is_good = fb_val == "good"

        from memory.retriever import record_feedback
        record_feedback(tid, card_id, is_good)

        if not hasattr(self, '_auto_count'):
            self._auto_count = 0
        self._auto_count += 1
        label = "✓" if fb_val == "good" else ("✗" if fb_val == "bad" else "○")
        msg = f"已标记 {label} → {card_id} (第{self._auto_count}张)"
        self.fb_status.config(text=msg)

        if self.auto_var.get() and self._auto_count >= 5:
            self.fb_status.config(text=f"{msg} — 自动触发微调...")
            self._auto_count = 0
            self.after(200, self._apply_auto)
        self._load()

    def _apply_auto(self):
        from memory.retriever import apply_feedback_adjustments, get_effective_weights
        result = apply_feedback_adjustments()
        eff = get_effective_weights()
        self.fb_status.config(
            text=f"自动微调完成: ✓{result['good']}张 ✗{result['bad']}张 | "
                 f"kw={eff.get('w_keyword','?'):.2f} sem={eff.get('w_semantic','?'):.2f}")
        self._load()

    def _show_qa_context(self, trace: dict):
        self.qa_text.config(state=tk.NORMAL)
        self.qa_text.delete("1.0", tk.END)
        query = trace.get("query", "")
        ts = trace.get("ts", "")

        chat_path = os.path.join(PROJECT_ROOT, "chat_logs.json")
        if not os.path.exists(chat_path):
            self.qa_text.insert(tk.END, "(chat_logs.json 不存在)")
            self.qa_text.config(state=tk.DISABLED)
            return

        entries = []
        with open(chat_path, "r", encoding="utf-8") as f:
            for line in f:
                try:
                    entries.append(json.loads(line.strip()))
                except json.JSONDecodeError:
                    pass

        best_idx = -1
        for i, e in enumerate(entries):
            content = str(e.get("content", ""))
            if query and len(query) >= 8 and query[:30] in content:
                best_idx = i
                break

        if best_idx < 0 and ts and entries:
            try:
                trace_dt = datetime.strptime(ts[:19], "%Y-%m-%d %H:%M:%S")
                best_idx = len(entries) - 1
                best_dist = 999999
                for i, e in enumerate(entries):
                    ets = e.get("timestamp", "")
                    if not ets:
                        continue
                    try:
                        entry_dt = datetime.fromisoformat(ets.replace("+0000", "+00:00").replace("Z", "+00:00"))
                        dist = abs((trace_dt - entry_dt.replace(tzinfo=None)).total_seconds())
                        if dist < best_dist:
                            best_dist = dist
                            best_idx = i
                    except Exception:
                        pass
            except Exception:
                pass

        if best_idx < 0:
            self.qa_text.insert(tk.END, f"(未找到匹配对话)\n查询: {query}")
            self.qa_text.config(state=tk.DISABLED)
            return

        start = max(0, best_idx - 2)
        end = min(len(entries), best_idx + 4)
        for i in range(start, end):
            e = entries[i]
            role = "沐泽" if e.get("role") == "user" else ("DS" if e.get("role") == "ghost" else e.get("role", "?"))
            ts_short = e.get("timestamp", "")[:19]
            content = str(e.get("content", ""))[:200]
            marker = " →" if i == best_idx else "  "
            self.qa_text.insert(tk.END, f"{marker}[{ts_short}] {role}:\n{content}\n\n")
        self.qa_text.config(state=tk.DISABLED)

    def _apply(self):
        if not messagebox.askyesno("确认", "根据所有已标记反馈微调探针权重？"):
            return
        from memory.retriever import apply_feedback_adjustments, get_effective_weights
        result = apply_feedback_adjustments()
        eff = get_effective_weights()
        msg = f"✓{result['good']}张 ✗{result['bad']}张\n\n当前有效权重:\n"
        for k in ["w_keyword", "w_semantic", "w_importance", "w_anchor", "w_diffusion", "w_recency", "w_fire", "w_water"]:
            msg += f"  {k}: {eff.get(k, '?')}\n"
        messagebox.showinfo("微调完成", msg)
        self._load()

    def _delete_selected(self):
        selected = self.tree.selection()
        if not selected:
            messagebox.showinfo("未选中", "请在左侧列表选中要删除的 trace")
            return
        if not messagebox.askyesno("确认删除", f"确定删除选中的 {len(selected)} 条检索记录吗？"):
            return
        for tid in selected:
            self._traces.pop(tid, None)
        self._rewrite_traces()
        self._load()

    def _delete_all(self):
        if not self._traces:
            return
        if not messagebox.askyesno("确认清空", f"确定删除全部 {len(self._traces)} 条检索记录吗？"):
            return
        self._traces.clear()
        self._rewrite_traces()
        self._load()

    def _rewrite_traces(self):
        if not os.path.exists(self.TRACE_PATH):
            return
        to_keep = set(self._traces.keys())
        lines = []
        with open(self.TRACE_PATH, "r", encoding="utf-8") as f:
            for line in f:
                try:
                    t = json.loads(line.strip())
                    if t.get("trace_id") in to_keep:
                        lines.append(line)
                except Exception:
                    pass
        with open(self.TRACE_PATH, "w", encoding="utf-8") as f:
            for line in lines:
                f.write(line)

    def _load_weights(self):
        try:
            from memory.retriever import get_effective_weights, SCORING_CONFIG
            eff = get_effective_weights()
            for key, var in self._wt_vars.items():
                val = eff.get(key, SCORING_CONFIG.get(key, 0))
                var.set(round(val, 3))
            self._wt_status.config(text="已加载", fg="#666")
        except Exception as e:
            self._wt_status.config(text=f"加载失败: {e}", fg="red")

    def _save_weights(self):
        try:
            path = os.path.join(os.path.dirname(__file__), "..", "memory", "retrieval_weights.json")
            path = os.path.normpath(path)
            data = {}
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
            for key, var in self._wt_vars.items():
                data[key] = round(var.get(), 4)
            from delegate_tools import atomic_write_json
            atomic_write_json(path, data)
            self._wt_status.config(text="已保存，下次检索生效", fg="#4caf50")
        except Exception as e:
            self._wt_status.config(text=f"保存失败: {e}", fg="red")

    def _reset_weights(self):
        if not messagebox.askyesno("恢复默认", "确定恢复所有检索权重到默认值吗？"):
            return
        try:
            from memory.retriever import SCORING_CONFIG
            for key, var in self._wt_vars.items():
                var.set(round(SCORING_CONFIG.get(key, 0), 3))
            self._wt_status.config(text="已填入默认值，请点「保存并生效」", fg="#ff9800")
        except Exception as e:
            self._wt_status.config(text=f"恢复失败: {e}", fg="red")
