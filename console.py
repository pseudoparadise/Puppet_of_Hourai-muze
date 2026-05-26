"""
console.py — ghost-trigger 统一控制台
用法: python console.py
"""
import json
import os
import sys
import time
import socket
import sqlite3
import subprocess
import tkinter as tk
from tkinter import ttk, messagebox
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "memory"))

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(PROJECT_ROOT, "memory", "cards.db")
CONFIG_PATH = os.path.join(PROJECT_ROOT, "config.json")


class DashboardTab(ttk.Frame):
    def __init__(self, parent):
        super().__init__(parent)
        self.refresh()

    def refresh(self):
        for w in self.winfo_children():
            w.destroy()

        try:
            self._build()
        except Exception as e:
            ttk.Label(self, text=f"刷新失败: {e}", foreground="red",
                      font=("", 12, "bold")).pack(pady=40)

    def _build(self):
        # ── 服务控制栏 ──
        svc_frame = ttk.LabelFrame(self, text="服务控制", padding=10)
        svc_frame.pack(fill=tk.X, padx=10, pady=5)

        row_svc = ttk.Frame(svc_frame)
        row_svc.pack(fill=tk.X)

        # 守护进程
        daemon_pid = self._read_pid_file(".daemon.pid")
        daemon_alive = self._pid_alive(daemon_pid)
        daemon_label = f"守护进程: PID {daemon_pid}" if daemon_alive else "守护进程: 未启动"
        ttk.Label(row_svc, text=daemon_label,
                  foreground="green" if daemon_alive else "red").pack(side=tk.LEFT, padx=5)
        ttk.Button(row_svc, text="重启守护", command=self._restart_daemon).pack(side=tk.LEFT, padx=2)

        # 音乐轮询（检查 .music_state.json 最近 30s 是否更新过）
        ttk.Label(row_svc, text="|", foreground="#ccc").pack(side=tk.LEFT, padx=10)
        music_alive = self._file_recently_updated(".music_state.json", 30)
        music_label = "音乐轮询: 活跃" if music_alive else "音乐轮询: 无信号"
        ttk.Label(row_svc, text=music_label,
                  foreground="green" if music_alive else "red").pack(side=tk.LEFT, padx=5)

        # 轮询守护
        ttk.Label(row_svc, text="|", foreground="#ccc").pack(side=tk.LEFT, padx=10)
        poll_pid = self._read_pid_file(".polling_loop.pid")
        poll_alive = self._pid_alive(poll_pid)
        poll_label = f"轮询守护: PID {poll_pid}" if poll_alive else "轮询守护: 未启动"
        ttk.Label(row_svc, text=poll_label,
                  foreground="green" if poll_alive else "red").pack(side=tk.LEFT, padx=5)

        # 自动刷新
        ttk.Label(row_svc, text="|", foreground="#ccc").pack(side=tk.LEFT, padx=10)
        self.auto_refresh_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(row_svc, text="30s自动刷新", variable=self.auto_refresh_var,
                        command=self._toggle_auto).pack(side=tk.LEFT, padx=5)

        # ── 总览数字 ──
        stats_frame = ttk.LabelFrame(self, text="系统状态", padding=10)
        stats_frame.pack(fill=tk.X, padx=10, pady=5)

        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM cards WHERE review_status='final'")
        final = c.fetchone()[0]
        c.execute("SELECT COUNT(*) FROM cards WHERE review_status='final' AND resolved=0")
        active = c.fetchone()[0]
        c.execute("SELECT COUNT(*) FROM cards WHERE review_status='final' AND embedding IS NOT NULL")
        vec = c.fetchone()[0]
        try:
            c.execute("SELECT COUNT(*) FROM card_links")
            links = c.fetchone()[0]
        except Exception:
            links = 0
        conn.close()

        pending_path = os.path.join(PROJECT_ROOT, "memory", "pending_cards.json")
        pending = 0
        if os.path.exists(pending_path):
            with open(pending_path, "r", encoding="utf-8") as f:
                pending = len(json.load(f))

        row1 = ttk.Frame(stats_frame)
        row1.pack(fill=tk.X)
        for label, val in [("定稿卡片", final), ("活跃", active), ("已解决", final - active),
                            ("待审核", pending), ("有向量", vec), ("Link 边", links)]:
            frm = ttk.Frame(row1)
            frm.pack(side=tk.LEFT, padx=15)
            ttk.Label(frm, text=label, font=("", 9)).pack()
            ttk.Label(frm, text=str(val), font=("", 18, "bold")).pack()

        # ── 分类分布 ──
        cat_frame = ttk.LabelFrame(self, text="分类分布", padding=10)
        cat_frame.pack(fill=tk.X, padx=10, pady=5)

        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("SELECT category, COUNT(*) FROM cards WHERE review_status='final' GROUP BY category ORDER BY COUNT(*) DESC")
        cats = c.fetchall()
        conn.close()

        for cat_name, cnt in cats:
            bar_frame = ttk.Frame(cat_frame)
            bar_frame.pack(fill=tk.X, pady=1)
            ttk.Label(bar_frame, text=f"{cat_name:<16}", width=16, anchor=tk.W).pack(side=tk.LEFT)
            bar = ttk.Progressbar(bar_frame, length=300, maximum=max(1, cats[0][1]), value=cnt)
            bar.pack(side=tk.LEFT, padx=5)
            ttk.Label(bar_frame, text=str(cnt), width=4).pack(side=tk.LEFT)

        # ── 最近日记 ──
        diary_frame = ttk.LabelFrame(self, text="最近日记", padding=10)
        diary_frame.pack(fill=tk.X, padx=10, pady=5)

        diary_dir = os.path.join(PROJECT_ROOT, "diary")
        text = tk.Text(diary_frame, height=6, wrap=tk.WORD)
        text.pack(fill=tk.X)
        for d_offset in range(3):
            d = (datetime.now() - timedelta(days=d_offset)).strftime("%Y-%m-%d")
            md = os.path.join(diary_dir, f"{d}.md")
            ev = os.path.join(diary_dir, f"{d}_events.json")
            status_parts = []
            if os.path.exists(md):
                status_parts.append(f"diary ({os.path.getsize(md)}B)")
            if os.path.exists(ev):
                with open(ev, "r", encoding="utf-8") as f:
                    ev_data = json.load(f)
                comp = len(ev_data.get("completions", []))
                eis_count = sum(len(v) for v in ev_data.get("eisenhower", {}).values())
                status_parts.append(f"{comp}完成 {eis_count}待办")
            marker = " ← 今天" if d_offset == 0 else (" ← 昨天" if d_offset == 1 else "")
            text.insert(tk.END, f"{d}: {', '.join(status_parts) or '无'}{marker}\n")
        text.config(state=tk.DISABLED)

        # ── 实时音乐 ──
        music_frame = ttk.LabelFrame(self, text="实时音乐", padding=10)
        music_frame.pack(fill=tk.X, padx=10, pady=5)

        music_state = os.path.join(PROJECT_ROOT, ".music_state.json")
        ms = None
        if os.path.exists(music_state):
            try:
                with open(music_state, "r", encoding="utf-8") as f:
                    ms = json.load(f)
            except Exception:
                pass

        # 第一行：当前播放 + 歌词数 + 数据新鲜度
        row_m = ttk.Frame(music_frame)
        row_m.pack(fill=tk.X)
        try:
            if ms and ms.get("playing") and ms.get("song_name"):
                artist = ms.get("artist", "?") or "?"
                lc = len(ms.get("lyrics", []))
                freshness = ""
                try:
                    age = time.time() - os.path.getmtime(music_state)
                    freshness = f" ({int(age)}s前更新)"
                except Exception:
                    pass
                label = f"> {ms['song_name']} — {artist}  [{lc}句歌词]{freshness}"
                fg = "green" if lc > 0 else "orange"
            else:
                label = "■ 未在播放"
                fg = "gray"
        except Exception:
            label = "■ 读取失败"
            fg = "red"
        ttk.Label(row_m, text=label, foreground=fg, font=("", 10, "bold")).pack(side=tk.LEFT, padx=5)

        # 第二行：歌词预览
        lyrics_frame = ttk.Frame(music_frame)
        lyrics_frame.pack(fill=tk.X, pady=(5, 0))
        lyrics_text = tk.Text(lyrics_frame, height=4, wrap=tk.WORD)
        lyrics_text.pack(fill=tk.X)
        try:
            if ms and ms.get("lyrics"):
                started = ms.get("started_at", "")
                elapsed = 0
                if started:
                    try:
                        start_dt = datetime.fromisoformat(started)
                        elapsed = (datetime.now(timezone.utc) - start_dt).total_seconds()
                    except Exception:
                        pass
                idx = 0
                for i, l in enumerate(ms["lyrics"]):
                    if isinstance(l, dict) and l.get("seconds", 0) <= elapsed:
                        idx = i
                window = ms["lyrics"][idx:idx+4]
                for line in window:
                    if isinstance(line, dict):
                        marker = " -> " if line.get("seconds", 0) <= elapsed else "   "
                        lyrics_text.insert(tk.END, f"{marker}[{line.get('time','?')}] {line.get('text','')}\n")
        except Exception:
            pass
        lyrics_text.config(state=tk.DISABLED)

        # 第三行：最近播放历史
        music_history = os.path.join(PROJECT_ROOT, "memory", "music_history.jsonl")
        if os.path.exists(music_history):
            try:
                with open(music_history, "r", encoding="utf-8") as f:
                    recent_lines = f.readlines()[-3:]
                recents = []
                for line in recent_lines:
                    try:
                        e = json.loads(line.strip())
                        recents.append(f"{str(e.get('song','?'))[:15]}—{str(e.get('artist','?'))[:10]}")
                    except Exception:
                        pass
                if recents:
                    ttk.Label(music_frame, text="历史: " + "  |  ".join(recents),
                              foreground="gray", font=("", 8)).pack(anchor=tk.W, padx=5, pady=(3, 0))
            except Exception:
                pass

        # ── 刷新按钮 ──
        ttk.Button(self, text="刷新", command=self.refresh).pack(pady=5)

    def _check_port(self, port):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(0.5)
        result = s.connect_ex(("127.0.0.1", port))
        s.close()
        return result == 0

    def _file_recently_updated(self, filename, max_age_sec):
        path = os.path.join(PROJECT_ROOT, filename)
        try:
            return (time.time() - os.path.getmtime(path)) < max_age_sec
        except Exception:
            return False

    def _check_process(self, name):
        try:
            r = subprocess.run(["tasklist", "/fi", "imagename eq python.exe", "/fo", "csv"],
                               capture_output=True, text=True, timeout=5,
                               creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0)
            return name in r.stdout
        except Exception:
            return False

    def _read_pid_file(self, name):
        path = os.path.join(PROJECT_ROOT, name)
        try:
            with open(path, "r") as f:
                return int(f.read().strip())
        except Exception:
            return None

    def _pid_alive(self, pid):
        if pid is None:
            return False
        try:
            import ctypes
            SYNCHRONIZE = 0x00100000
            PROCESS_QUERY_LIMITED = 0x1000
            h = ctypes.windll.kernel32.OpenProcess(SYNCHRONIZE | PROCESS_QUERY_LIMITED, False, pid)
            if h:
                ctypes.windll.kernel32.CloseHandle(h)
                return True
            return False
        except Exception:
            return False

    def _restart_daemon(self):
        daemon_pid = self._read_pid_file(".daemon.pid")
        if daemon_pid and self._pid_alive(daemon_pid):
            try:
                subprocess.run(["taskkill", "/f", "/pid", str(daemon_pid)],
                               capture_output=True,
                               creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0)
            except Exception:
                pass
        # 干掉所有旧子进程
        for pid_name in [".polling_loop.pid"]:
            old = self._read_pid_file(pid_name)
            if old and self._pid_alive(old):
                try:
                    subprocess.run(["taskkill", "/f", "/pid", str(old)],
                                   capture_output=True,
                                   creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0)
                except Exception:
                    pass
        # 启动 daemon
        python = sys.executable
        subprocess.Popen(
            [python, "daemon.py"], cwd=PROJECT_ROOT,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
        self.after(3000, self.refresh)

    def _toggle_auto(self):
        if self.auto_refresh_var.get():
            self._auto_refresh()

    def _auto_refresh(self):
        if not self.auto_refresh_var.get():
            return
        try:
            self.refresh()
        except Exception:
            pass
        self.after(30000, self._auto_refresh)


class DiaryPersonaTab(ttk.Frame):
    def __init__(self, parent):
        super().__init__(parent)
        self.build()

    def build(self):
        # ── 日记列表 ──
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
                [f for f in os.listdir(diary_dir) if f.endswith(".md")],
                reverse=True
            )
            for f in diary_files:
                self.diary_list.insert(tk.END, f)
        except Exception as e:
            self.diary_list.insert(tk.END, f"(错误: {e})")

        # ── Persona ──
        right_frame = ttk.LabelFrame(self, text="Persona / 基座", padding=5)
        right_frame.pack(side=tk.RIGHT, fill=tk.BOTH, expand=True, padx=5, pady=5)

        persona_files = [
            ("动态人格", os.path.join(PROJECT_ROOT, "persona", "prompt_v1.txt")),
            ("基座人格", os.path.join(PROJECT_ROOT, "persona", "prompt_v1_base.txt")),
        ]
        for label, path in persona_files:
            frm = ttk.LabelFrame(right_frame, text=label, padding=5)
            frm.pack(fill=tk.BOTH, expand=True, pady=3)
            txt = tk.Text(frm, height=6, wrap=tk.WORD)
            txt.pack(fill=tk.BOTH, expand=True)
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8") as f:
                    content = f.read()[:2000]
                txt.insert(tk.END, content)
            else:
                txt.insert(tk.END, "(文件不存在)")
            txt.config(state=tk.DISABLED)

        # 滚动总结 + 操作按钮
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

        # 异步调用 DeepSeek 压缩
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
            # 回主线程刷新
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


class BarkLogTab(ttk.Frame):
    def __init__(self, parent):
        super().__init__(parent)
        self.build()

    def build(self):
        # ── Bark 推送记录 ──
        bark_frame = ttk.LabelFrame(self, text="Bark 推送记录", padding=5)
        bark_frame.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)

        btn_row = ttk.Frame(bark_frame)
        btn_row.pack(fill=tk.X, pady=(0, 3))
        ttk.Button(btn_row, text="刷新", command=self._refresh_bark).pack(side=tk.LEFT, padx=2)

        columns = ("time", "msg")
        self.bark_tree = ttk.Treeview(bark_frame, columns=columns, show="headings", height=8)
        self.bark_tree.heading("time", text="时间")
        self.bark_tree.heading("msg", text="内容")
        self.bark_tree.column("time", width=160)
        self.bark_tree.column("msg", width=500)
        self.bark_tree.pack(fill=tk.BOTH, expand=True)

        self._refresh_bark()

    def _refresh_bark(self):
        for item in self.bark_tree.get_children():
            self.bark_tree.delete(item)
        state_path = os.path.join(PROJECT_ROOT, "state.json")
        if os.path.exists(state_path):
            try:
                with open(state_path, "r", encoding="utf-8") as f:
                    state = json.load(f)
                for entry in state.get("recent_bark", [])[-20:]:
                    self.bark_tree.insert("", tk.END, values=(
                        entry.get("time", ""),
                        entry.get("msg", "")[:80],
                    ))
            except Exception:
                pass

        # ── 聊天日志 ──
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


class Console:
    def __init__(self, root):
        self.root = root
        self.root.title("ghost-trigger 控制台")
        self.root.geometry("1100x750")
        self.root.resizable(True, True)

        notebook = ttk.Notebook(root)
        notebook.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)

        # Tab 1: 总览
        self.dash_frame = ttk.Frame(notebook)
        notebook.add(self.dash_frame, text="总览")
        self.dashboard = DashboardTab(self.dash_frame)
        self.dashboard.pack(fill=tk.BOTH, expand=True)

        # Tab 2: 卡片管理
        self.card_frame = ttk.Frame(notebook)
        notebook.add(self.card_frame, text="卡片管理")
        from card_manager import CardManager
        self.card_mgr = CardManager(self.card_frame, standalone=False)

        # Tab 3: 待办管理
        self.todo_frame = ttk.Frame(notebook)
        notebook.add(self.todo_frame, text="待办事项")
        from todo_manager import TodoManager
        self.todo_mgr = TodoManager(self.todo_frame, standalone=False)

        # Tab 4: 日记 & Persona
        self.diary_frame = ttk.Frame(notebook)
        notebook.add(self.diary_frame, text="日记/Persona")
        self.diary_tab = DiaryPersonaTab(self.diary_frame)
        self.diary_tab.pack(fill=tk.BOTH, expand=True)

        # Tab 5: Bark & 日志
        self.log_frame = ttk.Frame(notebook)
        notebook.add(self.log_frame, text="Bark/日志")
        self.log_tab = BarkLogTab(self.log_frame)
        self.log_tab.pack(fill=tk.BOTH, expand=True)


if __name__ == "__main__":
    root = tk.Tk()
    app = Console(root)
    root.mainloop()
