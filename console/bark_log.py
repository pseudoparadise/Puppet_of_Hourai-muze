import json
import os
import tkinter as tk
from tkinter import ttk

from . import PROJECT_ROOT


class BarkLogTab(ttk.Frame):
    def __init__(self, parent):
        super().__init__(parent)
        self.build()

    def build(self):
        bark_frame = ttk.LabelFrame(self, text="Bark 推送记录", padding=5)
        bark_frame.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)

        btn_row = ttk.Frame(bark_frame)
        btn_row.pack(fill=tk.X, pady=(0, 3))
        ttk.Button(btn_row, text="刷新", command=self._refresh_bark).pack(side=tk.LEFT, padx=2)

        columns = ("time", "heat", "silence", "msg")
        self.bark_tree = ttk.Treeview(bark_frame, columns=columns, show="headings", height=8)
        self.bark_tree.heading("time", text="时间")
        self.bark_tree.heading("heat", text="热度")
        self.bark_tree.heading("silence", text="沉默")
        self.bark_tree.heading("msg", text="内容")
        self.bark_tree.column("time", width=100)
        self.bark_tree.column("heat", width=50)
        self.bark_tree.column("silence", width=55)
        self.bark_tree.column("msg", width=420)
        self.bark_tree.pack(fill=tk.BOTH, expand=True)

        self._refresh_bark()

        chat_frame = ttk.LabelFrame(self, text="最近聊天 (chat_logs.json)", padding=5)
        chat_frame.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)

        self.chat_text = tk.Text(chat_frame, height=12, wrap=tk.WORD, font=("Consolas", 9))
        self.chat_text.pack(fill=tk.BOTH, expand=True)

        chat_log_path = os.path.join(PROJECT_ROOT, "chat_logs.json")
        if os.path.exists(chat_log_path):
            entries = []
            with open(chat_log_path, "r", encoding="utf-8") as f:
                for line in f:
                    try:
                        entries.append(json.loads(line.strip()))
                    except json.JSONDecodeError:
                        pass
            for e in entries[-30:]:
                role = e.get("role", "?")
                ts = e.get("timestamp", "")[:19]
                content = str(e.get("content", ""))[:100]
                chord = f" [{e.get('chord', '')}]" if e.get("chord") else ""
                prefix = "You" if role == "user" else "DS" if role == "ghost" else role
                self.chat_text.insert(tk.END, f"[{ts}] {prefix}{chord}: {content}\n")
            self.chat_text.see(tk.END)
        self.chat_text.config(state=tk.DISABLED)

    def _refresh_bark(self):
        for item in self.bark_tree.get_children():
            self.bark_tree.delete(item)
        state_path = os.path.join(PROJECT_ROOT, "state.json")
        if os.path.exists(state_path):
            try:
                with open(state_path, "r", encoding="utf-8") as f:
                    state = json.load(f)
                for entry in state.get("recent_bark", [])[-20:]:
                    heat = entry.get("heat", "")
                    silence = f"{entry['silence']}m" if entry.get("silence") else ""
                    self.bark_tree.insert("", tk.END, values=(
                        entry.get("time", ""), heat, silence, entry.get("msg", "")[:80],
                    ))
            except Exception:
                pass
