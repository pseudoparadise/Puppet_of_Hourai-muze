import os
import re
import threading
import tkinter as tk
from tkinter import ttk, messagebox
from datetime import datetime, timedelta

from . import PROJECT_ROOT


class WorkLogTab(ttk.Frame):
    def __init__(self, parent):
        super().__init__(parent)
        self._busy = False
        self.build()

    def build(self):
        ctrl_frame = ttk.Frame(self)
        ctrl_frame.pack(fill=tk.X, padx=5, pady=5)

        ttk.Button(ctrl_frame, text="从 Claude 会话提取", command=self._extract_sessions).pack(side=tk.LEFT, padx=2)
        ttk.Button(ctrl_frame, text="从 chat 重新生成", command=self._regenerate).pack(side=tk.LEFT, padx=2)
        ttk.Button(ctrl_frame, text="刷新", command=self._refresh).pack(side=tk.LEFT, padx=2)
        self._busy_label = ttk.Label(ctrl_frame, text="", foreground="gray")
        self._busy_label.pack(side=tk.LEFT, padx=10)

        panes = ttk.Panedwindow(self, orient=tk.HORIZONTAL)
        panes.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)

        left_frame = ttk.Frame(panes)
        panes.add(left_frame, weight=1)

        self._file_list = tk.Listbox(left_frame, width=22)
        self._file_list.pack(fill=tk.BOTH, expand=True)
        self._file_list.bind("<<ListboxSelect>>", self._on_select)

        right_frame = ttk.Frame(panes)
        panes.add(right_frame, weight=3)

        self._text = tk.Text(right_frame, wrap=tk.WORD, font=("Consolas", 10))
        self._text.pack(fill=tk.BOTH, expand=True)

        scrollbar = ttk.Scrollbar(self._text, command=self._text.yview)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self._text.config(yscrollcommand=scrollbar.set)

        status_frame = ttk.Frame(self)
        status_frame.pack(fill=tk.X, padx=5, pady=(0, 5))
        self._status_label = ttk.Label(status_frame, text="", foreground="gray")
        self._status_label.pack(side=tk.LEFT, padx=5)

        self._refresh()

    def _set_busy(self, busy: bool, text: str = ""):
        self._busy = busy
        self._busy_label.config(text=text)

    def _refresh(self):
        self._file_list.delete(0, tk.END)
        work_dir = os.path.join(PROJECT_ROOT, "diary", "work")
        files = []
        if os.path.isdir(work_dir):
            for f in os.listdir(work_dir):
                m = re.match(r"(\d{4}-\d{2}-\d{2})_work\.md$", f)
                if m:
                    files.append((m.group(1), f))
        files.sort(reverse=True)

        if not files:
            self._file_list.insert(tk.END, "(无工作日志)")
            return

        today = datetime.now().strftime("%Y-%m-%d")
        for date_str, fname in files:
            label = f"{date_str}  <- 今天" if date_str == today else date_str
            self._file_list.insert(tk.END, label)

        self._files = files

        if files and files[0][0] == today:
            self._file_list.selection_set(0)
            self._show_file(files[0][1])

    def _on_select(self, event):
        sel = self._file_list.curselection()
        if not sel or not hasattr(self, "_files"):
            return
        idx = sel[0]
        if idx < len(self._files):
            self._show_file(self._files[idx][1])

    def _show_file(self, fname: str):
        path = os.path.join(PROJECT_ROOT, "diary", "work", fname)
        self._text.config(state=tk.NORMAL)
        self._text.delete("1.0", tk.END)
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                self._text.insert(tk.END, f.read())
        else:
            self._text.insert(tk.END, "(文件不存在)")
        self._text.config(state=tk.DISABLED)

    def _selected_date(self):
        sel = self._file_list.curselection()
        if sel and hasattr(self, "_files") and sel[0] < len(self._files):
            return self._files[sel[0]][0]
        return datetime.now().strftime("%Y-%m-%d")

    def _extract_sessions(self):
        if self._busy:
            return
        date_str = self._selected_date()
        self._set_busy(True, "提取中...")
        self._status_label.config(text=f"正在从 Claude Code session 提取 {date_str}...", foreground="orange")

        def run():
            try:
                from work_log import from_claude_sessions
                n = from_claude_sessions(date_str)
                self.after(0, lambda: self._on_extract_done(date_str, n))
            except Exception as e:
                import traceback
                traceback.print_exc()
                self.after(0, lambda: self._on_extract_error(str(e)))

        threading.Thread(target=run, daemon=True).start()

    def _regenerate(self):
        if self._busy:
            return
        date_str = self._selected_date()
        self._set_busy(True, "生成中...")
        self._status_label.config(text=f"正在从 chat_logs_work.json 重新生成 {date_str}...", foreground="orange")

        def run():
            try:
                from work_log import from_chat
                n = from_chat(date_str, force=True)
                self.after(0, lambda: self._on_extract_done(date_str, n))
            except Exception as e:
                import traceback
                traceback.print_exc()
                self.after(0, lambda: self._on_extract_error(str(e)))

        threading.Thread(target=run, daemon=True).start()

    def _on_extract_done(self, date_str: str, n: int):
        self._set_busy(False, "")
        if n > 0:
            self._status_label.config(text=f"{date_str} 已提取 {n} 条工作记录", foreground="green")
        else:
            self._status_label.config(text=f"{date_str} 无新对话或提取失败", foreground="gray")
        self._refresh()

    def _on_extract_error(self, err: str):
        self._set_busy(False, "")
        self._status_label.config(text=f"错误: {err[:80]}", foreground="red")
        try:
            from daemon.panic import panic_popup
            panic_popup("工作日志提取", err[:200])
        except Exception:
            pass
